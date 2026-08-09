"""Validated, atomic ingestion from upstream acquisition outputs into ``ready``."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xarray as xr

from arctic_route_data.errors import DataValidationError, MissingMetadataError
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import ManifestRecord, QualityFlag
from arctic_route_data.normalization import netcdf_encoding, normalize_dataset, spatial_metadata
from arctic_route_data.specs import get_data_type_spec
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _data_id(
    *, route_id: str, data_type: str, issue_time: datetime, valid_time: datetime, checksum: str
) -> str:
    identity = "|".join(
        (route_id, data_type, isoformat_utc(issue_time), isoformat_utc(valid_time), checksum)
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
        version: str = "1",
        quality_flag: QualityFlag = QualityFlag.GOOD,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ManifestRecord:
        input_path = Path(source_path).resolve()
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        issue_time = ensure_utc(issue_time, field="issue_time")
        valid_time = ensure_utc(valid_time, field="valid_time")
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
                "upstream_file": input_path.name,
                "source_family": spec.source_family,
                **(extra_metadata or {}),
            },
        )
        self.manifest.register(record)
        return record

    def ingest_geojson(
        self,
        source_path: str | Path,
        *,
        route_id: str,
        issue_time: datetime,
        valid_time: datetime,
        source: str,
        version: str = "1",
        quality_flag: QualityFlag = QualityFlag.GOOD,
        extra_metadata: dict[str, Any] | None = None,
    ) -> ManifestRecord:
        input_path = Path(source_path).resolve()
        try:
            data = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataValidationError(f"无法读取 GeoJSON: {input_path}") from exc
        if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            raise DataValidationError("限制区数据必须是 GeoJSON FeatureCollection")
        issue_time = ensure_utc(issue_time, field="issue_time")
        valid_time = ensure_utc(valid_time, field="valid_time")
        coordinates = list(_iter_coordinates(data))
        if coordinates:
            longitudes, latitudes = zip(*coordinates, strict=True)
            bbox = (min(longitudes), min(latitudes), max(longitudes), max(latitudes))
        else:
            bbox = (0.0, 0.0, 0.0, 0.0)
            quality_flag = QualityFlag.SUSPECT

        day_path = self.ready_root / route_id / "long_term_restricted_area"
        day_path.mkdir(parents=True, exist_ok=True)
        stem = f"restricted_{valid_time:%Y%m%dT%H%M%SZ}_{version}"
        temporary = day_path / f".{stem}.{os.getpid()}.part"
        temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        checksum = sha256_file(temporary)
        destination = day_path / f"{stem}_{checksum[:12]}.geojson"
        temporary.replace(destination)
        record = ManifestRecord(
            data_id=_data_id(
                route_id=route_id,
                data_type="long_term_restricted_area",
                issue_time=issue_time,
                valid_time=valid_time,
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
                "feature_count": len(data["features"]),
                "upstream_file": input_path.name,
                **(extra_metadata or {}),
            },
        )
        self.manifest.register(record)
        return record

    def ingest_sidecar(self, sidecar_path: str | Path) -> ManifestRecord:
        path = Path(sidecar_path)
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataValidationError(f"无法读取 sidecar: {path}") from exc
        required = {"file", "data_type", "route_id", "issue_time", "valid_time", "source"}
        missing = sorted(required - sidecar.keys())
        if missing:
            raise MissingMetadataError(f"sidecar 缺少字段: {', '.join(missing)}")
        payload_name = Path(sidecar["file"])
        payload = (path.parent / payload_name).resolve()
        if payload_name.is_absolute() or not payload.is_relative_to(path.parent.resolve()):
            raise DataValidationError("sidecar.file 必须是 sidecar 同目录内的相对路径")
        common = {
            "route_id": sidecar["route_id"],
            "issue_time": parse_utc(sidecar["issue_time"], field="issue_time"),
            "valid_time": parse_utc(sidecar["valid_time"], field="valid_time"),
            "source": sidecar["source"],
            "version": str(sidecar.get("version", "1")),
            "quality_flag": QualityFlag(sidecar.get("quality_flag", "good")),
            "extra_metadata": dict(sidecar.get("metadata", {})),
        }
        if sidecar["data_type"] == "long_term_restricted_area":
            return self.ingest_geojson(payload, **common)
        return self.ingest_netcdf(payload, data_type=sidecar["data_type"], **common)


def _iter_coordinates(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "coordinates":
                yield from _iter_coordinate_array(child)
            elif key != "bbox":
                yield from _iter_coordinates(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_coordinates(child)


def _iter_coordinate_array(value: Any):
    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], int | float)
        and isinstance(value[1], int | float)
    ):
        yield float(value[0]), float(value[1])
    elif isinstance(value, list):
        for child in value:
            yield from _iter_coordinate_array(child)
