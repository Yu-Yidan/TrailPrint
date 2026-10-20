import json
from dataclasses import asdict

import pytest

from trailprint.blender_bridge import BuildSettings
from trailprint.blender_worker import terrain_relief_scaling
from trailprint.build_settings import current_model_settings


def test_default_retains_natural_relief_and_comparison_only_scales_elevation():
    natural = terrain_relief_scaling([113, 777.5], [1.23, 1.24], .54, 15)
    scaled = terrain_relief_scaling([113, 777.5], [1.23, 1.24], .54, 15, 1.5)
    assert natural["elevation_multiplier"] == 1
    assert not natural["cap_applied"]
    assert not scaled["cap_applied"]
    assert scaled["requested_elevation_multiplier"] == scaled["elevation_multiplier"] == 1.5
    assert scaled["final_model_relief_mm"] == pytest.approx(natural["final_model_relief_mm"] * 1.5)


def test_relief_cap_is_applied_after_requested_multiplier():
    result = terrain_relief_scaling([1000, 1200], [1, 1], 40, 15, 2)
    assert result["cap_applied"]
    assert result["requested_elevation_multiplier"] == 2
    assert result["uncapped_model_relief_mm"] == 16
    assert result["final_model_relief_mm"] == 15
    assert result["elevation_multiplier"] == 1.875


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_multipliers_are_rejected(value):
    with pytest.raises(ValueError):
        BuildSettings.validated(terrain_elevation_multiplier=value)
    with pytest.raises(ValueError):
        terrain_relief_scaling([0, 1000], [1, 1], 10, 15, value)


def test_comparison_setting_restores_and_older_models_keep_one_x(tmp_path):
    directory = tmp_path / "products/fridge_magnet_raised_map"
    (directory / "runs").mkdir(parents=True)
    settings = BuildSettings.validated(terrain_elevation_multiplier=1.5)
    (directory / "current.json").write_text(json.dumps({"id_build": "comparison"}))
    run = directory / "runs/comparison.json"
    values = asdict(settings)
    run.write_text(json.dumps({"state": "success", "settings": values}))
    assert current_model_settings(tmp_path, BuildSettings()) == settings
    values.pop("terrain_elevation_multiplier")
    run.write_text(json.dumps({"state": "success", "settings": values}))
    assert current_model_settings(tmp_path, BuildSettings()).terrain_elevation_multiplier == 1
