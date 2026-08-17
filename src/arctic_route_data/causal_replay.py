"""Causal replay feasibility scan over archived A source records.

The scan answers one question per simulation tick ``t``: under the strict
causal rule ``knowledge_as_of == t`` and ``source.issue_time <= t``, can the
archived records already provide B's full formal hourly input window?

It never runs B, never rebuilds risk frames, and never mutates archives.  It
replicates A's manifest visibility rule and B's input-support rule (exact /
bracketing / nearest for categorical, prior support for static, bbox
containment) so the result is a *feasibility* statement, not a risk
computation.

Two concepts are deliberately kept separate:

* ``knowledge_as_of``: the logical UTC cutoff of this tick (== simulation time
  in causal mode);
* ``max_source_issue_time``: the newest ``issue_time`` among records actually
  visible at that cutoff.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

REQUIRED_FORMAL_DATA_TYPES = frozenset(
    {
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
)
STATIC_TYPES = frozenset({"land_sea_mask"})
CATEGORICAL_TYPES = frozenset({"sea_ice_type", "sea_ice_edge"})


@dataclass(frozen=True, slots=True)
class SourceRecord:
    data_id: str
    data_type: str
    category: str
    issue_time: datetime
    valid_time: datetime
    quality_flag: str
    bbox: tuple[float, float, float, float]
    evidence_authoritative: bool | None
    evidence_method: str | None


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def load_manifest_records(manifest_path: str, route_id: str) -> tuple[SourceRecord, ...]:
    """Read one route's archived records from A's manifest (read-only)."""

    connection = sqlite3.connect(f"file:{manifest_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT data_id, data_type, category, issue_time, valid_time, "
            "quality_flag, bbox_json, metadata_json "
            "FROM manifest WHERE route_id = ?",
            (route_id,),
        ).fetchall()
    finally:
        connection.close()
    records: list[SourceRecord] = []
    for row in rows:
        metadata = json.loads(row[7] or "{}")
        evidence = metadata.get("issue_time_evidence") or {}
        authoritative = evidence.get("authoritative")
        method = evidence.get("method")
        bbox = tuple(float(value) for value in json.loads(row[6]))
        records.append(
            SourceRecord(
                data_id=str(row[0]),
                data_type=str(row[1]),
                category=str(row[2]),
                issue_time=_parse_utc(str(row[3])),
                valid_time=_parse_utc(str(row[4])),
                quality_flag=str(row[5]),
                bbox=bbox,  # type: ignore[arg-type]
                evidence_authoritative=(
                    None if not isinstance(authoritative, bool) else authoritative
                ),
                evidence_method=None if not isinstance(method, str) else method,
            )
        )
    return tuple(records)


def _record_set_digest(visible: tuple[SourceRecord, ...]) -> str:
    payload = "\n".join(sorted(record.data_id for record in visible))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    west, south, east, north = outer
    target_west, target_south, target_east, target_north = inner
    tolerance = 1e-9
    return (
        west <= target_west + tolerance
        and south <= target_south + tolerance
        and east >= target_east - tolerance
        and north >= target_north - tolerance
    )


def _bracket_support(
    visible: tuple[SourceRecord, ...],
    target: datetime,
    *,
    categorical: bool,
    target_bbox: tuple[float, float, float, float],
) -> tuple[SourceRecord, ...]:
    exact = [
        record
        for record in visible
        if record.valid_time == target and _bbox_contains(record.bbox, target_bbox)
    ]
    if exact:
        return (exact[-1],)
    lower = [
        record
        for record in visible
        if record.valid_time < target and _bbox_contains(record.bbox, target_bbox)
    ]
    upper = [
        record
        for record in visible
        if record.valid_time > target and _bbox_contains(record.bbox, target_bbox)
    ]
    if categorical:
        # nearest with earlier-frame preference, mirroring B _nearest
        if not lower and not upper:
            return ()
        if not lower:
            return (upper[0],)
        if not upper:
            return (lower[-1],)
        return (
            (lower[-1],)
            if target - lower[-1].valid_time <= upper[0].valid_time - target
            else (upper[0],)
        )
    if not lower or not upper:
        return ()
    return (lower[-1], upper[0])


def b_input_ready(
    visible: tuple[SourceRecord, ...],
    *,
    window_start: datetime,
    window_end: datetime,
    target_bbox: tuple[float, float, float, float],
) -> tuple[bool, list[str], dict[str, dict[str, Any]]]:
    """Mirror B's input-support rule for the full formal hourly window."""

    by_type: dict[str, list[SourceRecord]] = {}
    for record in visible:
        by_type.setdefault(record.data_type, []).append(record)
    blockers: list[str] = []
    per_type: dict[str, dict[str, Any]] = {}
    missing_types = sorted(REQUIRED_FORMAL_DATA_TYPES - set(by_type))
    for data_type in missing_types:
        blockers.append(f"missing data type: {data_type}")
        per_type[data_type] = {"supported": False, "reason": "no visible records"}
    for data_type in sorted(REQUIRED_FORMAL_DATA_TYPES & set(by_type)):
        records = tuple(
            sorted(by_type[data_type], key=lambda record: record.valid_time)
        )
        unsupported: list[str] = []
        target = window_start
        while target <= window_end:
            if data_type in STATIC_TYPES:
                support = (
                    (records[-1],)
                    if any(record.valid_time <= target for record in records)
                    else (records[0],) if len(records) == 1 else ()
                )
            else:
                support = _bracket_support(
                    records,
                    target,
                    categorical=data_type in CATEGORICAL_TYPES,
                    target_bbox=target_bbox,
                )
            if (
                not support
                or any(
                    not _bbox_contains(record.bbox, target_bbox)
                    for record in support
                )
            ) and len(unsupported) < 4:
                unsupported.append(target.isoformat())
            target += timedelta(hours=1)
        if unsupported:
            reason = (
                "insufficient temporal support"
                if len(unsupported) <= 4
                else f"insufficient temporal support (first examples: {unsupported})"
            )
            blockers.append(f"{data_type}: {reason}")
            per_type[data_type] = {
                "supported": False,
                "unsupported_target_examples": unsupported,
            }
        else:
            per_type[data_type] = {"supported": True}
    ready = not blockers
    return ready, blockers, per_type


def tick_scan(
    records: tuple[SourceRecord, ...],
    *,
    tick: datetime,
    previous_tick: datetime | None,
    window_start: datetime,
    window_end: datetime,
    target_bbox: tuple[float, float, float, float],
    readiness_windows_hours: tuple[int, ...] = (12, 24, 48),
) -> dict[str, Any]:
    visible = tuple(
        sorted(
            (record for record in records if record.issue_time <= tick),
            key=lambda record: (record.data_type, record.valid_time, record.data_id),
        )
    )
    if previous_tick is None:
        newly_visible = visible
    else:
        newly_visible = tuple(
            record
            for record in visible
            if previous_tick < record.issue_time <= tick
        )
    max_source_issue = max((record.issue_time for record in visible), default=None)
    ready, blockers, per_type = b_input_ready(
        visible,
        window_start=window_start,
        window_end=window_end,
        target_bbox=target_bbox,
    )
    window_ready: dict[str, bool] = {"full": ready}
    for hours in readiness_windows_hours:
        rolling_end = min(tick + timedelta(hours=hours), window_end)
        if rolling_end < tick:
            rolling_end = tick
        window_ready[f"plus_{hours}h"] = b_input_ready(
            visible,
            window_start=tick,
            window_end=rolling_end,
            target_bbox=target_bbox,
        )[0]
    type_summary: dict[str, dict[str, Any]] = {}
    for data_type in sorted(REQUIRED_FORMAL_DATA_TYPES):
        type_records = tuple(
            record for record in visible if record.data_type == data_type
        )
        quality = Counter(record.quality_flag for record in type_records)
        authoritative = sum(
            1 for record in type_records if record.evidence_authoritative is True
        )
        non_authoritative = sum(
            1 for record in type_records if record.evidence_authoritative is False
        )
        unknown_evidence = sum(
            1 for record in type_records if record.evidence_authoritative is None
        )
        valid_times = [record.valid_time for record in type_records]
        type_summary[data_type] = {
            "visible_record_count": len(type_records),
            "newest_visible_issue_time": (
                max(record.issue_time for record in type_records).isoformat()
                if type_records
                else None
            ),
            "newest_visible_valid_time": (
                max(valid_times).isoformat() if valid_times else None
            ),
            "forecast_coverage_start": (
                min(valid_times).isoformat() if valid_times else None
            ),
            "forecast_coverage_end": (
                max(valid_times).isoformat() if valid_times else None
            ),
            "quality_summary": dict(sorted(quality.items())),
            "authoritative_evidence_count": authoritative,
            "non_authoritative_evidence_count": non_authoritative,
            "unknown_evidence_count": unknown_evidence,
        }
    return {
        "simulation_time": tick.isoformat(),
        "knowledge_as_of": tick.isoformat(),
        "max_source_issue_time": max_source_issue.isoformat() if max_source_issue else None,
        "visible_record_count": len(visible),
        "newly_visible_count": len(newly_visible),
        "visible_record_set_digest": _record_set_digest(visible),
        "data_types": type_summary,
        "source_visibility_status": "PASS" if visible else "FAIL",
        "b_input_ready": ready,
        "readiness_by_window": window_ready,
        "blockers": blockers,
        "per_type_support": per_type,
    }


def run_causal_scan(
    records: tuple[SourceRecord, ...],
    *,
    simulation_start: datetime,
    simulation_end: datetime,
    target_bbox: tuple[float, float, float, float],
    tick_cadence_hours: int = 1,
    readiness_windows_hours: tuple[int, ...] = (12, 24, 48),
) -> dict[str, Any]:
    ticks: list[dict[str, Any]] = []
    previous: datetime | None = None
    tick = simulation_start
    while tick <= simulation_end:
        ticks.append(
            tick_scan(
                records,
                tick=tick,
                previous_tick=previous,
                window_start=simulation_start,
                window_end=simulation_end,
                target_bbox=target_bbox,
                readiness_windows_hours=readiness_windows_hours,
            )
        )
        previous = tick
        tick += timedelta(hours=tick_cadence_hours)
    ready_ticks = [
        item for item in ticks if item["b_input_ready"] is True
    ]
    first_ready = ready_ticks[0]["simulation_time"] if ready_ticks else None
    longest = _longest_ready_span(ticks, "b_input_ready")
    evidence_methods = Counter(
        record.evidence_method or "unknown" for record in records
    )
    non_authoritative_delays: list[float] = []
    for record in records:
        if (
            record.evidence_authoritative is False
            and record.category != "static"
            and record.issue_time > record.valid_time
        ):
            non_authoritative_delays.append(
                (record.issue_time - record.valid_time).total_seconds() / 3600.0
            )
    visible_at_start = sum(
        1 for record in records if record.issue_time <= simulation_start
    )
    readiness_summary: dict[str, dict[str, Any]] = {}
    for label in ("full", *(f"plus_{hours}h" for hours in readiness_windows_hours)):
        ready_items = [item for item in ticks if item["readiness_by_window"][label]]
        first = ready_items[0]["simulation_time"] if ready_items else None
        span = _longest_ready_span(ticks, f"readiness_by_window.{label}")
        longest_interval = (
            [ticks[span[0]]["simulation_time"], ticks[span[1]]["simulation_time"]]
            if span[1] >= span[0]
            else None
        )
        readiness_summary[label] = {
            "ready_ticks": len(ready_items),
            "first_ready_tick": first,
            "longest_ready_interval": longest_interval,
        }
    return {
        "schema_version": "a.causal-replay-feasibility.v1",
        "simulation_start": simulation_start.isoformat(),
        "simulation_end": simulation_end.isoformat(),
        "tick_cadence_hours": tick_cadence_hours,
        "total_ticks": len(ticks),
        "ready_ticks": len(ready_ticks),
        "first_ready_tick": first_ready,
        "longest_ready_interval": (
            [ticks[longest[0]]["simulation_time"], ticks[longest[1]]["simulation_time"]]
            if longest[1] >= longest[0]
            else None
        ),
        "full_window_feasible": first_ready is not None,
        "overall_status": "PASS" if first_ready is not None else "FAIL",
        "readiness_by_window": readiness_summary,
        "source_evidence_summary": {
            "total_archived_records": len(records),
            "visible_at_simulation_start": visible_at_start,
            "method_counts": dict(sorted(evidence_methods.items())),
            "non_authoritative_delay_hours": {
                "count": len(non_authoritative_delays),
                "min": round(min(non_authoritative_delays), 3)
                if non_authoritative_delays
                else None,
                "median": round(
                    sorted(non_authoritative_delays)[
                        len(non_authoritative_delays) // 2
                    ],
                    3,
                )
                if non_authoritative_delays
                else None,
                "max": round(max(non_authoritative_delays), 3)
                if non_authoritative_delays
                else None,
            },
        },
        "ticks": ticks,
    }


def _longest_ready_span(
    ticks: list[dict[str, Any]],
    key: str,
) -> tuple[int, int]:
    longest: tuple[int, int] = (0, -1)
    current_start = -1
    previous_ready = False
    for index, item in enumerate(ticks):
        ready = _nested(item, key) is True
        if ready and not previous_ready:
            current_start = index
        if not ready and previous_ready and index - current_start > longest[1] - longest[0]:
            longest = (current_start, index - 1)
        previous_ready = ready
    if previous_ready and len(ticks) - current_start > longest[1] - longest[0]:
        longest = (current_start, len(ticks) - 1)
    return longest


def _nested(mapping: dict[str, Any], dotted: str) -> Any:
    value: Any = mapping
    for part in dotted.split("."):
        value = value[part]
    return value
