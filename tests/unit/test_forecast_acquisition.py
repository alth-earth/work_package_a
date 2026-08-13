from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

import arctic_route_data.forecast_acquisition as forecast_module
from arctic_route_data.forecast_acquisition import (
    AcquisitionMode,
    Bounds,
    NativeForecastAcquirer,
    _gfs_request_signature,
    _select_hourly_aligned,
    build_gfs_filter_params,
    forecast_hours,
    ncei_gfs_analysis_url,
    ncei_inventory_ranges,
    resolve_acquisition_window,
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


def test_explicit_end_and_horizon_resolve_to_the_same_utc_window():
    by_end = resolve_acquisition_window(
        start_time=T0,
        end_time=T0 + timedelta(hours=96),
        mode="retrospective_best_estimate",
    )
    by_horizon = resolve_acquisition_window(start_time=T0, horizon_hours=96)

    assert by_end.horizon_hours == by_horizon.horizon_hours == 96
    assert by_end.mode is AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE
    with pytest.raises(ValueError, match="只能指定一个"):
        resolve_acquisition_window(
            start_time=T0, end_time=T0 + timedelta(hours=1), horizon_hours=1
        )


def test_ncei_analysis_url_and_inventory_ranges_select_only_required_messages():
    inventory = "\n".join(
        [
            "1:0:d=2026071500:PRMSL:mean sea level:anl:",
            "2:100:d=2026071500:VIS:surface:anl:",
            "3:200:d=2026071500:TMP:2 m above ground:anl:",
            "4:300:d=2026071500:UGRD:10 m above ground:anl:",
            "5:400:d=2026071500:VGRD:10 m above ground:anl:",
            "6:500:d=2026071500:APCP:surface:anl:",
        ]
    )

    assert ncei_gfs_analysis_url(T0).endswith(
        "/202607/20260715/gfs_4_20260715_0000_000.grb2"
    )
    assert ncei_inventory_ranges(
        inventory, data_types=("wind_field", "temperature", "visibility")
    ) == ((100, 199), (200, 299), (300, 399), (400, 499))


def test_ncei_range_downloader_never_fetches_full_global_object(tmp_path):
    inventory = "\n".join(
        [
            "1:0:d=2026071500:VIS:surface:anl:",
            "2:16:d=2026071500:TMP:2 m above ground:anl:",
            "3:32:d=2026071500:UGRD:10 m above ground:anl:",
            "4:48:d=2026071500:VGRD:10 m above ground:anl:",
            "5:64:d=2026071500:APCP:surface:anl:",
        ]
    )

    class Response:
        def __init__(
            self,
            content=b"",
            *,
            text="",
            headers=None,
            status_code=200,
        ):
            self.content = content
            self.text = text
            self.headers = headers or {}
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def close(self):
            return None

    class Session:
        def __init__(self):
            self.headers = []

        def get(self, url, *, timeout, headers=None, stream=False):
            if url.endswith(".inv"):
                return Response(
                    text=inventory,
                    content=inventory.encode(),
                    headers={"Last-Modified": "Sat, 18 Jul 2026 08:49:00 GMT"},
                )
            assert stream is True
            self.headers.append(headers)
            body = (
                b"GRIB" + b"x" * 12
                if len(self.headers) == 1
                else b"x" * 12 + b"7777"
                if len(self.headers) == 4
                else b"x" * 16
            )
            byte_range = headers["Range"].removeprefix("bytes=")
            return Response(
                body,
                status_code=206,
                headers={
                    "Content-Range": f"bytes {byte_range}/149691871",
                    "Content-Length": "16",
                },
            )

    session = Session()
    acquirer = NativeForecastAcquirer(tmp_path / "data", http_session=session)
    path, evidence, _ = acquirer._obtain_ncei_analysis_file(
        cycle=T0,
        data_types=("wind_field", "temperature", "visibility"),
    )

    assert path.is_file()
    assert len(session.headers) == 4
    assert all(request and "Range" in request for request in session.headers)
    assert evidence.method is IssueTimeMethod.HTTP_LAST_MODIFIED
    assert evidence.issue_time > T0
    request_metadata = path.with_suffix(path.suffix + ".metadata.json")
    assert request_metadata.is_file()
    metadata = forecast_module.json.loads(request_metadata.read_text(encoding="utf-8"))
    assert metadata["byte_ranges"] == [[0, 15], [16, 31], [32, 47], [48, 63]]
    assert (path.parent / metadata["inventory_file"]).is_file()


def test_ncei_range_downloader_rejects_a_proxy_that_ignores_range(tmp_path):
    inventory = "\n".join(
        [
            "1:0:d=2026071500:VIS:surface:anl:",
            "2:16:d=2026071500:APCP:surface:anl:",
        ]
    )

    class Response:
        def __init__(self, *, inventory_response=False):
            self.text = inventory if inventory_response else ""
            self.content = inventory.encode() if inventory_response else b""
            self.headers = {}
            self.status_code = 200

        def raise_for_status(self):
            return None

        def close(self):
            return None

    class Session:
        def get(self, url, *, timeout, headers=None, stream=False):
            return Response(inventory_response=url.endswith(".inv"))

    with pytest.raises(forecast_module.DataValidationError, match="206 Partial"):
        NativeForecastAcquirer(
            tmp_path / "data",
            http_session=Session(),
        )._obtain_ncei_analysis_file(
            cycle=T0,
            data_types=("visibility",),
        )


def test_total_current_is_strictly_selected_to_utc_hours_before_load():
    time = np.arange(
        np.datetime64("2026-07-15T00:00"),
        np.datetime64("2026-07-15T02:15"),
        np.timedelta64(15, "m"),
    )
    dataset = xr.Dataset({"vxo": ("time", np.arange(time.size))}, coords={"time": time})

    selected = _select_hourly_aligned(
        dataset, start=T0, end=T0 + timedelta(hours=2)
    )

    np.testing.assert_array_equal(
        selected.time.values,
        time[[0, 4, 8]].astype("datetime64[ns]"),
    )
    missing = dataset.isel(time=[0, 1, 2, 3, 5, 6, 7, 8])
    with pytest.raises(forecast_module.DataValidationError, match="禁止近邻"):
        _select_hourly_aligned(missing, start=T0, end=T0 + timedelta(hours=2))


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
        path = (
            tmp_path
            / "data"
            / "source_snapshots"
            / "gfs"
            / f"f{forecast_hour:03d}.grib2"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
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
    assert {record.metadata["nominal_interval_hours"] for record in result.records} == {
        3.0
    }
    assert all(
        record.metadata["source_snapshot_relative_path"].startswith(
            "source_snapshots/gfs/"
        )
        for record in result.records
    )


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
                "VHM0": (
                    ("time", "latitude", "longitude"),
                    [[[2.0, np.nan]], [[2.5, np.nan]]],
                ),
                "VMDR": (
                    ("time", "latitude", "longitude"),
                    [[[10.0, np.nan]], [[20.0, np.nan]]],
                ),
                "VTPK": (
                    ("time", "latitude", "longitude"),
                    [[[8.0, np.nan]], [[9.0, np.nan]]],
                ),
            },
            coords={
                "time": [
                    np.datetime64(T0.replace(tzinfo=None)),
                    np.datetime64((T0 + timedelta(hours=3)).replace(tzinfo=None)),
                ],
                "latitude": [75.0],
                "longitude": [20.0, 21.0],
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

    assert len(result.records) == 2
    assert calls[0]["dataset_part"] == "default"
    record = result.records[0]
    assert record.metadata["dataset_part"] == "default"
    assert record.metadata["nominal_interval_hours"] == 3.0
    assert record.metadata["source_snapshot_relative_path"].startswith(
        "source_snapshots/copernicus/"
    )
    assert record.quality_flag.value == "suspect"
    assert record.metadata["content_qc"]["structural_mask_fraction"] == 0.5
    assert set(record.metadata["content_qc"]["valid_domain_missing_fraction"].values()) == {
        0.0
    }
    mask_metadata = record.metadata["normalization"]["source_valid_mask"]
    assert mask_metadata["present"] is True
    assert mask_metadata["semantic_role"] == "source_valid_domain"
    assert mask_metadata["navigation_semantics"] == "none"
    for item in result.records:
        with xr.open_dataset(tmp_path / "data" / item.relative_path) as published:
            assert published.source_valid_mask.dtype == np.dtype(bool)
            assert published.source_valid_mask.dims == ("latitude", "longitude")
            assert published.source_valid_mask.values.tolist() == [[True, False]]


def test_copernicus_valid_domain_gaps_still_lower_content_quality(tmp_path):
    values = np.array(
        [
            [[np.nan, 0.2, 0.2, 0.2, 0.2, 0.2]],
            [[np.nan, np.nan, np.nan, 0.3, 0.3, 0.3]],
        ]
    )

    def open_dataset(**kwargs):
        dataset = xr.Dataset(
            {"siconc": (("time", "latitude", "longitude"), values)},
            coords={
                "time": [
                    np.datetime64(T0.replace(tzinfo=None)),
                    np.datetime64((T0 + timedelta(hours=1)).replace(tzinfo=None)),
                ],
                "latitude": [75.0],
                "longitude": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            },
        )
        dataset["siconc"].attrs.update(
            {"units": "1", "standard_name": "sea_ice_area_fraction"}
        )
        return dataset

    acquirer = NativeForecastAcquirer(
        tmp_path / "data", copernicus_open_dataset=open_dataset
    )
    result = acquirer.acquire_copernicus(
        route_id="route-a",
        bounds=Bounds(10, 68, 22, 79),
        start_time=T0,
        horizon_hours=1,
        data_types=("sea_ice_concentration",),
    )

    assert [record.quality_flag.value for record in result.records] == [
        "suspect",
        "degraded",
    ]
    first_qc = result.records[0].metadata["content_qc"]
    second_qc = result.records[1].metadata["content_qc"]
    assert first_qc["structural_mask_fraction"] == pytest.approx(1 / 6)
    assert first_qc["maximum_valid_domain_missing_fraction"] == 0
    assert second_qc["maximum_valid_domain_missing_fraction"] == pytest.approx(0.4)


def test_copernicus_source_valid_domain_requires_every_requested_variable(tmp_path):
    def open_dataset(**kwargs):
        dataset = xr.Dataset(
            {
                "VHM0": (("time", "latitude", "longitude"), [[[2.0, 2.0]]]),
                "VMDR": (("time", "latitude", "longitude"), [[[10.0, np.nan]]]),
                "VTPK": (("time", "latitude", "longitude"), [[[8.0, 8.0]]]),
            },
            coords={
                "time": [np.datetime64(T0.replace(tzinfo=None))],
                "latitude": [75.0],
                "longitude": [20.0, 21.0],
            },
        )
        dataset["VHM0"].attrs["units"] = "m"
        dataset["VMDR"].attrs.update(
            {"units": "degree", "standard_name": "sea_surface_wave_from_direction"}
        )
        dataset["VTPK"].attrs["units"] = "s"
        return dataset

    result = NativeForecastAcquirer(
        tmp_path / "data", copernicus_open_dataset=open_dataset
    ).acquire_copernicus(
        route_id="route-a",
        bounds=Bounds(10, 68, 22, 79),
        start_time=T0,
        horizon_hours=1,
        data_types=("wave",),
    )

    with xr.open_dataset(tmp_path / "data" / result.records[0].relative_path) as frame:
        assert frame.source_valid_mask.values.tolist() == [[True, False]]
        assert (
            frame.source_valid_mask.attrs["derivation_method"]
            == "all_required_variables_finite_over_complete_requested_dataset"
        )
        assert frame.source_valid_mask.attrs["semantic_version"] == "a.source-valid-mask.v2"


def test_copernicus_reuses_one_nextsim_download_for_type_and_edge(tmp_path):
    calls = []

    def open_dataset(**kwargs):
        calls.append(kwargs)
        dataset = xr.Dataset(
            {
                "siconc": (("time", "latitude", "longitude"), [[[0.3, 0.0]]]),
                "siconc_young": (("time", "latitude", "longitude"), [[[0.1, 0.0]]]),
                "siconc_my": (("time", "latitude", "longitude"), [[[0.05, 0.0]]]),
            },
            coords={
                "time": [np.datetime64(T0.replace(tzinfo=None))],
                "latitude": [75.0],
                "longitude": [20.0, 21.0],
            },
        )
        for name in ("siconc", "siconc_young", "siconc_my"):
            dataset[name].attrs["units"] = "1"
        dataset["siconc"].attrs["standard_name"] = "sea_ice_area_fraction"
        return dataset

    result = NativeForecastAcquirer(
        tmp_path / "data", copernicus_open_dataset=open_dataset
    ).acquire_copernicus(
        route_id="route-a",
        bounds=Bounds(10, 68, 22, 79),
        start_time=T0,
        horizon_hours=1,
        data_types=("sea_ice_edge", "sea_ice_type"),
    )

    assert len(calls) == 1
    assert set(calls[0]["variables"]) == {"siconc", "siconc_young", "siconc_my"}
    assert {record.data_type for record in result.records} == {
        "sea_ice_type",
        "sea_ice_edge",
    }


def test_total_current_is_preferred_and_detided_is_explicit_fallback(tmp_path):
    calls = []

    def open_dataset(**kwargs):
        calls.append(kwargs)
        if kwargs["dataset_id"] == "dataset-topaz6-arc-15min-3km-be":
            raise RuntimeError("preferred unavailable")
        dataset = xr.Dataset(
            {
                "vxo": (("time", "latitude", "longitude"), [[[0.1]]]),
                "vyo": (("time", "latitude", "longitude"), [[[0.2]]]),
            },
            coords={
                "time": [np.datetime64(T0.replace(tzinfo=None))],
                "latitude": [75.0],
                "longitude": [20.0],
            },
        )
        dataset.vxo.attrs.update(
            {"units": "m s-1", "standard_name": "eastward_sea_water_velocity"}
        )
        dataset.vyo.attrs.update(
            {"units": "m s-1", "standard_name": "northward_sea_water_velocity"}
        )
        return dataset

    result = NativeForecastAcquirer(
        tmp_path / "data", copernicus_open_dataset=open_dataset
    ).acquire_copernicus(
        route_id="route-a",
        bounds=Bounds(10, 68, 22, 79),
        start_time=T0,
        horizon_hours=1,
        data_types=("ocean_current",),
    )

    assert [call["dataset_id"] for call in calls] == [
        "dataset-topaz6-arc-15min-3km-be",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
    ]
    assert result.records[0].metadata["current_component"] == "detided"
    assert result.records[0].metadata["tide_included"] is False
    assert result.records[0].metadata["source_combination_policy"].endswith("never_sum")
    assert "两者未相加" in result.warnings[0]
