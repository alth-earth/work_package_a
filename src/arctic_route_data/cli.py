"""Command line entry point for ingestion, archive queries and replay checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import xarray as xr

from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.config import config_to_dict, load_config
from arctic_route_data.doctor import inspect_archive
from arctic_route_data.folder_watch import FolderWatchSource
from arctic_route_data.forecast_acquisition import (
    COPERNICUS_FORECAST_SPECS,
    GFS_DATA_TYPES,
    AcquisitionMode,
    Bounds,
    NativeForecastAcquirer,
    resolve_acquisition_window,
)
from arctic_route_data.ingestion import IngestionPipeline
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod, SourceIssueTimeResolver
from arctic_route_data.legacy_downloaders import LEGACY_DOWNLOADERS, LegacyDownloaderRunner
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import QualityFlag
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.service import WorkPackageA
from arctic_route_data.shared_context import (
    create_run_context_from_bundle,
    load_shared_scenario_request,
)
from arctic_route_data.sources import LocalArchiveSource
from arctic_route_data.specs import DATA_TYPE_SPECS
from arctic_route_data.static_acquisition import StaticLayerAcquirer
from arctic_route_data.timeutils import parse_utc

_MARKER = ".arctic-route-data-workspace"


def _initialize_workspace(path: Path) -> None:
    for relative in (
        "raw",
        "normalized",
        "incoming",
        "ready",
        "manifest",
        "quarantine",
        "source_snapshots",
        "demo_scenarios",
        "output/logs",
    ):
        (path / relative).mkdir(parents=True, exist_ok=True)
    (path / _MARKER).write_text("arctic-route-data-v1\n", encoding="utf-8")
    ManifestStore(path / "manifest" / "manifest.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arctic-data", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="初始化 A 的离线数据目录")
    init.add_argument("--data-root", type=Path, default=Path("data"))

    ingest = subparsers.add_parser(
        "ingest", help="自动拆分 NetCDF、生成 sidecar、规范化并登记 manifest"
    )
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--data-root", type=Path, default=Path("data"))
    ingest.add_argument("--data-type", required=True, choices=sorted(DATA_TYPE_SPECS))
    ingest.add_argument("--route-id", required=True)
    ingest.add_argument("--issue-time", required=True, help="UTC ISO-8601；不允许省略或猜测")
    ingest.add_argument(
        "--valid-time",
        required=True,
        help="无时间坐标/GeoJSON 的 UTC 时刻；多时次 NetCDF 使用文件内 valid_time",
    )
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--issue-authority", help="发布时间的权威机构；默认与 source 相同")
    ingest.add_argument("--issue-reference", default="operator supplied catalogue timestamp")
    ingest.add_argument("--version", default="1")
    ingest.add_argument(
        "--quality",
        choices=[QualityFlag.GOOD.value, QualityFlag.SUSPECT.value, QualityFlag.DEGRADED.value],
        default="good",
    )

    scan = subparsers.add_parser("scan", help="处理 incoming 中已原子发布的 sidecar")
    scan.add_argument("--data-root", type=Path, default=Path("data"))

    listing = subparsers.add_parser("list", help="按模拟时刻查询 manifest")
    listing.add_argument("--data-root", type=Path, default=Path("data"))
    listing.add_argument("--route-id", required=True)
    listing.add_argument("--data-type", required=True, choices=sorted(DATA_TYPE_SPECS))
    listing.add_argument("--start", required=True)
    listing.add_argument("--end", required=True)
    listing.add_argument("--as-of", required=True, help="模拟时刻；过滤 issue_time > as-of")

    replay = subparsers.add_parser(
        "replay", help="按模拟时钟准备一致窗口、覆盖报告与不可变数据 bundle"
    )
    replay.add_argument("--data-root", type=Path, default=Path("data"))
    replay.add_argument("--route-id", required=True)
    replay.add_argument("--at", required=True)
    replay.add_argument(
        "--mode",
        choices=("causal", AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE.value),
        default="causal",
        help=(
            "causal 将知识截止时刻锁为模拟时刻；"
            "retrospective_best_estimate 必须另外显式给出 --knowledge-as-of"
        ),
    )
    replay.add_argument(
        "--knowledge-as-of",
        help=(
            "UTC ISO-8601 事后知识截止时刻；只允许在 "
            "retrospective_best_estimate 中与模拟时钟分离"
        ),
    )
    replay.add_argument("--types", nargs="+", required=True, choices=sorted(DATA_TYPE_SPECS))
    replay.add_argument("--config", type=Path, default=Path("configs/work_package_a.toml"))
    replay.add_argument("--horizon-hours", type=int)
    replay.add_argument("--minimum-horizon-hours", type=int)
    replay.add_argument("--max-memory-mb", type=float)
    replay.add_argument(
        "--bundle-output",
        type=Path,
        help="可选：把 DatasetBundle.v2 原子写入指定 JSON 文件",
    )
    replay.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="诊断模式：覆盖不完整时仍返回 0；不完整 bundle 仍禁止持久化",
    )
    replay.add_argument(
        "--summary-only",
        action="store_true",
        help="stdout 仅输出 coverage、计数和 bundle 身份；完整 records 仍写入 bundle 文件",
    )

    config_show = subparsers.add_parser("config-show", help="校验并显示实际生效的 TOML 配置")
    config_show.add_argument("--config", type=Path, default=Path("configs/work_package_a.toml"))

    acquire = subparsers.add_parser(
        "acquire-forecast",
        aliases=("acquire-window",),
        help="从 NOAA/Copernicus 获取显式 UTC 窗并正式发布",
    )
    acquire.add_argument("--config", type=Path, default=Path("configs/work_package_a.toml"))
    acquire.add_argument("--data-root", type=Path, default=Path("data"))
    acquisition_target = acquire.add_mutually_exclusive_group(required=True)
    acquisition_target.add_argument(
        "--corridor",
        "--scenario",
        dest="corridor",
        help="采集走廊 ID；--scenario 是 0.2 兼容别名",
    )
    acquisition_target.add_argument(
        "--shared-scenario",
        dest="shared_scenario_id",
        help="直接读取 arctic_route_contracts 中的唯一场景事实",
    )
    acquire.add_argument("--contracts-config-root", type=Path)
    acquire.add_argument(
        "--shared-simulation-start",
        help="物化 frozen_forecast 共享模板时必须显式提供的 UTC anchor",
    )
    acquire.add_argument(
        "--shared-candidate-route-distance-nm",
        type=float,
        help="按候选航线距离选择完整航程时域；仅用于 frozen_forecast 共享模板",
    )

    shared = subparsers.add_parser(
        "shared-scenario",
        help="读取共享 Scenario/Corridor/Vessel，并输出 A 采集请求或绑定 RunContext",
    )
    shared.add_argument("--scenario", required=True, dest="scenario_id")
    shared.add_argument("--contracts-config-root", type=Path)
    shared.add_argument(
        "--simulation-start",
        help="冻结预测模板的显式 UTC anchor；具体 retrospective 场景不得覆盖",
    )
    shared.add_argument(
        "--candidate-route-distance-nm",
        type=float,
        help="按共享 HorizonPolicy 物化候选航线所需时域；超来源上限则拒绝",
    )
    shared.add_argument("--dataset-bundle", type=Path)
    shared.add_argument("--run-context-output", type=Path)
    shared.add_argument("--run-id")
    acquire.add_argument(
        "--sources",
        nargs="+",
        choices=("gfs", "copernicus", "gebco", "emodnet"),
        default=("gfs",),
    )
    acquire.add_argument("--types", nargs="+", choices=sorted(DATA_TYPE_SPECS))
    acquire.add_argument(
        "--start",
        help="corridor 模式必填的 UTC ISO-8601 起点；禁止隐式 latest",
    )
    window = acquire.add_mutually_exclusive_group()
    window.add_argument("--end", help="UTC ISO-8601；与 --horizon-hours 互斥")
    window.add_argument("--horizon-hours", type=int)
    acquire.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in AcquisitionMode),
        default=None,
        help="冻结当次预测，或下载事后最佳估计；二者不可混称",
    )
    acquire.add_argument(
        "--copernicus-env-file",
        type=Path,
        help="严格解析的凭据 dotenv；仅允许 Copernicus 用户名/密码键",
    )

    doctor = subparsers.add_parser("doctor", help="校验 manifest、路径和 SHA-256")
    doctor.add_argument("--data-root", type=Path, default=Path("data"))
    doctor.add_argument("--allow-empty", action="store_true")

    demo = subparsers.add_parser("demo", help="生成小型数据并演示防未来信息泄漏")
    demo.add_argument("--workspace", type=Path, default=Path("data/demo-run"))
    demo.add_argument("--reset", action="store_true")

    legacy = subparsers.add_parser(
        "legacy-run", help="运行一个旧下载器并自动解析发布时间、拆帧和写 sidecar"
    )
    legacy.add_argument("--legacy-root", type=Path, required=True)
    legacy.add_argument("--data-root", type=Path, default=Path("data"))
    legacy.add_argument("--downloader", required=True, choices=sorted(LEGACY_DOWNLOADERS))
    legacy.add_argument(
        "--allow-conservative-retrieval",
        action="store_true",
        help="源站无权威发布时间时，用成功获取时刻作为安全上界并标为 suspect",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_native_libraries()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        _initialize_workspace(args.data_root.resolve())
        print(args.data_root.resolve())
        return 0
    if args.command == "ingest":
        issue_time = parse_utc(args.issue_time, field="issue_time")
        evidence = IssueTimeEvidence(
            issue_time=issue_time,
            method=IssueTimeMethod.EXPLICIT_CATALOG,
            authority=args.issue_authority or args.source,
            reference=args.issue_reference,
            observed_at=datetime.now(UTC),
            raw_value=args.issue_time,
            authoritative=True,
        )
        publisher = AcquisitionPublisher(args.data_root)
        if args.data_type == "long_term_restricted_area":
            data = json.loads(args.file.read_text(encoding="utf-8"))
            published = publisher.publish_geojson(
                data,
                route_id=args.route_id,
                source=args.source,
                version=args.version,
                issue_evidence=evidence,
                valid_time=parse_utc(args.valid_time, field="valid_time"),
                quality_flag=QualityFlag(args.quality),
            )
        else:
            published = publisher.publish_netcdf_path(
                args.file,
                data_type=args.data_type,
                route_id=args.route_id,
                source=args.source,
                version=args.version,
                issue_evidence=evidence,
                valid_time=parse_utc(args.valid_time, field="valid_time"),
                quality_flag=QualityFlag(args.quality),
            )
        print(
            json.dumps(
                [record.to_dict() for record in published.records],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "scan":
        result = FolderWatchSource(args.data_root).scan_once()
        print(json.dumps({
            "ingested": [record.data_id for record in result.ingested],
            "failures": [[str(path), message] for path, message in result.failures],
        }, ensure_ascii=False, indent=2))
        return 1 if result.failures else 0
    if args.command == "list":
        store = ManifestStore(args.data_root / "manifest" / "manifest.sqlite3")
        records = store.list_available(
            args.data_type,
            parse_utc(args.start, field="start"),
            parse_utc(args.end, field="end"),
            route_id=args.route_id,
            as_of=parse_utc(args.as_of, field="as_of"),
        )
        print(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))
        return 0
    if args.command == "replay":
        config = load_config(args.config)
        simulation_time = parse_utc(args.at, field="at")
        if args.mode == AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE.value:
            if args.knowledge_as_of is None:
                parser.error(
                    "retrospective_best_estimate 回放必须显式指定 --knowledge-as-of"
                )
            knowledge_as_of = parse_utc(
                args.knowledge_as_of,
                field="knowledge_as_of",
            )
            if knowledge_as_of < simulation_time:
                parser.error("--knowledge-as-of 不得早于 --at")
        else:
            if args.knowledge_as_of is not None:
                parser.error("causal 回放不允许指定 --knowledge-as-of")
            knowledge_as_of = simulation_time
        clock = SimulationClock(simulation_time)
        cache = PartitionedABCache(
            max_memory_mb=args.max_memory_mb or config.cache.max_memory_mb,
            slow_frames_per_partition=config.cache.slow_frames_per_partition,
            dynamic_frames_per_partition=config.cache.dynamic_frames_per_partition,
        )
        service = WorkPackageA(
            source=LocalArchiveSource(args.data_root),
            clock=clock,
            cache=cache,
            history_hours=config.cache.history_hours,
        )
        target_horizon = (
            args.horizon_hours
            if args.horizon_hours is not None
            else config.cache.target_horizon_hours
        )
        minimum_horizon = (
            args.minimum_horizon_hours
            if args.minimum_horizon_hours is not None
            else min(config.cache.minimum_complete_horizon_hours, target_horizon)
        )
        prepared = service.prepare_window_for_b(
            route_id=args.route_id,
            data_types=args.types,
            target_horizon_hours=target_horizon,
            minimum_complete_horizon_hours=minimum_horizon,
            knowledge_as_of=knowledge_as_of,
        )
        all_required_complete = all(
            report.complete for report in prepared.coverage.values()
        )
        bundle_persisted = args.bundle_output is not None and all_required_complete
        if bundle_persisted:
            _atomic_json_output(
                args.bundle_output,
                prepared.dataset_bundle.to_dict(),
            )
        bundle_payload = prepared.dataset_bundle.to_dict()
        if args.summary_only:
            bundle_payload = {
                key: value
                for key, value in bundle_payload.items()
                if key != "records"
            }
        print(
            json.dumps(
                {
                    "route_id": prepared.route_id,
                    "as_of_time": prepared.as_of_time.isoformat(),
                    "simulation_time": simulation_time.isoformat(),
                    "knowledge_as_of": knowledge_as_of.isoformat(),
                    "replay_mode": args.mode,
                    "generation_id": prepared.generation_id,
                    "all_required_complete": all_required_complete,
                    "coverage": {
                        data_type: report.to_dict()
                        for data_type, report in prepared.coverage.items()
                    },
                    (
                        "selected_record_counts"
                        if args.summary_only
                        else "selected_data_ids"
                    ): {
                        data_type: (
                            len(frames)
                            if args.summary_only
                            else [frame.record.data_id for frame in frames]
                        )
                        for data_type, frames in prepared.frames.items()
                    },
                    "dataset_bundle": bundle_payload,
                    "bundle_output": (
                        str(args.bundle_output.resolve())
                        if bundle_persisted
                        else None
                    ),
                    "bundle_persisted": bundle_persisted,
                    "health": service.health(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if all_required_complete or args.allow_incomplete else 1
    if args.command == "config-show":
        print(json.dumps(config_to_dict(load_config(args.config)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "shared-scenario":
        simulation_start = (
            parse_utc(args.simulation_start, field="simulation_start")
            if args.simulation_start
            else None
        )
        request = load_shared_scenario_request(
            scenario_id=args.scenario_id,
            config_root=args.contracts_config_root,
            simulation_start=simulation_start,
            candidate_route_distance_nm=args.candidate_route_distance_nm,
        )
        if (args.dataset_bundle is None) != (args.run_context_output is None):
            parser.error("--dataset-bundle 与 --run-context-output 必须成对出现")
        context = None
        if args.dataset_bundle is not None:
            context = create_run_context_from_bundle(
                request=request,
                bundle_path=args.dataset_bundle,
                output_path=args.run_context_output,
                run_id=args.run_id,
            )
        print(
            json.dumps(
                {
                    "scenario_id": request.scenario.scenario_id,
                    "scenario_version": request.scenario.version,
                    "corridor_id": request.route_id,
                    "manifest_route_id": request.route_id,
                    "vessel_profile_id": request.vessel.vessel_profile_id,
                    "start": request.start.isoformat(),
                    "end": request.end.isoformat(),
                    "horizon_hours": request.horizon_hours,
                    "acquisition_mode": request.mode.value,
                    "bbox": [
                        request.bounds.west,
                        request.bounds.south,
                        request.bounds.east,
                        request.bounds.north,
                    ],
                    "run_context_output": (
                        str(args.run_context_output.resolve()) if context else None
                    ),
                    "run_id": context.run_id if context else None,
                    "config_digest": context.config_digest if context else None,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command in {"acquire-forecast", "acquire-window"}:
        if args.copernicus_env_file is not None:
            _load_copernicus_env_file(args.copernicus_env_file)
        config = load_config(args.config)
        if args.shared_scenario_id is not None:
            forbidden = {
                "--start": args.start,
                "--end": args.end,
                "--horizon-hours": args.horizon_hours,
                "--mode": args.mode,
            }
            conflicts = [name for name, value in forbidden.items() if value is not None]
            if conflicts:
                parser.error(
                    "--shared-scenario 已唯一决定时间和模式，不得同时给出 "
                    + ", ".join(conflicts)
                )
            shared_start = (
                parse_utc(
                    args.shared_simulation_start,
                    field="shared_simulation_start",
                )
                if args.shared_simulation_start
                else None
            )
            shared_request = load_shared_scenario_request(
                scenario_id=args.shared_scenario_id,
                config_root=args.contracts_config_root,
                simulation_start=shared_start,
                candidate_route_distance_nm=args.shared_candidate_route_distance_nm,
            )
            route_id = shared_request.route_id
            bounds = shared_request.bounds
            window = resolve_acquisition_window(
                start_time=shared_request.start,
                end_time=shared_request.end,
                mode=shared_request.mode,
            )
        else:
            if (
                args.shared_simulation_start is not None
                or args.shared_candidate_route_distance_nm is not None
            ):
                parser.error(
                    "--shared-simulation-start/--shared-candidate-route-distance-nm "
                    "只能与 --shared-scenario 同用"
                )
            if args.start is None or (args.end is None and args.horizon_hours is None):
                parser.error(
                    "corridor 模式必须显式指定 --start，以及 --end/--horizon-hours"
                )
            if args.mode is None:
                parser.error("corridor 模式必须显式指定 --mode")
            try:
                corridor = config.corridors[args.corridor]
            except KeyError as exc:
                supported = ", ".join(sorted(config.corridors))
                raise ValueError(
                    f"未知 corridor={args.corridor!r}；支持: {supported}"
                ) from exc
            route_id = corridor.corridor_id
            bounds = Bounds(
                west=corridor.bbox[0],
                south=corridor.bbox[1],
                east=corridor.bbox[2],
                north=corridor.bbox[3],
            )
            window = resolve_acquisition_window(
                start_time=parse_utc(args.start, field="start"),
                end_time=parse_utc(args.end, field="end") if args.end else None,
                horizon_hours=args.horizon_hours,
                mode=args.mode,
            )
        start = window.start
        horizon = window.horizon_hours
        acquirer = NativeForecastAcquirer(
            args.data_root,
            request_timeout_seconds=config.acquisition.request_timeout_seconds,
        )
        requested = set(args.types or ())
        supported_by_sources: set[str] = set()
        if "gfs" in args.sources:
            supported_by_sources.update(GFS_DATA_TYPES)
        if "copernicus" in args.sources:
            supported_by_sources.update(COPERNICUS_FORECAST_SPECS)
        if "gebco" in args.sources:
            supported_by_sources.update(("bathymetry", "land_sea_mask"))
        if "emodnet" in args.sources:
            supported_by_sources.add("long_term_restricted_area")
        unsupported_requested = sorted(requested - supported_by_sources)
        if unsupported_requested:
            parser.error(
                "所选 sources 无法提供这些 data_type: "
                + ", ".join(unsupported_requested)
            )
        results = []
        if "gfs" in args.sources:
            gfs_types = sorted(requested & GFS_DATA_TYPES) if requested else sorted(GFS_DATA_TYPES)
            if gfs_types:
                results.append(
                    acquirer.acquire_gfs(
                        route_id=route_id,
                        bounds=bounds,
                        as_of=start,
                        horizon_hours=horizon,
                        step_hours=config.acquisition.gfs_step_hours,
                        cycle_lookback_count=config.acquisition.cycle_lookback_count,
                        data_types=gfs_types,
                        mode=window.mode,
                    )
                )
        if "copernicus" in args.sources:
            copernicus_types = (
                sorted(requested & COPERNICUS_FORECAST_SPECS.keys())
                if requested
                else sorted(COPERNICUS_FORECAST_SPECS)
            )
            if copernicus_types:
                results.append(
                    acquirer.acquire_copernicus(
                        route_id=route_id,
                        bounds=bounds,
                        start_time=start,
                        horizon_hours=horizon,
                        data_types=copernicus_types,
                        mode=window.mode,
                    )
                )
        if "gebco" in args.sources:
            gebco_types = (
                sorted(requested & {"bathymetry", "land_sea_mask"})
                if requested
                else ["bathymetry", "land_sea_mask"]
            )
            if gebco_types:
                results.append(
                    StaticLayerAcquirer(
                        args.data_root,
                        request_timeout_seconds=config.acquisition.request_timeout_seconds,
                    ).acquire_gebco(
                        route_id=route_id,
                        bounds=bounds,
                        data_types=tuple(gebco_types),
                        mode=window.mode,
                    )
                )
        if "emodnet" in args.sources and (
            not requested or "long_term_restricted_area" in requested
        ):
            results.append(
                StaticLayerAcquirer(
                    args.data_root,
                    request_timeout_seconds=config.acquisition.request_timeout_seconds,
                ).acquire_emodnet_restrictions(
                    route_id=route_id,
                    bounds=bounds,
                    valid_time=start,
                    mode=window.mode,
                )
            )
        if not results:
            parser.error("sources/types 组合没有产生任何采集任务")
        print(
            json.dumps(
                {
                    "corridor_id": route_id,
                    "shared_scenario_id": args.shared_scenario_id,
                    "requested_start": start.isoformat(),
                    "requested_end": window.end.isoformat(),
                    "horizon_hours": horizon,
                    "acquisition_mode": window.mode.value,
                    "results": [
                        {
                            "source": result.source,
                            "source_snapshot_ids": result.source_snapshot_ids,
                            "record_count": len(result.records),
                            "data_types": sorted({record.data_type for record in result.records}),
                            "warnings": result.warnings,
                        }
                        for result in results
                    ],
                    "usable_as_of": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "doctor":
        report = inspect_archive(args.data_root, allow_empty=args.allow_empty)
        print(json.dumps({
            "ok": report.ok,
            "checked": report.checked,
            "errors": report.errors,
            "warnings": report.warnings,
        }, ensure_ascii=False, indent=2))
        return 0 if report.ok else 1
    if args.command == "demo":
        return _run_demo(args.workspace.resolve(), reset=args.reset)
    if args.command == "legacy-run":
        runner = LegacyDownloaderRunner(
            legacy_root=args.legacy_root,
            data_root=args.data_root,
            issue_time_resolver=SourceIssueTimeResolver(
                allow_conservative_retrieval=args.allow_conservative_retrieval
            ),
        )
        result = runner.run(args.downloader)
        print(
            json.dumps(
                {
                    "downloader": result.downloader,
                    "captured_http_exchanges": result.captured_http_exchanges,
                    "records": [record.to_dict() for record in result.records],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return 2


def _atomic_json_output(path: Path, value: object) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(
        destination.suffix + f".{os.getpid()}.part"
    )
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(destination)


_COPERNICUS_ENV_KEYS = frozenset(
    {
        "COPERNICUSMARINE_SERVICE_USERNAME",
        "COPERNICUSMARINE_SERVICE_PASSWORD",
        "COPERNICUSMARINE_USERNAME",
        "COPERNICUSMARINE_PASSWORD",
    }
)
_DOTENV_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z")


def _load_copernicus_env_file(path: Path) -> None:
    """Load credentials as data; never execute the dotenv as shell code."""

    resolved = path.resolve()
    try:
        mode = resolved.stat().st_mode & 0o777
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取 Copernicus 凭据文件 {resolved}: {exc}") from exc
    if mode != 0o600:
        raise RuntimeError(
            f"Copernicus 凭据文件权限必须为 600（当前 {mode:o}）: {resolved}"
        )
    loaded: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        match = _DOTENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            raise RuntimeError(f"凭据文件第 {line_number} 行不是 KEY=VALUE")
        key, raw_value = match.groups()
        if key not in _COPERNICUS_ENV_KEYS:
            raise RuntimeError(f"凭据文件包含不允许的键 {key!r}")
        if key in loaded:
            raise RuntimeError(f"凭据文件重复定义 {key!r}")
        value = raw_value.strip()
        if value[:1] in {"'", '"'} or value[-1:] in {"'", '"'}:
            if len(value) < 2 or value[0] != value[-1]:
                raise RuntimeError(f"凭据文件中的 {key!r} 引号不成对")
            value = value[1:-1]
        if not value:
            raise RuntimeError(f"凭据文件中的 {key!r} 不能为空")
        loaded[key] = value
    official_pair = {
        "COPERNICUSMARINE_SERVICE_USERNAME",
        "COPERNICUSMARINE_SERVICE_PASSWORD",
    }
    compatibility_pair = {
        "COPERNICUSMARINE_USERNAME",
        "COPERNICUSMARINE_PASSWORD",
    }
    if not (official_pair <= loaded.keys() or compatibility_pair <= loaded.keys()):
        raise RuntimeError("凭据文件必须包含一组成对的 Copernicus 用户名和密码")
    os.environ.update(loaded)


def _run_demo(workspace: Path, *, reset: bool) -> int:
    if reset and workspace.exists():
        marker = workspace / _MARKER
        if not marker.is_file():
            raise RuntimeError(f"拒绝清理未标记为 A 工作区的目录: {workspace}")
        shutil.rmtree(workspace)
    _initialize_workspace(workspace)
    pipeline = IngestionPipeline(workspace)
    simulation_time = datetime(2026, 7, 15, 12, tzinfo=UTC)
    longitude = np.array([18.0, 19.0, 20.0])
    latitude = np.array([70.0, 71.0])
    cases = (
        (simulation_time, simulation_time - timedelta(hours=6), 0.20, "analysis"),
        (
            simulation_time + timedelta(hours=6),
            simulation_time - timedelta(hours=6),
            0.35,
            "forecast",
        ),
        (
            simulation_time + timedelta(hours=1),
            simulation_time + timedelta(hours=2),
            0.90,
            "future-observation",
        ),
    )
    source_root = workspace / "demo_scenarios"
    source_root.mkdir(parents=True, exist_ok=True)
    for valid_time, issue_time, value, label in cases:
        dataset = xr.Dataset(
            {"ice_conc": (("latitude", "longitude"), np.full((2, 3), value))},
            coords={"longitude": longitude, "latitude": latitude},
        )
        dataset["ice_conc"].attrs["units"] = "1"
        source_file = source_root / f"{label}.nc"
        dataset.to_netcdf(source_file, engine="h5netcdf")
        pipeline.ingest_netcdf(
            source_file,
            data_type="sea_ice_concentration",
            route_id="tromso_to_svalbard",
            issue_time=issue_time,
            valid_time=valid_time,
            source="synthetic-demo",
            issue_time_evidence=IssueTimeEvidence(
                issue_time=issue_time,
                method=IssueTimeMethod.EXPLICIT_CATALOG,
                authority="synthetic-demo fixture",
                reference=f"built-in demo case {label}",
                observed_at=max(issue_time, simulation_time + timedelta(hours=2)),
                raw_value=issue_time.isoformat(),
            ),
            version=label,
        )

    clock = SimulationClock(simulation_time)
    cache = PartitionedABCache(max_memory_mb=16)
    service = WorkPackageA(
        source=LocalArchiveSource(workspace), clock=clock, cache=cache
    )
    frames = service.prefetch(
        route_id="tromso_to_svalbard",
        data_types=["sea_ice_concentration"],
        horizon_hours=24,
    )
    visible_versions = [frame.record.version for frame in frames]
    print(json.dumps({
        "simulation_time": simulation_time.isoformat(),
        "visible_versions": visible_versions,
        "future_observation_hidden": "future-observation" not in visible_versions,
        "cache": cache.stats(),
        "workspace": str(workspace),
    }, ensure_ascii=False, indent=2))
    return 0 if "future-observation" not in visible_versions else 1


def _configure_native_libraries() -> None:
    if os.getenv("ECCODES_DIR"):
        return
    project_root = Path(__file__).resolve().parents[2]
    candidate = project_root / ".mamba-env"
    if (candidate / "lib").is_dir():
        os.environ["ECCODES_DIR"] = str(candidate)


if __name__ == "__main__":
    sys.exit(main())
