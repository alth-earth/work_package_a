"""SQLite-backed manifest with simulation-time availability filtering."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from arctic_route_data.models import ManifestRecord
from arctic_route_data.timeutils import isoformat_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (
    data_id TEXT PRIMARY KEY,
    data_type TEXT NOT NULL,
    category TEXT NOT NULL,
    route_id TEXT NOT NULL,
    variables_json TEXT NOT NULL,
    issue_time TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    ingest_time TEXT NOT NULL,
    bbox_json TEXT NOT NULL,
    crs TEXT NOT NULL,
    resolution_json TEXT NOT NULL,
    source TEXT NOT NULL,
    quality_flag TEXT NOT NULL,
    version TEXT NOT NULL,
    checksum TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(route_id, data_type, valid_time, issue_time, version, checksum)
);
CREATE INDEX IF NOT EXISTS idx_manifest_available
    ON manifest(route_id, data_type, issue_time, valid_time);
CREATE INDEX IF NOT EXISTS idx_manifest_path ON manifest(relative_path);
"""


class ManifestStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _open(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._open() as connection:
            connection.executescript(_SCHEMA)

    def register(self, record: ManifestRecord) -> None:
        values = record.to_dict()
        with self._open() as connection:
            connection.execute(
                """
                INSERT INTO manifest (
                    data_id, data_type, category, route_id, variables_json,
                    issue_time, valid_time, ingest_time, bbox_json, crs,
                    resolution_json, source, quality_flag, version, checksum,
                    relative_path, size_bytes, media_type, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(data_id) DO UPDATE SET
                    quality_flag=excluded.quality_flag,
                    metadata_json=excluded.metadata_json,
                    relative_path=excluded.relative_path,
                    size_bytes=excluded.size_bytes
                """,
                (
                    values["data_id"],
                    values["data_type"],
                    values["category"],
                    values["route_id"],
                    json.dumps(values["variables"], ensure_ascii=False),
                    values["issue_time"],
                    values["valid_time"],
                    values["ingest_time"],
                    json.dumps(values["bbox"]),
                    values["crs"],
                    json.dumps(values["resolution"]),
                    values["source"],
                    values["quality_flag"],
                    values["version"],
                    values["checksum"],
                    values["relative_path"],
                    values["size_bytes"],
                    values["media_type"],
                    json.dumps(values["metadata"], ensure_ascii=False, sort_keys=True),
                ),
            )

    def register_many(self, records: Iterable[ManifestRecord]) -> None:
        for record in records:
            self.register(record)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ManifestRecord:
        return ManifestRecord.from_dict(
            {
                "data_id": row["data_id"],
                "data_type": row["data_type"],
                "category": row["category"],
                "route_id": row["route_id"],
                "variables": json.loads(row["variables_json"]),
                "issue_time": row["issue_time"],
                "valid_time": row["valid_time"],
                "ingest_time": row["ingest_time"],
                "bbox": json.loads(row["bbox_json"]),
                "crs": row["crs"],
                "resolution": json.loads(row["resolution_json"]),
                "source": row["source"],
                "quality_flag": row["quality_flag"],
                "version": row["version"],
                "checksum": row["checksum"],
                "relative_path": row["relative_path"],
                "size_bytes": row["size_bytes"],
                "media_type": row["media_type"],
                "metadata": json.loads(row["metadata_json"]),
            }
        )

    def get(self, data_id: str) -> ManifestRecord | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM manifest WHERE data_id = ?", (data_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def list_available(
        self,
        data_type: str,
        start_time: datetime,
        end_time: datetime,
        *,
        route_id: str,
        as_of: datetime,
        resolve_versions: bool = True,
    ) -> list[ManifestRecord]:
        """Return frames valid in the window and published no later than ``as_of``."""
        params = (
            route_id,
            data_type,
            isoformat_utc(start_time),
            isoformat_utc(end_time),
            isoformat_utc(as_of),
        )
        with self._open() as connection:
            rows = connection.execute(
                """
                SELECT * FROM manifest
                WHERE route_id = ? AND data_type = ?
                  AND valid_time >= ? AND valid_time <= ? AND issue_time <= ?
                ORDER BY valid_time ASC,
                    CASE quality_flag
                        WHEN 'good' THEN 3 WHEN 'suspect' THEN 2
                        WHEN 'degraded' THEN 1 ELSE 0 END DESC,
                    issue_time DESC, ingest_time DESC, version DESC
                """,
                params,
            ).fetchall()
        records = [self._from_row(row) for row in rows]
        if not resolve_versions:
            return records
        selected: dict[datetime, ManifestRecord] = {}
        for record in records:
            selected.setdefault(record.valid_time, record)
        return list(selected.values())

    def get_latest_before(
        self,
        data_type: str,
        target_time: datetime,
        *,
        route_id: str,
        as_of: datetime | None = None,
    ) -> ManifestRecord | None:
        available_at = as_of or target_time
        with self._open() as connection:
            row = connection.execute(
                """
                SELECT * FROM manifest
                WHERE route_id = ? AND data_type = ?
                  AND valid_time <= ? AND issue_time <= ?
                ORDER BY valid_time DESC,
                    CASE quality_flag
                        WHEN 'good' THEN 3 WHEN 'suspect' THEN 2
                        WHEN 'degraded' THEN 1 ELSE 0 END DESC,
                    issue_time DESC, ingest_time DESC, version DESC
                LIMIT 1
                """,
                (
                    route_id,
                    data_type,
                    isoformat_utc(target_time),
                    isoformat_utc(available_at),
                ),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def get_bracketing(
        self,
        data_type: str,
        target_time: datetime,
        *,
        route_id: str,
        as_of: datetime,
    ) -> tuple[ManifestRecord | None, ManifestRecord | None]:
        lower = self.get_latest_before(
            data_type, target_time, route_id=route_id, as_of=as_of
        )
        with self._open() as connection:
            row = connection.execute(
                """
                SELECT * FROM manifest
                WHERE route_id = ? AND data_type = ?
                  AND valid_time >= ? AND issue_time <= ?
                ORDER BY valid_time ASC,
                    CASE quality_flag
                        WHEN 'good' THEN 3 WHEN 'suspect' THEN 2
                        WHEN 'degraded' THEN 1 ELSE 0 END DESC,
                    issue_time DESC, ingest_time DESC, version DESC
                LIMIT 1
                """,
                (
                    route_id,
                    data_type,
                    isoformat_utc(target_time),
                    isoformat_utc(as_of),
                ),
            ).fetchone()
        upper = None if row is None else self._from_row(row)
        return lower, upper

    def all_records(self) -> list[ManifestRecord]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM manifest ORDER BY route_id, data_type, valid_time, issue_time"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def export_json(self, destination: str | Path) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        payload: Sequence[dict[str, object]] = [record.to_dict() for record in self.all_records()]
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)
        return path
