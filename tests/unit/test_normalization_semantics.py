from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.normalization import normalize_dataset, spatial_metadata

T0 = datetime(2026, 7, 15, 6, tzinfo=UTC)


def _normalize(dataset: xr.Dataset, data_type: str) -> xr.Dataset:
    return normalize_dataset(
        dataset,
        data_type=data_type,
        valid_time=T0,
        issue_time=T0,
        route_id="route-a",
        source="test",
    )


def test_east_north_ice_drift_is_not_rotated_twice():
    dataset = xr.Dataset(
        {
            "vxsi": (("latitude", "longitude"), [[1.0, 1.0]]),
            "vysi": (("latitude", "longitude"), [[2.0, 2.0]]),
        },
        coords={"latitude": [75.0], "longitude": [-45.0, 0.0]},
    )
    dataset["vxsi"].attrs.update(
        {"units": "m s-1", "standard_name": "eastward_sea_ice_velocity"}
    )
    dataset["vysi"].attrs.update(
        {"units": "m s-1", "standard_name": "northward_sea_ice_velocity"}
    )

    result = _normalize(dataset, "sea_ice_drift")

    np.testing.assert_allclose(result.ice_drift_u.isel(time=0), [[1.0, 1.0]])
    np.testing.assert_allclose(result.ice_drift_v.isel(time=0), [[2.0, 2.0]])
    assert result.attrs["vector_rotation"] == "none"
    assert result.ice_drift_u.attrs["source_standard_name"] == (
        "eastward_sea_ice_velocity"
    )


def test_polar_xy_current_is_rotated_to_true_east_north():
    dataset = xr.Dataset(
        {
            "vxo": (("latitude", "longitude"), [[1.0, 1.0]]),
            "vyo": (("latitude", "longitude"), [[0.0, 0.0]]),
        },
        coords={"latitude": [75.0], "longitude": [-45.0, 0.0]},
        attrs={"copernicus_product": "ARCTIC_ANALYSISFORECAST_PHY_002_001"},
    )
    dataset["vxo"].attrs.update(
        {"units": "m s-1", "standard_name": "sea_water_x_velocity"}
    )
    dataset["vyo"].attrs.update(
        {"units": "m s-1", "standard_name": "sea_water_y_velocity"}
    )

    result = _normalize(dataset, "ocean_current")

    np.testing.assert_allclose(
        result.ocean_current_u.isel(time=0), [[1.0, 2**-0.5]], atol=1e-12
    )
    np.testing.assert_allclose(
        result.ocean_current_v.isel(time=0), [[0.0, -(2**-0.5)]], atol=1e-12
    )
    assert result.attrs["vector_rotation"] == (
        "polar_stereographic_xy_to_true_east_north"
    )


def test_ambiguous_projected_component_names_are_rejected():
    dataset = xr.Dataset(
        {
            "vxsi": (("latitude", "longitude"), [[1.0]]),
            "vysi": (("latitude", "longitude"), [[1.0]]),
        },
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["vxsi"].attrs["units"] = "m s-1"
    dataset["vysi"].attrs["units"] = "m s-1"

    with pytest.raises(DataValidationError, match="无法证明"):
        _normalize(dataset, "sea_ice_drift")


def test_wave_to_direction_is_converted_to_from_direction():
    dataset = xr.Dataset(
        {
            "VHM0": (("latitude", "longitude"), [[2.0]]),
            "VMDR": (("latitude", "longitude"), [[10.0]]),
            "VTPK": (("latitude", "longitude"), [[8.0]]),
        },
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["VHM0"].attrs["units"] = "m"
    dataset["VMDR"].attrs.update(
        {"units": "degree", "standard_name": "sea_surface_wave_to_direction"}
    )
    dataset["VTPK"].attrs["units"] = "s"

    result = _normalize(dataset, "wave")

    assert float(result.mean_wave_direction.item()) == 190.0
    assert result.mean_wave_direction.attrs["direction_convention"] == "from"
    assert result.mean_wave_direction.attrs["reference_direction"] == "true_north"


def test_unknown_units_and_invalid_physical_values_are_rejected():
    unknown = xr.Dataset(
        {"vis": (("latitude", "longitude"), [[10.0]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    unknown["vis"].attrs["units"] = "furlong"
    with pytest.raises(DataValidationError, match="不能转换"):
        _normalize(unknown, "visibility")

    invalid = xr.Dataset(
        {"siconc": (("latitude", "longitude"), [[1.2]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    invalid["siconc"].attrs["units"] = "1"
    with pytest.raises(DataValidationError, match="物理合同上限"):
        _normalize(invalid, "sea_ice_concentration")


def test_positive_down_bathymetric_depth_becomes_negative_elevation():
    dataset = xr.Dataset(
        {"depth": (("latitude", "longitude"), [[100.0]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["depth"].attrs.update(
        {
            "units": "m",
            "positive": "down",
            "standard_name": "sea_floor_depth_below_geoid",
        }
    )

    result = _normalize(dataset, "bathymetry")

    assert float(result.elevation.item()) == -100.0
    assert result.elevation.attrs["positive"] == "up"


def test_paired_point_coordinates_keep_pair_order_and_topology():
    dataset = xr.Dataset(
        {"vis": (("point",), [1000.0, 2000.0, 3000.0])},
        coords={
            "longitude": (("point",), [30.0, 10.0, 20.0]),
            "latitude": (("point",), [70.0, 72.0, 71.0]),
        },
    )
    dataset["vis"].attrs["units"] = "m"

    normalized = _normalize(dataset, "visibility")

    assert normalized.attrs["grid_topology"] == "unstructured_points"
    assert normalized["longitude"].values.tolist() == [30.0, 10.0, 20.0]
    assert normalized["latitude"].values.tolist() == [70.0, 72.0, 71.0]
    assert normalized["visibility"].values.reshape(-1).tolist() == [
        1000.0,
        2000.0,
        3000.0,
    ]
    assert spatial_metadata(normalized)[1] == (None, None)
