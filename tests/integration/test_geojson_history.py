import json
from datetime import UTC, datetime

from arctic_route_data.doctor import inspect_archive
from arctic_route_data.ingestion import IngestionPipeline, sha256_file
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod

T0 = datetime(2026, 7, 15, tzinfo=UTC)


def _evidence():
    return IssueTimeEvidence(
        issue_time=T0,
        method=IssueTimeMethod.EXPLICIT_CATALOG,
        authority="test authority",
        reference="test catalogue",
        observed_at=T0,
        raw_value=T0.isoformat(),
    )


def _write(path, longitude):
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "layer_class": "marine_protected_area",
                            "authority": "test authority",
                        },
                        "geometry": {"type": "Point", "coordinates": [longitude, 75.0]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_geojson_revisions_are_content_addressed_and_never_overwrite_history(tmp_path):
    pipeline = IngestionPipeline(tmp_path / "data")
    first_source = tmp_path / "first.geojson"
    second_source = tmp_path / "second.geojson"
    _write(first_source, 20.0)
    _write(second_source, 21.0)

    first = pipeline.ingest_geojson(
        first_source,
        route_id="route-a",
        issue_time=T0,
        valid_time=T0,
        source="test",
        issue_time_evidence=_evidence(),
        version="v1",
    )
    first_checksum = sha256_file(first.absolute_path(tmp_path / "data"))
    second = pipeline.ingest_geojson(
        second_source,
        route_id="route-a",
        issue_time=T0,
        valid_time=T0,
        source="test",
        issue_time_evidence=_evidence(),
        version="v1",
    )

    assert first.relative_path != second.relative_path
    assert sha256_file(first.absolute_path(tmp_path / "data")) == first_checksum
    assert first.metadata["automatic_hard_mask_allowed"] is False
    summary = first.metadata["constraint_summary"]
    assert summary["unknown_navigation_effect"] == 0
    assert summary["defaulted_to_information"] == 1
    assert summary["navigation_effect_counts"]["information"] == 1
    assert summary["automatic_hard_mask_allowed"] is False
    assert inspect_archive(tmp_path / "data").ok
