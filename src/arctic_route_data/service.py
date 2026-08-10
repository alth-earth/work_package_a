"""Work package A orchestrator: clock-aware prefetch and AB publication."""

from __future__ import annotations

from datetime import timedelta

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import ClockSnapshot, SimulationClock
from arctic_route_data.events import (
    DataArrivalEvent,
    EventBus,
    GenerationChangedEvent,
    MissingDataAlert,
)
from arctic_route_data.models import DataCategory, StandardDataFrame
from arctic_route_data.sources import DataSource


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
        horizon_hours: int = 24,
    ) -> list[StandardDataFrame]:
        snapshot = self.clock.snapshot()
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
                frame = self.source.load_frame(
                    record,
                    generation_id=snapshot.generation_id,
                    as_of=snapshot.current_time,
                )
                self.cache.put(frame, simulation_time=snapshot.current_time)
                published.append(frame)
                self.events.publish(DataArrivalEvent(record, snapshot.generation_id))
        return published

    def latest_for_b(self, data_type: str) -> StandardDataFrame | None:
        return self.cache.latest(data_type)

    def window_for_b(
        self, data_type: str, *, hours_before: int = 48, hours_after: int = 24
    ) -> list[StandardDataFrame]:
        now = self.clock.now
        return self.cache.get_window(
            data_type,
            now - timedelta(hours=hours_before),
            now + timedelta(hours=hours_after),
        )

    def health(self) -> dict[str, object]:
        snapshot = self.clock.snapshot()
        return {
            "simulation_time": snapshot.current_time.isoformat(),
            "running": snapshot.running,
            "speed": snapshot.speed,
            "generation_id": snapshot.generation_id,
            "cache": self.cache.stats(),
            "categories": [category.value for category in DataCategory],
        }
