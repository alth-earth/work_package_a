from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.errors import (
    CacheCapacityError,
    FutureInformationError,
    StaleGenerationError,
)
from arctic_route_data.models import DataCategory, QualityFlag, StandardDataFrame


class Payload:
    def __init__(self, nbytes: int = 64):
        self.nbytes = nbytes


T0 = datetime(2026, 7, 15, tzinfo=UTC)


def test_slow_partition_retains_latest_two(make_record):
    cache = PartitionedABCache(max_memory_mb=1, slow_frames_per_partition=2)
    for index in range(3):
        record = make_record(
            data_id=f"slow-{index}",
            data_type="sea_ice_concentration",
            category=DataCategory.SLOW,
            variables=("ice_concentration",),
            valid_time=T0 + timedelta(days=index),
        )
        cache.put(StandardDataFrame(record, Payload(), 0))
    frames = cache.get_window(
        "sea_ice_concentration", T0, T0 + timedelta(days=3), route_id="route-a"
    )
    assert [frame.record.data_id for frame in frames] == ["slow-1", "slow-2"]


def test_seek_keeps_static_and_rejects_late_old_generation(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    static = StandardDataFrame(
        make_record(
            data_id="static",
            data_type="bathymetry",
            category=DataCategory.STATIC,
            variables=("elevation",),
        ),
        Payload(),
        0,
    )
    dynamic = StandardDataFrame(make_record(data_id="dynamic"), Payload(), 0)
    cache.put(static)
    cache.put(dynamic)
    cache.reset_generation(1, simulation_time=T0)
    assert cache.latest("bathymetry", route_id="route-a").generation_id == 1
    assert cache.latest("wind_field", route_id="route-a") is None
    with pytest.raises(StaleGenerationError):
        cache.put(dynamic)


def test_seek_backwards_drops_static_published_after_target(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    static = StandardDataFrame(
        make_record(
            data_id="future-static",
            data_type="bathymetry",
            category=DataCategory.STATIC,
            variables=("elevation",),
            issue_time=T0 + timedelta(hours=1),
        ),
        Payload(),
        0,
    )
    cache.put(static)

    cache.reset_generation(1, simulation_time=T0)

    assert cache.generation_id == 1
    assert cache.latest("bathymetry", route_id="route-a") is None


def test_put_rejects_record_published_after_supplied_simulation_time(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    future = StandardDataFrame(
        make_record(
            data_id="future",
            issue_time=T0 + timedelta(seconds=1),
        ),
        Payload(),
        0,
    )

    with pytest.raises(FutureInformationError, match="未来帧"):
        cache.put(future, simulation_time=T0)

    assert not cache.contains("future")


def test_knowledge_cutoff_does_not_replace_simulation_time_for_event_expiry(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    event = StandardDataFrame(
        make_record(
            data_id="retrospective-event",
            data_type="long_term_restricted_area",
            category=DataCategory.EVENT,
            variables=("restricted_area",),
            issue_time=T0 + timedelta(days=2),
            valid_time=T0,
            metadata={"end_time": (T0 + timedelta(hours=6)).isoformat()},
        ),
        Payload(),
        0,
    )

    assert cache.put(
        event,
        simulation_time=T0,
        knowledge_as_of=T0 + timedelta(days=3),
    )
    assert cache.contains("retrospective-event")


def test_reset_generation_without_simulation_time_clears_static(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    static = StandardDataFrame(
        make_record(
            data_id="static",
            data_type="bathymetry",
            category=DataCategory.STATIC,
            variables=("elevation",),
        ),
        Payload(),
        0,
    )
    cache.put(static)

    cache.reset_generation(1)

    assert cache.generation_id == 1
    assert cache.latest("bathymetry", route_id="route-a") is None


def test_leased_frame_is_not_evicted(make_record):
    cache = PartitionedABCache(max_memory_mb=0.0001, dynamic_frames_per_partition=10)
    first = StandardDataFrame(make_record(data_id="first"), Payload(80), 0)
    second = StandardDataFrame(
        make_record(data_id="second", valid_time=T0 + timedelta(hours=1)), Payload(80), 0
    )
    cache.put(first)
    with cache.lease("first"):
        with pytest.raises(CacheCapacityError):
            cache.put(second)
        assert cache.latest("wind_field", route_id="route-a").record.data_id == "first"


def test_routes_are_strictly_isolated(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    cache.put(StandardDataFrame(make_record(data_id="route-a-frame"), Payload(), 0))
    cache.put(
        StandardDataFrame(
            make_record(data_id="route-b-frame", route_id="route-b"), Payload(), 0
        )
    )

    assert cache.latest("wind_field", route_id="route-a").record.data_id == "route-a-frame"
    assert cache.latest("wind_field", route_id="route-b").record.data_id == "route-b-frame"


def test_newer_revision_replaces_same_valid_time(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    old = StandardDataFrame(make_record(data_id="old"), Payload(), 0)
    new = StandardDataFrame(
        make_record(
            data_id="new",
            issue_time=T0 + timedelta(hours=1),
            version="v2",
        ),
        Payload(),
        0,
    )
    cache.put(old)
    assert cache.put(new)

    frames = cache.get_window(
        "wind_field", T0, T0 + timedelta(hours=12), route_id="route-a"
    )
    assert [frame.record.data_id for frame in frames] == ["new"]


def test_latest_at_or_before_never_returns_future(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    for offset in (-3, 6):
        cache.put(
            StandardDataFrame(
                make_record(
                    data_id=f"frame-{offset}", valid_time=T0 + timedelta(hours=offset)
                ),
                Payload(),
                0,
            )
        )

    latest = cache.latest("wind_field", route_id="route-a", at_or_before=T0)
    assert latest is not None
    assert latest.record.data_id == "frame--3"


def test_quality_wins_revision_resolution(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    good = StandardDataFrame(make_record(data_id="good"), Payload(), 0)
    degraded = StandardDataFrame(
        make_record(
            data_id="degraded",
            issue_time=T0 + timedelta(hours=2),
            quality_flag=QualityFlag.DEGRADED,
        ),
        Payload(),
        0,
    )
    cache.put(good)

    assert not cache.put(degraded)
    assert cache.latest("wind_field", route_id="route-a").record.data_id == "good"


def test_leased_frame_is_hidden_when_partition_limit_selects_newer_frame(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    old = StandardDataFrame(
        make_record(
            data_id="old-static",
            data_type="bathymetry",
            category=DataCategory.STATIC,
            variables=("elevation",),
            valid_time=T0,
        ),
        Payload(),
        0,
    )
    new = StandardDataFrame(
        make_record(
            data_id="new-static",
            data_type="bathymetry",
            category=DataCategory.STATIC,
            variables=("elevation",),
            valid_time=T0 + timedelta(hours=1),
        ),
        Payload(),
        0,
    )
    cache.put(old)

    with cache.lease("old-static") as leased:
        cache.put(new)
        assert leased.record.data_id == "old-static"
        assert cache.latest("bathymetry", route_id="route-a").record.data_id == (
            "new-static"
        )

    assert cache.stats()["leased_inactive_frames"] == 0


def test_cache_isolates_xarray_container_from_producer(make_record):
    cache = PartitionedABCache(max_memory_mb=1)
    dataset = xr.Dataset(
        {
            "wind_u10": ("x", np.array([1.0])),
            "wind_v10": ("x", np.array([2.0])),
        }
    )
    frame = StandardDataFrame(make_record(), dataset, 0)
    cache.put(frame)

    frame.payload["producer_only"] = ("x", [3.0])

    cached = cache.latest("wind_field", route_id="route-a")
    assert cached is not None
    assert "producer_only" not in cached.payload
