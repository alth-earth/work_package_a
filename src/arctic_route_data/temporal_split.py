"""Split a source NetCDF with one or many forecast times into one frame per valid time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.timeutils import isoformat_utc


@dataclass(frozen=True, slots=True)
class TemporalSlice:
    dataset: xr.Dataset
    valid_time: datetime
    source_index: dict[str, int]
    forecast_reference_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class _TimeAxis:
    name: str
    values: np.ndarray
    dims: tuple[str, ...]


def to_utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            raise DataValidationError("时间坐标包含 NaT")
        microseconds = value.astype("datetime64[us]").astype(np.int64)
        return datetime.fromtimestamp(int(microseconds) / 1_000_000, tz=UTC)
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    raise DataValidationError(f"无法解析时间坐标值: {value!r}")


def discover_valid_times(dataset: xr.Dataset) -> tuple[datetime, ...]:
    axis = _valid_time_coordinate(dataset)
    result = tuple(to_utc_datetime(item) for item in axis.values.reshape(-1))
    if not result:
        raise DataValidationError("NetCDF 没有有效时次")
    return result


def split_dataset_by_valid_time(
    dataset: xr.Dataset, *, max_frames: int = 1000
) -> tuple[TemporalSlice, ...]:
    axis = _valid_time_coordinate(dataset)
    coordinate_name = axis.name
    values = axis.values
    if values.size == 0:
        raise DataValidationError("NetCDF 时间坐标为空")
    if values.size > max_frames:
        raise DataValidationError(
            f"NetCDF 包含 {values.size} 个时次，超过单次拆分上限 {max_frames}"
        )

    dims = axis.dims
    if values.ndim != len(dims):
        raise DataValidationError("时间坐标维度无法映射到数据维度")
    slices: list[TemporalSlice] = []
    seen: set[datetime] = set()
    indices = np.ndindex(values.shape) if values.shape else [()]
    for position in indices:
        valid_time = to_utc_datetime(values[position] if position else values.item())
        if valid_time in seen:
            raise DataValidationError(
                f"同一文件包含重复 valid_time={isoformat_utc(valid_time)}；"
                "请先按预报循环拆分，避免版本歧义"
            )
        seen.add(valid_time)
        indexer = dict(zip(dims, position, strict=True))
        selected = dataset.isel(indexer, drop=True) if indexer else dataset.copy()
        reference_time = _forecast_reference_time(dataset, selected, coordinate_name, indexer)
        selected = _replace_time_coordinates(selected, valid_time)
        selected.attrs = dict(selected.attrs)
        selected.attrs["valid_time"] = isoformat_utc(valid_time)
        if reference_time is not None:
            selected.attrs["forecast_reference_time"] = isoformat_utc(reference_time)
            selected.attrs["forecast_lead_hours"] = (
                valid_time - reference_time
            ).total_seconds() / 3600.0
        selected.attrs["source_time_index"] = ",".join(
            f"{name}={index}" for name, index in indexer.items()
        )
        slices.append(TemporalSlice(selected, valid_time, indexer, reference_time))
    return tuple(slices)


def _valid_time_coordinate(dataset: xr.Dataset) -> _TimeAxis:
    for name in ("valid_time", "forecast_time"):
        if name in dataset.variables:
            return _TimeAxis(name, np.asarray(dataset[name].values), dataset[name].dims)
    if "time" in dataset.variables and "step" in dataset.variables:
        base = np.asarray(dataset["time"].values)
        step = np.asarray(dataset["step"].values)
        if np.issubdtype(base.dtype, np.datetime64) and np.issubdtype(step.dtype, np.timedelta64):
            if base.ndim == 0:
                return _TimeAxis("step", base + step, dataset["step"].dims)
            if step.ndim == 0:
                return _TimeAxis("time", base + step, dataset["time"].dims)
            if base.ndim == 1 and step.ndim == 1:
                values = base[:, None] + step[None, :]
                return _TimeAxis(
                    "__derived_valid_time",
                    values,
                    (dataset["time"].dims[0], dataset["step"].dims[0]),
                )
    if "time" in dataset.variables:
        return _TimeAxis("time", np.asarray(dataset["time"].values), dataset["time"].dims)
    raise DataValidationError("NetCDF 缺少 time/valid_time/forecast_time 坐标")


def _forecast_reference_time(
    original: xr.Dataset,
    selected: xr.Dataset,
    valid_coordinate: str,
    indexer: dict[str, int],
) -> datetime | None:
    for name in ("forecast_reference_time", "reference_time"):
        if name in original.variables:
            value = original[name]
            relevant = {dim: index for dim, index in indexer.items() if dim in value.dims}
            if relevant:
                value = value.isel(relevant)
            values = np.asarray(value.values).reshape(-1)
            if values.size:
                return to_utc_datetime(values[0])
    if valid_coordinate != "time" and "time" in original.variables:
        value = original["time"]
        relevant = {dim: index for dim, index in indexer.items() if dim in value.dims}
        if relevant:
            value = value.isel(relevant)
        values = np.asarray(value.values).reshape(-1)
        if values.size == 1:
            return to_utc_datetime(values[0])
    reference = selected.attrs.get("forecast_reference_time")
    if reference:
        return to_utc_datetime(reference)
    return None


def _replace_time_coordinates(dataset: xr.Dataset, valid_time: datetime) -> xr.Dataset:
    result = dataset
    for name in (
        "valid_time",
        "forecast_time",
        "forecast_reference_time",
        "reference_time",
        "step",
        "time",
        "__derived_valid_time",
    ):
        if name in result.variables:
            result = result.drop_vars(name)
    timestamp = np.datetime64(valid_time.replace(tzinfo=None), "ns")
    return result.expand_dims(time=[timestamp])
