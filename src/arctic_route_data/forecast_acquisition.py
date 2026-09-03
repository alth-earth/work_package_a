"""Native forecast-window acquisition maintained inside work package A.

The supplied legacy scripts remain supported for migration, but they only fetch
recent analysis frames.  This module acquires a reproducible multi-day window
and publishes every returned valid time through A's normal ingestion path.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np
import requests
import xarray as xr

from arctic_route_data.derivations import derive_sea_ice_edge, derive_sea_ice_type
from arctic_route_data.errors import DataValidationError
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.models import ManifestRecord
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.temporal_split import discover_valid_times
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc

NOMADS_GFS_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
NCEI_GFS_ANALYSIS_ROOT = (
    "https://www.ncei.noaa.gov/oa/prod-model/global-forecast-system/access/"
    "grid-004-0.5-degree/analysis"
)
NCEI_GFS_ANALYSIS_THREDDS_ROOT = (
    "https://www.ncei.noaa.gov/thredds/fileServer/model-gfs-g4-anl-files"
)

# --- Reachability status for the NCEI GFS Grid 4 analysis direct-access layer ---
#
# Live probes (2026-08-22) showed:
#   * NCEI archive `grid-004-0.5-degree/analysis/` root returns `NoSuchKey`
#     (the whole direct-access path was withdrawn during NCEI cloud migration);
#   * the THREDDS `model-gfs-g4-anl-files` dataset scan exposes no month
#     subdirectories for the target window;
#   * NOMADS retains only a rolling recent window (historical cycles return 403).
# The `gfs_4_{date}_{cycle}_{step}.grb2` naming used here is still correct and is
# identical to the convention already used by A's nine complete rows; the failure
# is source-object resolution, not a malformed URL.
#
# Status vocabulary (used by explicit gates instead of silent generic errors):
#   DIRECT_BLOCKED          - exact direct object 404/403/NoSuchKey; not usable now.
#   OFFLINE_ORDER_CANDIDATE - NCEI HAS (Historical Archive System) order path.
#   AVAILABLE_RECENT        - NOMADS rolling window; usable for recent cycles only.
class NceiReachability(StrEnum):
    DIRECT_BLOCKED = "DIRECT_BLOCKED"
    OFFLINE_ORDER_CANDIDATE = "OFFLINE_ORDER_CANDIDATE"
    AVAILABLE_RECENT = "AVAILABLE_RECENT"


# NOMADS recently-published GFS 0.25-degree analysis base (rolling window only).
# Historical cycles (e.g. 2026-02) are NOT available here (verified 403); this
# base is therefore only a candidate for *recent* cycles, never a winter filler.
NOMADS_GFS_RECENT_ANALYSIS_ROOT = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"

# Official offline recovery path for the missing winter window (human-order only).
# NCEI HAS / archive order for GFS Grid 4 analysis (DSI 6182). Not a programmatic
# adapter: requires operator identity and a new checkpointed ingest step.
NCEI_HAS_ORDER_URL = "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC00634"

# Recommended alternative source for the three missing winter rows (proposal
# A-WINTER-MET-001, pending approval). Requires a CDS personal access token,
# dataset-terms acceptance, an approved A CARRA adapter, and grid-relative
# wind (u/v) -> true-east/true-north rotation before normalization.
#   C3S/ECMWF CARRA single levels: reanalysis-carra-single-levels
#   DOI 10.24381/cds.713858f6, CC-BY-4.0.  The official catalogue describes
#   coverage from 1991 to present with monthly updates; availability at the
#   moving edge is deliberately decided by the CDS request, not a fixed date.
CARRA_DATASET_ID = "reanalysis-carra-single-levels"

# AWS Open Data GFS 0.5-degree analysis mirror. NOT hardcoded as a dependency:
# unreachable from this host (network timeout) and documented as a trailing
# ~30-day window, so it cannot be assumed to hold 2026-02. Verified only after
# connectivity changes.
AWS_NOAA_GFS_BDP_PDS_ROOT = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"


@dataclass(frozen=True, slots=True)
class Bounds:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("bbox 经度必须满足 -180 <= west < east <= 180")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("bbox 纬度必须满足 -90 <= south < north <= 90")


@dataclass(frozen=True, slots=True)
class ForecastAcquisitionResult:
    source: str
    route_id: str
    source_snapshot_ids: tuple[str, ...]
    records: tuple[ManifestRecord, ...]
    warnings: tuple[str, ...] = ()


class AcquisitionMode(StrEnum):
    """User-visible time semantics; modes must never be silently mixed."""

    FROZEN_FORECAST = "frozen_forecast"
    RETROSPECTIVE_BEST_ESTIMATE = "retrospective_best_estimate"


@dataclass(frozen=True, slots=True)
class AcquisitionWindow:
    start: datetime
    end: datetime
    horizon_hours: int
    mode: AcquisitionMode


def resolve_acquisition_window(
    *,
    start_time: datetime,
    end_time: datetime | None = None,
    horizon_hours: int | None = None,
    mode: AcquisitionMode | str = AcquisitionMode.FROZEN_FORECAST,
) -> AcquisitionWindow:
    """Resolve one explicit UTC window without changing its historical meaning."""

    start = ensure_utc(start_time, field="start_time")
    selected_mode = AcquisitionMode(mode)
    if end_time is not None and horizon_hours is not None:
        raise ValueError("end_time 与 horizon_hours 只能指定一个")
    if end_time is None and horizon_hours is None:
        raise ValueError("必须指定 end_time 或 horizon_hours")
    if horizon_hours is not None:
        if not isinstance(horizon_hours, int) or isinstance(horizon_hours, bool):
            raise ValueError("horizon_hours 必须是正整数")
        if horizon_hours <= 0:
            raise ValueError("horizon_hours 必须是正整数")
        end = start + timedelta(hours=horizon_hours)
    else:
        end = ensure_utc(end_time, field="end_time")  # type: ignore[arg-type]
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            raise ValueError("end_time 必须晚于 start_time")
        hours, remainder = divmod(seconds, 3600)
        if remainder:
            raise ValueError("当前原生采集窗口要求 start_time 到 end_time 为整小时")
        horizon_hours = int(hours)
    return AcquisitionWindow(start, end, horizon_hours, selected_mode)


@dataclass(frozen=True, slots=True)
class CopernicusForecastSpec:
    data_type: str
    dataset_id: str
    variables: tuple[str, ...]
    product_id: str
    nominal_interval_hours: float
    surface_depth: bool = False
    current_component: str | None = None
    tide_included: bool | None = None
    derivation: str | None = None
    dataset_part: str = "default"
    query_mode: str = "geographic"


TOPAZ_POLAR_STEREOGRAPHIC_PROJ4 = (
    "+proj=stere +lon_0=-45 +lat_0=90 +k=1 +R=6378273"
)
TOPAZ_POLAR_STEREOGRAPHIC_LON_0_DEGREES = -45.0
TOPAZ_POLAR_STEREOGRAPHIC_RADIUS_METRES = 6_378_273.0
# ``originalGrid`` exposes x/y in 100-km grid units, rather than metres.
TOPAZ_ORIGINAL_GRID_COORDINATE_UNIT_METRES = 100_000.0
TOPAZ_TIDAL_CURRENT_ORIGINAL_GRID_DATASET_ID = "dataset-topaz6-arc-15min-3km-be"


COPERNICUS_FORECAST_SPECS: dict[str, CopernicusForecastSpec] = {
    "wave": CopernicusForecastSpec(
        "wave",
        "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i",
        ("VHM0", "VMDR", "VTPK"),
        "GLOBAL_ANALYSISFORECAST_WAV_001_027",
        3.0,
    ),
    "ocean_current": CopernicusForecastSpec(
        "ocean_current",
        "dataset-topaz6-arc-15min-3km-be",
        ("vxo", "vyo"),
        "ARCTIC_ANALYSISFORECAST_PHY_TIDE_002_015",
        0.25,
        current_component="total",
        tide_included=True,
        dataset_part="originalGrid",
        query_mode="projected",
    ),
    "water_level": CopernicusForecastSpec(
        "water_level",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("zos",),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
        1.0,
    ),
    "sea_ice_concentration": CopernicusForecastSpec(
        "sea_ice_concentration",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("siconc",),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
        1.0,
    ),
    "sea_ice_drift": CopernicusForecastSpec(
        "sea_ice_drift",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("vxsi", "vysi"),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
        1.0,
    ),
    "sea_ice_thickness": CopernicusForecastSpec(
        "sea_ice_thickness",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("sithick",),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
        1.0,
    ),
    "sea_ice_type": CopernicusForecastSpec(
        "sea_ice_type",
        "cmems_mod_arc_phy_anfc_nextsim_hm",
        ("siconc", "siconc_young", "siconc_my"),
        "ARCTIC_ANALYSISFORECAST_PHY_ICE_002_011",
        1.0,
        derivation="dominant_class_from_nextsim_concentration_fractions_v1",
    ),
    "sea_ice_edge": CopernicusForecastSpec(
        "sea_ice_edge",
        "cmems_mod_arc_phy_anfc_nextsim_hm",
        ("siconc",),
        "ARCTIC_ANALYSISFORECAST_PHY_ICE_002_011",
        1.0,
        derivation="four_neighbour_ice_side_edge_at_siconc_0.15_v1",
    ),
}

COPERNICUS_DETIDED_CURRENT_FALLBACK = CopernicusForecastSpec(
    "ocean_current",
    "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
    ("vxo", "vyo"),
    "ARCTIC_ANALYSISFORECAST_PHY_002_001",
    1.0,
    True,
    current_component="detided",
    tide_included=False,
)

GFS_DATA_TYPES = frozenset({"wind_field", "temperature", "visibility"})


def _topaz_polar_stereographic_xy(
    *, longitude_degrees: float, latitude_degrees: float
) -> tuple[float, float]:
    """Project one lon/lat point using the TOPAZ native-grid projection.

    TOPAZ ``originalGrid`` uses a spherical north-polar stereographic grid.  A
    local formula keeps the acquisition path independent of an optional
    projection package while matching the official ``+proj=stere`` definition
    supplied by the Copernicus catalogue.
    """

    if not math.isfinite(longitude_degrees) or not math.isfinite(latitude_degrees):
        raise ValueError("TOPAZ 投影坐标必须是有限数值")
    if not -90.0 <= latitude_degrees <= 90.0:
        raise ValueError("TOPAZ 投影 latitude 必须位于 [-90, 90]")
    latitude = math.radians(latitude_degrees)
    longitude_delta = math.radians(
        longitude_degrees - TOPAZ_POLAR_STEREOGRAPHIC_LON_0_DEGREES
    )
    rho = (
        2.0
        * TOPAZ_POLAR_STEREOGRAPHIC_RADIUS_METRES
        * math.tan(math.pi / 4.0 - latitude / 2.0)
        / TOPAZ_ORIGINAL_GRID_COORDINATE_UNIT_METRES
    )
    return (
        rho * math.sin(longitude_delta),
        -rho * math.cos(longitude_delta),
    )


def _topaz_projected_query_bounds(bounds: Bounds) -> dict[str, float]:
    """Return a safe x/y rectangle for a geographic TOPAZ request bbox."""

    # A geographic rectangle's transformed extrema are not always its four
    # corners: its longitude span can cross the TOPAZ central meridian or a
    # 90-degree axis.  Include those stationary longitudes so ``outside``
    # really encloses the requested geographic bbox.
    longitude_samples = {bounds.west, bounds.east}
    for multiple in range(-4, 5):
        candidate = TOPAZ_POLAR_STEREOGRAPHIC_LON_0_DEGREES + 90.0 * multiple
        if bounds.west <= candidate <= bounds.east:
            longitude_samples.add(candidate)
    projected = tuple(
        _topaz_polar_stereographic_xy(
            longitude_degrees=longitude,
            latitude_degrees=latitude,
        )
        for latitude in (bounds.south, bounds.north)
        for longitude in sorted(longitude_samples)
    )
    x_values = tuple(point[0] for point in projected)
    y_values = tuple(point[1] for point in projected)
    return {
        "minimum_x": min(x_values),
        "maximum_x": max(x_values),
        "minimum_y": min(y_values),
        "maximum_y": max(y_values),
    }


def _copernicus_query_bounds(
    spec: CopernicusForecastSpec, bounds: Bounds
) -> dict[str, float]:
    if spec.query_mode == "geographic":
        return {
            "minimum_longitude": bounds.west,
            "maximum_longitude": bounds.east,
            "minimum_latitude": bounds.south,
            "maximum_latitude": bounds.north,
        }
    if spec.query_mode == "projected":
        return _topaz_projected_query_bounds(bounds)
    raise ValueError(
        f"{spec.data_type} 使用了不支持的 Copernicus 查询模式: "
        f"{spec.query_mode!r}"
    )


def _attach_topaz_original_grid_projection_metadata(
    dataset: xr.Dataset, *, spec: CopernicusForecastSpec
) -> None:
    """Supply the documented TOPAZ CRS when the returned CF mapping is absent.

    The TIDE ``originalGrid`` response has 2-D latitude/longitude and projected
    velocity components, but the live service currently omits the scalar
    ``stereographic`` mapping variable that its components reference.  This
    exact acquisition spec is our authoritative product/part selection, so
    preserve that projection explicitly for normalisation.  No other product
    or dataset part receives an inferred central meridian.
    """

    if not (
        spec.dataset_id == TOPAZ_TIDAL_CURRENT_ORIGINAL_GRID_DATASET_ID
        and spec.dataset_part == "originalGrid"
        and spec.query_mode == "projected"
    ):
        return
    dataset.attrs.update(
        {
            "projection": "polar_stereographic",
            "straight_vertical_longitude_from_pole": (
                TOPAZ_POLAR_STEREOGRAPHIC_LON_0_DEGREES
            ),
        }
    )


def _with_copernicus_source_valid_mask(
    dataset: xr.Dataset,
    *,
    spec: CopernicusForecastSpec,
    requested_start: datetime,
    requested_end: datetime,
) -> xr.Dataset:
    """Attach the native source's spatial validity domain before frame splitting.

    This mask is deliberately only a data-availability domain. It carries no
    navigation, jurisdiction, or surface-classification meaning.
    """

    coordinate_names = {
        name.casefold(): name for name in (*dataset.coords, *dataset.variables)
    }
    longitude_name = next(
        (
            coordinate_names[name]
            for name in ("longitude", "lon", "nav_lon")
            if name in coordinate_names
        ),
        None,
    )
    latitude_name = next(
        (
            coordinate_names[name]
            for name in ("latitude", "lat", "nav_lat")
            if name in coordinate_names
        ),
        None,
    )
    if longitude_name is None or latitude_name is None:
        raise DataValidationError(
            f"{spec.data_type} 无法从 Copernicus 完整请求结果派生 source_valid_mask："
            "缺少经纬度坐标"
        )
    spatial_dims = set(dataset[longitude_name].dims) | set(dataset[latitude_name].dims)
    if not spatial_dims:
        raise DataValidationError(
            f"{spec.data_type} 无法从 Copernicus 完整请求结果派生 source_valid_mask："
            "经纬度没有空间维度"
        )

    source_names = {name.casefold(): name for name in dataset.data_vars}
    required_arrays: list[tuple[str, xr.DataArray]] = []
    for requested_name in spec.variables:
        actual_name = source_names.get(requested_name.casefold())
        if actual_name is None:
            raise DataValidationError(
                f"{spec.data_type} 的 Copernicus 完整请求结果缺少必需变量 "
                f"{requested_name!r}"
            )
        array = dataset[actual_name]
        if not np.issubdtype(array.dtype, np.number):
            raise DataValidationError(
                f"{spec.data_type} 的 Copernicus 必需变量 {actual_name!r} 必须是数值"
            )
        if not spatial_dims.issubset(array.dims):
            raise DataValidationError(
                f"{spec.data_type} 的 Copernicus 必需变量 {actual_name!r} "
                f"没有覆盖空间维度 {sorted(spatial_dims)}"
            )
        required_arrays.append((actual_name, array))

    source_valid_mask: xr.DataArray | None = None
    ordered_spatial_dims = tuple(
        dim for dim in required_arrays[0][1].dims if dim in spatial_dims
    )
    for _, array in required_arrays:
        finite = xr.apply_ufunc(np.isfinite, array)
        reduction_dims = [dim for dim in finite.dims if dim not in spatial_dims]
        finite_any = finite.any(dim=reduction_dims) if reduction_dims else finite
        source_valid_mask = (
            finite_any
            if source_valid_mask is None
            else source_valid_mask & finite_any
        )
    assert source_valid_mask is not None
    source_valid_mask = source_valid_mask.transpose(*ordered_spatial_dims).astype(bool)
    if not bool(source_valid_mask.any().item()):
        raise DataValidationError(
            f"{spec.data_type} 的 Copernicus 请求域没有任何含有限必需变量的空间单元"
        )
    source_valid_mask.name = "source_valid_mask"
    source_valid_mask.attrs = {
        "semantic_version": "a.source-valid-mask.v2",
        "semantic_role": "source_valid_domain",
        "derivation_method": (
            "all_required_variables_finite_over_complete_requested_dataset"
        ),
        "derivation_scope": "native_copernicus_request_before_temporal_split",
        "required_source_variables": json.dumps(
            [name for name, _ in required_arrays], sort_keys=True
        ),
        "requested_start": isoformat_utc(requested_start),
        "requested_end": isoformat_utc(requested_end),
        "navigation_semantics": "none",
        "classification_semantics": "none",
    }
    result = dataset.copy()
    result["source_valid_mask"] = source_valid_mask
    return result


def _select_hourly_aligned(
    dataset: xr.Dataset, *, start: datetime, end: datetime
) -> xr.Dataset:
    """Select exact UTC whole-hour values lazily before loading 15-minute currents."""

    if "time" not in dataset.coords:
        raise DataValidationError("含潮总流缺少 time 坐标，不能安全抽取逐小时值")
    values = np.asarray(dataset.time.values)
    if not np.issubdtype(values.dtype, np.datetime64) or values.ndim != 1:
        raise DataValidationError("含潮总流 time 必须是一维 datetime64 坐标")
    targets = np.arange(
        np.datetime64(start.replace(tzinfo=None), "h"),
        np.datetime64(end.replace(tzinfo=None), "h") + np.timedelta64(1, "h"),
        np.timedelta64(1, "h"),
    ).astype("datetime64[ns]")
    source_values = values.astype("datetime64[ns]")
    indices = np.searchsorted(source_values, targets)
    if (
        indices.size != targets.size
        or np.any(indices >= source_values.size)
        or not np.array_equal(source_values[indices], targets)
    ):
        raise DataValidationError(
            "含潮总流 15 分钟时间轴没有覆盖全部 UTC 整点；禁止近邻代替"
        )
    return dataset.isel(time=xr.DataArray(indices, dims="time"))


class NativeForecastAcquirer:
    def __init__(
        self,
        data_root: str | Path,
        *,
        request_timeout_seconds: int = 120,
        http_session: requests.Session | None = None,
        copernicus_open_dataset=None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.publisher = AcquisitionPublisher(self.data_root)
        self.request_timeout_seconds = request_timeout_seconds
        self.http = http_session or requests.Session()
        self._copernicus_open_dataset = copernicus_open_dataset

    def acquire_gfs(
        self,
        *,
        route_id: str,
        bounds: Bounds,
        as_of: datetime,
        horizon_hours: int = 156,
        step_hours: int = 3,
        cycle_lookback_count: int = 4,
        data_types: Iterable[str] = ("wind_field", "temperature", "visibility"),
        mode: AcquisitionMode | str = AcquisitionMode.FROZEN_FORECAST,
    ) -> ForecastAcquisitionResult:
        acquisition_mode = AcquisitionMode(mode)
        if acquisition_mode is AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE:
            return self.acquire_gfs_analysis(
                route_id=route_id,
                bounds=bounds,
                start_time=as_of,
                horizon_hours=horizon_hours,
                data_types=data_types,
            )
        requested_types = tuple(dict.fromkeys(data_types))
        if not requested_types:
            raise ValueError("GFS data_types 不能为空")
        unsupported = sorted(set(requested_types) - GFS_DATA_TYPES)
        if unsupported:
            raise ValueError(f"GFS 不支持这些 data_type: {', '.join(unsupported)}")
        if horizon_hours < 0 or horizon_hours > 384:
            raise ValueError("GFS horizon_hours 必须位于 [0, 384]")
        if step_hours <= 0:
            raise ValueError("step_hours 必须大于 0")
        as_of = ensure_utc(as_of, field="as_of")
        requested_end = as_of + timedelta(hours=horizon_hours)
        cycle, final_hour = self._select_complete_gfs_cycle(
            as_of=as_of,
            bounds=bounds,
            requested_end=requested_end,
            step_hours=step_hours,
            data_types=requested_types,
            lookback_count=cycle_lookback_count,
        )
        if final_hour > 120 and step_hours % 3:
            raise ValueError(
                "GFS f120 之后只发布 3 小时时效；跨越 f120 时 step_hours 必须是 3 的倍数"
            )
        hours = forecast_hours(final_hour, step_hours)
        request_signature = _gfs_request_signature(bounds, requested_types)
        snapshot_id = f"gfs-{cycle:%Y%m%dT%HZ}-{request_signature}"
        records: list[ManifestRecord] = []
        for forecast_hour in hours:
            path, evidence, source_url = self._obtain_gfs_file(
                cycle=cycle,
                forecast_hour=forecast_hour,
                bounds=bounds,
                data_types=requested_types,
            )
            datasets = _open_gfs_data_types(path, requested_types)
            for data_type, dataset in datasets.items():
                dataset.attrs.update(
                    {
                        "product_id": "NOAA_GFS_0P25",
                        "forecast_reference_time": isoformat_utc(cycle),
                        "source_snapshot_id": snapshot_id,
                    }
                )
                result = self.publisher.publish_dataset(
                    dataset,
                    data_type=data_type,
                    route_id=route_id,
                    source="NOAA GFS/NOMADS",
                    version=snapshot_id,
                    issue_evidence=evidence,
                    metadata={
                        "product_kind": "forecast",
                        "acquisition_mode": acquisition_mode.value,
                        "source_fidelity": "frozen_operational_forecast_cycle",
                        "source_snapshot_id": snapshot_id,
                        "forecast_cycle_id": f"{cycle:%Y%m%dT%HZ}",
                        "source_uri": source_url,
                        "source_file": path.name,
                        "source_file_checksum": sha256_file(path),
                        "source_snapshot_relative_path": path.relative_to(
                            self.data_root
                        ).as_posix(),
                        "nominal_interval_hours": float(step_hours),
                        "requested_forecast_hour": forecast_hour,
                    },
                )
                records.extend(result.records)
        return ForecastAcquisitionResult(
            source="NOAA GFS/NOMADS",
            route_id=route_id,
            source_snapshot_ids=(snapshot_id,),
            records=tuple(records),
        )

    def acquire_gfs_analysis(
        self,
        *,
        route_id: str,
        bounds: Bounds,
        start_time: datetime,
        horizon_hours: int,
        data_types: Iterable[str] = ("wind_field", "temperature", "visibility"),
    ) -> ForecastAcquisitionResult:
        """Acquire NCEI f000 analyses every 6 h as retrospective best estimates."""

        requested_types = tuple(dict.fromkeys(data_types))
        unsupported = sorted(set(requested_types) - GFS_DATA_TYPES)
        if not requested_types or unsupported:
            raise ValueError(
                "NCEI GFS analysis data_types 无效"
                + (": " + ", ".join(unsupported) if unsupported else "")
            )
        if horizon_hours <= 0:
            raise ValueError("horizon_hours 必须大于 0")
        start = ensure_utc(start_time, field="start_time")
        if start.minute or start.second or start.microsecond or start.hour % 6:
            raise ValueError("NCEI GFS analysis 窗口必须从 00/06/12/18Z 整点开始")
        end = start + timedelta(hours=horizon_hours)
        cycles = []
        current = start
        while current <= end:
            cycles.append(current)
            current += timedelta(hours=6)
        records: list[ManifestRecord] = []
        snapshots: list[str] = []
        for cycle in cycles:
            path, evidence, source_url = self._obtain_ncei_analysis_file(
                cycle=cycle,
                data_types=requested_types,
            )
            request_metadata_path = path.with_suffix(path.suffix + ".metadata.json")
            request_metadata = json.loads(
                request_metadata_path.read_text(encoding="utf-8")
            )
            snapshot_id = (
                f"ncei-gfs-analysis-{cycle:%Y%m%dT%HZ}-"
                f"{_gfs_request_signature(bounds, requested_types)}"
            )
            snapshots.append(snapshot_id)
            datasets = _open_gfs_data_types(path, requested_types)
            for data_type, source_dataset in datasets.items():
                dataset = _crop_rectilinear(source_dataset, bounds)
                dataset.attrs.update(
                    {
                        "product_id": "NCEI_GFS_GRID4_ANALYSIS",
                        "forecast_reference_time": isoformat_utc(cycle),
                        "source_snapshot_id": snapshot_id,
                    }
                )
                published = self.publisher.publish_dataset(
                    dataset,
                    data_type=data_type,
                    route_id=route_id,
                    source="NOAA NCEI GFS 0.5-degree Analysis",
                    version=snapshot_id,
                    issue_evidence=evidence,
                    metadata={
                        "product_kind": "analysis",
                        "acquisition_mode": (
                            AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE.value
                        ),
                        "source_fidelity": "retrospective_f000_analysis",
                        "source_snapshot_id": snapshot_id,
                        "analysis_cycle_id": f"{cycle:%Y%m%dT%HZ}",
                        "forecast_lead_hours": 0,
                        "source_uri": source_url,
                        "source_file": path.name,
                        "source_file_checksum": sha256_file(path),
                        "source_snapshot_relative_path": path.relative_to(
                            self.data_root
                        ).as_posix(),
                        "source_inventory_file": request_metadata["inventory_file"],
                        "source_inventory_checksum": request_metadata[
                            "inventory_checksum"
                        ],
                        "source_byte_ranges": request_metadata["byte_ranges"],
                        "source_request_metadata_relative_path": (
                            request_metadata_path.relative_to(self.data_root).as_posix()
                        ),
                        "nominal_interval_hours": 6.0,
                        "knowledge_time_policy": (
                            "NCEI archive Last-Modified/retrieval gate; replay as_of must "
                            "not predate issue_time"
                        ),
                    },
                )
                records.extend(published.records)
        return ForecastAcquisitionResult(
            source="NOAA NCEI GFS 0.5-degree Analysis",
            route_id=route_id,
            source_snapshot_ids=tuple(snapshots),
            records=tuple(records),
        )

    def _acquire_ncei_analysis_file(self, **kwargs):
        """Compatibility spelling retained for tests/extensions."""

        return self._obtain_ncei_analysis_file(**kwargs)

    def _obtain_ncei_analysis_file(
        self,
        *,
        cycle: datetime,
        data_types: tuple[str, ...],
    ) -> tuple[Path, IssueTimeEvidence, str]:
        base_url = ncei_gfs_analysis_url(cycle)
        thredds_url = ncei_gfs_analysis_thredds_url(cycle)
        inventory_url = base_url + ".inv"
        try:
            inventory_response = self.http.get(
                inventory_url, timeout=self.request_timeout_seconds
            )
            inventory_response.raise_for_status()
        except Exception as primary_error:
            status = getattr(primary_error, "response", None)
            code = getattr(status, "status_code", None)
            # Explicit reachability gate: a missing direct object is not a generic
            # failure. The NCEI archive `analysis/` path was withdrawn during
            # cloud migration (root returns NoSuchKey; exact object 404); the only
            # official recovery for the missing winter window is an HAS/archive
            # order, not a corrected direct URL.
            if code in (403, 404) or "NoSuchKey" in str(primary_error):
                raise DataValidationError(
                    "NCEI GFS Grid 4 analysis direct object unreachable "
                    f"({NceiReachability.DIRECT_BLOCKED}); status={code}. "
                    "The NCEI direct-access path was withdrawn during cloud "
                    "migration. Use the HAS/archive order recovery path "
                    f"({NceiReachability.OFFLINE_ORDER_CANDIDATE}): "
                    f"{NCEI_HAS_ORDER_URL}"
                ) from primary_error
            raise
        inventory_text = inventory_response.text
        inventory_bytes = bytes(getattr(inventory_response, "content", b""))
        if not inventory_bytes:
            inventory_bytes = inventory_text.encode("utf-8")
        ranges = ncei_inventory_ranges(inventory_text, data_types=data_types)
        signature = hashlib.sha256(
            json.dumps(ranges, sort_keys=True).encode()
        ).hexdigest()[:12]
        directory = (
            self.data_root
            / "source_snapshots"
            / "ncei_gfs_analysis"
            / f"{cycle:%Y%m%dT%HZ}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"gfs-grid4-anl-f000.{signature}.grib2"
        request_metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        inventory_checksum = hashlib.sha256(inventory_bytes).hexdigest()
        inventory_path = directory / (
            f"gfs-grid4-anl-f000.{inventory_checksum[:12]}.grib2.inv"
        )
        _atomic_bytes(inventory_path, inventory_bytes)
        source_url = base_url
        access_method = "ncei_archive_https_range"
        fallback_reason: str | None = None
        if not path.is_file() or not _valid_grib(path):
            temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")

            def write_subset(data_url: str) -> None:
                with temporary.open("wb") as handle:
                    for start, end in ranges:
                        headers = {
                            "Range": (
                                f"bytes={start}-{end}"
                                if end is not None
                                else f"bytes={start}-"
                            )
                        }
                        response = self.http.get(
                            data_url,
                            headers=headers,
                            timeout=self.request_timeout_seconds,
                            stream=True,
                        )
                        try:
                            response.raise_for_status()
                            _validate_partial_content_response(
                                response,
                                start=start,
                                end=end,
                            )
                            body = bytes(response.content)
                            if end is not None and len(body) != end - start + 1:
                                raise DataValidationError(
                                    "NCEI Range 响应实际字节数与请求不一致"
                                )
                            handle.write(body)
                        finally:
                            with suppress(AttributeError):
                                response.close()

            try:
                try:
                    write_subset(base_url)
                except Exception as primary_error:
                    temporary.unlink(missing_ok=True)
                    fallback_reason = f"{type(primary_error).__name__}: {primary_error}"
                    source_url = thredds_url
                    access_method = "ncei_thredds_httpserver_range"
                    write_subset(thredds_url)
            except Exception as fallback_error:
                temporary.unlink(missing_ok=True)
                if fallback_reason is None:
                    raise
                raise DataValidationError(
                    "NCEI GFS 两种官方访问方式均失败；"
                    f"archive={fallback_reason}; "
                    f"thredds={type(fallback_error).__name__}: {fallback_error}"
                ) from fallback_error
            if not _valid_grib(temporary):
                temporary.unlink(missing_ok=True)
                raise DataValidationError("NCEI Range 响应无法组成完整的所需 GRIB2 消息")
            temporary.replace(path)
        elif request_metadata_path.is_file():
            previous_metadata = json.loads(request_metadata_path.read_text(encoding="utf-8"))
            previous_source_url = previous_metadata.get("source_url", base_url)
            if previous_source_url not in {base_url, thredds_url}:
                raise DataValidationError("缓存的 NCEI source_url 不属于当前两种官方入口")
            source_url = previous_source_url
            access_method = previous_metadata.get(
                "data_access_method",
                (
                    "ncei_thredds_httpserver_range"
                    if source_url == thredds_url
                    else "ncei_archive_https_range"
                ),
            )
            expected_access_method = (
                "ncei_thredds_httpserver_range"
                if source_url == thredds_url
                else "ncei_archive_https_range"
            )
            if access_method != expected_access_method:
                raise DataValidationError("缓存的 NCEI data_access_method 与 source_url 不一致")
            fallback_reason = previous_metadata.get("data_access_fallback_reason")
        observed = datetime.now(UTC)
        last_modified = inventory_response.headers.get("Last-Modified")
        issue = observed
        method = IssueTimeMethod.CONSERVATIVE_RETRIEVAL
        authoritative = False
        if last_modified:
            with suppress(ValueError, TypeError, OverflowError):
                parsed = parsedate_to_datetime(last_modified)
                aware = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
                issue = aware.astimezone(UTC)
                method = IssueTimeMethod.HTTP_LAST_MODIFIED
                authoritative = True
        evidence = IssueTimeEvidence(
            issue_time=issue,
            method=method,
            authority="NOAA NCEI archive",
            reference=inventory_url,
            observed_at=observed,
            raw_value=last_modified or isoformat_utc(observed),
            authoritative=authoritative,
        )
        _atomic_json(
            request_metadata_path,
            {
                "source_url": source_url,
                "primary_source_url": base_url,
                "fallback_source_url": thredds_url,
                "data_access_method": access_method,
                "data_access_fallback_reason": fallback_reason,
                "inventory_url": inventory_url,
                "inventory_file": inventory_path.name,
                "inventory_checksum": inventory_checksum,
                "byte_ranges": [
                    [start, end] for start, end in ranges
                ],
                "subset_checksum": sha256_file(path),
                "retrieved_at": isoformat_utc(observed),
                "issue_time_evidence": evidence.to_dict(),
            },
        )
        return path, evidence, source_url

    def acquire_copernicus(
        self,
        *,
        route_id: str,
        bounds: Bounds,
        start_time: datetime,
        horizon_hours: int = 156,
        data_types: Iterable[str] = tuple(COPERNICUS_FORECAST_SPECS),
        mode: AcquisitionMode | str = AcquisitionMode.FROZEN_FORECAST,
        require_total_current: bool = False,
    ) -> ForecastAcquisitionResult:
        acquisition_mode = AcquisitionMode(mode)
        requested_types = tuple(dict.fromkeys(data_types))
        if not requested_types:
            raise ValueError("Copernicus data_types 不能为空")
        unsupported = sorted(set(requested_types) - COPERNICUS_FORECAST_SPECS.keys())
        if unsupported:
            raise ValueError(f"Copernicus 不支持这些 data_type: {', '.join(unsupported)}")
        if horizon_hours <= 0 or horizon_hours > 240:
            raise ValueError("Copernicus horizon_hours 必须位于 [1, 240]")
        reuse_nextsim = {"sea_ice_type", "sea_ice_edge"}.issubset(requested_types)
        if reuse_nextsim:
            requested_types = (
                "sea_ice_type",
                *(item for item in requested_types if item != "sea_ice_type"),
            )
        start = ensure_utc(start_time, field="start_time")
        end = start + timedelta(hours=horizon_hours)
        records: list[ManifestRecord] = []
        snapshot_ids: list[str] = []
        warnings: list[str] = []
        nextsim_source_dataset: xr.Dataset | None = None
        open_dataset = self._copernicus_open_dataset
        if open_dataset is None:
            try:
                import copernicusmarine
            except ImportError as exc:
                raise RuntimeError("请安装 acquisition extra 以使用 Copernicus") from exc
            open_dataset = copernicusmarine.open_dataset
            username, password = _copernicus_credentials_from_environment()
        else:
            username = password = None

        for data_type in requested_types:
            preferred_spec = COPERNICUS_FORECAST_SPECS[data_type]
            candidates = (preferred_spec,)
            if data_type == "ocean_current":
                candidates = (preferred_spec, COPERNICUS_DETIDED_CURRENT_FALLBACK)
            failures: list[str] = []
            query_bounds = _copernicus_query_bounds(preferred_spec, bounds)
            for spec in candidates:
                if (
                    nextsim_source_dataset is not None
                    and spec.dataset_id == "cmems_mod_arc_phy_anfc_nextsim_hm"
                    and set(spec.variables).issubset(nextsim_source_dataset.data_vars)
                ):
                    dataset = nextsim_source_dataset.copy(deep=False)
                    output_interval = spec.nominal_interval_hours
                    break
                if (
                    spec is not preferred_spec
                    and data_type == "ocean_current"
                    and require_total_current
                ):
                    raise DataValidationError(
                        "require_total_current=True 时含潮总流首选源不可用；"
                        "拒绝 detided fallback，未发布后备记录。"
                        + (" 原因: " + " | ".join(failures) if failures else "")
                    )
                query_bounds = _copernicus_query_bounds(spec, bounds)
                kwargs: dict[str, Any] = {
                    "dataset_id": spec.dataset_id,
                    "dataset_part": spec.dataset_part,
                    "variables": list(spec.variables),
                    **query_bounds,
                    "start_datetime": start,
                    "end_datetime": end,
                    "coordinates_selection_method": "outside",
                }
                if spec.surface_depth:
                    kwargs.update({"minimum_depth": 0, "maximum_depth": 0})
                if username and password:
                    kwargs.update({"username": username, "password": password})
                try:
                    opened = open_dataset(**kwargs)
                    try:
                        output_interval = spec.nominal_interval_hours
                        if spec.current_component == "total":
                            opened = _select_hourly_aligned(opened, start=start, end=end)
                            output_interval = 1.0
                        dataset = opened.load()
                    finally:
                        opened.close()
                    if reuse_nextsim and data_type == "sea_ice_type":
                        nextsim_source_dataset = dataset.copy(deep=False)
                    break
                except Exception as exc:
                    failures.append(
                        f"{spec.dataset_id}: {type(exc).__name__}: {exc!r}"
                    )
            else:
                raise DataValidationError(
                    f"{data_type} 的 Copernicus 首选/后备源均失败: " + " | ".join(failures)
                )
            if spec is not preferred_spec:
                warnings.append(
                    "ocean_current 含潮总流不可用，已明确降级为 detided 后备；"
                    "两者未相加"
                )
            retrieved_at = datetime.now(UTC)
            dataset = _with_copernicus_source_valid_mask(
                dataset,
                spec=spec,
                requested_start=start,
                requested_end=end,
            )
            if data_type == "sea_ice_type":
                dataset = derive_sea_ice_type(dataset)
            elif data_type == "sea_ice_edge":
                dataset = derive_sea_ice_edge(dataset)
            valid_times = discover_valid_times(dataset)
            snapshot_id = _snapshot_id(spec.dataset_id, retrieved_at, valid_times)
            snapshot_ids.append(snapshot_id)
            dataset.attrs.update(
                {
                    "copernicus_product": spec.product_id,
                    "copernicus_dataset_id": spec.dataset_id,
                    "copernicus_dataset_part": spec.dataset_part,
                    "query_mode": spec.query_mode,
                    # NetCDF attributes cannot contain mappings.  The manifest
                    # retains the structured form below, while the immutable
                    # source snapshot records an equivalent canonical JSON
                    # string.
                    "query_bounds": json.dumps(query_bounds, sort_keys=True),
                    "source_snapshot_id": snapshot_id,
                    "acquisition_mode": acquisition_mode.value,
                    "source_fidelity": (
                        "retrospective_best_estimate"
                        if acquisition_mode
                        is AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE
                        else "frozen_service_forecast_snapshot"
                    ),
                }
            )
            if spec.query_mode == "projected":
                dataset.attrs["query_projection"] = TOPAZ_POLAR_STEREOGRAPHIC_PROJ4
            _attach_topaz_original_grid_projection_metadata(dataset, spec=spec)
            evidence = IssueTimeEvidence(
                issue_time=retrieved_at,
                method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
                authority="Copernicus Marine Data Store",
                reference=(
                    "copernicusmarine.open_dataset("
                    f"dataset_id={spec.dataset_id!r}, "
                    f"dataset_part={spec.dataset_part!r})"
                ),
                observed_at=retrieved_at,
                raw_value=isoformat_utc(retrieved_at),
                authoritative=False,
            )
            source_file = self._save_copernicus_snapshot(dataset, snapshot_id, data_type)
            published = self.publisher.publish_dataset(
                dataset,
                data_type=data_type,
                route_id=route_id,
                source="Copernicus Marine",
                version=snapshot_id,
                issue_evidence=evidence,
                metadata={
                    "product_kind": (
                        "retrospective_best_estimate"
                        if acquisition_mode
                        is AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE
                        else "forecast"
                    ),
                    "acquisition_mode": acquisition_mode.value,
                    "source_fidelity": (
                        "retrospective_best_estimate"
                        if acquisition_mode
                        is AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE
                        else "frozen_service_forecast_snapshot"
                    ),
                    "source_snapshot_id": snapshot_id,
                    "product_id": spec.product_id,
                    "dataset_id": spec.dataset_id,
                    "dataset_part": spec.dataset_part,
                    "query_mode": spec.query_mode,
                    "query_bounds": query_bounds,
                    "query_projection": (
                        TOPAZ_POLAR_STEREOGRAPHIC_PROJ4
                        if spec.query_mode == "projected"
                        else None
                    ),
                    "service": "selected_by_copernicusmarine",
                    "source_uri": (
                        f"https://data.marine.copernicus.eu/product/{spec.product_id}/description"
                    ),
                    "source_file": source_file.name,
                    "source_file_checksum": sha256_file(source_file),
                    "source_snapshot_relative_path": source_file.relative_to(
                        self.data_root
                    ).as_posix(),
                    "nominal_interval_hours": output_interval,
                    "source_native_interval_hours": spec.nominal_interval_hours,
                    "output_interval_hours": output_interval,
                    "temporal_selection": (
                        "strict_utc_whole_hour_before_load"
                        if spec.current_component == "total"
                        else "native"
                    ),
                    "current_component": spec.current_component,
                    "tide_included": spec.tide_included,
                    "derivation_method": spec.derivation,
                    "source_combination_policy": (
                        "single_source_preferred_then_detided_fallback_never_sum"
                        if data_type == "ocean_current"
                        else None
                    ),
                    "source_fallback_reason": (
                        " | ".join(failures) if spec is not preferred_spec else None
                    ),
                    "requested_start": isoformat_utc(start),
                    "requested_end": isoformat_utc(end),
                },
            )
            records.extend(published.records)
            if data_type == "sea_ice_edge":
                nextsim_source_dataset = None
            available_start = min(valid_times)
            available_end = max(valid_times)
            if available_start > start or available_end < end:
                warnings.append(
                    f"{data_type} 实际覆盖 {isoformat_utc(available_start)} .. "
                    f"{isoformat_utc(available_end)}，未完全覆盖请求窗"
                )
        return ForecastAcquisitionResult(
            source="Copernicus Marine",
            route_id=route_id,
            source_snapshot_ids=tuple(snapshot_ids),
            records=tuple(records),
            warnings=tuple(warnings),
        )


    def _select_complete_gfs_cycle(
        self,
        *,
        as_of: datetime,
        bounds: Bounds,
        requested_end: datetime,
        step_hours: int,
        data_types: tuple[str, ...],
        lookback_count: int,
    ) -> tuple[datetime, int]:
        cycle = as_of.replace(minute=0, second=0, microsecond=0) - timedelta(
            hours=as_of.hour % 6
        )
        errors: list[str] = []
        for index in range(lookback_count):
            candidate = cycle - timedelta(hours=6 * index)
            required_hours = (requested_end - candidate).total_seconds() / 3600.0
            final_hour = math.ceil(required_hours / step_hours) * step_hours
            if final_hour > 384:
                errors.append(
                    f"{candidate:%Y%m%dT%HZ}: 需 f{final_hour:03d}，超过 GFS f384"
                )
                continue
            try:
                path, _, _ = self._obtain_gfs_file(
                    cycle=candidate,
                    forecast_hour=final_hour,
                    bounds=bounds,
                    data_types=data_types,
                )
                _open_gfs_data_types(path, data_types)
                return candidate, final_hour
            except Exception as exc:
                errors.append(f"{candidate:%Y%m%dT%HZ}: {exc}")
        raise DataValidationError(
            "找不到已完整发布目标时效的 GFS 周期；" + " | ".join(errors)
        )

    def _obtain_gfs_file(
        self,
        *,
        cycle: datetime,
        forecast_hour: int,
        bounds: Bounds,
        data_types: tuple[str, ...],
    ) -> tuple[Path, IssueTimeEvidence, str]:
        directory = self.data_root / "source_snapshots" / "gfs" / f"{cycle:%Y%m%dT%HZ}"
        directory.mkdir(parents=True, exist_ok=True)
        signature = _gfs_request_signature(bounds, data_types)
        path = directory / (
            f"gfs.t{cycle:%H}z.pgrb2.0p25.f{forecast_hour:03d}.{signature}.grib2"
        )
        evidence_path = path.with_suffix(path.suffix + ".metadata.json")
        if path.is_file() and evidence_path.is_file() and _valid_grib(path):
            metadata = json.loads(evidence_path.read_text(encoding="utf-8"))
            if metadata.get("checksum") == sha256_file(path):
                return path, _evidence_from_dict(metadata["issue_time_evidence"]), str(
                    metadata["source_url"]
                )

        params = build_gfs_filter_params(
            cycle=cycle,
            forecast_hour=forecast_hour,
            bounds=bounds,
            data_types=data_types,
        )
        response = self.http.get(
            NOMADS_GFS_FILTER_URL,
            params=params,
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        temporary.write_bytes(response.content)
        if not _valid_grib(temporary):
            temporary.unlink(missing_ok=True)
            raise DataValidationError(
                f"NOMADS 返回的 f{forecast_hour:03d} 不是完整 GRIB2；可能尚未发布"
            )
        temporary.replace(path)
        observed_at = datetime.now(UTC)
        last_modified = response.headers.get("Last-Modified")
        parsed_last_modified = None
        if last_modified:
            with suppress(ValueError, TypeError, OverflowError):
                parsed_last_modified = parsedate_to_datetime(last_modified)
                if parsed_last_modified.tzinfo is None:
                    parsed_last_modified = parsed_last_modified.replace(tzinfo=UTC)
                parsed_last_modified = parsed_last_modified.astimezone(UTC)
        # The NOMADS filter endpoint can generate a subset on demand. Its HTTP
        # timestamp is therefore not automatically the producer's publication
        # time. Use successful retrieval as the conservative availability gate.
        evidence = IssueTimeEvidence(
            issue_time=observed_at,
            method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
            authority="NOAA GFS/NOMADS",
            reference=str(response.url),
            observed_at=observed_at,
            raw_value=isoformat_utc(observed_at),
            authoritative=False,
        )
        metadata = {
            "source_url": str(response.url),
            "request_params": params,
            "checksum": sha256_file(path),
            "issue_time_evidence": evidence.to_dict(),
            "http_last_modified": (
                isoformat_utc(parsed_last_modified) if parsed_last_modified else None
            ),
        }
        _atomic_json(evidence_path, metadata)
        return path, evidence, str(response.url)

    def _save_copernicus_snapshot(
        self, dataset: xr.Dataset, snapshot_id: str, data_type: str
    ) -> Path:
        directory = self.data_root / "source_snapshots" / "copernicus" / snapshot_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{data_type}.nc"
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        dataset.to_netcdf(temporary, engine="h5netcdf")
        temporary.replace(path)
        return path


def _copernicus_credentials_from_environment() -> tuple[str, str]:
    candidates = (
        (
            "COPERNICUSMARINE_SERVICE_USERNAME",
            "COPERNICUSMARINE_SERVICE_PASSWORD",
        ),
        ("COPERNICUSMARINE_USERNAME", "COPERNICUSMARINE_PASSWORD"),
    )
    partial: list[str] = []
    for username_name, password_name in candidates:
        username = os.getenv(username_name)
        password = os.getenv(password_name)
        if username and password:
            return username, password
        if username or password:
            partial.append(f"{username_name}/{password_name}")
    if partial:
        raise RuntimeError(
            "Copernicus 凭据只配置了一半；请成对设置 " + " 或 ".join(partial)
        )
    raise RuntimeError(
        "Copernicus Marine Toolbox 下载需要免费账户凭据；请设置 "
        "COPERNICUSMARINE_SERVICE_USERNAME/PASSWORD（或项目兼容名 "
        "COPERNICUSMARINE_USERNAME/PASSWORD）"
    )


def forecast_hours(horizon_hours: int, step_hours: int) -> tuple[int, ...]:
    hours = list(range(0, horizon_hours + 1, step_hours))
    if hours[-1] != horizon_hours:
        hours.append(horizon_hours)
    return tuple(hours)


def build_gfs_filter_params(
    *,
    cycle: datetime,
    forecast_hour: int,
    bounds: Bounds,
    data_types: Iterable[str],
) -> dict[str, str]:
    cycle = ensure_utc(cycle, field="cycle")
    requested = set(data_types)
    params = {
        "dir": f"/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos",
        "file": f"gfs.t{cycle:%H}z.pgrb2.0p25.f{forecast_hour:03d}",
        "subregion": "",
        "leftlon": str(bounds.west),
        "rightlon": str(bounds.east),
        "toplat": str(bounds.north),
        "bottomlat": str(bounds.south),
    }
    if "wind_field" in requested:
        params.update(
            {"var_UGRD": "on", "var_VGRD": "on", "lev_10_m_above_ground": "on"}
        )
    if "temperature" in requested:
        params.update({"var_TMP": "on", "lev_2_m_above_ground": "on"})
    if "visibility" in requested:
        params.update({"var_VIS": "on", "lev_surface": "on"})
    return params


def ncei_gfs_analysis_url(cycle: datetime) -> str:
    cycle = ensure_utc(cycle, field="cycle")
    if cycle.minute or cycle.second or cycle.microsecond or cycle.hour % 6:
        raise ValueError("NCEI GFS analysis cycle 必须是 00/06/12/18Z")
    return (
        f"{NCEI_GFS_ANALYSIS_ROOT}/{cycle:%Y%m}/{cycle:%Y%m%d}/"
        f"gfs_4_{cycle:%Y%m%d_%H00}_000.grb2"
    )


def ncei_gfs_analysis_thredds_url(cycle: datetime) -> str:
    cycle = ensure_utc(cycle, field="cycle")
    if cycle.minute or cycle.second or cycle.microsecond or cycle.hour % 6:
        raise ValueError("NCEI GFS analysis cycle 必须是 00/06/12/18Z")
    return (
        f"{NCEI_GFS_ANALYSIS_THREDDS_ROOT}/{cycle:%Y%m}/{cycle:%Y%m%d}/"
        f"gfs_4_{cycle:%Y%m%d_%H00}_000.grb2"
    )


def nomads_recent_analysis_url(cycle: datetime, forecast_hour: int = 0) -> str:
    """NOMADS recent-window GFS 0.25-degree analysis object name.

    NOMADS uses a different naming convention from NCEI archive:
    ``gfs.t{HH}z.pgrb2.0p25.f{fff}`` versus NCEI ``gfs_4_{date}_{HHMM}_000.grb2``.
    This helper is provided so a future recent-cycle path can reuse the same
    cycle identity without hard-coding the format mismatch. It does NOT cover
    historical winter cycles (verified 403); use it only for ``AVAILABLE_RECENT``.
    """
    cycle = ensure_utc(cycle, field="cycle")
    return (
        f"{NOMADS_GFS_RECENT_ANALYSIS_ROOT}/gfs.{cycle:%Y%m%d}/"
        f"{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{forecast_hour:03d}"
    )


def gfs_naming_bridges() -> dict[str, str]:
    """Documented mapping between NCEI archive and NOMADS object naming.

    Pure reference helper (no I/O); keeps the two conventions explicit so the
    reachability gate and any future adapter stay aligned. Not invoked at runtime
    for the winter gap, because NOMADS does not serve 2026-02 (DIRECT_BLOCKED).
    """

    return {
        "ncei": "gfs_4_{YYYYMMDD}_{HHMM}_000.grb2",
        "nomads": "gfs.t{HH}z.pgrb2.0p25.f{fff}",
    }


def ncei_inventory_ranges(
    inventory: str,
    *,
    data_types: Iterable[str],
) -> tuple[tuple[int, int | None], ...]:
    patterns: dict[str, tuple[str, ...]] = {
        "wind_field": (":UGRD:10 m above ground:anl:", ":VGRD:10 m above ground:anl:"),
        "temperature": (":TMP:2 m above ground:anl:",),
        "visibility": (":VIS:surface:anl:",),
    }
    requested_patterns = tuple(
        pattern for data_type in data_types for pattern in patterns[data_type]
    )
    entries: list[tuple[int, str]] = []
    for line in inventory.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            entries.append((int(parts[1]), line))
        except ValueError:
            continue
    ranges = []
    matched: set[str] = set()
    for index, (offset, line) in enumerate(entries):
        matching = next((pattern for pattern in requested_patterns if pattern in line), None)
        if matching is None:
            continue
        matched.add(matching)
        next_offset = entries[index + 1][0] if index + 1 < len(entries) else None
        ranges.append((offset, next_offset - 1 if next_offset is not None else None))
    missing = sorted(set(requested_patterns) - matched)
    if missing:
        raise DataValidationError("NCEI inventory 缺少所需分析记录: " + ", ".join(missing))
    return tuple(sorted(ranges))


def _validate_partial_content_response(
    response: Any,
    *,
    start: int,
    end: int | None,
) -> None:
    """Reject servers/proxies that ignore Range before reading a global GRIB body."""

    if getattr(response, "status_code", None) != 206:
        raise DataValidationError(
            "NCEI 未返回 206 Partial Content；拒绝下载 150MB 级全球文件"
        )
    content_range = str(response.headers.get("Content-Range", ""))
    expected_prefix = f"bytes {start}-{end if end is not None else ''}"
    if not content_range.startswith(expected_prefix):
        raise DataValidationError(
            f"NCEI Content-Range 与请求不一致: {content_range!r}"
        )
    if end is not None:
        expected_size = end - start + 1
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None and int(declared_size) != expected_size:
            raise DataValidationError("NCEI Content-Length 与请求字节范围不一致")


def _crop_rectilinear(dataset: xr.Dataset, bounds: Bounds) -> xr.Dataset:
    west = bounds.west % 360
    east = bounds.east % 360
    if west <= east:
        result = dataset.sel(longitude=slice(west, east))
    else:
        result = xr.concat(
            (
                dataset.sel(longitude=slice(west, 360)),
                dataset.sel(longitude=slice(0, east)),
            ),
            dim="longitude",
        )
    latitude = result.latitude
    lat_slice = (
        slice(bounds.north, bounds.south)
        if float(latitude[0]) > float(latitude[-1])
        else slice(bounds.south, bounds.north)
    )
    return result.sel(latitude=lat_slice)


def _open_gfs_data_types(path: Path, requested: Iterable[str]) -> dict[str, xr.Dataset]:
    try:
        import cfgrib
    except ImportError as exc:
        raise RuntimeError("请安装 acquisition extra 以读取 GFS GRIB2") from exc
    with xr.set_options(use_new_combine_kwarg_defaults=True):
        datasets = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    variable_candidates = {
        "wind_field": (("u10", "u"), ("v10", "v")),
        "temperature": (("t2m", "t"),),
        "visibility": (("vis",),),
    }
    results: dict[str, xr.Dataset] = {}
    for data_type in requested:
        arrays: list[xr.DataArray] = []
        for aliases in variable_candidates[data_type]:
            match = next(
                (
                    dataset[name]
                    for name in aliases
                    for dataset in datasets
                    if name in dataset.data_vars
                ),
                None,
            )
            if match is None:
                raise DataValidationError(
                    f"{path.name} 缺少 {data_type} 所需变量候选 {aliases}"
                )
            arrays.append(match)
        results[data_type] = xr.merge(arrays, compat="override", join="exact")
    return results


def _valid_grib(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 16:
        return False
    with path.open("rb") as handle:
        prefix = handle.read(4)
        handle.seek(-4, 2)
        suffix = handle.read(4)
    return prefix == b"GRIB" and suffix == b"7777"


def _gfs_request_signature(bounds: Bounds, data_types: Iterable[str]) -> str:
    request_identity = json.dumps(
        {
            "bbox": [bounds.west, bounds.south, bounds.east, bounds.north],
            "data_types": sorted(set(data_types)),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(request_identity.encode()).hexdigest()[:12]


def _snapshot_id(
    dataset_id: str, retrieved_at: datetime, valid_times: tuple[datetime, ...]
) -> str:
    text = "|".join(
        (
            dataset_id,
            isoformat_utc(retrieved_at),
            isoformat_utc(min(valid_times)),
            isoformat_utc(max(valid_times)),
        )
    )
    return "cmems-" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _evidence_from_dict(value: Mapping[str, Any]) -> IssueTimeEvidence:
    return IssueTimeEvidence(
        issue_time=parse_utc(value["issue_time"], field="issue_time"),
        method=IssueTimeMethod(value["method"]),
        authority=str(value["authority"]),
        reference=str(value["reference"]),
        observed_at=parse_utc(value["observed_at"], field="observed_at"),
        raw_value=str(value["raw_value"]),
        authoritative=bool(value["authoritative"]),
    )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    temporary.write_bytes(value)
    temporary.replace(path)
