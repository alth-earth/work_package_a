import hashlib
import json

import numpy as np
import xarray as xr

from arctic_route_data.folder_watch import FolderWatchSource


def _write_pair(data_root):
    source = FolderWatchSource(data_root)
    incoming = data_root / "incoming"
    payload = incoming / "ice.nc"
    dataset = xr.Dataset(
        {"ice_conc": (("latitude", "longitude"), np.full((1, 2), 0.25))},
        coords={"longitude": [18.0, 19.0], "latitude": [70.0]},
    )
    dataset["ice_conc"].attrs["units"] = "1"
    dataset.to_netcdf(payload, engine="h5netcdf")
    payload_checksum = hashlib.sha256(payload.read_bytes()).hexdigest()
    sidecar = incoming / "ice.metadata.json"
    sidecar.write_text(
        json.dumps(
            {
                "file": payload.name,
                "payload_sha256": payload_checksum,
                "payload_size_bytes": payload.stat().st_size,
                "publication_id": "test-publication",
                "data_type": "sea_ice_concentration",
                "route_id": "tromso_to_svalbard",
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
    return source, payload, sidecar


def test_folder_watch_atomically_ingests_and_archives_pair(tmp_path):
    data_root = tmp_path / "data"
    source, payload, sidecar = _write_pair(data_root)

    result = source.scan_once()

    assert not result.failures
    assert len(result.ingested) == 1
    record = result.ingested[0]
    assert not payload.exists()
    assert not sidecar.exists()
    assert (data_root / "raw" / record.route_id / record.data_type / record.data_id).is_dir()
    assert record.absolute_path(data_root).is_file()


def test_archive_failure_keeps_pair_for_idempotent_retry(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    source, payload, sidecar = _write_pair(data_root)
    real_archive = source._archive_upstream_pair

    def fail_archive(*args, **kwargs):
        raise OSError("temporary archive failure")

    monkeypatch.setattr(source, "_archive_upstream_pair", fail_archive)
    first = source.scan_once()

    assert first.failures
    assert payload.is_file() and sidecar.is_file()
    assert not list((data_root / "quarantine").iterdir())
    assert len(source.manifest.all_records()) == 1

    monkeypatch.setattr(source, "_archive_upstream_pair", real_archive)
    second = source.scan_once()

    assert not second.failures
    assert len(second.ingested) == 1
    assert not payload.exists() and not sidecar.exists()
