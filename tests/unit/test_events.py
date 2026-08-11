from datetime import UTC, datetime

from arctic_route_data.events import EventBus, MissingDataAlert


def test_bad_event_handler_does_not_block_other_subscribers():
    bus = EventBus()
    seen = []

    def bad_handler(event):
        raise RuntimeError("broken subscriber")

    bus.subscribe(MissingDataAlert, bad_handler)
    bus.subscribe(MissingDataAlert, seen.append)
    event = MissingDataAlert(
        "route-a", "wind_field", datetime(2026, 7, 15, tzinfo=UTC), "missing"
    )

    failures = bus.publish(event)

    assert seen == [event]
    assert failures[0].message == "broken subscriber"
    assert bus.failures == failures
