from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.temporal_split import split_dataset_by_valid_time


def test_splits_time_dimension_into_single_valid_time_frames():
    times = np.array(
        ["2026-07-15T00", "2026-07-15T06", "2026-07-15T12"], dtype="datetime64[h]"
    )
    dataset = xr.Dataset(
        {"ice_conc": (("time", "latitude", "longitude"), np.ones((3, 1, 2)))},
        coords={"time": times, "latitude": [70.0], "longitude": [18.0, 19.0]},
    )
    frames = split_dataset_by_valid_time(dataset)
    assert [frame.valid_time.hour for frame in frames] == [0, 6, 12]
    assert all(frame.dataset.sizes["time"] == 1 for frame in frames)
    assert [float(frame.dataset.ice_conc.mean()) for frame in frames] == [1.0, 1.0, 1.0]


def test_cfgrib_reference_time_plus_steps_preserves_lead_time():
    reference = np.datetime64("2026-07-15T00:00:00")
    steps = np.array([0, 6, 12], dtype="timedelta64[h]")
    valid = reference + steps
    dataset = xr.Dataset(
        {"u10": (("step", "latitude", "longitude"), np.ones((3, 1, 1)))},
        coords={
            "time": reference,
            "step": steps,
            "valid_time": ("step", valid),
            "latitude": [70.0],
            "longitude": [19.0],
        },
    )
    frames = split_dataset_by_valid_time(dataset)
    assert [frame.forecast_reference_time for frame in frames] == [
        datetime(2026, 7, 15, tzinfo=UTC)
    ] * 3
    assert [frame.dataset.attrs["forecast_lead_hours"] for frame in frames] == [0.0, 6.0, 12.0]
    assert all("step" not in frame.dataset.coords for frame in frames)


def test_duplicate_valid_time_is_rejected():
    dataset = xr.Dataset(
        {"vis": (("time", "latitude", "longitude"), np.ones((2, 1, 1)))},
        coords={
            "time": np.array(["2026-07-15", "2026-07-15"], dtype="datetime64[D]"),
            "latitude": [70.0],
            "longitude": [19.0],
        },
    )
    with pytest.raises(DataValidationError, match="重复 valid_time"):
        split_dataset_by_valid_time(dataset)


def test_time_and_step_on_same_dimension_are_paired_not_outer_product():
    times = np.array(["2026-07-15T00", "2026-07-15T06"], dtype="datetime64[h]")
    steps = np.array([0, 6], dtype="timedelta64[h]")
    dataset = xr.Dataset(
        {"vis": (("forecast", "latitude", "longitude"), np.ones((2, 1, 1)))},
        coords={
            "time": ("forecast", times),
            "step": ("forecast", steps),
            "latitude": [70.0],
            "longitude": [19.0],
        },
    )

    frames = split_dataset_by_valid_time(dataset)

    assert [frame.valid_time for frame in frames] == [
        datetime(2026, 7, 15, tzinfo=UTC),
        datetime(2026, 7, 15, 12, tzinfo=UTC),
    ]


def test_non_datetime_valid_time_variable_is_rejected():
    dataset = xr.Dataset(
        {"vis": (("valid_time", "latitude", "longitude"), np.ones((2, 1, 1)))},
        coords={"valid_time": [0, 1], "latitude": [70.0], "longitude": [19.0]},
    )

    with pytest.raises(DataValidationError, match="不是日期时间"):
        split_dataset_by_valid_time(dataset)


def test_scalar_nanosecond_valid_time_remains_datetime_not_integer():
    dataset = xr.Dataset(
        {"vis": (("latitude", "longitude"), np.ones((1, 1)))},
        coords={
            "valid_time": np.datetime64("2026-07-15T06:00:00", "ns"),
            "latitude": [70.0],
            "longitude": [19.0],
        },
    )

    frames = split_dataset_by_valid_time(dataset)

    assert frames[0].valid_time == datetime(2026, 7, 15, 6, tzinfo=UTC)
