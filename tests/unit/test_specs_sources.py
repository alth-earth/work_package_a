"""Provenance consistency for source-family metadata in ``specs``.

CARRA (C3S east-domain reanalysis) is a winter fill-in source for the three
dynamic weather data types. Its normalized key ``"c3s_carra"`` MUST stay aligned
between ``specs.DataTypeSpec.source_families`` and ``carra_acquisition.py``.
"""

from arctic_route_data.specs import DATA_TYPE_SPECS, get_data_type_spec

WINTER_CARRA_TYPES = ("wind_field", "temperature", "visibility")
NORMALIZED_SOURCE_TYPES = (*WINTER_CARRA_TYPES, "vessel_traffic")


def test_winter_carra_types_register_dual_source():
    for dt in WINTER_CARRA_TYPES:
        spec = get_data_type_spec(dt)
        # Primary source remains the human-readable NOAA GFS/NOMADS label so that
        # downstream ``source_family`` comparisons (issue_time.py, legacy_publish)
        # are untouched.
        assert spec.source_family == "NOAA GFS/NOMADS"
        # Normalized-key tuple now records both allowed families.
        assert "noaa_gfs" in spec.source_families
        assert "c3s_carra" in spec.source_families


def test_non_carra_types_default_to_single_family():
    for name, spec in DATA_TYPE_SPECS.items():
        if name in NORMALIZED_SOURCE_TYPES:
            continue
        # Unspecified source_families falls back to (source_family,) so existing
        # single-source specs need no change and remain internally consistent.
        assert spec.source_families == (spec.source_family,)
