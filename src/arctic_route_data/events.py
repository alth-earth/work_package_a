"""Small synchronous event bus for A/B integration and operational alerts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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


EventHandler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: type[Any], handler: EventHandler) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Any) -> None:
        for handler in tuple(self._handlers[type(event)]):
            handler(event)
