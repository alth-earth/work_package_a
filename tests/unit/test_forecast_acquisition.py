from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

import arctic_route_data.forecast_acquisition as forecast_module
from arctic_route_data.forecast_acquisition import (
    Bounds,
    NativeForecastAcquirer,
    _gfs_request_signature,
    build_gfs_filter_params,
    forecast_hours,
)
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod

T0 = datetime(2026, 7, 15, tzinfo=UTC)


class _Response:
    def __init__(self):
        self.content = b"GRIB" + (b"x" * 8) + b"7777"
        self.headers = {}
        self.url = "https://nomads.example/filter"

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.params = []

    def get(self, url, *, params, timeout):
        self.params.append((url, params, timeout))
        return _Response()


def test_forecast_hour_policy_includes_non_divisible_horizon():
    assert forecast_hours(10, 3) == (0, 3, 6, 9, 10)


def test_gfs_request_identity_includes_bbox_and_variables():
    first = _gfs_request_signature(Bounds(10, 68, 22, 79), ("wind_field",))
    other_route = _gfs_request_signature(
        Bounds(30, 67, 85, 75), ("wind_field",)
    )
    other_variables = _gfs_request_signature(
        Bounds(10, 68, 22, 79), ("wind_field", "temperature")
    )

    assert len({first, other_route, other_variables}) == 3


def test_gfs_source_snapshot_paths_do_not_collide_between_routes(tmp_path):
    session = _Session()
    acquirer = NativeForecastAcquirer(tmp_path, http_session=session)

    path_a, evidence_a, _ = acquirer._obtain_gfs_file(
        cycle=T0,
        forecast_hour=6,
        bounds=Bounds(10, 68, 22, 79),
        data_types=("wind_field",),
    )
    path_b, evidence_b, _ = acquirer._obtain_gfs_file(
        cycle=T0,
        forecast_hour=6,
        bounds=Bounds(30, 67, 85, 75),
        data_types=("wind_field",),
    )

    assert path_a != path_b
    assert path_a.is_file() and path_b.is_file()
    assert len(session.params) == 2
    assert evidence_a.method is IssueTimeMethod.CONSERVATIVE_RETRIEVAL
    assert evidence_b.method is IssueTimeMethod.CONSERVATIVE_RETRIEVAL
    assert not evidence_a.authoritative


def test_build_gfs_filter_selects_required_levels_and_variables():
    params = build_gfs_filter_params(
        cycle=T0,
        forecast_hour=6,
        bounds=Bounds(10, 68, 22, 79),
        data_types=("wind_field", "temperature", "visibility"),
    )

    assert params["var_UGRD"] == params["var_VGRD"] == "on"
    assert params["lev_10_m_above_ground"] == "on"
    assert params["var_TMP"] == params["lev_2_m_above_ground"] == "on"
    assert params["var_VIS"] == params["lev_surface"] == "on"


def test_cycle_selection_extends_lead_to_cover_horizon_from_as_of(tmp_path, monkeypatch):
    acquirer = NativeForecastAcquirer(tmp_path / "data")
    requested_hours = []

    def obtain(**kwargs):
        requested_hours.append(kwargs["forecast_hour"])
        return tmp_path / "probe.grib2", object(), "fixture"

    monkeypatch.setattr(acquirer, "_obtain_gfs_file", obtain)
    monkeypatch.setattr(forecast_module, "_open_gfs_data_types", lambda *args: {})

    cycle, final_hour = acquirer._select_complete_gfs_cycle(
        as_of=T0 + timedelta(hours=5, minutes=30),
        bounds=Bounds(10, 68, 22, 79),
        requested_end=T0 + timedelta(hours=161, minutes=30),
        step_hours=3,
        data_types=("wind_field",),
        lookback_count=1,
    )

    assert cycle == T0
    assert final_hour == 162
    assert requested_hours == [162]


def test_full_gfs_window_is_published_one_frame_per_valid_time(tmp_path, monkeypatch):
    acquirer = NativeForecastAcquirer(tmp_path / "data")
    monkeypatch.setattr(acquirer, "_select_complete_gfs_cycle", lambda **kwargs: (T0, 6))
    evidence = IssueTimeEvidence(
        issue_time=T0,
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="NOAA test catalogue",
        reference="fixture",
        observed_at=T0,
        raw_value=T0.isoformat(),
    )

    def obtain(**kwargs):
        forecast_hour = kwargs["forecast_hour"]
        path = tmp_path / f"f{forecast_hour:03d}.grib2"
        path.write_bytes(_Response().content)
        return path, evidence, f"https://example.invalid/f{forecast_hour:03d}"

    def open_types(path: Path, requested):
        lead = int(path.stem[1:])
        valid = np.datetime64((T0 + timedelta(hours=lead)).replace(tzinfo=None))
        coords = {"valid_time": valid, "latitude": [70.0], "longitude": [19.0]}
        wind = xr.Dataset(
            {
                "u10": (("latitude", "longitude"), [[1.0]]),
                "v10": (("latitude", "longitude"), [[2.0]]),
            },
            coords=coords,
        )
        wind["u10"].attrs.update({"units": "m s-1", "standard_name": "eastward_wind"})
        wind["v10"].attrs.update({"units": "m s-1", "standard_name": "northward_wind"})
        temperature = xr.Dataset(
            {"t2m": (("latitude", "longitude"), [[270.0]])}, coords=coords
        )
        temperature["t2m"].attrs["units"] = "K"
        visibility = xr.Dataset(
            {"vis": (("latitude", "longitude"), [[10_000.0]])}, coords=coords
        )
        visibility["vis"].attrs["units"] = "m"
        all_results = {
            "wind_field": wind,
            "temperature": temperature,
            "visibility": visibility,
        }
        return {name: all_results[name] for name in requested}

    monkeypatch.setattr(acquirer, "_obtain_gfs_file", obtain)
    monkeypatch.setattr(forecast_module, "_open_gfs_data_types", open_types)

    result = acquirer.acquire_gfs(
        route_id="route-a",
        bounds=Bounds(10, 68, 22, 79),
        as_of=T0,
        horizon_hours=6,
        step_hours=3,
    )

    assert len(result.records) == 9
    assert {record.valid_time for record in result.records} == {
        T0,
        T0 + timedelta(hours=3),
        T0 + timedelta(hours=6),
    }
    assert {record.data_type for record in result.records} == {
        "wind_field",
        "temperature",
        "visibility",
    }


def test_copernicus_requires_complete_credentials_before_toolbox_call(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("COPERNICUSMARINE_SERVICE_USERNAME", raising=False)
    monkeypatch.delenv("COPERNICUSMARINE_SERVICE_PASSWORD", raising=False)
    monkeypatch.delenv("COPERNICUSMARINE_USERNAME", raising=False)
    monkeypatch.delenv("COPERNICUSMARINE_PASSWORD", raising=False)
    acquirer = NativeForecastAcquirer(tmp_path / "data")

    with pytest.raises(RuntimeError, match="需要免费账户凭据"):
        acquirer.acquire_copernicus(
            route_id="route-a",
            bounds=Bounds(10, 68, 22, 79),
            start_time=T0,
            horizon_hours=6,
            data_types=("wave",),
        )


def test_copernicus_explicitly_requests_default_lat_lon_part(tmp_path):
    calls = []

    def open_dataset(**kwargs):
        calls.append(kwargs)
        dataset = xr.Dataset(
            {
                "VHM0": (("time", "latitude", "longitude"), [[[2.0]]]),
                "VMDR": (("time", "latitude", "longitude"), [[[10.0]]]),
                "VTPK": (("time", "latitude", "longitude"), [[[8.0]]]),
            },
            coords={
                "time": [np.datetime64(T0.replace(tzinfo=None))],
                "latitude": [75.0],
                "longitude": [20.0],
            },
        )
        dataset["VHM0"].attrs["units"] = "m"
        dataset["VMDR"].attrs.update(
            {"units": "degree", "standard_name": "sea_surface_wave_from_direction"}
        )
        dataset["VTPK"].attrs["units"] = "s"
        return dataset

    acquirer = NativeForecastAcquirer(
        tmp_path / "data", copernicus_open_dataset=open_dataset
    )
    result = acquirer.acquire_copernicus(
        route_id="route-a",
        bounds=Bounds(10, 68, 22, 79),
        start_time=T0,
        horizon_hours=6,
        data_types=("wave",),
    )

    assert len(result.records) == 1
    assert calls[0]["dataset_part"] == "default"
    assert result.records[0].metadata["dataset_part"] == "default"
