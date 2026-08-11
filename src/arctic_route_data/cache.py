"""Route-isolated, revision-aware and bounded AB cache."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from time import monotonic
from typing import Any

from arctic_route_data.errors import CacheCapacityError, StaleGenerationError
from arctic_route_data.models import (
    QUALITY_RANK,
    DataCategory,
    ManifestRecord,
    StandardDataFrame,
)
from arctic_route_data.timeutils import UTC, ensure_utc, parse_utc

_PartitionKey = tuple[str, str, str]
_LogicalFrameKey = tuple[str, str, datetime]


@dataclass(slots=True)
class _CacheEntry:
    frame: StandardDataFrame
    ref_count: int
    last_access: float
    active: bool = True


class PartitionedABCache:
    def __init__(
        self,
        *,
        max_memory_mb: float = 512,
        slow_frames_per_partition: int = 256,
        dynamic_frames_per_partition: int = 256,
    ) -> None:
        if max_memory_mb <= 0:
            raise ValueError("max_memory_mb 必须大于 0")
        self.max_bytes = int(max_memory_mb * 1024 * 1024)
        self.slow_frames_per_partition = max(slow_frames_per_partition, 2)
        self.dynamic_frames_per_partition = max(dynamic_frames_per_partition, 2)
        self._entries: dict[str, _CacheEntry] = {}
        self._partitions: dict[_PartitionKey, list[str]] = defaultdict(list)
        self._active_revisions: dict[_LogicalFrameKey, str] = {}
        self._generation_id = 0
        self._bytes = 0
        self._lock = RLock()

    @property
    def generation_id(self) -> int:
        with self._lock:
            return self._generation_id

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def contains(self, data_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(data_id)
            return entry is not None and entry.active

    def put(self, frame: StandardDataFrame, *, simulation_time: datetime | None = None) -> bool:
        """Activate a frame and return whether it became the selected revision."""

        with self._lock:
            if frame.generation_id != self._generation_id:
                raise StaleGenerationError(
                    f"拒绝代次 {frame.generation_id} 的迟到帧；当前代次为 {self._generation_id}"
                )
            stored_frame = frame.consumer_copy()
            existing = self._entries.get(frame.record.data_id)
            if existing is not None:
                existing.last_access = monotonic()
                return existing.active

            logical_key = self._logical_key(frame.record)
            incumbent_id = self._active_revisions.get(logical_key)
            incumbent = self._entries.get(incumbent_id) if incumbent_id else None
            if incumbent is not None and self._revision_rank(frame.record) <= self._revision_rank(
                incumbent.frame.record
            ):
                return False

            removable_bytes = (
                incumbent.frame.estimated_bytes
                if incumbent is not None and incumbent.ref_count == 0
                else 0
            )
            self._make_room(
                frame.estimated_bytes - removable_bytes,
                protected={incumbent_id} if incumbent_id else set(),
            )
            if incumbent is not None:
                self._deactivate(incumbent_id)
                if incumbent.ref_count == 0:
                    self._delete_entry(incumbent_id)

            entry = _CacheEntry(stored_frame, 0, monotonic())
            self._entries[frame.record.data_id] = entry
            self._active_revisions[logical_key] = frame.record.data_id
            self._bytes += frame.estimated_bytes
            for variable in frame.record.variables:
                key = (frame.record.route_id, frame.record.data_type, variable)
                self._partitions[key].append(frame.record.data_id)
                self._partitions[key].sort(key=self._partition_sort_key)
            self._enforce_partition_limits(
                frame.record.category,
                frame.record.route_id,
                frame.record.data_type,
            )
            self._evict_expired_events(simulation_time)
            return True

    def _make_room(self, additional_bytes: int, *, protected: set[str]) -> None:
        needed = max(additional_bytes, 0)
        if self._bytes + needed <= self.max_bytes:
            return
        candidates = sorted(
            (
                (data_id, entry)
                for data_id, entry in self._entries.items()
                if entry.ref_count == 0 and data_id not in protected
            ),
            key=lambda item: (
                item[1].frame.record.category is DataCategory.STATIC,
                item[1].last_access,
            ),
        )
        for data_id, _ in candidates:
            if self._bytes + needed <= self.max_bytes:
                break
            self._deactivate(data_id)
            self._delete_entry(data_id)
        if self._bytes + needed > self.max_bytes:
            raise CacheCapacityError(
                f"AB 缓存需要新增 {needed} bytes，当前 {self._bytes} bytes；"
                f"受租用帧保护后无法满足上限 {self.max_bytes} bytes"
            )

    def _enforce_partition_limits(
        self, category: DataCategory, route_id: str, data_type: str
    ) -> None:
        if category is DataCategory.EVENT:
            return
        limit = {
            DataCategory.STATIC: 1,
            DataCategory.SLOW: self.slow_frames_per_partition,
            DataCategory.DYNAMIC: self.dynamic_frames_per_partition,
        }[category]
        ids = sorted(
            {
                data_id
                for (partition_route, partition_type, _), values in self._partitions.items()
                if partition_route == route_id and partition_type == data_type
                for data_id in values
            },
            key=self._partition_sort_key,
        )
        for data_id in ids[:-limit]:
            entry = self._entries.get(data_id)
            if entry is not None:
                self._deactivate(data_id)
                if entry.ref_count == 0:
                    self._delete_entry(data_id)

    def _evict_expired_events(self, simulation_time: datetime | None) -> None:
        if simulation_time is None:
            return
        now = ensure_utc(simulation_time, field="simulation_time")
        for data_id, entry in tuple(self._entries.items()):
            if not entry.active or entry.frame.record.category is not DataCategory.EVENT:
                continue
            end_time = entry.frame.record.metadata.get("end_time")
            if (
                end_time
                and parse_utc(str(end_time), field="end_time") < now
            ):
                self._deactivate(data_id)
                if entry.ref_count == 0:
                    self._delete_entry(data_id)

    def evict_expired_events(self, simulation_time: datetime) -> None:
        """Remove events that are no longer active at the supplied simulation time."""

        with self._lock:
            self._evict_expired_events(simulation_time)

    def _deactivate(self, data_id: str) -> None:
        entry = self._entries.get(data_id)
        if entry is None or not entry.active:
            return
        entry.active = False
        logical_key = self._logical_key(entry.frame.record)
        if self._active_revisions.get(logical_key) == data_id:
            del self._active_revisions[logical_key]
        for key, values in tuple(self._partitions.items()):
            if data_id in values:
                values.remove(data_id)
            if not values:
                del self._partitions[key]

    def _delete_entry(self, data_id: str) -> None:
        entry = self._entries.get(data_id)
        if entry is None or entry.ref_count:
            return
        if entry.active:
            self._deactivate(data_id)
        self._bytes -= entry.frame.estimated_bytes
        del self._entries[data_id]

    @staticmethod
    def _logical_key(record: ManifestRecord) -> _LogicalFrameKey:
        return record.route_id, record.data_type, record.valid_time

    @staticmethod
    def _revision_rank(record: ManifestRecord) -> tuple[Any, ...]:
        return (
            QUALITY_RANK[record.quality_flag],
            record.issue_time,
            record.ingest_time,
            record.version,
            record.data_id,
        )

    def _partition_sort_key(self, data_id: str) -> tuple[Any, ...]:
        record = self._entries[data_id].frame.record
        return record.valid_time, self._revision_rank(record)

    def get_window(
        self,
        data_type: str,
        start_time: datetime,
        end_time: datetime,
        *,
        route_id: str,
        variable: str | None = None,
    ) -> list[StandardDataFrame]:
        start = ensure_utc(start_time, field="start_time")
        end = ensure_utc(end_time, field="end_time")
        if end < start:
            raise ValueError("end_time 不能早于 start_time")
        with self._lock:
            if variable is None:
                data_ids = {
                    data_id
                    for (partition_route, partition_type, _), values in self._partitions.items()
                    if partition_route == route_id and partition_type == data_type
                    for data_id in values
                }
            else:
                data_ids = set(self._partitions.get((route_id, data_type, variable), []))
            frames = [
                self._entries[data_id].frame.consumer_copy()
                for data_id in data_ids
                if start <= self._entries[data_id].frame.record.valid_time <= end
            ]
            for frame in frames:
                self._entries[frame.record.data_id].last_access = monotonic()
        return sorted(frames, key=lambda frame: frame.record.valid_time)

    def latest(
        self,
        data_type: str,
        *,
        route_id: str,
        variable: str | None = None,
        at_or_before: datetime | None = None,
    ) -> StandardDataFrame | None:
        end = (
            ensure_utc(at_or_before, field="at_or_before")
            if at_or_before is not None
            else datetime.max.replace(tzinfo=UTC)
        )
        candidates = self.get_window(
            data_type,
            datetime.min.replace(tzinfo=UTC),
            end,
            route_id=route_id,
            variable=variable,
        )
        return candidates[-1] if candidates else None

    @contextmanager
    def lease(self, data_id: str):
        with self._lock:
            entry = self._entries[data_id]
            entry.ref_count += 1
            entry.last_access = monotonic()
            frame = entry.frame.consumer_copy()
        try:
            yield frame
        finally:
            with self._lock:
                current = self._entries.get(data_id)
                if current is not None:
                    current.ref_count -= 1
                    if current.ref_count == 0 and not current.active:
                        self._delete_entry(data_id)

    def reset_generation(
        self,
        generation_id: int,
        *,
        simulation_time: datetime | None = None,
    ) -> None:
        as_of = (
            ensure_utc(simulation_time, field="simulation_time")
            if simulation_time is not None
            else None
        )
        with self._lock:
            if generation_id <= self._generation_id:
                raise ValueError("新 generation_id 必须严格递增")
            static_frames = [
                entry.frame.with_generation(generation_id)
                for entry in self._entries.values()
                if entry.active
                and entry.frame.record.category is DataCategory.STATIC
                and as_of is not None
                and entry.frame.record.issue_time <= as_of
            ]
            self._entries.clear()
            self._partitions.clear()
            self._active_revisions.clear()
            self._bytes = 0
            self._generation_id = generation_id
            for frame in static_frames:
                self.put(frame)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generation_id": self._generation_id,
                "frames": sum(entry.active for entry in self._entries.values()),
                "leased_inactive_frames": sum(
                    not entry.active and entry.ref_count > 0 for entry in self._entries.values()
                ),
                "used_bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "partitions": {
                    f"{route_id}/{data_type}/{variable}": len(values)
                    for (route_id, data_type, variable), values in sorted(
                        self._partitions.items()
                    )
                },
            }
