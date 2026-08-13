"""Canonical variable registry for all data types in the supplied acquisition package."""

from __future__ import annotations

from dataclasses import dataclass

from arctic_route_data.models import DataCategory


@dataclass(frozen=True, slots=True)
class VariableSpec:
    canonical_name: str
    aliases: tuple[str, ...]
    canonical_unit: str | None = None


@dataclass(frozen=True, slots=True)
class DataTypeSpec:
    name: str
    category: DataCategory
    variables: tuple[VariableSpec, ...]
    source_family: str
    allow_single_variable_fallback: bool = False


DATA_TYPE_SPECS: dict[str, DataTypeSpec] = {
    "sea_ice_concentration": DataTypeSpec(
        "sea_ice_concentration",
        DataCategory.SLOW,
        (
            VariableSpec(
                "ice_concentration",
                ("ice_conc", "sic", "siconc", "sea_ice_area_fraction", "ice concentration"),
                "1",
            ),
        ),
        "Copernicus Marine / OSI SAF",
    ),
    "sea_ice_type": DataTypeSpec(
        "sea_ice_type",
        DataCategory.SLOW,
        (VariableSpec("ice_type", ("ice_type", "ice_class", "ice_type_code"), "1"),),
        "Copernicus Marine neXtSIM / OSI SAF legacy",
        True,
    ),
    "sea_ice_edge": DataTypeSpec(
        "sea_ice_edge",
        DataCategory.SLOW,
        (VariableSpec("ice_edge", ("ice_edge", "ice_edge_code", "ice_edge_type"), "1"),),
        "Derived from Copernicus Marine sea-ice concentration / OSI SAF legacy",
        True,
    ),
    "sea_ice_drift": DataTypeSpec(
        "sea_ice_drift",
        DataCategory.SLOW,
        (
            VariableSpec(
                "ice_drift_u",
                ("vxsi", "usi", "uice", "ice_u", "eastward_sea_ice_velocity"),
                "m s-1",
            ),
            VariableSpec(
                "ice_drift_v",
                ("vysi", "vsi", "vice", "ice_v", "northward_sea_ice_velocity"),
                "m s-1",
            ),
        ),
        "Copernicus Marine",
    ),
    "sea_ice_thickness": DataTypeSpec(
        "sea_ice_thickness",
        DataCategory.SLOW,
        (VariableSpec("ice_thickness", ("sithick", "sit", "sea_ice_thickness"), "m"),),
        "Copernicus Marine",
    ),
    "wave": DataTypeSpec(
        "wave",
        DataCategory.DYNAMIC,
        (
            VariableSpec(
                "significant_wave_height",
                ("VHM0", "swh", "hs", "sea_surface_wave_significant_height"),
                "m",
            ),
            VariableSpec(
                "mean_wave_direction",
                ("VMDR", "mwd", "sea_surface_wave_from_direction"),
                "degree",
            ),
            VariableSpec(
                "peak_wave_period",
                (
                    "VTPK",
                    "pp1d",
                    "tp",
                    "sea_surface_wave_period_at_variance_spectral_density_maximum",
                ),
                "s",
            ),
        ),
        "Copernicus Marine",
    ),
    "ocean_current": DataTypeSpec(
        "ocean_current",
        DataCategory.SLOW,
        (
            VariableSpec(
                "ocean_current_u",
                ("uo", "vxo", "vozocrtx", "eastward_sea_water_velocity"),
                "m s-1",
            ),
            VariableSpec(
                "ocean_current_v",
                ("vo", "vyo", "vomecrty", "northward_sea_water_velocity"),
                "m s-1",
            ),
        ),
        "Copernicus Marine",
    ),
    "water_level": DataTypeSpec(
        "water_level",
        DataCategory.SLOW,
        (VariableSpec("sea_surface_height", ("zos", "ssh", "water_level"), "m"),),
        "Copernicus Marine",
    ),
    "wind_field": DataTypeSpec(
        "wind_field",
        DataCategory.DYNAMIC,
        (
            VariableSpec(
                "wind_u10", ("u10", "10u", "UGRD", "eastward_wind", "u"), "m s-1"
            ),
            VariableSpec(
                "wind_v10", ("v10", "10v", "VGRD", "northward_wind", "v"), "m s-1"
            ),
        ),
        "NOAA GFS/NOMADS",
    ),
    "temperature": DataTypeSpec(
        "temperature",
        DataCategory.DYNAMIC,
        (VariableSpec("air_temperature_2m", ("t2m", "2t", "TMP", "temperature"), "K"),),
        "NOAA GFS/NOMADS",
    ),
    "visibility": DataTypeSpec(
        "visibility",
        DataCategory.DYNAMIC,
        (VariableSpec("visibility", ("vis", "VIS", "visibility"), "m"),),
        "NOAA GFS/NOMADS",
    ),
    "bathymetry": DataTypeSpec(
        "bathymetry",
        DataCategory.STATIC,
        (VariableSpec("elevation", ("elevation", "z", "depth", "bathymetry"), "m"),),
        "GEBCO",
    ),
    "land_sea_mask": DataTypeSpec(
        "land_sea_mask",
        DataCategory.STATIC,
        (VariableSpec("land_sea_mask", ("land_sea_mask", "sea_mask"), "1"),),
        "GEBCO",
    ),
    "long_term_restricted_area": DataTypeSpec(
        "long_term_restricted_area",
        DataCategory.EVENT,
        (VariableSpec("restricted_area", ("restricted_area",), None),),
        "EMODnet Human Activities",
    ),
}


def get_data_type_spec(data_type: str) -> DataTypeSpec:
    try:
        return DATA_TYPE_SPECS[data_type]
    except KeyError as exc:
        supported = ", ".join(sorted(DATA_TYPE_SPECS))
        raise ValueError(f"未知 data_type={data_type!r}；支持: {supported}") from exc
