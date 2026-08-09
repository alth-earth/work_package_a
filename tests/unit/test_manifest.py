from datetime import UTC, datetime, timedelta

from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import QualityFlag

UTC = UTC
T0 = datetime(2026, 7, 15, 12, tzinfo=UTC)


def test_as_of_filter_prevents_future_information(tmp_path, make_record):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    available = make_record(
        data_id="available",
        valid_time=T0 + timedelta(hours=6),
        issue_time=T0 - timedelta(hours=1),
    )
    leaked = make_record(
        data_id="future-release",
        valid_time=T0 + timedelta(hours=1),
        issue_time=T0 + timedelta(hours=2),
    )
    store.register_many((available, leaked))
    result = store.list_available(
        "wind_field",
        T0,
        T0 + timedelta(hours=24),
        route_id="route-a",
        as_of=T0,
    )
    assert [record.data_id for record in result] == ["available"]


def test_version_resolution_prefers_quality_then_latest_issue(tmp_path, make_record):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    valid = T0 + timedelta(hours=6)
    store.register_many(
        (
            make_record(
                data_id="late-degraded",
                valid_time=valid,
                issue_time=T0,
                quality_flag=QualityFlag.DEGRADED,
            ),
            make_record(
                data_id="early-good",
                valid_time=valid,
                issue_time=T0 - timedelta(hours=1),
                quality_flag=QualityFlag.GOOD,
            ),
        )
    )
    result = store.list_available(
        "wind_field", T0, valid, route_id="route-a", as_of=T0
    )
    assert [record.data_id for record in result] == ["early-good"]


def test_bracketing_only_uses_already_issued_frames(tmp_path, make_record):
    store = ManifestStore(tmp_path / "manifest.sqlite3")
    for offset in (-6, 6, 12):
        store.register(
            make_record(
                data_id=f"frame-{offset}",
                valid_time=T0 + timedelta(hours=offset),
                issue_time=T0 - timedelta(hours=1)
                if offset != 12
                else T0 + timedelta(hours=1),
            )
        )
    lower, upper = store.get_bracketing(
        "wind_field", T0, route_id="route-a", as_of=T0
    )
    assert lower and lower.data_id == "frame--6"
    assert upper and upper.data_id == "frame-6"
