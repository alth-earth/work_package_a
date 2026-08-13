from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import xarray as xr

from arctic_route_data import semantic_payload_digest
from arctic_route_data.bundle import DatasetBundle, record_provenance_id
from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.errors import (
    ChecksumMismatchError,
    DataValidationError,
    StaleGenerationError,
)
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.service import WorkPackageA
from arctic_route_data.sources import LocalArchiveSource

T0 = datetime(2026, 7, 15, tzinfo=UTC)


def _evidence() -> IssueTimeEvidence:
    return IssueTimeEvidence(
        issue_time=T0 - timedelta(hours=1),
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="test catalogue",
        reference="exact-bundle fixture",
        observed_at=T0,
        raw_value=(T0 - timedelta(hours=1)).isoformat(),
    )


def _published_archive(tmp_path):
    data_root = tmp_path / "data"
    snapshot = data_root / "source_snapshots" / "test" / "snapshot-a" / "source.bin"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"immutable source snapshot")
    records = []
    for offset in range(4):
        dataset = xr.Dataset(
            {
                "significant_wave_height": (
                    ("latitude", "longitude"),
                    np.full((1, 1), 1.0 + offset),
                ),
                "mean_wave_direction": (
                    ("latitude", "longitude"),
                    np.full((1, 1), 90.0),
                ),
                "peak_wave_period": (
                    ("latitude", "longitude"),
                    np.full((1, 1), 8.0),
                ),
            },
            coords={"latitude": [70.0], "longitude": [19.0]},
        )
        dataset["significant_wave_height"].attrs["units"] = "m"
        dataset["mean_wave_direction"].attrs.update(
            {
                "units": "degree",
                "standard_name": "sea_surface_wave_from_direction",
            }
        )
        dataset["peak_wave_period"].attrs["units"] = "s"
        result = AcquisitionPublisher(data_root).publish_dataset(
            dataset,
            data_type="wave",
            route_id="route-a",
            source="test",
            version="snapshot-a",
            issue_evidence=_evidence(),
            valid_time=T0 + timedelta(hours=3 * offset),
            metadata={
                "source_snapshot_id": "snapshot-a",
                "source_file": snapshot.name,
                "source_file_checksum": sha256_file(snapshot),
                "source_snapshot_relative_path": snapshot.relative_to(
                    data_root
                ).as_posix(),
                "nominal_interval_hours": 3.0,
            },
        )
        records.extend(result.records)
    verified = {
        record.data_id: record_provenance_id(record) for record in records
    }
    bundle = DatasetBundle.create(
        corridor_id="route-a",
        as_of_time=T0,
        requested_start=T0,
        requested_end=T0 + timedelta(hours=9),
        minimum_required_end=T0 + timedelta(hours=9),
        requested_data_types=("wave",),
        records=tuple(records),
        verified_provenance_ids=verified,
        expected_interval_hours={"wave": 3.0},
    )
    return data_root, snapshot, bundle


def test_exact_bundle_resolver_restores_persisted_frames_in_new_service(tmp_path):
    data_root, _, bundle = _published_archive(tmp_path)
    service = WorkPackageA(
        source=LocalArchiveSource(data_root),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=4),
    )

    prepared = service.resolve_dataset_bundle_for_b(
        bundle.to_dict(),
        generation_id=0,
        knowledge_as_of=T0,
    )

    assert prepared.dataset_bundle.to_dict() == bundle.to_dict()
    assert prepared.coverage["wave"].complete
    assert [frame.record.data_id for frame in prepared.frames["wave"]] == [
        record.data_id for record in bundle.records
    ]
    assert all(frame.generation_id == 0 for frame in prepared.frames["wave"])
    assert set(prepared.payload_attestations) == {
        frame.record.data_id for frame in prepared.frames["wave"]
    }
    assert all(
        prepared.payload_attestations[frame.record.data_id]
        == semantic_payload_digest(frame.record, frame.payload)
        for frame in prepared.frames["wave"]
    )


def test_exact_bundle_resolver_rejects_v1_cutoff_and_stale_generation(tmp_path):
    data_root, _, bundle = _published_archive(tmp_path)
    service = WorkPackageA(
        source=LocalArchiveSource(data_root),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=4),
    )
    legacy = bundle.to_dict()
    legacy.pop("coverage")
    legacy["schema_version"] = "a.dataset-bundle.v1"
    from arctic_route_data.bundle import _bundle_digest

    digest = _bundle_digest(
        schema_version="a.dataset-bundle.v1",
        corridor_id=bundle.corridor_id,
        as_of_time=bundle.as_of_time,
        requested_start=bundle.requested_start,
        requested_end=bundle.requested_end,
        minimum_required_end=bundle.minimum_required_end,
        requested_data_types=bundle.requested_data_types,
        source_snapshot_ids=bundle.source_snapshot_ids,
        records=bundle.records,
    )
    legacy["bundle_digest"] = digest
    legacy["bundle_id"] = f"a-bundle-{digest[:24]}"

    with pytest.raises(DataValidationError, match="v1 仅供历史读取"):
        service.resolve_dataset_bundle_for_b(
            legacy,
            generation_id=0,
            knowledge_as_of=T0,
        )
    with pytest.raises(DataValidationError, match="knowledge_as_of"):
        service.resolve_dataset_bundle_for_b(
            bundle,
            generation_id=0,
            knowledge_as_of=T0 + timedelta(seconds=1),
        )
    with pytest.raises(StaleGenerationError, match="generation_id"):
        service.resolve_dataset_bundle_for_b(
            bundle,
            generation_id=1,
            knowledge_as_of=T0,
        )


def test_exact_bundle_resolver_rejects_tampered_bound_snapshot(tmp_path):
    data_root, snapshot, bundle = _published_archive(tmp_path)
    service = WorkPackageA(
        source=LocalArchiveSource(data_root),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=4),
    )
    snapshot.write_bytes(b"tampered source snapshot")

    with pytest.raises(ChecksumMismatchError, match="provenance"):
        service.resolve_dataset_bundle_for_b(
            bundle,
            generation_id=0,
            knowledge_as_of=T0,
        )


def test_exact_bundle_resolver_rejects_tampered_ready_payload(tmp_path):
    data_root, _, bundle = _published_archive(tmp_path)
    source = LocalArchiveSource(data_root)
    service = WorkPackageA(
        source=source,
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=4),
    )
    record = source.get_record_by_id(bundle.records[0].data_id)
    assert record is not None
    ready = record.absolute_path(data_root)
    ready.write_bytes(ready.read_bytes() + b"tampered ready payload")

    with pytest.raises(ChecksumMismatchError, match="provenance"):
        service.resolve_dataset_bundle_for_b(
            bundle,
            generation_id=0,
            knowledge_as_of=T0,
        )
