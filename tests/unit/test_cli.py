import pytest

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
