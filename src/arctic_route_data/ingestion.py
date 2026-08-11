"""Validated, atomic ingestion from upstream acquisition outputs into ``ready``."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.errors import DataValidationError, MissingMetadataError
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import (
    QUALITY_RANK,
    ManifestRecord,
    QualityFlag,
    validate_identifier,
)
from arctic_route_data.normalization import netcdf_encoding, normalize_dataset, spatial_metadata
from arctic_route_data.specs import DATA_TYPE_SPECS, get_data_type_spec
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _data_id(
    *,
    route_id: str,
    data_type: str,
    issue_time: datetime,
    valid_time: datetime,
    source: str,
    version: str,
    checksum: str,
) -> str:
    identity = "|".join(
        (
            route_id,
            data_type,
            isoformat_utc(issue_time),
            isoformat_utc(valid_time),
            source,
            version,
            checksum,
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


class IngestionPipeline:
    def __init__(self, data_root: str | Path, manifest: ManifestStore | None = None) -> None:
        self.data_root = Path(data_root).resolve()
        self.ready_root = self.data_root / "ready"
        self.incoming_root = self.data_root / "incoming"
        self.quarantine_root = self.data_root / "quarantine"
        for directory in (self.ready_root, self.incoming_root, self.quarantine_root):
            directory.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest or ManifestStore(self.data_root / "manifest" / "manifest.sqlite3")

    def ingest_netcdf(
        self,
        source_path: str | Path,
        *,
        data_type: str,
        route_id: str,
        issue_time: datetime,
        valid_time: datetime,
        source: str,
        issue_time_evidence: IssueTimeEvidence | dict[str, Any] | None = None,
        version: str = "1",
        quality_flag: QualityFlag = QualityFlag.GOOD,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ManifestRecord:
        input_path = Path(source_path).resolve()
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        validate_identifier(route_id, field="route_id")
        validate_identifier(version, field="version")
        issue_time = ensure_utc(issue_time, field="issue_time")
        valid_time = ensure_utc(valid_time, field="valid_time")
        evidence = _validated_direct_evidence(
            issue_time_evidence,
            issue_time=issue_time,
            quality=quality_flag,
        )
        spec = get_data_type_spec(data_type)

        try:
            with xr.open_dataset(input_path) as opened:
                normalized = normalize_dataset(
                    opened.load(),
                    data_type=data_type,
                    valid_time=valid_time,
                    issue_time=issue_time,
                    route_id=route_id,
                    source=source,
                )
        except Exception as exc:
            raise DataValidationError(f"无法读取或规范化 {input_path}: {exc}") from exc

        content_quality, content_qc = _content_quality(normalized)
        quality_flag = _lower_quality(quality_flag, content_quality)

        day_path = self.ready_root / route_id / data_type / valid_time.strftime("%Y/%m/%d")
        day_path.mkdir(parents=True, exist_ok=True)
        stem = (
            f"{data_type}_{valid_time:%Y%m%dT%H%M%SZ}_"
            f"issued_{issue_time:%Y%m%dT%H%M%SZ}_{version}"
        )
        temporary = day_path / f".{stem}.{os.getpid()}.part"
        normalized.to_netcdf(temporary, engine="h5netcdf", encoding=netcdf_encoding(normalized))
        checksum = sha256_file(temporary)
        destination = day_path / f"{stem}_{checksum[:12]}.nc"
        if destination.exists() and sha256_file(destination) == checksum:
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(destination)

        bbox, resolution, crs = spatial_metadata(normalized)
        data_id = _data_id(
            route_id=route_id,
            data_type=data_type,
            issue_time=issue_time,
            valid_time=valid_time,
            source=source,
            version=version,
            checksum=checksum,
        )
        record = ManifestRecord(
            data_id=data_id,
            data_type=data_type,
            category=spec.category,
            route_id=route_id,
            variables=tuple(normalized.data_vars),
            issue_time=issue_time,
            valid_time=valid_time,
            ingest_time=datetime.now(UTC),
            bbox=bbox,
            crs=crs,
            resolution=resolution,
            source=source,
            quality_flag=quality_flag,
            version=version,
            checksum=checksum,
            relative_path=destination.relative_to(self.data_root).as_posix(),
            size_bytes=destination.stat().st_size,
            metadata={
                **(extra_metadata or {}),
                "upstream_file": input_path.name,
                "source_family": spec.source_family,
                "normalization": _normalization_metadata(normalized),
                "issue_time_evidence": evidence,
                "content_qc": content_qc,
            },
        )
        return self.manifest.register(record)

    def ingest_geojson(
        self,
        source_path: str | Path,
        *,
        route_id: str,
        issue_time: datetime,
        valid_time: datetime,
        source: str,
        issue_time_evidence: IssueTimeEvidence | dict[str, Any] | None = None,
        version: str = "1",
        quality_flag: QualityFlag = QualityFlag.GOOD,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ManifestRecord:
        input_path = Path(source_path).resolve()
        validate_identifier(route_id, field="route_id")
        validate_identifier(version, field="version")
        try:
            data = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataValidationError(f"无法读取 GeoJSON: {input_path}") from exc
        if (
            not isinstance(data, dict)
            or data.get("type") != "FeatureCollection"
            or not isinstance(data.get("features"), list)
        ):
            raise DataValidationError("限制区数据必须是 GeoJSON FeatureCollection")
        issue_time = ensure_utc(issue_time, field="issue_time")
        valid_time = ensure_utc(valid_time, field="valid_time")
        evidence = _validated_direct_evidence(
            issue_time_evidence,
            issue_time=issue_time,
            quality=quality_flag,
        )
        coordinates = _validate_geojson_features(data)
        if coordinates:
            longitudes, latitudes = zip(*coordinates, strict=True)
            bbox = (min(longitudes), min(latitudes), max(longitudes), max(latitudes))
        else:
            bbox = (0.0, 0.0, 0.0, 0.0)
            quality_flag = QualityFlag.SUSPECT
        constraint_summary = _constraint_summary(data)
        if constraint_summary["unknown_navigation_effect"]:
            quality_flag = _lower_quality(quality_flag, QualityFlag.SUSPECT)

        day_path = self.ready_root / route_id / "long_term_restricted_area"
        day_path.mkdir(parents=True, exist_ok=True)
        stem = (
            f"restricted_valid_{valid_time:%Y%m%dT%H%M%SZ}_"
            f"issued_{issue_time:%Y%m%dT%H%M%SZ}_{version}"
        )
        temporary = day_path / f".{stem}.{os.getpid()}.part"
        temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        checksum = sha256_file(temporary)
        destination = day_path / f"{stem}_{checksum[:12]}.geojson"
        if destination.exists() and sha256_file(destination) == checksum:
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(destination)
        record = ManifestRecord(
            data_id=_data_id(
                route_id=route_id,
                data_type="long_term_restricted_area",
                issue_time=issue_time,
                valid_time=valid_time,
                source=source,
                version=version,
                checksum=checksum,
            ),
            data_type="long_term_restricted_area",
            category=get_data_type_spec("long_term_restricted_area").category,
            route_id=route_id,
            variables=("restricted_area",),
            issue_time=issue_time,
            valid_time=valid_time,
            ingest_time=datetime.now(UTC),
            bbox=tuple(float(item) for item in bbox),
            crs="EPSG:4326",
            resolution=(None, None),
            source=source,
            quality_flag=quality_flag,
            version=version,
            checksum=checksum,
            relative_path=destination.relative_to(self.data_root).as_posix(),
            size_bytes=destination.stat().st_size,
            media_type="application/geo+json",
            metadata={
                **(extra_metadata or {}),
                "feature_count": len(data["features"]),
                "upstream_file": input_path.name,
                "issue_time_evidence": evidence,
                "constraint_summary": constraint_summary,
                "automatic_hard_mask_allowed": False,
            },
        )
        return self.manifest.register(record)

    def ingest_sidecar(self, sidecar_path: str | Path) -> ManifestRecord:
        path = Path(sidecar_path)
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataValidationError(f"无法读取 sidecar: {path}") from exc
        _validate_sidecar(sidecar)
        payload_name = Path(sidecar["file"])
        payload = (path.parent / payload_name).resolve()
        if payload_name.is_absolute() or not payload.is_relative_to(path.parent.resolve()):
            raise DataValidationError("sidecar.file 必须是 sidecar 同目录内的相对路径")
        if not payload.is_file():
            raise DataValidationError(f"sidecar payload 不存在: {payload}")
        if payload.stat().st_size != sidecar["payload_size_bytes"]:
            raise DataValidationError("sidecar payload_size_bytes 与实际文件不一致")
        if sha256_file(payload) != sidecar["payload_sha256"]:
            raise DataValidationError("sidecar payload_sha256 与实际文件不一致")
        metadata = dict(sidecar["metadata"])
        evidence = dict(metadata.pop("issue_time_evidence"))
        common = {
            "route_id": sidecar["route_id"],
            "issue_time": parse_utc(sidecar["issue_time"], field="issue_time"),
            "valid_time": parse_utc(sidecar["valid_time"], field="valid_time"),
            "source": sidecar["source"],
            "issue_time_evidence": evidence,
            "version": str(sidecar.get("version", "1")),
            "quality_flag": QualityFlag(sidecar.get("quality_flag", "good")),
            "extra_metadata": {
                **metadata,
                "publication_id": sidecar["publication_id"],
                "upstream_checksum": sidecar["payload_sha256"],
                "upstream_size_bytes": sidecar["payload_size_bytes"],
            },
        }
        if sidecar["data_type"] == "long_term_restricted_area":
            return self.ingest_geojson(payload, **common)
        return self.ingest_netcdf(payload, data_type=sidecar["data_type"], **common)


def _validate_sidecar(sidecar: Any) -> None:
    if not isinstance(sidecar, dict):
        raise DataValidationError("sidecar 顶层必须是 JSON object")
    required = {
        "file",
        "payload_sha256",
        "payload_size_bytes",
        "publication_id",
        "data_type",
        "route_id",
        "issue_time",
        "valid_time",
        "source",
        "version",
        "quality_flag",
        "metadata",
    }
    missing = sorted(required - sidecar.keys())
    if missing:
        raise MissingMetadataError(f"sidecar 缺少字段: {', '.join(missing)}")
    extra = sorted(sidecar.keys() - required)
    if extra:
        raise DataValidationError(f"sidecar 包含未声明字段: {', '.join(extra)}")
    if sidecar["data_type"] not in DATA_TYPE_SPECS:
        raise DataValidationError(f"sidecar.data_type 不受支持: {sidecar['data_type']!r}")
    for field in ("file", "route_id", "source", "version", "publication_id"):
        if not isinstance(sidecar[field], str) or not sidecar[field].strip():
            raise DataValidationError(f"sidecar.{field} 必须是非空字符串")
    validate_identifier(sidecar["route_id"], field="route_id")
    validate_identifier(sidecar["version"], field="version")
    validate_identifier(sidecar["publication_id"], field="publication_id")
    checksum = sidecar["payload_sha256"]
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
    ):
        raise DataValidationError("sidecar.payload_sha256 必须是小写 SHA-256")
    if (
        not isinstance(sidecar["payload_size_bytes"], int)
        or isinstance(sidecar["payload_size_bytes"], bool)
        or sidecar["payload_size_bytes"] < 0
    ):
        raise DataValidationError("sidecar.payload_size_bytes 必须是非负整数")
    for field in ("issue_time", "valid_time"):
        if not isinstance(sidecar[field], str):
            raise DataValidationError(f"sidecar.{field} 必须是 ISO-8601 字符串")
    try:
        quality = QualityFlag(sidecar["quality_flag"])
    except (TypeError, ValueError) as exc:
        raise DataValidationError("sidecar.quality_flag 无效") from exc
    if quality is QualityFlag.MISSING:
        raise DataValidationError("实际 payload 不能标记为 missing")
    issue_time = parse_utc(sidecar["issue_time"], field="issue_time")
    valid_time = parse_utc(sidecar["valid_time"], field="valid_time")
    metadata = sidecar["metadata"]
    if not isinstance(metadata, dict):
        raise DataValidationError("sidecar.metadata 必须是 object")
    evidence = metadata.get("issue_time_evidence")
    _validate_issue_time_evidence(evidence, issue_time=issue_time, quality=quality)
    reference = metadata.get("forecast_reference_time")
    lead = metadata.get("forecast_lead_hours")
    if (reference is None) != (lead is None):
        raise DataValidationError(
            "forecast_reference_time 与 forecast_lead_hours 必须同时出现或同时缺失"
        )
    if reference is not None:
        reference_time = parse_utc(reference, field="forecast_reference_time")
        if not isinstance(lead, int | float) or not np.isfinite(lead) or lead < 0:
            raise DataValidationError("forecast_lead_hours 必须是非负有限数")
        expected = reference_time + timedelta(hours=float(lead))
        if abs((expected - valid_time).total_seconds()) > 1.0:
            raise DataValidationError(
                "valid_time 必须等于 forecast_reference_time + forecast_lead_hours"
            )


def _validate_issue_time_evidence(
    evidence: Any, *, issue_time: datetime, quality: QualityFlag
) -> None:
    if not isinstance(evidence, dict):
        raise MissingMetadataError("sidecar.metadata.issue_time_evidence 必须存在")
    required = {
        "issue_time",
        "method",
        "authority",
        "reference",
        "observed_at",
        "raw_value",
        "authoritative",
    }
    if set(evidence) != required:
        missing = sorted(required - evidence.keys())
        extra = sorted(evidence.keys() - required)
        details = []
        if missing:
            details.append("缺少 " + ", ".join(missing))
        if extra:
            details.append("多出 " + ", ".join(extra))
        raise DataValidationError("issue_time_evidence 结构无效: " + "; ".join(details))
    try:
        method = IssueTimeMethod(evidence["method"])
    except (TypeError, ValueError) as exc:
        raise DataValidationError("issue_time_evidence.method 无效") from exc
    evidence_time = parse_utc(evidence["issue_time"], field="evidence.issue_time")
    observed_at = parse_utc(evidence["observed_at"], field="evidence.observed_at")
    if evidence_time != issue_time:
        raise DataValidationError("sidecar.issue_time 与证据中的 issue_time 不一致")
    if evidence_time > observed_at + timedelta(minutes=10):
        raise DataValidationError("issue_time 不能明显晚于证据观测时刻")
    for field in ("authority", "reference", "raw_value"):
        if not isinstance(evidence[field], str) or (field != "raw_value" and not evidence[field]):
            raise DataValidationError(f"issue_time_evidence.{field} 必须是有效字符串")
    if not isinstance(evidence["authoritative"], bool):
        raise DataValidationError("issue_time_evidence.authoritative 必须是 boolean")
    if method in {
        IssueTimeMethod.COPERNICUS_SERVICE_SYNC,
        IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
    } and evidence["authoritative"]:
        raise DataValidationError(f"{method.value} 不能标记为 authoritative")
    if quality is QualityFlag.GOOD and not evidence["authoritative"]:
        raise DataValidationError("非权威 issue_time 证据不能把 payload 标记为 good")


def _validated_direct_evidence(
    evidence: IssueTimeEvidence | dict[str, Any] | None,
    *,
    issue_time: datetime,
    quality: QualityFlag,
) -> dict[str, Any]:
    if isinstance(evidence, IssueTimeEvidence):
        value = evidence.to_dict()
    elif isinstance(evidence, dict):
        value = dict(evidence)
    else:
        raise MissingMetadataError(
            "所有摄取入口都必须提供 issue_time_evidence；请使用 AcquisitionPublisher "
            "或显式传入证据"
        )
    _validate_issue_time_evidence(value, issue_time=issue_time, quality=quality)
    return value


def _lower_quality(left: QualityFlag, right: QualityFlag) -> QualityFlag:
    return left if QUALITY_RANK[left] <= QUALITY_RANK[right] else right


def _content_quality(dataset: xr.Dataset) -> tuple[QualityFlag, dict[str, Any]]:
    try:
        fractions = {
            str(key): float(value)
            for key, value in json.loads(
                str(dataset.attrs.get("qc_missing_fraction", "{}"))
            ).items()
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataValidationError("规范化结果缺少可解析的 content QC") from exc
    maximum = max(fractions.values(), default=0.0)
    if maximum > 0.8:
        raise DataValidationError(f"有效空间覆盖不足 20%；最大缺测比例={maximum:.3f}")
    if maximum == 0:
        quality = QualityFlag.GOOD
    elif maximum <= 0.2:
        quality = QualityFlag.SUSPECT
    else:
        quality = QualityFlag.DEGRADED
    return quality, {
        "status": quality.value,
        "missing_fraction": fractions,
        "maximum_missing_fraction": maximum,
        "ruleset": "a.content-qc.v1",
    }


def _constraint_summary(geojson: dict[str, Any]) -> dict[str, Any]:
    allowed_effects = {"hard", "soft", "information"}
    counts = {effect: 0 for effect in (*sorted(allowed_effects), "unknown")}
    missing_authority = 0
    for feature in geojson.get("features", []):
        properties = feature.get("properties", {}) if isinstance(feature, dict) else {}
        if not isinstance(properties, dict):
            properties = {}
        effect = str(properties.get("navigation_effect", "unknown")).casefold()
        counts[effect if effect in allowed_effects else "unknown"] += 1
        if not str(properties.get("authority", "")).strip():
            missing_authority += 1
    return {
        "navigation_effect_counts": counts,
        "unknown_navigation_effect": counts["unknown"],
        "missing_authority": missing_authority,
        "policy_note": (
            "A 只发布来源分类；B/C 必须由场景政策决定 hard/soft，禁止按图层名称自动硬屏蔽"
        ),
    }


def _validate_geojson_features(geojson: dict[str, Any]) -> list[tuple[float, float]]:
    coordinates: list[tuple[float, float]] = []
    for index, feature in enumerate(geojson["features"]):
        path = f"features[{index}]"
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise DataValidationError(f"{path} 必须是 GeoJSON Feature")
        properties = feature.get("properties", {})
        if properties is not None and not isinstance(properties, dict):
            raise DataValidationError(f"{path}.properties 必须是 object 或 null")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise DataValidationError(f"{path}.geometry 必须是非空 GeoJSON geometry")
        coordinates.extend(_validate_geojson_geometry(geometry, f"{path}.geometry"))
    return coordinates


def _validate_geojson_geometry(
    geometry: dict[str, Any], path: str
) -> list[tuple[float, float]]:
    geometry_type = geometry.get("type")
    if geometry_type == "GeometryCollection":
        children = geometry.get("geometries")
        if not isinstance(children, list) or not children:
            raise DataValidationError(f"{path}.geometries 必须是非空数组")
        result: list[tuple[float, float]] = []
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                raise DataValidationError(f"{path}.geometries[{index}] 必须是 geometry")
            result.extend(
                _validate_geojson_geometry(child, f"{path}.geometries[{index}]")
            )
        return result

    depth_by_type = {
        "Point": 0,
        "MultiPoint": 1,
        "LineString": 1,
        "MultiLineString": 2,
        "Polygon": 2,
        "MultiPolygon": 3,
    }
    if geometry_type not in depth_by_type:
        raise DataValidationError(f"{path}.type 是不支持的 GeoJSON geometry 类型")
    if "coordinates" not in geometry:
        raise DataValidationError(f"{path}.coordinates 缺失")
    coordinates = geometry["coordinates"]
    result = _validate_coordinate_nesting(
        coordinates,
        depth=depth_by_type[geometry_type],
        path=f"{path}.coordinates",
    )
    _validate_geojson_coordinate_shape(
        geometry_type, coordinates, f"{path}.coordinates"
    )
    return result


def _validate_geojson_coordinate_shape(
    geometry_type: str, value: Any, path: str
) -> None:
    if geometry_type == "LineString" and len(value) < 2:
        raise DataValidationError(f"{path} 的 LineString 至少需要两个位置")
    if geometry_type == "MultiLineString":
        for index, line in enumerate(value):
            if len(line) < 2:
                raise DataValidationError(
                    f"{path}[{index}] 的 LineString 至少需要两个位置"
                )
    polygons = (
        [value]
        if geometry_type == "Polygon"
        else value
        if geometry_type == "MultiPolygon"
        else []
    )
    for polygon_index, polygon in enumerate(polygons):
        for ring_index, ring in enumerate(polygon):
            ring_path = f"{path}[{ring_index}]"
            if geometry_type == "MultiPolygon":
                ring_path = f"{path}[{polygon_index}][{ring_index}]"
            if len(ring) < 4:
                raise DataValidationError(f"{ring_path} 的线性环至少需要四个位置")
            if tuple(float(item) for item in ring[0][:2]) != tuple(
                float(item) for item in ring[-1][:2]
            ):
                raise DataValidationError(f"{ring_path} 的首尾位置必须闭合")


def _validate_coordinate_nesting(
    value: Any, *, depth: int, path: str
) -> list[tuple[float, float]]:
    if depth:
        if not isinstance(value, list) or not value:
            raise DataValidationError(f"{path} 必须是非空坐标数组")
        result: list[tuple[float, float]] = []
        for index, child in enumerate(value):
            result.extend(
                _validate_coordinate_nesting(
                    child,
                    depth=depth - 1,
                    path=f"{path}[{index}]",
                )
            )
        return result
    if (
        not isinstance(value, list)
        or len(value) < 2
        or any(
            not isinstance(item, int | float) or isinstance(item, bool)
            for item in value
        )
    ):
        raise DataValidationError(f"{path} 必须是至少含经纬度、且全部为数值的位置")
    if any(not np.isfinite(float(item)) for item in value):
        raise DataValidationError(f"{path} 的所有坐标分量必须是有限数")
    longitude = float(value[0])
    latitude = float(value[1])
    if not -180.0 <= longitude <= 180.0:
        raise DataValidationError(f"{path} 经度必须位于 [-180, 180]")
    if not -90.0 <= latitude <= 90.0:
        raise DataValidationError(f"{path} 纬度必须位于 [-90, 90]")
    return [(longitude, latitude)]


def _normalization_metadata(dataset: xr.Dataset) -> dict[str, Any]:
    variables: dict[str, dict[str, str]] = {}
    for name, array in dataset.data_vars.items():
        details = {
            key: str(array.attrs[key])
            for key in (
                "source_variable",
                "source_standard_name",
                "source_units",
                "standard_name",
                "units",
                "vector_reference_frame",
            )
            if key in array.attrs
        }
        if details:
            variables[name] = details
    try:
        missing_fraction = json.loads(str(dataset.attrs.get("qc_missing_fraction", "{}")))
    except json.JSONDecodeError:
        missing_fraction = {}
    result: dict[str, Any] = {
        "schema_version": "a.normalization.v1",
        "normalizer_version": str(dataset.attrs.get("normalizer_version", "unknown")),
        "coordinate_crs": str(dataset.attrs.get("coordinate_crs", "unknown")),
        "source_grid_crs": str(dataset.attrs.get("source_grid_crs", "unknown")),
        "grid_topology": str(dataset.attrs.get("grid_topology", "unknown")),
        "grid_id": str(dataset.attrs.get("grid_id", "unknown")),
        "coordinate_digest": str(dataset.attrs.get("coordinate_digest", "unknown")),
        "longitude_wrap": str(dataset.attrs.get("longitude_wrap", "unknown")),
        "missing_fraction": missing_fraction,
        "variables": variables,
    }
    try:
        result["source_grid_mapping"] = json.loads(
            str(dataset.attrs.get("source_grid_mapping", "{}"))
        )
    except json.JSONDecodeError:
        result["source_grid_mapping"] = {}
    for key in (
        "vector_source_reference_frame",
        "vector_reference_frame",
        "vector_rotation",
        "vector_projection_central_meridian_deg",
    ):
        if key in dataset.attrs:
            result[key] = dataset.attrs[key]
    dataset_id = str(dataset.attrs.get("copernicus_dataset_id", ""))
    if "detided" in dataset_id.casefold():
        result["current_component"] = "detided"
        result["tide_included"] = False
    return result
