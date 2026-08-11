"""Publish downloader results as atomic frame files plus sidecars, then ingest them into A."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.folder_watch import FolderWatchSource
from arctic_route_data.issue_time import IssueTimeEvidence
from arctic_route_data.models import QUALITY_RANK, ManifestRecord, QualityFlag
from arctic_route_data.temporal_split import TemporalSlice, split_dataset_by_valid_time
from arctic_route_data.timeutils import ensure_utc, isoformat_utc


@dataclass(frozen=True, slots=True)
class PublishResult:
    records: tuple[ManifestRecord, ...]
    sidecars_written: int


@dataclass(frozen=True, slots=True)
class _ExpectedPublication:
    sidecar_name: str
    payload_name: str
    publication_id: str


class AcquisitionPublisher:
    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root).resolve()
        self.folder_source = FolderWatchSource(self.data_root)
        self.incoming = self.data_root / "incoming"

    def publish_netcdf_path(
        self,
        path: str | Path,
        **kwargs,
    ) -> PublishResult:
        with xr.open_dataset(path) as opened:
            return self.publish_dataset(opened.load(), **kwargs)

    def publish_dataset(
        self,
        dataset: xr.Dataset,
        *,
        data_type: str,
        route_id: str,
        source: str,
        version: str,
        issue_evidence: IssueTimeEvidence,
        valid_time: datetime | None = None,
        quality_flag: QualityFlag | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublishResult:
        prepared = dataset
        if valid_time is not None and not any(
            name in prepared.variables for name in ("time", "valid_time", "forecast_time")
        ):
            prepared = prepared.expand_dims(
                time=[ensure_utc(valid_time, field="valid_time").replace(tzinfo=None)]
            )
        temporal_slices = split_dataset_by_valid_time(prepared)
        expected: list[_ExpectedPublication] = []
        for temporal_slice in temporal_slices:
            expected.append(
                self._write_frame_pair(
                    temporal_slice,
                    data_type=data_type,
                    route_id=route_id,
                    source=source,
                    version=version,
                    issue_evidence=issue_evidence,
                    quality_flag=quality_flag,
                    metadata=metadata,
                )
            )
        return self._scan_expected(expected)

    def publish_geojson(
        self,
        geojson: dict[str, Any],
        *,
        route_id: str,
        source: str,
        version: str,
        issue_evidence: IssueTimeEvidence,
        valid_time: datetime | None = None,
        quality_flag: QualityFlag | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublishResult:
        if geojson.get("type") != "FeatureCollection":
            raise DataValidationError("限制区下载结果必须是 GeoJSON FeatureCollection")
        valid = ensure_utc(valid_time or issue_evidence.issue_time, field="valid_time")
        payload_text = json.dumps(geojson, ensure_ascii=False, sort_keys=True)
        payload_bytes = payload_text.encode()
        payload_checksum = hashlib.sha256(payload_bytes).hexdigest()
        publication_id = self._publication_id(
            data_type="long_term_restricted_area",
            route_id=route_id,
            valid_time=valid,
            source=source,
            version=version,
            issue_evidence=issue_evidence,
            metadata=metadata,
            payload_checksum=payload_checksum,
        )
        stem = self._stem(
            "long_term_restricted_area",
            route_id,
            valid,
            issue_evidence.issue_time,
            version,
            publication_id,
        )
        payload_name = f"{stem}.geojson"
        sidecar_name = f"{stem}.metadata.json"
        payload = self.incoming / payload_name
        self._atomic_text(payload, payload_text)
        sidecar = {
            "file": payload_name,
            "payload_sha256": payload_checksum,
            "payload_size_bytes": len(payload_bytes),
            "publication_id": publication_id,
            "data_type": "long_term_restricted_area",
            "route_id": route_id,
            "issue_time": isoformat_utc(issue_evidence.issue_time),
            "valid_time": isoformat_utc(valid),
            "source": source,
            "version": version,
            "quality_flag": _publication_quality(issue_evidence, quality_flag).value,
            "metadata": {
                **(metadata or {}),
                "issue_time_evidence": issue_evidence.to_dict(),
            },
        }
        self._atomic_text(
            self.incoming / sidecar_name,
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return self._scan_expected(
            [_ExpectedPublication(sidecar_name, payload_name, publication_id)]
        )

    def _write_frame_pair(
        self,
        temporal_slice: TemporalSlice,
        *,
        data_type: str,
        route_id: str,
        source: str,
        version: str,
        issue_evidence: IssueTimeEvidence,
        quality_flag: QualityFlag | None,
        metadata: dict[str, Any] | None,
    ) -> _ExpectedPublication:
        temporary = self.incoming / f".publish-{uuid.uuid4().hex}.nc.part"
        temporal_slice.dataset.to_netcdf(temporary, engine="h5netcdf")
        payload_checksum = _sha256(temporary)
        payload_size = temporary.stat().st_size
        publication_id = self._publication_id(
            data_type=data_type,
            route_id=route_id,
            valid_time=temporal_slice.valid_time,
            source=source,
            version=version,
            issue_evidence=issue_evidence,
            metadata=metadata,
            payload_checksum=payload_checksum,
        )
        stem = self._stem(
            data_type,
            route_id,
            temporal_slice.valid_time,
            issue_evidence.issue_time,
            version,
            publication_id,
        )
        payload_name = f"{stem}.nc"
        sidecar_name = f"{stem}.metadata.json"
        payload = self.incoming / payload_name
        if payload.exists() and _sha256(payload) == payload_checksum:
            temporary.unlink()
        else:
            temporary.replace(payload)
        frame_metadata: dict[str, Any] = {
            **(metadata or {}),
            "issue_time_evidence": issue_evidence.to_dict(),
            "source_time_index": temporal_slice.source_index,
        }
        if temporal_slice.forecast_reference_time is not None:
            frame_metadata["forecast_reference_time"] = isoformat_utc(
                temporal_slice.forecast_reference_time
            )
            frame_metadata["forecast_lead_hours"] = (
                temporal_slice.valid_time - temporal_slice.forecast_reference_time
            ).total_seconds() / 3600.0
        sidecar = {
            "file": payload_name,
            "payload_sha256": payload_checksum,
            "payload_size_bytes": payload_size,
            "publication_id": publication_id,
            "data_type": data_type,
            "route_id": route_id,
            "issue_time": isoformat_utc(issue_evidence.issue_time),
            "valid_time": isoformat_utc(temporal_slice.valid_time),
            "source": source,
            "version": version,
            "quality_flag": _publication_quality(issue_evidence, quality_flag).value,
            "metadata": frame_metadata,
        }
        self._atomic_text(
            self.incoming / sidecar_name,
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return _ExpectedPublication(sidecar_name, payload_name, publication_id)

    def _scan_expected(
        self, expected_publications: list[_ExpectedPublication]
    ) -> PublishResult:
        expected_sidecars = {item.sidecar_name for item in expected_publications}
        expected_ids = {item.publication_id for item in expected_publications}
        result = self.folder_source.scan_once()
        failures = [
            f"{path.name}: {message}"
            for path, message in result.failures
            if path.name in expected_sidecars
        ]
        if failures:
            raise DataValidationError("自动 sidecar 入库失败: " + " | ".join(failures))
        records_by_publication = {
            str(record.metadata.get("publication_id")): record
            for record in result.ingested
            if record.metadata.get("publication_id") in expected_ids
        }
        if len(records_by_publication) != len(expected_publications):
            for record in self.folder_source.manifest.all_records():
                publication_id = str(record.metadata.get("publication_id", ""))
                if publication_id in expected_ids:
                    records_by_publication[publication_id] = record
        if len(records_by_publication) != len(expected_publications):
            raise DataValidationError(
                f"期望发布 {len(expected_publications)} 帧，实际入库 "
                f"{len(records_by_publication)} 帧"
            )
        records = tuple(
            records_by_publication[item.publication_id]
            for item in expected_publications
        )
        return PublishResult(records, len(expected_publications))

    @staticmethod
    def _stem(
        data_type: str,
        route_id: str,
        valid_time: datetime,
        issue_time: datetime,
        version: str,
        publication_id: str,
    ) -> str:
        return (
            f"{_safe(data_type)}_{_safe(route_id)}_"
            f"valid_{valid_time:%Y%m%dT%H%M%SZ}_"
            f"issued_{issue_time:%Y%m%dT%H%M%SZ}_{_safe(version)}_{publication_id}"
        )

    @staticmethod
    def _publication_id(
        *,
        data_type: str,
        route_id: str,
        valid_time: datetime,
        source: str,
        version: str,
        issue_evidence: IssueTimeEvidence,
        metadata: dict[str, Any] | None,
        payload_checksum: str,
    ) -> str:
        identity = json.dumps(
            {
                "data_type": data_type,
                "route_id": route_id,
                "valid_time": isoformat_utc(valid_time),
                "source": source,
                "version": version,
                "issue_time_evidence": issue_evidence.to_dict(),
                "metadata": metadata or {},
                "payload_sha256": payload_checksum,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(identity.encode()).hexdigest()[:16]

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.part")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)


def _safe(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return safe or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _publication_quality(
    evidence: IssueTimeEvidence, requested: QualityFlag | None
) -> QualityFlag:
    if requested is QualityFlag.MISSING:
        raise DataValidationError("实际 payload 不能标记为 missing")
    evidence_quality = (
        QualityFlag.GOOD if evidence.authoritative else QualityFlag.SUSPECT
    )
    if requested is None:
        return evidence_quality
    return (
        requested
        if QUALITY_RANK[requested] <= QUALITY_RANK[evidence_quality]
        else evidence_quality
    )
