#!/usr/bin/env python3
"""Inventory and explicitly retire detided ocean-current artifacts.

This is deliberately separate from the generic residue cleaner.  It only
touches exact manifest rows whose current component is ``detided`` (or whose
metadata explicitly says ``tide_included=false``), plus the bundles supplied
on the command line.  The default is a dry-run; ``--purge`` requires the
second ``--confirm`` flag and writes the same JSON ledger after deletion.
Frozen/production paths are rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_protected(path: Path) -> bool:
    parts = {part.casefold() for part in path.resolve().parts}
    return bool(parts & {"frozen", "frozen_demo_backup", "production"})


def _manifest_rows(root: Path) -> list[dict[str, Any]]:
    manifest = root / "manifest" / "manifest.sqlite3"
    if not manifest.is_file():
        return []
    connection = sqlite3.connect(f"file:{manifest.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT data_id, route_id, relative_path, version, size_bytes, valid_time,
                   metadata_json
            FROM manifest
            WHERE data_type = 'ocean_current'
              AND (
                json_extract(metadata_json, '$.current_component') = 'detided'
                OR json_extract(metadata_json, '$.tide_included') = 0
                OR json_extract(metadata_json, '$.normalization.current_component') = 'detided'
                OR json_extract(metadata_json, '$.normalization.tide_included') = 0
                OR lower(coalesce(json_extract(metadata_json, '$.dataset_id'), ''))
                   LIKE '%detided%'
                OR lower(coalesce(json_extract(metadata_json, '$.normalization.dataset_id'), ''))
                   LIKE '%detided%'
              )
            ORDER BY route_id, valid_time, data_id
            """
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    for data_id, route_id, relative_path, version, size_bytes, valid_time, metadata_json in rows:
        result.append(
            {
                "data_id": str(data_id),
                "route_id": str(route_id),
                "relative_path": str(relative_path),
                "version": str(version),
                "size_bytes_manifest": int(size_bytes),
                "valid_time": str(valid_time),
                "metadata": json.loads(metadata_json),
            }
        )
    return result


def _inode_map(roots: list[Path]) -> dict[tuple[int, int], list[str]]:
    result: dict[tuple[int, int], list[str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for directory, _, names in os.walk(root):
            for name in names:
                path = Path(directory) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                result.setdefault((stat.st_dev, stat.st_ino), []).append(str(path.resolve()))
    return result


def _candidate(path: Path, *, reason: str, references: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    if _is_protected(path):
        raise RuntimeError(f"refusing protected detided path: {path}")
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "reason": reason,
        "size_bytes": stat.st_size,
        "sha256": _sha256(path),
        "inode": stat.st_ino,
        "hardlink_count": stat.st_nlink,
        "hardlink_references": sorted(references),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="retire-detided-data")
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--bundle", action="append", default=[], type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    roots = [path.resolve() for path in args.root]
    if any(_is_protected(path) for path in roots):
        raise SystemExit("refusing a frozen/production root")
    for root in roots:
        if not root.is_dir():
            raise SystemExit(f"root is not a directory: {root}")
    inode_map = _inode_map(roots)
    files: list[dict[str, Any]] = []
    rows_by_root: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()

    for root in roots:
        rows = _manifest_rows(root)
        rows_by_root[str(root)] = rows
        for row in rows:
            ready = root / row["relative_path"]
            raw = root / "raw" / row["route_id"] / "ocean_current" / row["data_id"]
            for path, reason in (
                (ready, "detided manifest ready payload"),
                (ready.with_suffix(ready.suffix + ".metadata.json"), "detided ready sidecar"),
            ):
                if path.is_file() and str(path.resolve()) not in seen:
                    stat = path.stat()
                    files.append(
                        _candidate(
                            path,
                            reason=reason,
                            references=inode_map[(stat.st_dev, stat.st_ino)],
                        )
                        or {}
                    )
                    seen.add(str(path.resolve()))
            if raw.is_dir():
                for path in sorted(raw.rglob("*")):
                    if path.is_file() and str(path.resolve()) not in seen:
                        stat = path.stat()
                        files.append(
                            _candidate(
                                path,
                                reason="detided raw payload/sidecar",
                                references=inode_map[(stat.st_dev, stat.st_ino)],
                            )
                            or {}
                        )
                        seen.add(str(path.resolve()))
            snapshot = (
                root
                / "source_snapshots"
                / "copernicus"
                / row["version"]
                / "ocean_current.nc"
            )
            if snapshot.is_file() and str(snapshot.resolve()) not in seen:
                stat = snapshot.stat()
                files.append(
                    _candidate(
                        snapshot,
                        reason=f"detided source snapshot {row['version']}",
                        references=inode_map[(stat.st_dev, stat.st_ino)],
                    )
                    or {}
                )
                seen.add(str(snapshot.resolve()))

    bundles: list[dict[str, Any]] = []
    for bundle in args.bundle:
        path = bundle.resolve()
        if _is_protected(path):
            raise SystemExit(f"refusing protected bundle: {path}")
        if path.is_file():
            stat = path.stat()
            bundles.append(
                _candidate(
                    path,
                    reason="bundle contains detided current records",
                    references=inode_map.get((stat.st_dev, stat.st_ino), []),
                )
                or {}
            )

    ledger: dict[str, Any] = {
        "schema_version": "research.detided-retirement.v1",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": "PURGE" if args.purge else "DRY_RUN",
        "roots": [str(root) for root in roots],
        "manifest_rows": {
            root: {
                "count": len(rows),
                "bytes": sum(row["size_bytes_manifest"] for row in rows),
                "versions": sorted({row["version"] for row in rows}),
            }
            for root, rows in rows_by_root.items()
        },
        "files": files,
        "bundles": bundles,
        "protected_policy": "frozen/production paths rejected; frozen backups are retained",
        "purged": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not args.purge:
        print(
            json.dumps(
                {"status": "DRY_RUN", "files": len(files), "bundles": len(bundles)},
                ensure_ascii=False,
            )
        )
        return 0
    if not args.confirm:
        raise SystemExit("--purge requires --confirm")

    for entry in [*files, *bundles]:
        path = Path(entry["path"])
        if not path.is_file():
            continue
        if _sha256(path) != entry["sha256"]:
            raise SystemExit(f"checksum changed before purge: {path}")
        path.unlink()

    deleted_rows = 0
    for root, rows in rows_by_root.items():
        if not rows:
            continue
        manifest = Path(root) / "manifest" / "manifest.sqlite3"
        connection = sqlite3.connect(manifest)
        try:
            ids = [row["data_id"] for row in rows]
            connection.executemany(
                "DELETE FROM manifest WHERE data_id = ? AND data_type = 'ocean_current'",
                ((data_id,) for data_id in ids),
            )
            deleted_rows += connection.total_changes
            connection.commit()
        finally:
            connection.close()
    ledger["purged"] = True
    ledger["purged_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    ledger["deleted_manifest_rows"] = deleted_rows
    ledger["deleted_files"] = sum(
        1 for entry in [*files, *bundles] if not Path(entry["path"]).exists()
    )
    args.output.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "PURGED",
                "files": ledger["deleted_files"],
                "manifest_rows": deleted_rows,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
