from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import xarray as xr

from arctic_route_data.models import QualityFlag
from arctic_route_data.vessel_traffic_integration import (
    import_vessel_traffic_model_outputs,
)


def test_import_vessel_traffic_model_output_registers_manifest(tmp_path):
    source_dir = tmp_path / "model_input"
    source_dir.mkdir()
    source_file = source_dir / "vessel_traffic_risk_tromso_to_svalbard.nc"
    valid_time = datetime(2026, 8, 6, 12, tzinfo=UTC)
    dataset = xr.Dataset(
        {
            "vessel_traffic_risk": (
                ("lat", "lon"),
                np.array([[0.2, 0.4], [0.3, 0.5]], dtype=np.float32),
                {"units": "0-1"},
            ),
            "ship_count": (
                ("lat", "lon"),
                np.array([[3, 4], [5, 6]], dtype=np.float32),
                {"units": "vessels"},
            ),
        },
        coords={
            "lat": np.array([78.0, 78.25], dtype=np.float32),
            "lon": np.array([12.0, 12.25], dtype=np.float32),
        },
        attrs={
            "created_at_utc": valid_time.isoformat(),
            "realtime_collected_at_utc": valid_time.isoformat(),
        },
    )
    dataset.to_netcdf(source_file)

    result = import_vessel_traffic_model_outputs(
        source_dir=source_dir,
        data_root=tmp_path / "data",
    )

    assert result.skipped == ()
    assert len(result.imported) == 1
    record = result.imported[0]
    assert record.data_type == "vessel_traffic"
    assert record.route_id == "tromso_to_isfjorden_outer"
    assert record.quality_flag == QualityFlag.SUSPECT
    assert record.variables == ("vessel_traffic_risk",)
    assert (tmp_path / "data" / record.relative_path).is_file()
