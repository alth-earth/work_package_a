"""Archive consistency checks used before replay and demonstrations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctic_route_data.bundle import record_provenance_id
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import QUALITY_RANK, ManifestRecord, QualityFlag
from arctic_route_data.timeutils import parse_utc


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checked: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def inspect_archive(
    data_root: str | Path,
    manifest: ManifestStore | None = None,
    *,
    allow_empty: bool = False,
) -> DoctorReport:
    root = Path(data_root).resolve()
    store = manifest or ManifestStore(root / "manifest" / "manifest.sqlite3")
    errors: list[str] = []
    warnings: list[str] = []
    records = store.all_records()
    seen_paths: set[str] = set()
    checksum_cache: dict[Path, str] = {}
    snapshot_index = _source_snapshot_index(root)
    for record in records:
        try:
            path = record.absolute_path(root)
        except ValueError as exc:
            errors.append(f"{record.data_id}: {exc}")
            continue
        if not path.is_file():
            errors.append(f"{record.data_id}: 文件不存在 {path}")
            continue
        if _cached_sha256(path, checksum_cache) != record.checksum:
            errors.append(f"{record.data_id}: SHA-256 不匹配")
        if record.relative_path in seen_paths:
            warnings.append(f"多个记录共用文件: {record.relative_path}")
        seen_paths.add(record.relative_path)
        _inspect_raw_archive(
            root,
            record,
            errors=errors,
            checksum_cache=checksum_cache,
        )
        _inspect_source_snapshot(
            record,
            root=root,
            snapshot_index=snapshot_index,
            errors=errors,
            checksum_cache=checksum_cache,
        )
    ready_root = root / "ready"
    for path in ready_root.rglob("*") if ready_root.is_dir() else ():
        if (
            path.is_file()
            and path.name != ".gitkeep"
            and path.relative_to(root).as_posix() not in seen_paths
        ):
            errors.append(f"ready 存在未登记文件: {path}")
    incoming_root = root / "incoming"
    pending = (
        [
            path
            for path in incoming_root.iterdir()
            if path.is_file() and path.name != ".gitkeep"
        ]
        if incoming_root.is_dir()
        else []
    )
    if pending:
        warnings.append(f"incoming 仍有 {len(pending)} 个待处理或孤立文件")
    if not records:
        message = "manifest 当前为空"
        if allow_empty:
            warnings.append(message)
        else:
            errors.append(message)
    return DoctorReport(len(records), tuple(errors), tuple(warnings))


_RAW_BINDING_FIELDS = ("publication_id", "upstream_checksum", "upstream_size_bytes")
_GFS_SNAPSHOT_ID = re.compile(r"gfs-(\d{8}T\d{2}Z)-([0-9a-f]+)\Z")


def verified_archived_provenance_id(
    root: str | Path,
    record: ManifestRecord,
    *,
    checksum_cache: dict[Path, str] | None = None,
) -> str | None:
    """Return provenance only after verifying the record's archived evidence.

    A manifest row and its metadata are declarations, not proof.  Formal AB
    completeness therefore requires the ready payload plus either the declared
    native source snapshot or the immutable raw payload/sidecar pair to verify
    against files below ``data_root``.
    """

    provenance_id = record_provenance_id(record)
    if provenance_id is None:
        return None
    archive_root = Path(root).resolve()
    cache = checksum_cache if checksum_cache is not None else {}
    try:
        ready_path = record.absolute_path(archive_root)
        if (
            not ready_path.is_file()
            or ready_path.stat().st_size != record.size_bytes
            or _cached_sha256(ready_path, cache) != record.checksum
        ):
            return None
    except OSError:
        return None

    # A declared native binding must verify the exact source snapshot.  Do not
    # silently fall back to raw evidence if the stronger declaration is broken.
    if record.metadata.get("source_file_checksum") is not None:
        errors: list[str] = []
        _inspect_source_snapshot(
            record,
            root=archive_root,
            snapshot_index=(
                {}
                if record.metadata.get("source_snapshot_relative_path") is not None
                else _source_snapshot_index(archive_root)
            ),
            errors=errors,
            checksum_cache=cache,
        )
        return provenance_id if not errors else None

    errors = []
    _inspect_raw_archive(
        archive_root,
        record,
        errors=errors,
        checksum_cache=cache,
    )
    return provenance_id if not errors else None


def _cached_sha256(path: Path, cache: dict[Path, str]) -> str:
    resolved = path.resolve()
    if resolved not in cache:
        cache[resolved] = sha256_file(resolved)
    return cache[resolved]


def _inspect_raw_archive(
    root: Path,
    record: ManifestRecord,
    *,
    errors: list[str],
    checksum_cache: dict[Path, str],
) -> None:
    """Verify the immutable producer payload/sidecar pair for a manifest row."""

    raw_root = (root / "raw").resolve()
    raw_directory = (
        raw_root / record.route_id / record.data_type / record.data_id
    ).resolve()
    declared = [field for field in _RAW_BINDING_FIELDS if field in record.metadata]
    if not raw_directory.is_dir():
        # Records created by the legacy/direct ingestion API predate raw-binding
        # declarations. Keep them readable; any record that claims such a binding
        # must have the archived evidence it declares.
        if declared:
            errors.append(f"{record.data_id}: raw 归档目录不存在 {raw_directory}")
        return
    if not raw_directory.is_relative_to(raw_root):
        errors.append(f"{record.data_id}: raw 归档路径逃逸")
        return

    sidecars = sorted(raw_directory.glob("*.metadata.json"))
    if len(sidecars) != 1:
        errors.append(
            f"{record.data_id}: raw 归档必须恰有一个 sidecar，实际 {len(sidecars)} 个"
        )
        return
    sidecar_path = sidecars[0]
    if not sidecar_path.resolve().is_relative_to(raw_directory):
        errors.append(f"{record.data_id}: raw sidecar 路径逃逸")
        return
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{record.data_id}: raw sidecar 无法读取: {exc}")
        return
    if not isinstance(sidecar, dict):
        errors.append(f"{record.data_id}: raw sidecar 顶层必须是 JSON object")
        return

    payload_name = sidecar.get("file")
    if not isinstance(payload_name, str) or not payload_name.strip():
        errors.append(f"{record.data_id}: raw sidecar.file 必须是非空字符串")
        return
    relative_payload = Path(payload_name)
    payload_path = (raw_directory / relative_payload).resolve()
    if (
        relative_payload.is_absolute()
        or ".." in relative_payload.parts
        or not payload_path.is_relative_to(raw_directory)
    ):
        errors.append(f"{record.data_id}: raw sidecar.file 路径逃逸")
        return
    if not payload_path.is_file():
        errors.append(f"{record.data_id}: raw payload 不存在 {payload_path}")
        return

    archived_files = {
        path.resolve()
        for path in raw_directory.iterdir()
        if path.is_file() and path.name != ".gitkeep"
    }
    expected_files = {sidecar_path.resolve(), payload_path}
    unexpected = sorted(path.name for path in archived_files - expected_files)
    if unexpected:
        errors.append(
            f"{record.data_id}: raw 归档包含未绑定文件: {', '.join(unexpected)}"
        )

    payload_size = sidecar.get("payload_size_bytes")
    payload_checksum = sidecar.get("payload_sha256")
    if not isinstance(payload_size, int) or isinstance(payload_size, bool):
        errors.append(f"{record.data_id}: raw sidecar.payload_size_bytes 无效")
    elif payload_size != payload_path.stat().st_size:
        errors.append(f"{record.data_id}: raw payload 大小与 sidecar 不匹配")
    if not _is_sha256(payload_checksum):
        errors.append(f"{record.data_id}: raw sidecar.payload_sha256 无效")
    elif _cached_sha256(payload_path, checksum_cache) != payload_checksum:
        errors.append(f"{record.data_id}: raw payload SHA-256 与 sidecar 不匹配")

    _check_sidecar_record_binding(sidecar, record, errors=errors)


def _check_sidecar_record_binding(
    sidecar: dict[str, Any], record: ManifestRecord, *, errors: list[str]
) -> None:
    identity = {
        "route_id": record.route_id,
        "data_type": record.data_type,
        "source": record.source,
        "version": record.version,
    }
    for field, expected in identity.items():
        if sidecar.get(field) != expected:
            errors.append(
                f"{record.data_id}: raw sidecar.{field} 与 manifest 不一致"
            )
    for field, expected in (
        ("issue_time", record.issue_time),
        ("valid_time", record.valid_time),
    ):
        try:
            actual = parse_utc(sidecar.get(field), field=f"sidecar.{field}")
        except (TypeError, ValueError) as exc:
            errors.append(f"{record.data_id}: raw sidecar.{field} 无效: {exc}")
        else:
            if actual != expected:
                errors.append(
                    f"{record.data_id}: raw sidecar.{field} 与 manifest 不一致"
                )

    record_publication = record.metadata.get("publication_id")
    if record_publication is not None and sidecar.get("publication_id") != record_publication:
        errors.append(f"{record.data_id}: raw publication_id 与 manifest 不一致")
    upstream_checksum = record.metadata.get("upstream_checksum")
    if upstream_checksum is not None and sidecar.get("payload_sha256") != upstream_checksum:
        errors.append(f"{record.data_id}: raw payload checksum 与 manifest 绑定不一致")
    upstream_size = record.metadata.get("upstream_size_bytes")
    if upstream_size is not None and sidecar.get("payload_size_bytes") != upstream_size:
        errors.append(f"{record.data_id}: raw payload size 与 manifest 绑定不一致")

    try:
        sidecar_quality = QualityFlag(sidecar.get("quality_flag"))
    except (TypeError, ValueError):
        errors.append(f"{record.data_id}: raw sidecar.quality_flag 无效")
    else:
        if QUALITY_RANK[record.quality_flag] > QUALITY_RANK[sidecar_quality]:
            errors.append(
                f"{record.data_id}: manifest 质量等级高于 raw sidecar 证据上限"
            )

    raw_metadata = sidecar.get("metadata")
    if not isinstance(raw_metadata, dict):
        errors.append(f"{record.data_id}: raw sidecar.metadata 必须是 object")
        return
    for field in ("source_snapshot_id", "source_file", "source_file_checksum"):
        expected = record.metadata.get(field)
        if expected is not None and raw_metadata.get(field) != expected:
            errors.append(
                f"{record.data_id}: raw sidecar.metadata.{field} 与 manifest 不一致"
            )


def _source_snapshot_index(root: Path) -> dict[str, tuple[Path, ...]]:
    snapshot_root = (root / "source_snapshots").resolve()
    if not snapshot_root.is_dir():
        return {}
    indexed: dict[str, list[Path]] = {}
    for path in snapshot_root.rglob("*"):
        resolved = path.resolve()
        if (
            path.is_file()
            and path.name != ".gitkeep"
            and not path.name.endswith(".part")
            and resolved.is_relative_to(snapshot_root)
        ):
            indexed.setdefault(path.name, []).append(resolved)
    return {name: tuple(paths) for name, paths in indexed.items()}


def _inspect_source_snapshot(
    record: ManifestRecord,
    *,
    root: Path,
    snapshot_index: dict[str, tuple[Path, ...]],
    errors: list[str],
    checksum_cache: dict[Path, str],
) -> None:
    declared_checksum = record.metadata.get("source_file_checksum")
    if declared_checksum is None:
        return
    if not _is_sha256(declared_checksum):
        errors.append(f"{record.data_id}: metadata.source_file_checksum 无效")
        return
    source_file = record.metadata.get("source_file")
    snapshot_id = record.metadata.get("source_snapshot_id")
    if not isinstance(source_file, str) or not source_file.strip():
        errors.append(
            f"{record.data_id}: 声明 source_file_checksum 时必须声明 source_file"
        )
        return
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        errors.append(
            f"{record.data_id}: 声明 source_file_checksum 时必须声明 source_snapshot_id"
        )
        return
    declared_relative = record.metadata.get("source_snapshot_relative_path")
    if declared_relative is not None:
        if not isinstance(declared_relative, str) or not declared_relative.strip():
            errors.append(
                f"{record.data_id}: metadata.source_snapshot_relative_path 无效"
            )
            return
        relative = Path(declared_relative)
        source_root = (root / "source_snapshots").resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) < 3
            or relative.parts[0] != "source_snapshots"
        ):
            errors.append(
                f"{record.data_id}: metadata.source_snapshot_relative_path 路径无效"
            )
            return
        exact = (root / relative).resolve()
        if not exact.is_relative_to(source_root) or not exact.is_file():
            errors.append(
                f"{record.data_id}: 精确 source snapshot 文件不存在 {declared_relative!r}"
            )
            return
        if exact.name != Path(source_file).name:
            errors.append(
                f"{record.data_id}: 精确 source snapshot 路径与 source_file 不一致"
            )
            return
        if snapshot_id not in exact.parts:
            match = _GFS_SNAPSHOT_ID.fullmatch(snapshot_id)
            if match is None or not (
                match.group(1) in exact.parts and match.group(2) in exact.name
            ):
                errors.append(
                    f"{record.data_id}: 精确 source snapshot 路径与 snapshot_id 不一致"
                )
                return
        if _cached_sha256(exact, checksum_cache) != declared_checksum:
            errors.append(
                f"{record.data_id}: 精确 source snapshot 文件 SHA-256 不匹配"
            )
        return
    source_name = Path(source_file)
    if source_name.is_absolute() or ".." in source_name.parts:
        errors.append(f"{record.data_id}: metadata.source_file 路径逃逸")
        return
    candidates = snapshot_index.get(source_name.name, ())
    candidates = _bind_candidates_to_snapshot(candidates, snapshot_id)
    if not candidates:
        errors.append(
            f"{record.data_id}: 找不到 source_snapshot={snapshot_id!r} "
            f"声明的文件 {source_file!r}"
        )
        return
    matching = [
        path
        for path in candidates
        if _cached_sha256(path, checksum_cache) == declared_checksum
    ]
    if not matching:
        errors.append(
            f"{record.data_id}: source_snapshot={snapshot_id!r} 文件 SHA-256 不匹配"
        )


def _bind_candidates_to_snapshot(
    candidates: tuple[Path, ...], snapshot_id: str
) -> tuple[Path, ...]:
    direct = tuple(
        path for path in candidates if snapshot_id in path.parts or snapshot_id in path.name
    )
    if direct:
        return direct
    match = _GFS_SNAPSHOT_ID.fullmatch(snapshot_id)
    if match:
        cycle, signature = match.groups()
        return tuple(
            path
            for path in candidates
            if cycle in path.parts and signature in path.name
        )
    return ()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
