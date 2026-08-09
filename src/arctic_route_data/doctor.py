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


def inspect_archive(data_root: str | Path, manifest: ManifestStore | None = None) -> DoctorReport:
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
    if not records:
        warnings.append("manifest 当前为空")
    return DoctorReport(len(records), tuple(errors), tuple(warnings))
