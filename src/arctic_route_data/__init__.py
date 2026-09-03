"""Work package A public API."""

from arctic_route_data.bundle import (
    DatasetBundle,
    DatasetBundleCoverage,
    DatasetBundleRecord,
)
from arctic_route_data.cache import PartitionedABCache
from arctic_route_data.clock import SimulationClock
from arctic_route_data.config import WorkPackageAConfig, load_config
from arctic_route_data.forecast_acquisition import Bounds, NativeForecastAcquirer
from arctic_route_data.issue_time import IssueTimeEvidence, SourceIssueTimeResolver
from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import (
    DataCategory,
    ManifestRecord,
    QualityFlag,
    StandardDataFrame,
    semantic_payload_digest,
)
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.service import CoverageReport, PreparedWindow, WorkPackageA
from arctic_route_data.sources import CompositeDataSource, LocalArchiveSource

_OPTIONAL_EXPORTS = {
    "LegacyDownloaderRunner": (
        "arctic_route_data.legacy_downloaders",
        "LegacyDownloaderRunner",
    ),
    "VesselTrafficSimulationSource": (
        "arctic_route_data.vessel_traffic",
        "VesselTrafficSimulationSource",
    ),
}


def __getattr__(name: str):
    """Load migration and diagnostic APIs only when explicitly requested."""

    try:
        module_name, attribute = _OPTIONAL_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    from importlib import import_module

    return getattr(import_module(module_name), attribute)

__all__ = [
    "AcquisitionPublisher",
    "Bounds",
    "CompositeDataSource",
    "CoverageReport",
    "DataCategory",
    "DatasetBundle",
    "DatasetBundleCoverage",
    "DatasetBundleRecord",
    "IssueTimeEvidence",
    "LegacyDownloaderRunner",
    "LocalArchiveSource",
    "ManifestRecord",
    "ManifestStore",
    "NativeForecastAcquirer",
    "PartitionedABCache",
    "PreparedWindow",
    "QualityFlag",
    "SimulationClock",
    "SourceIssueTimeResolver",
    "StandardDataFrame",
    "VesselTrafficSimulationSource",
    "WorkPackageA",
    "WorkPackageAConfig",
    "load_config",
    "semantic_payload_digest",
]

__version__ = "0.4.2"
