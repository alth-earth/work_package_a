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

import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
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
#
# Scope note: CARRA is one of the allowed source families for exactly three A data
# types -- ``wind_field``, ``temperature`` and ``visibility`` -- and is registered
# in ``specs.DataTypeSpec.source_families`` as the normalized key ``"c3s_carra"``
# (alongside the primary ``"noaa_gfs"``). It is the winter fill-in source approved
# by proposal A-WINTER-MET-001 (2026-08-22). The public entry accepts any
# registered corridor whose bounds pass the East-domain gate; the proposal's
# February window remains the validated smoke/evidence window, not a runtime
# temporal allow-list. Adding a new CARRA-backed data type here MUST also extend
# ``specs.py`` source_families and the governance record.
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

# Allowed A data types for CARRA acquisition.  CARRA is deliberately not a
# fallback for the other A variables: its single-level dataset only supplies
# these three meteorological fields.
CARRA_SUPPORTED_DATA_TYPES = tuple(CARRA_DATA_TYPE_TO_API_VARIABLES.keys())

# Public acquisition limits.  The source's catalogue coverage is intentionally
# not copied into a stale date constant: each requested cycle is retrieved
# explicitly and an incomplete/failed cycle fails the run closed.
CARRA_MAX_HORIZON_HOURS = 216
CARRA_CYCLE_HOURS = 3

# The East-domain GRIB observed by the adapter spans approximately 60.5..85.1N.
# Keep a conservative spatial gate for registered corridors.  Longitude is
# cyclic in the native 0..360 grid, while A's Bounds uses [-180, 180], so the
# latitude gate is the meaningful preflight check; the finite regrid check below
# verifies the actual requested target cells as well.
CARRA_EAST_DOMAIN_LATITUDE_MIN = 60.5
CARRA_EAST_DOMAIN_LATITUDE_MAX = 85.0

# Nominal cadence of CARRA analysis fields (3 h).
CARRA_NOMINAL_INTERVAL_HOURS = 3.0

# Target regrid maximum distance (km). CARRA East 2.5 km grid -> generous cap so
# coastal/route cells still receive a finite value without over-reaching.
CARRA_REGRID_MAX_DISTANCE_KM = 25.0

@dataclass(frozen=True, slots=True)
class CarraAcquisitionResult:
    source: str
    route_id: str
    source_snapshot_ids: tuple[str, ...]
    frames_processed: int
    frames_published: int
    published: bool
    cycles_requested: int = 0
    cache_hits: int = 0
    downloaded_cycles: int = 0


@dataclass(frozen=True, slots=True)
class CarraWindow:
    """Explicit inclusive CARRA cycle window used by the public CLI."""

    start: datetime
    end: datetime
    horizon_hours: int
    cycles: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class _RawFrame:
    path: Path
    sidecar: Path
    request: dict[str, Any]
    request_sha256: str
    cache_hit: bool


_RAW_CACHE_SCHEMA = "a.carra-raw-cache.v1"
_SAFE_REASON = re.compile(r"[^A-Za-z0-9._-]+")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability for POSIX; harmless on Windows."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def validate_carra_bounds(bounds: Any) -> None:
    """Reject corridors outside the validated CARRA East latitude domain."""

    try:
        west = float(bounds.west)
        south = float(bounds.south)
        east = float(bounds.east)
        north = float(bounds.north)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("CARRA bounds 必须提供 west/south/east/north 数值") from exc
    if not all(np.isfinite(value) for value in (west, south, east, north)):
        raise ValueError("CARRA bounds 不得包含 NaN 或无穷值")
    if west >= east or south >= north:
        raise ValueError("CARRA bounds 的经纬度顺序无效")
    if south < CARRA_EAST_DOMAIN_LATITUDE_MIN or north > CARRA_EAST_DOMAIN_LATITUDE_MAX:
        raise ValueError(
            "CARRA East domain 不覆盖该 corridor 纬度；"
            f"支持约 {CARRA_EAST_DOMAIN_LATITUDE_MIN:g}.."
            f"{CARRA_EAST_DOMAIN_LATITUDE_MAX:g}N"
        )


def _is_three_hour_boundary(value: datetime) -> bool:
    return (
        value.minute == 0
        and value.second == 0
        and value.microsecond == 0
        and value.hour % 3 == 0
    )


def resolve_carra_window(
    *,
    start_time: datetime,
    end_time: datetime,
    now: datetime | None = None,
) -> CarraWindow:
    """Validate an inclusive, UTC, 3-hour CARRA request window.

    CARRA is a reanalysis product rather than a forecast source.  Future
    cycles are therefore rejected before a client is created.  The source
    catalogue's temporal end is not duplicated here; a missing/unavailable
    historical cycle is discovered by its explicit retrieve call and aborts
    the run.
    """

    start = ensure_utc(start_time, field="start")
    end = ensure_utc(end_time, field="end")
    if not _is_three_hour_boundary(start) or not _is_three_hour_boundary(end):
        raise ValueError("CARRA start/end 必须落在 UTC 的 3 小时边界（00/03/06/...）")
    if end < start:
        raise ValueError("CARRA end 必须不早于 start")
    seconds = (end - start).total_seconds()
    if seconds % 3600:
        raise ValueError("CARRA start/end 必须相差整小时")
    horizon_hours = int(seconds // 3600)
    if horizon_hours % CARRA_CYCLE_HOURS:
        raise ValueError("CARRA start/end 跨度必须是 3 小时的整数倍")
    if horizon_hours > CARRA_MAX_HORIZON_HOURS:
        raise ValueError(
            f"CARRA 请求跨度不得超过 {CARRA_MAX_HORIZON_HOURS} 小时"
        )
    current = ensure_utc(now or datetime.now(UTC), field="now")
    if end > current:
        raise ValueError("CARRA 是再分析数据，不允许请求未来时次")
    cycles = tuple(
        start + timedelta(hours=offset)
        for offset in range(0, horizon_hours + 1, CARRA_CYCLE_HOURS)
    )
    return CarraWindow(start, end, horizon_hours, cycles)


def validate_cdsapi_rc_file(path: str | Path) -> Path:
    """Validate an external CDS credentials file without reading its secret."""

    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise ValueError("CDS API 凭据文件必须使用外部绝对路径")
    resolved = expanded.resolve()
    try:
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError as exc:
        raise ValueError(f"CDS API 凭据文件不可读取: {resolved}") from exc
    if not resolved.is_file():
        raise ValueError(f"CDS API 凭据路径不是文件: {resolved}")
    # Windows ACLs are not represented by POSIX mode bits reliably.  Native
    # Windows builds enforce existence/readability; POSIX requires no group or
    # world access, matching the existing A credential policy.
    if os.name != "nt" and mode & 0o077:
        raise ValueError(f"CDS API 凭据文件权限必须禁止 group/world 访问: {resolved}")
    return resolved


@contextmanager
def cdsapi_rc_environment(path: str | Path | None) -> Iterator[None]:
    """Temporarily bind cdsapi to one rc file and restore all env overrides."""

    if path is None:
        yield
        return
    resolved = validate_cdsapi_rc_file(path)
    sentinel = object()
    previous = {
        name: os.environ.get(name, sentinel)
        for name in ("CDSAPI_RC", "CDSAPI_URL", "CDSAPI_KEY")
    }
    try:
        # cdsapi gives URL/key environment variables precedence over CDSAPI_RC;
        # remove them only for this operation so the explicit path is binding.
        os.environ["CDSAPI_RC"] = str(resolved)
        os.environ.pop("CDSAPI_URL", None)
        os.environ.pop("CDSAPI_KEY", None)
        yield
    finally:
        for name, value in previous.items():
            if value is sentinel:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)


def _raw_cache_identity(request: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(request).encode("utf-8")).hexdigest()


def _raw_cache_paths(
    data_root: Path,
    *,
    cycle: datetime,
    request_sha256: str,
) -> tuple[Path, Path]:
    root = data_root / "carra" / "_raw_grib"
    stem = f"carra_{cycle:%Y%m%dT%H%M%SZ}_{request_sha256[:16]}"
    return root / f"{stem}.grib", root / f"{stem}.request.json"


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _fsync_file(temporary)
    temporary.replace(path)
    _fsync_directory(path.parent)


def _quarantine_raw_cache(payload: Path, sidecar: Path, *, reason: str) -> None:
    """Move a suspicious raw payload/sidecar pair aside for later inspection."""

    existing = [path for path in (payload, sidecar) if path.exists()]
    if not existing:
        return
    quarantine = payload.parent.parent / "quarantine" / "carra"
    quarantine.mkdir(parents=True, exist_ok=True)
    safe_reason = _SAFE_REASON.sub("-", reason).strip("-")[:48] or "invalid"
    token = uuid.uuid4().hex
    for source in existing:
        target = quarantine / f"{source.name}.{safe_reason}.{token}"
        source.replace(target)


def _read_verified_raw_cache(
    payload: Path,
    sidecar: Path,
    *,
    cycle: datetime,
    request: dict[str, Any],
    request_sha256: str,
) -> _RawFrame | None:
    present = [payload.exists(), sidecar.exists()]
    if not any(present):
        return None
    if not all(present):
        _quarantine_raw_cache(payload, sidecar, reason="missing-pair")
        return None
    try:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("sidecar must be an object")
        if metadata.get("schema_version") != _RAW_CACHE_SCHEMA:
            raise ValueError("sidecar schema mismatch")
        if metadata.get("payload_file") != payload.name:
            raise ValueError("payload filename mismatch")
        if metadata.get("cycle") != cycle.isoformat().replace("+00:00", "Z"):
            raise ValueError("cycle mismatch")
        if metadata.get("dataset_id") != CARRA_DATASET_ID or metadata.get("domain") != CARRA_DOMAIN:
            raise ValueError("source identity mismatch")
        if metadata.get("request") != request or metadata.get("request_sha256") != request_sha256:
            raise ValueError("request identity mismatch")
        size = metadata.get("payload_size_bytes")
        checksum = metadata.get("payload_sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("payload size is invalid")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("payload checksum is invalid")
        if payload.stat().st_size != size or _sha256_file(payload) != checksum:
            raise ValueError("payload checksum or size mismatch")
    except (OSError, ValueError, json.JSONDecodeError):
        _quarantine_raw_cache(payload, sidecar, reason="checksum-or-sidecar")
        return None
    return _RawFrame(payload, sidecar, request, request_sha256, True)


def _download_raw_frame(
    *,
    client: Any,
    cycle: datetime,
    data_types: Iterable[str],
    request: dict[str, Any],
    request_sha256: str,
    payload: Path,
    sidecar: Path,
) -> _RawFrame:
    payload.parent.mkdir(parents=True, exist_ok=True)
    temporary = payload.with_name(f".{payload.name}.{uuid.uuid4().hex}.part")
    try:
        _retrieve_carra_frame(
            client=client,
            cycle=cycle,
            data_types=data_types,
            out_path=temporary,
        )
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError("CDS CARRA 下载结果为空")
        with temporary.open("rb") as handle:
            if handle.read(4) != b"GRIB":
                raise RuntimeError("CDS CARRA 下载结果不是 GRIB 文件")
        payload_size = temporary.stat().st_size
        payload_sha256 = _sha256_file(temporary)
        _fsync_file(temporary)
        temporary.replace(payload)
        _fsync_directory(payload.parent)
        _atomic_json(
            sidecar,
            {
                "schema_version": _RAW_CACHE_SCHEMA,
                "dataset_id": CARRA_DATASET_ID,
                "domain": CARRA_DOMAIN,
                "cycle": cycle.isoformat().replace("+00:00", "Z"),
                "request": request,
                "request_sha256": request_sha256,
                "payload_file": payload.name,
                "payload_size_bytes": payload_size,
                "payload_sha256": payload_sha256,
                "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return _RawFrame(payload, sidecar, request, request_sha256, False)


def _ensure_raw_frame(
    *,
    data_root: Path,
    client: Any,
    cycle: datetime,
    data_types: tuple[str, ...],
) -> _RawFrame:
    request = _carra_request(cycle=cycle, data_types=data_types)
    request_sha256 = _raw_cache_identity(request)
    payload, sidecar = _raw_cache_paths(
        data_root,
        cycle=cycle,
        request_sha256=request_sha256,
    )
    cached = _read_verified_raw_cache(
        payload,
        sidecar,
        cycle=cycle,
        request=request,
        request_sha256=request_sha256,
    )
    if cached is not None:
        return cached
    return _download_raw_frame(
        client=client,
        cycle=cycle,
        data_types=data_types,
        request=request,
        request_sha256=request_sha256,
        payload=payload,
        sidecar=sidecar,
    )


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


def _is_local_parser_dependency_error(exc: BaseException) -> bool:
    """Keep a verified download when only the local GRIB runtime is unavailable."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (ImportError, ModuleNotFoundError)):
            return True
        message = str(current).casefold()
        if "cannot find the eccodes library" in message or "acquisition extra" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


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


def _require_finite_target(dataset: xr.Dataset, *, data_type: str) -> None:
    """Fail closed when any requested target cell is outside source coverage."""

    if not dataset.data_vars:
        raise ValueError(f"CARRA {data_type} 没有可发布的数据变量")
    for name, variable in dataset.data_vars.items():
        values = np.asarray(variable.values)
        if values.size == 0 or not np.isfinite(values).all():
            raise ValueError(
                f"CARRA {data_type}/{name} 未完整覆盖目标 corridor；"
                "禁止以外推或零值填充"
            )


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
        # CARRA publishes analysis fields retrospectively.  The analysis cycle
        # is the valid time, not evidence of when this system could first know
        # the field.  Without authoritative catalogue publication metadata the
        # successful retrieval time is the safe availability upper bound.
        issue_time=observed_at,
        method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
        authority="C3S/CDS CARRA retrieval time",
        reference=f"cdsapi retrieve {CARRA_DATASET_ID} {cycle:%Y%m%dT%HZ}",
        observed_at=observed_at,
        raw_value=observed_at.isoformat().replace("+00:00", "Z"),
        authoritative=False,
    )


def _ensure_source_snapshot(
    *,
    data_root: Path,
    raw: _RawFrame,
    snapshot_id: str,
) -> Path:
    """Bind a verified cache payload into A's immutable source evidence tree."""

    snapshot_dir = data_root / "source_snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    destination = snapshot_dir / raw.path.name
    expected_sha256 = _sha256_file(raw.path)
    if destination.exists():
        if not destination.is_file() or _sha256_file(destination) != expected_sha256:
            raise RuntimeError(
                f"CARRA source snapshot 已存在但内容不一致: {snapshot_id}/{raw.path.name}"
            )
        return destination

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        shutil.copyfile(raw.path, temporary)
        _fsync_file(temporary)
        if _sha256_file(temporary) != expected_sha256:
            raise RuntimeError("CARRA source snapshot 原子复制后校验失败")
        temporary.replace(destination)
        _fsync_directory(snapshot_dir)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


@dataclass
class CarraAcquisition:
    """CARRA acquisition driver (proposal ``A-WINTER-MET-001``).

    ``publish`` defaults to ``False``: the driver downloads, parses, regrids and
    validates but does NOT publish into A's incoming pipeline. Set ``publish=True``
    only when the caller intends to add the resulting frames to A's immutable
    archive. The public CLI uses ``acquire_between`` and always supplies an
    explicit external CDS rc file.
    """

    route_id: str
    bounds: Any
    data_types: tuple[str, ...] = CARRA_SUPPORTED_DATA_TYPES
    publish: bool = False
    publisher: Any | None = field(default=None, repr=False)
    data_root: Path = field(default=Path("/tmp/carra_dry_run"), repr=False)
    cdsapi_rc_file: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root).expanduser().resolve()
        self.data_types = tuple(dict.fromkeys(self.data_types))
        if not self.data_types:
            raise ValueError("CARRA data_types 不能为空")
        unsupported = sorted(set(self.data_types) - set(CARRA_SUPPORTED_DATA_TYPES))
        if unsupported:
            raise ValueError(f"CARRA 不支持的数据类型: {', '.join(unsupported)}")
        validate_carra_bounds(self.bounds)
        if self.cdsapi_rc_file is not None:
            self.cdsapi_rc_file = validate_cdsapi_rc_file(self.cdsapi_rc_file)

    # -- single-frame smoke ---------------------------------------------------
    def acquire_frame_smoke(self, cycle: datetime) -> dict[str, xr.Dataset]:
        """Download + parse + regrid ONE cycle. No publish. Smoke/test only."""
        cycle = ensure_utc(cycle, field="cycle")
        if not _is_three_hour_boundary(cycle):
            raise ValueError("CARRA cycle 必须落在 UTC 的 3 小时边界（00/03/06/...）")
        if cycle > datetime.now(UTC):
            raise ValueError("CARRA 是再分析数据，不允许请求未来时次")
        self.data_root.mkdir(parents=True, exist_ok=True)
        with cdsapi_rc_environment(self.cdsapi_rc_file):
            client = _cds_client()
            raw = _ensure_raw_frame(
                data_root=self.data_root,
                client=client,
                cycle=cycle,
                data_types=self.data_types,
            )
        try:
            per_type = _open_carra_frame(path=raw.path, data_types=self.data_types)
        except Exception as exc:
            if not _is_local_parser_dependency_error(exc):
                _quarantine_raw_cache(raw.path, raw.sidecar, reason="parse-failed")
            raise
        target_lon, target_lat = _target_grid(self.bounds)
        regridded: dict[str, xr.Dataset] = {}
        for dt, ds in per_type.items():
            result = _regrid_to_bounds(
                ds, target_lon=target_lon, target_lat=target_lat
            )
            _require_finite_target(result, data_type=dt)
            regridded[dt] = result
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
        start = ensure_utc(start_time, field="start_time")
        if not isinstance(horizon_hours, int) or isinstance(horizon_hours, bool):
            raise ValueError("CARRA horizon_hours 必须是整数")
        if horizon_hours <= 0:
            raise ValueError("CARRA horizon_hours 必须大于 0")
        end = start + timedelta(hours=horizon_hours)
        return self.acquire_between(start_time=start, end_time=end)

    def acquire_between(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
    ) -> CarraAcquisitionResult:
        """Acquire and optionally publish an explicit inclusive CARRA window."""

        window = resolve_carra_window(start_time=start_time, end_time=end_time)
        target_lon, target_lat = _target_grid(self.bounds)
        observed_at = datetime.now(UTC)
        snapshots: list[str] = []
        frames_processed = 0
        frames_published = 0
        cache_hits = 0
        downloaded_cycles = 0

        self.data_root.mkdir(parents=True, exist_ok=True)
        raw_frames: list[tuple[datetime, _RawFrame]] = []
        # The explicit rc path only exists in this context.  In particular, a
        # later Copernicus or CDS job in the same worker cannot accidentally
        # inherit this operation's credential selection.
        with cdsapi_rc_environment(self.cdsapi_rc_file):
            client = _cds_client()
            for cycle in window.cycles:
                raw = _ensure_raw_frame(
                    data_root=self.data_root,
                    client=client,
                    cycle=cycle,
                    data_types=self.data_types,
                )
                raw_frames.append((cycle, raw))
                if raw.cache_hit:
                    cache_hits += 1
                else:
                    downloaded_cycles += 1

        for cycle, raw in raw_frames:
            try:
                per_type = _open_carra_frame(path=raw.path, data_types=self.data_types)
            except Exception as exc:
                if not _is_local_parser_dependency_error(exc):
                    _quarantine_raw_cache(raw.path, raw.sidecar, reason="parse-failed")
                raise
            snapshot_id = (
                f"carra-{CARRA_DOMAIN}-{cycle:%Y%m%dT%H%M%SZ}-req-"
                f"{raw.request_sha256[:12]}"
            )
            snapshots.append(snapshot_id)
            evidence = _issue_evidence_for(cycle, observed_at)
            source_snapshot = _ensure_source_snapshot(
                data_root=self.data_root,
                raw=raw,
                snapshot_id=snapshot_id,
            )
            for dt, src in per_type.items():
                dataset = _regrid_to_bounds(
                    src, target_lon=target_lon, target_lat=target_lat
                )
                _require_finite_target(dataset, data_type=dt)
                # CARRA single-level analysis has no time axis; the analysis
                # cycle IS the valid time. The publisher requires a time/valid_time
                # coordinate for temporal slicing, so pin it to the cycle.
                dataset = dataset.assign_coords(
                    time=np.datetime64(cycle.replace(tzinfo=None))
                )
                dataset.attrs.update(
                    {
                        "product_id": "CARRA_SINGLE_LEVELS",
                        "analysis_time": cycle.isoformat(),
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
                            "analysis_time": cycle.isoformat().replace("+00:00", "Z"),
                            "nominal_interval_hours": CARRA_NOMINAL_INTERVAL_HOURS,
                            "domain": CARRA_DOMAIN,
                            "source_file": source_snapshot.name,
                            "source_file_checksum": _sha256_file(source_snapshot),
                            "source_file_size_bytes": source_snapshot.stat().st_size,
                            "source_snapshot_relative_path": source_snapshot.relative_to(
                                self.data_root
                            ).as_posix(),
                            "request_sha256": raw.request_sha256,
                            "request": raw.request,
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
            cycles_requested=len(window.cycles),
            cache_hits=cache_hits,
            downloaded_cycles=downloaded_cycles,
        )


def acquire_carra_dry_run(
    *,
    route_id: str,
    bounds: Any,
    cycle: datetime,
    data_types: Iterable[str] = CARRA_SUPPORTED_DATA_TYPES,
    data_root: Path = Path("/tmp/carra_dry_run"),
    cdsapi_rc_file: Path | None = None,
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
        cdsapi_rc_file=cdsapi_rc_file,
    )
    return driver.acquire_frame_smoke(cycle)
