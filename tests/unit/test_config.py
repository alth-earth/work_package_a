from pathlib import Path

import pytest

from arctic_route_data.config import config_to_dict, load_config
from arctic_route_data.errors import MetadataValidationError


def test_checked_in_configuration_is_loaded_and_complete():
    config = load_config(Path("configs/work_package_a.toml"))

    assert config.cache.target_horizon_hours == 156
    assert config.cache.minimum_complete_horizon_hours == 132
    assert config.acquisition.gfs_step_hours == 3
    assert config.clock.default_speed == 360.0
    assert set(config.corridors) == {
        "offshore_murmansk_to_offshore_dikson",
        "tromso_to_isfjorden_outer",
    }
    assert config.corridors["offshore_murmansk_to_offshore_dikson"].start == (
        33.60,
        69.15,
    )
    assert config.corridors["tromso_to_isfjorden_outer"].destination == (
        13.00,
        78.15,
    )
    assert config_to_dict(config)["corridors"]["tromso_to_isfjorden_outer"]["bbox"] == [
        10.0,
        68.5,
        22.0,
        79.5,
    ]


def test_invalid_horizon_policy_is_rejected(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text(
        """
[cache]
max_memory_mb = 1
slow_frames_per_partition = 2
dynamic_frames_per_partition = 2
history_hours = 0
target_horizon_hours = 24
minimum_complete_horizon_hours = 132
[acquisition]
gfs_step_hours = 3
cycle_lookback_count = 1
request_timeout_seconds = 5
[clock]
default_speed = 1
[corridors.route_a]
bbox = [10, 60, 20, 70]
start = [11, 61]
destination = [19, 69]
""",
        encoding="utf-8",
    )

    with pytest.raises(MetadataValidationError, match="不能超过"):
        load_config(path)
