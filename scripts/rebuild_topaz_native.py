#!/usr/bin/env python3
"""Rebuild the five TOPAZ-6km-derived data types from the native originalGrid.

The Copernicus Marine ``default`` part that A ingested contains curvilinear ->
rectilinear reconstruction holes although the native ``originalGrid`` data is
finite (verified for 22/28 previously unknown planning nodes).  This script
re-acquires the native grid and conservatively regrids it to A's existing
rectilinear target grid before re-publishing frames and manifest records.

Usage (validation first, then full publish):

    python scripts/rebuild_topaz_native.py --validate-only --limit-steps 3 \
        --out /tmp/topaz_validate.json
    python scripts/rebuild_topaz_native.py --limit-steps 0 \
        --log data/output/golden/topaz-rebuild-20260816.log
"""

from __future__ import annotations

import argparse
import hashlib
import gc
import json
import os
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.cli import _load_copernicus_env_file
from arctic_route_data.curvilinear import regrid_components
from arctic_route_data.forecast_acquisition import _copernicus_credentials_from_environment
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.models import QualityFlag
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.sources import LocalArchiveSource

DSID = "cmems_mod_arc_phy_anfc_6km_detided_PT1H-i"
PRODUCT_ID = "ARCTIC_ANALYSISFORECAST_PHY_002_001"
ROUTE_ID = "offshore_murmansk_to_offshore_dikson"

# data_type -> native variable names
TYPES: dict[str, tuple[str, ...]] = {
    "ocean_current": ("vxo", "vyo"),
    "sea_ice_concentration": ("siconc",),
    "sea_ice_drift": ("vxsi", "vysi"),
    "sea_ice_thickness": ("sithick",),
    "water_level": ("zos",),
}


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _load_target_grid(data_root: Path) -> tuple[np.ndarray, np.ndarray]:
    path = (
        data_root
        / "source_snapshots"
        / "copernicus"
        / "cmems-886d938d635c073d"
        / "ocean_current.nc"
    )
    with xr.open_dataset(path, engine="h5netcdf") as ds:
        lat = np.asarray(ds["latitude"].values, dtype=float)
        lon = np.asarray(ds["longitude"].values, dtype=float)
    return lat, lon


def _target_water_mask(data_root: Path, target_lat: np.ndarray, target_lon: np.ndarray) -> np.ndarray:
    source = LocalArchiveSource(data_root)
    record = source.get_latest_before(
        "land_sea_mask",
        datetime(2026, 8, 9, tzinfo=UTC),
        route_id=ROUTE_ID,
        as_of=datetime(2026, 8, 16, 4, 5, 17, 2419, tzinfo=UTC),
    )
    frame = source.load_frame(record, generation_id=0, as_of=datetime(2026, 8, 16, 4, 5, 17, 2419, tzinfo=UTC))
    arr = frame.payload["land_sea_mask"]
    for dim in arr.dims:
        if dim not in ("latitude", "longitude"):
            arr = arr.isel({dim: 0}, drop=True)
    mask = np.asarray(
        arr.interp(latitude=target_lat, longitude=target_lon, method="nearest").values,
        dtype=float,
    )
    return mask > 0.5


def _sample_existing_metadata(data_root: Path, data_type: str) -> dict[str, Any]:
    raw_root = data_root / "raw" / ROUTE_ID / data_type
    candidates = sorted(raw_root.glob("*/*.metadata.json"))
    if not candidates:
        raise FileNotFoundError(f"no raw sidecar found for {data_type} under {raw_root}")
    return json.loads(candidates[0].read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--max-distance-km", type=float, default=20.0)
    parser.add_argument("--limit-steps", type=int, default=0, help="0 = full window")
    parser.add_argument("--window-hours", type=int, default=20,
                        help="time-window length per open_dataset (match ARCO dask chunks)")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("/tmp/topaz_validate.json"))
    parser.add_argument("--log", type=Path)
    parser.add_argument("--copernicus-env-file", type=Path, default=Path(".env.copernicus"))
    args = parser.parse_args(argv)

    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handler = args.log.open("a", encoding="utf-8")
        sys.stdout = handler
        sys.stderr = handler

    root = args.data_root.resolve()
    _load_copernicus_env_file(args.copernicus_env_file.resolve())
    username, password = _copernicus_credentials_from_environment()

    import copernicusmarine

    target_lat, target_lon = _load_target_grid(root)
    water_mask = _target_water_mask(root, target_lat, target_lon)
    print(f"[topaz-rebuild] target grid {target_lat.size}x{target_lon.size} "
          f"water cells {int(water_mask.sum())}", flush=True)

    t0 = time.time()
    all_vars = sorted({v for vars_ in TYPES.values() for v in vars_})
    window_hours = max(1, args.window_hours)
    start = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)
    end = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
    native_times_all: list[np.datetime64] = []
    native_lat = native_lon = None
    n_times = 145
    if args.limit_steps > 0:
        n_times = min(args.limit_steps, n_times)
        end = start + (n_times - 1) * np.timedelta64(1, "h") + np.timedelta64(1, "h")
    arrays: dict[str, np.ndarray] = {
        data_type: np.full((n_times, target_lat.size, target_lon.size), np.nan, dtype=np.float32)
        for data_type in TYPES
    }
    native_attrs: dict[str, dict[str, Any]] = {}
    done = 0
    seen_times: set[int] = set()
    window_start = start
    while done < n_times:
        window_end = min(window_start + np.timedelta64(window_hours, "h"), end)
        w0 = time.time()
        ds = copernicusmarine.open_dataset(
            dataset_id=DSID,
            dataset_part="originalGrid",
            variables=all_vars,
            start_datetime=window_start,
            end_datetime=window_end,
            username=username,
            password=password,
        )
        window = ds.load()
        if native_lat is None:
            native_lat = window["latitude"].values
            native_lon = window["longitude"].values
        for var in all_vars:
            native_attrs[var] = dict(window[var].attrs)
        n_window = window.sizes["time"]
        for offset in range(n_window):
            if done >= n_times:
                break
            vt = np.asarray(window["time"].values)[offset]
            key = int(np.datetime64(vt).astype("datetime64[ns]").view("i8"))
            if key in seen_times:
                continue
            seen_times.add(key)
            t = done
            step_arrays: dict[str, np.ndarray] = {
                var: np.asarray(window[var].isel(time=offset).values, dtype=np.float64)
                for var in all_vars
            }
            regridded = regrid_components(
                values_by_name=step_arrays,
                native_lon=native_lon,
                native_lat=native_lat,
                target_lon=target_lon,
                target_lat=target_lat,
                max_distance_km=args.max_distance_km,
                target_water_mask=water_mask,
            )
            for data_type, vars_ in TYPES.items():
                for var in vars_:
                    arrays[data_type][t] = regridded[var].astype(np.float32)
            native_times_all.append(np.datetime64(vt))
            done += 1
            if (t + 1) % 10 == 0 or t == n_times - 1:
                print(f"[topaz-rebuild] step {t+1}/{n_times} rss={_rss_mb():.0f}MB "
                      f"elapsed={time.time()-t0:.0f}s", flush=True)
        ds.close()
        del window
        gc.collect()
        print(f"[topaz-rebuild] window {window_start:%Y-%m-%dT%H:%M}..{window_end:%H:%M} "
              f"done in {time.time()-w0:.0f}s rss={_rss_mb():.0f}MB", flush=True)
        window_start = window_end

    # validation table over the 28 r4 unknown nodes
    lat_nodes = np.array([67.5,68.1818,68.8636,69.5455,70.2273,70.9091,71.5909,72.2727,72.9545,73.6364,74.3182,75.0])
    lon_nodes = np.array([30.0,31.7188,33.4375,35.1562,36.875,38.5938,40.3125,42.0312,43.75,45.4688,47.1875,48.9062,50.625,52.3438,54.0625,55.7812,57.5,59.2188,60.9375,62.6562,64.375,66.0938,67.8125,69.5312,71.25,72.9688,74.6875,76.4062,78.125,79.8438,81.5625,83.2812,85.0])
    unk = [(0,8),(0,9),(0,25),(2,13),(2,14),(2,15),(2,16),(2,22),(2,25),(2,26),(3,11),(3,20),(5,21),(5,31),(6,15),(6,22),(7,13),(7,15),(7,25),(7,26),(7,30),(8,15),(8,24),(9,24),(9,30),(9,31),(10,29),(10,32)]
    rows = []
    for r, c in unk:
        la, lo = float(lat_nodes[r]), float(lon_nodes[c])
        i = int(np.abs(target_lat - la).argmin())
        j = int(np.abs(target_lon - lo).argmin())
        node_status = {}
        for data_type, vars_ in TYPES.items():
            for var in vars_:
                vals = arrays[data_type][:, i, j]
                node_status[var] = {
                    "finite_steps": int(np.isfinite(vals).sum()),
                    "total_steps": n_times,
                    "sample_t0": None if not np.isfinite(vals[0]) else round(float(vals[0]), 5),
                }
        rows.append({"node": [r, c], "lat": round(la, 3), "lon": round(lo, 3), "vars": node_status})

    if args.validate_only:
        summary = {
            "mode": "validate-only",
            "steps": n_times,
            "max_distance_km": args.max_distance_km,
            "all_vars_finite_all_steps": sum(
                1
                for row in rows
                if all(v["finite_steps"] == n_times for v in row["vars"].values())
            ),
            "rows": rows,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({"validate": summary["all_vars_finite_all_steps"]}, ensure_ascii=False), flush=True)
        ds.close()
        return 0

    # publish path
    retrieved_at = datetime.now(UTC)
    signature = hashlib.sha256(
        f"{DSID}|originalGrid|{target_lat[0]:.6f}|{target_lat[-1]:.6f}|"
        f"{target_lon[0]:.6f}|{target_lon[-1]:.6f}|{args.max_distance_km}".encode()
    ).hexdigest()[:12]
    snapshot_id = f"cmems-origg-{signature}"
    publisher = AcquisitionPublisher(root)
    evidence = IssueTimeEvidence(
        issue_time=retrieved_at,
        method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
        authority="Copernicus Marine Data Store",
        reference=f"copernicusmarine.open_dataset(dataset_id={DSID!r}, dataset_part='originalGrid')",
        observed_at=retrieved_at,
        raw_value=retrieved_at.isoformat(),
        authoritative=False,
    )
    valid_times = [np.datetime64(v).astype("datetime64[ns]") for v in native_times_all]
    for data_type, vars_ in TYPES.items():
        ds_out = xr.Dataset(
            data_vars={
                var: (("time", "latitude", "longitude"), arrays[data_type])
                for var in vars_
            },
            coords={
                "time": valid_times,
                "latitude": target_lat,
                "longitude": target_lon,
            },
            attrs={
                "copernicus_product": PRODUCT_ID,
                "copernicus_dataset_id": DSID,
                "source_snapshot_id": snapshot_id,
                "acquisition_mode": "retrospective_best_estimate",
                "source_fidelity": "retrospective_best_estimate",
                "grid_topology": "rectilinear",
                "crs": "EPSG:4326",
                "regridding": f"native_originalGrid_nearest_max_{args.max_distance_km}km_v1",
            },
        )
        for var in vars_:
            ds_out[var].attrs = {**native_attrs.get(var, {}), "regridded_from": "originalGrid"}
        snapshot_dir = root / "source_snapshots" / "copernicus" / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{data_type}.nc"
        tmp_path = snapshot_path.with_suffix(snapshot_path.suffix + f".{os.getpid()}.part")
        ds_out.to_netcdf(
            tmp_path,
            engine="h5netcdf",
            encoding={var: {"zlib": True, "complevel": 4, "shuffle": True} for var in vars_},
        )
        tmp_path.replace(snapshot_path)
        sample_meta = _sample_existing_metadata(root, data_type)
        metadata = dict(sample_meta.get("metadata", {}))
        metadata.update(
            {
                "source_snapshot_id": snapshot_id,
                "source_file": snapshot_path.name,
                "source_file_checksum": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                "source_snapshot_relative_path": snapshot_path.relative_to(root).as_posix(),
                "dataset_part": "originalGrid",
                "regridding": f"native_originalGrid_nearest_max_{args.max_distance_km}km_v1",
                "service": "selected_by_copernicusmarine",
            }
        )
        published = publisher.publish_dataset(
            ds_out,
            data_type=data_type,
            route_id=ROUTE_ID,
            source="Copernicus Marine",
            version=snapshot_id,
            issue_evidence=evidence,
            quality_flag=QualityFlag.SUSPECT,
            metadata=metadata,
        )
        print(f"[topaz-rebuild] published {data_type}: {len(published.records)} records "
              f"snapshot={snapshot_id}", flush=True)
    ds.close()
    print(f"[topaz-rebuild] DONE elapsed={time.time()-t0:.0f}s rss={_rss_mb():.0f}MB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
