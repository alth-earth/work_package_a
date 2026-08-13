"""Small deterministic A-owned preprocessing steps with explicit semantics."""

from __future__ import annotations

import numpy as np
import xarray as xr

ICE_EDGE_CONCENTRATION_THRESHOLD = 0.15


def derive_sea_ice_type(dataset: xr.Dataset) -> xr.Dataset:
    """Turn neXtSIM concentration fractions into a documented dominant class.

    Codes are 0=open water (<15%), 1=young ice, 2=residual/first-year ice,
    and 3=multi-year ice. This is deterministic preprocessing, not an ML model.
    """

    required = {"siconc", "siconc_young", "siconc_my"}
    missing = sorted(required - set(dataset.data_vars))
    if missing:
        raise ValueError("neXtSIM 冰型派生缺少变量: " + ", ".join(missing))
    concentration = dataset["siconc"]
    raw_young = dataset["siconc_young"]
    raw_multi_year = dataset["siconc_my"]
    component_valid = (
        concentration.notnull() & raw_young.notnull() & raw_multi_year.notnull()
    )
    young = raw_young.clip(min=0)
    multi_year = raw_multi_year.clip(min=0)
    residual = (concentration - young - multi_year).clip(min=0)
    dominant = (
        xr.concat((young, residual, multi_year), dim="ice_component")
        .fillna(-np.inf)
        .argmax("ice_component", skipna=False)
        + 1
    )
    ice_type = dominant.where(
        concentration >= ICE_EDGE_CONCENTRATION_THRESHOLD, 0
    ).where(component_valid)
    ice_type = ice_type.astype(np.float32)
    ice_type.name = "ice_type"
    ice_type.attrs = {
        "units": "1",
        "long_name": "dominant neXtSIM sea-ice type",
        "flag_values": "0 1 2 3",
        "flag_meanings": "open_water young_ice residual_first_year_ice multi_year_ice",
        "derivation_method": "dominant_concentration_fraction_v1",
        "open_water_threshold": ICE_EDGE_CONCENTRATION_THRESHOLD,
        "model_role": "none_deterministic_preprocessing",
    }
    result = dataset.copy()
    result["ice_type"] = ice_type
    return result


def derive_sea_ice_edge(dataset: xr.Dataset) -> xr.Dataset:
    """Create an ice-side four-neighbour edge mask from concentration >= 15%."""

    if "siconc" not in dataset.data_vars:
        raise ValueError("冰缘派生缺少 siconc")
    concentration = dataset["siconc"]
    spatial_dims = tuple(
        dim for dim in concentration.dims if dim.casefold() in {"latitude", "longitude"}
    )
    if len(spatial_dims) != 2:
        raise ValueError("冰缘派生要求规则经纬度网格")
    ice = concentration >= ICE_EDGE_CONCENTRATION_THRESHOLD
    open_neighbour = xr.zeros_like(ice, dtype=bool)
    for dim in spatial_dims:
        open_neighbour = (
            open_neighbour
            | ~ice.shift({dim: 1}, fill_value=True)
            | ~ice.shift({dim: -1}, fill_value=True)
        )
    edge = (ice & open_neighbour).where(concentration.notnull()).astype(np.float32)
    edge.name = "ice_edge"
    edge.attrs = {
        "units": "1",
        "long_name": "ice-side sea-ice edge mask",
        "flag_values": "0 1",
        "flag_meanings": "not_edge ice_edge",
        "derivation_method": "four_neighbour_ice_side_edge_v1",
        "ice_concentration_threshold": ICE_EDGE_CONCENTRATION_THRESHOLD,
        "model_role": "none_deterministic_preprocessing",
    }
    result = dataset.copy()
    result["ice_edge"] = edge
    return result


def derive_land_sea_mask(dataset: xr.Dataset) -> xr.Dataset:
    """Derive a formal surface-class mask from the same GEBCO elevation grid.

    ``1`` means sea (elevation < 0 m) and ``0`` means land/coast. It is not a
    navigability mask and must not be replaced by ``source_valid_mask``.
    """

    elevation_name = next(
        (name for name in ("elevation", "z") if name in dataset.data_vars), None
    )
    if elevation_name is None:
        raise ValueError("land_sea_mask 派生缺少 GEBCO elevation")
    elevation = dataset[elevation_name]
    mask = (elevation < 0).where(elevation.notnull()).astype(np.float32)
    mask.name = "land_sea_mask"
    mask.attrs = {
        "units": "1",
        "long_name": "GEBCO-derived land sea classification",
        "flag_values": "0 1",
        "flag_meanings": "land_or_coast sea",
        "derivation_method": "elevation_below_mean_sea_level_v1",
        "navigation_semantics": "none",
        "hard_mask_semantics": "none",
    }
    result = dataset.copy()
    result["land_sea_mask"] = mask
    return result
