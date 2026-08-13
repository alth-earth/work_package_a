"""Deterministic simulation clock; UI wall time never leaks into model time."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

from arctic_route_data.timeutils import ensure_utc


@dataclass(frozen=True, slots=True)
class ClockSnapshot:
    current_time: datetime
    speed: float
    running: bool
    generation_id: int


SeekListener = Callable[[ClockSnapshot], None]


class SimulationClock:
    def __init__(self, start_time: datetime, *, speed: float = 1.0, running: bool = False) -> None:
        if speed <= 0:
            raise ValueError("speed 必须大于 0")
        self._current_time = ensure_utc(start_time, field="start_time")
        self._speed = float(speed)
        self._running = running
        self._generation_id = 0
        self._seek_listeners: list[SeekListener] = []
        self._lock = RLock()

    def snapshot(self) -> ClockSnapshot:
        with self._lock:
            return ClockSnapshot(
                self._current_time, self._speed, self._running, self._generation_id
            )

    @property
    def now(self) -> datetime:
        return self.snapshot().current_time

    @property
    def generation_id(self) -> int:
        return self.snapshot().generation_id

    def play(self) -> None:
        with self._lock:
            self._running = True

    def pause(self) -> None:
        with self._lock:
            self._running = False

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("speed 必须大于 0")
        with self._lock:
            self._speed = float(speed)

    def tick(self, real_elapsed: timedelta | float) -> ClockSnapshot:
        seconds = (
            real_elapsed.total_seconds()
            if isinstance(real_elapsed, timedelta)
            else float(real_elapsed)
        )
        if seconds < 0:
            raise ValueError("real_elapsed 不能为负数；倒退请使用 seek()")
        with self._lock:
            if self._running:
                self._current_time += timedelta(seconds=seconds * self._speed)
            return self.snapshot()

    def seek(self, new_time: datetime) -> ClockSnapshot:
        with self._lock:
            self._current_time = ensure_utc(new_time, field="new_time")
            self._generation_id += 1
            snapshot = self.snapshot()
            listeners = tuple(self._seek_listeners)
        for listener in listeners:
            listener(snapshot)
        return snapshot

    def subscribe_seek(self, listener: SeekListener) -> Callable[[], None]:
        with self._lock:
            self._seek_listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                # Cleanup callbacks are deliberately idempotent so independent
                # A/B owners can safely unwind the same orchestration scope.
                if listener in self._seek_listeners:
                    self._seek_listeners.remove(listener)

        return unsubscribe
