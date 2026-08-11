from datetime import UTC, datetime

import numpy as np
import xarray as xr

from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.publisher import AcquisitionPublisher


def test_publisher_splits_frames_writes_sidecars_and_registers_manifest(tmp_path):
    times = np.array(["2026-07-15T00", "2026-07-15T06"], dtype="datetime64[h]")
    dataset = xr.Dataset(
        {
            "vxsi": (("time", "latitude", "longitude"), np.ones((2, 1, 2))),
            "vysi": (("time", "latitude", "longitude"), np.ones((2, 1, 2)) * 2),
        },
        coords={"time": times, "latitude": [70.0], "longitude": [18.0, 19.0]},
    )
    dataset["vxsi"].attrs.update(
        {"units": "m s-1", "standard_name": "eastward_sea_ice_velocity"}
    )
    dataset["vysi"].attrs.update(
        {"units": "m s-1", "standard_name": "northward_sea_ice_velocity"}
    )
    evidence = IssueTimeEvidence(
        issue_time=datetime(2026, 7, 14, 18, tzinfo=UTC),
        method=IssueTimeMethod.HTTP_LAST_MODIFIED,
        authority="Copernicus Marine",
        reference="https://example.invalid/product.nc",
        observed_at=datetime(2026, 7, 15, 1, tzinfo=UTC),
        raw_value="Tue, 14 Jul 2026 18:00:00 GMT",
    )
    result = AcquisitionPublisher(tmp_path / "data").publish_dataset(
        dataset,
        data_type="sea_ice_drift",
        route_id="tromso_to_svalbard",
        source="Copernicus Marine",
        version="test-v1",
        issue_evidence=evidence,
    )
    assert len(result.records) == 2
    assert {record.valid_time.hour for record in result.records} == {0, 6}
    assert all(record.variables == ("ice_drift_u", "ice_drift_v") for record in result.records)
    assert all(
        record.metadata["issue_time_evidence"]["method"] == "http_last_modified"
        for record in result.records
    )
    raw_sidecars = list((tmp_path / "data" / "raw").rglob("*.metadata.json"))
    assert len(raw_sidecars) == 2


def test_same_logical_publication_with_different_content_cannot_cross_bind(tmp_path):
    evidence = IssueTimeEvidence(
        issue_time=datetime(2026, 7, 14, 18, tzinfo=UTC),
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="test catalogue",
        reference="fixture",
        observed_at=datetime(2026, 7, 14, 19, tzinfo=UTC),
        raw_value="2026-07-14T18:00:00Z",
    )
    publisher = AcquisitionPublisher(tmp_path / "data")

    def dataset(value):
        result = xr.Dataset(
            {"vis": (("latitude", "longitude"), [[value]])},
            coords={"latitude": [70.0], "longitude": [19.0]},
        )
        result["vis"].attrs["units"] = "m"
        return result

    first = publisher.publish_dataset(
        dataset(1_000.0),
        data_type="visibility",
        route_id="route-a",
        source="test",
        version="v1",
        issue_evidence=evidence,
        valid_time=datetime(2026, 7, 15, tzinfo=UTC),
    ).records[0]
    second = publisher.publish_dataset(
        dataset(2_000.0),
        data_type="visibility",
        route_id="route-a",
        source="test",
        version="v1",
        issue_evidence=evidence,
        valid_time=datetime(2026, 7, 15, tzinfo=UTC),
    ).records[0]

    assert first.data_id != second.data_id
    assert first.metadata["publication_id"] != second.metadata["publication_id"]
    assert first.metadata["upstream_checksum"] != second.metadata["upstream_checksum"]
    assert len(list((tmp_path / "data" / "raw").rglob("*.metadata.json"))) == 2
    assert not list((tmp_path / "data" / "incoming").glob("*.metadata.json"))
