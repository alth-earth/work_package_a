"""Folder-watch ingestion adapter for future near-real-time producers."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from arctic_route_data.ingestion import IngestionPipeline, sha256_file
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
        self._recover_stale_claims()
        for original in sorted(self.pipeline.incoming_root.glob("*.metadata.json")):
            claimed_at_us = int(datetime.now(UTC).timestamp() * 1_000_000)
            claimed = original.with_name(
                f".{original.name}.processing-{claimed_at_us}-{os.getpid()}"
            )
            record: ManifestRecord | None = None
            try:
                original.replace(claimed)
            except FileNotFoundError:
                continue
            try:
                metadata = json.loads(claimed.read_text(encoding="utf-8"))
                payload = (claimed.parent / metadata["file"]).resolve()
                if payload.suffix == ".part" or not payload.is_file():
                    claimed.replace(original)
                    continue
                record = self.pipeline.ingest_sidecar(claimed)
                self._archive_upstream_pair(
                    claimed, payload, record, archived_sidecar_name=original.name
                )
                ingested.append(record)
            except Exception as exc:  # a bad producer item must not stop the watcher
                failures.append((original, str(exc)))
                if record is None:
                    self._quarantine_pair(claimed, original_name=original.name)
                elif claimed.is_file() and not original.exists():
                    # Ready + manifest already succeeded. Keep the raw pair for
                    # an idempotent archive retry instead of quarantining valid data.
                    claimed.replace(original)
        return ScanResult(tuple(ingested), tuple(failures))

    def _archive_upstream_pair(
        self,
        sidecar: Path,
        payload: Path,
        record: ManifestRecord,
        *,
        archived_sidecar_name: str,
    ) -> None:
        destination = (
            self.archive_root
            / "raw"
            / record.route_id
            / record.data_type
            / record.data_id
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(f".{destination.name}.{datetime.now(UTC):%H%M%S%f}.part")
        if destination.exists():
            archived_payload = destination / payload.name
            archived_sidecar = destination / archived_sidecar_name
            if (
                not archived_payload.is_file()
                or not archived_sidecar.is_file()
                or sha256_file(archived_payload) != sha256_file(payload)
                or sha256_file(archived_sidecar) != sha256_file(sidecar)
            ):
                raise RuntimeError(f"raw 归档目标已存在但内容不同: {destination}")
        else:
            staging.mkdir()
            try:
                shutil.copy2(payload, staging / payload.name)
                shutil.copy2(sidecar, staging / archived_sidecar_name)
                staging.replace(destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        payload.unlink()
        sidecar.unlink()

    def _quarantine_pair(self, sidecar: Path, *, original_name: str) -> None:
        destination = self.pipeline.quarantine_root / (
            f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}_{sidecar.stem}"
        )
        destination.mkdir(parents=True, exist_ok=False)
        payload: Path | None = None
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            candidate = (sidecar.parent / str(metadata.get("file", ""))).resolve()
            if candidate.is_relative_to(self.pipeline.incoming_root.resolve()):
                payload = candidate
        except (OSError, json.JSONDecodeError):
            pass
        if payload is not None and payload.is_file():
            shutil.move(payload, destination / payload.name)
        if sidecar.is_file():
            shutil.move(sidecar, destination / original_name)

    def _recover_stale_claims(self) -> None:
        """Return abandoned claims to the queue after a previous process crashed."""

        now = datetime.now(UTC).timestamp()
        for claimed in self.pipeline.incoming_root.glob(".*.metadata.json.processing-*"):
            try:
                claim_suffix = claimed.name.rsplit(".processing-", 1)[1]
                claimed_at = int(claim_suffix.split("-", 1)[0]) / 1_000_000
                if now - claimed_at < 300:
                    continue
                visible_name = claimed.name[1:].split(".processing-", 1)[0]
                visible = claimed.with_name(visible_name)
                if not visible.exists():
                    claimed.replace(visible)
            except (FileNotFoundError, ValueError):
                continue
