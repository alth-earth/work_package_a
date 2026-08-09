from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.normalization import normalize_dataset, spatial_metadata

T0 = datetime(2026, 7, 15, 6, tzinfo=UTC)


def test_normalizes_names_order_and_percent_units():
    dataset = xr.Dataset(
        {"ice_conc": (("lat", "lon"), np.array([[25.0, 50.0], [75.0, 100.0]]))},
        coords={"lon": [20.0, 19.0], "lat": [71.0, 70.0]},
    )
    dataset["ice_conc"].attrs["units"] = "%"
    result = normalize_dataset(
        dataset,
        data_type="sea_ice_concentration",
        valid_time=T0,
        issue_time=T0,
        route_id="route-a",
        source="test",
    )
    assert list(result.data_vars) == ["ice_concentration"]
    assert result.longitude.values.tolist() == [19.0, 20.0]
    assert result.latitude.values.tolist() == [70.0, 71.0]
    assert float(result.ice_concentration.max()) == 1.0
    assert result.ice_concentration.attrs["units"] == "1"
    assert spatial_metadata(result)[:2] == ((19.0, 70.0, 20.0, 71.0), (1.0, 1.0))


def test_rejects_ambiguous_multi_time_file():
    dataset = xr.Dataset(
        {"vis": (("time", "latitude", "longitude"), np.ones((2, 1, 1)))},
        coords={
            "time": np.array(["2026-07-15T00", "2026-07-15T06"], dtype="datetime64[h]"),
            "longitude": [19.0],
            "latitude": [70.0],
        },
    )
    with pytest.raises(DataValidationError, match="精确匹配"):
        normalize_dataset(
            dataset,
            data_type="visibility",
            valid_time=datetime(2026, 7, 15, 3, tzinfo=UTC),
            issue_time=T0,
            route_id="route-a",
            source="test",
        )
