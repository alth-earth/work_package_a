from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.errors import StaleGenerationError
from arctic_route_data.models import DataCategory, StandardDataFrame


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
        "sea_ice_concentration", T0, T0 + timedelta(days=3)
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
    assert cache.latest("bathymetry").generation_id == 1
    assert cache.latest("wind_field") is None
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
    assert cache.latest("bathymetry") is None


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
    assert cache.latest("bathymetry") is None


def test_leased_frame_is_not_evicted(make_record):
    cache = PartitionedABCache(max_memory_mb=0.0001, dynamic_frames_per_partition=10)
    first = StandardDataFrame(make_record(data_id="first"), Payload(80), 0)
    second = StandardDataFrame(
        make_record(data_id="second", valid_time=T0 + timedelta(hours=1)), Payload(80), 0
    )
    cache.put(first)
    with cache.lease("first"):
        cache.put(second)
        assert cache.latest("wind_field").record.data_id == "first"
