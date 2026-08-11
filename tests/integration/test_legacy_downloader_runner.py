from datetime import UTC, datetime, timedelta

from arctic_route_data.issue_time import SourceIssueTimeResolver
from arctic_route_data.legacy_downloaders import (
    LEGACY_DOWNLOADERS,
    LegacyDownloaderRunner,
    LegacyDownloaderSpec,
)


class _Catalogue:
    def __init__(self, updated_at: datetime) -> None:
        self.updated_at = updated_at

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return {"datasets": [{"arco_updated_date": self.updated_at.isoformat()}]}


def test_legacy_runner_resolves_issue_time_and_splits_all_valid_times(
    tmp_path, monkeypatch
):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    time0 = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    time1 = (now + timedelta(hours=7)).replace(tzinfo=None).isoformat()
    legacy_root = tmp_path / "legacy"
    module_path = legacy_root / "fake_downloader.py"
    legacy_root.mkdir()
    module_path.write_text(
        f"""
import numpy as np
import xarray as xr

DATASET_ID = "official-dataset-id"

def download():
    times = np.array(["{time0}", "{time1}"], dtype="datetime64[s]")
    dataset = xr.Dataset(
        {{
            "vxsi": (("time", "latitude", "longitude"), np.ones((2, 1, 1))),
            "vysi": (("time", "latitude", "longitude"), np.ones((2, 1, 1)) * 2),
        }},
        coords={{"time": times, "latitude": [75.0], "longitude": [20.0]}},
        attrs={{"copernicus_dataset_id": DATASET_ID}},
    )
    dataset["vxsi"].attrs.update(
        {{"units": "m s-1", "standard_name": "eastward_sea_ice_velocity"}}
    )
    dataset["vysi"].attrs.update(
        {{"units": "m s-1", "standard_name": "northward_sea_ice_velocity"}}
    )
    return {{"route-a": dataset}}
""",
        encoding="utf-8",
    )
    monkeypatch.setitem(
        LEGACY_DOWNLOADERS,
        "fake",
        LegacyDownloaderSpec(
            name="fake",
            module_relative_path="fake_downloader.py",
            function_name="download",
            data_type="sea_ice_drift",
            source_family="copernicus_marine",
            source_label="Official test catalogue",
        ),
    )

    def describe(**kwargs):
        assert kwargs["dataset_id"] == "official-dataset-id"
        return _Catalogue(now)

    result = LegacyDownloaderRunner(
        legacy_root=legacy_root,
        data_root=tmp_path / "data",
        issue_time_resolver=SourceIssueTimeResolver(copernicus_describe=describe),
    ).run("fake")

    assert len(result.records) == 2
    assert {record.valid_time.hour for record in result.records} == {
        (now.hour + 1) % 24,
        (now.hour + 7) % 24,
    }
    assert all(record.issue_time == now for record in result.records)
    assert all(
        record.metadata["issue_time_evidence"]["method"] == "copernicus_service_sync"
        for record in result.records
    )
    assert all(record.quality_flag.value == "suspect" for record in result.records)
    assert len(list((tmp_path / "data" / "raw").rglob("*.metadata.json"))) == 2
