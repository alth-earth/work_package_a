from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr

from arctic_route_data.errors import MetadataValidationError
from arctic_route_data.models import StandardDataFrame
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


def test_unsafe_identifiers_are_rejected_before_becoming_paths(make_record):
    with pytest.raises(MetadataValidationError, match="route_id"):
        make_record(route_id="../other-route")
    with pytest.raises(MetadataValidationError, match="version"):
        make_record(version="../../escape")


def test_metadata_and_payload_buffers_are_read_only(make_record):
    record = make_record(metadata={"nested": {"value": 1}})
    dataset = xr.Dataset({"value": ("x", np.array([1.0, 2.0]))})
    frame = StandardDataFrame(record, dataset, 0)

    with pytest.raises(TypeError):
        record.metadata["nested"]["value"] = 2
    with pytest.raises(ValueError, match="read-only"):
        frame.payload["value"].values[0] = 3.0

    consumer = frame.consumer_copy()
    consumer.payload["derived"] = ("x", [5.0, 6.0])
    assert "derived" not in frame.payload
