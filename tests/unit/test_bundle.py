from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_data.bundle import DatasetBundle, record_provenance_id
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
