from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.doctor import inspect_archive
from arctic_route_data.errors import DataValidationError
from arctic_route_data.ingestion import IngestionPipeline, sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.service import WorkPackageA
from arctic_route_data.sources import LocalArchiveSource

T0 = datetime(2026, 7, 15, 12, tzinfo=UTC)


def _evidence(issue_time):
    return IssueTimeEvidence(
        issue_time=issue_time,
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="test catalogue",
        reference="test fixture",
        observed_at=max(issue_time, T0 + timedelta(hours=3)),
        raw_value=issue_time.isoformat(),
    )


def _write_ice(path, value):
    dataset = xr.Dataset(
        {"ice_conc": (("latitude", "longitude"), np.full((2, 2), value))},
        coords={"longitude": [18.0, 19.0], "latitude": [70.0, 71.0]},
    )
    dataset["ice_conc"].attrs["units"] = "1"
    dataset.to_netcdf(path, engine="h5netcdf")


def _write_bathymetry(path, value):
    dataset = xr.Dataset(
        {"elevation": (("latitude", "longitude"), np.full((2, 2), value))},
        coords={"longitude": [18.0, 19.0], "latitude": [70.0, 71.0]},
    )
    dataset["elevation"].attrs.update(
        {
            "units": "m",
            "positive": "up",
            "standard_name": "height_above_mean_sea_level",
        }
    )
    dataset.to_netcdf(path, engine="h5netcdf")


def test_ingest_replay_jump_and_archive_integrity(tmp_path):
    data_root = tmp_path / "data"
    pipeline = IngestionPipeline(data_root)
    source_file = tmp_path / "ice.nc"
    _write_ice(source_file, 0.25)
    visible = pipeline.ingest_netcdf(
        source_file,
        data_type="sea_ice_concentration",
        route_id="tromso_to_svalbard",
        issue_time=T0 - timedelta(hours=6),
        valid_time=T0 + timedelta(hours=6),
        source="test",
        issue_time_evidence=_evidence(T0 - timedelta(hours=6)),
    )
    future_file = tmp_path / "future.nc"
    _write_ice(future_file, 0.9)
    pipeline.ingest_netcdf(
        future_file,
        data_type="sea_ice_concentration",
        route_id="tromso_to_svalbard",
        issue_time=T0 + timedelta(hours=2),
        valid_time=T0 + timedelta(hours=1),
        source="test",
        issue_time_evidence=_evidence(T0 + timedelta(hours=2)),
    )

    clock = SimulationClock(T0)
    cache = PartitionedABCache(max_memory_mb=8)
    service = WorkPackageA(
        source=LocalArchiveSource(data_root), clock=clock, cache=cache
    )
    frames = service.prefetch(
        route_id="tromso_to_svalbard",
        data_types=["sea_ice_concentration"],
    )
    assert [frame.record.data_id for frame in frames] == [visible.data_id]
    assert float(frames[0].payload.ice_concentration.mean()) == 0.25

    clock.seek(T0 + timedelta(hours=3))
    assert cache.latest(
        "sea_ice_concentration", route_id="tromso_to_svalbard"
    ) is None
    frames = service.prefetch(
        route_id="tromso_to_svalbard",
        data_types=["sea_ice_concentration"],
    )
    assert {frame.record.valid_time for frame in frames} == {
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=6),
    }
    report = inspect_archive(data_root)
    assert report.ok
    assert report.checked == 2


def test_seek_backwards_removes_static_frame_not_yet_published(tmp_path):
    data_root = tmp_path / "data"
    pipeline = IngestionPipeline(data_root)
    source_file = tmp_path / "bathymetry.nc"
    _write_bathymetry(source_file, -100.0)
    pipeline.ingest_netcdf(
        source_file,
        data_type="bathymetry",
        route_id="tromso_to_svalbard",
        issue_time=T0,
        valid_time=T0,
        source="test",
        issue_time_evidence=_evidence(T0),
    )

    clock = SimulationClock(T0 + timedelta(hours=1))
    cache = PartitionedABCache(max_memory_mb=8)
    service = WorkPackageA(
        source=LocalArchiveSource(data_root), clock=clock, cache=cache
    )
    frames = service.prefetch(
        route_id="tromso_to_svalbard",
        data_types=["bathymetry"],
    )
    assert len(frames) == 1
    assert cache.latest("bathymetry", route_id="tromso_to_svalbard") is not None

    clock.seek(T0 - timedelta(hours=1))

    assert cache.latest("bathymetry", route_id="tromso_to_svalbard") is None
    assert (
        service.prefetch(
            route_id="tromso_to_svalbard",
            data_types=["bathymetry"],
        )
        == []
    )


def test_direct_ingest_cannot_self_assert_structural_source_mask(tmp_path):
    source_file = tmp_path / "forged-mask.nc"
    dataset = xr.Dataset(
        {
            "ice_conc": (
                ("latitude", "longitude"),
                np.array([[0.25, np.nan]]),
            ),
            "source_valid_mask": (
                ("latitude", "longitude"), np.array([[True, False]])
            ),
        },
        coords={"longitude": [18.0, 19.0], "latitude": [70.0]},
    )
    dataset["ice_conc"].attrs["units"] = "1"
    dataset["source_valid_mask"].attrs.update(
        {
            "semantic_version": "a.source-valid-mask.v1",
            "semantic_role": "source_valid_domain",
            "derivation_method": (
                "any_required_variable_finite_over_complete_requested_dataset"
            ),
            "derivation_scope": "native_copernicus_request_before_temporal_split",
            "required_source_variables": '["ice_conc"]',
            "requested_start": T0.isoformat().replace("+00:00", "Z"),
            "requested_end": (T0 + timedelta(hours=6)).isoformat().replace(
                "+00:00", "Z"
            ),
            "navigation_semantics": "none",
            "classification_semantics": "none",
        }
    )
    dataset.to_netcdf(source_file, engine="h5netcdf")

    with pytest.raises(DataValidationError, match="source_snapshot"):
        IngestionPipeline(tmp_path / "data").ingest_netcdf(
            source_file,
            data_type="sea_ice_concentration",
            route_id="tromso_to_svalbard",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0,
            source="untrusted-direct-input",
            issue_time_evidence=_evidence(T0 - timedelta(hours=1)),
        )


def test_structural_mask_must_equal_the_bound_copernicus_snapshot(tmp_path):
    data_root = tmp_path / "data"
    snapshot_id = "cmems-snapshot-a"
    dataset_id = "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i"
    requested_start = T0.isoformat().replace("+00:00", "Z")
    requested_end = (T0 + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    mask_attrs = {
        "semantic_version": "a.source-valid-mask.v1",
        "semantic_role": "source_valid_domain",
        "derivation_method": (
            "any_required_variable_finite_over_complete_requested_dataset"
        ),
        "derivation_scope": "native_copernicus_request_before_temporal_split",
        "required_source_variables": '["ice_conc"]',
        "requested_start": requested_start,
        "requested_end": requested_end,
        "navigation_semantics": "none",
        "classification_semantics": "none",
    }
    coords = {"longitude": [18.0, 19.0], "latitude": [70.0]}

    snapshot = xr.Dataset(
        {
            "ice_conc": (("latitude", "longitude"), [[0.25, 0.5]]),
            "source_valid_mask": (
                ("latitude", "longitude"), np.array([[True, True]])
            ),
        },
        coords=coords,
        attrs={"copernicus_dataset_id": dataset_id},
    )
    snapshot["ice_conc"].attrs["units"] = "1"
    snapshot["source_valid_mask"].attrs.update(mask_attrs)
    snapshot_path = (
        data_root
        / "source_snapshots"
        / "copernicus"
        / snapshot_id
        / "sea_ice_concentration.nc"
    )
    snapshot_path.parent.mkdir(parents=True)
    snapshot.to_netcdf(snapshot_path, engine="h5netcdf")

    forged = snapshot.copy(deep=True)
    forged["ice_conc"] = (("latitude", "longitude"), [[0.25, np.nan]])
    forged["ice_conc"].attrs["units"] = "1"
    forged["source_valid_mask"] = (
        ("latitude", "longitude"), np.array([[True, False]])
    )
    forged["source_valid_mask"].attrs.update(mask_attrs)
    forged_path = tmp_path / "forged-bound-mask.nc"
    forged.to_netcdf(forged_path, engine="h5netcdf")

    with pytest.raises(DataValidationError, match="逐值、坐标或语义"):
        IngestionPipeline(data_root).ingest_netcdf(
            forged_path,
            data_type="sea_ice_concentration",
            route_id="tromso_to_svalbard",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0,
            source="Copernicus Marine",
            issue_time_evidence=_evidence(T0 - timedelta(hours=1)),
            extra_metadata={
                "source_snapshot_id": snapshot_id,
                "source_snapshot_relative_path": snapshot_path.relative_to(
                    data_root
                ).as_posix(),
                "source_file": snapshot_path.name,
                "source_file_checksum": sha256_file(snapshot_path),
                "dataset_id": dataset_id,
                "requested_start": requested_start,
                "requested_end": requested_end,
            },
        )
