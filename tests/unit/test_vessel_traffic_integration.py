from datetime import UTC, datetime, timedelta

from arctic_route_data import PartitionedABCache, SimulationClock, WorkPackageA, WorkPackageAConfig
from arctic_route_data.config import (
    AcquisitionSettings,
    CacheSettings,
    ClockSettings,
    CorridorSettings,
)
from arctic_route_data.vessel_traffic import VesselTrafficSimulationSource


def _config():
    return WorkPackageAConfig(
        cache=CacheSettings(
            max_memory_mb=64,
            slow_frames_per_partition=8,
            dynamic_frames_per_partition=8,
            history_hours=144,
            target_horizon_hours=144,
            minimum_complete_horizon_hours=144,
        ),
        acquisition=AcquisitionSettings(
            gfs_step_hours=3,
            cycle_lookback_count=8,
            request_timeout_seconds=30,
        ),
        clock=ClockSettings(default_speed=1.0),
        corridors={
            "offshore_murmansk_to_offshore_dikson": CorridorSettings(
                corridor_id="offshore_murmansk_to_offshore_dikson",
                bbox=(30.0, 67.5, 85.0, 75.0),
                start=(34.00, 69.55),
                destination=(80.00, 73.80),
            ),
            "tromso_to_isfjorden_outer": CorridorSettings(
                corridor_id="tromso_to_isfjorden_outer",
                bbox=(10.0, 68.5, 22.0, 79.5),
                start=(19.00, 69.75),
                destination=(13.00, 78.15),
            ),
        },
    )


def test_vessel_traffic_source_provides_144_hour_window():
    config = _config()
    as_of = datetime(2026, 8, 6, 12, tzinfo=UTC)
    source = VesselTrafficSimulationSource.from_config(corridors=config.corridors)

    records = source.list_available(
        "vessel_traffic",
        as_of - timedelta(hours=144),
        as_of,
        route_id="offshore_murmansk_to_offshore_dikson",
        as_of=as_of,
    )

    assert len(records) == 49
    frame = source.load_frame(records[-1], generation_id=1, as_of=as_of)
    assert set(frame.payload.data_vars) == {
        "traffic_density",
        "traffic_count",
        "traffic_risk",
        "traffic_confidence",
    }
    assert float(frame.payload["traffic_risk"].min()) >= 0.0
    assert float(frame.payload["traffic_risk"].max()) <= 1.0


def test_vessel_traffic_integrates_with_work_package_a_bundle():
    config = _config()
    as_of = datetime(2026, 8, 6, 12, tzinfo=UTC)
    source = VesselTrafficSimulationSource.from_config(corridors=config.corridors)
    package = WorkPackageA(
        source=source,
        clock=SimulationClock(as_of),
        cache=PartitionedABCache(
            max_memory_mb=config.cache.max_memory_mb,
            slow_frames_per_partition=config.cache.slow_frames_per_partition,
            dynamic_frames_per_partition=config.cache.dynamic_frames_per_partition,
        ),
        history_hours=config.cache.history_hours,
    )

    frames = package._prefetch_at_snapshot(
        route_id="tromso_to_isfjorden_outer",
        data_types=["vessel_traffic"],
        horizon_hours=144,
        snapshot=package.clock.snapshot(),
        knowledge_as_of=as_of,
    )

    assert len(frames) == 49
    assert frames[0].record.valid_time == as_of - timedelta(hours=144)
    assert frames[-1].record.valid_time == as_of
    assert all(
        frame.record.metadata["source_family"] == "vessel_traffic_simulation" for frame in frames
    )
