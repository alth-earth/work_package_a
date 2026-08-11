"""NetCDF coordinate, variable, unit and directional-semantic normalization."""

from __future__ import annotations

import hashlib
import json
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

_PROJECTED_X_NAMES = {"vxo", "vxsi"}
_PROJECTED_Y_NAMES = {"vyo", "vysi"}
_KNOWN_ARCTIC_POLAR_PRODUCTS = (
    "ARCTIC_ANALYSISFORECAST_PHY_002_001",
    "cmems_mod_arc_phy_anfc_6km",
)
_UNIT_RANGES: dict[str, tuple[float | None, float | None]] = {
    "ice_concentration": (0.0, 1.0),
    "ice_thickness": (0.0, 100.0),
    "significant_wave_height": (0.0, 50.0),
    "mean_wave_direction": (0.0, 360.0),
    "peak_wave_period": (0.0, 60.0),
    "ocean_current_u": (-20.0, 20.0),
    "ocean_current_v": (-20.0, 20.0),
    "ice_drift_u": (-10.0, 10.0),
    "ice_drift_v": (-10.0, 10.0),
    "wind_u10": (-150.0, 150.0),
    "wind_v10": (-150.0, 150.0),
    "sea_surface_height": (-50.0, 50.0),
    "air_temperature_2m": (150.0, 350.0),
    "visibility": (0.0, 1_000_000.0),
    "elevation": (-12_000.0, 10_000.0),
}

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


def _unit_key(value: str) -> str:
    return (
        value.strip()
        .casefold()
        .replace("**", "^")
        .replace("·", " ")
        .replace("_", " ")
        .replace(" ", "")
    )


def _canonicalize_unit(
    array: xr.DataArray,
    canonical_name: str,
    unit: str | None,
    *,
    source_name: str,
) -> xr.DataArray:
    if unit is None:
        return array
    original_unit = str(array.attrs.get("units", "")).strip()
    source_unit = _unit_key(original_unit)
    assumed = False
    if not source_unit:
        if canonical_name in {"ice_type", "ice_edge"}:
            source_unit = _unit_key(unit)
            assumed = True
        else:
            raise DataValidationError(
                f"{canonical_name} 缺少 units；不能仅按源变量 {source_name!r} 猜测单位"
            )

    result = array
    if canonical_name == "ice_concentration" and source_unit in {"%", "percent", "percentage"}:
        result = array / 100.0
    elif canonical_name == "air_temperature_2m" and source_unit in {
        "c",
        "degc",
        "degreecelsius",
        "degreescelsius",
        "°c",
    }:
        result = array + 273.15
    elif canonical_name == "visibility" and source_unit in {
        "km",
        "kilometer",
        "kilometers",
        "kilometre",
        "kilometres",
    }:
        result = array * 1000.0
    elif unit == "m s-1" and source_unit in {"kn", "knot", "knots", "kt", "kts"}:
        result = array * 0.514444
    elif unit == "m s-1" and source_unit in {"cm/s", "cms^-1", "cms-1"}:
        result = array / 100.0
    elif unit == "degree" and source_unit in {"rad", "radian", "radians"}:
        result = np.rad2deg(array)
    elif not _unit_matches(source_unit, unit):
        raise DataValidationError(
            f"{canonical_name} 的单位 {original_unit!r} 不能转换为规范单位 {unit!r}"
        )
    result.attrs = dict(array.attrs)
    if original_unit:
        result.attrs["source_units"] = original_unit
    if assumed:
        result.attrs["unit_assumption"] = f"inferred_from_known_variable:{source_name}"
    result.attrs["units"] = unit
    return result


def _unit_matches(source_unit: str, canonical_unit: str) -> bool:
    accepted = {
        "1": {"1", "dimensionless", "fraction"},
        "m": {"m", "meter", "meters", "metre", "metres"},
        "m s-1": {"m/s", "ms^-1", "ms-1", "meterpersecond", "metrepersecond"},
        "K": {"k", "kelvin", "kelvins"},
        "degree": {"degree", "degrees", "deg", "degreeeast", "degreestrue"},
        "s": {"s", "sec", "second", "seconds"},
    }
    return source_unit in accepted[canonical_unit]


def _polar_projection_central_meridian(dataset: xr.Dataset) -> float | None:
    keys = (
        "straight_vertical_longitude_from_pole",
        "longitude_of_projection_origin",
        "central_meridian",
        "lon_0",
    )
    containers = [dataset.attrs, *(variable.attrs for variable in dataset.variables.values())]
    projection_confirmed = any(
        str(attrs.get("grid_mapping_name", "")).casefold() == "polar_stereographic"
        or str(attrs.get("projection", "")).casefold() == "polar_stereographic"
        for attrs in containers
    )
    for attrs in containers:
        for key in keys:
            if key in attrs:
                if not projection_confirmed:
                    continue
                try:
                    return float(attrs[key])
                except (TypeError, ValueError):
                    raise DataValidationError(f"投影参数 {key} 不是有效数值") from None
    identity = " ".join(
        str(dataset.attrs.get(key, ""))
        for key in ("copernicus_product", "copernicus_dataset_id", "product_id", "dataset_id")
    )
    if any(token.casefold() in identity.casefold() for token in _KNOWN_ARCTIC_POLAR_PRODUCTS):
        return -45.0
    return None


def _component_frame(array: xr.DataArray, source_name: str, *, axis: str) -> str:
    standard_name = str(array.attrs.get("standard_name", "")).casefold()
    projected_names = _PROJECTED_X_NAMES if axis == "x" else _PROJECTED_Y_NAMES
    known_earth_names = (
        {
            "eastward_sea_water_velocity",
            "eastward_wind",
            "eastward_sea_ice_velocity",
        }
        if axis == "x"
        else {
            "northward_sea_water_velocity",
            "northward_wind",
            "northward_sea_ice_velocity",
        }
    )
    earth_token = "eastward" if axis == "x" else "northward"
    if earth_token in standard_name:
        return "earth"
    if f"_{axis}_velocity" in standard_name:
        return "projected"
    if source_name.casefold() in projected_names:
        return "ambiguous"
    if source_name.casefold() in known_earth_names:
        return "earth"
    return "unknown"


def _normalize_vector_semantics(
    dataset: xr.Dataset,
    *,
    data_type: str,
    source_names: dict[str, str],
    source_dataset: xr.Dataset,
) -> xr.Dataset:
    pair_by_type = {
        "ocean_current": ("ocean_current_u", "ocean_current_v"),
        "sea_ice_drift": ("ice_drift_u", "ice_drift_v"),
        "wind_field": ("wind_u10", "wind_v10"),
    }
    pair = pair_by_type.get(data_type)
    if pair is None:
        return dataset
    u_name, v_name = pair
    source_u = source_names[u_name]
    source_v = source_names[v_name]
    u = dataset[u_name]
    v = dataset[v_name]
    frame_u = _component_frame(u, source_u, axis="x")
    frame_v = _component_frame(v, source_v, axis="y")
    if {frame_u, frame_v} & {"ambiguous", "unknown"}:
        raise DataValidationError(
            f"{data_type} 的 {source_u}/{source_v} 缺少明确 standard_name；"
            "无法证明它们是投影 X/Y 还是东/北分量"
        )
    if frame_u != frame_v:
        raise DataValidationError(f"{data_type} 的两个矢量分量使用了不一致的参考坐标系")

    result = dataset
    rotation = "none"
    source_frame = "true_east_north"
    if frame_u == "projected":
        central_meridian = _polar_projection_central_meridian(source_dataset)
        if central_meridian is None:
            raise DataValidationError(
                f"{data_type} 是投影 X/Y 速度分量，但缺少中央经线；拒绝把它误标成东/北分量"
            )
        longitude_delta = np.deg2rad(result["longitude"] - central_meridian)
        source_u_values = u.copy(deep=False)
        source_v_values = v.copy(deep=False)
        east = source_u_values * np.cos(longitude_delta) + source_v_values * np.sin(
            longitude_delta
        )
        north = -source_u_values * np.sin(longitude_delta) + source_v_values * np.cos(
            longitude_delta
        )
        result[u_name] = east
        result[v_name] = north
        result.attrs["vector_projection_central_meridian_deg"] = central_meridian
        rotation = "polar_stereographic_xy_to_true_east_north"
        source_frame = "polar_stereographic_xy"

    standard_names = {
        "ocean_current": ("eastward_sea_water_velocity", "northward_sea_water_velocity"),
        "sea_ice_drift": ("eastward_sea_ice_velocity", "northward_sea_ice_velocity"),
        "wind_field": ("eastward_wind", "northward_wind"),
    }[data_type]
    for name, standard_name, source_array in (
        (u_name, standard_names[0], u),
        (v_name, standard_names[1], v),
    ):
        attrs = dict(source_array.attrs)
        attrs["source_variable"] = source_names[name]
        if "standard_name" in source_array.attrs:
            attrs["source_standard_name"] = str(source_array.attrs["standard_name"])
        attrs["standard_name"] = standard_name
        attrs["vector_reference_frame"] = "true_east_north"
        grid_mapping = str(attrs.get("grid_mapping", ""))
        if grid_mapping and grid_mapping not in result.variables:
            attrs["source_grid_mapping"] = grid_mapping
            attrs.pop("grid_mapping", None)
        result[name].attrs = attrs
    result.attrs["vector_source_reference_frame"] = source_frame
    result.attrs["vector_reference_frame"] = "true_east_north"
    result.attrs["vector_rotation"] = rotation
    return result


def _normalize_wave_direction(
    dataset: xr.Dataset, *, source_name: str
) -> xr.Dataset:
    direction = dataset["mean_wave_direction"]
    standard_name = str(direction.attrs.get("standard_name", "")).casefold()
    declared = str(direction.attrs.get("direction_convention", "")).casefold()
    standard_convention = ""
    if "from_direction" in standard_name:
        standard_convention = "from"
    elif "to_direction" in standard_name:
        standard_convention = "to"
    if declared and standard_convention and declared not in {
        standard_convention,
        "coming_from" if standard_convention == "from" else "going_to",
    }:
        raise DataValidationError("mean_wave_direction 的 standard_name 与方向声明冲突")
    if standard_convention:
        declared = standard_convention
    elif not declared and source_name.casefold() in {
        "vmdr",
        "mwd",
        "sea_surface_wave_from_direction",
    }:
        declared = "from"
    if declared in {"to", "towards", "going_to"}:
        direction = (direction + 180.0) % 360.0
    elif declared not in {"from", "coming_from"}:
        raise DataValidationError(
            "mean_wave_direction 缺少 from/to 方向约定；拒绝猜测可能相差 180° 的波向"
        )
    else:
        direction = direction % 360.0
    attrs = dict(dataset["mean_wave_direction"].attrs)
    attrs.update(
        {
            "standard_name": "sea_surface_wave_from_direction",
            "direction_convention": "from",
            "reference_direction": "true_north",
            "positive_direction": "clockwise",
        }
    )
    direction.attrs = attrs
    result = dataset.copy()
    result["mean_wave_direction"] = direction
    return result


def _normalize_bathymetry(
    dataset: xr.Dataset,
    *,
    source_name: str,
) -> xr.Dataset:
    elevation = dataset["elevation"]
    standard_name = str(elevation.attrs.get("standard_name", "")).casefold()
    positive = str(elevation.attrs.get("positive", "")).casefold()
    is_depth = "depth_below" in standard_name or positive == "down"
    is_elevation = (
        "height_above" in standard_name
        or "surface_altitude" in standard_name
        or positive == "up"
        or source_name.casefold() in {"elevation", "z", "bathymetry"}
    )
    if source_name.casefold() == "depth" and not (is_depth or is_elevation):
        raise DataValidationError(
            "bathymetry 源变量名为 depth，但未声明 positive=down/up 或 CF standard_name"
        )
    result = dataset.copy()
    if is_depth:
        converted = -elevation
        attrs = dict(elevation.attrs)
        attrs["source_vertical_positive"] = positive or "down_by_standard_name"
        converted.attrs = attrs
        result["elevation"] = converted
    attrs = dict(result["elevation"].attrs)
    attrs.update(
        {
            "standard_name": "height_above_mean_sea_level",
            "positive": "up",
            "source_variable": source_name,
        }
    )
    result["elevation"].attrs = attrs
    return result


def _validate_values(dataset: xr.Dataset, canonical_variables: list[str]) -> dict[str, float]:
    missing_fraction: dict[str, float] = {}
    for name in canonical_variables:
        values = np.asarray(dataset[name].values)
        if not np.issubdtype(values.dtype, np.number):
            raise DataValidationError(f"{name} 必须是数值变量")
        finite = np.isfinite(values)
        if not finite.any():
            raise DataValidationError(f"{name} 全部缺测")
        if np.isinf(values).any():
            raise DataValidationError(f"{name} 包含正负无穷值")
        missing_fraction[name] = float(1.0 - finite.mean())
        bounds = _UNIT_RANGES.get(name)
        if bounds is None:
            continue
        lower, upper = bounds
        finite_values = values[finite]
        if lower is not None and np.any(finite_values < lower):
            raise DataValidationError(f"{name} 小于物理合同下限 {lower}")
        if upper is not None and np.any(finite_values > upper):
            raise DataValidationError(f"{name} 大于物理合同上限 {upper}")
    return missing_fraction


def _grid_identity(dataset: xr.Dataset) -> tuple[str, str]:
    digest = hashlib.sha256()
    for name in ("longitude", "latitude"):
        values = np.asarray(dataset[name].values, dtype="<f8")
        digest.update(name.encode())
        digest.update(str(dataset[name].dims).encode())
        digest.update(str(values.shape).encode())
        digest.update(values.tobytes(order="C"))
    coordinate_digest = digest.hexdigest()
    return f"a-grid-{coordinate_digest[:16]}", coordinate_digest


def _source_grid_mapping(dataset: xr.Dataset) -> dict[str, Any]:
    for variable in dataset.data_vars.values():
        mapping_name = str(variable.attrs.get("grid_mapping", ""))
        if mapping_name and mapping_name in dataset.variables:
            mapping = dataset[mapping_name]
            return {
                "variable": mapping_name,
                "attributes": {
                    str(key): value.item() if isinstance(value, np.generic) else value
                    for key, value in mapping.attrs.items()
                    if isinstance(value, str | int | float | bool | np.generic)
                },
            }
    return {}


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
    source_crs = str(working.attrs.get("crs", "")).strip()

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
    for coordinate in ("longitude", "latitude"):
        if coordinate in working.data_vars:
            working = working.set_coords(coordinate)

    variable_rename: dict[str, str] = {}
    found_names: list[str] = []
    source_names: dict[str, str] = {}
    for variable in spec.variables:
        found = _find_variable(working, variable)
        if found is not None:
            found_names.append(found)
            source_names[variable.canonical_name] = found
            if found != variable.canonical_name:
                variable_rename[found] = variable.canonical_name
    if not found_names and spec.allow_single_variable_fallback and len(working.data_vars) == 1:
        only = next(iter(working.data_vars))
        found_names.append(only)
        source_names[spec.variables[0].canonical_name] = only
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
            working[variable.canonical_name],
            variable.canonical_name,
            variable.canonical_unit,
            source_name=source_names[variable.canonical_name],
        )

    working = _normalize_vector_semantics(
        working,
        data_type=data_type,
        source_names=source_names,
        source_dataset=dataset,
    )
    if data_type == "wave":
        working = _normalize_wave_direction(
            working,
            source_name=source_names["mean_wave_direction"],
        )
    if data_type == "bathymetry":
        working = _normalize_bathymetry(
            working,
            source_name=source_names["elevation"],
        )

    rectilinear = (
        working["longitude"].ndim == working["latitude"].ndim == 1
        and working["longitude"].dims != working["latitude"].dims
    )
    paired_points = (
        working["longitude"].ndim == working["latitude"].ndim == 1
        and working["longitude"].dims == working["latitude"].dims
    )
    for coordinate in ("longitude", "latitude"):
        values = np.asarray(working[coordinate].values, dtype=float)
        if not np.isfinite(values).all():
            raise DataValidationError(f"{coordinate} 坐标包含缺测或无穷值")
        if coordinate == "latitude" and np.any((values < -90.0) | (values > 90.0)):
            raise DataValidationError("latitude 必须位于 [-90, 90]")
        if coordinate == "longitude" and np.any((values < -180.0) | (values > 360.0)):
            raise DataValidationError("longitude 必须位于 [-180, 360]")
        if coordinate == "longitude" and np.any(values > 180.0):
            working = working.assign_coords(
                longitude=((working[coordinate] + 180.0) % 360.0) - 180.0
            )
            values = np.asarray(working[coordinate].values, dtype=float)
        if rectilinear and values.size > 1 and np.any(np.diff(values) < 0):
            working = working.sortby(coordinate)

    spatial_dims = set(working["longitude"].dims) | set(working["latitude"].dims)
    for name in canonical_variables:
        if not spatial_dims.issubset(working[name].dims):
            raise DataValidationError(
                f"{name} 没有覆盖经纬度网格维度 {sorted(spatial_dims)}"
            )
    missing_fraction = _validate_values(working, canonical_variables)
    grid_id, coordinate_digest = _grid_identity(working)
    source_mapping = _source_grid_mapping(dataset)

    source_grid_crs = source_crs or str(
        dataset.attrs.get("grid_mapping", dataset.attrs.get("projection", "unknown"))
    )
    grid_topology = (
        "rectilinear"
        if rectilinear
        else "unstructured_points"
        if paired_points
        else "curvilinear"
    )

    working.attrs = {
        **dict(working.attrs),
        "data_type": data_type,
        "route_id": route_id,
        "issue_time": isoformat_utc(issue_time),
        "valid_time": isoformat_utc(valid_time),
        "source": source,
        "crs": "EPSG:4326",
        "coordinate_crs": "EPSG:4326",
        "source_grid_crs": source_grid_crs,
        "grid_topology": grid_topology,
        "grid_id": grid_id,
        "coordinate_digest": coordinate_digest,
        "longitude_wrap": "-180_180",
        "source_grid_mapping": json.dumps(source_mapping, sort_keys=True),
        "qc_missing_fraction": json.dumps(missing_fraction, sort_keys=True),
        "normalizer_version": "arctic-route-data/0.3.0",
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
        if values.ndim != 1:
            return None
        unique = np.unique(values[np.isfinite(values)])
        if unique.size < 2:
            return None
        return float(np.median(np.abs(np.diff(np.sort(unique)))))

    resolution = (
        (spacing(longitude), spacing(latitude))
        if dataset.attrs.get("grid_topology") == "rectilinear"
        else (None, None)
    )
    return bbox, resolution, str(dataset.attrs.get("crs", "EPSG:4326"))


def netcdf_encoding(dataset: xr.Dataset) -> dict[str, dict[str, Any]]:
    return {
        name: {"zlib": True, "complevel": 4, "shuffle": True}
        for name in dataset.data_vars
    }
