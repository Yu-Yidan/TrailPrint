import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from trailprint.blender_bridge import BuildSettings
from trailprint.projects import create_project, purge_trash, read_upload, slugify, trash_project
from trailprint.web import create_app


ROOT = Path(__file__).resolve().parents[1]
GPX = (ROOT / "prints/example-trail/route.gpx").read_bytes()


class ProjectServiceTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("  Mount Tam_Loop! "), "mount-tam-loop")
        with self.assertRaises(ValueError):
            slugify("../")

    def test_creation_is_complete_and_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            prints = Path(temp) / "prints"
            project = create_project(prints, ROOT / "presets", "Test Route", GPX)
            for relative in ("route.gpx", "print.yaml", "README.md", "build/.gitkeep", "deliverables/.gitkeep"):
                self.assertTrue((project / relative).exists(), relative)
            with self.assertRaisesRegex(ValueError, "already exists"):
                create_project(prints, ROOT / "presets", "Test Route", GPX)

    def test_invalid_gpx_leaves_no_project(self):
        with tempfile.TemporaryDirectory() as temp:
            prints = Path(temp) / "prints"
            with self.assertRaisesRegex(ValueError, "invalid GPX"):
                create_project(prints, ROOT / "presets", "Bad", b"not XML")
            self.assertEqual(list(prints.iterdir()), [])

    def test_upload_rejects_wrong_extension_and_size(self):
        with self.assertRaisesRegex(ValueError, "\.gpx extension"):
            read_upload(io.BytesIO(GPX), "route.txt")
        with self.assertRaisesRegex(ValueError, "10 MB or smaller"):
            read_upload(io.BytesIO(b"x" * 11), "route.gpx", limit=10)

    def test_confirmed_project_is_moved_to_recoverable_trash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = create_project(root / "prints", ROOT / "presets", "Keep My Files", GPX)
            (project / "deliverables/model.stl").write_text("mesh", encoding="utf-8")

            destination = trash_project(
                root / "prints", root / ".trailprint-trash", "keep-my-files", "keep-my-files",
                now=datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc))

            self.assertFalse(project.exists())
            self.assertTrue((destination / "route.gpx").is_file())
            self.assertTrue((destination / "deliverables/model.stl").is_file())
            self.assertTrue(destination.name.startswith("keep-my-files--20260823T120000Z--"))

    def test_delete_requires_exact_slug_and_protects_example(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = create_project(root / "prints", ROOT / "presets", "My Route", GPX)
            with self.assertRaisesRegex(ValueError, "exactly"):
                trash_project(root / "prints", root / ".trailprint-trash", "my-route", "My Route")
            self.assertTrue(project.is_dir())

            example = create_project(root / "prints", ROOT / "presets", "Example Trail", GPX)
            with self.assertRaisesRegex(ValueError, "protected"):
                trash_project(root / "prints", root / ".trailprint-trash",
                              "example-trail", "example-trail")
            self.assertTrue(example.is_dir())

    def test_purge_only_removes_recognized_trash_older_than_30_days(self):
        with tempfile.TemporaryDirectory() as temp:
            trash = Path(temp) / ".trailprint-trash"
            trash.mkdir()
            old = trash / "old-route--20260701T120000Z--0123abcd"
            recent = trash / "new-route--20260820T120000Z--0123abcd"
            unknown = trash / "do-not-touch"
            for path in (old, recent, unknown):
                path.mkdir()
            now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

            removed = purge_trash(trash, retention_days=30, now=now)

            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(recent.is_dir())
            self.assertTrue(unknown.is_dir())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prints").mkdir()
        (self.root / "presets").mkdir()
        (self.root / "presets/standard.yaml").write_text(
            (ROOT / "presets/standard.yaml").read_text(), encoding="utf-8")
        self.build_manager = FakeBuildManager()
        self.app = create_app(self.root, self.build_manager)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_and_create_redirect(self):
        self.assertIn(b"No projects yet", self.client.get("/").data)
        response = self.client.post("/projects", data={
            "name": "Coastal Run", "gpx": (io.BytesIO(GPX), "coast.gpx")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertTrue((self.root / "prints/coastal-run/route.gpx").is_file())
        page = self.client.get(response.headers["Location"])
        self.assertIn(b"Coastal Run", page.data)

    def test_invalid_upload_has_no_partial_project(self):
        response = self.client.post("/projects", data={
            "name": "Broken", "gpx": (io.BytesIO(b"bad"), "bad.gpx")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.root / "prints/broken").exists())

    def test_build_action_starts_blender_job_with_validated_settings(self):
        create_project(self.root / "prints", self.root / "presets", "Build Me", GPX)
        response = self.client.post("/projects/build-me/build", data={
            "shape": "CIRCLE", "shape_extra": "OUTER TEXT", "resolution": "6"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.build_manager.started.settings, BuildSettings("CIRCLE", "OUTER TEXT", 6))

    def test_build_rejects_incompatible_extra(self):
        create_project(self.root / "prints", self.root / "presets", "Build Me", GPX)
        response = self.client.post("/projects/build-me/build", data={
            "shape": "SQUARE", "shape_extra": "OUTER TEXT", "resolution": "4"},
            follow_redirects=True)
        self.assertIn(b"unavailable for Rectangle", response.data)
        self.assertIsNone(self.build_manager.started)

    def test_dashboard_shows_delete_control_except_for_example(self):
        create_project(self.root / "prints", self.root / "presets", "Example Trail", GPX)
        create_project(self.root / "prints", self.root / "presets", "Coastal Run", GPX)

        page = self.client.get("/").data

        self.assertIn(b'data-delete-slug="coastal-run"', page)
        self.assertNotIn(b'data-delete-slug="example-trail"', page)

    def test_delete_moves_project_after_exact_confirmation(self):
        project = create_project(self.root / "prints", self.root / "presets", "Coastal Run", GPX)

        response = self.client.post(
            "/projects/coastal-run/delete", data={"confirmation": "coastal-run"},
            follow_redirects=True)

        self.assertIn(b"Moved &#39;coastal-run&#39; to local trash", response.data)
        self.assertFalse(project.exists())
        trashed = list((self.root / ".trailprint-trash").iterdir())
        self.assertEqual(len(trashed), 1)
        self.assertTrue((trashed[0] / "route.gpx").is_file())

    def test_delete_rejects_wrong_confirmation_and_active_build(self):
        project = create_project(self.root / "prints", self.root / "presets", "Coastal Run", GPX)
        response = self.client.post(
            "/projects/coastal-run/delete", data={"confirmation": "Coastal Run"},
            follow_redirects=True)
        self.assertIn(b"exactly to confirm deletion", response.data)
        self.assertTrue(project.is_dir())

        self.build_manager.active = True
        response = self.client.post(
            "/projects/coastal-run/delete", data={"confirmation": "coastal-run"},
            follow_redirects=True)
        self.assertIn(b"currently building", response.data)
        self.assertTrue(project.is_dir())


class FakeBuildManager:
    def __init__(self):
        self.started = None
        self.active = False

    def start(self, project, settings):
        self.started = SimpleNamespace(id="test-job", project=project.resolve(), settings=settings,
                                       state="queued", message="Waiting to start")
        return self.started

    def for_project(self, project):
        return self.started if self.started and self.started.project == project.resolve() else None

    def is_active(self, project):
        return self.active

    def get(self, job_id):
        return self.started if self.started and self.started.id == job_id else None

    def serialize(self, job):
        return {"id": job.id, "state": job.state, "message": job.message, "phase": job.message,
                "percent": None, "elapsed_seconds": 0, "outputs": []}


if __name__ == "__main__":
    unittest.main()
