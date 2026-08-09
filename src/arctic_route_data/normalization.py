"""NetCDF coordinate, variable and unit normalization."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.specs import DataTypeSpec, VariableSpec, get_data_type_spec
from arctic_route_data.timeutils import ensure_utc, isoformat_utc

_LONGITUDE_NAMES = ("longitude", "lon", "nav_lon")
_LATITUDE_NAMES = ("latitude", "lat", "nav_lat")
_TIME_NAMES = ("time", "valid_time", "forecast_time")


def _find_name(dataset: xr.Dataset, candidates: tuple[str, ...]) -> str | None:
    names = {name.casefold(): name for name in (*dataset.coords, *dataset.variables)}
    for candidate in candidates:
        if candidate.casefold() in names:
            return names[candidate.casefold()]
    return None


def _find_variable(dataset: xr.Dataset, spec: VariableSpec) -> str | None:
    names = {name.casefold(): name for name in dataset.data_vars}
    for candidate in (spec.canonical_name, *spec.aliases):
        if candidate.casefold() in names:
            return names[candidate.casefold()]
    return None


def _select_valid_time(dataset: xr.Dataset, valid_time: datetime) -> xr.Dataset:
    if "time" not in dataset.coords:
        return dataset.expand_dims(time=[np.datetime64(valid_time.replace(tzinfo=None), "ns")])
    times = np.asarray(dataset["time"].values).reshape(-1)
    if times.size == 0:
        raise DataValidationError("time 坐标为空")
    wanted = np.datetime64(valid_time.replace(tzinfo=None), "ns")
    normalized = times.astype("datetime64[ns]")
    matches = np.flatnonzero(normalized == wanted)
    if times.size > 1 and matches.size == 0:
        raise DataValidationError(
            "文件含多个时次，但 valid_time 在 time 坐标中没有精确匹配；请拆帧或修正元数据。"
        )
    if times.size > 1:
        return dataset.isel(time=[int(matches[0])])
    if normalized[0] != wanted:
        raise DataValidationError("文件 time 坐标与显式 valid_time 不一致")
    return dataset


def _canonicalize_unit(array: xr.DataArray, canonical_name: str, unit: str | None) -> xr.DataArray:
    if unit is None:
        return array
    source_unit = str(array.attrs.get("units", "")).strip().casefold()
    result = array
    if canonical_name == "ice_concentration" and source_unit in {"%", "percent", "percentage"}:
        result = array / 100.0
    elif canonical_name == "air_temperature_2m" and source_unit in {
        "c",
        "degc",
        "degree_celsius",
        "degrees_celsius",
        "°c",
    }:
        result = array + 273.15
    elif canonical_name == "visibility" and source_unit in {"km", "kilometer", "kilometre"}:
        result = array * 1000.0
    elif unit == "m s-1" and source_unit in {"kn", "knot", "knots"}:
        result = array * 0.514444
    result.attrs = dict(array.attrs)
    result.attrs["units"] = unit
    return result


def normalize_dataset(
    dataset: xr.Dataset,
    *,
    data_type: str,
    valid_time: datetime,
    issue_time: datetime,
    route_id: str,
    source: str,
) -> xr.Dataset:
    spec: DataTypeSpec = get_data_type_spec(data_type)
    valid_time = ensure_utc(valid_time, field="valid_time")
    issue_time = ensure_utc(issue_time, field="issue_time")
    working = dataset.copy()

    rename: dict[str, str] = {}
    lon_name = _find_name(working, _LONGITUDE_NAMES)
    lat_name = _find_name(working, _LATITUDE_NAMES)
    time_name = _find_name(working, _TIME_NAMES)
    if lon_name is None or lat_name is None:
        raise DataValidationError("NetCDF 必须包含经纬度坐标")
    if lon_name != "longitude":
        rename[lon_name] = "longitude"
    if lat_name != "latitude":
        rename[lat_name] = "latitude"
    if time_name is not None and time_name != "time":
        rename[time_name] = "time"
    if rename:
        working = working.rename(rename)

    variable_rename: dict[str, str] = {}
    found_names: list[str] = []
    for variable in spec.variables:
        found = _find_variable(working, variable)
        if found is not None:
            found_names.append(found)
            if found != variable.canonical_name:
                variable_rename[found] = variable.canonical_name
    if not found_names and spec.allow_single_variable_fallback and len(working.data_vars) == 1:
        only = next(iter(working.data_vars))
        found_names.append(only)
        variable_rename[only] = spec.variables[0].canonical_name
    if len(found_names) != len(spec.variables):
        expected = [item.canonical_name for item in spec.variables]
        raise DataValidationError(
            f"{data_type} 变量不完整；期望 {expected}，实际 {list(working.data_vars)}"
        )
    if variable_rename:
        working = working.rename(variable_rename)
    canonical_variables = [item.canonical_name for item in spec.variables]
    working = working[canonical_variables]
    working = _select_valid_time(working, valid_time)

    for variable in spec.variables:
        working[variable.canonical_name] = _canonicalize_unit(
            working[variable.canonical_name], variable.canonical_name, variable.canonical_unit
        )

    for coordinate in ("longitude", "latitude"):
        values = np.asarray(working[coordinate].values, dtype=float)
        if not np.isfinite(values).any():
            raise DataValidationError(f"{coordinate} 坐标全部缺测")
        if working[coordinate].ndim == 1 and values.size > 1 and np.any(np.diff(values) < 0):
            working = working.sortby(coordinate)

    for name in canonical_variables:
        if working[name].size == 0:
            raise DataValidationError(f"{name} 数据为空")

    working.attrs = {
        **dict(working.attrs),
        "data_type": data_type,
        "route_id": route_id,
        "issue_time": isoformat_utc(issue_time),
        "valid_time": isoformat_utc(valid_time),
        "source": source,
        "crs": str(working.attrs.get("crs", "EPSG:4326")),
        "normalizer_version": "arctic-route-data/0.1.0",
    }
    return working


def spatial_metadata(dataset: xr.Dataset) -> tuple[
    tuple[float, float, float, float], tuple[float | None, float | None], str
]:
    longitude = np.asarray(dataset["longitude"].values, dtype=float)
    latitude = np.asarray(dataset["latitude"].values, dtype=float)
    bbox = (
        float(np.nanmin(longitude)),
        float(np.nanmin(latitude)),
        float(np.nanmax(longitude)),
        float(np.nanmax(latitude)),
    )

    def spacing(values: np.ndarray) -> float | None:
        unique = np.unique(values[np.isfinite(values)])
        if unique.size < 2:
            return None
        return float(np.median(np.abs(np.diff(np.sort(unique)))))

    resolution = (spacing(longitude), spacing(latitude))
    return bbox, resolution, str(dataset.attrs.get("crs", "EPSG:4326"))


def netcdf_encoding(dataset: xr.Dataset) -> dict[str, dict[str, Any]]:
    return {
        name: {"zlib": True, "complevel": 4, "shuffle": True}
        for name in dataset.data_vars
    }
