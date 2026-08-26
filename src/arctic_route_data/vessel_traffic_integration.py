"""Import vessel traffic simulator outputs into Work Package A storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import xarray as xr

from arctic_route_data.ingestion import IngestionPipeline
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.models import ManifestRecord, QualityFlag
from arctic_route_data.timeutils import ensure_utc, parse_utc


DEFAULT_MODEL_OUTPUT_DIR = (
    Path(__file__).resolve().parents[4]
    / "my_model"
    / "IceNavRisk_Final_Delivery"
    / "04_vessel_traffic_model"
    / "downloads"
    / "model_input"
)

SOURCE_TO_A_ROUTE_ID = {
    "offshore_murmansk_to_offshore_dikson": "offshore_murmansk_to_offshore_dikson",
    "tromso_to_svalbard": "tromso_to_isfjorden_outer",
}


@dataclass(frozen=True, slots=True)
class VesselTrafficImportResult:
    """Summary returned after vessel traffic files are imported."""

    imported: tuple[ManifestRecord, ...]
    skipped: tuple[str, ...]


def import_vessel_traffic_model_outputs(
    *,
    source_dir: str | Path | None = None,
    data_root: str | Path = "data",
    quality_flag: QualityFlag = QualityFlag.SUSPECT,
    version: str = "traffic-simulator-v1",
) -> VesselTrafficImportResult:
    """Import generated vessel-traffic NetCDF files through A's manifest pipeline.

    The generated vessel-traffic layer is an AIS/statistics-calibrated simulator
    output, not an authoritative maritime traffic observation. It is therefore
    registered as ``suspect`` by default and should be consumed by B as an
    optional dynamic traffic-risk factor.
    """

    root = Path(source_dir).resolve() if source_dir is not None else DEFAULT_MODEL_OUTPUT_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"vessel traffic source directory not found: {root}")

    pipeline = IngestionPipeline(data_root)
    imported: list[ManifestRecord] = []
    skipped: list[str] = []
    for path in sorted(root.glob("vessel_traffic_risk_*.nc")):
        try:
            source_route_id = _source_route_id_from_path(path)
            route_id = SOURCE_TO_A_ROUTE_ID[source_route_id]
            issue_time, valid_time = _read_times(path)
            evidence = IssueTimeEvidence(
                issue_time=issue_time,
                method=IssueTimeMethod.CONSERVATIVE_RETRIEVAL,
                authority="AIS/statistics-calibrated vessel traffic simulator",
                reference=f"generated NetCDF file: {path.name}",
                observed_at=max(datetime.now(UTC), issue_time),
                raw_value=issue_time.isoformat(),
                authoritative=False,
            )
            record = pipeline.ingest_netcdf(
                path,
                data_type="vessel_traffic",
                route_id=route_id,
                issue_time=issue_time,
                valid_time=valid_time,
                source="AIS/statistics-calibrated vessel traffic simulator",
                issue_time_evidence=evidence,
                version=version,
                quality_flag=quality_flag,
                extra_metadata={
                    "source_route_id": source_route_id,
                    "a_route_id": route_id,
                    "traffic_model_role": "optional_dynamic_factor_for_B",
                    "necessity": (
                        "Historical vessel-passage records for Arctic corridors are hard to "
                        "obtain continuously and openly. This layer provides realtime-like "
                        "traffic-state features for downstream comprehensive risk modelling."
                    ),
                },
            )
            imported.append(record)
        except Exception as exc:  # pragma: no cover - surfaced through CLI output
            skipped.append(f"{path}: {exc}")

    return VesselTrafficImportResult(imported=tuple(imported), skipped=tuple(skipped))


def _source_route_id_from_path(path: Path) -> str:
    stem = path.stem
    prefix = "vessel_traffic_risk_"
    if not stem.startswith(prefix):
        raise ValueError(f"unsupported vessel traffic file name: {path.name}")
    source_route_id = stem[len(prefix) :]
    if source_route_id not in SOURCE_TO_A_ROUTE_ID:
        supported = ", ".join(sorted(SOURCE_TO_A_ROUTE_ID))
        raise ValueError(f"unsupported source route {source_route_id!r}; supported: {supported}")
    return source_route_id


def _read_times(path: Path) -> tuple[datetime, datetime]:
    attrs: dict[str, object] | None = None
    errors: list[str] = []
    for engine in ("scipy", "h5netcdf", None):
        try:
            with xr.open_dataset(path, engine=engine) as dataset:
                attrs = dict(dataset.attrs)
            break
        except Exception as exc:  # pragma: no cover - depends on optional local NetCDF backends
            errors.append(f"{engine or 'auto'}: {exc}")

    if attrs is None:
        raise RuntimeError(f"Cannot read vessel traffic NetCDF metadata: {path}. Tried {' | '.join(errors)}")

    issue_time = _parse_time_attr(attrs, ("issue_time", "created_at_utc", "realtime_collected_at_utc"))
    valid_time = _parse_time_attr(attrs, ("valid_time", "realtime_collected_at_utc", "created_at_utc"))
    if issue_time is None or valid_time is None:
        fallback = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        issue_time = issue_time or fallback
        valid_time = valid_time or issue_time
    return ensure_utc(issue_time, field="issue_time"), ensure_utc(valid_time, field="valid_time")


def _parse_time_attr(attrs: dict[str, object], names: tuple[str, ...]) -> datetime | None:
    for name in names:
        value = attrs.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            return parse_utc(text, field=name)
        except Exception:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def import_result_to_json(result: VesselTrafficImportResult) -> str:
    """Serialize import results for CLI use."""

    return json.dumps(
        {
            "imported": [record.to_dict() for record in result.imported],
            "skipped": list(result.skipped),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


__all__ = [
    "DEFAULT_MODEL_OUTPUT_DIR",
    "SOURCE_TO_A_ROUTE_ID",
    "VesselTrafficImportResult",
    "import_result_to_json",
    "import_vessel_traffic_model_outputs",
]
