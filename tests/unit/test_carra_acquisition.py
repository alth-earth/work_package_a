"""Focused tests for the CARRA single-levels adapter (proposal A-WINTER-MET-001).

These tests do NOT touch the network or any real GRIB file. Download
(``_retrieve_carra_frame``) and GRIB parsing (``_open_carra_frame``) are mocked
with small synthetic xarray Datasets so the request-construction, cycle loop,
regridding gating, and publisher wiring can be exercised offline.

A real one-frame smoke against the live source is exercised separately by
``acquire_frame_smoke``/``acquire_carra_dry_run`` and is documented as a manual
validation step, not a CI test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from arctic_route_data.carra_acquisition import (
    CARRA_COVERAGE_END,
    CARRA_SOURCE_LABEL,
    CARRA_SUPPORTED_DATA_TYPES,
    CarraAcquisition,
    CarraAcquisitionResult,
    _carra_request,
    _target_grid,
    _wrap_longitude,
)
from arctic_route_data.forecast_acquisition import Bounds


# --------------------------------------------------------------------------- #
# synthetic frames                                                             #
# --------------------------------------------------------------------------- #
def _synthetic_frame(data_types, *, lat=69.0, lon=15.0):
    """A tiny curvilinear-ish Dataset per data type (2D lat/lon)."""

    ny, nx = 3, 3
    lat2d = np.full((ny, nx), lat, dtype="float64")
    lon2d = np.full((ny, nx), lon, dtype="float64")
    out = {}
    for dt in data_types:
        out[dt] = (
            np.ones((ny, nx), dtype="float64"),
            lat2d,
            lon2d,
        )
    return out


@pytest.fixture
def bounds():
    return Bounds(west=10.0, south=68.0, east=30.0, north=80.0)


@pytest.fixture
def driver(bounds, tmp_path):
    return CarraAcquisition(
        route_id="A-winter-smoke",
        bounds=bounds,
        data_root=tmp_path / "carra",
        publish=False,
    )


# --------------------------------------------------------------------------- #
# request construction                                                         #
# --------------------------------------------------------------------------- #
def test_carra_request_has_no_leadtime_and_uses_display_names():
    req = _carra_request(
        cycle=datetime(2026, 2, 15, 0, 0, tzinfo=UTC),
        data_types=("wind_field", "temperature", "visibility"),
    )
    # The dataset id is passed separately to client.retrieve(), not in the dict.
    assert "dataset" not in req
    assert req["domain"] == "east_domain"
    assert req["level_type"] == "surface_or_atmosphere"
    assert req["product_type"] == "analysis"
    assert req["data_format"] == "grib"
    # The operator-validated request does NOT include a leadtime_hour field.
    assert "leadtime_hour" not in req
    # Variables must use CDS display names, not GRIB shortNames.
    assert set(req["variable"]) == {
        "10m_u_component_of_wind",
        "10m_v_component_of_wind",
        "2m_temperature",
        "visibility",
    }
    assert req["year"] == ["2026"]
    assert req["month"] == ["02"]
    assert req["day"] == ["15"]
    assert req["time"] == ["00:00"]


def test_carra_request_single_type():
    req = _carra_request(
        cycle=datetime(2026, 2, 15, 3, 0, tzinfo=UTC),
        data_types=("temperature",),
    )
    assert req["variable"] == ["2m_temperature"]


def test_carra_request_unsupported_type_raises():
    with pytest.raises(KeyError):
        _carra_request(
            cycle=datetime(2026, 2, 15, 0, 0, tzinfo=UTC),
            data_types=("not_a_type",),
        )


# --------------------------------------------------------------------------- #
# longitude wrapping (CARRA is 2D curvilinear, 0..360)                         #
# --------------------------------------------------------------------------- #
def test_wrap_longitude_2d_wraps_in_place():
    import xarray as xr

    lon2d = np.array([[350.0, 355.0, 5.0], [10.0, 20.0, 30.0]])
    ds = xr.Dataset(
        {"x": (("y", "x"), np.zeros_like(lon2d))},
        coords={"longitude": (("y", "x"), lon2d)},
    )
    out = _wrap_longitude(ds)
    # 2D curvilinear is wrapped to -180..180 (350 -> -10, 355 -> -5).
    np.testing.assert_allclose(
        out["longitude"].values,
        np.array([[-10.0, -5.0, 5.0], [10.0, 20.0, 30.0]]),
    )


def test_wrap_longitude_1d_wraps_past_180():
    import xarray as xr

    # 1D monotonic ascending that crosses the antimeridian.
    lon1d = np.array([350.0, 355.0, 5.0, 10.0])
    ds = xr.Dataset(
        {"x": ("x", np.zeros_like(lon1d))},
        coords={"longitude": ("x", lon1d)},
    )
    out = _wrap_longitude(ds)
    # After wrap + sortby: ascending -10, -5, 5, 10.
    np.testing.assert_allclose(
        out["longitude"].values, np.array([-10.0, -5.0, 5.0, 10.0])
    )


# --------------------------------------------------------------------------- #
# target grid                                                                  #
# --------------------------------------------------------------------------- #
def test_target_grid_inclusive_bounds(bounds):
    lons, lats = _target_grid(bounds, step_deg=1.0)
    assert lons[0] == pytest.approx(10.0)
    assert lons[-1] == pytest.approx(30.0)
    assert lats[0] == pytest.approx(68.0)
    assert lats[-1] == pytest.approx(80.0)
    assert lons.size == 21
    assert lats.size == 13


# --------------------------------------------------------------------------- #
# full acquisition loop (mocked download + parse)                             #
# --------------------------------------------------------------------------- #
def _patch_frames(monkeypatch, driver, data_types):
    """Patch download/parse/client so no network or GRIB is touched."""

    calls = {}

    def fake_retrieve(client, cycle, data_types, out_path):
        calls[cycle] = out_path
        # Simulate a downloaded file (download creates the parent dir too).
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"GRIB")

    def fake_open(path, data_types):
        return {
            dt: _dataset_for(dt, lat=70.0, lon=15.0) for dt in data_types
        }

    class _DummyClient:
        def retrieve(self, *args, **kwargs):
            raise AssertionError("network must not be reached in unit tests")

    monkeypatch.setattr(
        "arctic_route_data.carra_acquisition._retrieve_carra_frame", fake_retrieve
    )
    monkeypatch.setattr(
        "arctic_route_data.carra_acquisition._open_carra_frame", fake_open
    )
    monkeypatch.setattr(
        "arctic_route_data.carra_acquisition._cds_client", lambda: _DummyClient()
    )
    return calls


def _dataset_for(dt, *, lat, lon):
    ny, nx = 3, 3
    lat2d = np.full((ny, nx), lat, dtype="float64")
    lon2d = np.full((ny, nx), lon, dtype="float64")
    val = np.ones((ny, nx), dtype="float64")
    da = __import__("xarray").DataArray(
        val,
        dims=("y", "x"),
        coords={"latitude": (("y", "x"), lat2d), "longitude": (("y", "x"), lon2d)},
        name=dt,
    )
    return __import__("xarray").Dataset({dt: da})


def test_acquire_dry_run_counts_frames(monkeypatch, driver, bounds):
    data_types = CARRA_SUPPORTED_DATA_TYPES
    calls = _patch_frames(monkeypatch, driver, data_types)

    # 2026-02-15T00Z .. +6h inclusive => 3 cycles (00, 03, 06).
    start = datetime(2026, 2, 15, 0, 0, tzinfo=UTC)
    result = driver.acquire(start_time=start, horizon_hours=6)

    assert isinstance(result, CarraAcquisitionResult)
    assert result.source == CARRA_SOURCE_LABEL
    # frames_processed counts (cycle x data_type) regridded datasets:
    # 3 cycles x 3 data types = 9 in dry-run, none published.
    assert result.frames_processed == 9
    assert result.frames_published == 0
    assert result.published is False
    assert len(result.source_snapshot_ids) == 3
    assert len(calls) == 3
    # Every cycle within coverage was requested.
    assert datetime(2026, 2, 15, 6, 0, tzinfo=UTC) in calls


def test_acquire_publish_true_invokes_publisher(monkeypatch, driver, bounds):
    data_types = CARRA_SUPPORTED_DATA_TYPES
    _patch_frames(monkeypatch, driver, data_types)

    published = []

    class FakePublisher:
        def publish_dataset(self, dataset, *, source, version, issue_evidence,
                            data_type, route_id, metadata=None):
            published.append((data_type, route_id))
            return f"snap-{data_type}"

    driver.publisher = FakePublisher()
    driver.publish = True

    start = datetime(2026, 2, 15, 0, 0, tzinfo=UTC)
    result = driver.acquire(start_time=start, horizon_hours=3)

    # 2 cycles (00, 03) x 3 data types = 6 publishes.
    assert result.frames_processed == 6
    assert result.frames_published == 6
    assert result.published is True
    assert len(published) == 6
    # source_snapshot_ids recorded one per cycle (2 cycles).
    assert len(result.source_snapshot_ids) == 2


def test_acquire_rejects_nonpositive_horizon(driver):
    with pytest.raises(ValueError):
        driver.acquire(start_time=datetime(2026, 2, 15, tzinfo=UTC), horizon_hours=0)


def test_acquire_rejects_start_past_coverage(driver):
    start = CARRA_COVERAGE_END + timedelta(days=1)
    with pytest.raises(ValueError):
        driver.acquire(start_time=start, horizon_hours=3)


def test_acquire_stops_at_coverage_end(monkeypatch, driver):
    _patch_frames(monkeypatch, driver, CARRA_SUPPORTED_DATA_TYPES)
    # Start at coverage end; only that single cycle is in range.
    result = driver.acquire(start_time=CARRA_COVERAGE_END, horizon_hours=72)
    # 1 cycle x 3 data types = 3 processed frames.
    assert result.frames_processed == 3


def test_supported_data_types_constant():
    assert CARRA_SUPPORTED_DATA_TYPES == ("wind_field", "temperature", "visibility")
