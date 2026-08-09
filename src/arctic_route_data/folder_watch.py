"""Folder-watch ingestion adapter for future near-real-time producers."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from arctic_route_data.ingestion import IngestionPipeline
from arctic_route_data.models import ManifestRecord
from arctic_route_data.sources import LocalArchiveSource


@dataclass(frozen=True, slots=True)
class ScanResult:
    ingested: tuple[ManifestRecord, ...]
    failures: tuple[tuple[Path, str], ...]


class FolderWatchSource(LocalArchiveSource):
    """Local archive plus an explicit ``scan_once`` producer boundary.

    An upstream downloader writes ``payload.nc.part``, atomically renames it to
    ``payload.nc`` and finally publishes ``payload.metadata.json``. Only then is
    the pair eligible for ingestion.
    """

    def __init__(self, archive_root: str | Path, **kwargs) -> None:
        super().__init__(archive_root, **kwargs)
        self.pipeline = IngestionPipeline(self.archive_root, self.manifest)

    def scan_once(self) -> ScanResult:
        ingested: list[ManifestRecord] = []
        failures: list[tuple[Path, str]] = []
        for sidecar in sorted(self.pipeline.incoming_root.glob("*.metadata.json")):
            try:
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                payload = (sidecar.parent / metadata["file"]).resolve()
                if payload.suffix == ".part" or not payload.is_file():
                    continue
                record = self.pipeline.ingest_sidecar(sidecar)
                self._archive_upstream_pair(sidecar, payload, record)
                ingested.append(record)
            except Exception as exc:  # a bad producer item must not stop the watcher
                failures.append((sidecar, str(exc)))
                quarantine = self.pipeline.quarantine_root / (
                    f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{sidecar.name}"
                )
                shutil.move(sidecar, quarantine)
        return ScanResult(tuple(ingested), tuple(failures))

    def _archive_upstream_pair(
        self, sidecar: Path, payload: Path, record: ManifestRecord
    ) -> None:
        destination = (
            self.archive_root
            / "raw"
            / record.route_id
            / record.data_type
            / record.data_id
        )
        destination.mkdir(parents=True, exist_ok=True)
        shutil.move(payload, destination / payload.name)
        shutil.move(sidecar, destination / sidecar.name)
