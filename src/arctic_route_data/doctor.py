"""Archive consistency checks used before replay and demonstrations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arctic_route_data.ingestion import sha256_file
from arctic_route_data.manifest import ManifestStore


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
    for record in records:
        try:
            path = record.absolute_path(root)
        except ValueError as exc:
            errors.append(f"{record.data_id}: {exc}")
            continue
        if not path.is_file():
            errors.append(f"{record.data_id}: 文件不存在 {path}")
            continue
        if sha256_file(path) != record.checksum:
            errors.append(f"{record.data_id}: SHA-256 不匹配")
        if record.relative_path in seen_paths:
            warnings.append(f"多个记录共用文件: {record.relative_path}")
        seen_paths.add(record.relative_path)
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
