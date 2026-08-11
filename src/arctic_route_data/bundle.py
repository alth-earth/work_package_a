"""Immutable identity for the exact A records selected for one B window."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from arctic_route_data.errors import MetadataValidationError
from arctic_route_data.models import ManifestRecord, QualityFlag, validate_identifier
from arctic_route_data.timeutils import ensure_utc, isoformat_utc, parse_utc


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_safe_identifier(value: object, *, field: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_identifier(value, field=field)
    except MetadataValidationError:
        return False
    return True


def record_provenance_id(record: ManifestRecord) -> str | None:
    """Return a syntactically bound provenance identity, never a truthy guess.

    Native acquisition binds a source snapshot to its source-file checksum.
    Publisher-backed legacy/explicit ingestion binds an immutable raw publication
    to its payload checksum and size.  The latter receives a deterministic
    compatibility identity when an older record has no explicit snapshot ID.
    """

    metadata = record.metadata
    snapshot = metadata.get("source_snapshot_id")
    if snapshot is not None and not _is_safe_identifier(
        snapshot, field="source_snapshot_id"
    ):
        return None
    native_binding = (
        _is_sha256(metadata.get("source_file_checksum"))
        and isinstance(metadata.get("source_file"), str)
        and bool(str(metadata["source_file"]).strip())
        and not Path(str(metadata["source_file"])).is_absolute()
        and ".." not in Path(str(metadata["source_file"])).parts
    )
    publication = metadata.get("publication_id")
    upstream_size = metadata.get("upstream_size_bytes")
    raw_binding = (
        _is_safe_identifier(publication, field="publication_id")
        and _is_sha256(metadata.get("upstream_checksum"))
        and isinstance(upstream_size, int)
        and not isinstance(upstream_size, bool)
        and upstream_size >= 0
    )
    if snapshot is not None and (native_binding or raw_binding):
        return snapshot
    if snapshot is None and raw_binding:
        return f"raw-{str(metadata['upstream_checksum'])[:24]}"
    return None


@dataclass(frozen=True, slots=True)
class DatasetBundleRecord:
    data_id: str
    data_type: str
    issue_time: datetime
    valid_time: datetime
    source: str
    version: str
    quality_flag: str
    checksum: str
    source_snapshot_id: str | None

    @classmethod
    def from_manifest(cls, record: ManifestRecord) -> DatasetBundleRecord:
        return cls(
            data_id=record.data_id,
            data_type=record.data_type,
            issue_time=record.issue_time,
            valid_time=record.valid_time,
            source=record.source,
            version=record.version,
            quality_flag=record.quality_flag.value,
            checksum=record.checksum,
            source_snapshot_id=record_provenance_id(record),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DatasetBundleRecord:
        if not isinstance(value, Mapping):
            raise MetadataValidationError("DatasetBundleRecord 必须是 object")
        required = {
            "data_id",
            "data_type",
            "issue_time",
            "valid_time",
            "source",
            "version",
            "quality_flag",
            "checksum",
            "source_snapshot_id",
        }
        if set(value) != required:
            raise MetadataValidationError(
                "DatasetBundleRecord 字段必须精确匹配 v1 合同"
            )
        for field in ("data_id", "data_type", "source", "version"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise MetadataValidationError(f"DatasetBundleRecord.{field} 不能为空")
        try:
            quality = QualityFlag(value["quality_flag"])
        except (TypeError, ValueError) as exc:
            raise MetadataValidationError(
                "DatasetBundleRecord.quality_flag 无效"
            ) from exc
        if quality is QualityFlag.MISSING:
            raise MetadataValidationError(
                "DatasetBundleRecord 不能引用 missing payload"
            )
        if not _is_sha256(value["checksum"]):
            raise MetadataValidationError("DatasetBundleRecord.checksum 必须是 SHA-256")
        snapshot = value["source_snapshot_id"]
        if snapshot is not None and not _is_safe_identifier(
            snapshot, field="source_snapshot_id"
        ):
            raise MetadataValidationError(
                "DatasetBundleRecord.source_snapshot_id 无效"
            )
        return cls(
            data_id=str(value["data_id"]),
            data_type=str(value["data_type"]),
            issue_time=parse_utc(value["issue_time"], field="issue_time"),
            valid_time=parse_utc(value["valid_time"], field="valid_time"),
            source=str(value["source"]),
            version=str(value["version"]),
            quality_flag=quality.value,
            checksum=str(value["checksum"]),
            source_snapshot_id=snapshot,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "data_id": self.data_id,
            "data_type": self.data_type,
            "issue_time": isoformat_utc(self.issue_time),
            "valid_time": isoformat_utc(self.valid_time),
            "source": self.source,
            "version": self.version,
            "quality_flag": self.quality_flag,
            "checksum": self.checksum,
            "source_snapshot_id": self.source_snapshot_id,
        }


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    """A content-addressed list of every manifest record exposed to B.

    A source snapshot identifies one producer request or model cycle.  It does
    not identify a complete multi-source planning input.  ``DatasetBundle``
    closes that gap by hashing the query cutoff/window and the exact selected
    record identities and checksums.
    """

    schema_version: str
    bundle_id: str
    bundle_digest: str
    corridor_id: str
    as_of_time: datetime
    requested_start: datetime
    requested_end: datetime
    minimum_required_end: datetime
    requested_data_types: tuple[str, ...]
    source_snapshot_ids: tuple[str, ...]
    records: tuple[DatasetBundleRecord, ...]

    @classmethod
    def create(
        cls,
        *,
        corridor_id: str,
        as_of_time: datetime,
        requested_start: datetime,
        requested_end: datetime,
        minimum_required_end: datetime,
        requested_data_types: tuple[str, ...],
        records: tuple[ManifestRecord, ...],
        verified_provenance_ids: Mapping[str, str] | None = None,
    ) -> DatasetBundle:
        as_of = ensure_utc(as_of_time, field="as_of_time")
        start = ensure_utc(requested_start, field="requested_start")
        end = ensure_utc(requested_end, field="requested_end")
        minimum_end = ensure_utc(
            minimum_required_end, field="minimum_required_end"
        )
        validate_identifier(corridor_id, field="corridor_id")
        if not requested_data_types or any(
            not isinstance(data_type, str) for data_type in requested_data_types
        ):
            raise MetadataValidationError("DatasetBundle requested_data_types 不能为空")
        requested_types = tuple(sorted(set(requested_data_types)))
        for data_type in requested_types:
            validate_identifier(data_type, field="data_type")
        if not start <= minimum_end <= end:
            raise MetadataValidationError(
                "DatasetBundle 时间必须满足 requested_start <= minimum_required_end "
                "<= requested_end"
            )
        if any(record.route_id != corridor_id for record in records):
            raise MetadataValidationError("DatasetBundle 不能混入其他 corridor 的记录")
        if any(record.issue_time > as_of for record in records):
            raise MetadataValidationError("DatasetBundle 不能包含 as_of_time 之后发布的记录")
        if any(record.quality_flag is QualityFlag.MISSING for record in records):
            raise MetadataValidationError("DatasetBundle 不能包含没有 payload 的 missing 记录")
        if len({record.data_id for record in records}) != len(records):
            raise MetadataValidationError("DatasetBundle records 不得重复 data_id")
        unexpected_types = sorted(
            {record.data_type for record in records} - set(requested_types)
        )
        if unexpected_types:
            raise MetadataValidationError(
                "DatasetBundle records 含未请求类型: " + ", ".join(unexpected_types)
            )

        verified = dict(verified_provenance_ids or {})
        unknown_verified = sorted(set(verified) - {record.data_id for record in records})
        if unknown_verified:
            raise MetadataValidationError(
                "DatasetBundle verified provenance 含未知 data_id: "
                + ", ".join(unknown_verified)
            )
        for record in records:
            provenance_id = verified.get(record.data_id)
            if provenance_id is not None and provenance_id != record_provenance_id(record):
                raise MetadataValidationError(
                    f"DatasetBundle verified provenance 与 {record.data_id} 声明不一致"
                )

        entries = tuple(
            sorted(
                (
                    DatasetBundleRecord(
                        data_id=record.data_id,
                        data_type=record.data_type,
                        issue_time=record.issue_time,
                        valid_time=record.valid_time,
                        source=record.source,
                        version=record.version,
                        quality_flag=record.quality_flag.value,
                        checksum=record.checksum,
                        source_snapshot_id=verified.get(record.data_id),
                    )
                    for record in records
                ),
                key=lambda item: (item.data_type, item.valid_time, item.data_id),
            )
        )
        snapshots = tuple(
            sorted(
                {
                    entry.source_snapshot_id
                    for entry in entries
                    if entry.source_snapshot_id is not None
                }
            )
        )
        digest = _bundle_digest(
            corridor_id=corridor_id,
            as_of_time=as_of,
            requested_start=start,
            requested_end=end,
            minimum_required_end=minimum_end,
            requested_data_types=requested_types,
            source_snapshot_ids=snapshots,
            records=entries,
        )
        return cls(
            schema_version="a.dataset-bundle.v1",
            bundle_id=f"a-bundle-{digest[:24]}",
            bundle_digest=digest,
            corridor_id=corridor_id,
            as_of_time=as_of,
            requested_start=start,
            requested_end=end,
            minimum_required_end=minimum_end,
            requested_data_types=requested_types,
            source_snapshot_ids=snapshots,
            records=entries,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DatasetBundle:
        """Parse a serialized v1 bundle and verify its internal content digest."""

        if not isinstance(value, Mapping):
            raise MetadataValidationError("DatasetBundle 必须是 object")
        required = {
            "schema_version",
            "bundle_id",
            "bundle_digest",
            "corridor_id",
            "as_of_time",
            "requested_start",
            "requested_end",
            "minimum_required_end",
            "requested_data_types",
            "source_snapshot_ids",
            "record_count",
            "records",
        }
        if set(value) != required or value.get("schema_version") != "a.dataset-bundle.v1":
            raise MetadataValidationError("DatasetBundle 顶层字段或 schema_version 无效")
        corridor_id = value["corridor_id"]
        if not _is_safe_identifier(corridor_id, field="corridor_id"):
            raise MetadataValidationError("DatasetBundle.corridor_id 无效")
        raw_types = value["requested_data_types"]
        raw_snapshots = value["source_snapshot_ids"]
        raw_records = value["records"]
        if (
            not isinstance(raw_types, list)
            or not raw_types
            or any(not _is_safe_identifier(item, field="data_type") for item in raw_types)
            or len(set(raw_types)) != len(raw_types)
        ):
            raise MetadataValidationError("DatasetBundle.requested_data_types 无效")
        if (
            not isinstance(raw_snapshots, list)
            or any(
                not _is_safe_identifier(item, field="source_snapshot_id")
                for item in raw_snapshots
            )
            or len(set(raw_snapshots)) != len(raw_snapshots)
        ):
            raise MetadataValidationError("DatasetBundle.source_snapshot_ids 无效")
        if not isinstance(raw_records, list):
            raise MetadataValidationError("DatasetBundle.records 必须是数组")
        record_count = value["record_count"]
        if (
            not isinstance(record_count, int)
            or isinstance(record_count, bool)
            or record_count != len(raw_records)
        ):
            raise MetadataValidationError(
                "DatasetBundle.record_count 必须等于 records 长度"
            )
        records = tuple(DatasetBundleRecord.from_dict(item) for item in raw_records)
        as_of = parse_utc(value["as_of_time"], field="as_of_time")
        start = parse_utc(value["requested_start"], field="requested_start")
        end = parse_utc(value["requested_end"], field="requested_end")
        minimum_end = parse_utc(
            value["minimum_required_end"], field="minimum_required_end"
        )
        requested_types = tuple(sorted(raw_types))
        snapshots = tuple(sorted(raw_snapshots))
        if not start <= minimum_end <= end:
            raise MetadataValidationError("DatasetBundle 时间范围无效")
        if len({record.data_id for record in records}) != len(records):
            raise MetadataValidationError("DatasetBundle records 不得重复 data_id")
        if any(record.issue_time > as_of for record in records):
            raise MetadataValidationError("DatasetBundle 包含 as_of_time 后的记录")
        if {record.data_type for record in records} - set(requested_types):
            raise MetadataValidationError("DatasetBundle records 含未请求类型")
        derived_snapshots = tuple(
            sorted(
                {
                    record.source_snapshot_id
                    for record in records
                    if record.source_snapshot_id is not None
                }
            )
        )
        if snapshots != derived_snapshots:
            raise MetadataValidationError(
                "DatasetBundle.source_snapshot_ids 与 records 不一致"
            )
        canonical_records = tuple(
            sorted(records, key=lambda item: (item.data_type, item.valid_time, item.data_id))
        )
        if records != canonical_records:
            raise MetadataValidationError("DatasetBundle.records 未按 v1 规范排序")
        digest = _bundle_digest(
            corridor_id=corridor_id,
            as_of_time=as_of,
            requested_start=start,
            requested_end=end,
            minimum_required_end=minimum_end,
            requested_data_types=requested_types,
            source_snapshot_ids=snapshots,
            records=records,
        )
        if value["bundle_digest"] != digest or value["bundle_id"] != (
            f"a-bundle-{digest[:24]}"
        ):
            raise MetadataValidationError("DatasetBundle digest 或 bundle_id 校验失败")
        return cls(
            schema_version="a.dataset-bundle.v1",
            bundle_id=f"a-bundle-{digest[:24]}",
            bundle_digest=digest,
            corridor_id=corridor_id,
            as_of_time=as_of,
            requested_start=start,
            requested_end=end,
            minimum_required_end=minimum_end,
            requested_data_types=requested_types,
            source_snapshot_ids=snapshots,
            records=records,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "bundle_digest": self.bundle_digest,
            "corridor_id": self.corridor_id,
            "as_of_time": isoformat_utc(self.as_of_time),
            "requested_start": isoformat_utc(self.requested_start),
            "requested_end": isoformat_utc(self.requested_end),
            "minimum_required_end": isoformat_utc(self.minimum_required_end),
            "requested_data_types": list(self.requested_data_types),
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "record_count": len(self.records),
            "records": [entry.to_dict() for entry in self.records],
        }


def _bundle_digest(
    *,
    corridor_id: str,
    as_of_time: datetime,
    requested_start: datetime,
    requested_end: datetime,
    minimum_required_end: datetime,
    requested_data_types: tuple[str, ...],
    source_snapshot_ids: tuple[str, ...],
    records: tuple[DatasetBundleRecord, ...],
) -> str:
    identity = {
        "schema_version": "a.dataset-bundle.v1",
        "corridor_id": corridor_id,
        "as_of_time": isoformat_utc(as_of_time),
        "requested_start": isoformat_utc(requested_start),
        "requested_end": isoformat_utc(requested_end),
        "minimum_required_end": isoformat_utc(minimum_required_end),
        "requested_data_types": list(requested_data_types),
        "source_snapshot_ids": list(source_snapshot_ids),
        "records": [entry.to_dict() for entry in records],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
