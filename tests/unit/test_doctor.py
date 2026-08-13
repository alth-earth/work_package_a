import json
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import xarray as xr

from arctic_route_data.doctor import inspect_archive, verified_archived_provenance_id
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.sources import LocalArchiveSource

T0 = datetime(2026, 7, 15, tzinfo=UTC)


def _published_archive(tmp_path):
    data_root = tmp_path / "data"
    snapshot = data_root / "source_snapshots" / "test" / "snapshot-1" / "source.bin"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"source snapshot")
    dataset = xr.Dataset(
        {"vis": (("latitude", "longitude"), np.array([[1_000.0]]))},
        coords={"latitude": [70.0], "longitude": [19.0]},
    )
    dataset["vis"].attrs["units"] = "m"
    evidence = IssueTimeEvidence(
        issue_time=T0,
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="test catalogue",
        reference="fixture",
        observed_at=T0,
        raw_value=T0.isoformat(),
    )
    record = AcquisitionPublisher(data_root).publish_dataset(
        dataset,
        data_type="visibility",
        route_id="route-a",
        source="test",
        version="snapshot-1",
        issue_evidence=evidence,
        valid_time=T0,
        metadata={
            "source_snapshot_id": "snapshot-1",
            "source_file": snapshot.name,
            "source_file_checksum": sha256_file(snapshot),
            "source_snapshot_relative_path": snapshot.relative_to(data_root).as_posix(),
        },
    ).records[0]
    raw_directory = data_root / "raw" / "route-a" / "visibility" / record.data_id
    sidecar = next(raw_directory.glob("*.metadata.json"))
    raw_payload = raw_directory / json.loads(sidecar.read_text(encoding="utf-8"))["file"]
    return data_root, record, snapshot, sidecar, raw_payload


def test_empty_archive_fails_unless_explicitly_allowed(tmp_path):
    strict = inspect_archive(tmp_path / "data")
    allowed = inspect_archive(tmp_path / "data", allow_empty=True)

    assert not strict.ok
    assert strict.errors == ("manifest 当前为空",)
    assert allowed.ok
    assert allowed.warnings == ("manifest 当前为空",)


def test_orphan_ready_file_is_an_error(tmp_path):
    path = tmp_path / "data" / "ready" / "orphan.nc"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")

    report = inspect_archive(tmp_path / "data", allow_empty=True)

    assert not report.ok
    assert any("未登记" in error for error in report.errors)


def test_repository_placeholders_are_not_reported_as_archive_content(tmp_path):
    root = tmp_path / "data"
    for directory in ("ready", "incoming"):
        path = root / directory / ".gitkeep"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n", encoding="utf-8")

    report = inspect_archive(root, allow_empty=True)

    assert report.ok
    assert report.errors == ()
    assert report.warnings == ("manifest 当前为空",)


def test_doctor_accepts_bound_raw_pair_and_source_snapshot(tmp_path):
    data_root, record, _, _, _ = _published_archive(tmp_path)

    report = inspect_archive(data_root)

    assert report.ok
    assert report.errors == ()
    assert verified_archived_provenance_id(data_root, record) == "snapshot-1"
    assert LocalArchiveSource(data_root).verified_provenance_id(record) == "snapshot-1"


def test_local_archive_rehashes_provenance_after_same_process_tampering(tmp_path):
    data_root, record, snapshot, _, _ = _published_archive(tmp_path)
    source = LocalArchiveSource(data_root)

    assert source.verified_provenance_id(record) == "snapshot-1"

    snapshot.write_bytes(b"tampered after first formal verification")

    assert source.verified_provenance_id(record) is None


def test_doctor_detects_tampered_raw_payload(tmp_path):
    data_root, _, _, _, raw_payload = _published_archive(tmp_path)
    raw_payload.write_bytes(raw_payload.read_bytes() + b"tampered")

    report = inspect_archive(data_root)

    assert not report.ok
    assert any("raw payload SHA-256" in error for error in report.errors)


def test_doctor_detects_raw_sidecar_bound_to_other_publication(tmp_path):
    data_root, _, _, sidecar, _ = _published_archive(tmp_path)
    value = json.loads(sidecar.read_text(encoding="utf-8"))
    value["publication_id"] = "other-publication"
    sidecar.write_text(json.dumps(value), encoding="utf-8")

    report = inspect_archive(data_root)

    assert not report.ok
    assert any("publication_id" in error for error in report.errors)


def test_doctor_detects_missing_declared_source_snapshot(tmp_path):
    data_root, _, snapshot, _, _ = _published_archive(tmp_path)
    snapshot.unlink()

    report = inspect_archive(data_root)

    assert not report.ok
    assert any("source snapshot" in error and "不存在" in error for error in report.errors)


def test_doctor_detects_tampered_declared_source_snapshot(tmp_path):
    data_root, record, snapshot, _, _ = _published_archive(tmp_path)
    snapshot.write_bytes(b"tampered snapshot")

    report = inspect_archive(data_root)

    assert not report.ok
    assert any("source snapshot" in error and "SHA-256" in error for error in report.errors)
    assert verified_archived_provenance_id(data_root, record) is None


def test_doctor_keeps_legacy_record_without_archive_declarations_compatible(
    tmp_path, make_record
):
    data_root = tmp_path / "data"
    ready = data_root / "ready" / "legacy.nc"
    ready.parent.mkdir(parents=True)
    ready.write_bytes(b"legacy")
    record = replace(
        make_record(data_id="legacy"),
        checksum=sha256_file(ready),
        relative_path="ready/legacy.nc",
        size_bytes=ready.stat().st_size,
    )
    ManifestStore(data_root / "manifest" / "manifest.sqlite3").register(record)

    report = inspect_archive(data_root)

    assert report.ok
