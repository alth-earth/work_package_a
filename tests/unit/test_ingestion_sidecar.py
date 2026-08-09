import json

import pytest

from arctic_route_data.errors import DataValidationError, MissingMetadataError
from arctic_route_data.ingestion import IngestionPipeline


def test_sidecar_requires_all_time_metadata(tmp_path):
    pipeline = IngestionPipeline(tmp_path / "data")
    sidecar = tmp_path / "missing.metadata.json"
    sidecar.write_text(json.dumps({"file": "sample.nc"}), encoding="utf-8")
    with pytest.raises(MissingMetadataError, match="issue_time"):
        pipeline.ingest_sidecar(sidecar)


def test_sidecar_cannot_reference_file_outside_its_directory(tmp_path):
    pipeline = IngestionPipeline(tmp_path / "data")
    sidecar = tmp_path / "escape.metadata.json"
    sidecar.write_text(
        json.dumps(
            {
                "file": "../outside.nc",
                "data_type": "sea_ice_drift",
                "route_id": "route-a",
                "issue_time": "2026-07-15T00:00:00Z",
                "valid_time": "2026-07-15T00:00:00Z",
                "source": "test",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="相对路径"):
        pipeline.ingest_sidecar(sidecar)
