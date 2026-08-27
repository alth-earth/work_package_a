"""Work package A orchestrator: clock-aware prefetch and AB publication."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType

import numpy as np
import xarray as xr

from arctic_route_data.bundle import (
    DatasetBundle,
    DatasetBundleRecord,
    record_provenance_id,
)
from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import ClockSnapshot, SimulationClock
from arctic_route_data.errors import (
    DataNotFoundError,
    DataValidationError,
    FutureInformationError,
    StaleGenerationError,
)
from arctic_route_data.events import (
    DataArrivalEvent,
    DataLoadFailureEvent,
    EventBus,
    GenerationChangedEvent,
    MissingDataAlert,
)
from arctic_route_data.models import (
    DataCategory,
    ManifestRecord,
    StandardDataFrame,
    semantic_payload_digest,
)
from arctic_route_data.sources import DataSource
from arctic_route_data.timeutils import ensure_utc, isoformat_utc


@dataclass(frozen=True, slots=True)
class CoverageReport:
    data_type: str
    requested_start: datetime
    requested_end: datetime
    minimum_required_end: datetime
    available_start: datetime | None
    available_end: datetime | None
    expected_interval_hours: float | None
    missing_intervals: tuple[tuple[datetime, datetime], ...]
    source_snapshot_ids: tuple[str, ...]
    has_start_support: bool
    meets_minimum_horizon: bool
    covers_requested_window: bool
    provenance_complete: bool
    complete: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "data_type": self.data_type,
            "requested_start": isoformat_utc(self.requested_start),
            "requested_end": isoformat_utc(self.requested_end),
            "minimum_required_end": isoformat_utc(self.minimum_required_end),
            "available_start": (
                isoformat_utc(self.available_start) if self.available_start else None
            ),
            "available_end": (
                isoformat_utc(self.available_end) if self.available_end else None
            ),
            "expected_interval_hours": self.expected_interval_hours,
            "missing_intervals": [
                [isoformat_utc(lower), isoformat_utc(upper)]
                for lower, upper in self.missing_intervals
            ],
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "has_start_support": self.has_start_support,
            "meets_minimum_horizon": self.meets_minimum_horizon,
            "covers_requested_window": self.covers_requested_window,
            "provenance_complete": self.provenance_complete,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class PreparedWindow:
    route_id: str
    generation_id: int
    as_of_time: datetime
    frames: Mapping[str, tuple[StandardDataFrame, ...]]
    payload_attestations: Mapping[str, str]
    coverage: Mapping[str, CoverageReport]
    dataset_bundle: DatasetBundle


_DEFAULT_INTERVAL_HOURS: dict[str, float | None] = {
    "wind_field": 3.0,
    "temperature": 3.0,
    "visibility": 3.0,
    "vessel_traffic": 3.0,
    "wave": 3.0,
    # ``prepare_window_for_b`` emits formal v2, so its metadata-free fallback
    # must itself be accepted by the formal cadence contract. Coarser legacy
    # products remain readable with an explicit diagnostic cadence, but cannot
    # silently become a formal v2 window.
    "ocean_current": 1.0,
    "water_level": 1.0,
    "sea_ice_concentration": 1.0,
    "sea_ice_type": 1.0,
    "sea_ice_edge": 1.0,
    "sea_ice_drift": 1.0,
    "sea_ice_thickness": 1.0,
    "bathymetry": None,
    "land_sea_mask": None,
    "long_term_restricted_area": None,
}
_NO_INTERVAL_OVERRIDE = object()


class WorkPackageA:
    def __init__(
        self,
        *,
        source: DataSource,
        clock: SimulationClock,
        cache: PartitionedABCache,
        event_bus: EventBus | None = None,
        history_hours: int = 48,
    ) -> None:
        self.source = source
        self.clock = clock
        self.cache = cache
        self.events = event_bus or EventBus()
        self.history_hours = history_hours
        self._knowledge_generation = clock.generation_id
        self._knowledge_as_of: datetime | None = None
        self._unsubscribe = clock.subscribe_seek(self._on_seek)

    def close(self) -> None:
        self._unsubscribe()

    def _on_seek(self, snapshot: ClockSnapshot) -> None:
        self._knowledge_generation = snapshot.generation_id
        self._knowledge_as_of = None
        self.cache.reset_generation(
            snapshot.generation_id,
            simulation_time=snapshot.current_time,
        )
        self.events.publish(
            GenerationChangedEvent(snapshot.generation_id, snapshot.current_time)
        )

    def prefetch(
        self,
        *,
        route_id: str,
        data_types: list[str] | tuple[str, ...],
        horizon_hours: int = 156,
    ) -> list[StandardDataFrame]:
        snapshot = self.clock.snapshot()
        return self._prefetch_at_snapshot(
            route_id=route_id,
            data_types=data_types,
            horizon_hours=horizon_hours,
            snapshot=snapshot,
        )

    def _prefetch_at_snapshot(
        self,
        *,
        route_id: str,
        data_types: list[str] | tuple[str, ...],
        horizon_hours: int,
        snapshot: ClockSnapshot,
        knowledge_as_of: datetime | None = None,
    ) -> list[StandardDataFrame]:
        """Load only records knowable at one immutable clock snapshot."""

        availability_time = ensure_utc(
            knowledge_as_of or snapshot.current_time,
            field="knowledge_as_of",
        )
        if self._knowledge_generation != snapshot.generation_id:
            raise StaleGenerationError("知识截止时刻与当前模拟代次不一致")
        if self._knowledge_as_of is not None and availability_time < self._knowledge_as_of:
            raise FutureInformationError(
                "同一模拟代次不得将 knowledge_as_of 倒退；请先 seek 生成新代次"
            )
        self._knowledge_as_of = availability_time
        self.cache.evict_expired_events(snapshot.current_time)
        start = snapshot.current_time - timedelta(hours=self.history_hours)
        end = snapshot.current_time + timedelta(hours=horizon_hours)
        published: list[StandardDataFrame] = []
        for data_type in data_types:
            candidates = list(
                self.source.list_available(
                    data_type,
                    start,
                    end,
                    route_id=route_id,
                    as_of=availability_time,
                )
            )
            records: list[ManifestRecord] = []
            for candidate in candidates:
                try:
                    records.append(
                        _validated_source_record(
                            candidate,
                            route_id=route_id,
                            data_type=data_type,
                            as_of=availability_time,
                        )
                    )
                except Exception as exc:
                    self._publish_load_failure(
                        route_id=route_id,
                        data_type=data_type,
                        candidate=candidate,
                        simulation_time=snapshot.current_time,
                        exc=exc,
                    )
            latest = self.source.get_latest_before(
                data_type,
                snapshot.current_time,
                route_id=route_id,
                as_of=availability_time,
            )
            if latest is not None:
                try:
                    validated_latest = _validated_source_record(
                        latest,
                        route_id=route_id,
                        data_type=data_type,
                        as_of=availability_time,
                    )
                except Exception as exc:
                    self._publish_load_failure(
                        route_id=route_id,
                        data_type=data_type,
                        candidate=latest,
                        simulation_time=snapshot.current_time,
                        exc=exc,
                    )
                else:
                    if validated_latest.data_id not in {
                        record.data_id for record in records
                    }:
                        records.insert(0, validated_latest)
            try:
                _, upper = self.source.get_bracketing(
                    data_type,
                    end,
                    route_id=route_id,
                    as_of=availability_time,
                )
            except Exception as exc:
                self._publish_load_failure(
                    route_id=route_id,
                    data_type=data_type,
                    candidate="<end-bracketing-query>",
                    simulation_time=snapshot.current_time,
                    exc=exc,
                )
            else:
                if upper is not None:
                    try:
                        validated_upper = _validated_source_record(
                            upper,
                            route_id=route_id,
                            data_type=data_type,
                            as_of=availability_time,
                        )
                    except Exception as exc:
                        self._publish_load_failure(
                            route_id=route_id,
                            data_type=data_type,
                            candidate=upper,
                            simulation_time=snapshot.current_time,
                            exc=exc,
                        )
                    else:
                        if validated_upper.data_id not in {
                            record.data_id for record in records
                        }:
                            records.append(validated_upper)
            if not records:
                self.events.publish(
                    MissingDataAlert(
                        route_id,
                        data_type,
                        snapshot.current_time,
                        "模拟时刻之前没有已发布且可用的数据",
                    )
                )
                continue
            for record in records:
                if self.cache.contains(record.data_id):
                    continue
                try:
                    frame = self.source.load_frame(
                        record,
                        generation_id=snapshot.generation_id,
                        as_of=availability_time,
                    )
                    frame = _validated_source_frame(
                        frame,
                        requested_record=record,
                        route_id=route_id,
                        data_type=data_type,
                        snapshot=snapshot,
                        knowledge_as_of=availability_time,
                    )
                    selected = self.cache.put(
                        frame,
                        simulation_time=snapshot.current_time,
                        knowledge_as_of=availability_time,
                    )
                except Exception as exc:
                    self.events.publish(
                        DataLoadFailureEvent(
                            route_id,
                            data_type,
                            record.data_id,
                            snapshot.current_time,
                            str(exc),
                        )
                    )
                    continue
                if selected:
                    published.append(frame.consumer_copy())
                    self.events.publish(DataArrivalEvent(record, snapshot.generation_id))
        return published

    def _publish_load_failure(
        self,
        *,
        route_id: str,
        data_type: str,
        candidate: object,
        simulation_time: datetime,
        exc: Exception,
    ) -> None:
        data_id = (
            candidate.data_id
            if isinstance(candidate, ManifestRecord)
            else candidate
            if isinstance(candidate, str) and candidate.startswith("<")
            else f"<invalid-{type(candidate).__name__}>"
        )
        self.events.publish(
            DataLoadFailureEvent(
                route_id,
                data_type,
                data_id,
                simulation_time,
                str(exc),
            )
        )

    def latest_for_b(self, route_id: str, data_type: str) -> StandardDataFrame | None:
        """Return the latest frame valid at or before the simulation clock."""

        self.cache.evict_expired_events(self.clock.now)
        return self.cache.latest(data_type, route_id=route_id, at_or_before=self.clock.now)

    def latest_forecast_for_b(self, route_id: str, data_type: str) -> StandardDataFrame | None:
        """Return the furthest cached forecast, explicitly including future valid times."""

        self.cache.evict_expired_events(self.clock.now)
        return self.cache.latest(data_type, route_id=route_id)

    def window_for_b(
        self,
        route_id: str,
        data_type: str,
        *,
        hours_before: int = 48,
        hours_after: int = 156,
    ) -> list[StandardDataFrame]:
        now = self.clock.now
        self.cache.evict_expired_events(now)
        return self.cache.get_window(
            data_type,
            now - timedelta(hours=hours_before),
            now + timedelta(hours=hours_after),
            route_id=route_id,
        )

    def prepare_window_for_b(
        self,
        *,
        route_id: str,
        data_types: list[str] | tuple[str, ...],
        start_time: datetime | None = None,
        target_horizon_hours: int = 156,
        minimum_complete_horizon_hours: int = 132,
        expected_interval_hours: Mapping[str, float | None] | None = None,
        knowledge_as_of: datetime | None = None,
    ) -> PreparedWindow:
        requested_types = tuple(dict.fromkeys(data_types))
        if not requested_types:
            raise ValueError("data_types 不能为空")
        for field, value in (
            ("target_horizon_hours", target_horizon_hours),
            ("minimum_complete_horizon_hours", minimum_complete_horizon_hours),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field} 必须是正整数")
        snapshot = self.clock.snapshot()
        start = ensure_utc(start_time or snapshot.current_time, field="start_time")
        availability_time = ensure_utc(
            knowledge_as_of or snapshot.current_time,
            field="knowledge_as_of",
        )
        if start != snapshot.current_time:
            raise ValueError("prepare_window_for_b 当前要求 start_time 等于模拟时钟")
        if minimum_complete_horizon_hours > target_horizon_hours:
            raise ValueError("minimum_complete_horizon_hours 不能超过 target_horizon_hours")
        self._prefetch_at_snapshot(
            route_id=route_id,
            data_types=requested_types,
            horizon_hours=target_horizon_hours,
            snapshot=snapshot,
            knowledge_as_of=availability_time,
        )
        requested_end = start + timedelta(hours=target_horizon_hours)
        minimum_end = start + timedelta(hours=minimum_complete_horizon_hours)
        interval_overrides = dict(expected_interval_hours or {})
        unknown_overrides = sorted(set(interval_overrides) - set(requested_types))
        if unknown_overrides:
            raise ValueError(
                "expected_interval_hours 包含未请求类型: "
                + ", ".join(unknown_overrides)
            )
        for data_type, value in interval_overrides.items():
            if value is not None and (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(
                    f"expected_interval_hours[{data_type!r}] 必须是正有限数或 None"
                )
        frames_by_type: dict[str, tuple[StandardDataFrame, ...]] = {}
        reports: dict[str, CoverageReport] = {}
        resolved_intervals: dict[str, float | None] = {}
        verified_provenance_ids: dict[str, str] = {}
        for data_type in requested_types:
            cached_frames = tuple(
                self.cache.get_window(
                    data_type,
                    datetime.min.replace(tzinfo=UTC),
                    datetime.max.replace(tzinfo=UTC),
                    route_id=route_id,
                )
            )
            interval = _resolve_expected_interval(
                data_type=data_type,
                frames=cached_frames,
                override=interval_overrides.get(data_type, _NO_INTERVAL_OVERRIDE),
            )
            resolved_intervals[data_type] = interval
            frames = _select_window_support_frames(
                cached_frames,
                start=start,
                requested_end=requested_end,
                temporal=interval is not None,
            )
            frames_by_type[data_type] = frames
            verified_for_type = _verified_provenance_ids(self.source, frames)
            verified_provenance_ids.update(verified_for_type)
            reports[data_type] = _coverage_report(
                data_type=data_type,
                frames=frames,
                start=start,
                requested_end=requested_end,
                minimum_end=minimum_end,
                expected_interval_hours=interval,
                verified_provenance_ids=verified_for_type,
            )
        delivered_frames, payload_attestations = _attest_consumer_frames(frames_by_type)
        final_snapshot = self.clock.snapshot()
        if (
            final_snapshot.current_time != snapshot.current_time
            or final_snapshot.generation_id != snapshot.generation_id
            or any(
                frame.generation_id != snapshot.generation_id
                for frames in delivered_frames.values()
                for frame in frames
            )
        ):
            raise StaleGenerationError(
                "prepare_window_for_b 期间模拟时刻发生推进或跳转；请重试"
            )
        selected_records = tuple(
            frame.record
            for data_type in requested_types
            for frame in delivered_frames[data_type]
        )
        dataset_bundle = DatasetBundle.create(
            corridor_id=route_id,
            as_of_time=availability_time,
            requested_start=start,
            requested_end=requested_end,
            minimum_required_end=minimum_end,
            requested_data_types=requested_types,
            records=selected_records,
            verified_provenance_ids=verified_provenance_ids,
            expected_interval_hours=resolved_intervals,
        )
        return PreparedWindow(
            route_id=route_id,
            generation_id=snapshot.generation_id,
            as_of_time=availability_time,
            frames=delivered_frames,
            payload_attestations=payload_attestations,
            coverage=MappingProxyType(reports),
            dataset_bundle=dataset_bundle,
        )

    def resolve_dataset_bundle_for_b(
        self,
        bundle: DatasetBundle | Mapping[str, object],
        *,
        generation_id: int,
        knowledge_as_of: datetime,
    ) -> PreparedWindow:
        """Restore the exact formal v2 frames bound by a persisted bundle.

        The caller supplies generation and knowledge cutoff from its runtime
        envelope; neither value is inferred from ``RunContext``. Resolution
        uses public archive-source capabilities and never asks B to scan A's
        SQLite, ready or raw directories.
        """

        if (
            not isinstance(generation_id, int)
            or isinstance(generation_id, bool)
            or generation_id < 0
        ):
            raise ValueError("generation_id 必须是非负整数")
        payload = bundle.to_dict() if isinstance(bundle, DatasetBundle) else bundle
        if not isinstance(payload, Mapping):
            raise TypeError("bundle 必须是 DatasetBundle 或 mapping")
        # Dataclass construction is not a trust boundary. Always round-trip
        # through the semantic parser, even for an existing DatasetBundle.
        verified_bundle = DatasetBundle.from_dict(payload)
        if verified_bundle.schema_version != "a.dataset-bundle.v2":
            raise DataValidationError(
                "跨进程正式恢复只接受 a.dataset-bundle.v2；v1 仅供历史读取"
            )
        if not verified_bundle.coverage or not all(
            proof.complete for proof in verified_bundle.coverage
        ):
            raise DataValidationError("正式恢复要求 DatasetBundle v2 每个请求层 complete=true")

        cutoff = ensure_utc(knowledge_as_of, field="knowledge_as_of")
        if cutoff != verified_bundle.as_of_time:
            raise DataValidationError(
                "knowledge_as_of 必须与 DatasetBundle.as_of_time 精确一致"
            )
        snapshot = self.clock.snapshot()
        if generation_id != snapshot.generation_id:
            raise StaleGenerationError(
                "请求 generation_id 与 A 当前模拟代次不一致："
                f"{generation_id} != {snapshot.generation_id}"
            )
        if snapshot.current_time != verified_bundle.requested_start:
            raise DataValidationError(
                "A 当前 simulation_time 必须与 DatasetBundle.requested_start 精确一致"
            )

        get_record = getattr(self.source, "get_record_by_id", None)
        load_verified = getattr(self.source, "load_verified_frame", None)
        if not callable(get_record) or not callable(load_verified):
            raise DataValidationError(
                "DataSource 不支持正式 exact-bundle 恢复；"
                "需要 get_record_by_id/load_verified_frame 公共能力"
            )

        frames: list[StandardDataFrame] = []
        actual_records: list[ManifestRecord] = []
        verified_provenance_ids: dict[str, str] = {}
        for expected in verified_bundle.records:
            record = get_record(expected.data_id)
            if record is None:
                raise DataNotFoundError(
                    f"DatasetBundle 引用的精确记录不存在: {expected.data_id}"
                )
            record = _validated_source_record(
                record,
                route_id=verified_bundle.corridor_id,
                data_type=expected.data_type,
                as_of=cutoff,
            )
            actual_identity = DatasetBundleRecord(
                data_id=record.data_id,
                data_type=record.data_type,
                issue_time=record.issue_time,
                valid_time=record.valid_time,
                source=record.source,
                version=record.version,
                quality_flag=record.quality_flag.value,
                checksum=record.checksum,
                source_snapshot_id=expected.source_snapshot_id,
            )
            if actual_identity != expected:
                raise DataValidationError(
                    f"归档记录与 DatasetBundle 身份不一致: {expected.data_id}"
                )
            if expected.source_snapshot_id is None:
                # Complete v2 already forbids this; keep the archive capability
                # call's expected provenance type explicit and fail closed.
                raise DataValidationError(
                    f"正式 DatasetBundle 记录缺少 provenance: {expected.data_id}"
                )
            candidate = load_verified(
                record,
                generation_id=generation_id,
                as_of=cutoff,
                expected_provenance_id=expected.source_snapshot_id,
            )
            frame = _validated_source_frame(
                candidate,
                requested_record=record,
                route_id=verified_bundle.corridor_id,
                data_type=expected.data_type,
                snapshot=snapshot,
                knowledge_as_of=cutoff,
            )
            frames.append(frame.consumer_copy())
            actual_records.append(record)
            verified_provenance_ids[record.data_id] = expected.source_snapshot_id

        intervals = {
            proof.data_type: proof.expected_interval_hours
            for proof in verified_bundle.coverage
        }
        rebuilt = DatasetBundle.create(
            corridor_id=verified_bundle.corridor_id,
            as_of_time=cutoff,
            requested_start=verified_bundle.requested_start,
            requested_end=verified_bundle.requested_end,
            minimum_required_end=verified_bundle.minimum_required_end,
            requested_data_types=verified_bundle.requested_data_types,
            records=tuple(actual_records),
            verified_provenance_ids=verified_provenance_ids,
            expected_interval_hours=intervals,
        )
        if rebuilt.to_dict() != verified_bundle.to_dict():
            raise DataValidationError(
                "从归档精确记录重建的 DatasetBundle 与持久身份不一致"
            )

        frames_by_type = {
            data_type: tuple(
                frame for frame in frames if frame.record.data_type == data_type
            )
            for data_type in verified_bundle.requested_data_types
        }
        reports = {
            data_type: _coverage_report(
                data_type=data_type,
                frames=frames_by_type[data_type],
                start=verified_bundle.requested_start,
                requested_end=verified_bundle.requested_end,
                minimum_end=verified_bundle.minimum_required_end,
                expected_interval_hours=intervals[data_type],
                verified_provenance_ids=verified_provenance_ids,
            )
            for data_type in verified_bundle.requested_data_types
        }
        delivered_frames, payload_attestations = _attest_consumer_frames(frames_by_type)
        final_snapshot = self.clock.snapshot()
        if (
            final_snapshot.current_time != snapshot.current_time
            or final_snapshot.generation_id != snapshot.generation_id
            or any(
                frame.generation_id != generation_id
                for items in delivered_frames.values()
                for frame in items
            )
        ):
            raise StaleGenerationError(
                "exact-bundle 恢复期间模拟时刻或 generation 发生变化；请重试"
            )
        return PreparedWindow(
            route_id=verified_bundle.corridor_id,
            generation_id=generation_id,
            as_of_time=cutoff,
            frames=delivered_frames,
            payload_attestations=payload_attestations,
            coverage=MappingProxyType(reports),
            dataset_bundle=verified_bundle,
        )

    def health(self) -> dict[str, object]:
        snapshot = self.clock.snapshot()
        self.cache.evict_expired_events(snapshot.current_time)
        return {
            "simulation_time": snapshot.current_time.isoformat(),
            "running": snapshot.running,
            "speed": snapshot.speed,
            "generation_id": snapshot.generation_id,
            "knowledge_as_of": (
                self._knowledge_as_of.isoformat() if self._knowledge_as_of else None
            ),
            "cache": self.cache.stats(),
            "categories": [category.value for category in DataCategory],
        }


def _verified_provenance_ids(
    source: DataSource, frames: tuple[StandardDataFrame, ...]
) -> dict[str, str]:
    """Resolve provenance through an explicit verifier capability.

    Metadata carried by a generic ``DataSource`` remains useful for diagnosis,
    but cannot by itself grant formal completeness.
    """

    verifier = getattr(source, "verified_provenance_id", None)
    if not callable(verifier):
        return {}
    verified: dict[str, str] = {}
    for frame in frames:
        try:
            provenance_id = verifier(frame.record)
        except Exception:
            continue
        if (
            isinstance(provenance_id, str)
            and provenance_id == record_provenance_id(frame.record)
        ):
            verified[frame.record.data_id] = provenance_id
    return verified


def _attest_consumer_frames(
    frames_by_type: Mapping[str, tuple[StandardDataFrame, ...]],
) -> tuple[
    Mapping[str, tuple[StandardDataFrame, ...]],
    Mapping[str, str],
]:
    """Deep-snapshot frames and bind their actual public payload content."""

    delivered: dict[str, tuple[StandardDataFrame, ...]] = {}
    attestations: dict[str, str] = {}
    for data_type, frames in frames_by_type.items():
        snapshots: list[StandardDataFrame] = []
        for frame in frames:
            snapshot = frame.consumer_copy()
            data_id = snapshot.record.data_id
            if data_id in attestations:
                raise DataValidationError(
                    f"PreparedWindow 包含重复 data_id，无法 attestation: {data_id}"
                )
            attestations[data_id] = semantic_payload_digest(
                snapshot.record,
                snapshot.payload,
            )
            snapshots.append(snapshot)
        delivered[data_type] = tuple(snapshots)
    return MappingProxyType(delivered), MappingProxyType(attestations)


def _coverage_report(
    *,
    data_type: str,
    frames: tuple[StandardDataFrame, ...],
    start: datetime,
    requested_end: datetime,
    minimum_end: datetime,
    expected_interval_hours: float | None,
    verified_provenance_ids: Mapping[str, str],
) -> CoverageReport:
    times = [frame.record.valid_time for frame in frames]
    missing: list[tuple[datetime, datetime]] = []
    if expected_interval_hours is not None:
        tolerance = timedelta(hours=expected_interval_hours)
        for lower, upper in pairwise(times):
            if upper - lower > tolerance and lower < requested_end and upper > start:
                missing.append((lower, upper))
    available_start = times[0] if times else None
    available_end = times[-1] if times else None
    snapshot_ids = tuple(
        sorted(
            {
                verified_provenance_ids[frame.record.data_id]
                for frame in frames
                if frame.record.data_id in verified_provenance_ids
            }
        )
    )
    provenance_complete = bool(frames) and all(
        frame.record.data_id in verified_provenance_ids for frame in frames
    )
    if expected_interval_hours is None:
        has_start_support = bool(frames)
        meets_minimum = bool(frames)
        covers_requested = bool(frames)
    else:
        has_lower_support = any(value <= start for value in times)
        has_upper_support = any(value >= start for value in times)
        has_start_support = has_lower_support and has_upper_support
        missing_to_minimum = any(lower < minimum_end for lower, _ in missing)
        missing_to_requested_end = any(lower < requested_end for lower, _ in missing)
        meets_minimum = (
            has_start_support
            and available_end is not None
            and available_end >= minimum_end
            and not missing_to_minimum
        )
        covers_requested = (
            has_start_support
            and available_end is not None
            and available_end >= requested_end
            and not missing_to_requested_end
        )
    complete = covers_requested and provenance_complete
    return CoverageReport(
        data_type=data_type,
        requested_start=start,
        requested_end=requested_end,
        minimum_required_end=minimum_end,
        available_start=available_start,
        available_end=available_end,
        expected_interval_hours=expected_interval_hours,
        missing_intervals=tuple(missing),
        source_snapshot_ids=snapshot_ids,
        has_start_support=has_start_support,
        meets_minimum_horizon=meets_minimum,
        covers_requested_window=covers_requested,
        provenance_complete=provenance_complete,
        complete=complete,
    )


def _resolve_expected_interval(
    *,
    data_type: str,
    frames: tuple[StandardDataFrame, ...],
    override: object,
) -> float | None:
    if override is not _NO_INTERVAL_OVERRIDE:
        return None if override is None else float(override)
    declared: set[float] = set()
    for frame in frames:
        value = frame.record.metadata.get("nominal_interval_hours")
        if value is None:
            continue
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise DataValidationError(
                f"{frame.record.data_id} 的 nominal_interval_hours 无效"
            )
        declared.add(float(value))
    if len(declared) > 1:
        raise DataValidationError(
            f"{data_type} 窗口混入不同 nominal_interval_hours={sorted(declared)}；"
            "请按来源快照拆分或显式指定策略"
        )
    if declared:
        return declared.pop()
    return _DEFAULT_INTERVAL_HOURS.get(data_type)


def _select_window_support_frames(
    frames: tuple[StandardDataFrame, ...],
    *,
    start: datetime,
    requested_end: datetime,
    temporal: bool,
) -> tuple[StandardDataFrame, ...]:
    if not temporal:
        return frames
    lower = [frame for frame in frames if frame.record.valid_time <= start]
    middle = [
        frame for frame in frames if start < frame.record.valid_time < requested_end
    ]
    upper = [frame for frame in frames if frame.record.valid_time >= requested_end]
    selected: list[StandardDataFrame] = []
    if lower:
        selected.append(lower[-1])
    selected.extend(middle)
    if upper and (not selected or upper[0].record.data_id != selected[-1].record.data_id):
        selected.append(upper[0])
    return tuple(selected)


def _validated_source_record(
    candidate: object,
    *,
    route_id: str,
    data_type: str,
    as_of: datetime,
) -> ManifestRecord:
    """Reject records that violate the immutable source query boundary."""

    if not isinstance(candidate, ManifestRecord):
        raise DataValidationError(
            "DataSource 必须返回 ManifestRecord，"
            f"实际为 {type(candidate).__name__}"
        )
    if candidate.route_id != route_id:
        raise DataValidationError(
            f"DataSource 返回了其他 route_id：{candidate.route_id!r} != {route_id!r}"
        )
    if candidate.data_type != data_type:
        raise DataValidationError(
            f"DataSource 返回了其他 data_type："
            f"{candidate.data_type!r} != {data_type!r}"
        )
    if candidate.issue_time > as_of:
        raise FutureInformationError(
            f"DataSource 返回未来记录 {candidate.data_id}：issue_time="
            f"{candidate.issue_time.isoformat()} 晚于固定快照 {as_of.isoformat()}"
        )
    return candidate


def _validated_source_frame(
    candidate: object,
    *,
    requested_record: ManifestRecord,
    route_id: str,
    data_type: str,
    snapshot: ClockSnapshot,
    knowledge_as_of: datetime | None = None,
) -> StandardDataFrame:
    """Validate a plugin-loaded frame before it can cross into the AB cache."""

    if not isinstance(candidate, StandardDataFrame):
        raise DataValidationError(
            "DataSource.load_frame 必须返回 StandardDataFrame，"
            f"实际为 {type(candidate).__name__}"
        )
    _validated_source_record(
        candidate.record,
        route_id=route_id,
        data_type=data_type,
        as_of=knowledge_as_of or snapshot.current_time,
    )
    if candidate.record != requested_record:
        raise DataValidationError(
            "DataSource.load_frame 返回的 record 与请求记录不一致："
            f"{candidate.record.data_id!r} != {requested_record.data_id!r}"
        )
    if candidate.generation_id != snapshot.generation_id:
        raise StaleGenerationError(
            "DataSource.load_frame 返回的 generation_id 与固定快照不一致："
            f"{candidate.generation_id} != {snapshot.generation_id}"
        )
    if not isinstance(candidate.payload, xr.Dataset | Mapping):
        raise DataValidationError(
            "StandardDataFrame.payload 必须是 xarray.Dataset 或 Mapping，"
            f"实际为 {type(candidate.payload).__name__}"
        )
    _validate_frame_payload(candidate)
    return candidate


def _validate_frame_payload(frame: StandardDataFrame) -> None:
    record = frame.record
    payload = frame.payload
    if isinstance(payload, Mapping):
        if record.media_type == "application/geo+json":
            if payload.get("type") != "FeatureCollection" or not isinstance(
                payload.get("features"), tuple | list
            ):
                raise DataValidationError(
                    f"{record.data_id} 的 GeoJSON payload 必须是 FeatureCollection"
                )
            return
        missing = sorted(set(record.variables) - set(payload))
        if missing:
            raise DataValidationError(
                f"{record.data_id} 的 Mapping payload 缺少变量: {', '.join(missing)}"
            )
        for name in record.variables:
            value = payload[name]
            try:
                array = np.asarray(value)
            except Exception as exc:
                raise DataValidationError(
                    f"{record.data_id} 的 Mapping 变量 {name} 无法转换为数组"
                ) from exc
            if array.size == 0:
                raise DataValidationError(
                    f"{record.data_id} 的 Mapping 变量 {name} 为空"
                )
            if name == "source_valid_mask":
                if not np.issubdtype(array.dtype, np.bool_) or not bool(array.any()):
                    raise DataValidationError(
                        f"{record.data_id} 的 Mapping source_valid_mask "
                        "必须是非空 boolean 且至少包含一个有效单元"
                    )
                continue
            if not np.issubdtype(array.dtype, np.number):
                raise DataValidationError(
                    f"{record.data_id} 的 Mapping 变量 {name} 必须是数值"
                )
            if not np.isfinite(array).any():
                raise DataValidationError(
                    f"{record.data_id} 的 Mapping 变量 {name} 没有任何有限值"
                )
        return

    missing = sorted(set(record.variables) - set(payload.data_vars))
    if missing:
        raise DataValidationError(
            f"{record.data_id} 的 Dataset payload 缺少变量: {', '.join(missing)}"
        )
    for field, expected in (
        ("data_type", record.data_type),
        ("route_id", record.route_id),
        ("issue_time", isoformat_utc(record.issue_time)),
        ("valid_time", isoformat_utc(record.valid_time)),
    ):
        actual = payload.attrs.get(field)
        if actual != expected:
            raise DataValidationError(
                f"{record.data_id} 的 Dataset attrs.{field} 与 manifest 不一致"
            )
    for name in record.variables:
        array = payload[name]
        if array.size == 0:
            raise DataValidationError(f"{record.data_id} 的变量 {name} 为空")
        if name == "source_valid_mask":
            if not np.issubdtype(array.dtype, np.bool_) or not bool(array.any().item()):
                raise DataValidationError(
                    f"{record.data_id} 的 source_valid_mask 必须是非空 boolean 有效域"
                )
            continue
        if not np.issubdtype(array.dtype, np.number):
            raise DataValidationError(
                f"{record.data_id} 的 NetCDF 变量 {name} 必须是数值"
            )
        if not np.isfinite(array.values).any():
            raise DataValidationError(
                f"{record.data_id} 的变量 {name} 没有任何有限值"
            )
