from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _dry_run(target: str) -> str:
    result = subprocess.run(
        ["make", "--dry-run", target],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_shared_acquisition_targets_install_contracts_adapter() -> None:
    for target in (
        "acquire-gfs",
        "acquire-copernicus",
        "acquire-land-sea-mask",
        "acquire-bathymetry",
        "acquire-emodnet",
    ):
        assert "--extra acquisition --extra contracts" in _dry_run(target)


def test_required_static_layer_is_separate_from_optional_sources() -> None:
    required = _dry_run("acquire-static")
    assert "--sources gebco --types land_sea_mask" in required
    assert "bathymetry" not in required
    assert "emodnet" not in required

    bathymetry = _dry_run("acquire-bathymetry")
    assert "--sources gebco --types bathymetry" in bathymetry
    emodnet = _dry_run("acquire-emodnet")
    assert "--sources emodnet --types long_term_restricted_area" in emodnet
