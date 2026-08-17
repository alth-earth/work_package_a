from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_data.bundle import (
    DatasetBundle,
    _build_coverage,
    _bundle_digest,
    record_provenance_id,
)
from arctic_route_data.errors import MetadataValidationError

T0 = datetime(2026, 8, 11, 12, tzinfo=UTC)


def _provenance(snapshot_id):
    return {
        "source_snapshot_id": snapshot_id,
        "publication_id": "publication-a",
        "upstream_checksum": "b" * 64,
        "upstream_size_bytes": 1,
    }


def _bundle(make_record, records, *, verified=True):
    verified_ids = {
        record.data_id: provenance_id
        for record in records
        if verified
        and (provenance_id := record_provenance_id(record)) is not None
    }
    return DatasetBundle.create(
        corridor_id="route-a",
        as_of_time=T0,
        requested_start=T0,
        requested_end=T0 + timedelta(hours=156),
        minimum_required_end=T0 + timedelta(hours=132),
        requested_data_types=("wave", "wind_field"),
        records=tuple(records),
        verified_provenance_ids=verified_ids,
        expected_interval_hours={"wave": 3.0, "wind_field": 3.0},
    )


def test_dataset_bundle_digest_is_order_independent_and_covers_exact_records(
    make_record,
):
    first = make_record(
        data_id="frame-a",
        data_type="wind_field",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
        metadata=_provenance("gfs-cycle-a"),
    )
    second = make_record(
        data_id="frame-b",
        data_type="wave",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0 + timedelta(hours=3),
        metadata=_provenance("cmems-a"),
    )

    forward = _bundle(make_record, [first, second])
    reverse = _bundle(make_record, [second, first])

    assert forward.bundle_id == reverse.bundle_id
    assert forward.bundle_digest == reverse.bundle_digest
    assert forward.source_snapshot_ids == ("cmems-a", "gfs-cycle-a")
    assert forward.to_dict()["record_count"] == 2
    assert forward.schema_version == "a.dataset-bundle.v2"
    assert len(forward.coverage) == 2

    changed = _bundle(
        make_record,
        [
            first,
            make_record(
                data_id="frame-c",
                data_type="wave",
                issue_time=T0 - timedelta(hours=1),
                valid_time=T0 + timedelta(hours=3),
                metadata=_provenance("cmems-a"),
            ),
        ],
    )
    assert changed.bundle_digest != forward.bundle_digest


def test_bundle_accepts_logical_cutoff_later_than_max_selected_issue_time(
    make_record,
) -> None:
    """The bundle contract expresses knowledge_as_of independently of the
    newest selected source issue_time (only issue_time <= as_of is enforced)."""

    records = [
        make_record(
            data_id="frame-a",
            data_type="wind_field",
            issue_time=T0 - timedelta(hours=2),
            valid_time=T0,
            metadata=_provenance("gfs-cycle-a"),
        ),
        make_record(
            data_id="frame-b",
            data_type="wave",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=3),
            metadata=_provenance("cmems-a"),
        ),
    ]
    logical_cutoff = T0 + timedelta(hours=1)
    verified_ids = {
        record.data_id: record_provenance_id(record) for record in records
    }
    bundle = DatasetBundle.create(
        corridor_id="route-a",
        as_of_time=logical_cutoff,
        requested_start=T0,
        requested_end=T0 + timedelta(hours=156),
        minimum_required_end=T0 + timedelta(hours=132),
        requested_data_types=("wave", "wind_field"),
        records=tuple(records),
        verified_provenance_ids=verified_ids,
        expected_interval_hours={"wave": 3.0, "wind_field": 3.0},
    )
    assert bundle.as_of_time == logical_cutoff
    assert max(record.issue_time for record in records) < logical_cutoff
    round_trip = DatasetBundle.from_dict(bundle.to_dict())
    assert round_trip.as_of_time == logical_cutoff


def test_dataset_bundle_rejects_future_or_cross_corridor_records(make_record):
    future = make_record(
        data_id="future",
        issue_time=T0 + timedelta(seconds=1),
        valid_time=T0,
    )
    with pytest.raises(MetadataValidationError, match="as_of_time"):
        _bundle(make_record, [future])

    other = make_record(
        data_id="other",
        route_id="route-b",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
    )
    with pytest.raises(MetadataValidationError, match="corridor"):
        _bundle(make_record, [other])


def test_dataset_bundle_round_trip_verifies_count_and_digest(make_record):
    record = make_record(
        data_id="frame-a",
        data_type="wave",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
        metadata=_provenance("cmems-a"),
    )
    payload = _bundle(make_record, [record]).to_dict()

    restored = DatasetBundle.from_dict(payload)

    assert restored.to_dict() == payload

    wrong_count = dict(payload)
    wrong_count["record_count"] = 2
    with pytest.raises(MetadataValidationError, match="record_count"):
        DatasetBundle.from_dict(wrong_count)

    tampered = dict(payload)
    tampered["bundle_digest"] = "0" * 64
    with pytest.raises(MetadataValidationError, match="digest"):
        DatasetBundle.from_dict(tampered)


def test_malformed_snapshot_metadata_is_not_stringified(make_record):
    record = make_record(
        data_id="malformed-provenance",
        data_type="wave",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
        metadata={
            "source_snapshot_id": {"not": "a string"},
            "publication_id": "publication-a",
            "upstream_checksum": "b" * 64,
            "upstream_size_bytes": 1,
        },
    )

    bundle = _bundle(make_record, [record])

    assert bundle.records[0].source_snapshot_id is None
    assert bundle.source_snapshot_ids == ()


def test_unverified_metadata_cannot_claim_bundle_provenance(make_record):
    record = make_record(
        data_id="self-reported",
        data_type="wave",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
        metadata=_provenance("cmems-self-reported"),
    )

    bundle = _bundle(make_record, [record], verified=False)

    assert bundle.records[0].source_snapshot_id is None
    assert bundle.source_snapshot_ids == ()


def test_v2_coverage_is_recomputed_and_cannot_fake_complete(make_record):
    records = [
        make_record(
            data_id=f"wave-{offset}",
            data_type="wave",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=offset),
            metadata=_provenance("cmems-a"),
        )
        for offset in (0, 3, 6, 156)
    ]
    payload = _bundle(make_record, records).to_dict()
    wave = next(item for item in payload["coverage"] if item["data_type"] == "wave")
    assert wave["complete"] is False
    assert wave["missing_intervals"]

    wave["complete"] = True
    with pytest.raises(MetadataValidationError, match="重算结果"):
        DatasetBundle.from_dict(payload)


def test_v2_rejects_ninety_minute_spacing_for_hourly_cadence(make_record):
    records = [
        make_record(
            data_id=f"ice-{index:03d}",
            data_type="sea_ice_type",
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(minutes=90 * index),
            metadata=_provenance("cmems-hourly"),
        )
        for index in range(105)
    ]
    verified = {
        record.data_id: record_provenance_id(record)
        for record in records
        if record_provenance_id(record) is not None
    }
    bundle = DatasetBundle.create(
        corridor_id="route-a",
        as_of_time=T0,
        requested_start=T0,
        requested_end=T0 + timedelta(hours=156),
        minimum_required_end=T0 + timedelta(hours=132),
        requested_data_types=("sea_ice_type",),
        records=tuple(records),
        verified_provenance_ids=verified,
        expected_interval_hours={"sea_ice_type": 1.0},
    )

    assert bundle.coverage[0].missing_intervals
    assert bundle.coverage[0].complete is False


def test_complete_v2_is_independently_accepted_by_shared_contracts(make_record):
    records = [
        make_record(
            data_id=f"{data_type}-{offset:03d}",
            data_type=data_type,
            issue_time=T0 - timedelta(hours=1),
            valid_time=T0 + timedelta(hours=offset),
            metadata=_provenance(f"snapshot-{data_type}"),
        )
        for data_type in ("wave", "wind_field")
        for offset in range(0, 157, 3)
    ]

    bundle = _bundle(make_record, records)

    from arctic_route_contracts import verify_dataset_bundle

    identity = verify_dataset_bundle(bundle.to_dict())
    assert identity.schema_version == "a.dataset-bundle.v2"
    assert identity.coverage_complete is True
    assert identity.formal_run_eligible is True


def test_empty_v2_cannot_claim_formal_coverage(make_record):
    bundle = _bundle(make_record, [])

    assert bundle.coverage
    assert all(not proof.complete for proof in bundle.coverage)

    from arctic_route_contracts import verify_dataset_bundle

    identity = verify_dataset_bundle(bundle.to_dict())
    assert identity.record_count == 0
    assert identity.coverage_complete is False
    assert identity.formal_run_eligible is False


def test_formal_v2_enforces_shared_cadence_while_v1_remains_readable(make_record):
    record = make_record(
        data_id="current-at-start",
        data_type="ocean_current",
        valid_time=T0,
        metadata=_provenance("cmems-current"),
    )
    verified = {record.data_id: record_provenance_id(record)}
    common = {
        "corridor_id": "route-a",
        "as_of_time": T0,
        "requested_start": T0,
        "requested_end": T0,
        "minimum_required_end": T0,
        "requested_data_types": ("ocean_current",),
        "records": (record,),
        "verified_provenance_ids": verified,
    }

    with pytest.raises(MetadataValidationError, match="共享正式合同"):
        DatasetBundle.create(
            **common,
            expected_interval_hours={"ocean_current": 6.0},
        )
    with pytest.raises(MetadataValidationError, match="正数或 null"):
        DatasetBundle.create(
            **common,
            expected_interval_hours={"ocean_current": True},
        )

    valid = DatasetBundle.create(
        **common,
        expected_interval_hours={"ocean_current": 1.0},
    )
    from arctic_route_contracts import verify_dataset_bundle

    assert verify_dataset_bundle(valid.to_dict()).formal_run_eligible
    invalid_coverage = _build_coverage(
        data_type="ocean_current",
        records=valid.records,
        requested_start=T0,
        requested_end=T0,
        minimum_required_end=T0,
        expected_interval_hours=6.0,
    )
    invalid_digest = _bundle_digest(
        schema_version="a.dataset-bundle.v2",
        corridor_id=valid.corridor_id,
        as_of_time=valid.as_of_time,
        requested_start=valid.requested_start,
        requested_end=valid.requested_end,
        minimum_required_end=valid.minimum_required_end,
        requested_data_types=valid.requested_data_types,
        source_snapshot_ids=valid.source_snapshot_ids,
        records=valid.records,
        coverage=(invalid_coverage,),
    )
    invalid_payload = valid.to_dict()
    invalid_payload["coverage"] = [invalid_coverage.to_dict()]
    invalid_payload["bundle_digest"] = invalid_digest
    invalid_payload["bundle_id"] = f"a-bundle-{invalid_digest[:24]}"
    with pytest.raises(MetadataValidationError, match="共享正式合同"):
        DatasetBundle.from_dict(invalid_payload)

    legacy_payload = dict(invalid_payload)
    legacy_payload.pop("coverage")
    legacy_payload["schema_version"] = "a.dataset-bundle.v1"
    legacy_digest = _bundle_digest(
        schema_version="a.dataset-bundle.v1",
        corridor_id=valid.corridor_id,
        as_of_time=valid.as_of_time,
        requested_start=valid.requested_start,
        requested_end=valid.requested_end,
        minimum_required_end=valid.minimum_required_end,
        requested_data_types=valid.requested_data_types,
        source_snapshot_ids=valid.source_snapshot_ids,
        records=valid.records,
    )
    legacy_payload["bundle_digest"] = legacy_digest
    legacy_payload["bundle_id"] = f"a-bundle-{legacy_digest[:24]}"

    assert DatasetBundle.from_dict(legacy_payload).schema_version == "a.dataset-bundle.v1"


def test_v1_bundle_remains_readable_as_legacy(make_record):
    record = make_record(
        data_id="frame-a",
        data_type="wave",
        issue_time=T0 - timedelta(hours=1),
        valid_time=T0,
        metadata=_provenance("cmems-a"),
    )
    bundle = _bundle(make_record, [record])
    payload = bundle.to_dict()
    payload.pop("coverage")
    payload["schema_version"] = "a.dataset-bundle.v1"
    from arctic_route_data.bundle import _bundle_digest

    digest = _bundle_digest(
        schema_version="a.dataset-bundle.v1",
        corridor_id=bundle.corridor_id,
        as_of_time=bundle.as_of_time,
        requested_start=bundle.requested_start,
        requested_end=bundle.requested_end,
        minimum_required_end=bundle.minimum_required_end,
        requested_data_types=bundle.requested_data_types,
        source_snapshot_ids=bundle.source_snapshot_ids,
        records=bundle.records,
    )
    payload["bundle_digest"] = digest
    payload["bundle_id"] = f"a-bundle-{digest[:24]}"

    restored = DatasetBundle.from_dict(payload)

    assert restored.schema_version == "a.dataset-bundle.v1"
    assert restored.coverage == ()
