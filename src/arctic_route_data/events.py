"""Small synchronous event bus for A/B integration and operational alerts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from arctic_route_data.models import ManifestRecord


@dataclass(frozen=True, slots=True)
class DataArrivalEvent:
    record: ManifestRecord
    generation_id: int


@dataclass(frozen=True, slots=True)
class MissingDataAlert:
    route_id: str
    data_type: str
    simulation_time: datetime
    message: str


@dataclass(frozen=True, slots=True)
class GenerationChangedEvent:
    generation_id: int
    simulation_time: datetime


@dataclass(frozen=True, slots=True)
class DataLoadFailureEvent:
    route_id: str
    data_type: str
    data_id: str
    simulation_time: datetime
    message: str


@dataclass(frozen=True, slots=True)
class EventHandlerFailure:
    event_type: str
    handler: str
    message: str


EventHandler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[EventHandler]] = defaultdict(list)
        self._failures: list[EventHandlerFailure] = []
        self._lock = RLock()

    def subscribe(self, event_type: type[Any], handler: EventHandler) -> Callable[[], None]:
        with self._lock:
            self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Any) -> tuple[EventHandlerFailure, ...]:
        with self._lock:
            handlers = tuple(self._handlers[type(event)])
        failures: list[EventHandlerFailure] = []
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                failure = EventHandlerFailure(
                    event_type=type(event).__name__,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    message=str(exc),
                )
                failures.append(failure)
        if failures:
            with self._lock:
                self._failures.extend(failures)
        return tuple(failures)

    @property
    def failures(self) -> tuple[EventHandlerFailure, ...]:
        with self._lock:
            return tuple(self._failures)
