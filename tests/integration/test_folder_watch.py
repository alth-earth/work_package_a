import json

import numpy as np
import xarray as xr

from arctic_route_data.folder_watch import FolderWatchSource


def test_folder_watch_atomically_ingests_and_archives_pair(tmp_path):
    data_root = tmp_path / "data"
    source = FolderWatchSource(data_root)
    incoming = data_root / "incoming"
    payload = incoming / "ice.nc"
    dataset = xr.Dataset(
        {"ice_conc": (("latitude", "longitude"), np.full((1, 2), 0.25))},
        coords={"longitude": [18.0, 19.0], "latitude": [70.0]},
    )
    dataset["ice_conc"].attrs["units"] = "1"
    dataset.to_netcdf(payload, engine="h5netcdf")
    sidecar = incoming / "ice.metadata.json"
    sidecar.write_text(
        json.dumps(
            {
                "file": payload.name,
                "data_type": "sea_ice_concentration",
                "route_id": "tromso_to_svalbard",
                "issue_time": "2026-07-15T00:00:00Z",
                "valid_time": "2026-07-15T06:00:00Z",
                "source": "test",
            }
        ),
        encoding="utf-8",
    )

    result = source.scan_once()

    assert not result.failures
    assert len(result.ingested) == 1
    record = result.ingested[0]
    assert not payload.exists()
    assert not sidecar.exists()
    assert (data_root / "raw" / record.route_id / record.data_type / record.data_id).is_dir()
    assert record.absolute_path(data_root).is_file()
