"""Optional adapter from system-wide shared facts into Work Package A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from arctic_route_data.forecast_acquisition import AcquisitionMode, Bounds


@dataclass(frozen=True, slots=True)
class SharedScenarioRequest:
    scenario: Any
    corridor: Any
    vessel: Any
    route_id: str
    bounds: Bounds
    start: datetime
    end: datetime
    horizon_hours: int
    mode: AcquisitionMode


def load_shared_scenario_request(
    *,
    scenario_id: str,
    config_root: str | Path | None = None,
    simulation_start: datetime | None = None,
    candidate_route_distance_nm: float | None = None,
) -> SharedScenarioRequest:
    try:
        from arctic_route_contracts import (
            load_corridor,
            load_scenario,
            load_vessel_profile,
            materialize_frozen_forecast,
        )
    except ImportError as exc:
        raise RuntimeError(
            "场景适配需要相邻 arctic_route_contracts 包；"
            "请用 uv 安装 ../arctic_route_contracts"
        ) from exc
    root = Path(config_root) if config_root is not None else None
    scenario = load_scenario(root, scenario_id)
    corridor = load_corridor(root, scenario.corridor_id)
    vessel = load_vessel_profile(root, scenario.default_vessel_profile_id)
    if scenario.is_template:
        if simulation_start is None:
            raise ValueError("冻结预测模板必须显式提供 simulation_start")
        selected_horizon = None
        if candidate_route_distance_nm is not None:
            selected_horizon = corridor.horizon_policy.recommend_hours(
                great_circle_distance_nm=corridor.great_circle_distance_nm,
                nominal_speed_knots=vessel.nominal_speed_knots,
                candidate_route_distance_nm=candidate_route_distance_nm,
            )
        scenario = materialize_frozen_forecast(
            scenario,
            simulation_start,
            horizon_hours=selected_horizon,
        )
    elif simulation_start is not None and simulation_start != scenario.simulation_start:
        raise ValueError("具体共享场景不允许用 simulation_start 静默改写时域")
    elif candidate_route_distance_nm is not None:
        raise ValueError("具体共享场景不允许用候选距离静默改写时域")
    if scenario.simulation_start is None or scenario.simulation_end is None:
        raise ValueError("共享场景必须物化为具体 UTC 时间窗")
    return SharedScenarioRequest(
        scenario=scenario,
        corridor=corridor,
        vessel=vessel,
        route_id=corridor.corridor_id,
        bounds=Bounds(
            corridor.data_bbox.west,
            corridor.data_bbox.south,
            corridor.data_bbox.east,
            corridor.data_bbox.north,
        ),
        start=scenario.simulation_start,
        end=scenario.simulation_end,
        horizon_hours=scenario.horizon_hours,
        mode=AcquisitionMode(scenario.mode.value),
    )


def create_run_context_from_bundle(
    *,
    request: SharedScenarioRequest,
    bundle_path: str | Path,
    output_path: str | Path,
    run_id: str | None = None,
) -> Any:
    try:
        from arctic_route_contracts import (
            create_run_context,
            load_dataset_bundle,
            write_run_context_atomic,
        )
    except ImportError as exc:
        raise RuntimeError("缺少 arctic_route_contracts，无法创建 RunContext") from exc
    context = create_run_context(
        scenario=request.scenario,
        corridor=request.corridor,
        vessel=request.vessel,
        dataset_bundle=load_dataset_bundle(bundle_path),
        run_id=run_id,
    )
    write_run_context_atomic(context, output_path)
    return context
