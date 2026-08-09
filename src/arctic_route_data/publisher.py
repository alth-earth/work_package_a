"""Publish downloader results as atomic frame files plus sidecars, then ingest them into A."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import xarray as xr

from arctic_route_data.errors import DataValidationError
from arctic_route_data.folder_watch import FolderWatchSource
from arctic_route_data.issue_time import IssueTimeEvidence
from arctic_route_data.models import ManifestRecord, QualityFlag
from arctic_route_data.temporal_split import TemporalSlice, split_dataset_by_valid_time
from arctic_route_data.timeutils import ensure_utc, isoformat_utc


@dataclass(frozen=True, slots=True)
class PublishResult:
    records: tuple[ManifestRecord, ...]
    sidecars_written: int


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
        names: list[str] = []
        for temporal_slice in temporal_slices:
            names.append(
                self._write_frame_pair(
                    temporal_slice,
                    data_type=data_type,
                    route_id=route_id,
                    source=source,
                    version=version,
                    issue_evidence=issue_evidence,
                    metadata=metadata,
                )
            )
        return self._scan_expected(names)

    def publish_geojson(
        self,
        geojson: dict[str, Any],
        *,
        route_id: str,
        source: str,
        version: str,
        issue_evidence: IssueTimeEvidence,
        valid_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublishResult:
        if geojson.get("type") != "FeatureCollection":
            raise DataValidationError("限制区下载结果必须是 GeoJSON FeatureCollection")
        valid = ensure_utc(valid_time or issue_evidence.issue_time, field="valid_time")
        stem = self._stem(
            "long_term_restricted_area",
            route_id,
            valid,
            issue_evidence.issue_time,
            version,
        )
        payload_name = f"{stem}.geojson"
        sidecar_name = f"{stem}.metadata.json"
        payload = self.incoming / payload_name
        self._atomic_text(payload, json.dumps(geojson, ensure_ascii=False, sort_keys=True))
        sidecar = {
            "file": payload_name,
            "data_type": "long_term_restricted_area",
            "route_id": route_id,
            "issue_time": isoformat_utc(issue_evidence.issue_time),
            "valid_time": isoformat_utc(valid),
            "source": source,
            "version": version,
            "quality_flag": (
                QualityFlag.GOOD.value
                if issue_evidence.authoritative
                else QualityFlag.SUSPECT.value
            ),
            "metadata": {
                "issue_time_evidence": issue_evidence.to_dict(),
                **(metadata or {}),
            },
        }
        self._atomic_text(
            self.incoming / sidecar_name,
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return self._scan_expected([sidecar_name])

    def _write_frame_pair(
        self,
        temporal_slice: TemporalSlice,
        *,
        data_type: str,
        route_id: str,
        source: str,
        version: str,
        issue_evidence: IssueTimeEvidence,
        metadata: dict[str, Any] | None,
    ) -> str:
        stem = self._stem(
            data_type,
            route_id,
            temporal_slice.valid_time,
            issue_evidence.issue_time,
            version,
        )
        payload_name = f"{stem}.nc"
        sidecar_name = f"{stem}.metadata.json"
        payload = self.incoming / payload_name
        temporary = payload.with_suffix(payload.suffix + f".{os.getpid()}.part")
        temporal_slice.dataset.to_netcdf(temporary, engine="h5netcdf")
        temporary.replace(payload)
        frame_metadata: dict[str, Any] = {
            "issue_time_evidence": issue_evidence.to_dict(),
            "source_time_index": temporal_slice.source_index,
            **(metadata or {}),
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
            "data_type": data_type,
            "route_id": route_id,
            "issue_time": isoformat_utc(issue_evidence.issue_time),
            "valid_time": isoformat_utc(temporal_slice.valid_time),
            "source": source,
            "version": version,
            "quality_flag": (
                QualityFlag.GOOD.value
                if issue_evidence.authoritative
                else QualityFlag.SUSPECT.value
            ),
            "metadata": frame_metadata,
        }
        self._atomic_text(
            self.incoming / sidecar_name,
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return sidecar_name

    def _scan_expected(self, expected_sidecars: list[str]) -> PublishResult:
        expected_payloads = {
            json.loads((self.incoming / name).read_text(encoding="utf-8"))["file"]
            for name in expected_sidecars
        }
        result = self.folder_source.scan_once()
        failures = [
            f"{path.name}: {message}"
            for path, message in result.failures
            if path.name in expected_sidecars
        ]
        if failures:
            raise DataValidationError("自动 sidecar 入库失败: " + " | ".join(failures))
        records = tuple(
            record
            for record in result.ingested
            if record.metadata.get("upstream_file") in expected_payloads
        )
        if len(records) != len(expected_sidecars):
            raise DataValidationError(
                f"期望发布 {len(expected_sidecars)} 帧，实际入库 {len(records)} 帧"
            )
        return PublishResult(records, len(expected_sidecars))

    @staticmethod
    def _stem(
        data_type: str,
        route_id: str,
        valid_time: datetime,
        issue_time: datetime,
        version: str,
    ) -> str:
        return (
            f"{_safe(data_type)}_{_safe(route_id)}_"
            f"valid_{valid_time:%Y%m%dT%H%M%SZ}_"
            f"issued_{issue_time:%Y%m%dT%H%M%SZ}_{_safe(version)}"
        )

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)


def _safe(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return safe or "unknown"
