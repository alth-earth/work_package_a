#!/usr/bin/env python3
"""Audit and cautiously clean incomplete Winter acquisition residue.

This utility is intentionally scoped to an *explicit experimental* A data
root.  It never considers published manifest payloads as cleanup candidates.
The default action is a dry-run that emits a machine-readable ledger; an
operator can subsequently quarantine the exact ledger entries and, in a
separate invocation with an explicit confirmation, purge that quarantine.

It is not a general archive cleanup command.  Canonical, production, and
frozen roots are rejected before the manifest is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "research.winter-cleanup-ledger.v1"
_RESIDUE_SUFFIXES = (".part", ".partial", ".tmp", ".claim")
_PROTECTED_PATH_TERMS = frozenset({"canonical", "production", "prod", "frozen"})
_EXPERIMENTAL_PATH_TERMS = frozenset(
    {"experiment", "experiments", "experimental", "research", "screening", "scratch"}
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="audit-winter-cleanup")
    parser.add_argument(
        "--experimental-root",
        type=Path,
        required=True,
        help="explicit experimental A data root; canonical/production/frozen roots are refused",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="explicit manifest at <experimental-root>/manifest/manifest.sqlite3",
    )
    parser.add_argument(
        "--ledger-output",
        type=Path,
        help="write the emitted ledger atomically; required for --quarantine",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--quarantine",
        action="store_true",
        help="move unreferenced residue into an in-root quarantine after auditing it",
    )
    action.add_argument(
        "--purge",
        action="store_true",
        help="purge a previously quarantined ledger; requires --ledger-input and --confirm-purge",
    )
    parser.add_argument("--ledger-input", type=Path, help="ledger created by --quarantine")
    parser.add_argument(
        "--confirm-purge",
        action="store_true",
        help="explicit acknowledgement that the selected quarantine files will be removed",
    )
    return parser.parse_args()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_scope(experimental_root: Path, manifest: Path) -> tuple[Path, Path]:
    """Return resolved, explicitly experimental paths or reject the invocation."""

    root = experimental_root.resolve()
    expected_manifest = root / "manifest" / "manifest.sqlite3"
    manifest_path = manifest.resolve()
    canonical_root = (Path(__file__).resolve().parents[1] / "data").resolve()
    lower_parts = {part.lower() for part in root.parts}
    path_terms = {
        term
        for part in lower_parts
        for term in re.split(r"[^a-z0-9]+", part)
        if term
    }
    if root == canonical_root or path_terms & _PROTECTED_PATH_TERMS:
        raise ValueError("refusing canonical, production, or frozen root")
    if not path_terms & _EXPERIMENTAL_PATH_TERMS:
        raise ValueError(
            "experimental root path must contain an explicit "
            "experiment/research/screening/scratch marker"
        )
    if manifest_path != expected_manifest:
        raise ValueError("manifest must be exactly <experimental-root>/manifest/manifest.sqlite3")
    if not root.is_dir():
        raise FileNotFoundError(f"experimental root not found: {root}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    return root, manifest_path


def _manifest_references(manifest: Path, root: Path) -> dict[str, list[dict[str, str]]]:
    """Build a conservative file-reference index without modifying SQLite."""

    connection = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT data_id, relative_path, metadata_json FROM manifest ORDER BY data_id"
        ).fetchall()
    finally:
        connection.close()

    references: dict[str, list[dict[str, str]]] = {}

    def add(value: object, *, data_id: str, field: str) -> None:
        if not isinstance(value, str) or not value:
            return
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if not _is_relative_to(resolved, root):
            return
        relative = resolved.relative_to(root).as_posix()
        references.setdefault(relative, []).append({"data_id": data_id, "field": field})

    def walk(value: object, *, data_id: str, field: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, data_id=data_id, field=f"{field}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, data_id=data_id, field=f"{field}[{index}]")
        elif isinstance(value, str):
            add(value, data_id=data_id, field=field)

    for row in rows:
        data_id = str(row["data_id"])
        add(row["relative_path"], data_id=data_id, field="relative_path")
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid metadata_json for manifest record {data_id}") from exc
        walk(metadata, data_id=data_id, field="metadata")
    return references


def _residue_reason(path: Path, *, manifest: Path) -> str | None:
    if path == manifest:
        return None
    name = path.name
    if name.endswith(_RESIDUE_SUFFIXES):
        return "INCOMPLETE_PUBLICATION_RESIDUE"
    if name.endswith(("-wal", "-shm")):
        return "MANIFEST_OR_SQLITE_SIDECAR_REQUIRES_RECOVERY"
    if name in {".lock", "LOCK"}:
        return "INCOMPLETE_PUBLICATION_LOCK"
    return None


def _audit(root: Path, manifest: Path) -> dict[str, Any]:
    references = _manifest_references(manifest, root)
    entries: list[dict[str, Any]] = []
    quarantine_root = root / "quarantine" / "winter-cleanup"
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_relative_to(path.resolve(), quarantine_root.resolve()):
            continue
        reason = _residue_reason(path, manifest=manifest)
        if reason is None:
            continue
        relative = path.resolve().relative_to(root).as_posix()
        path_references = references.get(relative, [])
        actionable = (
            reason not in {"MANIFEST_OR_SQLITE_SIDECAR_REQUIRES_RECOVERY"}
            and not path_references
        )
        entries.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "references": path_references,
                "reason": reason,
                "eligible_for_quarantine": actionable,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_now(),
        "experimental_root": str(root),
        "manifest": str(manifest),
        "action": "DRY_RUN",
        "entries": entries,
        "summary": {
            "entries": len(entries),
            "eligible_for_quarantine": sum(item["eligible_for_quarantine"] for item in entries),
            "protected_or_recovery_required": sum(
                not item["eligible_for_quarantine"] for item in entries
            ),
        },
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _quarantine(ledger: dict[str, Any], root: Path) -> None:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination_root = root / "quarantine" / "winter-cleanup" / run_id
    for entry in ledger["entries"]:
        if not entry["eligible_for_quarantine"]:
            entry["action"] = "SKIPPED_NOT_ELIGIBLE"
            continue
        source = (root / entry["path"]).resolve()
        if not source.is_file() or _sha256(source) != entry["sha256"]:
            entry["action"] = "SKIPPED_CHANGED_OR_MISSING"
            continue
        destination = (destination_root / entry["path"]).resolve()
        if not _is_relative_to(destination, destination_root.resolve()):
            raise RuntimeError("unsafe quarantine destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        entry["quarantine_path"] = destination.relative_to(root).as_posix()
        entry["action"] = "QUARANTINED"
    ledger["action"] = "QUARANTINE"
    ledger["quarantine_root"] = str(destination_root)


def _purge(ledger_input: Path, root: Path, manifest: Path) -> dict[str, Any]:
    ledger = json.loads(ledger_input.read_text(encoding="utf-8"))
    if ledger.get("schema_version") != SCHEMA_VERSION or ledger.get("action") != "QUARANTINE":
        raise ValueError("--ledger-input must be a quarantine ledger from this tool")
    if ledger.get("experimental_root") != str(root) or ledger.get("manifest") != str(manifest):
        raise ValueError("ledger scope does not match explicit experimental root and manifest")
    quarantine_root = Path(str(ledger.get("quarantine_root", ""))).resolve()
    required_quarantine_parent = (root / "quarantine" / "winter-cleanup").resolve()
    if not _is_relative_to(quarantine_root, required_quarantine_parent):
        raise ValueError("ledger quarantine root is outside the permitted in-root quarantine")
    purged = 0
    skipped = 0
    for entry in ledger.get("entries", []):
        if entry.get("action") != "QUARANTINED":
            continue
        candidate = (root / str(entry.get("quarantine_path", ""))).resolve()
        if not _is_relative_to(candidate, quarantine_root) or not candidate.is_file():
            entry["purge_action"] = "SKIPPED_MISSING_OR_OUTSIDE_QUARANTINE"
            skipped += 1
            continue
        if _sha256(candidate) != entry.get("sha256"):
            entry["purge_action"] = "SKIPPED_CHECKSUM_MISMATCH"
            skipped += 1
            continue
        candidate.unlink()
        entry["purge_action"] = "PURGED"
        purged += 1
    ledger["purge"] = {"executed_at": _iso_now(), "purged": purged, "skipped": skipped}
    return ledger


def main() -> int:
    args = _parse_args()
    root, manifest = _validate_scope(args.experimental_root, args.manifest)
    if args.purge:
        if not args.ledger_input or not args.ledger_output or not args.confirm_purge:
            raise ValueError(
                "--purge requires --ledger-input, --ledger-output, and --confirm-purge"
            )
        ledger = _purge(args.ledger_input, root, manifest)
        _atomic_json(args.ledger_output, ledger)
        print(json.dumps(ledger, ensure_ascii=False, indent=2))
        return 0
    if args.ledger_input or args.confirm_purge:
        raise ValueError("--ledger-input/--confirm-purge are only valid with --purge")
    if args.quarantine and not args.ledger_output:
        raise ValueError("--quarantine requires --ledger-output for its recovery ledger")
    ledger = _audit(root, manifest)
    if args.quarantine:
        _quarantine(ledger, root)
    if args.ledger_output:
        _atomic_json(args.ledger_output, ledger)
    print(json.dumps(ledger, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"audit-winter-cleanup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
