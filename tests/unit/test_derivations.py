import numpy as np
import xarray as xr

from arctic_route_data.derivations import (
    derive_land_sea_mask,
    derive_sea_ice_edge,
    derive_sea_ice_type,
)


def _grid(values, name):
    return xr.Dataset(
        {name: (("latitude", "longitude"), np.asarray(values, dtype=float))},
        coords={"latitude": [70.0, 71.0, 72.0], "longitude": [10.0, 11.0, 12.0]},
    )


def test_ice_edge_is_deterministic_15_percent_ice_side_boundary():
    result = derive_sea_ice_edge(
        _grid([[0.0, 0.0, 0.0], [0.0, 0.15, 0.0], [0.0, 0.0, 0.0]], "siconc")
    )

    assert result.ice_edge.values.tolist() == [
        [0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0],
    ]
    assert result.ice_edge.attrs["ice_concentration_threshold"] == 0.15
    assert result.ice_edge.attrs["model_role"] == "none_deterministic_preprocessing"


def test_nextsim_ice_type_codebook_uses_dominant_fraction_and_open_water_threshold():
    dataset = _grid([[0.1, 0.8, 0.8], [0.8, 0.8, 0.8], [0.8, 0.8, 0.8]], "siconc")
    dataset["siconc_young"] = xr.full_like(dataset.siconc, 0.1)
    dataset["siconc_my"] = xr.full_like(dataset.siconc, 0.2)
    dataset["siconc_young"][0, 1] = 0.7
    dataset["siconc_my"][0, 2] = 0.7

    result = derive_sea_ice_type(dataset)

    assert result.ice_type[0].values.tolist() == [0.0, 1.0, 3.0]
    assert result.ice_type[1, 1].item() == 2.0
    assert "residual_first_year_ice" in result.ice_type.attrs["flag_meanings"]


def test_nextsim_ice_type_keeps_incomplete_component_cells_missing():
    dataset = xr.Dataset(
        {
            "siconc": (("latitude", "longitude"), [[0.8, np.nan]]),
            "siconc_young": (("latitude", "longitude"), [[np.nan, np.nan]]),
            "siconc_my": (("latitude", "longitude"), [[0.2, np.nan]]),
        },
        coords={"latitude": [75.0], "longitude": [80.0, 81.0]},
    )

    result = derive_sea_ice_type(dataset)

    assert np.isnan(result.ice_type.values).all()


def test_land_sea_mask_comes_from_gebco_elevation_not_source_validity():
    dataset = _grid([[-10, 0, 12], [-1, 2, 3], [-4, -5, 6]], "elevation")

    result = derive_land_sea_mask(dataset)

    assert result.land_sea_mask.values.tolist()[0] == [1.0, 0.0, 0.0]
    assert result.land_sea_mask.attrs["navigation_semantics"] == "none"
    assert "source_valid_mask" not in result
