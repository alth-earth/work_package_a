"""Command line entry point for ingestion, archive queries and replay checks."""

from __future__ import annotations

import argparse
import json
import os
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
    Bounds,
    NativeForecastAcquirer,
)
from arctic_route_data.ingestion import IngestionPipeline
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod, SourceIssueTimeResolver
from arctic_route_data.legacy_downloaders import LEGACY_DOWNLOADERS, LegacyDownloaderRunner
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import QualityFlag
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.service import WorkPackageA
from arctic_route_data.sources import LocalArchiveSource
from arctic_route_data.specs import DATA_TYPE_SPECS
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

    replay = subparsers.add_parser("replay", help="按模拟时钟预取并输出 AB 缓存状态")
    replay.add_argument("--data-root", type=Path, default=Path("data"))
    replay.add_argument("--route-id", required=True)
    replay.add_argument("--at", required=True)
    replay.add_argument("--types", nargs="+", required=True, choices=sorted(DATA_TYPE_SPECS))
    replay.add_argument("--config", type=Path, default=Path("configs/work_package_a.toml"))
    replay.add_argument("--horizon-hours", type=int)
    replay.add_argument("--max-memory-mb", type=float)

    config_show = subparsers.add_parser("config-show", help="校验并显示实际生效的 TOML 配置")
    config_show.add_argument("--config", type=Path, default=Path("configs/work_package_a.toml"))

    acquire = subparsers.add_parser(
        "acquire-forecast", help="从 NOAA/Copernicus 获取完整未来窗并正式发布"
    )
    acquire.add_argument("--config", type=Path, default=Path("configs/work_package_a.toml"))
    acquire.add_argument("--data-root", type=Path, default=Path("data"))
    acquire.add_argument(
        "--corridor",
        "--scenario",
        dest="corridor",
        required=True,
        help="采集走廊 ID；--scenario 是 0.2 兼容别名",
    )
    acquire.add_argument("--sources", nargs="+", choices=("gfs", "copernicus"), default=("gfs",))
    acquire.add_argument("--types", nargs="+", choices=sorted(DATA_TYPE_SPECS))
    acquire.add_argument("--start", help="UTC ISO-8601；默认当前时刻")
    acquire.add_argument("--horizon-hours", type=int)

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
        clock = SimulationClock(parse_utc(args.at, field="at"))
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
        frames = service.prefetch(
            route_id=args.route_id,
            data_types=args.types,
            horizon_hours=args.horizon_hours or config.cache.target_horizon_hours,
        )
        print(json.dumps({
            "published": [frame.record.data_id for frame in frames],
            "health": service.health(),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "config-show":
        print(json.dumps(config_to_dict(load_config(args.config)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "acquire-forecast":
        config = load_config(args.config)
        try:
            corridor = config.corridors[args.corridor]
        except KeyError as exc:
            supported = ", ".join(sorted(config.corridors))
            raise ValueError(f"未知 corridor={args.corridor!r}；支持: {supported}") from exc
        start = parse_utc(args.start, field="start") if args.start else datetime.now(UTC)
        horizon = args.horizon_hours or config.cache.target_horizon_hours
        bounds = Bounds(
            west=corridor.bbox[0],
            south=corridor.bbox[1],
            east=corridor.bbox[2],
            north=corridor.bbox[3],
        )
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
                        route_id=corridor.corridor_id,
                        bounds=bounds,
                        as_of=start,
                        horizon_hours=horizon,
                        step_hours=config.acquisition.gfs_step_hours,
                        cycle_lookback_count=config.acquisition.cycle_lookback_count,
                        data_types=gfs_types,
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
                        route_id=corridor.corridor_id,
                        bounds=bounds,
                        start_time=start,
                        horizon_hours=horizon,
                        data_types=copernicus_types,
                    )
                )
        if not results:
            parser.error("sources/types 组合没有产生任何采集任务")
        print(
            json.dumps(
                {
                    "corridor_id": corridor.corridor_id,
                    "requested_start": start.isoformat(),
                    "horizon_hours": horizon,
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
