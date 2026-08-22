"""Unit tests for the conservative curvilinear -> rectilinear regridder."""

from __future__ import annotations

import numpy as np
import pytest

from arctic_route_data.curvilinear import regrid_nearest_curvilinear


def _make_native(
    size: int = 21,
    spacing_km: float = 6.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # approximate regular grid around (70N, 40E) used as a "curvilinear" native grid
    lat0, lon0 = 70.0, 40.0
    dlat = spacing_km / 111.195
    dlon = spacing_km / (111.195 * np.cos(np.radians(lat0)))
    ys = np.arange(size) - size // 2
    xs = np.arange(size) - size // 2
    lat, lon = np.meshgrid(lat0 + ys * dlat, lon0 + xs * dlon, indexing="ij")
    return lat, lon, np.ones((size, size), dtype=np.float64)


def test_native_finite_recovers_rectilinear_cell() -> None:
    lat, lon, values = _make_native()
    target_lat = np.array([70.0])
    target_lon = np.array([40.0])
    out = regrid_nearest_curvilinear(
        values=values,
        native_lon=lon,
        native_lat=lat,
        target_lon=target_lon,
        target_lat=target_lat,
        max_distance_km=15.0,
    )
    assert out.shape == (1, 1)
    assert np.isfinite(out[0, 0])


def test_genuinely_missing_stays_nan() -> None:
    lat, lon, values = _make_native()
    values[:] = np.nan
    out = regrid_nearest_curvilinear(
        values=values,
        native_lon=lon,
        native_lat=lat,
        target_lon=np.array([40.0]),
        target_lat=np.array([70.0]),
        max_distance_km=15.0,
    )
    assert np.isnan(out[0, 0])


def test_distance_threshold_keeps_nan() -> None:
    lat, lon, values = _make_native()
    # target 100 km away from any native cell
    out = regrid_nearest_curvilinear(
        values=values,
        native_lon=lon,
        native_lat=lat,
        target_lon=np.array([40.0 + 100.0 / (111.195 * np.cos(np.radians(70.0)))]),
        target_lat=np.array([70.0]),
        max_distance_km=15.0,
    )
    assert np.isnan(out[0, 0])


def test_land_target_not_filled_across_coast() -> None:
    lat, lon, values = _make_native()
    target_lat = np.array([70.0])
    target_lon = np.array([40.0])
    target_water = np.array([[False]])  # land cell: must stay NaN
    out = regrid_nearest_curvilinear(
        values=values,
        native_lon=lon,
        native_lat=lat,
        target_lon=target_lon,
        target_lat=target_lat,
        max_distance_km=15.0,
        target_water_mask=target_water,
    )
    assert np.isnan(out[0, 0])


def test_native_water_mask_restricts_candidates() -> None:
    lat, lon, values = _make_native()
    water = np.zeros_like(values, dtype=bool)
    water[lat.shape[0] // 2, lat.shape[1] // 2] = True
    out = regrid_nearest_curvilinear(
        values=values,
        native_lon=lon,
        native_lat=lat,
        target_lon=np.array([40.0]),
        target_lat=np.array([70.0]),
        max_distance_km=15.0,
        native_water_mask=water,
    )
    assert np.isfinite(out[0, 0])


def test_component_values_preserved_at_nearest_cell() -> None:
    lat, lon, values = _make_native()
    values[:] = np.nan
    center = lat.shape[0] // 2
    values[center, center] = 3.25
    out = regrid_nearest_curvilinear(
        values=values,
        native_lon=lon,
        native_lat=lat,
        target_lon=np.array([40.0]),
        target_lat=np.array([70.0]),
        max_distance_km=15.0,
    )
    assert out[0, 0] == pytest.approx(3.25)
