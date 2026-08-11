"""Typed configuration loaded from the checked-in TOML file."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from arctic_route_data.errors import MetadataValidationError
from arctic_route_data.models import validate_identifier


@dataclass(frozen=True, slots=True)
class CacheSettings:
    max_memory_mb: float
    slow_frames_per_partition: int
    dynamic_frames_per_partition: int
    history_hours: int
    target_horizon_hours: int
    minimum_complete_horizon_hours: int


@dataclass(frozen=True, slots=True)
class AcquisitionSettings:
    gfs_step_hours: int
    cycle_lookback_count: int
    request_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ClockSettings:
    default_speed: float


@dataclass(frozen=True, slots=True)
class CorridorSettings:
    corridor_id: str
    bbox: tuple[float, float, float, float]
    start: tuple[float, float]
    destination: tuple[float, float]

    @property
    def scenario_id(self) -> str:
        """Compatibility alias for A 0.2 callers; A identifies acquisition corridors."""

        return self.corridor_id


@dataclass(frozen=True, slots=True)
class WorkPackageAConfig:
    cache: CacheSettings
    acquisition: AcquisitionSettings
    clock: ClockSettings
    corridors: Mapping[str, CorridorSettings]

    @property
    def scenarios(self) -> Mapping[str, CorridorSettings]:
        """Compatibility alias; shared system scenarios are defined above A."""

        return self.corridors


def load_config(path: str | Path = "configs/work_package_a.toml") -> WorkPackageAConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MetadataValidationError(f"无法读取 A 配置 {config_path}: {exc}") from exc
    cache_raw = _table(raw, "cache")
    cache = CacheSettings(
        max_memory_mb=_positive_float(cache_raw, "max_memory_mb"),
        slow_frames_per_partition=_positive_int(cache_raw, "slow_frames_per_partition", 2),
        dynamic_frames_per_partition=_positive_int(
            cache_raw, "dynamic_frames_per_partition", 2
        ),
        history_hours=_nonnegative_int(cache_raw, "history_hours"),
        target_horizon_hours=_positive_int(cache_raw, "target_horizon_hours", 1),
        minimum_complete_horizon_hours=_positive_int(
            cache_raw, "minimum_complete_horizon_hours", 1
        ),
    )
    if cache.minimum_complete_horizon_hours > cache.target_horizon_hours:
        raise MetadataValidationError(
            "minimum_complete_horizon_hours 不能超过 target_horizon_hours"
        )
    acquisition_raw = _table(raw, "acquisition")
    acquisition = AcquisitionSettings(
        gfs_step_hours=_positive_int(acquisition_raw, "gfs_step_hours", 1),
        cycle_lookback_count=_positive_int(acquisition_raw, "cycle_lookback_count", 1),
        request_timeout_seconds=_positive_int(
            acquisition_raw, "request_timeout_seconds", 1
        ),
    )
    clock_raw = _table(raw, "clock")
    clock = ClockSettings(default_speed=_positive_float(clock_raw, "default_speed"))
    corridors_raw = raw.get("corridors", raw.get("scenarios"))
    if not isinstance(corridors_raw, dict):
        raise MetadataValidationError("配置缺少 [corridors] table")
    corridors: dict[str, CorridorSettings] = {}
    for corridor_id, value in corridors_raw.items():
        validate_identifier(str(corridor_id), field="corridor_id")
        if not isinstance(value, dict):
            raise MetadataValidationError(f"corridors.{corridor_id} 必须是 table")
        bbox = _float_tuple(value, "bbox", 4)
        start = _float_tuple(value, "start", 2)
        destination = _float_tuple(value, "destination", 2)
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise MetadataValidationError(f"corridors.{corridor_id}.bbox 顺序无效")
        if not (-180 <= bbox[0] <= 180 and -180 <= bbox[2] <= 180):
            raise MetadataValidationError(f"corridors.{corridor_id}.bbox 经度超界")
        if not (-90 <= bbox[1] <= 90 and -90 <= bbox[3] <= 90):
            raise MetadataValidationError(f"corridors.{corridor_id}.bbox 纬度超界")
        corridors[str(corridor_id)] = CorridorSettings(
            corridor_id=str(corridor_id),
            bbox=bbox,
            start=start,
            destination=destination,
        )
    if not corridors:
        raise MetadataValidationError("至少需要一个 corridors 配置")
    return WorkPackageAConfig(cache, acquisition, clock, MappingProxyType(corridors))


def config_to_dict(config: WorkPackageAConfig) -> dict[str, Any]:
    return {
        "cache": {
            field: getattr(config.cache, field)
            for field in config.cache.__dataclass_fields__
        },
        "acquisition": {
            field: getattr(config.acquisition, field)
            for field in config.acquisition.__dataclass_fields__
        },
        "clock": {"default_speed": config.clock.default_speed},
        "corridors": {
            name: {
                "bbox": list(corridor.bbox),
                "start": list(corridor.start),
                "destination": list(corridor.destination),
            }
            for name, corridor in config.corridors.items()
        },
    }


ScenarioSettings = CorridorSettings


def _table(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise MetadataValidationError(f"配置缺少 [{key}] table")
    return result


def _positive_float(value: Mapping[str, Any], key: str) -> float:
    result = value.get(key)
    if not isinstance(result, int | float) or isinstance(result, bool) or result <= 0:
        raise MetadataValidationError(f"配置 {key} 必须是正数")
    return float(result)


def _positive_int(value: Mapping[str, Any], key: str, minimum: int) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < minimum:
        raise MetadataValidationError(f"配置 {key} 必须是 >= {minimum} 的整数")
    return result


def _nonnegative_int(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise MetadataValidationError(f"配置 {key} 必须是非负整数")
    return result


def _float_tuple(value: Mapping[str, Any], key: str, length: int) -> tuple[float, ...]:
    result = value.get(key)
    if not isinstance(result, list) or len(result) != length:
        raise MetadataValidationError(f"配置 {key} 必须包含 {length} 个数")
    if any(not isinstance(item, int | float) or isinstance(item, bool) for item in result):
        raise MetadataValidationError(f"配置 {key} 必须全部为数值")
    return tuple(float(item) for item in result)
