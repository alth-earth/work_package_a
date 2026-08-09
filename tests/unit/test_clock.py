from datetime import UTC, datetime, timedelta

from arctic_route_data.clock import SimulationClock


def test_clock_play_pause_speed_and_seek_generation():
    start = datetime(2026, 7, 15, tzinfo=UTC)
    clock = SimulationClock(start, speed=60)
    clock.tick(10)
    assert clock.now == start
    clock.play()
    clock.tick(10)
    assert clock.now == start + timedelta(minutes=10)
    clock.pause()
    seen = []
    clock.subscribe_seek(seen.append)
    snapshot = clock.seek(start + timedelta(days=1))
    assert snapshot.generation_id == 1
    assert seen == [snapshot]
