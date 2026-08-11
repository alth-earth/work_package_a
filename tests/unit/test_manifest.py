import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_data.errors import ManifestConflictError
from arctic_route_data.manifest import (
    _COLUMNS,
    _EXPECTED_UNIQUE,
    _TABLE_COLUMNS_SCHEMA,
    ManifestStore,
)
from arctic_route_data.models import QualityFlag

UTC = UTC
T0 = datetime(2026, 7, 15, 12, tzinfo=UTC)


def test_as_of_filter_prevents_future_information(tmp_path, make_record):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    available = make_record(
        data_id="available",
        valid_time=T0 + timedelta(hours=6),
        issue_time=T0 - timedelta(hours=1),
    )
    leaked = make_record(
        data_id="future-release",
        valid_time=T0 + timedelta(hours=1),
        issue_time=T0 + timedelta(hours=2),
    )
    store.register_many((available, leaked))
    result = store.list_available(
        "wind_field",
        T0,
        T0 + timedelta(hours=24),
        route_id="route-a",
        as_of=T0,
    )
    assert [record.data_id for record in result] == ["available"]


def test_version_resolution_prefers_quality_then_latest_issue(tmp_path, make_record):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    valid = T0 + timedelta(hours=6)
    store.register_many(
        (
            make_record(
                data_id="late-degraded",
                valid_time=valid,
                issue_time=T0,
                quality_flag=QualityFlag.DEGRADED,
            ),
            make_record(
                data_id="early-good",
                valid_time=valid,
                issue_time=T0 - timedelta(hours=1),
                quality_flag=QualityFlag.GOOD,
            ),
        )
    )
    result = store.list_available(
        "wind_field", T0, valid, route_id="route-a", as_of=T0
    )
    assert [record.data_id for record in result] == ["early-good"]


def test_bracketing_only_uses_already_issued_frames(tmp_path, make_record):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    for offset in (-6, 6, 12):
        store.register(
            make_record(
                data_id=f"frame-{offset}",
                valid_time=T0 + timedelta(hours=offset),
                issue_time=T0 - timedelta(hours=1)
                if offset != 12
                else T0 + timedelta(hours=1),
            )
        )
    lower, upper = store.get_bracketing(
        "wind_field", T0, route_id="route-a", as_of=T0
    )
    assert lower and lower.data_id == "frame--6"
    assert upper and upper.data_id == "frame-6"


def test_published_manifest_record_is_immutable_but_identical_retry_is_idempotent(
    tmp_path, make_record
):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    record = make_record(data_id="immutable")

    assert store.register(record) == record
    assert store.register(replace(record, ingest_time=T0 + timedelta(days=1))) == record

    with pytest.raises(ManifestConflictError, match="不可变"):
        store.register(replace(record, source="different source"))


def test_v1_schema_migration_preserves_rows_atomically(tmp_path, make_record):
    database = tmp_path / "manifest.sqlite3"
    record = make_record(data_id="migrated")
    values = record.to_dict()
    old_columns = _TABLE_COLUMNS_SCHEMA.replace(
        _EXPECTED_UNIQUE,
        "UNIQUE(route_id, data_type, valid_time, issue_time, version, checksum)",
    )
    connection = sqlite3.connect(database)
    connection.execute(f"CREATE TABLE manifest ({old_columns})")
    connection.execute(
        f"INSERT INTO manifest ({_COLUMNS}) VALUES ({', '.join('?' for _ in range(19))})",
        (
            values["data_id"],
            values["data_type"],
            values["category"],
            values["route_id"],
            json.dumps(values["variables"]),
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
            json.dumps(values["metadata"]),
        ),
    )
    connection.commit()
    connection.close()

    migrated = ManifestStore(database)

    assert migrated.get("migrated") == record
    with sqlite3.connect(database) as check:
        sql = check.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='manifest'"
        ).fetchone()[0]
        assert _EXPECTED_UNIQUE in " ".join(sql.split())
        assert check.execute("PRAGMA user_version").fetchone()[0] == 2
