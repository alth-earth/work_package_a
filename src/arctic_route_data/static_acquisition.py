"""Native acquisition for versioned static research layers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
import xarray as xr

from arctic_route_data.derivations import derive_land_sea_mask
from arctic_route_data.forecast_acquisition import (
    AcquisitionMode,
    Bounds,
    ForecastAcquisitionResult,
)
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.timeutils import ensure_utc, isoformat_utc

GEBCO_2026_DAP = (
    "https://dap.ceda.ac.uk/thredds/dodsC/bodc/gebco/global/gebco_2026/"
    "ice_surface_elevation/netcdf/GEBCO_2026.nc"
)
GEBCO_2026_DOI = "https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa"
GEBCO_2026_PUBLISHED = datetime(2026, 4, 23, tzinfo=UTC)
EMODNET_HUMAN_ACTIVITIES_WFS = "https://ows.emodnet-humanactivities.eu/wfs"
EMODNET_SERVICE_DOCUMENTATION = (
    "https://emodnet.ec.europa.eu/en/emodnet-web-service-documentation"
)
_GEBCO_STEP = 1.0 / 240.0
_GEBCO_LAT_ORIGIN = -90.0 + _GEBCO_STEP / 2.0
_GEBCO_LON_ORIGIN = -180.0 + _GEBCO_STEP / 2.0

_EMODNET_RESTRICTION_LAYERS: dict[str, dict[str, Any]] = {
    "marineprotectedareas": {
        "restriction_category": "marine_protected_area",
        "authority_fields": ("mang_auth",),
        "effective_from_fields": (),
        "effective_to_fields": (),
    },
    "militaryareaspoly": {
        "restriction_category": "military_area",
        "authority_fields": (),
        "effective_from_fields": (),
        "effective_to_fields": (),
    },
    "mspspatialplan": {
        "restriction_category": "maritime_spatial_plan",
        "authority_fields": ("offsource",),
        "effective_from_fields": ("validfrom",),
        "effective_to_fields": ("validto",),
    },
    "natura2000areas": {
        "restriction_category": "natura_2000_site",
        "authority_fields": (),
        "effective_from_fields": (),
        "effective_to_fields": (),
    },
}


class StaticLayerAcquirer:
    def __init__(
        self,
        data_root: str | Path,
        *,
        request_timeout_seconds: int = 120,
        http_session: requests.Session | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.publisher = AcquisitionPublisher(self.data_root)
        self.request_timeout_seconds = request_timeout_seconds
        self.http = http_session or requests.Session()

    def acquire_gebco(
        self,
        *,
        route_id: str,
        bounds: Bounds,
        data_types: tuple[str, ...] = ("bathymetry", "land_sea_mask"),
        resolution_degrees: float = 0.05,
        mode: AcquisitionMode | str = AcquisitionMode.FROZEN_FORECAST,
    ) -> ForecastAcquisitionResult:
        requested = tuple(dict.fromkeys(data_types))
        unsupported = sorted(set(requested) - {"bathymetry", "land_sea_mask"})
        if not requested:
            raise ValueError("GEBCO data_types 不能为空")
        if unsupported:
            raise ValueError("GEBCO 不支持这些 data_type: " + ", ".join(unsupported))
        if not math.isfinite(resolution_degrees) or resolution_degrees < _GEBCO_STEP:
            raise ValueError("GEBCO resolution_degrees 必须至少为 15 arc-second")
        acquisition_mode = AcquisitionMode(mode)
        query = _gebco_ascii_query(bounds, resolution_degrees)
        response = self.http.get(query, timeout=self.request_timeout_seconds)
        response.raise_for_status()
        dataset = _parse_gebco_ascii(response.text)
        dataset["elevation"].attrs = {
            "units": "m",
            "standard_name": "height_above_mean_sea_level",
            "positive": "up",
        }
        dataset.attrs.update(
            {
                "product_id": "GEBCO_2026",
                "source_uri": GEBCO_2026_DOI,
                "source_resolution": "15 arc-second",
                "selected_resolution_degrees": resolution_degrees,
            }
        )
        checksum = hashlib.sha256(response.content).hexdigest()
        signature = hashlib.sha256(query.encode()).hexdigest()[:12]
        snapshot_id = f"gebco-2026-{signature}-{checksum[:8]}"
        snapshot_dir = self.data_root / "source_snapshots" / "gebco" / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        source_path = snapshot_dir / "opendap-ascii.txt"
        _atomic_bytes(source_path, response.content)
        evidence_path = snapshot_dir / "request.metadata.json"
        _atomic_json(
            evidence_path,
            {
                "source_url": query,
                "checksum": checksum,
                "retrieved_at": isoformat_utc(datetime.now(UTC)),
                "product_version": "GEBCO_2026",
                "doi": GEBCO_2026_DOI,
            },
        )
        evidence = IssueTimeEvidence(
            issue_time=GEBCO_2026_PUBLISHED,
            method=IssueTimeMethod.EXPLICIT_CATALOG,
            authority="GEBCO/BODC",
            reference=GEBCO_2026_DOI,
            observed_at=datetime.now(UTC),
            raw_value=isoformat_utc(GEBCO_2026_PUBLISHED),
            authoritative=True,
        )
        common_metadata = {
            "product_kind": "static_research_layer",
            "acquisition_mode": acquisition_mode.value,
            "source_fidelity": "versioned_static_release",
            "source_snapshot_id": snapshot_id,
            "product_id": "GEBCO_2026",
            "source_uri": GEBCO_2026_DOI,
            "source_file": source_path.name,
            "source_file_checksum": sha256_file(source_path),
            "source_snapshot_relative_path": source_path.relative_to(
                self.data_root
            ).as_posix(),
            "selected_resolution_degrees": resolution_degrees,
            "navigation_use": "research_only_not_for_navigation",
        }
        records = []
        if "bathymetry" in requested:
            published = self.publisher.publish_dataset(
                dataset[["elevation"]],
                data_type="bathymetry",
                route_id=route_id,
                source="GEBCO_2026/CEDA OPeNDAP",
                version=snapshot_id,
                issue_evidence=evidence,
                valid_time=GEBCO_2026_PUBLISHED,
                metadata={
                    **common_metadata,
                    "constraint_role": "static_research_layer_not_core_hard_constraint",
                },
            )
            records.extend(published.records)
        if "land_sea_mask" in requested:
            mask_dataset = derive_land_sea_mask(dataset)[["land_sea_mask"]]
            published = self.publisher.publish_dataset(
                mask_dataset,
                data_type="land_sea_mask",
                route_id=route_id,
                source="GEBCO_2026/CEDA OPeNDAP",
                version=snapshot_id,
                issue_evidence=evidence,
                valid_time=GEBCO_2026_PUBLISHED,
                metadata={
                    **common_metadata,
                    "derivation_method": "elevation_below_mean_sea_level_v1",
                    "hard_mask_semantics": "none",
                },
            )
            records.extend(published.records)
        return ForecastAcquisitionResult(
            source="GEBCO_2026/CEDA OPeNDAP",
            route_id=route_id,
            source_snapshot_ids=(snapshot_id,),
            records=tuple(records),
        )

    def acquire_emodnet_restrictions(
        self,
        *,
        route_id: str,
        bounds: Bounds,
        valid_time: datetime,
        mode: AcquisitionMode | str = AcquisitionMode.FROZEN_FORECAST,
    ) -> ForecastAcquisitionResult:
        """Acquire four legally distinct EMODnet evidence layers without merging meaning."""

        acquisition_mode = AcquisitionMode(mode)
        valid = ensure_utc(valid_time, field="valid_time")
        retrieved_at = datetime.now(UTC)
        combined_features: list[dict[str, Any]] = []
        raw_by_layer: dict[str, bytes] = {}
        request_urls: dict[str, str] = {}
        layer_counts: dict[str, int] = {}
        for layer_name, semantics in _EMODNET_RESTRICTION_LAYERS.items():
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"emodnet:{layer_name}",
                "bbox": (
                    f"{bounds.west},{bounds.south},{bounds.east},{bounds.north},"
                    "EPSG:4326"
                ),
                "outputFormat": "application/json",
            }
            response = self.http.get(
                EMODNET_HUMAN_ACTIVITIES_WFS,
                params=params,
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            raw = bytes(response.content)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"EMODnet {layer_name} 未返回有效 GeoJSON") from exc
            if (
                not isinstance(payload, dict)
                or payload.get("type") != "FeatureCollection"
                or not isinstance(payload.get("features"), list)
            ):
                raise ValueError(f"EMODnet {layer_name} 不是 GeoJSON FeatureCollection")
            raw_by_layer[layer_name] = raw
            request_urls[layer_name] = str(getattr(response, "url", "")) or (
                f"{EMODNET_HUMAN_ACTIVITIES_WFS}?typeNames=emodnet:{layer_name}"
            )
            layer_counts[layer_name] = len(payload["features"])
            for feature in payload["features"]:
                combined_features.append(
                    _annotate_emodnet_feature(
                        feature,
                        layer_name=layer_name,
                        semantics=semantics,
                    )
                )
        if not combined_features:
            raise ValueError("EMODnet 所有法律类别均为空；拒绝发布空限制区快照")

        source_checksums = {
            layer: hashlib.sha256(raw).hexdigest()
            for layer, raw in sorted(raw_by_layer.items())
        }
        identity = {
            "bbox": [bounds.west, bounds.south, bounds.east, bounds.north],
            "checksums": source_checksums,
            "layers": sorted(raw_by_layer),
        }
        content_digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        snapshot_id = (
            f"emodnet-ha-{retrieved_at:%Y%m%dT%H%M%S%fZ}-{content_digest[:12]}"
        )
        snapshot_dir = self.data_root / "source_snapshots" / "emodnet" / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        for layer_name, raw in raw_by_layer.items():
            _atomic_bytes(snapshot_dir / f"{layer_name}.geojson", raw)

        combined = {
            "type": "FeatureCollection",
            "features": combined_features,
        }
        combined_bytes = json.dumps(
            combined,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        combined_path = snapshot_dir / "restrictions.annotated.geojson"
        _atomic_bytes(combined_path, combined_bytes)
        _atomic_json(
            snapshot_dir / "request.metadata.json",
            {
                "service": EMODNET_HUMAN_ACTIVITIES_WFS,
                "documentation": EMODNET_SERVICE_DOCUMENTATION,
                "retrieved_at": isoformat_utc(retrieved_at),
                "bbox": [bounds.west, bounds.south, bounds.east, bounds.north],
                "request_urls": request_urls,
                "source_checksums": source_checksums,
                "layer_counts": layer_counts,
                "combined_checksum": hashlib.sha256(combined_bytes).hexdigest(),
            },
        )
        evidence = IssueTimeEvidence(
            issue_time=retrieved_at,
            method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
            authority="EMODnet Human Activities WFS",
            reference=EMODNET_SERVICE_DOCUMENTATION,
            observed_at=retrieved_at,
            raw_value=isoformat_utc(retrieved_at),
            authoritative=False,
        )
        published = self.publisher.publish_geojson(
            combined,
            route_id=route_id,
            source="EMODnet Human Activities WFS",
            version=snapshot_id,
            issue_evidence=evidence,
            valid_time=valid,
            metadata={
                "product_kind": "versioned_policy_evidence_snapshot",
                "acquisition_mode": acquisition_mode.value,
                "source_fidelity": "current_catalog_snapshot",
                "temporal_fidelity": (
                    "current_catalog_not_historical_reconstruction"
                    if acquisition_mode
                    is AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE
                    else "catalog_snapshot_at_retrieval"
                ),
                "source_snapshot_id": snapshot_id,
                "source_uri": EMODNET_SERVICE_DOCUMENTATION,
                "source_file": combined_path.name,
                "source_file_checksum": sha256_file(combined_path),
                "source_snapshot_relative_path": combined_path.relative_to(
                    self.data_root
                ).as_posix(),
                "source_layers": sorted(raw_by_layer),
                "source_layer_counts": layer_counts,
                "legal_review_status": "unverified_information_only",
                "default_navigation_effect": "information",
                "automatic_hard_mask_allowed": False,
                "hard_mask_semantics": "none",
            },
        )
        return ForecastAcquisitionResult(
            source="EMODnet Human Activities WFS",
            route_id=route_id,
            source_snapshot_ids=(snapshot_id,),
            records=published.records,
            warnings=(
                "EMODnet 图层仅是分类法律/规划证据，未经人工法务复核；"
                "A 不将其转为 hard_mask",
            ),
        )


def _gebco_ascii_query(bounds: Bounds, resolution_degrees: float) -> str:
    stride = max(1, round(resolution_degrees / _GEBCO_STEP))
    lat_start = max(0, math.floor((bounds.south - _GEBCO_LAT_ORIGIN) / _GEBCO_STEP))
    lat_stop = min(43199, math.ceil((bounds.north - _GEBCO_LAT_ORIGIN) / _GEBCO_STEP))
    lon_start = max(0, math.floor((bounds.west - _GEBCO_LON_ORIGIN) / _GEBCO_STEP))
    lon_stop = min(86399, math.ceil((bounds.east - _GEBCO_LON_ORIGIN) / _GEBCO_STEP))
    lat_stop = min(43199, lat_start + math.ceil((lat_stop - lat_start) / stride) * stride)
    lon_stop = min(86399, lon_start + math.ceil((lon_stop - lon_start) / stride) * stride)
    constraint = (
        f"elevation[{lat_start}:{stride}:{lat_stop}][{lon_start}:{stride}:{lon_stop}],"
        f"lat[{lat_start}:{stride}:{lat_stop}],lon[{lon_start}:{stride}:{lon_stop}]"
    )
    return f"{GEBCO_2026_DAP}.ascii?{constraint}"


def _first_nonempty(
    properties: Mapping[str, Any], fields: tuple[str, ...]
) -> str | None:
    for field in fields:
        value = properties.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _annotate_emodnet_feature(
    feature: object,
    *,
    layer_name: str,
    semantics: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError(f"EMODnet {layer_name} 包含非 Feature 对象")
    raw_properties = feature.get("properties")
    if raw_properties is None:
        properties: dict[str, Any] = {}
    elif isinstance(raw_properties, dict):
        properties = dict(raw_properties)
    else:
        raise ValueError(f"EMODnet {layer_name} Feature.properties 不是 object")
    properties.update(
        {
            "source_layer": f"emodnet:{layer_name}",
            "restriction_category": semantics["restriction_category"],
            "authority": _first_nonempty(
                properties,
                tuple(semantics["authority_fields"]),
            ),
            "effective_from": _first_nonempty(
                properties,
                tuple(semantics["effective_from_fields"]),
            ),
            "effective_to": _first_nonempty(
                properties,
                tuple(semantics["effective_to_fields"]),
            ),
            "navigation_effect": "information",
            "automatic_hard_mask_allowed": False,
        }
    )
    annotated = dict(feature)
    annotated["properties"] = properties
    return annotated


def _parse_gebco_ascii(text: str) -> xr.Dataset:
    def vector(name: str) -> np.ndarray:
        match = re.search(rf"(?m)^{name}\[\d+\]\n([^\n]+)", text)
        if match is None:
            raise ValueError(f"GEBCO OPeNDAP 响应缺少 {name}")
        return np.asarray([float(item.strip()) for item in match.group(1).split(",")])

    longitude = vector("lon")
    latitude = vector("lat")
    match = re.search(
        r"(?ms)^elevation\.elevation\[\d+\]\[\d+\]\n(.*?)\n\nelevation\.lat",
        text,
    )
    if match is None:
        raise ValueError("GEBCO OPeNDAP 响应缺少 elevation 网格")
    rows = []
    for line in match.group(1).splitlines():
        _, separator, values = line.partition(",")
        if not separator:
            raise ValueError("GEBCO elevation 行格式无效")
        rows.append([float(item.strip()) for item in values.split(",")])
    elevation = np.asarray(rows, dtype=np.float32)
    if elevation.shape != (latitude.size, longitude.size):
        raise ValueError(
            f"GEBCO elevation 形状 {elevation.shape} 与坐标不匹配 "
            f"{(latitude.size, longitude.size)}"
        )
    return xr.Dataset(
        {"elevation": (("latitude", "longitude"), elevation)},
        coords={"latitude": latitude, "longitude": longitude},
    )


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    temporary.write_bytes(value)
    temporary.replace(path)


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)
