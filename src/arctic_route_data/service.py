"""Work package A orchestrator: clock-aware prefetch and AB publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import ClockSnapshot, SimulationClock
from arctic_route_data.errors import StaleGenerationError
from arctic_route_data.events import (
    DataArrivalEvent,
    DataLoadFailureEvent,
    EventBus,
    GenerationChangedEvent,
    MissingDataAlert,
)
from arctic_route_data.models import DataCategory, StandardDataFrame
from arctic_route_data.sources import DataSource
from arctic_route_data.timeutils import ensure_utc


@dataclass(frozen=True, slots=True)
class CoverageReport:
    data_type: str
    requested_start: datetime
    requested_end: datetime
    minimum_required_end: datetime
    available_start: datetime | None
    available_end: datetime | None
    expected_interval_hours: float | None
    missing_intervals: tuple[tuple[datetime, datetime], ...]
    source_snapshot_ids: tuple[str, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class PreparedWindow:
    route_id: str
    generation_id: int
    frames: Mapping[str, tuple[StandardDataFrame, ...]]
    coverage: Mapping[str, CoverageReport]


_DEFAULT_INTERVAL_HOURS: dict[str, float | None] = {
    "wind_field": 3.0,
    "temperature": 3.0,
    "visibility": 3.0,
    "wave": 3.0,
    "ocean_current": 6.0,
    "water_level": 6.0,
    "sea_ice_concentration": 24.0,
    "sea_ice_type": 24.0,
    "sea_ice_edge": 24.0,
    "sea_ice_drift": 24.0,
    "sea_ice_thickness": 24.0,
    "bathymetry": None,
    "long_term_restricted_area": None,
}


class WorkPackageA:
    def __init__(
        self,
        *,
        source: DataSource,
        clock: SimulationClock,
        cache: PartitionedABCache,
        event_bus: EventBus | None = None,
        history_hours: int = 48,
    ) -> None:
        self.source = source
        self.clock = clock
        self.cache = cache
        self.events = event_bus or EventBus()
        self.history_hours = history_hours
        self._unsubscribe = clock.subscribe_seek(self._on_seek)

    def close(self) -> None:
        self._unsubscribe()

    def _on_seek(self, snapshot: ClockSnapshot) -> None:
        self.cache.reset_generation(
            snapshot.generation_id,
            simulation_time=snapshot.current_time,
        )
        self.events.publish(
            GenerationChangedEvent(snapshot.generation_id, snapshot.current_time)
        )

    def prefetch(
        self,
        *,
        route_id: str,
        data_types: list[str] | tuple[str, ...],
        horizon_hours: int = 156,
    ) -> list[StandardDataFrame]:
        snapshot = self.clock.snapshot()
        return self._prefetch_at_snapshot(
            route_id=route_id,
            data_types=data_types,
            horizon_hours=horizon_hours,
            snapshot=snapshot,
        )

    def _prefetch_at_snapshot(
        self,
        *,
        route_id: str,
        data_types: list[str] | tuple[str, ...],
        horizon_hours: int,
        snapshot: ClockSnapshot,
    ) -> list[StandardDataFrame]:
        """Load only records knowable at one immutable clock snapshot."""

        self.cache.evict_expired_events(snapshot.current_time)
        start = snapshot.current_time - timedelta(hours=self.history_hours)
        end = snapshot.current_time + timedelta(hours=horizon_hours)
        published: list[StandardDataFrame] = []
        for data_type in data_types:
            records = list(
                self.source.list_available(
                    data_type,
                    start,
                    end,
                    route_id=route_id,
                    as_of=snapshot.current_time,
                )
            )
            latest = self.source.get_latest_before(
                data_type,
                snapshot.current_time,
                route_id=route_id,
                as_of=snapshot.current_time,
            )
            if latest is not None and latest.data_id not in {record.data_id for record in records}:
                records.insert(0, latest)
            if not records:
                self.events.publish(
                    MissingDataAlert(
                        route_id,
                        data_type,
                        snapshot.current_time,
                        "模拟时刻之前没有已发布且可用的数据",
                    )
                )
                continue
            for record in records:
                if self.cache.contains(record.data_id):
                    continue
                try:
                    frame = self.source.load_frame(
                        record,
                        generation_id=snapshot.generation_id,
                        as_of=snapshot.current_time,
                    )
                    selected = self.cache.put(
                        frame, simulation_time=snapshot.current_time
                    )
                except Exception as exc:
                    self.events.publish(
                        DataLoadFailureEvent(
                            route_id,
                            data_type,
                            record.data_id,
                            snapshot.current_time,
                            str(exc),
                        )
                    )
                    continue
                if selected:
                    published.append(frame.consumer_copy())
                    self.events.publish(DataArrivalEvent(record, snapshot.generation_id))
        return published

    def latest_for_b(self, route_id: str, data_type: str) -> StandardDataFrame | None:
        """Return the latest frame valid at or before the simulation clock."""

        self.cache.evict_expired_events(self.clock.now)
        return self.cache.latest(data_type, route_id=route_id, at_or_before=self.clock.now)

    def latest_forecast_for_b(self, route_id: str, data_type: str) -> StandardDataFrame | None:
        """Return the furthest cached forecast, explicitly including future valid times."""

        self.cache.evict_expired_events(self.clock.now)
        return self.cache.latest(data_type, route_id=route_id)

    def window_for_b(
        self,
        route_id: str,
        data_type: str,
        *,
        hours_before: int = 48,
        hours_after: int = 156,
    ) -> list[StandardDataFrame]:
        now = self.clock.now
        self.cache.evict_expired_events(now)
        return self.cache.get_window(
            data_type,
            now - timedelta(hours=hours_before),
            now + timedelta(hours=hours_after),
            route_id=route_id,
        )

    def prepare_window_for_b(
        self,
        *,
        route_id: str,
        data_types: list[str] | tuple[str, ...],
        start_time: datetime | None = None,
        target_horizon_hours: int = 156,
        minimum_complete_horizon_hours: int = 132,
        expected_interval_hours: Mapping[str, float | None] | None = None,
    ) -> PreparedWindow:
        snapshot = self.clock.snapshot()
        start = ensure_utc(start_time or snapshot.current_time, field="start_time")
        if start != snapshot.current_time:
            raise ValueError("prepare_window_for_b 当前要求 start_time 等于模拟时钟")
        if minimum_complete_horizon_hours > target_horizon_hours:
            raise ValueError("minimum_complete_horizon_hours 不能超过 target_horizon_hours")
        self._prefetch_at_snapshot(
            route_id=route_id,
            data_types=data_types,
            horizon_hours=target_horizon_hours,
            snapshot=snapshot,
        )
        requested_end = start + timedelta(hours=target_horizon_hours)
        minimum_end = start + timedelta(hours=minimum_complete_horizon_hours)
        interval_policy = {**_DEFAULT_INTERVAL_HOURS, **(expected_interval_hours or {})}
        frames_by_type: dict[str, tuple[StandardDataFrame, ...]] = {}
        reports: dict[str, CoverageReport] = {}
        for data_type in data_types:
            interval = interval_policy.get(data_type)
            supporting_start = (
                start - timedelta(hours=interval)
                if interval is not None
                else datetime.min.replace(tzinfo=UTC)
            )
            frames = tuple(
                self.cache.get_window(
                    data_type,
                    supporting_start,
                    requested_end,
                    route_id=route_id,
                )
            )
            frames_by_type[data_type] = frames
            reports[data_type] = _coverage_report(
                data_type=data_type,
                frames=frames,
                start=start,
                requested_end=requested_end,
                minimum_end=minimum_end,
                expected_interval_hours=interval,
            )
        final_snapshot = self.clock.snapshot()
        if (
            final_snapshot.current_time != snapshot.current_time
            or final_snapshot.generation_id != snapshot.generation_id
            or any(
                frame.generation_id != snapshot.generation_id
                for frames in frames_by_type.values()
                for frame in frames
            )
        ):
            raise StaleGenerationError(
                "prepare_window_for_b 期间模拟时刻发生推进或跳转；请重试"
            )
        return PreparedWindow(
            route_id=route_id,
            generation_id=snapshot.generation_id,
            frames=MappingProxyType(frames_by_type),
            coverage=MappingProxyType(reports),
        )

    def health(self) -> dict[str, object]:
        snapshot = self.clock.snapshot()
        self.cache.evict_expired_events(snapshot.current_time)
        return {
            "simulation_time": snapshot.current_time.isoformat(),
            "running": snapshot.running,
            "speed": snapshot.speed,
            "generation_id": snapshot.generation_id,
            "cache": self.cache.stats(),
            "categories": [category.value for category in DataCategory],
        }


def _coverage_report(
    *,
    data_type: str,
    frames: tuple[StandardDataFrame, ...],
    start: datetime,
    requested_end: datetime,
    minimum_end: datetime,
    expected_interval_hours: float | None,
) -> CoverageReport:
    times = [frame.record.valid_time for frame in frames]
    missing: list[tuple[datetime, datetime]] = []
    if expected_interval_hours is not None:
        tolerance = timedelta(hours=expected_interval_hours * 1.5)
        for lower, upper in pairwise(times):
            if upper - lower > tolerance and lower < minimum_end:
                missing.append((lower, upper))
    available_start = times[0] if times else None
    available_end = times[-1] if times else None
    if expected_interval_hours is None:
        complete = bool(frames)
    else:
        has_lower_support = any(value <= start for value in times)
        has_upper_support = any(value >= start for value in times)
        complete = (
            has_lower_support
            and has_upper_support
            and available_end is not None
            and available_end >= minimum_end
            and not missing
        )
    snapshot_ids = tuple(
        sorted(
            {
                str(frame.record.metadata.get("source_snapshot_id"))
                for frame in frames
                if frame.record.metadata.get("source_snapshot_id")
            }
        )
    )
    return CoverageReport(
        data_type=data_type,
        requested_start=start,
        requested_end=requested_end,
        minimum_required_end=minimum_end,
        available_start=available_start,
        available_end=available_end,
        expected_interval_hours=expected_interval_hours,
        missing_intervals=tuple(missing),
        source_snapshot_ids=snapshot_ids,
        complete=complete,
    )
