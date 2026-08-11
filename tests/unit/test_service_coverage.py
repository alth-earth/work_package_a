from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.errors import StaleGenerationError
from arctic_route_data.models import DataCategory, StandardDataFrame
from arctic_route_data.service import WorkPackageA

T0 = datetime(2026, 7, 15, tzinfo=UTC)


class _Payload:
    nbytes = 1


class _Source:
    def __init__(self, records):
        self.records = tuple(records)
        self.loads = 0

    def list_available(self, data_type, start_time, end_time, *, route_id, as_of):
        return [
            record
            for record in self.records
            if record.route_id == route_id
            and record.data_type == data_type
            and start_time <= record.valid_time <= end_time
            and record.issue_time <= as_of
        ]

    def get_latest_before(self, data_type, target_time, *, route_id, as_of):
        candidates = [
            record
            for record in self.records
            if record.route_id == route_id
            and record.data_type == data_type
            and record.valid_time <= target_time
            and record.issue_time <= as_of
        ]
        return max(candidates, key=lambda record: record.valid_time, default=None)

    def load_frame(self, record, *, generation_id, as_of):
        self.loads += 1
        return StandardDataFrame(record, _Payload(), generation_id)


def _records(make_record, *, omit=()):
    return [
        make_record(
            data_id=f"frame-{'m' if offset < 0 else 'p'}{abs(offset)}",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=offset),
        )
        for offset in range(-3, 157, 3)
        if offset not in omit
    ]


def test_prepare_window_requires_lower_bracket_and_132_hour_coverage(make_record):
    source = _Source(_records(make_record))
    service = WorkPackageA(
        source=source,
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    prepared = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["wind_field"],
        target_horizon_hours=156,
        minimum_complete_horizon_hours=132,
    )

    report = prepared.coverage["wind_field"]
    assert report.complete
    assert report.available_start == T0 - timedelta(hours=3)
    assert report.available_end == T0 + timedelta(hours=156)
    assert source.loads == len(source.records)

    service.prefetch(route_id="route-a", data_types=["wind_field"])
    assert source.loads == len(source.records)


def test_prepare_window_reports_internal_gap(make_record):
    source = _Source(_records(make_record, omit=(60, 63)))
    service = WorkPackageA(
        source=source,
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    prepared = service.prepare_window_for_b(
        route_id="route-a", data_types=["wind_field"]
    )

    report = prepared.coverage["wind_field"]
    assert not report.complete
    assert report.missing_intervals == (
        (T0 + timedelta(hours=57), T0 + timedelta(hours=66)),
    )


def test_prepare_window_keeps_historical_static_layer(make_record):
    bathymetry = make_record(
        data_id="bathymetry",
        data_type="bathymetry",
        category=DataCategory.STATIC,
        variables=("elevation",),
        issue_time=T0 - timedelta(days=200),
        valid_time=T0 - timedelta(days=100),
    )
    service = WorkPackageA(
        source=_Source([bathymetry]),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    prepared = service.prepare_window_for_b(
        route_id="route-a", data_types=["bathymetry"]
    )

    assert [frame.record.data_id for frame in prepared.frames["bathymetry"]] == [
        "bathymetry"
    ]
    assert prepared.coverage["bathymetry"].complete


def test_expired_event_disappears_after_clock_advances_without_new_put(make_record):
    event = make_record(
        data_id="event",
        data_type="long_term_restricted_area",
        category=DataCategory.EVENT,
        variables=("restricted_area",),
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
        metadata={"end_time": (T0 + timedelta(hours=1)).isoformat()},
    )
    clock = SimulationClock(T0)
    service = WorkPackageA(
        source=_Source([event]),
        clock=clock,
        cache=PartitionedABCache(max_memory_mb=1),
    )
    service.prefetch(route_id="route-a", data_types=["long_term_restricted_area"])
    assert service.latest_for_b("route-a", "long_term_restricted_area") is not None

    clock.play()
    clock.tick(timedelta(hours=2))

    assert service.latest_for_b("route-a", "long_term_restricted_area") is None


def test_prepare_window_reuses_initial_as_of_and_rejects_clock_tick(make_record):
    clock = SimulationClock(T0)
    future_revision = make_record(
        data_id="future-revision",
        issue_time=T0 + timedelta(minutes=30),
        valid_time=T0,
    )

    class TickingSource(_Source):
        def list_available(
            self, data_type, start_time, end_time, *, route_id, as_of
        ):
            clock.play()
            clock.tick(timedelta(hours=1))
            return super().list_available(
                data_type,
                start_time,
                end_time,
                route_id=route_id,
                as_of=as_of,
            )

    cache = PartitionedABCache(max_memory_mb=1)
    service = WorkPackageA(source=TickingSource([future_revision]), clock=clock, cache=cache)

    with pytest.raises(StaleGenerationError, match="推进或跳转"):
        service.prepare_window_for_b(route_id="route-a", data_types=["wind_field"])

    assert not cache.contains("future-revision")
