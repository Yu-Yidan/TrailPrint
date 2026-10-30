import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from trailprint.bambu import BambuSettings
from trailprint.bambu_jobs import BambuJobManager
from trailprint.blender_bridge import BuildSettings
from trailprint.jobs import BuildManager
from trailprint.project_operations import OPERATIONS, ProjectOperations
from trailprint.publishing import publish_files


@pytest.mark.parametrize("failure_index", [0, 1, 2])
def test_publication_restores_entire_previous_generation(tmp_path, failure_index):
    changes = {}
    for i in range(3):
        old, new = tmp_path / f"old-{i}", tmp_path / f"new-{i}"
        old.write_bytes(f"previous-{i}".encode())
        new.write_bytes(f"next-{i}".encode())
        changes[old] = new
    stale = tmp_path / "stale.3mf"
    stale.write_bytes(b"previous export")
    changes = {stale: None, **changes}
    original = os.replace
    counter = 0

    def fail_once(src, dst):
        nonlocal counter
        current = counter
        counter += 1
        if current == failure_index:
            raise OSError("injected publication failure")
        return original(src, dst)

    with patch("trailprint.publishing.os.replace", side_effect=fail_once):
        with pytest.raises(OSError, match="injected"):
            publish_files(changes, tmp_path)
    assert stale.read_bytes() == b"previous export"
    for i in range(3):
        assert (tmp_path / f"old-{i}").read_bytes() == f"previous-{i}".encode()


def test_publication_restores_new_files_and_leaves_unrelated_files(tmp_path):
    unrelated = tmp_path / "custom.3mf"
    unrelated.write_bytes(b"keep")
    staged = tmp_path / "staged"
    staged.write_bytes(b"new")
    destination = tmp_path / "new-result"
    with pytest.raises(FileNotFoundError):
        publish_files({destination: staged, tmp_path / "report": tmp_path / "missing"}, tmp_path)
    assert not destination.exists()
    assert unrelated.read_bytes() == b"keep"


def test_successful_publication_invalidates_only_owned_export(tmp_path):
    old = tmp_path / "old.3mf"
    old.write_bytes(b"stale")
    custom = tmp_path / "custom.3mf"
    custom.write_bytes(b"keep")
    new = tmp_path / "staged"
    new.write_bytes(b"new")
    publish_files({old: None, tmp_path / "result": new}, tmp_path)
    assert not old.exists()
    assert custom.read_bytes() == b"keep"
    assert (tmp_path / "result").read_bytes() == b"new"


def test_cancellation_after_backup_preserves_previous_outputs(tmp_path):
    from trailprint.blender_bridge import BuildCancelled

    destination, staged = tmp_path / "model", tmp_path / "staged"
    destination.write_bytes(b"previous model")
    staged.write_bytes(b"next model")

    def cancel():
        raise BuildCancelled("cancelled before commit")

    with pytest.raises(BuildCancelled):
        publish_files({destination: staged}, tmp_path, before_commit=cancel)
    assert destination.read_bytes() == b"previous model"
    assert staged.read_bytes() == b"next model"


def test_reservation_is_atomic_and_owner_scoped(tmp_path):
    operations = ProjectOperations()
    operations.acquire(tmp_path, "building", "first")
    operations.release(tmp_path, "wrong owner")
    with pytest.raises(ValueError, match="building"):
        operations.acquire(tmp_path / ".", "exporting", "second")
    operations.release(tmp_path, "first")
    with operations.hold(tmp_path, "deleting"):
        with pytest.raises(ValueError, match="deleting"):
            operations.acquire(tmp_path, "building", "third")
    with operations.hold(tmp_path, "exporting"):
        pass


def test_simultaneous_operations_have_exactly_one_owner(tmp_path):
    operations = ProjectOperations()
    ready = threading.Barrier(3)
    attempted = threading.Barrier(3)
    results = []

    def run(owner):
        ready.wait(timeout=5)
        try:
            operations.acquire(tmp_path, owner, owner)
        except ValueError:
            results.append(False)
        else:
            results.append(True)
        attempted.wait(timeout=5)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("building", "exporting")]
    for thread in threads:
        thread.start()
    ready.wait(timeout=5)
    attempted.wait(timeout=5)
    for thread in threads:
        thread.join(5)
    assert sorted(results) == [False, True]


@pytest.mark.parametrize("kind", ["build", "export"])
def test_thread_start_failure_releases_reservation(tmp_path, kind):
    manager = BuildManager() if kind == "build" else BambuJobManager()
    with patch("threading.Thread.start", side_effect=RuntimeError("start failed")):
        with pytest.raises(RuntimeError, match="start failed"):
            if kind == "build":
                manager.start(tmp_path, BuildSettings())
            else:
                manager.start(tmp_path, BambuSettings.validated(1), environment=object())
    assert not manager.is_active(tmp_path)
    with OPERATIONS.hold(tmp_path, "deleting"):
        pass


def test_build_blocks_export_and_releases_after_failure(tmp_path):
    entered, finish = threading.Event(), threading.Event()
    manager = BuildManager()

    def build(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        raise ValueError("failed build")

    with patch("trailprint.jobs.run_blender_build", side_effect=build):
        job = manager.start(tmp_path, BuildSettings())
        assert entered.wait(5)
        try:
            with pytest.raises(ValueError, match="building"):
                BambuJobManager().start(tmp_path, BambuSettings.validated(1), environment=object())
            with pytest.raises(ValueError, match="building"):
                with OPERATIONS.hold(tmp_path, "deleting"):
                    pass
        finally:
            finish.set()
        for thread in threading.enumerate():
            if thread.name == f"trailprint-{job.id[:8]}":
                thread.join(5)
    with OPERATIONS.hold(tmp_path, "deleting"):
        pass
    assert job.state == "error"
    assert job.validation["valid"] is False


def test_export_blocks_build_but_other_projects_are_available(tmp_path):
    manager = BambuJobManager()
    with patch("threading.Thread.start"):
        job = manager.start(tmp_path, BambuSettings.validated(1), environment=object())
    try:
        with pytest.raises(ValueError, match="exporting"):
            BuildManager().start(tmp_path, BuildSettings())
        with OPERATIONS.hold(tmp_path / "other", "building"):
            pass
        with patch("trailprint.bambu_jobs.create_bambu_project", side_effect=ValueError("export failed")):
            manager._run(job)
    finally:
        OPERATIONS.release(tmp_path, job.id)
    with OPERATIONS.hold(tmp_path, "deleting"):
        pass
    assert job.state == "error"


def test_build_publication_failure_preserves_models_metadata_and_export(tmp_path):
    import trimesh

    import trailprint.blender_bridge as bridge

    project = tmp_path / "project"
    deliverables, build = project / "deliverables", project / "build"
    deliverables.mkdir(parents=True)
    build.mkdir()
    (project / "route.gpx").write_text(
        '<gpx><trk><trkseg><trkpt lat="1" lon="2"/>'
        '<trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>')
    previous = {deliverables / "previous.stl": b"previous model",
                deliverables / "project.3mf": b"previous export",
                build / "preview.png": b"previous preview",
                build / "bambu-validation.json": b'{"valid":true}',
                build / "validation.json": b'{"valid":true}'}
    for path, contents in previous.items():
        path.write_bytes(contents)
    blender = tmp_path / "blender"
    blender.touch()

    def generate(command, **kwargs):
        config = json.loads(Path(command[-1]).read_text())
        if "output_path" in config:
            return subprocess.CompletedProcess(command, 1, "", "preview unavailable")
        output = Path(config["export_path"])
        trimesh.creation.box((100, 80, 5)).export(output / "project.stl")
        trail = trimesh.creation.box((60, 3, 2)).subdivide()
        trail.apply_translation((0, 0, 3.5))
        trail.export(output / "project_Trail.stl")
        return subprocess.CompletedProcess(command, 0, "", "")

    def failing_publish(changes, backup_root, **kwargs):
        original = os.replace
        injected = False

        def fail_once(src, dst):
            nonlocal injected
            if dst == build / "validation.json" and not injected:
                injected = True
                raise OSError("publication failed")
            return original(src, dst)

        with patch("trailprint.publishing.os.replace", side_effect=fail_once):
            publish_files(changes, backup_root, **kwargs)

    with patch.object(bridge.subprocess, "run", side_effect=generate), patch.object(
            bridge, "publish_files", side_effect=failing_publish):
        with pytest.raises(OSError, match="publication failed"):
            bridge.run_blender_build(project, BuildSettings(), blender=blender)
    for path, contents in previous.items():
        assert path.read_bytes() == contents
    assert not (deliverables / "project.stl").exists()
