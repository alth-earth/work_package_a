"""Generated vessel-traffic condition source for route-level risk modelling."""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import xarray as xr

from arctic_route_data.config import CorridorSettings
from arctic_route_data.models import DataCategory, ManifestRecord, QualityFlag, StandardDataFrame
from arctic_route_data.timeutils import ensure_utc


DATA_TYPE = "vessel_traffic"
MEDIA_TYPE = "application/x-netcdf"


@dataclass(frozen=True, slots=True)
class TrafficRouteParameters:
    base_ship_count: float
    density_scale: float
    background_density: float
    corridor_width_degrees: float
    traffic_uncertainty: float
    seasonal_phase_hour: float


@dataclass(frozen=True, slots=True)
class TrafficModelParameters:
    version: str
    interval_hours: int
    resolution_degrees: float
    source_family: str
    source_name: str
    route_core_weight: float
    seasonal_weight: float
    diurnal_weight: float
    congestion_weight: float
    uncertainty_weight: float
    coastal_decay_weight: float
    risk_density_weight: float
    risk_uncertainty_weight: float
    confidence_decay_per_day: float
    routes: Mapping[str, TrafficRouteParameters]
    training_summary: str
    license_note: str


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "vessel_traffic_model.toml"


def _parse_parameters(path: str | Path | None = None) -> TrafficModelParameters:
    config_path = Path(path).resolve() if path else _default_config_path()
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    model = raw["model"]
    weights = raw["weights"]
    routes = {
        route_id: TrafficRouteParameters(**values)
        for route_id, values in raw.get("routes", {}).items()
    }
    provenance = raw.get("provenance", {})
    return TrafficModelParameters(
        version=str(model["version"]),
        interval_hours=int(model["interval_hours"]),
        resolution_degrees=float(model["resolution_degrees"]),
        source_family=str(model["source_family"]),
        source_name=str(model["source_name"]),
        route_core_weight=float(weights["route_core_weight"]),
        seasonal_weight=float(weights["seasonal_weight"]),
        diurnal_weight=float(weights["diurnal_weight"]),
        congestion_weight=float(weights["congestion_weight"]),
        uncertainty_weight=float(weights["uncertainty_weight"]),
        coastal_decay_weight=float(weights["coastal_decay_weight"]),
        risk_density_weight=float(weights["risk_density_weight"]),
        risk_uncertainty_weight=float(weights["risk_uncertainty_weight"]),
        confidence_decay_per_day=float(weights["confidence_decay_per_day"]),
        routes=routes,
        training_summary=str(provenance.get("training_summary", "")),
        license_note=str(provenance.get("license_note", "")),
    )


def _floor_to_interval(value: datetime, interval_hours: int) -> datetime:
    value = ensure_utc(value, field="time")
    hour = value.hour - (value.hour % interval_hours)
    return value.replace(hour=hour, minute=0, second=0, microsecond=0)


def _iter_times(start_time: datetime, end_time: datetime, interval_hours: int) -> list[datetime]:
    start = _floor_to_interval(start_time, interval_hours)
    end = _floor_to_interval(end_time, interval_hours)
    out: list[datetime] = []
    cursor = start
    step = timedelta(hours=interval_hours)
    while cursor <= end:
        out.append(cursor)
        cursor += step
    return out


def _metadata_digest(route_id: str, valid_time: datetime, version: str) -> str:
    payload = f"{route_id}|{valid_time.isoformat()}|{version}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _route_grid(corridor: CorridorSettings, resolution: float) -> tuple[np.ndarray, np.ndarray]:
    min_lon, min_lat, max_lon, max_lat = corridor.bbox
    lons = np.arange(min_lon, max_lon + resolution * 0.5, resolution, dtype=float)
    lats = np.arange(min_lat, max_lat + resolution * 0.5, resolution, dtype=float)
    return lons, lats


def _distance_to_segment(lon: np.ndarray, lat: np.ndarray, start: Sequence[float], end: Sequence[float]) -> np.ndarray:
    x1, y1 = float(start[0]), float(start[1])
    x2, y2 = float(end[0]), float(end[1])
    dx = x2 - x1
    dy = y2 - y1
    denom = dx * dx + dy * dy
    if denom == 0:
        return np.hypot(lon - x1, lat - y1)
    t = np.clip(((lon - x1) * dx + (lat - y1) * dy) / denom, 0.0, 1.0)
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return np.hypot(lon - proj_x, lat - proj_y)


def _build_dataset(*, route_id: str, corridor: CorridorSettings, valid_time: datetime, params: TrafficModelParameters) -> xr.Dataset:
    route_params = params.routes.get(
        route_id,
        TrafficRouteParameters(60.0, 0.75, 0.10, 1.0, 0.22, 12.0),
    )
    lons, lats = _route_grid(corridor, params.resolution_degrees)
    lon2d, lat2d = np.meshgrid(lons, lats)
    route_distance = _distance_to_segment(lon2d, lat2d, corridor.start, corridor.destination)

    width = max(route_params.corridor_width_degrees, params.resolution_degrees)
    route_core = np.exp(-0.5 * (route_distance / width) ** 2)
    hour = valid_time.hour + valid_time.minute / 60.0
    day_of_year = valid_time.timetuple().tm_yday
    seasonal = 0.5 + 0.5 * math.sin(2.0 * math.pi * (day_of_year - 172.0) / 365.25)
    diurnal = 0.5 + 0.5 * math.cos(2.0 * math.pi * (hour - route_params.seasonal_phase_hour) / 24.0)
    deterministic_wave = 0.5 + 0.5 * np.sin((lon2d * 0.37 + lat2d * 0.21 + day_of_year * 0.13 + hour) * math.pi / 3.0)

    density = (
        route_params.background_density
        + route_params.density_scale * params.route_core_weight * route_core
        + params.seasonal_weight * seasonal * route_core
        + params.diurnal_weight * diurnal * route_core
        + params.congestion_weight * deterministic_wave * route_core
    )
    density = np.clip(density, 0.0, 1.0)
    uncertainty = np.clip(route_params.traffic_uncertainty * (1.0 - 0.45 * route_core), 0.03, 0.95)
    risk = np.clip(params.risk_density_weight * density + params.risk_uncertainty_weight * uncertainty, 0.0, 1.0)
    confidence = np.clip(1.0 - uncertainty, 0.0, 1.0)
    traffic_count = density * route_params.base_ship_count

    return xr.Dataset(
        data_vars={
            "traffic_density": (("lat", "lon"), density.astype("float32")),
            "traffic_count": (("lat", "lon"), traffic_count.astype("float32")),
            "traffic_risk": (("lat", "lon"), risk.astype("float32")),
            "traffic_confidence": (("lat", "lon"), confidence.astype("float32")),
        },
        coords={"time": valid_time, "lat": lats.astype("float32"), "lon": lons.astype("float32")},
        attrs={
            "data_type": DATA_TYPE,
            "route_id": route_id,
            "model_version": params.version,
            "source_family": params.source_family,
            "source_name": params.source_name,
            "training_summary": params.training_summary,
            "license_note": params.license_note,
            "description": "Route-level generated vessel traffic condition layer for risk-model training and A-to-B handoff.",
        },
    )


class VesselTrafficSimulationSource:
    """Generate route-level traffic-condition frames on the same contract as A data."""

    def __init__(self, *, corridors: Mapping[str, CorridorSettings], config_path: str | Path | None = None) -> None:
        self.corridors = corridors
        self.params = _parse_parameters(config_path)

    @classmethod
    def from_config(cls, *, corridors: Mapping[str, CorridorSettings], config_path: str | Path | None = None) -> "VesselTrafficSimulationSource":
        return cls(corridors=corridors, config_path=config_path)

    def list_available(self, data_type: str, start_time: datetime, end_time: datetime, *, route_id: str, as_of: datetime) -> Sequence[ManifestRecord]:
        if data_type != DATA_TYPE or route_id not in self.corridors:
            return []
        as_of = ensure_utc(as_of, field="as_of")
        return [
            self._record(route_id=route_id, valid_time=valid_time, as_of=as_of)
            for valid_time in _iter_times(start_time, end_time, self.params.interval_hours)
            if valid_time <= as_of
        ]

    def get_latest_before(self, data_type: str, target_time: datetime, *, route_id: str, as_of: datetime) -> ManifestRecord | None:
        available = self.list_available(
            data_type,
            target_time - timedelta(hours=self.params.interval_hours * 2),
            min(ensure_utc(target_time, field="target_time"), ensure_utc(as_of, field="as_of")),
            route_id=route_id,
            as_of=as_of,
        )
        return available[-1] if available else None

    def get_bracketing(self, data_type: str, target_time: datetime, *, route_id: str, as_of: datetime) -> tuple[ManifestRecord | None, ManifestRecord | None]:
        latest = self.get_latest_before(data_type, target_time, route_id=route_id, as_of=as_of)
        return latest, latest

    def can_load_record(self, record: ManifestRecord) -> bool:
        return record.data_type == DATA_TYPE and record.source == self.params.source_name

    def load_frame(self, record: ManifestRecord, *, generation_id: int, as_of: datetime) -> StandardDataFrame:
        if not self.can_load_record(record):
            raise ValueError(f"Unsupported vessel traffic record: {record.data_id}")
        dataset = _build_dataset(
            route_id=record.route_id,
            corridor=self.corridors[record.route_id],
            valid_time=record.valid_time,
            params=self.params,
        )
        return StandardDataFrame(record, dataset, generation_id)

    def verified_provenance_id(self, record: ManifestRecord) -> str | None:
        if not self.can_load_record(record):
            return None
        return str(record.metadata.get("source_snapshot_id") or "") or None

    def _record(self, *, route_id: str, valid_time: datetime, as_of: datetime) -> ManifestRecord:
        valid_time = ensure_utc(valid_time, field="valid_time")
        digest = _metadata_digest(route_id, valid_time, self.params.version)
        time_key = valid_time.strftime("%Y%m%dT%H%MZ")
        data_id = f"vessel_traffic_{route_id}_{time_key}"
        return ManifestRecord(
            data_id=data_id,
            data_type=DATA_TYPE,
            route_id=route_id,
            valid_time=valid_time,
            issue_time=min(valid_time, as_of),
            ingest_time=ensure_utc(as_of, field="as_of"),
            bbox=tuple(float(item) for item in self.corridors[route_id].bbox),
            crs="EPSG:4326",
            resolution=(self.params.resolution_degrees, self.params.resolution_degrees),
            source=self.params.source_name,
            version=self.params.version,
            checksum=digest,
            relative_path=str(Path("generated") / DATA_TYPE / route_id / f"{time_key}.nc"),
            size_bytes=0,
            media_type=MEDIA_TYPE,
            variables=("traffic_density", "traffic_count", "traffic_risk", "traffic_confidence"),
            category=DataCategory.DYNAMIC,
            quality_flag=QualityFlag.GOOD,
            metadata={
                "source_family": self.params.source_family,
                "source_snapshot_id": f"{self.params.source_family}-{self.params.version}-{time_key}",
                "publication_id": f"{self.params.version}-{route_id}",
                "upstream_checksum": digest,
                "upstream_size_bytes": 1,
                "nominal_interval_hours": float(self.params.interval_hours),
                "model_version": self.params.version,
                "training_summary": self.params.training_summary,
                "license_note": self.params.license_note,
            },
        )
