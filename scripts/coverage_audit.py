#!/usr/bin/env python3
"""Corridor spatial finite coverage gate for formal v3 readiness.

Answers: how many navigable planning nodes have finite values for every
B-required variable (Source->Ready chain), and where unknown-risk nodes are.
This is a *gate* that must print ``unknown_navigable_nodes = 0`` before C
initial planning is allowed; it does not weaken fail-closed semantics.

Usage:
    python scripts/coverage_audit.py \
        --data-root data \
        --route-id offshore_murmansk_to_offshore_dikson \
        --grid-config ../work_package_b/configs/models/demo_unvalidated_smoke_grid_v4.json \
        --contracts-config-root ../arctic_route_contracts/configs \
        --as-of 2026-08-16T04:05:17.002419Z \
        --sample-times 3
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from arctic_route_data.sources import LocalArchiveSource

# B-required variables (work_package_b service.py _COMPONENT_INPUTS).
READY_VARS: dict[str, tuple[str, ...]] = {
    "ocean_current": ("ocean_current_u", "ocean_current_v"),
    "sea_ice_concentration": ("ice_concentration",),
    "sea_ice_drift": ("ice_drift_u", "ice_drift_v"),
    "sea_ice_edge": ("ice_edge",),
    "sea_ice_thickness": ("ice_thickness",),
    "sea_ice_type": ("ice_type",),
    "temperature": ("air_temperature_2m",),
    "visibility": ("visibility",),
    "water_level": ("sea_surface_height",),
    "wave": ("significant_wave_height",),
    "wind_field": ("wind_u10", "wind_v10"),
    "land_sea_mask": ("land_sea_mask",),
}
CATEGORICAL = frozenset({"ice_edge", "ice_type", "land_sea_mask"})


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_grid_config(path: Path) -> tuple[float, float]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    grid = doc["grid_config"]
    if grid.get("schema_version") != "b.target-grid-policy.v1":
        raise SystemExit("unsupported grid config schema")
    return float(grid["latitude_step_degrees"]), float(grid["longitude_step_degrees"])


def load_bbox(contracts_root: Path, corridor_id: str) -> tuple[float, float, float, float]:
    path = contracts_root / "corridors" / f"{corridor_id}.toml"
    with path.open("rb") as handle:
        doc = tomllib.load(handle)
    b = doc["data_bbox"]
    return (float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"]))


def realize_grid(bbox: tuple[float, float, float, float], lat_step: float, lon_step: float):
    west, south, east, north = bbox
    ny = max(2, int(np.ceil((north - south) / lat_step)) + 1)
    nx = max(2, int(np.ceil((east - west) / lon_step)) + 1)
    return np.linspace(south, north, ny), np.linspace(west, east, nx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--grid-config", type=Path, required=True)
    parser.add_argument("--contracts-config-root", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--sample-times", type=int, default=3)
    parser.add_argument("--start", default="2026-08-11T06:00:00Z")
    parser.add_argument("--horizon-hours", type=int, default=144)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)

    lat_step, lon_step = load_grid_config(args.grid_config)
    bbox = load_bbox(args.contracts_config_root.resolve(), args.route_id)
    lat, lon = realize_grid(bbox, lat_step, lon_step)
    as_of = parse_utc(args.as_of)
    start = parse_utc(args.start)
    n_times = max(1, args.sample_times)
    times = [
        start + timedelta(hours=round(h * args.horizon_hours / (n_times - 1)))
        for h in range(n_times)
    ]

    source = LocalArchiveSource(args.data_root)

    def load_frame(dt: str, target: datetime):
        recs = list(
            source.list_available(
                dt, target, target, route_id=args.route_id, as_of=as_of
            )
        )
        if recs:
            rec = min(recs, key=lambda r: abs((r.valid_time - target).total_seconds()))
        else:
            rec = source.get_latest_before(dt, target, route_id=args.route_id, as_of=as_of)
        frame = source.load_frame(rec, generation_id=0, as_of=as_of)
        out: dict[str, object] = {}
        for var in READY_VARS[dt]:
            arr = frame.payload[var]
            for dim in arr.dims:
                if dim not in ("latitude", "longitude"):
                    arr = arr.isel({dim: 0}, drop=True)
            out[var] = arr
        return out

    # node status is the union over sampled times (a node is unknown if any sample NaN)
    unknown_node = np.zeros((len(lat), len(lon)), dtype=bool)
    hard_node = np.zeros((len(lat), len(lon)), dtype=bool)
    per_var_missing = {var: 0 for dt in READY_VARS for var in READY_VARS[dt]}
    for target in times:
        arrays: dict[str, object] = {}
        for dt in READY_VARS:
            arrays.update(load_frame(dt, target))
        valid = np.isfinite(np.asarray(arrays["land_sea_mask"].interp(
            latitude=lat, longitude=lon, method="nearest").values))
        hard = np.asarray(arrays["land_sea_mask"].interp(
            latitude=lat, longitude=lon, method="nearest").values) < 0.5
        for var, arr in arrays.items():
            method = "nearest" if var in CATEGORICAL else "linear"
            vals = np.asarray(arr.interp(latitude=lat, longitude=lon, method=method).values)
            finite = np.isfinite(vals)
            valid &= finite
            per_var_missing[var] += int((~finite).sum())
        unknown_node |= (~hard) & (~valid)
        hard_node |= hard

    navigable = int((~hard_node).sum())
    finite = int(((~hard_node) & (~unknown_node)).sum())
    unknown = int(unknown_node.sum())
    coverage = 100.0 * finite / navigable if navigable else float("nan")

    report = {
        "navigable_nodes": navigable,
        "all_required_variables_finite": finite,
        "unknown_navigable_nodes": unknown,
        "finite_coverage_percent": round(coverage, 2),
        "per_variable_missing_cell_counts": per_var_missing,
        "sample_times": [t.isoformat() for t in times],
        "gate_passed": unknown == 0,
    }
    print(
        f"navigable_nodes = {navigable}\n"
        f"all_required_variables_finite = {finite}\n"
        f"unknown_navigable_nodes = {unknown}\n"
        f"finite_coverage = {coverage:.2f}%"
    )
    for var, missing in sorted(per_var_missing.items()):
        print(f"{var} missing = {missing}")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0 if unknown == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
