import json
from datetime import UTC, datetime

import pytest

from arctic_route_data.doctor import inspect_archive
from arctic_route_data.forecast_acquisition import Bounds
from arctic_route_data.static_acquisition import (
    StaticLayerAcquirer,
    _parse_gebco_ascii,
)

ASCII = """Dataset { test; } x;
---------------------------------------------
lon[3]
10.0, 10.05, 10.1

lat[2]
70.0, 70.05

elevation.elevation[2][3]
[0], -100, 0, 10
[1], -50, -1, 20

elevation.lat[2]
70.0, 70.05

elevation.lon[3]
10.0, 10.05, 10.1
"""


class _Response:
    text = ASCII
    content = ASCII.encode()

    def raise_for_status(self):
        return None


class _Session:
    def get(self, url, *, timeout):
        assert ".ascii?elevation[" in url
        assert timeout == 9
        return _Response()


class _GeoJsonResponse:
    def __init__(self, layer):
        authority = "Example authority" if layer == "marineprotectedareas" else None
        properties = {
            "name": f"{layer} example",
            "mang_auth": authority,
            "offsource": "Planning authority" if layer == "mspspatialplan" else None,
            "validfrom": "2025-01-01" if layer == "mspspatialplan" else None,
            "validto": "2030-01-01" if layer == "mspspatialplan" else None,
        }
        self.content = json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": f"{layer}.1",
                        "properties": properties,
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[10, 70], [11, 70], [11, 71], [10, 70]]
                            ],
                        },
                    }
                ],
            }
        ).encode()
        self.url = f"https://example.test/wfs?typeNames=emodnet:{layer}"

    def raise_for_status(self):
        return None


class _EmodnetSession:
    def __init__(self):
        self.layers = []

    def get(self, url, *, params, timeout):
        assert url.endswith("/wfs")
        assert timeout == 9
        assert params["outputFormat"] == "application/json"
        assert params["bbox"] == "10,70,11,71,EPSG:4326"
        layer = params["typeNames"].removeprefix("emodnet:")
        self.layers.append(layer)
        return _GeoJsonResponse(layer)


def test_gebco_ascii_parser_and_shared_bathymetry_mask_snapshot(tmp_path):
    parsed = _parse_gebco_ascii(ASCII)
    assert parsed.elevation.shape == (2, 3)

    result = StaticLayerAcquirer(
        tmp_path / "data", request_timeout_seconds=9, http_session=_Session()
    ).acquire_gebco(
        route_id="route-a",
        bounds=Bounds(10, 70, 10.1, 70.05),
    )

    assert {record.data_type for record in result.records} == {
        "bathymetry",
        "land_sea_mask",
    }
    assert len(result.source_snapshot_ids) == 1
    assert {record.metadata["source_snapshot_id"] for record in result.records} == set(
        result.source_snapshot_ids
    )
    mask = next(record for record in result.records if record.data_type == "land_sea_mask")
    assert mask.metadata["hard_mask_semantics"] == "none"


def test_emodnet_keeps_legal_categories_separate_and_informational(tmp_path):
    session = _EmodnetSession()
    data_root = tmp_path / "data"

    result = StaticLayerAcquirer(
        data_root,
        request_timeout_seconds=9,
        http_session=session,
    ).acquire_emodnet_restrictions(
        route_id="route-a",
        bounds=Bounds(10, 70, 11, 71),
        valid_time=datetime(2026, 7, 15, tzinfo=UTC),
        mode="retrospective_best_estimate",
    )

    assert set(session.layers) == {
        "marineprotectedareas",
        "militaryareaspoly",
        "mspspatialplan",
        "natura2000areas",
    }
    assert len(result.records) == 1
    record = result.records[0]
    assert record.data_type == "long_term_restricted_area"
    assert record.quality_flag.value == "suspect"
    assert record.metadata["automatic_hard_mask_allowed"] is False
    assert record.metadata["temporal_fidelity"] == (
        "current_catalog_not_historical_reconstruction"
    )
    summary = record.metadata["constraint_summary"]
    assert summary["navigation_effect_counts"] == {
        "hard": 0,
        "information": 4,
        "soft": 0,
        "unknown": 0,
    }
    assert summary["restriction_category_counts"] == {
        "marine_protected_area": 1,
        "maritime_spatial_plan": 1,
        "military_area": 1,
        "natura_2000_site": 1,
    }
    payload = json.loads(record.absolute_path(data_root).read_text(encoding="utf-8"))
    properties = [feature["properties"] for feature in payload["features"]]
    assert {item["source_layer"] for item in properties} == {
        f"emodnet:{layer}" for layer in session.layers
    }
    assert {item["navigation_effect"] for item in properties} == {"information"}
    assert all(item["automatic_hard_mask_allowed"] is False for item in properties)
    msp = next(
        item
        for item in properties
        if item["restriction_category"] == "maritime_spatial_plan"
    )
    assert msp["authority"] == "Planning authority"
    assert msp["effective_from"] == "2025-01-01"
    assert inspect_archive(data_root).ok


def test_emodnet_refuses_to_publish_an_all_empty_snapshot(tmp_path):
    class EmptySession(_EmodnetSession):
        def get(self, url, *, params, timeout):
            response = super().get(url, params=params, timeout=timeout)
            response.content = b'{"type":"FeatureCollection","features":[]}'
            return response

    with pytest.raises(ValueError, match="空限制区"):
        StaticLayerAcquirer(
            tmp_path / "data",
            request_timeout_seconds=9,
            http_session=EmptySession(),
        ).acquire_emodnet_restrictions(
            route_id="route-a",
            bounds=Bounds(10, 70, 11, 71),
            valid_time=datetime(2026, 7, 15, tzinfo=UTC),
        )
