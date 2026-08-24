"""CARRA (C3S/ECMWF) single-levels reanalysis acquisition for Arctic Route A.

This module is the CARRA adapter for proposal ``A-WINTER-MET-001`` (APPROVED
2026-08-22). It fills the three missing winter rows (``wind_field``,
``temperature``, ``visibility``) for the 2026-02-15..02-21 Tromso->Isfjorden
window using the CARRA East domain.

Key facts validated by the 2026-08-22 single-frame probe (see
``WINTER_CARRA_PLAN_B_OPERATION_GUIDE.md``):

* CARRA single-levels surface wind (``10u``/``10v``) is already reported as
  **eastward/northward** (true-east/true-north) components
  (``standard_name = eastward_wind / northward_wind``). No grid-relative wind
  rotation is required -- the rotation risk flagged in earlier reports does NOT
  apply to CARRA single-levels surface wind.
* The grid is a **2D curvilinear** projection (``latitude``/``longitude`` are
  both 2D arrays, 0..360 longitude). We align it to the route rectilinear target
  grid with the existing ``curvilinear.regrid_nearest_curvilinear`` nearest-neighbour
  regrid already used by A's sea-ice / wind normalisation.
* ``vis`` lives on the ``surface`` level, ``2t`` on ``heightAboveGround 2``,
  ``10u``/``10v`` on ``heightAboveGround 10``. cfgrib cannot merge those into a
  single dataset; we read each shortName separately and merge with
  ``compat="override"``.
* Credentials are read from ``~/.cdsapirc`` or the ``CDSAPI_RC`` environment
  variable (the operator placed theirs at
  ``${ARCTIC_ROUTE_ROOT}/work_package_a/.cdsapirc``). The token is NEVER read into
  this module, printed, or committed.

Safety / scope:
* This adapter does NOT modify any NCEI / Copernicus acquisition path.
* ``acquire_carra`` defaults to ``publish=False`` (dry-run): it downloads,
  parses, crops and validates but does NOT publish into A's incoming pipeline.
  Full publication requires explicit ``publish=True`` after ``A-WINTER-MET-001``
  approval and a successful single-frame smoke.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.curvilinear import regrid_nearest_curvilinear
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.timeutils import ensure_utc

# --- CARRA request identity (mirrors the operator-validated API request) -------
CARRA_DATASET_ID = "reanalysis-carra-single-levels"
CARRA_DOMAIN = "east_domain"
CARRA_PRODUCT_TYPE = "analysis"
CARRA_DATA_FORMAT = "grib"
CARRA_SOURCE_LABEL = "C3S/ECMWF CARRA Single-Levels (East domain)"

# Map A data types -> CARRA dataset *variable display names* accepted by the CDS
# API (these are the names from the operator-validated request, NOT GRIB
# shortNames). Verified 2026-08-22: the API rejects GRIB shortNames (e.g. "10u")
# and requires these long names.
CARRA_DATA_TYPE_TO_API_VARIABLES: dict[str, tuple[str, ...]] = {
    "wind_field": ("10m_u_component_of_wind", "10m_v_component_of_wind"),
    "temperature": ("2m_temperature",),
    "visibility": ("visibility",),
}

# Map A data types -> CARRA GRIB *shortNames* used for local parsing with cfgrib.
# wind_field uses true-east/true-north components directly (no rotation needed).
CARRA_DATA_TYPE_TO_SHORTNAMES: dict[str, tuple[str, ...]] = {
    "wind_field": ("10u", "10v"),
    "temperature": ("2t",),
    "visibility": ("vis",),
}

# Allowed A data types for CARRA acquisition.
CARRA_SUPPORTED_DATA_TYPES = tuple(CARRA_DATA_TYPE_TO_API_VARIABLES.keys())

# Nominal cadence of CARRA analysis fields (3 h).
CARRA_NOMINAL_INTERVAL_HOURS = 3.0

# Target regrid maximum distance (km). CARRA East 2.5 km grid -> generous cap so
# coastal/route cells still receive a finite value without over-reaching.
CARRA_REGRID_MAX_DISTANCE_KM = 25.0

# CARRA analysis covers through 2026-05-31 (machine catalogue, validated).
CARRA_COVERAGE_END = datetime(2026, 5, 31, 23, 59, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CarraAcquisitionResult:
    source: str
    route_id: str
    source_snapshot_ids: tuple[str, ...]
    frames_processed: int
    frames_published: int
    published: bool


def _cds_client() -> Any:
    """Build a cdsapi.Client without ever touching the token contents."""
    try:
        import cdsapi
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "请安装 acquisition extra (cdsapi) 以采集 CARRA 数据"
        ) from exc
    # cdsapi reads ~/.cdsapirc or the CDSAPI_RC env var automatically.
    return cdsapi.Client()


def _carra_request(
    *,
    cycle: datetime,
    data_types: Iterable[str],
) -> dict[str, Any]:
    """Build the CARRA retrieve request dict for one analysis cycle."""
    short_names: set[str] = set()
    for dt in data_types:
        short_names.update(CARRA_DATA_TYPE_TO_API_VARIABLES[dt])
    return {
        "domain": CARRA_DOMAIN,
        "level_type": "surface_or_atmosphere",
        "variable": sorted(short_names),
        "product_type": CARRA_PRODUCT_TYPE,
        "year": [f"{cycle.year:04d}"],
        "month": [f"{cycle.month:02d}"],
        "day": [f"{cycle.day:02d}"],
        "time": [f"{cycle.hour:02d}:00"],
        "data_format": CARRA_DATA_FORMAT,
    }


def _retrieve_carra_frame(
    *,
    client: Any,
    cycle: datetime,
    data_types: Iterable[str],
    out_path: Path,
) -> Path:
    """Download a single CARRA analysis frame to ``out_path`` (no publish)."""
    request = _carra_request(cycle=cycle, data_types=data_types)
    client.retrieve(CARRA_DATASET_ID, request).download(str(out_path))
    return out_path


def _wrap_longitude(dataset: xr.Dataset) -> xr.Dataset:
    """Wrap 0..360 longitude to -180..180 for consistency with A's grid."""
    lon = dataset["longitude"].values
    if np.nanmax(lon) > 180:
        wrapped = (lon + 180.0) % 360.0 - 180.0
        dataset = dataset.assign_coords(
            longitude=("longitude" if lon.ndim == 1 else ("y", "x"), wrapped)
            if lon.ndim == 1
            else dataset["longitude"]
        )
        if lon.ndim == 2:
            # 2D curvilinear: wrap the 2D coordinate in place.
            new_lon = (lon + 180.0) % 360.0 - 180.0
            dataset = dataset.assign_coords(longitude=(("y", "x"), new_lon))
        dataset = dataset.sortby("longitude") if lon.ndim == 1 else dataset
    return dataset


def _open_carra_frame(
    *,
    path: Path,
    data_types: Iterable[str],
) -> dict[str, xr.Dataset]:
    """Open one CARRA GRIB frame and split into per-A-data_type datasets.

    Reads each shortName separately (different vertical levels cannot be merged
    by cfgrib), merges with ``compat="override"``, wraps longitude, and returns
    a dict keyed by A data type.
    """
    if importlib.util.find_spec("cfgrib") is None:  # dependency guard
        raise RuntimeError("请安装 acquisition extra (cfgrib) 以读取 CARRA GRIB")
    import cfgrib  # noqa: F401  (used by xr.open_dataset engine="cfgrib")

    requested = tuple(dict.fromkeys(data_types))
    short_to_dt: dict[str, str] = {}
    ordered_short_names: list[str] = []
    for dt in requested:
        for sn in CARRA_DATA_TYPE_TO_SHORTNAMES[dt]:
            short_to_dt.setdefault(sn, dt)
            if sn not in ordered_short_names:
                ordered_short_names.append(sn)

    arrays: dict[str, list[xr.DataArray]] = {dt: [] for dt in requested}
    with xr.set_options(use_new_combine_kwarg_defaults=True):
        for sn in ordered_short_names:
            ds = xr.open_dataset(
                str(path),
                engine="cfgrib",
                backend_kwargs={"indexpath": ""},
                filter_by_keys={"shortName": sn},
            )
            ds = _wrap_longitude(ds)
            # Pick the data_var matching the shortName (cfgrib keeps shortName as name).
            var = ds[sn] if sn in ds.data_vars else next(iter(ds.data_vars.values()))
            arrays[short_to_dt[sn]].append(var)

    results: dict[str, xr.Dataset] = {}
    for dt, vars_ in arrays.items():
        if not vars_:
            raise ValueError(f"CARRA frame {path.name} 缺少 {dt} 所需变量")
        results[dt] = xr.merge(vars_, compat="override", join="exact")
    return results


def _regrid_to_bounds(
    dataset: xr.Dataset,
    *,
    target_lon: np.ndarray,
    target_lat: np.ndarray,
) -> xr.Dataset:
    """Regrid a 2D curvilinear CARRA frame to the route's rectilinear target.

    Uses A's existing nearest-neighbour curvilinear regrid (no interpolation of
    vector direction beyond coordinate projection; wind is already true-east/
    true-north so component-wise regrid is physically correct).
    """
    native_lon = np.asarray(dataset["longitude"].values, dtype=np.float64)
    native_lat = np.asarray(dataset["latitude"].values, dtype=np.float64)
    out_vars: dict[str, xr.DataArray] = {}
    for name, da in dataset.data_vars.items():
        values = np.asarray(da.values, dtype=np.float64)
        regridded = regrid_nearest_curvilinear(
            values=values,
            native_lon=native_lon,
            native_lat=native_lat,
            target_lon=target_lon,
            target_lat=target_lat,
            max_distance_km=CARRA_REGRID_MAX_DISTANCE_KM,
        )
        out_vars[name] = xr.DataArray(
            regridded,
            dims=("latitude", "longitude"),
            coords={"latitude": target_lat, "longitude": target_lon},
            attrs={k: v for k, v in da.attrs.items() if k != "coordinates"},
            name=name,
        )
    result = xr.Dataset(out_vars)
    result.attrs.update(dataset.attrs)
    return result


def _target_grid(
    bounds: Any,
    *,
    step_deg: float = 0.1,
) -> tuple[np.ndarray, np.ndarray]:
    """Build a rectilinear target grid covering ``bounds`` (reused by regrid)."""
    lons = np.arange(bounds.west, bounds.east + step_deg / 2, step_deg)
    lats = np.arange(bounds.south, bounds.north + step_deg / 2, step_deg)
    return lons, lats


def _issue_evidence_for(cycle: datetime, observed_at: datetime) -> IssueTimeEvidence:
    """CARRA issue-time evidence: conservative retrieval (non-authoritative)."""
    return IssueTimeEvidence(
        issue_time=cycle,
        method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
        authority="C3S/CDS CARRA retrieval time",
        reference=f"cdsapi retrieve {CARRA_DATASET_ID} {cycle:%Y%m%dT%HZ}",
        observed_at=observed_at,
        raw_value=cycle.strftime("%Y-%m-%dT%H:00:00Z"),
        authoritative=False,
    )


@dataclass
class CarraAcquisition:
    """CARRA acquisition driver (proposal ``A-WINTER-MET-001``, APPROVED 2026-08-22).

    ``publish`` defaults to ``False``: the driver downloads, parses, regrids and
    validates but does NOT publish into A's incoming pipeline. Set
    ``publish=True`` for the approved full six-day acquisition (gates G5/G6);
    use ``acquire_carra_dry_run`` / ``acquire_frame_smoke`` for validation.
    """

    route_id: str
    bounds: Any
    data_types: tuple[str, ...] = CARRA_SUPPORTED_DATA_TYPES
    publish: bool = False
    publisher: Any | None = field(default=None, repr=False)
    data_root: Path = field(default=Path("/tmp/carra_dry_run"), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_root", Path(self.data_root))
        unsupported = sorted(set(self.data_types) - set(CARRA_SUPPORTED_DATA_TYPES))
        if unsupported:
            raise ValueError(f"CARRA 不支持的数据类型: {', '.join(unsupported)}")

    # -- single-frame smoke ---------------------------------------------------
    def acquire_frame_smoke(self, cycle: datetime) -> dict[str, xr.Dataset]:
        """Download + parse + regrid ONE cycle. No publish. Smoke/test only."""
        cycle = ensure_utc(cycle, field="cycle")
        self.data_root.mkdir(parents=True, exist_ok=True)
        out = self.data_root / f"carra_{cycle:%Y%m%dT%HZ}.grib"
        client = _cds_client()
        _retrieve_carra_frame(
            client=client, cycle=cycle, data_types=self.data_types, out_path=out
        )
        per_type = _open_carra_frame(path=out, data_types=self.data_types)
        target_lon, target_lat = _target_grid(self.bounds)
        regridded: dict[str, xr.Dataset] = {}
        for dt, ds in per_type.items():
            regridded[dt] = _regrid_to_bounds(
                ds, target_lon=target_lon, target_lat=target_lat
            )
        return regridded

    # -- full acquisition (publish gated) -------------------------------------
    def acquire(
        self,
        *,
        start_time: datetime,
        horizon_hours: int,
    ) -> CarraAcquisitionResult:
        """Acquire CARRA frames every 3 h over the window.

        With ``publish=False`` (default) this is a dry-run: frames are
        downloaded/parsed/regridded and validated, but nothing enters A's
        provenance pipeline. With ``publish=True`` it publishes each data type
        via ``self.publisher.publish_dataset`` using the same atomic
        ``.part -> sidecar`` flow as NCEI/Copernicus acquisitions.
        """
        if horizon_hours <= 0:
            raise ValueError("horizon_hours 必须大于 0")
        start = ensure_utc(start_time, field="start_time")
        if start > CARRA_COVERAGE_END:
            raise ValueError(
                f"CARRA coverage ends {CARRA_COVERAGE_END:%Y-%m-%d}; "
                f"start {start:%Y-%m-%d} 超出范围"
            )
        end = start + timedelta(hours=horizon_hours)
        cycles: list[datetime] = []
        current = start
        while current <= end:
            cycles.append(current)
            current += timedelta(hours=3)

        client = _cds_client()
        observed_at = datetime.now(UTC)
        snapshots: list[str] = []
        frames_processed = 0
        frames_published = 0

        for cycle in cycles:
            if cycle > CARRA_COVERAGE_END:
                break
            out = self.data_root / f"carra_{cycle:%Y%m%dT%HZ}.grib"
            _retrieve_carra_frame(
                client=client, cycle=cycle, data_types=self.data_types, out_path=out
            )
            per_type = _open_carra_frame(path=out, data_types=self.data_types)
            target_lon, target_lat = _target_grid(self.bounds)
            snapshot_id = (
                f"carra-{CARRA_DOMAIN}-{cycle:%Y%m%dT%HZ}-"
                f"{'-'.join(self.data_types)}"
            )
            snapshots.append(snapshot_id)
            evidence = _issue_evidence_for(cycle, observed_at)
            for dt, src in per_type.items():
                dataset = _regrid_to_bounds(
                    src, target_lon=target_lon, target_lat=target_lat
                )
                # CARRA single-level analysis has no time axis; the analysis
                # cycle IS the valid time. The publisher requires a time/valid_time
                # coordinate for temporal slicing, so pin it to the cycle.
                dataset = dataset.assign_coords(time=np.datetime64(cycle))
                dataset.attrs.update(
                    {
                        "product_id": "CARRA_SINGLE_LEVELS",
                        "forecast_reference_time": cycle.isoformat(),
                        "source_snapshot_id": snapshot_id,
                    }
                )
                frames_processed += 1
                if self.publish and self.publisher is not None:
                    self.publisher.publish_dataset(
                        dataset,
                        data_type=dt,
                        route_id=self.route_id,
                        source=CARRA_SOURCE_LABEL,
                        version=snapshot_id,
                        issue_evidence=evidence,
                        metadata={
                            "product_kind": "analysis",
                            "acquisition_mode": "retrospective_best_estimate",
                            "source_fidelity": "carra_3h_analysis",
                            "source_snapshot_id": snapshot_id,
                            "analysis_cycle_id": f"{cycle:%Y%m%dT%HZ}",
                            "forecast_lead_hours": 0,
                            "nominal_interval_hours": CARRA_NOMINAL_INTERVAL_HOURS,
                            "domain": CARRA_DOMAIN,
                        },
                    )
                    frames_published += 1
        return CarraAcquisitionResult(
            source=CARRA_SOURCE_LABEL,
            route_id=self.route_id,
            source_snapshot_ids=tuple(snapshots),
            frames_processed=frames_processed,
            frames_published=frames_published,
            published=self.publish,
        )


def acquire_carra_dry_run(
    *,
    route_id: str,
    bounds: Any,
    cycle: datetime,
    data_types: Iterable[str] = CARRA_SUPPORTED_DATA_TYPES,
    data_root: Path = Path("/tmp/carra_dry_run"),
) -> dict[str, xr.Dataset]:
    """Convenience single-frame smoke entry point (no publish, no side effects

    beyond a temporary GRIB under ``data_root``).

    Returns the regridded per-data-type datasets for inspection/assertion.
    """
    driver = CarraAcquisition(
        route_id=route_id,
        bounds=bounds,
        data_types=tuple(dict.fromkeys(data_types)),
        publish=False,
        data_root=data_root,
    )
    return driver.acquire_frame_smoke(cycle)
