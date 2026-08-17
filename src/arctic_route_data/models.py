"""Stable data contracts between normalized archives, A and the AB cache."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_data.errors import MetadataValidationError
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc


class DataCategory(StrEnum):
    STATIC = "static"
    SLOW = "slow"
    DYNAMIC = "dynamic"
    EVENT = "event"


class QualityFlag(StrEnum):
    GOOD = "good"
    SUSPECT = "suspect"
    DEGRADED = "degraded"
    MISSING = "missing"


QUALITY_RANK: dict[QualityFlag, int] = {
    QualityFlag.GOOD: 3,
    QualityFlag.SUSPECT: 2,
    QualityFlag.DEGRADED: 1,
    QualityFlag.MISSING: 0,
}

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\Z")


def validate_identifier(value: str, *, field: str) -> str:
    """Validate identifiers before they are used in archive paths or logical keys."""

    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise MetadataValidationError(
            f"{field} 只能包含字母、数字、点、下划线、加号和连字符，且最长 128 字符"
        )
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        frozen = value.copy()
        frozen.flags.writeable = False
        return frozen
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | frozenset):
        return [_deep_thaw(item) for item in value]
    return value


def _freeze_dataset(dataset: xr.Dataset) -> xr.Dataset:
    for variable in dataset.variables.values():
        values = variable.data
        if isinstance(values, np.ndarray):
            values.flags.writeable = False
    return dataset


def _digest_part(digest: Any, tag: str, value: bytes = b"") -> None:
    tag_bytes = tag.encode("ascii")
    digest.update(len(tag_bytes).to_bytes(4, "big"))
    digest.update(tag_bytes)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _update_semantic_value(digest: Any, value: Any) -> None:
    """Hash one value with explicit type and length boundaries."""

    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        _digest_part(digest, "none")
    elif isinstance(value, bool):
        _digest_part(digest, "bool", b"1" if value else b"0")
    elif isinstance(value, int):
        _digest_part(digest, "int", str(value).encode("ascii"))
    elif isinstance(value, float):
        if math.isnan(value):
            encoded = b"nan"
        elif math.isinf(value):
            encoded = b"+inf" if value > 0 else b"-inf"
        else:
            encoded = value.hex().encode("ascii")
        _digest_part(digest, "float", encoded)
    elif isinstance(value, str):
        _digest_part(digest, "str", value.encode("utf-8"))
    elif isinstance(value, bytes):
        _digest_part(digest, "bytes", value)
    elif isinstance(value, datetime):
        _digest_part(
            digest,
            "datetime",
            ensure_utc(value, field="semantic payload datetime").isoformat().encode("ascii"),
        )
    elif isinstance(value, np.ndarray):
        _update_semantic_array(digest, value)
    elif isinstance(value, Mapping):
        _digest_part(digest, "mapping-start", str(len(value)).encode("ascii"))
        if any(not isinstance(key, str) for key in value):
            raise MetadataValidationError("semantic payload mapping 的键必须是字符串")
        for key in sorted(value):
            _update_semantic_value(digest, key)
            _update_semantic_value(digest, value[key])
        _digest_part(digest, "mapping-end")
    elif isinstance(value, list | tuple):
        _digest_part(digest, "sequence-start", str(len(value)).encode("ascii"))
        for item in value:
            _update_semantic_value(digest, item)
        _digest_part(digest, "sequence-end")
    elif isinstance(value, set | frozenset):
        encoded_items: list[bytes] = []
        for item in value:
            item_digest = hashlib.sha256()
            _update_semantic_value(item_digest, item)
            encoded_items.append(item_digest.digest())
        _digest_part(digest, "set-start", str(len(encoded_items)).encode("ascii"))
        for encoded in sorted(encoded_items):
            _digest_part(digest, "set-item", encoded)
        _digest_part(digest, "set-end")
    else:
        raise MetadataValidationError(
            "semantic payload 包含不支持的值类型: " + type(value).__name__
        )


def _update_semantic_array(digest: Any, values: np.ndarray) -> None:
    array = np.asarray(values)
    _digest_part(digest, "array-start")
    _update_semantic_value(digest, tuple(int(item) for item in array.shape))
    _update_semantic_value(digest, array.dtype.str)
    if array.dtype.fields is not None:
        raise MetadataValidationError("semantic payload 不支持 structured dtype")
    if array.dtype.kind in "biufcmM":
        canonical_dtype = array.dtype.newbyteorder("<")
        canonical = np.ascontiguousarray(array.astype(canonical_dtype, copy=False))
        _digest_part(digest, "array-bytes", canonical.tobytes(order="C"))
    elif array.dtype.kind in "SUO":
        _digest_part(digest, "array-items", str(array.size).encode("ascii"))
        for item in array.reshape(-1, order="C"):
            _update_semantic_value(digest, item)
    else:
        raise MetadataValidationError(
            f"semantic payload 不支持 dtype {array.dtype}"
        )
    _digest_part(digest, "array-end")


def _update_xarray_variable(digest: Any, name: str, variable: xr.Variable) -> None:
    _update_semantic_value(digest, name)
    _update_semantic_value(digest, tuple(variable.dims))
    _update_semantic_value(digest, dict(variable.attrs))
    _update_semantic_array(digest, np.asarray(variable.values))


def semantic_payload_digest(record: ManifestRecord, payload: Any) -> str:
    """Return a canonical SHA-256 attestation for one record and live payload.

    The digest is independent of Python container identity and stable across a
    deep copy. It binds the complete public manifest record to Dataset
    dimensions, coordinates, variables, dtypes, values and attributes, or to a
    recursively canonical Mapping payload. It is an AB runtime attestation and
    intentionally does not change ``DatasetBundle.v2``.
    """

    if not isinstance(record, ManifestRecord):
        raise MetadataValidationError("semantic payload digest 需要 ManifestRecord")
    digest = hashlib.sha256()
    _digest_part(digest, "a.semantic-payload-attestation.v1")
    _update_semantic_value(digest, record.to_dict())
    if isinstance(payload, xr.Dataset):
        _digest_part(digest, "xarray-dataset-start")
        _update_semantic_value(digest, dict(payload.attrs))
        _update_semantic_value(
            digest,
            {name: int(size) for name, size in sorted(payload.sizes.items())},
        )
        _digest_part(digest, "coordinates-start")
        for name in sorted(payload.coords):
            _update_xarray_variable(digest, name, payload.coords[name].variable)
        _digest_part(digest, "coordinates-end")
        _digest_part(digest, "data-variables-start")
        for name in sorted(payload.data_vars):
            _update_xarray_variable(digest, name, payload.data_vars[name].variable)
        _digest_part(digest, "data-variables-end")
        _digest_part(digest, "xarray-dataset-end")
    elif isinstance(payload, Mapping):
        _digest_part(digest, "mapping-payload-start")
        _update_semantic_value(digest, payload)
        _digest_part(digest, "mapping-payload-end")
    else:
        raise MetadataValidationError(
            "semantic payload 必须是 xarray.Dataset 或 Mapping"
        )
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    data_id: str
    data_type: str
    category: DataCategory
    route_id: str
    variables: tuple[str, ...]
    issue_time: datetime
    valid_time: datetime
    ingest_time: datetime
    bbox: tuple[float, float, float, float]
    crs: str
    resolution: tuple[float | None, float | None]
    source: str
    quality_flag: QualityFlag
    version: str
    checksum: str
    relative_path: str
    size_bytes: int
    media_type: str = "application/x-netcdf"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("data_id", "data_type", "route_id", "source", "version", "relative_path"):
            if not getattr(self, name).strip():
                raise MetadataValidationError(f"{name} 不能为空")
        validate_identifier(self.data_id, field="data_id")
        validate_identifier(self.data_type, field="data_type")
        validate_identifier(self.route_id, field="route_id")
        validate_identifier(self.version, field="version")
        if not self.variables or any(not item.strip() for item in self.variables):
            raise MetadataValidationError("variables 不能为空")
        if len(set(self.variables)) != len(self.variables):
            raise MetadataValidationError("variables 不得重复")
        if not self.crs.strip():
            raise MetadataValidationError("crs 不能为空")
        if len(self.bbox) != 4 or self.bbox[0] > self.bbox[2] or self.bbox[1] > self.bbox[3]:
            raise MetadataValidationError("bbox 必须为 (west, south, east, north)")
        if not all(math.isfinite(item) for item in self.bbox):
            raise MetadataValidationError("bbox 必须全部为有限值")
        if len(self.resolution) != 2 or any(
            item is not None and (not math.isfinite(item) or item <= 0)
            for item in self.resolution
        ):
            raise MetadataValidationError("resolution 必须为两个正有限值或 null")
        if len(self.checksum) != 64 or any(c not in "0123456789abcdef" for c in self.checksum):
            raise MetadataValidationError("checksum 必须是小写 SHA-256")
        if self.size_bytes < 0:
            raise MetadataValidationError("size_bytes 不能为负数")
        relative = Path(self.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise MetadataValidationError("relative_path 必须是无路径逃逸的相对路径")
        object.__setattr__(self, "issue_time", ensure_utc(self.issue_time, field="issue_time"))
        object.__setattr__(self, "valid_time", ensure_utc(self.valid_time, field="valid_time"))
        object.__setattr__(self, "ingest_time", ensure_utc(self.ingest_time, field="ingest_time"))
        object.__setattr__(self, "metadata", _deep_freeze(self.metadata))

    def is_available_at(self, simulation_time: datetime) -> bool:
        return self.issue_time <= ensure_utc(simulation_time, field="simulation_time")

    def absolute_path(self, archive_root: Path) -> Path:
        root = archive_root.resolve()
        path = (root / self.relative_path).resolve()
        if not path.is_relative_to(root):
            raise MetadataValidationError("manifest 路径逃逸出 archive_root")
        return path

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_id": self.data_id,
            "data_type": self.data_type,
            "category": self.category.value,
            "route_id": self.route_id,
            "variables": list(self.variables),
            "issue_time": isoformat_utc(self.issue_time),
            "valid_time": isoformat_utc(self.valid_time),
            "ingest_time": isoformat_utc(self.ingest_time),
            "bbox": list(self.bbox),
            "crs": self.crs,
            "resolution": list(self.resolution),
            "source": self.source,
            "quality_flag": self.quality_flag.value,
            "version": self.version,
            "checksum": self.checksum,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "metadata": _deep_thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManifestRecord:
        required = {
            "data_id",
            "data_type",
            "category",
            "route_id",
            "variables",
            "issue_time",
            "valid_time",
            "ingest_time",
            "bbox",
            "crs",
            "resolution",
            "source",
            "quality_flag",
            "version",
            "checksum",
            "relative_path",
            "size_bytes",
        }
        missing = sorted(required - value.keys())
        if missing:
            raise MetadataValidationError(f"manifest 缺少字段: {', '.join(missing)}")
        return cls(
            data_id=str(value["data_id"]),
            data_type=str(value["data_type"]),
            category=DataCategory(value["category"]),
            route_id=str(value["route_id"]),
            variables=tuple(str(item) for item in value["variables"]),
            issue_time=parse_utc(value["issue_time"], field="issue_time"),
            valid_time=parse_utc(value["valid_time"], field="valid_time"),
            ingest_time=parse_utc(value["ingest_time"], field="ingest_time"),
            bbox=tuple(float(item) for item in value["bbox"]),  # type: ignore[arg-type]
            crs=str(value["crs"]),
            resolution=tuple(
                None if item is None else float(item) for item in value["resolution"]
            ),  # type: ignore[arg-type]
            source=str(value["source"]),
            quality_flag=QualityFlag(value["quality_flag"]),
            version=str(value["version"]),
            checksum=str(value["checksum"]),
            relative_path=str(value["relative_path"]),
            size_bytes=int(value["size_bytes"]),
            media_type=str(value.get("media_type", "application/x-netcdf")),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class StandardDataFrame:
    """A published AB frame. Payloads are treated as immutable after construction."""

    record: ManifestRecord
    payload: Any
    generation_id: int

    def __post_init__(self) -> None:
        if self.generation_id < 0:
            raise MetadataValidationError("generation_id 不能为负数")
        if isinstance(self.payload, xr.Dataset):
            object.__setattr__(self, "payload", _freeze_dataset(self.payload))
        elif isinstance(self.payload, Mapping):
            object.__setattr__(self, "payload", _deep_freeze(self.payload))

    @property
    def estimated_bytes(self) -> int:
        payload_bytes = getattr(self.payload, "nbytes", None)
        if payload_bytes is None:
            return self.record.size_bytes
        return max(int(payload_bytes), 1)

    def with_generation(self, generation_id: int) -> StandardDataFrame:
        return replace(self, generation_id=generation_id)

    def consumer_copy(self) -> StandardDataFrame:
        """Return a deep, read-only payload snapshot for an external consumer."""

        if isinstance(self.payload, xr.Dataset):
            copied = self.payload.copy(deep=True)
            copied.attrs = copy.deepcopy(dict(self.payload.attrs))
            for name in copied.variables:
                copied[name].attrs = copy.deepcopy(dict(self.payload[name].attrs))
            return replace(self, payload=_freeze_dataset(copied))
        if isinstance(self.payload, Mapping):
            return replace(self, payload=_deep_freeze(self.payload))
        return replace(self)

    def consumer_view(self) -> StandardDataFrame:
        """Return a fresh shell sharing the same read-only payload buffers.

        Payload arrays are already immutable by construction (``__post_init__``
        freezes every NumPy buffer), so this avoids the defensive deep copy
        while still returning a distinct xarray Dataset / Mapping object:
        structural mutation of one handle cannot affect the other, and in-place
        writes raise ``ValueError`` on either handle.
        """

        if isinstance(self.payload, xr.Dataset):
            copied = self.payload.copy(deep=False)
            copied.attrs = copy.deepcopy(dict(self.payload.attrs))
            for name in copied.variables:
                copied[name].attrs = copy.deepcopy(dict(self.payload[name].attrs))
            return replace(self, payload=_freeze_dataset(copied))
        if isinstance(self.payload, Mapping):
            return replace(self, payload=MappingProxyType(dict(self.payload)))
        return replace(self)
