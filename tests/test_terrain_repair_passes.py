import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh

from trailprint.blender_bridge import BuildSettings, run_blender_build
from trailprint.projects import create_project
from trailprint.water_geometry import (
    TerrainRepairError,
    _terrain_repair_pass,
    repair_terrain_for_water_boolean,
)


def test_captured_water_boolean_requires_second_pass(tmp_path):
    fixture = Path(__file__).parent / "fixtures/water-terrain-overlap.npz"
    with np.load(fixture, allow_pickle=False) as data:
        original = trimesh.Trimesh(data["vertices"], data["faces"], process=True)
    source, output = tmp_path / "source.stl", tmp_path / "fixed.stl"
    original.export(source)
    once, _ = _terrain_repair_pass(original.copy())
    assert not once.is_watertight  # The previous one-pass implementation failed here.
    report = repair_terrain_for_water_boolean(source, output)
    assert len(report["passes"]) == 2
    assert report["passes"][0]["remaining_non_manifold_edges"] == 2
    assert report["passes"][1]["remaining_non_manifold_edges"] == 0
    actual = trimesh.load_mesh(output, process=True)
    assert actual.is_watertight and actual.is_winding_consistent and actual.volume > 0
    assert abs(actual.volume - original.volume) < .01
    np.testing.assert_allclose(actual.bounds, original.bounds, atol=.01, rtol=0)


def test_actual_hole_stops_after_three_passes_and_reports_locations(tmp_path):
    mesh = trimesh.creation.box((10, 10, 4))
    mesh.update_faces(np.arange(len(mesh.faces)) != 0)
    source = tmp_path / "hole.stl"
    output = tmp_path / "fixed.stl"
    mesh.export(source)
    with pytest.raises(TerrainRepairError, match="after 3 repair passes") as error:
        repair_terrain_for_water_boolean(source, output)
    report = error.value.repair_report
    assert not report["valid"]
    assert len(report["passes"]) == 3
    assert all(item["remaining_non_manifold_edges"] == 3 for item in report["passes"])
    assert report["passes"][-1]["defect_locations_mm"]
    assert not output.exists()


def test_valid_terrain_stops_after_one_pass(tmp_path):
    source, output = tmp_path / "source.stl", tmp_path / "fixed.stl"
    trimesh.creation.box((10, 10, 4)).export(source)
    with patch("trailprint.water_geometry._terrain_repair_pass", wraps=_terrain_repair_pass) as run:
        report = repair_terrain_for_water_boolean(source, output)
    assert run.call_count == 1
    assert report["passes"][0]["remaining_non_manifold_edges"] == 0


def test_failed_repair_diagnostics_survive_staging_cleanup_and_preserve_models(tmp_path):
    fixture = Path(__file__).parent / "fixtures/example-route.gpx"
    project = create_project(tmp_path / "prints", "Preserve Model", fixture.read_bytes())
    preserved = [project / "deliverables/previous.stl", project / "deliverables/previous.3mf",
                 project / "build/preview.png"]
    for file in preserved:
        file.write_bytes(b"previous successful build")
    blender = tmp_path / "blender"
    blender.touch()
    diagnostic = {"valid": False, "passes": [{"remaining_non_manifold_edges": 3,
                                              "defect_locations_mm": [[1, 2, 3]]}]}

    def generate(project, settings, stage, *args):
        stage.mkdir(parents=True)
        return subprocess.CompletedProcess([], 0, "", "")

    with patch("trailprint.blender_bridge._run_attempt", side_effect=generate), patch(
            "trailprint.blender_bridge._integrate_generated_water",
            side_effect=TerrainRepairError("Repair failed after 3 passes", diagnostic)):
        with pytest.raises(ValueError, match="Repair failed after 3 passes"):
            run_blender_build(project, BuildSettings.validated(plate_title="Test"), blender=blender)
    report = json.loads((project / "build/validation.json").read_text())
    assert report["terrain_repair"] == diagnostic
    assert all(file.read_bytes() == b"previous successful build" for file in preserved)
    assert not list((project / "build").glob("trailprint3d-*"))
