"""Run all supplied legacy downloaders through A's authoritative publication pipeline."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import xarray as xr

from arctic_route_data.issue_time import (
    CapturedHttpExchange,
    IssueTimeContext,
    SourceIssueTimeResolver,
)
from arctic_route_data.models import ManifestRecord
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.specs import get_data_type_spec
from arctic_route_data.temporal_split import discover_valid_times, to_utc_datetime


@dataclass(frozen=True, slots=True)
class LegacyDownloaderSpec:
    name: str
    module_relative_path: str
    function_name: str
    data_type: str
    source_family: str
    source_label: str


LEGACY_DOWNLOADERS: dict[str, LegacyDownloaderSpec] = {
    spec.name: spec
    for spec in (
        LegacyDownloaderSpec(
            "sea_ice_concentration",
            "冰密集度/sea_ice_concentration_api.py",
            "download_recent_sea_ice_density",
            "sea_ice_concentration",
            "copernicus_marine",
            "Copernicus Marine / OSI SAF",
        ),
        LegacyDownloaderSpec(
            "sea_ice_type",
            "冰型/sea_ice_type_module.py",
            "download_recent_ice_type",
            "sea_ice_type",
            "osi_saf_thredds",
            "OSI SAF via MET Norway THREDDS",
        ),
        LegacyDownloaderSpec(
            "sea_ice_edge",
            "冰缘/sea_ice_edge_module.py",
            "download_recent_ice_edge",
            "sea_ice_edge",
            "osi_saf_thredds",
            "OSI SAF via MET Norway THREDDS",
        ),
        LegacyDownloaderSpec(
            "sea_ice_drift",
            "冰漂/sea_ice_drift_module.py",
            "download_recent_sea_ice_drift_data",
            "sea_ice_drift",
            "copernicus_marine",
            "Copernicus Marine",
        ),
        LegacyDownloaderSpec(
            "sea_ice_thickness",
            "海冰厚度/sea_ice_thickness_module.py",
            "download_recent_sea_ice_thickness_data",
            "sea_ice_thickness",
            "copernicus_marine",
            "Copernicus Marine",
        ),
        LegacyDownloaderSpec(
            "wave",
            "波浪/wave_data_module.py",
            "download_recent_wave_data",
            "wave",
            "copernicus_marine",
            "Copernicus Marine",
        ),
        LegacyDownloaderSpec(
            "ocean_current",
            "流场数据/ocean_current_data_module.py",
            "download_recent_ocean_current_data",
            "ocean_current",
            "copernicus_marine",
            "Copernicus Marine",
        ),
        LegacyDownloaderSpec(
            "water_level",
            "水位/water_level_data_module.py",
            "download_recent_water_level_data",
            "water_level",
            "copernicus_marine",
            "Copernicus Marine",
        ),
        LegacyDownloaderSpec(
            "wind_field",
            "获取风向/wind_field_data_module.py",
            "download_recent_wind_field_data",
            "wind_field",
            "noaa_gfs",
            "NOAA GFS/NOMADS",
        ),
        LegacyDownloaderSpec(
            "temperature",
            "获取温度/temperature_data_module.py",
            "download_recent_temperature_data",
            "temperature",
            "noaa_gfs",
            "NOAA GFS/NOMADS",
        ),
        LegacyDownloaderSpec(
            "visibility",
            "能见度/visibility_data_module.py",
            "download_recent_visibility_data",
            "visibility",
            "noaa_gfs",
            "NOAA GFS/NOMADS",
        ),
        LegacyDownloaderSpec(
            "bathymetry",
            "水深数据/bathymetry_data_module.py",
            "download_route_bathymetry_data",
            "bathymetry",
            "gebco",
            "GEBCO",
        ),
        LegacyDownloaderSpec(
            "long_term_restricted_area",
            "长期禁航区/long_term_restricted_areas_module.py",
            "download_long_term_restricted_areas",
            "long_term_restricted_area",
            "emodnet",
            "EMODnet Human Activities WFS",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class LegacyRunResult:
    downloader: str
    records: tuple[ManifestRecord, ...]
    captured_http_exchanges: int


class LegacyDownloaderRunner:
    """Adapter covering every downloader in the supplied ``获取数据`` directory."""

    def __init__(
        self,
        *,
        legacy_root: str | Path,
        data_root: str | Path,
        issue_time_resolver: SourceIssueTimeResolver | None = None,
    ) -> None:
        self.legacy_root = Path(legacy_root).resolve()
        self.publisher = AcquisitionPublisher(data_root)
        self.issue_time_resolver = issue_time_resolver or SourceIssueTimeResolver()

    def run(self, downloader: str) -> LegacyRunResult:
        spec = LEGACY_DOWNLOADERS[downloader]
        module_path = self.legacy_root / spec.module_relative_path
        module = _load_module(module_path, spec.name)
        with capture_http_metadata(module) as exchanges:
            result = getattr(module, spec.function_name)()
        observed_at = max(
            (exchange.observed_at for exchange in exchanges), default=datetime.now(UTC)
        )
        records: list[ManifestRecord] = []
        for route_id, payload in _iter_route_payloads(result):
            if isinstance(payload, xr.Dataset):
                valid_times = _valid_times_or_empty(payload)
                context = IssueTimeContext(
                    source_family=spec.source_family,
                    data_type=spec.data_type,
                    category=get_data_type_spec(spec.data_type).category,
                    source_label=spec.source_label,
                    valid_times=valid_times,
                    observed_at=observed_at,
                    dataset_attributes=dict(payload.attrs),
                    http_exchanges=tuple(exchanges),
                    product_id=_product_id(payload, module),
                    dataset_id=_dataset_id(payload, module),
                )
                evidence = self.issue_time_resolver.resolve(context)
                published = self.publisher.publish_dataset(
                    payload,
                    data_type=spec.data_type,
                    route_id=route_id,
                    source=spec.source_label,
                    version=_version(payload, module),
                    issue_evidence=evidence,
                    valid_time=evidence.issue_time if not valid_times else None,
                    metadata={
                        "legacy_downloader": spec.name,
                        "legacy_module": spec.module_relative_path,
                        "product_id": context.product_id,
                        "dataset_id": context.dataset_id,
                    },
                )
                records.extend(published.records)
            elif isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                valid_time = _geojson_valid_time(payload) or observed_at
                context = IssueTimeContext(
                    source_family=spec.source_family,
                    data_type=spec.data_type,
                    category=get_data_type_spec(spec.data_type).category,
                    source_label=spec.source_label,
                    valid_times=(valid_time,),
                    observed_at=observed_at,
                    dataset_attributes=dict(payload.get("metadata", {})),
                    http_exchanges=tuple(exchanges),
                )
                evidence = self.issue_time_resolver.resolve(context)
                published = self.publisher.publish_geojson(
                    payload,
                    route_id=route_id,
                    source=spec.source_label,
                    version=valid_time.strftime("%Y%m%dT%H%M%SZ"),
                    issue_evidence=evidence,
                    valid_time=valid_time,
                    metadata={
                        "legacy_downloader": spec.name,
                        "legacy_module": spec.module_relative_path,
                    },
                )
                records.extend(published.records)
            else:
                raise TypeError(f"旧下载器 {spec.name} 返回了不支持的对象: {type(payload)!r}")
        return LegacyRunResult(spec.name, tuple(records), len(exchanges))


def _load_module(path: Path, name: str) -> ModuleType:
    if not path.is_file():
        raise FileNotFoundError(path)
    import_spec = importlib.util.spec_from_file_location(f"arctic_legacy_{name}", path)
    if import_spec is None or import_spec.loader is None:
        raise ImportError(f"无法加载旧下载器: {path}")
    module = importlib.util.module_from_spec(import_spec)
    import_spec.loader.exec_module(module)
    return module


@contextmanager
def capture_http_metadata(module: ModuleType) -> Iterator[list[CapturedHttpExchange]]:
    requests_module = getattr(module, "requests", None)
    exchanges: list[CapturedHttpExchange] = []
    if requests_module is None:
        yield exchanges
        return
    originals: dict[str, Any] = {}

    def wrapper(method_name: str, original):
        def captured(url, *args, **kwargs):
            response = original(url, *args, **kwargs)
            exchanges.append(
                CapturedHttpExchange(
                    method=method_name.upper(),
                    request_url=str(url),
                    response_url=str(getattr(response, "url", url)),
                    request_params={
                        str(key): str(value)
                        for key, value in dict(kwargs.get("params") or {}).items()
                    },
                    response_headers={
                        str(key): str(value)
                        for key, value in dict(getattr(response, "headers", {})).items()
                    },
                    observed_at=datetime.now(UTC),
                    status_code=getattr(response, "status_code", None),
                )
            )
            return response

        return captured

    for method_name in ("get", "post", "head"):
        original = getattr(requests_module, method_name, None)
        if original is not None:
            originals[method_name] = original
            setattr(requests_module, method_name, wrapper(method_name, original))
    try:
        yield exchanges
    finally:
        for method_name, original in originals.items():
            setattr(requests_module, method_name, original)


def _iter_route_payloads(result: Any):
    batches = result if isinstance(result, tuple) else (result,)
    for batch in batches:
        if not isinstance(batch, Mapping):
            raise TypeError("旧下载接口应返回 route_id -> 数据对象字典")
        yield from batch.items()


def _valid_times_or_empty(dataset: xr.Dataset) -> tuple[datetime, ...]:
    try:
        return discover_valid_times(dataset)
    except ValueError:
        return ()


def _dataset_id(dataset: xr.Dataset, module: ModuleType) -> str | None:
    value = dataset.attrs.get("copernicus_dataset_id") or getattr(module, "DATASET_ID", None)
    return None if value is None else str(value)


def _product_id(dataset: xr.Dataset, module: ModuleType) -> str | None:
    value = dataset.attrs.get("copernicus_product")
    if value:
        return str(value)
    product_url = str(getattr(module, "PRODUCT_URL", ""))
    match = re.search(r"/product/([^/]+)/", product_url)
    return match.group(1) if match else None


def _version(dataset: xr.Dataset, module: ModuleType) -> str:
    for value in (
        dataset.attrs.get("dataset_version"),
        dataset.attrs.get("copernicus_dataset_label"),
        getattr(module, "DATASET_VERSION", None),
        dataset.attrs.get("copernicus_dataset_id"),
    ):
        if value:
            return str(value)
    return "legacy"


def _geojson_valid_time(payload: Mapping[str, Any]) -> datetime | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    for key in ("valid_time", "generated_at_utc", "generated_at"):
        value = metadata.get(key)
        if value:
            try:
                return to_utc_datetime(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError:
                continue
    return None
