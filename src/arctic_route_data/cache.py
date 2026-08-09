"""Type/variable partitioned, bounded AB cache with generation isolation."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from time import monotonic
from typing import Any

from arctic_route_data.errors import CacheCapacityError, StaleGenerationError
from arctic_route_data.models import DataCategory, StandardDataFrame
from arctic_route_data.timeutils import UTC, ensure_utc, parse_utc


@dataclass(slots=True)
class _CacheEntry:
    frame: StandardDataFrame
    ref_count: int
    last_access: float


class PartitionedABCache:
    def __init__(
        self,
        *,
        max_memory_mb: float = 512,
        slow_frames_per_partition: int = 2,
        dynamic_frames_per_partition: int = 64,
    ) -> None:
        if max_memory_mb <= 0:
            raise ValueError("max_memory_mb 必须大于 0")
        self.max_bytes = int(max_memory_mb * 1024 * 1024)
        self.slow_frames_per_partition = max(slow_frames_per_partition, 2)
        self.dynamic_frames_per_partition = max(dynamic_frames_per_partition, 2)
        self._entries: dict[str, _CacheEntry] = {}
        self._partitions: dict[tuple[str, str], list[str]] = defaultdict(list)
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

    def put(self, frame: StandardDataFrame, *, simulation_time: datetime | None = None) -> None:
        with self._lock:
            if frame.generation_id != self._generation_id:
                raise StaleGenerationError(
                    f"拒绝代次 {frame.generation_id} 的迟到帧；当前代次为 {self._generation_id}"
                )
            if frame.record.data_id in self._entries:
                self._entries[frame.record.data_id].last_access = monotonic()
                return
            self._entries[frame.record.data_id] = _CacheEntry(frame, 0, monotonic())
            self._bytes += frame.estimated_bytes
            for variable in frame.record.variables:
                key = (frame.record.data_type, variable)
                self._partitions[key].append(frame.record.data_id)
                self._partitions[key].sort(
                    key=lambda data_id: (
                        self._entries[data_id].frame.record.valid_time,
                        self._entries[data_id].frame.record.issue_time,
                    )
                )
            self._enforce_partition_limits(frame.record.category, frame.record.data_type)
            self._evict_expired_events(simulation_time)
            self._enforce_memory_limit()

    def _enforce_partition_limits(self, category: DataCategory, data_type: str) -> None:
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
                for (partition_type, _), values in self._partitions.items()
                if partition_type == data_type
                for data_id in values
            },
            key=lambda data_id: self._entries[data_id].frame.record.valid_time,
        )
        for data_id in ids[:-limit]:
            self._remove_if_unleased(data_id)

    def _evict_expired_events(self, simulation_time: datetime | None) -> None:
        if simulation_time is None:
            return
        now = ensure_utc(simulation_time, field="simulation_time")
        for data_id, entry in tuple(self._entries.items()):
            if entry.frame.record.category is not DataCategory.EVENT:
                continue
            end_time = entry.frame.record.metadata.get("end_time")
            if end_time and parse_utc(str(end_time), field="end_time") < now:
                self._remove_if_unleased(data_id)

    def _enforce_memory_limit(self) -> None:
        candidates = sorted(
            (
                (data_id, entry)
                for data_id, entry in self._entries.items()
                if entry.ref_count == 0
            ),
            key=lambda item: (
                item[1].frame.record.category is DataCategory.STATIC,
                item[1].last_access,
            ),
        )
        for data_id, _ in candidates:
            if self._bytes <= self.max_bytes:
                break
            self._remove_if_unleased(data_id)
        if self._bytes > self.max_bytes:
            raise CacheCapacityError(
                f"AB 缓存已使用 {self._bytes} bytes，且受引用帧无法回收；"
                f"上限 {self.max_bytes} bytes"
            )

    def _remove_if_unleased(self, data_id: str) -> bool:
        entry = self._entries.get(data_id)
        if entry is None or entry.ref_count:
            return False
        self._bytes -= entry.frame.estimated_bytes
        del self._entries[data_id]
        for key, values in tuple(self._partitions.items()):
            if data_id in values:
                values.remove(data_id)
            if not values:
                del self._partitions[key]
        return True

    def get_window(
        self,
        data_type: str,
        start_time: datetime,
        end_time: datetime,
        *,
        variable: str | None = None,
    ) -> list[StandardDataFrame]:
        start = ensure_utc(start_time, field="start_time")
        end = ensure_utc(end_time, field="end_time")
        with self._lock:
            if variable is None:
                data_ids = {
                    data_id
                    for (partition_type, _), values in self._partitions.items()
                    if partition_type == data_type
                    for data_id in values
                }
            else:
                data_ids = set(self._partitions.get((data_type, variable), []))
            frames = [
                self._entries[data_id].frame
                for data_id in data_ids
                if start <= self._entries[data_id].frame.record.valid_time <= end
            ]
            for frame in frames:
                self._entries[frame.record.data_id].last_access = monotonic()
        return sorted(frames, key=lambda frame: frame.record.valid_time)

    def latest(self, data_type: str, *, variable: str | None = None) -> StandardDataFrame | None:
        with self._lock:
            candidates = self.get_window(
                data_type,
                datetime.min.replace(tzinfo=UTC),
                datetime.max.replace(tzinfo=UTC),
                variable=variable,
            )
        return candidates[-1] if candidates else None

    @contextmanager
    def lease(self, data_id: str):
        with self._lock:
            entry = self._entries[data_id]
            entry.ref_count += 1
            entry.last_access = monotonic()
            frame = entry.frame
        try:
            yield frame
        finally:
            with self._lock:
                current = self._entries.get(data_id)
                if current is not None:
                    current.ref_count -= 1

    def reset_generation(self, generation_id: int) -> None:
        with self._lock:
            if generation_id <= self._generation_id:
                raise ValueError("新 generation_id 必须严格递增")
            static_frames = [
                entry.frame.with_generation(generation_id)
                for entry in self._entries.values()
                if entry.frame.record.category is DataCategory.STATIC
            ]
            self._entries.clear()
            self._partitions.clear()
            self._bytes = 0
            self._generation_id = generation_id
            for frame in static_frames:
                self.put(frame)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generation_id": self._generation_id,
                "frames": len(self._entries),
                "used_bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "partitions": {
                    f"{data_type}/{variable}": len(values)
                    for (data_type, variable), values in sorted(self._partitions.items())
                },
            }
