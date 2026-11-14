import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trailprint.bambu import BambuSettings
from trailprint.preview_jobs import PreviewJobs
from trailprint.project_operations import OPERATIONS


def test_scale_failure_keeps_previous_model_and_preferences(tmp_path):
    (tmp_path / 'build').mkdir()
    (tmp_path / 'build/current-settings.json').write_text(json.dumps({'terrain_elevation_multiplier': 2}))
    (tmp_path / 'route.gpx').write_text('route')
    old = tmp_path / 'build/preview.png'
    old.write_bytes(b'original')
    job = dict(id='scale', scale=3)
    saved = []
    with patch('trailprint.blender_bridge.run_blender_build', side_effect=ValueError('failed')) as build:
        PreviewJobs().run(job, tmp_path, BambuSettings.validated(6), SimpleNamespace(), lambda: saved.append(True))
    assert job['state'] == 'error'
    assert build.call_args.args[1].terrain_elevation_multiplier == 3
    assert build.call_args.args[0] != tmp_path
    assert old.read_bytes() == b'original'
    assert json.loads((tmp_path / 'build/current-settings.json').read_text())['terrain_elevation_multiplier'] == 2
    assert not saved


def test_refresh_failure_preserves_existing_exports_and_releases_lock(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build/preview.png").write_bytes(b"previous")
    (tmp_path / "model.3mf").write_bytes(b"previous 3mf")
    manager = PreviewJobs()
    job = dict(id="test", state="queued")
    OPERATIONS.acquire(tmp_path, "rendering previews", "test")
    with patch(
        "trailprint.preview_jobs.stage_export_previews", side_effect=ValueError("render failed")
    ):
        manager.run(
            job, tmp_path, BambuSettings.validated(6, terrain_mode="single"), SimpleNamespace()
        )
    assert job["state"] == "error"
    assert (tmp_path / "build/preview.png").read_bytes() == b"previous"
    assert (tmp_path / "model.3mf").read_bytes() == b"previous 3mf"
    with OPERATIONS.hold(tmp_path, "testing"):
        pass


def test_preview_captures_settings_and_environment_before_thread_runs(tmp_path):
    manager = PreviewJobs()
    environment = SimpleNamespace(filaments=[dict(color="#000000")])
    settings = BambuSettings.validated(6, terrain_mode="single")
    with patch("trailprint.preview_jobs.threading.Thread") as thread:
        job = manager.start(tmp_path, settings, environment)
        environment.filaments[0]["color"] = "#FFFFFF"
        captured = thread.call_args.kwargs["args"]
        assert captured[3].filaments[0]["color"] == "#000000"
        with pytest.raises(ValueError):
            manager.start(tmp_path, settings, environment)
    OPERATIONS.release(tmp_path, job["id"])


def test_preferences_failure_releases_reservation(tmp_path):
    manager = PreviewJobs()

    def fail():
        raise ValueError("cannot save")

    with pytest.raises(ValueError):
        manager.start(tmp_path, BambuSettings.validated(6), SimpleNamespace(), fail)
    with OPERATIONS.hold(tmp_path, "testing"):
        pass


def test_success_marks_preview_only_without_touching_3mf(tmp_path):
    (tmp_path / "build").mkdir()
    export = tmp_path / "model.3mf"
    export.write_bytes(b"unchanged")
    manager = PreviewJobs()
    job = dict(id="success", state="queued")

    def stage(project, stage, *args):
        image = stage / "preview.png"
        image.write_bytes(b"new preview")
        report = stage / "validation.json"
        report.write_text(json.dumps({"valid": True}))
        return {project / "build/preview.png": image, project / "build/validation.json": report}, {
            "color_revision": "colors"
        }

    with patch("trailprint.preview_jobs.stage_export_previews", side_effect=stage):
        manager.run(
            job, tmp_path, BambuSettings.validated(6, terrain_mode="single"), SimpleNamespace()
        )
    assert job["state"] == "success"
    assert json.loads((tmp_path / "build/validation.json").read_text())["preview_only"]
    assert export.read_bytes() == b"unchanged"
