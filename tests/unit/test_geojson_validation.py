import json
from datetime import UTC, datetime

import pytest

from arctic_route_data.errors import DataValidationError
from arctic_route_data.ingestion import IngestionPipeline
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


def _ingest(tmp_path, geometry):
    source = tmp_path / "constraints.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"authority": "test"},
                        "geometry": geometry,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return IngestionPipeline(tmp_path / "data").ingest_geojson(
        source,
        route_id="route-a",
        issue_time=T0,
        valid_time=T0,
        source="test",
        issue_time_evidence=_evidence(),
    )


@pytest.mark.parametrize(
    "geometry, message",
    [
        ({"type": "Point", "coordinates": [999.0, 75.0]}, "经度"),
        ({"type": "Point", "coordinates": [20.0, 999.0]}, "纬度"),
        ({"type": "Point", "coordinates": [float("nan"), 75.0]}, "有限数"),
        ({"type": "Point", "coordinates": [20.0, 75.0, float("nan")]}, "有限数"),
        ({"type": "Point"}, "coordinates 缺失"),
        ({"type": "Unknown", "coordinates": [20.0, 75.0]}, "不支持"),
        ({"type": "LineString", "coordinates": [[20.0, 75.0]]}, "至少需要两个"),
    ],
)
def test_invalid_geojson_geometry_is_rejected(tmp_path, geometry, message):
    with pytest.raises(DataValidationError, match=message):
        _ingest(tmp_path, geometry)


def test_geometry_collection_is_validated_and_bounded(tmp_path):
    record = _ingest(
        tmp_path,
        {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "Point", "coordinates": [20.0, 75.0]},
                {
                    "type": "LineString",
                    "coordinates": [[21.0, 74.0], [22.0, 76.0]],
                },
            ],
        },
    )

    assert record.bbox == (20.0, 74.0, 22.0, 76.0)


def test_geojson_top_level_must_be_an_object(tmp_path):
    source = tmp_path / "invalid.geojson"
    source.write_text("[]", encoding="utf-8")

    with pytest.raises(DataValidationError, match="FeatureCollection"):
        IngestionPipeline(tmp_path / "data").ingest_geojson(
            source,
            route_id="route-a",
            issue_time=T0,
            valid_time=T0,
            source="test",
            issue_time_evidence=_evidence(),
        )
