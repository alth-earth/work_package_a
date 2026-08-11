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
from pathlib import Path
from typing import Any

import requests
import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.models import ManifestRecord
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.temporal_split import discover_valid_times
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc

NOMADS_GFS_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


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


@dataclass(frozen=True, slots=True)
class CopernicusForecastSpec:
    data_type: str
    dataset_id: str
    variables: tuple[str, ...]
    product_id: str
    surface_depth: bool = False


COPERNICUS_FORECAST_SPECS: dict[str, CopernicusForecastSpec] = {
    "wave": CopernicusForecastSpec(
        "wave",
        "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i",
        ("VHM0", "VMDR", "VTPK"),
        "GLOBAL_ANALYSISFORECAST_WAV_001_027",
    ),
    "ocean_current": CopernicusForecastSpec(
        "ocean_current",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("vxo", "vyo"),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
        True,
    ),
    "water_level": CopernicusForecastSpec(
        "water_level",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("zos",),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
    ),
    "sea_ice_concentration": CopernicusForecastSpec(
        "sea_ice_concentration",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("siconc",),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
    ),
    "sea_ice_drift": CopernicusForecastSpec(
        "sea_ice_drift",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("vxsi", "vysi"),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
    ),
    "sea_ice_thickness": CopernicusForecastSpec(
        "sea_ice_thickness",
        "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i",
        ("sithick",),
        "ARCTIC_ANALYSISFORECAST_PHY_002_001",
    ),
}

GFS_DATA_TYPES = frozenset({"wind_field", "temperature", "visibility"})


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
    ) -> ForecastAcquisitionResult:
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
                        "source_snapshot_id": snapshot_id,
                        "forecast_cycle_id": f"{cycle:%Y%m%dT%HZ}",
                        "source_uri": source_url,
                        "source_file": path.name,
                        "source_file_checksum": sha256_file(path),
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

    def acquire_copernicus(
        self,
        *,
        route_id: str,
        bounds: Bounds,
        start_time: datetime,
        horizon_hours: int = 156,
        data_types: Iterable[str] = tuple(COPERNICUS_FORECAST_SPECS),
    ) -> ForecastAcquisitionResult:
        requested_types = tuple(dict.fromkeys(data_types))
        if not requested_types:
            raise ValueError("Copernicus data_types 不能为空")
        unsupported = sorted(set(requested_types) - COPERNICUS_FORECAST_SPECS.keys())
        if unsupported:
            raise ValueError(f"Copernicus 不支持这些 data_type: {', '.join(unsupported)}")
        if horizon_hours <= 0 or horizon_hours > 240:
            raise ValueError("Copernicus horizon_hours 必须位于 [1, 240]")
        start = ensure_utc(start_time, field="start_time")
        end = start + timedelta(hours=horizon_hours)
        records: list[ManifestRecord] = []
        snapshot_ids: list[str] = []
        warnings: list[str] = []
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
            spec = COPERNICUS_FORECAST_SPECS[data_type]
            kwargs: dict[str, Any] = {
                "dataset_id": spec.dataset_id,
                "dataset_part": "default",
                "variables": list(spec.variables),
                "minimum_longitude": bounds.west,
                "maximum_longitude": bounds.east,
                "minimum_latitude": bounds.south,
                "maximum_latitude": bounds.north,
                "start_datetime": start,
                "end_datetime": end,
                "coordinates_selection_method": "outside",
            }
            if spec.surface_depth:
                kwargs.update({"minimum_depth": 0, "maximum_depth": 0})
            if username and password:
                kwargs.update({"username": username, "password": password})
            dataset = open_dataset(**kwargs).load()
            retrieved_at = datetime.now(UTC)
            valid_times = discover_valid_times(dataset)
            snapshot_id = _snapshot_id(spec.dataset_id, retrieved_at, valid_times)
            snapshot_ids.append(snapshot_id)
            dataset.attrs.update(
                {
                    "copernicus_product": spec.product_id,
                    "copernicus_dataset_id": spec.dataset_id,
                    "source_snapshot_id": snapshot_id,
                }
            )
            evidence = IssueTimeEvidence(
                issue_time=retrieved_at,
                method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
                authority="Copernicus Marine Data Store",
                reference=f"copernicusmarine.open_dataset(dataset_id={spec.dataset_id!r})",
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
                    "product_kind": "forecast",
                    "source_snapshot_id": snapshot_id,
                    "product_id": spec.product_id,
                    "dataset_id": spec.dataset_id,
                    "dataset_part": "default",
                    "service": "selected_by_copernicusmarine",
                    "source_uri": (
                        f"https://data.marine.copernicus.eu/product/{spec.product_id}/description"
                    ),
                    "source_file": source_file.name,
                    "source_file_checksum": sha256_file(source_file),
                    "requested_start": isoformat_utc(start),
                    "requested_end": isoformat_utc(end),
                },
            )
            records.extend(published.records)
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
