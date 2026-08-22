"""Approved full CARRA single-levels acquisition (proposal A-WINTER-MET-001).

Runs the six-day retrospective window 2026-02-15T00Z .. 2026-02-21T21Z at 3h
cadence (56 analysis cycles), regrids to the Tromso->Isfjorden corridor bounds,
and publishes each data type into A's incoming tree via the atomic
``.part -> sidecar`` publisher flow.

This is a production action: it writes to the real ``data/`` tree and is
credentialed (CDS). It must run with:
  LD_LIBRARY_PATH=<repo>/.mamba-env/lib   (eccodes C library)
  CDSAPI_RC=<repo>/.cdsapirc               (CDS credentials, not printed)
  uv run --extra acquisition python scripts/carra_full_acquisition.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from arctic_route_data.carra_acquisition import CarraAcquisition
from arctic_route_data.forecast_acquisition import Bounds
from arctic_route_data.publisher import AcquisitionPublisher

# Corridor window (proposal A-WINTER-MET-001): matches the winter research
# scenario `tromso_isfjorden_february_2026_research_v1`
# (corridor_id = tromso_to_isfjorden_outer, 2026-02-15T00Z .. 02-21T00Z, 144h).
START = datetime(2026, 2, 15, 0, 0, tzinfo=UTC)
END = datetime(2026, 2, 21, 0, 0, tzinfo=UTC)
HORIZON_HOURS = int((END - START).total_seconds() // 3600)  # 144 -> 48 cycles

ROUTE_ID = "tromso_to_isfjorden_outer"
BOUNDS = Bounds(west=10.0, south=68.0, east=30.0, north=80.0)
PUBLISHED_DATA_ROOT = "data"          # real incoming tree
SCRATCH_ROOT = "data/carra/_raw_grib"  # raw GRIB downloads (not the published tree)


def main() -> int:
    publisher = AcquisitionPublisher(PUBLISHED_DATA_ROOT)
    driver = CarraAcquisition(
        route_id=ROUTE_ID,
        bounds=BOUNDS,
        data_types=("wind_field", "temperature", "visibility"),
        publish=True,
        publisher=publisher,
        data_root=SCRATCH_ROOT,
    )
    # multiurl.download does not create parent dirs; ensure the scratch tree exists.
    driver.data_root.mkdir(parents=True, exist_ok=True)
    print(
        f"[carra-full] start={START:%Y-%m-%dT%HZ} end={END:%Y-%m-%dT%HZ} "
        f"horizon_hours={HORIZON_HOURS} publish=True",
        file=sys.stderr,
    )
    result = driver.acquire(start_time=START, horizon_hours=HORIZON_HOURS)
    print(json.dumps({
        "source": result.source,
        "route_id": result.route_id,
        "frames_processed": result.frames_processed,
        "frames_published": result.frames_published,
        "published": result.published,
        "snapshot_count": len(result.source_snapshot_ids),
        "snapshots": result.source_snapshot_ids,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
