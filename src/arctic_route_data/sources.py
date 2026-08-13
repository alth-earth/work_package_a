"""Pluggable source contract and current offline archive implementation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Protocol

import xarray as xr

from arctic_route_data.errors import ChecksumMismatchError, FutureInformationError
from arctic_route_data.ingestion import sha256_file
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import ManifestRecord, StandardDataFrame
from arctic_route_data.timeutils import ensure_utc


class DataSource(Protocol):
    def list_available(
        self,
        data_type: str,
        start_time: datetime,
        end_time: datetime,
        *,
        route_id: str,
        as_of: datetime,
    ) -> Sequence[ManifestRecord]: ...

    def get_latest_before(
        self,
        data_type: str,
        target_time: datetime,
        *,
        route_id: str,
        as_of: datetime,
    ) -> ManifestRecord | None: ...

    def get_bracketing(
        self,
        data_type: str,
        target_time: datetime,
        *,
        route_id: str,
        as_of: datetime,
    ) -> tuple[ManifestRecord | None, ManifestRecord | None]: ...

    def load_frame(
        self, record: ManifestRecord, *, generation_id: int, as_of: datetime
    ) -> StandardDataFrame: ...


class LocalArchiveSource:
    def __init__(
        self,
        archive_root: str | Path,
        manifest: ManifestStore | None = None,
        *,
        verify_checksums: bool = True,
    ) -> None:
        self.archive_root = Path(archive_root).resolve()
        self.manifest = manifest or ManifestStore(
            self.archive_root / "manifest" / "manifest.sqlite3"
        )
        self.verify_checksums = verify_checksums
        self._provenance_lock = RLock()

    def list_available(self, *args, **kwargs):
        return self.manifest.list_available(*args, **kwargs)

    def get_latest_before(self, *args, **kwargs):
        return self.manifest.get_latest_before(*args, **kwargs)

    def get_bracketing(self, *args, **kwargs):
        return self.manifest.get_bracketing(*args, **kwargs)

    def get_record_by_id(self, data_id: str) -> ManifestRecord | None:
        """Resolve one immutable manifest revision without exposing its storage."""

        return self.manifest.get(data_id)

    def load_frame(
        self, record: ManifestRecord, *, generation_id: int, as_of: datetime
    ) -> StandardDataFrame:
        if not record.is_available_at(ensure_utc(as_of, field="as_of")):
            raise FutureInformationError(
                f"{record.data_id} 的 issue_time={record.issue_time.isoformat()} 晚于模拟时刻"
            )
        path = record.absolute_path(self.archive_root)
        if self.verify_checksums and sha256_file(path) != record.checksum:
            raise ChecksumMismatchError(f"文件校验失败: {path}")
        if record.media_type == "application/geo+json":
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            with xr.open_dataset(path, engine="h5netcdf") as dataset:
                payload = dataset.load()
        return StandardDataFrame(record, payload, generation_id)

    def verified_provenance_id(self, record: ManifestRecord) -> str | None:
        """Verify on-disk archive evidence before granting formal provenance."""

        with self._provenance_lock:
            # Local import keeps the ordinary source/read path independent from
            # the heavier archive doctor module until formal coverage is asked.
            from arctic_route_data.doctor import verified_archived_provenance_id

            # Formal bundle publication is infrequent compared with ordinary
            # frame reads. Rehash every bound artifact here so a same-process
            # archive mutation cannot reuse stale provenance/checksum state.
            return verified_archived_provenance_id(
                self.archive_root,
                record,
                checksum_cache={},
            )

    def load_verified_frame(
        self,
        record: ManifestRecord,
        *,
        generation_id: int,
        as_of: datetime,
        expected_provenance_id: str,
    ) -> StandardDataFrame:
        """Load an exact revision while rechecking bound archive evidence.

        This formal cross-process/restart capability keeps manifest paths,
        SQLite and raw/source-snapshot layout behind Work Package A.
        """

        with self._provenance_lock:
            from arctic_route_data.doctor import verified_archived_provenance_id

            before = verified_archived_provenance_id(
                self.archive_root,
                record,
                checksum_cache={},
            )
            if before != expected_provenance_id:
                raise ChecksumMismatchError(
                    f"{record.data_id} 的归档 provenance 与 DatasetBundle 不一致"
                )
            frame = self.load_frame(
                record,
                generation_id=generation_id,
                as_of=as_of,
            )
            after = verified_archived_provenance_id(
                self.archive_root,
                record,
                checksum_cache={},
            )
            if after != expected_provenance_id:
                raise ChecksumMismatchError(
                    f"{record.data_id} 的归档 provenance 在读取期间发生变化"
                )
            return frame
