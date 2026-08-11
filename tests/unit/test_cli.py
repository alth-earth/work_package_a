import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import arctic_route_data.cli as cli_module
from arctic_route_data.cli import main


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
                "schema_version": "a.dataset-bundle.v1",
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
    assert persisted["schema_version"] == "a.dataset-bundle.v1"
    assert persisted["records"] == [{"data_id": "frame-a"}]
    assert captured["target_horizon_hours"] == 6
    assert captured["minimum_complete_horizon_hours"] == 6


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
