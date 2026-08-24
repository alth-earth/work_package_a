"""Causal replay feasibility scan tests (synthetic + real manifest)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arctic_route_data.causal_replay import (
    SourceRecord,
    load_manifest_records,
    run_causal_scan,
    tick_scan,
)


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


MANIFEST = _workspace_root() / "work_package_a" / "data" / "manifest" / "manifest.sqlite3"
MUR_ROUTE = "offshore_murmansk_to_offshore_dikson"
TROMSO_ROUTE = "tromso_to_isfjorden_outer"

_BBOX = (10.0, 68.5, 22.0, 79.5)


def _record(
    data_id: str,
    data_type: str,
    issue: str,
    valid: str,
    *,
    category: str = "dynamic",
    quality: str = "good",
    authoritative: bool | None = True,
    method: str = "explicit_catalog",
) -> SourceRecord:
    return SourceRecord(
        data_id=data_id,
        data_type=data_type,
        category=category,
        issue_time=datetime.fromisoformat(issue.replace("Z", "+00:00")).astimezone(UTC),
        valid_time=datetime.fromisoformat(valid.replace("Z", "+00:00")).astimezone(UTC),
        quality_flag=quality,
        bbox=_BBOX,
        evidence_authoritative=authoritative,
        evidence_method=method,
    )


def _tick(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _full_types(records: list[SourceRecord]) -> list[SourceRecord]:
    types = {
        "land_sea_mask",
        "ocean_current",
        "sea_ice_concentration",
        "sea_ice_drift",
        "sea_ice_edge",
        "sea_ice_thickness",
        "sea_ice_type",
        "temperature",
        "visibility",
        "water_level",
        "wave",
        "wind_field",
    }
    missing = sorted(
        data_type
        for data_type in types
        if not any(record.data_type == data_type for record in records)
    )
    support: list[SourceRecord] = []
    for data_type in missing:
        if data_type == "land_sea_mask":
            support.append(
                _record(
                    f"static-{data_type}",
                    data_type,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    category="static",
                )
            )
            continue
        support.append(
            _record(
                f"sup-{data_type}-lo",
                data_type,
                "2026-08-11T00:00:00Z",
                "2026-08-11T00:00:00Z",
            )
        )
        support.append(
            _record(
                f"sup-{data_type}-hi",
                data_type,
                "2026-08-11T00:00:00Z",
                "2026-08-11T18:00:00Z",
            )
        )
    return list(records) + support


def test_new_record_becomes_visible_at_issue_tick() -> None:
    records = (
        _record("r1", "wind_field", "2026-08-11T06:00:00Z", "2026-08-11T06:00:00Z"),
        _record("r2", "wind_field", "2026-08-11T12:00:00Z", "2026-08-11T06:00:00Z"),
    )
    before = tick_scan(
        records,
        tick=_tick("2026-08-11T11:00:00Z"),
        previous_tick=_tick("2026-08-11T10:00:00Z"),
        window_start=_tick("2026-08-11T06:00:00Z"),
        window_end=_tick("2026-08-11T18:00:00Z"),
        target_bbox=_BBOX,
    )
    after = tick_scan(
        records,
        tick=_tick("2026-08-11T12:00:00Z"),
        previous_tick=_tick("2026-08-11T11:00:00Z"),
        window_start=_tick("2026-08-11T06:00:00Z"),
        window_end=_tick("2026-08-11T18:00:00Z"),
        target_bbox=_BBOX,
    )
    assert before["visible_record_count"] == 1
    assert before["max_source_issue_time"] == "2026-08-11T06:00:00+00:00"
    assert after["visible_record_count"] == 2
    assert after["newly_visible_count"] == 1
    assert after["max_source_issue_time"] == "2026-08-11T12:00:00+00:00"


def test_future_issue_time_is_not_visible() -> None:
    records = (
        _record("r1", "wind_field", "2026-08-11T12:00:00Z", "2026-08-11T06:00:00Z"),
    )
    result = tick_scan(
        records,
        tick=_tick("2026-08-11T06:00:00Z"),
        previous_tick=None,
        window_start=_tick("2026-08-11T06:00:00Z"),
        window_end=_tick("2026-08-11T18:00:00Z"),
        target_bbox=_BBOX,
    )
    assert result["visible_record_count"] == 0
    assert result["source_visibility_status"] == "FAIL"


def test_full_window_readiness_requires_bracketing_support() -> None:
    records = _full_types(
        [
            _record("w-1", "wind_field", "2026-08-11T06:00:00Z", "2026-08-11T06:00:00Z"),
            _record("w-2", "wind_field", "2026-08-11T06:00:00Z", "2026-08-11T12:00:00Z"),
        ]
    )
    ready_short = tick_scan(
        records,
        tick=_tick("2026-08-11T06:00:00Z"),
        previous_tick=None,
        window_start=_tick("2026-08-11T06:00:00Z"),
        window_end=_tick("2026-08-11T12:00:00Z"),
        target_bbox=_BBOX,
    )
    not_ready_long = tick_scan(
        records,
        tick=_tick("2026-08-11T06:00:00Z"),
        previous_tick=None,
        window_start=_tick("2026-08-11T06:00:00Z"),
        window_end=_tick("2026-08-11T18:00:00Z"),
        target_bbox=_BBOX,
    )
    assert ready_short["b_input_ready"] is True
    assert not_ready_long["b_input_ready"] is False
    assert any("wind_field" in blocker for blocker in not_ready_long["blockers"])


def test_synthetic_causal_scan_window_boundaries() -> None:
    records = _full_types(
        [
            _record("w-1", "wind_field", "2026-08-11T06:00:00Z", "2026-08-11T06:00:00Z"),
            _record("w-2", "wind_field", "2026-08-11T06:00:00Z", "2026-08-11T12:00:00Z"),
        ]
    )
    scan = run_causal_scan(
        records,
        simulation_start=_tick("2026-08-11T06:00:00Z"),
        simulation_end=_tick("2026-08-11T12:00:00Z"),
        target_bbox=_BBOX,
    )
    assert scan["total_ticks"] == 7
    assert scan["ready_ticks"] == 7
    assert scan["full_window_feasible"] is True
    assert scan["first_ready_tick"] == "2026-08-11T06:00:00+00:00"


@pytest.mark.skipif(
    not MANIFEST.is_file(),
    reason="A manifest not present",
)
def test_real_scenario_a_scan_is_partial() -> None:
    records = load_manifest_records(str(MANIFEST), MUR_ROUTE)
    scan = run_causal_scan(
        records,
        simulation_start=_tick("2026-08-11T06:00:00Z"),
        simulation_end=_tick("2026-08-17T06:00:00Z"),
        target_bbox=(30.0, 67.5, 85.0, 75.0),
    )
    assert scan["total_ticks"] == 145
    assert 0 < scan["ready_ticks"] < 145
    assert scan["first_ready_tick"] is not None
    assert scan["source_evidence_summary"]["visible_at_simulation_start"] == 1


@pytest.mark.skipif(
    not MANIFEST.is_file(),
    reason="A manifest not present",
)
def test_real_scenario_b_scan_is_partial() -> None:
    records = load_manifest_records(str(MANIFEST), TROMSO_ROUTE)
    scan = run_causal_scan(
        records,
        simulation_start=_tick("2026-08-11T06:00:00Z"),
        simulation_end=_tick("2026-08-17T06:00:00Z"),
        target_bbox=(10.0, 68.5, 22.0, 79.5),
    )
    assert scan["total_ticks"] == 145
    assert 0 < scan["ready_ticks"] < 145
    assert scan["first_ready_tick"] is not None
