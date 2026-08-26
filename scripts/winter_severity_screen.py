#!/usr/bin/env python3
"""Rank preregistered Winter windows using an explicit, selection-only screen.

This tool reads a screening root's published manifest in SQLite read-only mode
and computes a deterministic rough severity score from five source variables.
It is not a risk model, does not call B/C, and must not be used as a scientific
calibration or route-quality claim.  Missing records, unknown units, or empty
finite samples fail closed instead of being filled or silently discarded.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.timeutils import parse_utc

ROUTE_ID = "tromso_to_isfjorden_outer"
SCREEN_TYPES = (
    "sea_ice_concentration",
    "sea_ice_thickness",
    "wind_field",
    "wave",
    "visibility",
)
ICE_WAVE_TYPES = (
    "sea_ice_concentration",
    "sea_ice_thickness",
    "wave",
)
VARIABLES = {
    "sea_ice_concentration": ("ice_concentration", "1"),
    "sea_ice_thickness": ("ice_thickness", "m"),
    "wind_field": ("wind_u10,wind_v10", "m s-1"),
    "wave": ("significant_wave_height", "m"),
    "visibility": ("visibility", "m"),
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="winter-severity-screen")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--route-id", default=ROUTE_ID)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("full", "ice_wave"), default="full")
    return parser.parse_args()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _records(
    data_root: Path,
    *,
    route_id: str,
    start: datetime,
    end: datetime,
    data_types: tuple[str, ...],
) -> dict[str, list[dict[str, Any]]]:
    manifest = data_root / "manifest" / "manifest.sqlite3"
    if not manifest.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest}")
    conn = sqlite3.connect(f"file:{manifest.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT data_type, valid_time, relative_path, variables_json,
                   metadata_json, quality_flag, checksum
            FROM manifest
            WHERE route_id = ? AND data_type IN ({placeholders})
              AND valid_time >= ? AND valid_time <= ?
            ORDER BY data_type, valid_time, data_id
            """.format(placeholders=", ".join("?" for _ in data_types)),
            (route_id, *data_types, _iso(start), _iso(end)),
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[dict[str, Any]]] = {item: [] for item in data_types}
    for row in rows:
        grouped[str(row["data_type"])].append(
            {
                "valid_time": str(row["valid_time"]),
                "relative_path": str(row["relative_path"]),
                "variables": json.loads(row["variables_json"]),
                "metadata": json.loads(row["metadata_json"]),
                "quality_flag": str(row["quality_flag"]),
                "checksum": str(row["checksum"]),
            }
        )
    return grouped


def _finite_values(
    data_root: Path, data_type: str, rows: list[dict[str, Any]]
) -> tuple[np.ndarray, dict[str, Any]]:
    variable_spec, expected_units = VARIABLES[data_type]
    variables = variable_spec.split(",")
    chunks: list[np.ndarray] = []
    u_chunks: list[np.ndarray] = []
    v_chunks: list[np.ndarray] = []
    units: set[str] = set()
    files: list[str] = []
    quality_flags: dict[str, int] = {}
    for row in rows:
        path = data_root / row["relative_path"]
        if not path.is_file():
            raise FileNotFoundError(f"manifest payload missing: {path}")
        with xr.open_dataset(path) as dataset:
            for variable in variables:
                if variable not in dataset:
                    raise ValueError(f"{data_type} missing variable {variable}: {path}")
                values = dataset[variable]
                units_value = str(values.attrs.get("units", "")).strip()
                if not units_value:
                    raise ValueError(f"{data_type}/{variable} has no units: {path}")
                units.add(units_value)
                values_array = np.asarray(values.values, dtype=float)
                if "source_valid_mask" in dataset and data_type != "wind_field":
                    valid_mask = np.asarray(dataset["source_valid_mask"].values, dtype=bool)
                    while valid_mask.ndim < values_array.ndim:
                        valid_mask = np.expand_dims(valid_mask, axis=0)
                    values_array = np.where(valid_mask, values_array, np.nan)
                if data_type == "wind_field" and variable == "wind_u10":
                    u_chunks.append(values_array.reshape(-1))
                elif data_type == "wind_field" and variable == "wind_v10":
                    v_chunks.append(values_array.reshape(-1))
                else:
                    chunks.append(values_array.reshape(-1))
        files.append(str(row["relative_path"]))
        quality_flags[row["quality_flag"]] = quality_flags.get(row["quality_flag"], 0) + 1
    if not chunks and (data_type != "wind_field" or not u_chunks or not v_chunks):
        raise ValueError(f"no values found for {data_type}")
    if data_type == "wind_field":
        if len(u_chunks) != len(v_chunks):
            raise ValueError("wind component record count mismatch")
        values = np.hypot(np.concatenate(u_chunks), np.concatenate(v_chunks))
    else:
        values = np.concatenate(chunks)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError(f"no finite values found for {data_type}")
    accepted_units = {
        "sea_ice_concentration": {"1", "fraction", ""},
        "sea_ice_thickness": {"m", "metre", "meter", "meters"},
        "wind_field": {"m s-1", "m s**-1", "m/s", "m s^-1"},
        "wave": {"m", "metre", "meter", "meters"},
        "visibility": {"m", "metre", "meter", "meters"},
    }[data_type]
    if not units.issubset(accepted_units):
        raise ValueError(
            f"{data_type} units {sorted(units)} are not accepted; expected {expected_units}"
        )
    return values, {
        "files": files,
        "record_count": len(rows),
        "quality_flags": quality_flags,
        "units": sorted(units),
        "finite_count": int(values.size),
    }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def screen(
    data_root: Path,
    *,
    route_id: str,
    start: datetime,
    end: datetime,
    profile: str,
) -> dict[str, Any]:
    data_types = SCREEN_TYPES if profile == "full" else ICE_WAVE_TYPES
    grouped = _records(
        data_root,
        route_id=route_id,
        start=start,
        end=end,
        data_types=data_types,
    )
    metrics: dict[str, dict[str, Any]] = {}
    for data_type in data_types:
        if not grouped[data_type]:
            raise ValueError(f"required screening type has no records: {data_type}")
        values, metadata = _finite_values(data_root, data_type, grouped[data_type])
        if data_type == "sea_ice_concentration":
            raw = float(np.percentile(values, 90))
            normalized = _clamp(raw)
            percentile = "p90"
        elif data_type == "sea_ice_thickness":
            raw = float(np.percentile(values, 90))
            normalized = _clamp(raw / 2.0)
            percentile = "p90"
        elif data_type == "wind_field":
            raw = float(np.percentile(values, 95))
            normalized = _clamp(raw / 25.0)
            percentile = "p95"
        elif data_type == "wave":
            raw = float(np.percentile(values, 95))
            normalized = _clamp(raw / 8.0)
            percentile = "p95"
        else:
            raw = float(np.percentile(values, 10))
            normalized = _clamp(1.0 - raw / 10000.0)
            percentile = "p10"
        metrics[data_type] = {
            **metadata,
            "percentile": percentile,
            "raw_value": raw,
            "normalized_value": normalized,
        }
    if profile == "full":
        weights = {
            "sea_ice_concentration": 0.30,
            "sea_ice_thickness": 0.25,
            "wind_field": 0.20,
            "wave": 0.15,
            "visibility": 0.10,
        }
    else:
        weights = {
            "sea_ice_concentration": 0.45,
            "sea_ice_thickness": 0.35,
            "wave": 0.20,
        }
    score = sum(weights[item] * metrics[item]["normalized_value"] for item in data_types)
    return {
        "schema_version": "research.winter-severity-screen.v1",
        "selection_only": True,
        "scientific_calibration_claim": False,
        "profile": profile,
        "route_id": route_id,
        "start": _iso(start),
        "end": _iso(end),
        "data_root": str(data_root.resolve()),
        "score": float(score),
        "tie_break": [
            metrics["sea_ice_concentration"]["normalized_value"],
            metrics["sea_ice_thickness"]["normalized_value"],
            metrics["wave"]["normalized_value"],
        ],
        "weights": weights,
        "metrics": metrics,
    }


def main() -> int:
    args = _args()
    start = parse_utc(args.start, field="start")
    end = parse_utc(args.end, field="end")
    if end <= start:
        raise ValueError("end must be after start")
    if args.route_id != ROUTE_ID:
        raise ValueError("screen is scoped to the approved Tromso-Isfjorden corridor")
    result = screen(
        args.data_root,
        route_id=args.route_id,
        start=start,
        end=end,
        profile=args.profile,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(
        json.dumps(
            {key: result[key] for key in ("start", "end", "score", "tie_break")},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
