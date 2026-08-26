#!/usr/bin/env python3
"""Bounded, parameterized acquisition for a Winter research window.

The runner keeps the existing A publication path and makes the window an
explicit command-line identity.  It is deliberately fail-closed: an existing
manifest record for the requested route/type/time aborts before any download,
and no missing value is synthesized.  Screening runs should point at a fresh
runtime data root; formal runs should point at A's canonical ``data`` root.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from arctic_route_data.carra_acquisition import CarraAcquisition
from arctic_route_data.cli import _load_copernicus_env_file
from arctic_route_data.config import load_config
from arctic_route_data.forecast_acquisition import (
    AcquisitionMode,
    Bounds,
    NativeForecastAcquirer,
)
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.static_acquisition import StaticLayerAcquirer
from arctic_route_data.timeutils import parse_utc

DEFAULT_ROUTE_ID = "tromso_to_isfjorden_outer"
DEFAULT_BOUNDS = Bounds(west=10.0, south=68.5, east=22.0, north=79.5)
STATIC_TYPES = {"land_sea_mask"}
CARRA_TYPES = {"wind_field", "temperature", "visibility"}
COPERNICUS_TYPES = {
    "ocean_current",
    "sea_ice_concentration",
    "sea_ice_drift",
    "sea_ice_edge",
    "sea_ice_thickness",
    "sea_ice_type",
    "water_level",
    "wave",
}
ALL_TYPES = tuple(sorted(STATIC_TYPES | CARRA_TYPES | COPERNICUS_TYPES))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="winter-window-acquisition")
    parser.add_argument("--route-id", default=DEFAULT_ROUTE_ID)
    parser.add_argument("--start", required=True, help="UTC ISO-8601 start")
    parser.add_argument(
        "--end", required=True, help="UTC ISO-8601 end (inclusive valid-time target)"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="fresh screening root or A canonical data root",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        choices=ALL_TYPES,
        default=list(ALL_TYPES),
    )
    parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in AcquisitionMode),
        default=AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE.value,
    )
    parser.add_argument(
        "--atmos-source",
        choices=("gfs", "carra"),
        default="gfs",
        help="atmospheric source for wind/visibility; GFS is the bounded default",
    )
    parser.add_argument(
        "--require-total-current",
        action="store_true",
        help="require Copernicus total current and fail closed if only detided is available",
    )
    parser.add_argument(
        "--copernicus-env-file",
        type=Path,
        default=Path(".env.copernicus"),
    )
    parser.add_argument("--summary-output", type=Path)
    return parser.parse_args()


def _parse_window(args: argparse.Namespace) -> tuple[datetime, datetime, int]:
    start = parse_utc(args.start, field="start")
    end = parse_utc(args.end, field="end")
    if end <= start:
        raise ValueError("end must be after start")
    seconds = (end - start).total_seconds()
    if seconds % 3600:
        raise ValueError("window must be an integral number of hours")
    return start, end, int(seconds // 3600)


def _existing_conflicts(
    data_root: Path,
    *,
    route_id: str,
    data_types: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> list[dict[str, str]]:
    if not data_types:
        return []
    manifest = data_root / "manifest" / "manifest.sqlite3"
    if not manifest.is_file():
        return []
    connection = sqlite3.connect(f"file:{manifest.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT data_type, valid_time, data_id
            FROM manifest
            WHERE route_id = ?
              AND data_type IN ({placeholders})
              AND valid_time >= ?
              AND valid_time <= ?
            ORDER BY data_type, valid_time
            """.format(placeholders=", ".join("?" for _ in data_types)),
            (
                route_id,
                *data_types,
                start.isoformat().replace("+00:00", "Z"),
                end.isoformat().replace("+00:00", "Z"),
            ),
        ).fetchall()
    finally:
        connection.close()
    return [
        {"data_type": str(data_type), "valid_time": str(valid_time), "data_id": str(data_id)}
        for data_type, valid_time, data_id in rows
    ]


def _manifest_data_types(data_root: Path, *, route_id: str) -> tuple[str, ...]:
    """Return data types already registered for a route, without opening payloads."""

    manifest = data_root / "manifest" / "manifest.sqlite3"
    if not manifest.is_file():
        return ()
    connection = sqlite3.connect(f"file:{manifest.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT data_type FROM manifest WHERE route_id = ? ORDER BY data_type",
            (route_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"unable to inspect manifest read-only: {manifest}") from exc
    finally:
        connection.close()
    return tuple(str(row[0]) for row in rows)


def _transient_residuals(data_root: Path) -> tuple[str, ...]:
    """Find incomplete publication markers before a run is allowed to proceed."""

    if not data_root.is_dir():
        return ()
    transient: list[str] = []
    for path in data_root.rglob("*"):
        name = path.name
        if (
            name.endswith((".part", ".partial", ".tmp", ".claim"))
            or name.endswith(("-wal", "-shm"))
            or name in {".lock", "LOCK"}
        ):
            transient.append(path.relative_to(data_root).as_posix())
    return tuple(sorted(transient))


def _preflight_data_root(
    data_root: Path,
    *,
    route_id: str,
    data_types: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Validate root freshness and immutable-record conflicts before acquisition."""

    if data_root.exists() and not data_root.is_dir():
        raise ValueError(f"data root is not a directory: {data_root}")
    fresh = not data_root.exists() or not any(data_root.iterdir())
    residuals = _transient_residuals(data_root)
    if residuals:
        raise RuntimeError(
            "refusing to use data root with incomplete publication residue: "
            + ", ".join(residuals[:5])
        )

    manifest = data_root / "manifest" / "manifest.sqlite3"
    registered_types = _manifest_data_types(data_root, route_id=route_id)
    if not fresh and not manifest.is_file():
        raise RuntimeError(
            "data root is non-empty but has no manifest; use a fresh empty root "
            "instead of mixing residual files"
        )
    conflicts = _existing_conflicts(
        data_root,
        route_id=route_id,
        data_types=tuple(item for item in data_types if item not in STATIC_TYPES),
        start=start,
        end=end,
    )
    if conflicts:
        raise RuntimeError(
            "refusing to republish existing logical records; first conflict: "
            + json.dumps(conflicts[0], ensure_ascii=False)
        )
    return {
        "fresh": fresh,
        "manifest_present": manifest.is_file(),
        "registered_types": registered_types,
        "residuals": residuals,
    }


def _validate_total_current(result: Any, *, required: bool) -> dict[str, Any]:
    """Enforce the runner's fail-closed total-current policy at the boundary."""

    records = tuple(record for record in result.records if record.data_type == "ocean_current")
    components = sorted(
        {str(record.metadata.get("current_component", "")) for record in records}
    )
    if required:
        if not records:
            raise RuntimeError("total-current-only policy received no ocean_current records")
        invalid = [
            record
            for record in records
            if record.metadata.get("current_component") != "total"
            or record.metadata.get("tide_included") is not True
        ]
        if invalid:
            raise RuntimeError(
                "total-current-only policy rejected a non-total or unlabelled current record"
            )
    return {
        "required": required,
        "components": components,
        "records": len(records),
        "validated": bool(required and records),
        "fallback_allowed": not required,
    }


def _result_summary(result: Any) -> dict[str, Any]:
    return {
        "record_count": len(result.records),
        "snapshots": list(result.source_snapshot_ids),
        "warnings": list(result.warnings),
    }


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{__import__('os').getpid()}.part")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = _parse_args()
    start, end, horizon_hours = _parse_window(args)
    selected = tuple(dict.fromkeys(args.types))
    unsupported = set(selected) - set(ALL_TYPES)
    if unsupported:
        raise ValueError(f"unsupported data types: {sorted(unsupported)}")
    if args.route_id != DEFAULT_ROUTE_ID:
        raise ValueError(
            "this bounded Winter runner is scoped to the approved Tromso-Isfjorden corridor"
        )
    if args.require_total_current and "ocean_current" not in selected:
        raise ValueError("--require-total-current requires ocean_current in --types")
    root_state = _preflight_data_root(
        args.data_root,
        route_id=args.route_id,
        data_types=selected,
        start=start,
        end=end,
    )
    args.data_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": "research.winter-window-acquisition.v1",
        "status": "RUNNING",
        "route_id": args.route_id,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "horizon_hours": horizon_hours,
        "mode": args.mode,
        "atmos_source": args.atmos_source,
        "data_root": str(args.data_root.resolve()),
        "requested_types": list(selected),
        "root_policy": {
            "fresh_root": root_state["fresh"],
            "manifest_present_before_run": root_state["manifest_present"],
            "registered_types_before_run": list(root_state["registered_types"]),
            "residuals": list(root_state["residuals"]),
            "fail_closed_on_residual": True,
        },
        "require_total_current": bool(args.require_total_current),
        "total_current_policy": {
            "mode": (
                "TOTAL_ONLY_FAIL_CLOSED"
                if args.require_total_current
                else "PREFERRED_TOTAL_EXPLICIT_DETIDED_FALLBACK"
            ),
            "fallback_allowed": not args.require_total_current,
            "failure_behavior": "abort_without_success_summary",
        },
        "static_reused": False,
        "static": {
            "requested": "land_sea_mask" in selected,
            "action": "not_requested",
            "record_count": 0,
            "snapshots": [],
        },
        "carra": None,
        "copernicus": None,
    }
    try:
        mode = AcquisitionMode(args.mode)
        if "land_sea_mask" in selected:
            if "land_sea_mask" in root_state["registered_types"]:
                summary["static"].update({"action": "reused_existing"})
                summary["static_reused"] = True
            else:
                static_result = StaticLayerAcquirer(args.data_root).acquire_gebco(
                    route_id=args.route_id,
                    bounds=DEFAULT_BOUNDS,
                    data_types=("land_sea_mask",),
                    mode=mode,
                )
                summary["static"].update(
                    {"action": "acquired_gebco", **_result_summary(static_result)}
                )

        if set(selected) & CARRA_TYPES and args.atmos_source == "carra":
            carra_types = tuple(item for item in selected if item in CARRA_TYPES)
            raw_root = args.data_root / "carra" / "_raw_grib"
            raw_root.mkdir(parents=True, exist_ok=True)
            driver = CarraAcquisition(
                route_id=args.route_id,
                bounds=DEFAULT_BOUNDS,
                data_types=carra_types,
                publish=True,
                publisher=AcquisitionPublisher(args.data_root),
                data_root=raw_root,
            )
            result = driver.acquire(start_time=start, horizon_hours=horizon_hours)
            summary["carra"] = {
                "types": list(carra_types),
                "frames_processed": result.frames_processed,
                "frames_published": result.frames_published,
                "snapshots": list(result.source_snapshot_ids),
            }
        elif set(selected) & CARRA_TYPES:
            gfs_types = tuple(item for item in selected if item in CARRA_TYPES)
            config = load_config(
                Path(__file__).resolve().parents[1] / "configs/work_package_a.toml"
            )
            acquirer = NativeForecastAcquirer(
                args.data_root,
                request_timeout_seconds=config.acquisition.request_timeout_seconds,
            )
            result = acquirer.acquire_gfs(
                route_id=args.route_id,
                bounds=DEFAULT_BOUNDS,
                as_of=start,
                horizon_hours=horizon_hours,
                step_hours=config.acquisition.gfs_step_hours,
                cycle_lookback_count=config.acquisition.cycle_lookback_count,
                data_types=gfs_types,
                mode=mode,
            )
            summary["carra"] = {
                "source": "NOAA GFS/NOMADS or NCEI analysis",
                "types": list(gfs_types),
                "record_count": len(result.records),
                "snapshots": list(result.source_snapshot_ids),
            }

        if set(selected) & COPERNICUS_TYPES:
            _load_copernicus_env_file(args.copernicus_env_file)
            copernicus_types = tuple(item for item in selected if item in COPERNICUS_TYPES)
            acquirer = NativeForecastAcquirer(args.data_root)
            copernicus_kwargs: dict[str, Any] = {}
            if args.require_total_current:
                copernicus_kwargs["require_total_current"] = True
            result = acquirer.acquire_copernicus(
                route_id=args.route_id,
                bounds=DEFAULT_BOUNDS,
                start_time=start,
                horizon_hours=horizon_hours,
                data_types=copernicus_types,
                mode=mode,
                **copernicus_kwargs,
            )
            summary["total_current_policy"].update(
                _validate_total_current(result, required=args.require_total_current)
            )
            summary["copernicus"] = {
                "source": "Copernicus Marine",
                "types": list(copernicus_types),
                **_result_summary(result),
            }

        summary["status"] = "SUCCEEDED"
    except Exception as exc:
        summary["status"] = "FAILED"
        summary["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        if args.summary_output:
            _write_summary(args.summary_output, summary)
        raise
    if args.summary_output:
        _write_summary(args.summary_output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"winter-window-acquisition failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
