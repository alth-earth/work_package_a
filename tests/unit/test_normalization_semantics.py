import json
from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.ingestion import _content_quality
from arctic_route_data.models import QualityFlag
from arctic_route_data.normalization import normalize_dataset, spatial_metadata

T0 = datetime(2026, 7, 15, 6, tzinfo=UTC)


def _source_valid_mask(values):
    mask = xr.DataArray(
        np.asarray(values, dtype=bool), dims=("latitude", "longitude")
    )
    mask.attrs = {
        "semantic_version": "a.source-valid-mask.v1",
        "semantic_role": "source_valid_domain",
        "derivation_method": (
            "any_required_variable_finite_over_complete_requested_dataset"
        ),
        "derivation_scope": "native_copernicus_request_before_temporal_split",
        "required_source_variables": json.dumps(["siconc"]),
        "requested_start": T0.isoformat(),
        "requested_end": T0.isoformat(),
        "navigation_semantics": "none",
        "classification_semantics": "none",
    }
    return mask


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
    assert result.mean_wave_direction.attrs["positive_direction"] == "clockwise"
    assert result.mean_wave_direction.attrs["source_standard_name"] == (
        "sea_surface_wave_to_direction"
    )


def test_wave_explicit_east_counterclockwise_to_direction_is_fully_converted():
    dataset = xr.Dataset(
        {
            "VHM0": (("latitude", "longitude"), [[2.0]]),
            "VMDR": (("latitude", "longitude"), [[30.0]]),
            "VTPK": (("latitude", "longitude"), [[8.0]]),
        },
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["VHM0"].attrs["units"] = "m"
    dataset["VMDR"].attrs.update(
        {
            "units": "degree",
            "direction_convention": "to",
            "reference_direction": "true_east",
            "positive_direction": "counterclockwise",
        }
    )
    dataset["VTPK"].attrs["units"] = "s"

    result = _normalize(dataset, "wave")

    assert float(result.mean_wave_direction.item()) == 240.0
    assert result.mean_wave_direction.attrs["source_direction_convention"] == "to"
    assert result.mean_wave_direction.attrs["source_reference_direction"] == "true_east"
    assert result.mean_wave_direction.attrs["source_positive_direction"] == (
        "counterclockwise"
    )


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("reference_direction", "magnetic_north", "reference_direction 未知"),
        ("positive_direction", "sideways", "positive_direction 未知"),
    ],
)
def test_wave_unknown_orientation_declarations_are_rejected(attribute, value, message):
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
        {
            "units": "degree",
            "direction_convention": "from",
            "reference_direction": "true_north",
            "positive_direction": "clockwise",
            attribute: value,
        }
    )
    dataset["VTPK"].attrs["units"] = "s"

    with pytest.raises(DataValidationError, match=message):
        _normalize(dataset, "wave")


def test_wave_standard_name_and_direction_convention_conflict_is_rejected():
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
        {
            "units": "degree",
            "standard_name": "sea_surface_wave_from_direction",
            "direction_convention": "to",
        }
    )
    dataset["VTPK"].attrs["units"] = "s"

    with pytest.raises(DataValidationError, match="standard_name 与方向声明冲突"):
        _normalize(dataset, "wave")


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
    assert result.elevation.attrs["source_vertical_positive"] == "down"
    assert result.elevation.attrs["source_standard_name"] == (
        "sea_floor_depth_below_geoid"
    )


def test_bathymetry_trusted_cf_standard_name_can_supply_positive_direction():
    dataset = xr.Dataset(
        {"depth": (("latitude", "longitude"), [[100.0]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["depth"].attrs.update(
        {"units": "m", "standard_name": "sea_floor_depth_below_mean_sea_level"}
    )

    result = _normalize(dataset, "bathymetry")

    assert float(result.elevation.item()) == -100.0
    assert result.elevation.attrs["source_vertical_positive"] == (
        "down_by_standard_name"
    )


def test_bathymetry_explicit_positive_up_needs_no_variable_name_inference():
    dataset = xr.Dataset(
        {"z": (("latitude", "longitude"), [[-100.0]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["z"].attrs.update({"units": "m", "positive": "up"})

    result = _normalize(dataset, "bathymetry")

    assert float(result.elevation.item()) == -100.0
    assert result.elevation.attrs["source_vertical_positive"] == "up"


def test_bathymetry_variable_name_without_direction_evidence_is_rejected():
    dataset = xr.Dataset(
        {"elevation": (("latitude", "longitude"), [[-100.0]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["elevation"].attrs["units"] = "m"

    with pytest.raises(DataValidationError, match="不能仅按源变量 'elevation' 猜测"):
        _normalize(dataset, "bathymetry")


def test_bathymetry_positive_and_standard_name_conflict_is_rejected():
    dataset = xr.Dataset(
        {"depth": (("latitude", "longitude"), [[100.0]])},
        coords={"latitude": [75.0], "longitude": [20.0]},
    )
    dataset["depth"].attrs.update(
        {
            "units": "m",
            "positive": "up",
            "standard_name": "sea_floor_depth_below_geoid",
        }
    )

    with pytest.raises(DataValidationError, match="垂直正方向冲突"):
        _normalize(dataset, "bathymetry")


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


def test_source_valid_mask_is_preserved_as_boolean_with_auditable_semantics():
    dataset = xr.Dataset(
        {
            "siconc": (("latitude", "longitude"), [[0.2, np.nan]]),
            "source_valid_mask": _source_valid_mask([[True, False]]),
        },
        coords={"latitude": [75.0], "longitude": [20.0, 21.0]},
    )
    dataset.siconc.attrs["units"] = "1"

    result = _normalize(dataset, "sea_ice_concentration")

    assert result.source_valid_mask.dtype == np.dtype(bool)
    assert result.source_valid_mask.values.tolist() == [[True, False]]
    assert result.attrs["qc_structural_mask_fraction"] == "0.5"
    assert json.loads(result.attrs["qc_valid_domain_missing_fraction"]) == {
        "ice_concentration": 0.0
    }


def test_source_valid_mask_rejects_non_boolean_and_empty_valid_domain():
    dataset = xr.Dataset(
        {
            "siconc": (("latitude", "longitude"), [[0.2, 0.3]]),
            "source_valid_mask": _source_valid_mask([[True, False]]).astype("int8"),
        },
        coords={"latitude": [75.0], "longitude": [20.0, 21.0]},
    )
    dataset.siconc.attrs["units"] = "1"
    with pytest.raises(DataValidationError, match="boolean dtype"):
        _normalize(dataset, "sea_ice_concentration")

    dataset["source_valid_mask"] = _source_valid_mask([[False, False]])
    with pytest.raises(DataValidationError, match="没有任何有效空间单元"):
        _normalize(dataset, "sea_ice_concentration")


def test_content_qc_degrades_and_rejects_residual_valid_domain_missingness():
    degraded = xr.Dataset(
        {
            "siconc": (("latitude", "longitude"), [[0.1, 0.2, 0.3, np.nan]]),
            "source_valid_mask": _source_valid_mask([[True, True, True, True]]),
        },
        coords={"latitude": [75.0], "longitude": [20.0, 21.0, 22.0, 23.0]},
    )
    degraded.siconc.attrs["units"] = "1"
    quality, qc = _content_quality(_normalize(degraded, "sea_ice_concentration"))
    assert quality is QualityFlag.DEGRADED
    assert qc["maximum_valid_domain_missing_fraction"] == 0.25
    assert qc["structural_mask_fraction"] == 0.0

    rejected = xr.Dataset(
        {
            "siconc": (
                ("latitude", "longitude"),
                [[0.1, np.nan, np.nan, np.nan, np.nan, np.nan]],
            ),
            "source_valid_mask": _source_valid_mask(
                [[True, True, True, True, True, True]]
            ),
        },
        coords={
            "latitude": [75.0],
            "longitude": [20.0, 21.0, 22.0, 23.0, 24.0, 25.0],
        },
    )
    rejected.siconc.attrs["units"] = "1"
    with pytest.raises(DataValidationError, match="最大有效域缺测比例"):
        _content_quality(_normalize(rejected, "sea_ice_concentration"))


def test_legacy_frame_without_explicit_mask_does_not_infer_structural_domain():
    dataset = xr.Dataset(
        {"siconc": (("latitude", "longitude"), [[0.1, np.nan]])},
        coords={"latitude": [75.0], "longitude": [20.0, 21.0]},
    )
    dataset.siconc.attrs["units"] = "1"

    _, qc = _content_quality(_normalize(dataset, "sea_ice_concentration"))

    assert qc["structural_mask_fraction"] is None
    assert qc["source_valid_mask"] == {
        "present": False,
        "inference": "not_performed_without_explicit_source_evidence",
    }
