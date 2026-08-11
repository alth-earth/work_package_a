"""Manual bridge for legacy modules with caller-supplied authoritative metadata."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import xarray as xr

from arctic_route_data.errors import MissingMetadataError
from arctic_route_data.issue_time import IssueTimeEvidence, IssueTimeMethod
from arctic_route_data.models import DataCategory, ManifestRecord
from arctic_route_data.publisher import AcquisitionPublisher
from arctic_route_data.specs import get_data_type_spec
from arctic_route_data.temporal_split import discover_valid_times
from arctic_route_data.timeutils import ensure_utc

MetadataResolver = Callable[[str, str, Any, int], Mapping[str, Any]]


class LegacyDownloaderAdapter:
    """Run one legacy function and publish every returned time slice with a sidecar.

    Prefer :class:`LegacyDownloaderRunner` for the 13 registered downloaders. This
    adapter remains useful when an authoritative project-specific catalogue resolver
    is already available.
    """

    def __init__(
        self,
        *,
        module_path: str | Path,
        function_name: str,
        data_type: str,
        data_root: str | Path,
        metadata_resolver: MetadataResolver | None,
    ) -> None:
        self.module_path = Path(module_path)
        self.function_name = function_name
        self.data_type = data_type
        self.publisher = AcquisitionPublisher(data_root)
        self.metadata_resolver = metadata_resolver

    def run(self) -> list[ManifestRecord]:
        if self.metadata_resolver is None:
            raise MissingMetadataError(
                "旧下载函数没有统一 issue_time；必须提供 metadata_resolver，禁止按文件名猜测。"
            )
        module = self._load_module()
        result = getattr(module, self.function_name)()
        batches: Sequence[Any] = result if isinstance(result, tuple) else (result,)
        records: list[ManifestRecord] = []
        for batch_index, batch in enumerate(batches):
            if not isinstance(batch, dict):
                raise TypeError("旧下载接口应返回 route_id -> 数据对象字典")
            for route_id, payload in batch.items():
                metadata = dict(
                    self.metadata_resolver(self.data_type, route_id, payload, batch_index)
                )
                required = {"issue_time", "source"}
                missing = sorted(required - metadata.keys())
                if missing:
                    raise MissingMetadataError(
                        f"metadata_resolver 缺少字段: {', '.join(missing)}"
                    )
                issue_time = ensure_utc(metadata.pop("issue_time"), field="issue_time")
                source = str(metadata.pop("source"))
                evidence = IssueTimeEvidence(
                    issue_time=issue_time,
                    method=IssueTimeMethod.EXPLICIT_CATALOG,
                    authority=str(metadata.pop("issue_authority", source)),
                    reference=str(metadata.pop("issue_reference", "caller metadata_resolver")),
                    observed_at=ensure_utc(
                        metadata.pop("observed_at", datetime.now(UTC)), field="observed_at"
                    ),
                    raw_value=str(metadata.pop("issue_raw_value", issue_time.isoformat())),
                )
                version = str(metadata.pop("version", "legacy"))
                explicit_valid_time = metadata.pop("valid_time", None)
                if isinstance(payload, xr.Dataset):
                    has_time_axis = any(
                        name in payload.variables
                        for name in ("time", "valid_time", "forecast_time", "step")
                    )
                    has_times = bool(discover_valid_times(payload)) if has_time_axis else False
                    category = get_data_type_spec(self.data_type).category
                    if (
                        not has_times
                        and explicit_valid_time is None
                        and category is not DataCategory.STATIC
                    ):
                        raise MissingMetadataError(
                            f"{self.data_type} 是 {category.value} 数据，payload 必须带合法有效时刻"
                        )
                    published = self.publisher.publish_dataset(
                        payload,
                        data_type=self.data_type,
                        route_id=route_id,
                        source=source,
                        version=version,
                        issue_evidence=evidence,
                        valid_time=(
                            ensure_utc(explicit_valid_time, field="valid_time")
                            if explicit_valid_time is not None and not has_times
                            else issue_time if not has_times else None
                        ),
                        metadata=metadata,
                    )
                elif isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                    published = self.publisher.publish_geojson(
                        payload,
                        route_id=route_id,
                        source=source,
                        version=version,
                        issue_evidence=evidence,
                        valid_time=(
                            ensure_utc(explicit_valid_time, field="valid_time")
                            if explicit_valid_time is not None
                            else issue_time
                        ),
                        metadata=metadata,
                    )
                else:
                    raise TypeError(f"{route_id} 的返回值类型不受支持: {type(payload)!r}")
                records.extend(published.records)
        return records

    def _load_module(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(
            f"arctic_route_legacy_{self.module_path.stem}", self.module_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载旧模块: {self.module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
