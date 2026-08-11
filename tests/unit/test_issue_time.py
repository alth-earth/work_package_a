from datetime import UTC, datetime

import pytest

from arctic_route_data.issue_time import (
    CapturedHttpExchange,
    CopernicusCatalogueIssueTimeResolver,
    DatasetAttributeIssueTimeResolver,
    IssueTimeContext,
    IssueTimeMethod,
    IssueTimeResolutionError,
    SourceIssueTimeResolver,
)
from arctic_route_data.models import DataCategory

T0 = datetime(2026, 7, 15, 12, tzinfo=UTC)


def _context(**changes):
    values = {
        "source_family": "noaa_gfs",
        "data_type": "wind_field",
        "category": DataCategory.DYNAMIC,
        "source_label": "NOAA GFS/NOMADS",
        "valid_times": (T0,),
        "observed_at": datetime(2026, 7, 15, 13, tzinfo=UTC),
        "dataset_attributes": {},
    }
    values.update(changes)
    return IssueTimeContext(**values)


def test_http_last_modified_is_recorded_as_authoritative_evidence():
    exchange = CapturedHttpExchange(
        method="GET",
        request_url="https://nomads.example/gfs.20260715/06/gfs.t06z.f006.grib2",
        response_url="https://nomads.example/gfs.20260715/06/gfs.t06z.f006.grib2",
        request_params={},
        response_headers={"Last-Modified": "Wed, 15 Jul 2026 06:42:00 GMT"},
        observed_at=datetime(2026, 7, 15, 13, tzinfo=UTC),
    )
    evidence = SourceIssueTimeResolver().resolve(
        _context(http_exchanges=(exchange,))
    )
    assert evidence.issue_time == datetime(2026, 7, 15, 6, 42, tzinfo=UTC)
    assert evidence.method is IssueTimeMethod.HTTP_LAST_MODIFIED
    assert evidence.authoritative


def test_failed_http_response_cannot_supply_issue_time():
    exchange = CapturedHttpExchange(
        method="GET",
        request_url="https://nomads.example/gfs.20260715/gfs_f006.grib2",
        response_url="https://nomads.example/error",
        request_params={},
        response_headers={"Last-Modified": "Wed, 15 Jul 2026 06:42:00 GMT"},
        observed_at=datetime(2026, 7, 15, 13, tzinfo=UTC),
        status_code=503,
    )
    with pytest.raises(IssueTimeResolutionError, match="无法确定可审计"):
        SourceIssueTimeResolver().resolve(_context(http_exchanges=(exchange,)))


def test_stale_dataset_bulletin_is_rejected_in_strict_mode():
    context = _context(dataset_attributes={"bulletin_date": "2024-01-09"})
    with pytest.raises(IssueTimeResolutionError, match="无法确定可审计"):
        SourceIssueTimeResolver().resolve(context)


def test_retrieval_fallback_is_explicitly_non_authoritative():
    evidence = SourceIssueTimeResolver(allow_conservative_retrieval=True).resolve(_context())
    assert evidence.method is IssueTimeMethod.CONSERVATIVE_RETRIEVAL
    assert not evidence.authoritative
    assert evidence.issue_time == datetime(2026, 7, 15, 13, tzinfo=UTC)


def test_copernicus_official_catalogue_update_time():
    class Catalogue:
        def model_dump(self, mode):
            assert mode == "json"
            return {
                "products": [
                    {
                        "datasets": [
                            {
                                "dataset_id": "dataset-a",
                                "arco_updated_date": "2026-07-15T06:30:00Z",
                            }
                        ]
                    }
                ]
            }

    def describe(**kwargs):
        assert kwargs == {"dataset_id": "dataset-a", "disable_progress_bar": True}
        return Catalogue()

    context = _context(
        source_family="copernicus_marine",
        source_label="Copernicus Marine",
        dataset_id="dataset-a",
    )
    evidence = CopernicusCatalogueIssueTimeResolver(describe).resolve(context)
    assert evidence.method is IssueTimeMethod.COPERNICUS_SERVICE_SYNC
    assert not evidence.authoritative
    assert evidence.issue_time == datetime(2026, 7, 15, 6, 30, tzinfo=UTC)


def test_copernicus_update_start_is_not_treated_as_completed_publication():
    def describe(**kwargs):
        return {"datasets": [{"arco_updating_start_date": "2026-07-15T06:30:00Z"}]}

    context = _context(
        source_family="copernicus_marine",
        source_label="Copernicus Marine",
        dataset_id="dataset-a",
    )
    with pytest.raises(IssueTimeResolutionError, match="没有与数据时次匹配"):
        CopernicusCatalogueIssueTimeResolver(describe).resolve(context)


def test_dataset_attribute_resolver_accepts_plausible_source_time():
    evidence = DatasetAttributeIssueTimeResolver().resolve(
        _context(dataset_attributes={"date_created": "2026-07-15T06:00:00Z"})
    )
    assert evidence.method is IssueTimeMethod.DATASET_ATTRIBUTE
