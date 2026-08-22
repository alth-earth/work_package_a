"""补采 tromso_to_isfjorden_outer 冬季场景 02-21T03/06/09/12Z 末尾时次（方案 A-WINTER-MET-001 收尾）。

背景：冬季研究场景 tromso_isfjorden_february_2026_research_v1 窗口为
2026-02-15T00Z .. 02-21T00Z（144h）。CARRA 3 类已于 2026-08-22 全量补齐，但
其余 8 类动态非 CARRA 源（Copernicus/TOPAZ/CMEMS）数据末端停在 02-21T00Z，导致
G6 12-type 回放（默认 horizon=144 -> requested_end=02-21T12Z）出现 12h 末端缺口。

本脚本为这 8 类动态源补 02-21T03Z..13Z 窗口（覆盖 03/06/09/12Z 四时次，并自然
多取若干整点），以消除缺口、使 data 末端对齐 02-21T12Z。

注意：
- land_sea_mask 是 GEBCO 派生的 static 产品，与时间无关（全窗口 1 份），不在此补。
- 逐 data_type 独立调用 acquire_copernicus，任一类失败不影响其他类，且已成功类
  不重跑，避免触发 manifest 不可变冲突（重跑已登记时间片会报内容不同）。
- 写入正式 data/ 树（ManifestStore = data/manifest/manifest.sqlite3）。
- 需凭据：.env.copernicus（COPERNICUSMARINE_SERVICE_USERNAME/PASSWORD），且需
  LD_LIBRARY_PATH 指向 .mamba-env/lib（eccodes）。

运行：
  LD_LIBRARY_PATH=<repo>/.mamba-env/lib uv run --extra acquisition \\
      python scripts/winter_non_carra_tail_acquisition.py
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import UTC, datetime

from arctic_route_data.forecast_acquisition import (
    AcquisitionMode,
    Bounds,
    NativeForecastAcquirer,
)

# --- 目标路由/窗口（与冬天研究场景一致）---
ROUTE_ID = "tromso_to_isfjorden_outer"
BOUNDS = Bounds(west=10.0, south=68.0, east=30.0, north=80.0)
# 补 02-21T03Z..13Z：覆盖 03/06/09/12Z 四时次；1h 源自然补到 12Z，wave(3h)补 03/06/09/12。
START = datetime(2026, 2, 21, 3, 0, tzinfo=UTC)
HORIZON_HOURS = 10

# 8 类动态非 CARRA 源（排除 static 的 land_sea_mask）
DYNAMIC_NON_CARRA = (
    "ocean_current",
    "sea_ice_concentration",
    "sea_ice_drift",
    "sea_ice_thickness",
    "sea_ice_type",
    "sea_ice_edge",
    "water_level",
    "wave",
)

PUBLISHED_DATA_ROOT = "data"  # 正式 incoming 树


def _load_copernicus_credentials() -> None:
    """从 .env.copernicus（export 风格）注入 Copernicus 凭据到环境。"""
    path = ".env.copernicus"
    if not os.path.exists(path):
        raise RuntimeError("缺少 .env.copernicus 凭据文件")
    creds: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = shlex.split(v)[0]
    for key in (
        "COPERNICUSMARINE_SERVICE_USERNAME",
        "COPERNICUSMARINE_SERVICE_PASSWORD",
        "COPERNICUSMARINE_USERNAME",
        "COPERNICUSMARINE_PASSWORD",
    ):
        if key in creds:
            os.environ[key] = creds[key]


def main() -> int:
    _load_copernicus_credentials()
    # acquirer 内部会依据 data_root 自建 AcquisitionPublisher，无需（也不接受）显式 publisher
    acq = NativeForecastAcquirer(PUBLISHED_DATA_ROOT)

    summary: dict[str, object] = {"route_id": ROUTE_ID, "start": START.isoformat(),
                                  "horizon_hours": HORIZON_HOURS, "per_type": {}}
    print(f"[tail] route={ROUTE_ID} start={START:%Y-%m-%dT%HZ} "
          f"horizon={HORIZON_HOURS}h types={DYNAMIC_NON_CARRA}", file=sys.stderr)

    for dt in DYNAMIC_NON_CARRA:
        try:
            result = acq.acquire_copernicus(
                route_id=ROUTE_ID,
                bounds=BOUNDS,
                start_time=START,
                horizon_hours=HORIZON_HOURS,
                data_types=(dt,),
                mode=AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE,
            )
            n_snap = len(result.source_snapshot_ids)
            summary["per_type"][dt] = {"ok": True, "snapshots": n_snap}
            print(f"[tail] {dt}: OK snapshots={n_snap}", file=sys.stderr)
        except Exception as exc:  # 单类失败不影响其余类
            summary["per_type"][dt] = {"ok": False, "error": f"{type(exc).__name__}: {exc!r}"[:300]}
            print(f"[tail] {dt}: FAILED {type(exc).__name__}: {exc!r}", file=sys.stderr)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed = [d for d, s in summary["per_type"].items() if not s["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
