from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arctic_route_data.models import DataCategory, ManifestRecord, QualityFlag


@pytest.fixture
def make_record():
    def factory(
        *,
        data_id: str = "data-1",
        data_type: str = "wind_field",
        category: DataCategory = DataCategory.DYNAMIC,
        route_id: str = "route-a",
        variables: tuple[str, ...] = ("wind_u10", "wind_v10"),
        issue_time: datetime = datetime(2026, 7, 15, 0, tzinfo=UTC),
        valid_time: datetime = datetime(2026, 7, 15, 6, tzinfo=UTC),
        quality_flag: QualityFlag = QualityFlag.GOOD,
        version: str = "v1",
        size_bytes: int = 128,
        metadata: dict | None = None,
    ) -> ManifestRecord:
        return ManifestRecord(
            data_id=data_id,
            data_type=data_type,
            category=category,
            route_id=route_id,
            variables=variables,
            issue_time=issue_time,
            valid_time=valid_time,
            ingest_time=datetime(2026, 7, 16, tzinfo=UTC),
            bbox=(10.0, 68.0, 22.0, 79.0),
            crs="EPSG:4326",
            resolution=(0.25, 0.25),
            source="test",
            quality_flag=quality_flag,
            version=version,
            checksum="a" * 64,
            relative_path=f"ready/{data_id}.nc",
            size_bytes=size_bytes,
            metadata=metadata or {},
        )

    return factory
