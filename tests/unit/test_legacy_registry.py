from arctic_route_data.legacy_downloaders import LEGACY_DOWNLOADERS


def test_all_thirteen_supplied_downloaders_are_registered():
    assert len(LEGACY_DOWNLOADERS) == 13
    assert set(LEGACY_DOWNLOADERS) == {
        "bathymetry",
        "long_term_restricted_area",
        "ocean_current",
        "sea_ice_concentration",
        "sea_ice_drift",
        "sea_ice_edge",
        "sea_ice_thickness",
        "sea_ice_type",
        "temperature",
        "visibility",
        "water_level",
        "wave",
        "wind_field",
    }
