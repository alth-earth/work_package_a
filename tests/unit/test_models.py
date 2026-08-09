from datetime import UTC, datetime

import pytest

from arctic_route_data.errors import MetadataValidationError
from arctic_route_data.timeutils import parse_utc


def test_parse_utc_requires_timezone():
    with pytest.raises(MetadataValidationError, match="时区"):
        parse_utc("2026-07-15T12:00:00")


def test_record_times_are_normalized_to_utc(make_record):
    record = make_record(
        issue_time=parse_utc("2026-07-15T08:00:00+08:00"),
        valid_time=parse_utc("2026-07-15T14:00:00+08:00"),
    )
    assert record.issue_time == datetime(2026, 7, 15, 0, tzinfo=UTC)
    assert record.valid_time == datetime(2026, 7, 15, 6, tzinfo=UTC)


def test_manifest_path_cannot_escape_archive(make_record, tmp_path):
    record = make_record()
    object.__setattr__(record, "relative_path", "../secret.nc")
    with pytest.raises(MetadataValidationError, match="逃逸"):
        record.absolute_path(tmp_path)
