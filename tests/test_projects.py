import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from trailprint.blender_bridge import BuildSettings
from trailprint.projects import create_project, read_upload, slugify
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


class FakeBuildManager:
    def __init__(self):
        self.started = None

    def start(self, project, settings):
        self.started = SimpleNamespace(id="test-job", project=project.resolve(), settings=settings,
                                       state="queued", message="Waiting to start")
        return self.started

    def for_project(self, project):
        return self.started if self.started and self.started.project == project.resolve() else None

    def get(self, job_id):
        return self.started if self.started and self.started.id == job_id else None

    def serialize(self, job):
        return {"id": job.id, "state": job.state, "message": job.message, "phase": job.message,
                "percent": None, "elapsed_seconds": 0, "outputs": []}


if __name__ == "__main__":
    unittest.main()
