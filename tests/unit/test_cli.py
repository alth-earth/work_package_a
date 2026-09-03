import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import arctic_route_data.cli as cli_module
from arctic_route_data.cli import main
from arctic_route_data.shared_context import load_shared_scenario_request


def _workspace_root() -> Path:
    env = os.environ.get("ARCTIC_ROUTE_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "arctic_route_contracts").is_dir():
            return parent
    return Path.home()


@pytest.mark.parametrize("corridor_flag", ["--corridor", "--scenario"])
def test_acquire_cli_rejects_source_type_mismatch_before_network(
    tmp_path, corridor_flag
):
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "acquire-forecast",
                "--data-root",
                str(tmp_path / "data"),
                corridor_flag,
                "tromso_to_svalbard",
                "--sources",
                "gfs",
                "--types",
                "wave",
            ]
        )

    assert exc_info.value.code == 2


def test_carra_cli_rejects_non_utc_offset_before_network(tmp_path):
    with pytest.raises(ValueError, match="必须显式使用 UTC"):
        main(
            [
                "acquire-carra",
                "--data-root",
                str(tmp_path / "data"),
                "--corridor",
                "tromso_to_isfjorden_outer",
                "--start",
                "2026-02-15T03:00:00+03:00",
                "--end",
                "2026-02-15T06:00:00+03:00",
                "--types",
                "temperature",
                "--cdsapi-rc-file",
                str(tmp_path / ".cdsapirc"),
            ]
        )


def test_carra_cli_writes_machine_summary(tmp_path, monkeypatch, capsys):
    class FakeCarraAcquisition:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def acquire_between(self, **kwargs):
            return SimpleNamespace(
                cycles_requested=1,
                cache_hits=1,
                downloaded_cycles=0,
                frames_processed=1,
                frames_published=1,
                source_snapshot_ids=("carra-test-snapshot",),
            )

    monkeypatch.setattr(cli_module, "CarraAcquisition", FakeCarraAcquisition)
    summary = tmp_path / "job" / "carra-summary.json"
    credential = tmp_path / ".cdsapirc"
    assert (
        main(
            [
                "acquire-carra",
                "--data-root",
                str(tmp_path / "data"),
                "--corridor",
                "tromso_to_isfjorden_outer",
                "--start",
                "2026-02-15T00:00:00Z",
                "--end",
                "2026-02-15T00:00:00Z",
                "--types",
                "temperature",
                "--cdsapi-rc-file",
                str(credential),
                "--summary-output",
                str(summary),
            ]
        )
        == 0
    )
    stdout = json.loads(capsys.readouterr().out)
    persisted = json.loads(summary.read_text(encoding="utf-8"))
    assert persisted == stdout
    assert persisted["cache_hits"] == 1
    assert ".cdsapirc" not in summary.read_text(encoding="utf-8")


def test_historical_window_requires_explicit_start(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "acquire-window",
                "--corridor",
                "tromso_to_svalbard",
                "--mode",
                "retrospective_best_estimate",
            ]
        )
    assert exc_info.value.code == 2
    assert "必须显式指定 --start" in capsys.readouterr().err

def test_shared_scenario_adapter_uses_corridor_as_manifest_route_id(capsys):
    contracts_root = str(_workspace_root() / "arctic_route_contracts" / "configs")
    if not Path(contracts_root).is_dir():
        pytest.skip("shared contracts checkout is unavailable")

    assert (
        main(
            [
                "shared-scenario",
                "--contracts-config-root",
                contracts_root,
                "--scenario",
                "tromso_isfjorden_july_2026_retrospective_v1",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["corridor_id"] == payload["manifest_route_id"]
    assert payload["corridor_id"] == "tromso_to_isfjorden_outer"
    assert payload["horizon_hours"] == 96
    assert payload["acquisition_mode"] == "retrospective_best_estimate"


def test_frozen_shared_scenario_can_select_horizon_from_candidate_route(capsys):
    contracts_root = str(_workspace_root() / "arctic_route_contracts" / "configs")
    if not Path(contracts_root).is_dir():
        pytest.skip("shared contracts checkout is unavailable")

    assert (
        main(
            [
                "shared-scenario",
                "--contracts-config-root",
                contracts_root,
                "--scenario",
                "tromso_isfjorden_frozen_forecast_template_v1",
                "--simulation-start",
                "2026-08-12T00:00:00Z",
                "--candidate-route-distance-nm",
                "1000",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["horizon_hours"] == 144
    assert payload["scenario_id"].endswith("_h144_v1")
    assert payload["end"] == "2026-08-18T00:00:00+00:00"


def test_frozen_shared_scenario_rejects_candidate_beyond_formal_cap():
    contracts_root = str(_workspace_root() / "arctic_route_contracts" / "configs")
    if not Path(contracts_root).is_dir():
        pytest.skip("shared contracts checkout is unavailable")

    with pytest.raises(ValueError, match="forecast_coverage_insufficient"):
        load_shared_scenario_request(
            scenario_id="murmansk_dikson_frozen_forecast_template_v1",
            config_root=contracts_root,
            simulation_start=datetime(2026, 8, 12, tzinfo=UTC),
            candidate_route_distance_nm=3000,
        )


def test_acquire_window_can_take_all_time_and_identity_from_shared_scenario(
    tmp_path, monkeypatch, capsys
):
    contracts_root = str(_workspace_root() / "arctic_route_contracts" / "configs")
    if not Path(contracts_root).is_dir():
        pytest.skip("shared contracts checkout is unavailable")
    captured = {}

    def acquire(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            source="fixture",
            source_snapshot_ids=("fixture-snapshot",),
            records=(),
            warnings=(),
        )

    monkeypatch.setattr(cli_module.NativeForecastAcquirer, "acquire_gfs", acquire)

    assert (
        main(
            [
                "acquire-window",
                "--data-root",
                str(tmp_path / "data"),
                "--shared-scenario",
                "tromso_isfjorden_july_2026_retrospective_v1",
                "--contracts-config-root",
                contracts_root,
                "--sources",
                "gfs",
                "--types",
                "wind_field",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert captured["route_id"] == "tromso_to_isfjorden_outer"
    assert captured["as_of"] == datetime(2026, 7, 15, tzinfo=UTC)
    assert captured["horizon_hours"] == 96
    assert captured["mode"].value == "retrospective_best_estimate"
    assert payload["shared_scenario_id"] == (
        "tromso_isfjorden_july_2026_retrospective_v1"
    )


def test_replay_outputs_coverage_and_atomically_persists_bundle(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    class Report:
        complete = True

        def to_dict(self):
            return {"complete": True, "covers_requested_window": True}

    class Bundle:
        def to_dict(self):
            return {
                "schema_version": "a.dataset-bundle.v2",
                "bundle_id": "a-bundle-" + "a" * 24,
                "records": [{"data_id": "frame-a"}],
            }

    def prepare(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            route_id="tromso_to_svalbard",
            as_of_time=datetime(2026, 8, 11, 15, tzinfo=UTC),
            generation_id=0,
            coverage={"wave": Report()},
            frames={"wave": ()},
            dataset_bundle=Bundle(),
        )

    monkeypatch.setattr(cli_module.WorkPackageA, "prepare_window_for_b", prepare)
    output = tmp_path / "bundles" / "window.json"

    assert (
        main(
            [
                "replay",
                "--data-root",
                str(tmp_path / "data"),
                "--route-id",
                "tromso_to_svalbard",
                "--at",
                "2026-08-11T15:00:00Z",
                "--types",
                "wave",
                "--horizon-hours",
                "6",
                "--minimum-horizon-hours",
                "6",
                "--bundle-output",
                str(output),
                "--summary-only",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert payload["coverage"]["wave"]["complete"] is True
    assert payload["all_required_complete"] is True
    assert payload["bundle_persisted"] is True
    assert payload["selected_record_counts"] == {"wave": 0}
    assert "records" not in payload["dataset_bundle"]
    assert persisted["schema_version"] == "a.dataset-bundle.v2"
    assert persisted["records"] == [{"data_id": "frame-a"}]
    assert captured["target_horizon_hours"] == 6
    assert captured["minimum_complete_horizon_hours"] == 6
    assert captured["knowledge_as_of"] == datetime(2026, 8, 11, 15, tzinfo=UTC)
    assert payload["replay_mode"] == "causal"


def test_retrospective_replay_requires_and_propagates_explicit_knowledge_cutoff(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    class Report:
        complete = False

        def to_dict(self):
            return {"complete": False}

    class Bundle:
        def to_dict(self):
            return {"schema_version": "a.dataset-bundle.v1", "records": []}

    def prepare(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            route_id="route-a",
            as_of_time=kwargs["knowledge_as_of"],
            generation_id=0,
            coverage={"wave": Report()},
            frames={"wave": ()},
            dataset_bundle=Bundle(),
        )

    monkeypatch.setattr(cli_module.WorkPackageA, "prepare_window_for_b", prepare)
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "replay",
                "--data-root",
                str(tmp_path / "data"),
                "--route-id",
                "route-a",
                "--at",
                "2026-07-15T00:00:00Z",
                "--mode",
                "retrospective_best_estimate",
                "--types",
                "wave",
            ]
        )
    assert exc_info.value.code == 2
    assert "--knowledge-as-of" in capsys.readouterr().err

    assert (
        main(
            [
                "replay",
                "--data-root",
                str(tmp_path / "data"),
                "--route-id",
                "route-a",
                "--at",
                "2026-07-15T00:00:00Z",
                "--mode",
                "retrospective_best_estimate",
                "--knowledge-as-of",
                "2026-08-12T00:00:00Z",
                "--types",
                "wave",
                "--allow-incomplete",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert captured["knowledge_as_of"] == datetime(2026, 8, 12, tzinfo=UTC)
    assert payload["simulation_time"] == "2026-07-15T00:00:00+00:00"
    assert payload["knowledge_as_of"] == "2026-08-12T00:00:00+00:00"


def test_replay_does_not_persist_incomplete_bundle_without_opt_in(
    tmp_path, monkeypatch, capsys
):
    class Report:
        complete = False

        def to_dict(self):
            return {"complete": False, "covers_requested_window": False}

    class Bundle:
        def to_dict(self):
            return {"schema_version": "a.dataset-bundle.v1", "records": []}

    monkeypatch.setattr(
        cli_module.WorkPackageA,
        "prepare_window_for_b",
        lambda self, **kwargs: SimpleNamespace(
            route_id="tromso_to_svalbard",
            as_of_time=datetime(2026, 8, 11, 15, tzinfo=UTC),
            generation_id=0,
            coverage={"wave": Report()},
            frames={"wave": ()},
            dataset_bundle=Bundle(),
        ),
    )
    output = tmp_path / "incomplete.json"

    exit_code = main(
        [
            "replay",
            "--data-root",
            str(tmp_path / "data"),
            "--route-id",
            "tromso_to_svalbard",
            "--at",
            "2026-08-11T15:00:00Z",
            "--types",
            "wave",
            "--bundle-output",
            str(output),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["all_required_complete"] is False
    assert payload["bundle_persisted"] is False
    assert not output.exists()

    assert (
        main(
            [
                "replay",
                "--data-root",
                str(tmp_path / "data"),
                "--route-id",
                "tromso_to_svalbard",
                "--at",
                "2026-08-11T15:00:00Z",
                "--types",
                "wave",
                "--bundle-output",
                str(output),
                "--allow-incomplete",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle_persisted"] is False
    assert not output.exists()


def test_copernicus_env_file_is_parsed_as_data_and_requires_private_mode(
    tmp_path, monkeypatch
):
    credentials = tmp_path / ".env.copernicus"
    credentials.write_text(
        "COPERNICUSMARINE_SERVICE_USERNAME=demo-user\n"
        "COPERNICUSMARINE_SERVICE_PASSWORD='literal;not-shell'\n",
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    monkeypatch.delenv("COPERNICUSMARINE_SERVICE_USERNAME", raising=False)
    monkeypatch.delenv("COPERNICUSMARINE_SERVICE_PASSWORD", raising=False)

    cli_module._load_copernicus_env_file(credentials)

    assert cli_module.os.environ["COPERNICUSMARINE_SERVICE_USERNAME"] == "demo-user"
    assert (
        cli_module.os.environ["COPERNICUSMARINE_SERVICE_PASSWORD"]
        == "literal;not-shell"
    )

    credentials.chmod(0o644)
    with pytest.raises(RuntimeError, match="600"):
        cli_module._load_copernicus_env_file(credentials)
