from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_route_data.manifest import ManifestStore
from arctic_route_data.models import DataCategory, ManifestRecord, QualityFlag

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def test_winter_runner_acquires_static_layer_for_a_fresh_root(tmp_path, monkeypatch) -> None:
    runner = _load_script("winter_window_acquisition.py")
    root = tmp_path / "winter-screening-root"
    calls: list[dict] = []

    class FakeStaticLayerAcquirer:
        def __init__(self, data_root: Path) -> None:
            assert data_root == root

        def acquire_gebco(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                records=(object(),), source_snapshot_ids=("gebco-test",), warnings=()
            )

    monkeypatch.setattr(runner, "StaticLayerAcquirer", FakeStaticLayerAcquirer)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "winter-window-acquisition",
            "--start",
            "2026-02-01T00:00:00Z",
            "--end",
            "2026-02-01T01:00:00Z",
            "--data-root",
            str(root),
            "--types",
            "land_sea_mask",
        ],
    )

    assert runner.main() == 0
    assert calls == [
        {
            "route_id": runner.DEFAULT_ROUTE_ID,
            "bounds": runner.DEFAULT_BOUNDS,
            "data_types": ("land_sea_mask",),
            "mode": runner.AcquisitionMode.RETROSPECTIVE_BEST_ESTIMATE,
        }
    ]


def test_winter_runner_preflight_rejects_residual_before_acquisition(tmp_path) -> None:
    runner = _load_script("winter_window_acquisition.py")
    root = tmp_path / "winter-screening-root"
    root.mkdir()
    residue = root / "incoming" / "unfinished.nc.part"
    residue.parent.mkdir()
    residue.write_bytes(b"incomplete")

    with pytest.raises(RuntimeError, match="incomplete publication residue"):
        runner._preflight_data_root(
            root,
            route_id=runner.DEFAULT_ROUTE_ID,
            data_types=("land_sea_mask",),
            start=runner.parse_utc("2026-02-01T00:00:00Z", field="start"),
            end=runner.parse_utc("2026-02-01T01:00:00Z", field="end"),
        )


def test_cleanup_defaults_to_dry_run_and_requires_experimental_scope(tmp_path) -> None:
    cleanup = _load_script("audit_winter_cleanup.py")
    root = tmp_path / "winter-experiment"
    manifest = root / "manifest" / "manifest.sqlite3"
    ManifestStore(manifest)
    residue = root / "incoming" / "failed.nc.part"
    residue.parent.mkdir(exist_ok=True)
    residue.write_bytes(b"partial")

    scoped_root, scoped_manifest = cleanup._validate_scope(root, manifest)
    ledger = cleanup._audit(scoped_root, scoped_manifest)

    assert ledger["action"] == "DRY_RUN"
    entry = next(item for item in ledger["entries"] if item["path"] == "incoming/failed.nc.part")
    assert entry == {
        "path": "incoming/failed.nc.part",
        "size_bytes": len(b"partial"),
        "sha256": cleanup._sha256(residue),
        "references": [],
        "reason": "INCOMPLETE_PUBLICATION_RESIDUE",
        "eligible_for_quarantine": True,
    }
    assert any(
        item["reason"] == "MANIFEST_OR_SQLITE_SIDECAR_REQUIRES_RECOVERY"
        and not item["eligible_for_quarantine"]
        for item in ledger["entries"]
    )
    assert residue.is_file()
    with pytest.raises(ValueError, match="canonical, production, or frozen"):
        cleanup._validate_scope(tmp_path / "production-winter", manifest)


def test_cleanup_quarantine_then_purge_checks_the_ledger(tmp_path) -> None:
    cleanup = _load_script("audit_winter_cleanup.py")
    root = tmp_path / "winter-experiment"
    manifest = root / "manifest" / "manifest.sqlite3"
    ManifestStore(manifest)
    residue = root / "incoming" / "failed.nc.part"
    residue.parent.mkdir(exist_ok=True)
    residue.write_bytes(b"partial")
    ledger = cleanup._audit(root, manifest)

    cleanup._quarantine(ledger, root)
    assert ledger["action"] == "QUARANTINE"
    assert ledger["entries"][0]["action"] == "QUARANTINED"
    assert not residue.exists()
    quarantined = root / ledger["entries"][0]["quarantine_path"]
    assert quarantined.is_file()

    ledger_path = tmp_path / "ledger.json"
    cleanup._atomic_json(ledger_path, ledger)
    purged = cleanup._purge(ledger_path, root, manifest)
    assert purged["purge"]["purged"] == 1
    assert purged["purge"]["skipped"] == 0
    assert purged["purge"]["executed_at"].endswith("Z")
    assert not quarantined.exists()


def test_detided_retirement_detects_nested_normalization_metadata(tmp_path) -> None:
    retirement = _load_script("retire_detided_data.py")
    root = tmp_path / "winter-experiment"
    manifest = root / "manifest" / "manifest.sqlite3"
    store = ManifestStore(manifest)
    now = datetime(2026, 8, 26, tzinfo=UTC)
    store.register(
        ManifestRecord(
            data_id="detided-nested-test",
            data_type="ocean_current",
            category=DataCategory.DYNAMIC,
            route_id="winter-route",
            variables=("ocean_current_u", "ocean_current_v"),
            issue_time=now,
            valid_time=now,
            ingest_time=now,
            bbox=(0.0, 0.0, 1.0, 1.0),
            crs="EPSG:4326",
            resolution=(1.0, 1.0),
            source="Copernicus Marine",
            quality_flag=QualityFlag.GOOD,
            version="nested-detided",
            checksum="0" * 64,
            relative_path="ready/winter-route/ocean_current/test.nc",
            size_bytes=0,
            metadata={"normalization": {"current_component": "detided"}},
        )
    )

    rows = retirement._manifest_rows(root)
    assert len(rows) == 1
    assert rows[0]["data_id"] == "detided-nested-test"
