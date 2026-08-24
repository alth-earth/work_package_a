"""Causal replay feasibility audit CLI (A-owned, read-only).

Scans archived manifest records under the strict causal rule
``knowledge_as_of == simulation_time`` and
``source.issue_time <= simulation_time``, and reports whether B's formal
hourly input window could be built at each tick without any future data.

Output: ``causal-replay-feasibility.json`` (compact; no raw payloads).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_data.causal_replay import (
    load_manifest_records,
    run_causal_scan,
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _corridor_bbox(corridor_toml: str) -> tuple[float, float, float, float]:
    with open(corridor_toml, "rb") as handle:
        document = tomllib.load(handle)
    box = document["data_bbox"]
    return (float(box["west"]), float(box["south"]), float(box["east"]), float(box["north"]))


def _scan_scenario(
    item: dict[str, Any],
    *,
    manifest_path: Path,
    contracts_config_root: Path,
    tick_hours: int,
) -> dict[str, Any]:
    output = Path(item["output_dir"])
    run_context = json.loads((output / "run-context.json").read_text(encoding="utf-8"))
    corridor_id = str(run_context["corridor_id"])
    corridor_toml = (
        contracts_config_root / "corridors" / f"{corridor_id}.toml"
    )
    if not corridor_toml.is_file():
        raise FileNotFoundError(f"corridor config missing: {corridor_toml}")
    target_bbox = _corridor_bbox(str(corridor_toml))
    records = load_manifest_records(str(manifest_path), corridor_id)
    scan = run_causal_scan(
        records,
        simulation_start=_parse_utc(str(run_context["simulation_start"])),
        simulation_end=_parse_utc(str(run_context["simulation_end"])),
        target_bbox=target_bbox,
        tick_cadence_hours=tick_hours,
    )
    ticks = scan["ticks"]
    partial_ticks = [
        tick
        for tick in ticks
        if tick["b_input_ready"] is False
        and any(
            support.get("supported") is True
            for support in tick["per_type_support"].values()
        )
    ]
    return {
        "scenario_id": str(run_context["scenario_id"]),
        "corridor_id": corridor_id,
        "scenario_mode": str(run_context.get("scenario_mode", "")),
        "simulation_start": scan["simulation_start"],
        "simulation_end": scan["simulation_end"],
        "tick_cadence_hours": scan["tick_cadence_hours"],
        "total_ticks": scan["total_ticks"],
        "ready_ticks": scan["ready_ticks"],
        "partial_ticks": len(partial_ticks),
        "first_ready_tick": scan["first_ready_tick"],
        "longest_ready_interval": scan["longest_ready_interval"],
        "full_window_feasible": scan["full_window_feasible"],
        "readiness_by_window": scan["readiness_by_window"],
        "verdict": (
            "CAUSAL_REPLAY_FEASIBLE"
            if scan["ready_ticks"] == scan["total_ticks"]
            else (
                "PARTIAL_CAUSAL_WINDOW_AVAILABLE"
                if scan["ready_ticks"] > 0
                else "CURRENT_HISTORICAL_EVIDENCE_INSUFFICIENT"
            )
        ),
        "source_evidence_summary": scan["source_evidence_summary"],
        "ticks": ticks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="causal-replay-feasibility-audit",
        description="Strict causal visibility scan over archived A records",
    )
    parser.add_argument(
        "--demo-config",
        type=Path,
        default=_workspace_root() / "work_package_d" / "configs" / "demo_frozen_sources.json",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_workspace_root() / "work_package_a" / "data" / "manifest" / "manifest.sqlite3",
    )
    parser.add_argument(
        "--contracts-config-root",
        type=Path,
        default=_workspace_root() / "arctic_route_contracts" / "configs",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            _workspace_root() / "work_package_a" / "data" / "output" / "rc2-smoke"
            / "causal-replay-feasibility.json"
        ),
    )
    parser.add_argument("--tick-hours", type=int, default=1)
    args = parser.parse_args(argv)

    config = json.loads(args.demo_config.read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = []
    for key in ("scenario_a", "scenario_b"):
        scenarios.append(
            _scan_scenario(
                config[key],
                manifest_path=args.manifest,
                contracts_config_root=args.contracts_config_root,
                tick_hours=args.tick_hours,
            )
        )
    report = {
        "schema_version": "orchestrator.causal-replay-feasibility.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "tick_cadence_hours": args.tick_hours,
        "note": (
            "Strict causal scan: knowledge_as_of == simulation_time and "
            "source.issue_time <= simulation_time; B_INPUT_READY mirrors B's "
            "full formal hourly window support (exact/bracketing/nearest/"
            "static-prior + bbox containment). No B/C recomputation performed."
        ),
        "scenarios": scenarios,
        "overall": {
            "scenario_a": scenarios[0]["verdict"],
            "scenario_b": scenarios[1]["verdict"],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    for scenario in scenarios:
        windows = scenario["readiness_by_window"]
        print(
            f"{scenario['scenario_id']:<48} {scenario['verdict']:<42} "
            f"ready={scenario['ready_ticks']}/{scenario['total_ticks']} "
            f"first={scenario['first_ready_tick']} "
            f"longest={scenario['longest_ready_interval']}"
        )
        for label, summary in windows.items():
            print(
                f"    window {label:<8} ready={summary['ready_ticks']:>3} "
                f"first={summary['first_ready_tick']} "
                f"longest={summary['longest_ready_interval']}"
            )
    print(json.dumps({"ok": True, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
