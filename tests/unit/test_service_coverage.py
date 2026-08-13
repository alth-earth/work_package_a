from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_data.bundle import record_provenance_id
from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.errors import FutureInformationError, StaleGenerationError
from arctic_route_data.events import DataLoadFailureEvent, EventBus
from arctic_route_data.models import DataCategory, StandardDataFrame
from arctic_route_data.service import WorkPackageA

T0 = datetime(2026, 7, 15, tzinfo=UTC)


def _provenance(snapshot_id, *, interval=None):
    metadata = {
        "source_snapshot_id": snapshot_id,
        "publication_id": "publication-a",
        "upstream_checksum": "b" * 64,
        "upstream_size_bytes": 1,
    }
    if interval is not None:
        metadata["nominal_interval_hours"] = interval
    return metadata


class _Payload(dict):
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

    def get_bracketing(self, data_type, target_time, *, route_id, as_of):
        candidates = [
            record
            for record in self.records
            if record.route_id == route_id
            and record.data_type == data_type
            and record.issue_time <= as_of
        ]
        lower = max(
            (record for record in candidates if record.valid_time <= target_time),
            key=lambda record: record.valid_time,
            default=None,
        )
        upper = min(
            (record for record in candidates if record.valid_time >= target_time),
            key=lambda record: record.valid_time,
            default=None,
        )
        return lower, upper

    def load_frame(self, record, *, generation_id, as_of):
        self.loads += 1
        return StandardDataFrame(
            record,
            _Payload({name: 0.0 for name in record.variables}),
            generation_id,
        )


class _VerifiedSource(_Source):
    """Test double for an archive-backed source that verified real evidence."""

    def verified_provenance_id(self, record):
        return record_provenance_id(record)


def _records(make_record, *, omit=()):
    return [
        make_record(
            data_id=f"frame-{'m' if offset < 0 else 'p'}{abs(offset)}",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=offset),
            metadata=_provenance("snapshot-a", interval=3.0),
        )
        for offset in range(-3, 157, 3)
        if offset not in omit
    ]


def test_prepare_window_requires_lower_bracket_and_132_hour_coverage(make_record):
    source = _VerifiedSource(_records(make_record))
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
    assert report.meets_minimum_horizon
    assert report.covers_requested_window
    assert report.provenance_complete
    assert prepared.as_of_time == T0
    assert prepared.dataset_bundle.corridor_id == "route-a"
    assert prepared.dataset_bundle.source_snapshot_ids == ("snapshot-a",)
    assert report.available_start == T0
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


def test_minimum_horizon_does_not_masquerade_as_full_requested_window(make_record):
    records = [
        make_record(
            data_id=f"frame-{offset}",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=offset),
            metadata=_provenance("snapshot-a", interval=3.0),
        )
        for offset in range(-3, 133, 3)
    ]
    service = WorkPackageA(
        source=_Source(records),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    report = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["wind_field"],
        target_horizon_hours=156,
        minimum_complete_horizon_hours=132,
    ).coverage["wind_field"]

    assert report.meets_minimum_horizon
    assert not report.covers_requested_window
    assert not report.complete


def test_manifest_nominal_interval_overrides_legacy_data_type_default(make_record):
    records = [
        make_record(
            data_id=f"ice-{offset}",
            data_type="sea_ice_concentration",
            category=DataCategory.SLOW,
            variables=("ice_concentration",),
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=offset),
            metadata=_provenance("cmems-hourly", interval=1.0),
        )
        for offset in range(-1, 157)
        if offset not in {2, 3}
    ]
    service = WorkPackageA(
        source=_Source(records),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=2),
    )

    report = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["sea_ice_concentration"],
    ).coverage["sea_ice_concentration"]

    assert report.expected_interval_hours == 1.0
    assert not report.complete
    assert report.missing_intervals == (
        (T0 + timedelta(hours=1), T0 + timedelta(hours=4)),
    )


def test_hourly_service_coverage_rejects_ninety_minute_spacing(make_record):
    records = [
        make_record(
            data_id=f"ice-{index:03d}",
            data_type="sea_ice_type",
            category=DataCategory.SLOW,
            variables=("ice_type",),
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(minutes=90 * index),
            metadata=_provenance("cmems-hourly", interval=1.0),
        )
        for index in range(105)
    ]
    service = WorkPackageA(
        source=_Source(records),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=2),
    )

    report = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["sea_ice_type"],
        target_horizon_hours=156,
        minimum_complete_horizon_hours=156,
    ).coverage["sea_ice_type"]

    assert report.expected_interval_hours == 1.0
    assert report.missing_intervals[0] == (
        T0,
        T0 + timedelta(minutes=90),
    )
    assert not report.covers_requested_window


def test_prepare_window_loads_upper_bracket_beyond_requested_end(make_record):
    source = _VerifiedSource(_records(make_record))
    service = WorkPackageA(
        source=source,
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    report = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["wind_field"],
        target_horizon_hours=155,
        minimum_complete_horizon_hours=132,
    ).coverage["wind_field"]

    assert report.available_end == T0 + timedelta(hours=156)
    assert report.covers_requested_window
    assert report.complete


def test_prepare_window_requires_valid_nonempty_request(make_record):
    service = WorkPackageA(
        source=_Source(_records(make_record)),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    with pytest.raises(ValueError, match="data_types"):
        service.prepare_window_for_b(route_id="route-a", data_types=[])
    with pytest.raises(ValueError, match="target_horizon_hours"):
        service.prepare_window_for_b(
            route_id="route-a", data_types=["wind_field"], target_horizon_hours=0
        )
    with pytest.raises(ValueError, match="正有限数"):
        service.prepare_window_for_b(
            route_id="route-a",
            data_types=["wind_field"],
            expected_interval_hours={"wind_field": 0},
        )


def test_prepare_window_keeps_historical_static_layer(make_record):
    bathymetry = make_record(
        data_id="bathymetry",
        data_type="bathymetry",
        category=DataCategory.STATIC,
        variables=("elevation",),
        issue_time=T0 - timedelta(days=200),
        valid_time=T0 - timedelta(days=100),
        metadata=_provenance("bathymetry-snapshot"),
    )
    service = WorkPackageA(
        source=_VerifiedSource([bathymetry]),
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


def test_retrospective_window_separates_simulation_time_from_knowledge_cutoff(
    make_record,
):
    issued_after_voyage = T0 + timedelta(days=20)
    layer = make_record(
        data_id="retrospective-static",
        data_type="bathymetry",
        category=DataCategory.STATIC,
        variables=("elevation",),
        issue_time=issued_after_voyage,
        valid_time=T0,
        metadata=_provenance("retrospective-static-snapshot"),
    )
    service = WorkPackageA(
        source=_VerifiedSource([layer]),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    causal = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["bathymetry"],
    )
    assert not causal.coverage["bathymetry"].complete

    service.clock.seek(T0)
    retrospective = service.prepare_window_for_b(
        route_id="route-a",
        data_types=["bathymetry"],
        knowledge_as_of=issued_after_voyage,
    )
    assert retrospective.coverage["bathymetry"].complete
    assert retrospective.as_of_time == issued_after_voyage
    assert retrospective.dataset_bundle.as_of_time == issued_after_voyage
    assert retrospective.dataset_bundle.requested_start == T0

    with pytest.raises(FutureInformationError, match="knowledge_as_of"):
        service.prepare_window_for_b(
            route_id="route-a",
            data_types=["bathymetry"],
            knowledge_as_of=T0,
        )


def test_static_layer_without_source_snapshot_is_not_provenance_complete(make_record):
    bathymetry = make_record(
        data_id="bathymetry-without-provenance",
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

    report = service.prepare_window_for_b(
        route_id="route-a", data_types=["bathymetry"]
    ).coverage["bathymetry"]

    assert report.covers_requested_window
    assert not report.provenance_complete
    assert not report.complete


def test_self_reported_provenance_without_archive_verifier_is_not_complete(make_record):
    service = WorkPackageA(
        source=_Source(_records(make_record)),
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
    )

    prepared = service.prepare_window_for_b(
        route_id="route-a", data_types=["wind_field"]
    )
    report = prepared.coverage["wind_field"]

    assert report.covers_requested_window
    assert not report.provenance_complete
    assert not report.complete
    assert report.source_snapshot_ids == ()
    assert prepared.dataset_bundle.source_snapshot_ids == ()


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


def test_prefetch_rejects_malicious_source_records_without_blocking_valid_record(
    make_record,
):
    good = make_record(data_id="good", issue_time=T0 - timedelta(hours=1))
    candidates = (
        object(),
        make_record(data_id="wrong-route", route_id="route-b"),
        make_record(data_id="wrong-type", data_type="visibility"),
        make_record(data_id="future", issue_time=T0 + timedelta(seconds=1)),
        good,
    )

    class MaliciousSource(_Source):
        def list_available(
            self, data_type, start_time, end_time, *, route_id, as_of
        ):
            return list(self.records)

        def get_latest_before(self, data_type, target_time, *, route_id, as_of):
            return None

    events = EventBus()
    failures = []
    events.subscribe(DataLoadFailureEvent, failures.append)
    source = MaliciousSource(candidates)
    service = WorkPackageA(
        source=source,
        clock=SimulationClock(T0),
        cache=PartitionedABCache(max_memory_mb=1),
        event_bus=events,
    )

    published = service.prefetch(route_id="route-a", data_types=["wind_field"])

    assert [frame.record.data_id for frame in published] == ["good"]
    assert source.loads == 1
    assert {failure.data_id for failure in failures} == {
        "<invalid-object>",
        "wrong-route",
        "wrong-type",
        "future",
        "<end-bracketing-query>",
    }


@pytest.mark.parametrize(
    "bad_result",
    [
        "record",
        "generation",
        "payload",
        "empty_payload",
        "none_payload",
        "nan_payload",
        "string_payload",
    ],
)
def test_prefetch_rejects_frame_that_does_not_match_requested_snapshot(
    make_record, bad_result
):
    requested = make_record(data_id="requested", issue_time=T0 - timedelta(hours=1))
    replacement = make_record(data_id="replacement", issue_time=T0 - timedelta(hours=1))

    class BadFrameSource(_Source):
        def load_frame(self, record, *, generation_id, as_of):
            self.loads += 1
            if bad_result == "record":
                return StandardDataFrame(
                    replacement,
                    _Payload({name: 0.0 for name in replacement.variables}),
                    generation_id,
                )
            if bad_result == "payload":
                return StandardDataFrame(record, object(), generation_id)
            if bad_result == "empty_payload":
                return StandardDataFrame(record, {}, generation_id)
            if bad_result == "none_payload":
                return StandardDataFrame(
                    record,
                    _Payload({name: None for name in record.variables}),
                    generation_id,
                )
            if bad_result == "nan_payload":
                return StandardDataFrame(
                    record,
                    _Payload({name: float("nan") for name in record.variables}),
                    generation_id,
                )
            if bad_result == "string_payload":
                return StandardDataFrame(
                    record,
                    _Payload({name: "not-numeric" for name in record.variables}),
                    generation_id,
                )
            return StandardDataFrame(
                record,
                _Payload({name: 0.0 for name in record.variables}),
                generation_id + 1,
            )

    events = EventBus()
    failures = []
    events.subscribe(DataLoadFailureEvent, failures.append)
    cache = PartitionedABCache(max_memory_mb=1)
    service = WorkPackageA(
        source=BadFrameSource([requested]),
        clock=SimulationClock(T0),
        cache=cache,
        event_bus=events,
    )

    assert service.prefetch(route_id="route-a", data_types=["wind_field"]) == []
    assert not cache.contains("requested")
    assert len(failures) == 1
    assert failures[0].data_id == "requested"
