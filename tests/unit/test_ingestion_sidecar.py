import json

import numpy as np
import pytest
import xarray as xr

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
                "payload_sha256": "a" * 64,
                "payload_size_bytes": 1,
                "publication_id": "test-publication",
                "data_type": "sea_ice_drift",
                "route_id": "route-a",
                "issue_time": "2026-07-15T00:00:00Z",
                "valid_time": "2026-07-15T00:00:00Z",
                "source": "test",
                "version": "v1",
                "quality_flag": "good",
                "metadata": {
                    "issue_time_evidence": {
                        "issue_time": "2026-07-15T00:00:00Z",
                        "method": "explicit_catalog",
                        "authority": "test catalogue",
                        "reference": "catalogue item 1",
                        "observed_at": "2026-07-15T01:00:00Z",
                        "raw_value": "2026-07-15T00:00:00Z",
                        "authoritative": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="相对路径"):
        pipeline.ingest_sidecar(sidecar)


def test_good_sidecar_requires_authoritative_matching_evidence(tmp_path):
    pipeline = IngestionPipeline(tmp_path / "data")
    sidecar = tmp_path / "untrusted.metadata.json"
    sidecar.write_text(
        json.dumps(
            {
                "file": "sample.nc",
                "payload_sha256": "a" * 64,
                "payload_size_bytes": 1,
                "publication_id": "test-publication",
                "data_type": "visibility",
                "route_id": "route-a",
                "issue_time": "2026-07-15T00:00:00Z",
                "valid_time": "2026-07-15T06:00:00Z",
                "source": "test",
                "version": "v1",
                "quality_flag": "good",
                "metadata": {
                    "issue_time_evidence": {
                        "issue_time": "2026-07-15T00:00:00Z",
                        "method": "conservative_retrieval",
                        "authority": "test",
                        "reference": "retrieval",
                        "observed_at": "2026-07-15T01:00:00Z",
                        "raw_value": "2026-07-15T01:00:00Z",
                        "authoritative": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DataValidationError, match=r"不能.*good"):
        pipeline.ingest_sidecar(sidecar)


def test_sidecar_payload_checksum_is_verified_before_normalization(tmp_path):
    pipeline = IngestionPipeline(tmp_path / "data")
    payload = tmp_path / "sample.nc"
    dataset = xr.Dataset(
        {"vis": (("latitude", "longitude"), np.ones((1, 1)))},
        coords={"latitude": [70.0], "longitude": [19.0]},
    )
    dataset["vis"].attrs["units"] = "m"
    dataset.to_netcdf(payload, engine="h5netcdf")
    sidecar = tmp_path / "sample.metadata.json"
    sidecar.write_text(
        json.dumps(
            {
                "file": payload.name,
                "payload_sha256": "0" * 64,
                "payload_size_bytes": payload.stat().st_size,
                "publication_id": "test-publication",
                "data_type": "visibility",
                "route_id": "route-a",
                "issue_time": "2026-07-15T00:00:00Z",
                "valid_time": "2026-07-15T06:00:00Z",
                "source": "test",
                "version": "v1",
                "quality_flag": "good",
                "metadata": {
                    "issue_time_evidence": {
                        "issue_time": "2026-07-15T00:00:00Z",
                        "method": "explicit_catalog",
                        "authority": "test catalogue",
                        "reference": "fixture",
                        "observed_at": "2026-07-15T01:00:00Z",
                        "raw_value": "2026-07-15T00:00:00Z",
                        "authoritative": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DataValidationError, match="payload_sha256"):
        pipeline.ingest_sidecar(sidecar)
