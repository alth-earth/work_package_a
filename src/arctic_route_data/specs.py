"""Canonical variable registry for all data types in the supplied acquisition package.

Source-family naming convention
-------------------------------
``source_family`` is a **human-readable** label (single canonical source), while
``source_families`` is the **normalized-key** tuple used by the provenance and
legacy-publishing layers (see ``issue_time.py`` and ``legacy_downloaders.py``).
The two namespaces are intentionally separated so that ``specs.py`` stays the
human-readable SSOT and the normalized keys stay stable for code-level checks.

Normalized keys (must match ``LegacyDownloaderSpec.source_family`` / issue-time logic):
    "noaa_gfs"           -> NOAA GFS / NOMADS (dynamic weather)
    "copernicus_marine"  -> Copernicus Marine (wave, ocean, ice, etc.)
    "osi_saf_thredds"    -> OSI SAF via MET Norway THREDDS
    "c3s_carra"          -> C3S CARRA (east domain) reanalysis, winter fill-in
    "gebco"              -> GEBCO bathymetry / derived land-sea mask
    "emodnet"            -> EMODnet Human Activities (legal restrictions)
"""

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
    # Human-readable canonical source label (single source).
    source_family: str
    allow_single_variable_fallback: bool = False
    # Normalized-key tuple of ALL allowed source families for this data type.
    # Defaults to ``(source_family,)`` when not explicitly provided so existing
    # single-source specs need no change. Carries the same normalized keys used
    # by ``issue_time.py`` and ``legacy_downloaders.py``. Placed AFTER
    # ``allow_single_variable_fallback`` so that existing positional callers
    # (e.g. ``DataTypeSpec(..., source_family, allow_single_variable_fallback)``)
    # keep their argument positions intact.
    source_families: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_families:
            # frozen + slots: use object.__setattr__ to set the field post-init.
            object.__setattr__(self, "source_families", (self.source_family,))


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
    # wind_field / temperature / visibility are dual-sourced:
    #   primary : "noaa_gfs"  (NOAA GFS / NOMADS)        -- mainline dynamic weather
    #   winter  : "c3s_carra" (C3S CARRA east-domain)    -- reanalysis fill-in
    # Winter fill-in approved via proposal A-WINTER-MET-001 (2026-08-22). CARRA covers
    # 2026-02-15..02-21 for the tromso->isfjorden window only and is NOT yet published
    # into the A pipeline (requires CDS token + eccodes); see ``carra_acquisition.py``.
    # Identical variables/units are guaranteed by that module.
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
        source_families=("noaa_gfs", "c3s_carra"),
    ),
    "temperature": DataTypeSpec(
        "temperature",
        DataCategory.DYNAMIC,
        (VariableSpec("air_temperature_2m", ("t2m", "2t", "TMP", "temperature"), "K"),),
        "NOAA GFS/NOMADS",
        source_families=("noaa_gfs", "c3s_carra"),
    ),
    "visibility": DataTypeSpec(
        "visibility",
        DataCategory.DYNAMIC,
        (VariableSpec("visibility", ("vis", "VIS", "visibility"), "m"),),
        "NOAA GFS/NOMADS",
        source_families=("noaa_gfs", "c3s_carra"),
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
