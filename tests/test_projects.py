import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trailprint.bambu import BambuEnvironment
from trailprint.blender_bridge import BuildSettings
from trailprint.location import save_location
from trailprint.projects import (
    create_project,
    list_deleted_projects,
    permanently_delete_projects,
    purge_trash,
    read_upload,
    restore_project,
    slugify,
    trash_project,
    update_production_status,
)
from trailprint.web import create_app

ROOT = Path(__file__).resolve().parents[1]
GPX = (ROOT / "tests/fixtures/example-route.gpx").read_bytes()


class ProjectServiceTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("  Mount Tam_Loop! "), "mount-tam-loop")
        with self.assertRaises(ValueError):
            slugify("../")

    def test_creation_is_complete_and_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            prints = Path(temp) / "prints"
            project = create_project(prints, "Test Route", GPX)
            for relative in ("route.gpx", "README.md", "build/.gitkeep",
                             "deliverables/.gitkeep"):
                self.assertTrue((project / relative).exists(), relative)
            self.assertFalse((project / "print.yaml").exists())
            with self.assertRaisesRegex(ValueError, "already exists"):
                create_project(prints, "Test Route", GPX)

    def test_invalid_gpx_leaves_no_project(self):
        with tempfile.TemporaryDirectory() as temp:
            prints = Path(temp) / "prints"
            with self.assertRaisesRegex(ValueError, "invalid GPX"):
                create_project(prints, "Bad", b"not XML")
            self.assertEqual(list(prints.iterdir()), [])

    def test_upload_rejects_wrong_extension_and_size(self):
        with self.assertRaisesRegex(ValueError, r"\.gpx extension"):
            read_upload(io.BytesIO(GPX), "route.txt")
        with self.assertRaisesRegex(ValueError, "10 MB or smaller"):
            read_upload(io.BytesIO(b"x" * 11), "route.gpx", limit=10)

    def test_confirmed_project_is_moved_to_recoverable_trash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = create_project(root / "prints", "Keep My Files", GPX)
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
            project = create_project(root / "prints", "My Route", GPX)
            with self.assertRaisesRegex(ValueError, "exactly"):
                trash_project(root / "prints", root / ".trailprint-trash", "my-route", "My Route")
            self.assertTrue(project.is_dir())

            example = create_project(root / "prints", "Example Trail", GPX)
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

    def test_deleted_projects_can_be_listed_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = create_project(root / "prints", "Restore Me", GPX)
            deleted = trash_project(
                root / "prints", root / ".trailprint-trash", "restore-me", "restore-me",
                now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
            now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

            summaries = list_deleted_projects(root / ".trailprint-trash", now=now)
            restored = restore_project(
                root / "prints", root / ".trailprint-trash", deleted.name, now=now)

            self.assertEqual([(item.slug, item.days_remaining) for item in summaries],
                             [("restore-me", 27)])
            self.assertEqual(restored, project)
            self.assertTrue((restored / "route.gpx").is_file())
            self.assertFalse(deleted.exists())

    def test_restore_rejects_conflict_and_expired_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_project(root / "prints", "Conflict", GPX)
            trash = root / ".trailprint-trash"
            trash.mkdir()
            conflict = trash / "conflict--20260820T120000Z--0123abcd"
            conflict.mkdir()
            now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
            with self.assertRaisesRegex(ValueError, "already exists"):
                restore_project(root / "prints", trash, conflict.name, now=now)
            expired = trash / "expired--20260701T120000Z--0123abcd"
            expired.mkdir()
            with self.assertRaisesRegex(ValueError, "expired"):
                restore_project(root / "prints", trash, expired.name, now=now)

    def test_production_status_follows_validated_funnel_transitions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = create_project(root / "prints", "Production", GPX)
            with self.assertRaisesRegex(ValueError, "3MF"):
                update_production_status(project, "start")
            (project / "deliverables/production.3mf").write_bytes(b"3mf")
            started_at = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
            completed_at = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)

            started = update_production_status(project, "start", now=started_at)
            completed = update_production_status(project, "complete", now=completed_at)
            printing_again = update_production_status(project, "back")
            reset = update_production_status(project, "back")

            self.assertEqual(started["status"], "printing")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(printing_again["status"], "printing")
            self.assertEqual(reset, {})
            self.assertFalse((project / "workflow.json").exists())

    def test_permanent_delete_removes_only_validated_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            trash = Path(temp) / ".trailprint-trash"
            trash.mkdir()
            first = trash / "first--20260820T120000Z--0123abcd"
            second = trash / "second--20260820T120000Z--0123abcd"
            first.mkdir()
            second.mkdir()
            now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

            removed = permanently_delete_projects(
                trash, [first.name, first.name], "DELETE", now=now)

            self.assertEqual([path.name for path in removed], [first.name])
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())

    def test_permanent_delete_validates_every_entry_before_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            trash = Path(temp) / ".trailprint-trash"
            trash.mkdir()
            valid = trash / "valid--20260820T120000Z--0123abcd"
            valid.mkdir()
            now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
            with self.assertRaisesRegex(ValueError, "safely recoverable"):
                permanently_delete_projects(
                    trash, [valid.name, "../outside"], "DELETE", now=now)
            self.assertTrue(valid.exists())
            with self.assertRaisesRegex(ValueError, "Type DELETE"):
                permanently_delete_projects(trash, [valid.name], "delete", now=now)
            self.assertTrue(valid.exists())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prints").mkdir()
        self.build_manager = FakeBuildManager()
        self.bambu_manager = FakeBambuManager()
        self.bambu_environment = BambuEnvironment(
            Path("/Applications/BambuStudio"), self.root / "bambu",
            "Bambu Lab P1S 0.4 nozzle", "0.20mm Standard @BBL X1C",
            ({"number": 1, "name": "Black PLA", "color": "#000000"},
             {"number": 2, "name": "White PLA", "color": "#FFFFFF"},
             {"number": 3, "name": "Red PLA", "color": "#FF0000"},
             {"number": 4, "name": "Green PLA", "color": "#00AA00"}))
        self.environment_patch = patch(
            "trailprint.web.discover_environment", return_value=self.bambu_environment)
        self.environment_patch.start()
        self.app = create_app(self.root, self.build_manager, self.bambu_manager)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.environment_patch.stop()
        self.temp.cleanup()

    def test_dashboard_and_create_redirect(self):
        empty_page = self.client.get("/").data
        self.assertIn(b"Project created", empty_page)
        self.assertIn(b"No projects at this stage", empty_page)
        self.assertIn(b"Deleted projects", empty_page)
        self.assertIn(b'href="/analytics">Analytics</a>', empty_page)
        self.assertNotIn(b"analytics-home-link", empty_page)
        response = self.client.post("/projects", data={
            "name": "Coastal Run", "gpx": (io.BytesIO(GPX), "coast.gpx")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertTrue((self.root / "prints/0001-coastal-run/route.gpx").is_file())
        self.assertEqual(response.headers["Location"], "/projects/0001-coastal-run")
        page = self.client.get(response.headers["Location"])
        self.assertIn(b"Coastal Run", page.data)
        self.assertIn(b'TRAILPRINT_MODEL_FONT_CSS = "bold 1000px Arial, sans-serif"', page.data)

    def test_analytics_page_visualizes_statuses_and_creation_history(self):
        response = self.client.post("/projects", data={
            "name": "Chart Route", "gpx": (io.BytesIO(GPX), "chart.gpx")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)

        page = self.client.get("/analytics")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Project activity", page.data)
        self.assertIn(b"Product line summary", page.data)
        self.assertEqual(page.data.count(b'class="status-count-card"'), 5)
        self.assertNotIn(b"status-bar-track", page.data)
        self.assertIn(b"fridge_magnet_raised_map", page.data)
        self.assertIn(b"Cumulative products by product name", page.data)
        self.assertIn(b'id="created-chart-data"', page.data)
        self.assertIn(b'name="include_tests"', page.data)
        self.assertIn(b"analytics.js", page.data)
        self.assertIn(b"Projects created", page.data)
        self.assertIn(b'href="/analytics"', page.data)

        response = self.client.get("/static/analytics.js")
        self.addCleanup(response.close)
        script = response.data
        self.assertIn(b"createElementNS", script)
        self.assertIn(b"viewBox", script)
        self.assertNotIn(b"Chart.js", script)

    def test_test_project_is_recorded_and_filtered_by_default(self):
        response = self.client.post("/projects", data={
            "name": "Calibration", "is_test": "on",
            "gpx": (io.BytesIO(GPX), "test.gpx")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/analytics")
        self.assertIn(b"0</strong><span>Total projects", page.data)
        self.assertIn(b"1 test project(s) recorded", page.data)
        included = self.client.get("/analytics?include_tests=1")
        self.assertIn(b"1</strong><span>Total projects", included.data)
        project = self.root / "prints/0001-calibration/project.json"
        self.assertTrue(json.loads(project.read_text())["dim_is_test"])

    def test_invalid_upload_has_no_partial_project(self):
        response = self.client.post("/projects", data={
            "name": "Broken", "gpx": (io.BytesIO(b"bad"), "bad.gpx")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.root / "prints/broken").exists())

    def test_build_action_starts_blender_job_with_validated_settings(self):
        create_project(self.root / "prints", "Build Me", GPX)
        response = self.client.post("/projects/build-me/build", data={
            "shape": "CIRCLE", "shape_extra": "OUTER TEXT", "resolution": "6",
            "plate_title": "Volcano Loop", "plate_text_1": "{length}",
            "plate_text_2": "Gain {elevation}", "plate_text_3": "{duration}",
            "plate_text_4": "Hawaii", "plate_text_5": "2026",
            "x_offset": "1.25", "y_offset": "-0.5",
            "city": "Volcano", "state": "Hawaii", "country": "United States"})
        self.assertEqual(response.status_code, 302)
        settings = self.build_manager.started.settings
        self.assertEqual((settings.shape, settings.shape_extra, settings.resolution),
                         ("CIRCLE", "OUTER TEXT", 6))
        self.assertEqual(settings.plate_title, "Volcano Loop")
        self.assertEqual(settings.plate_text_1, "0.41 mi")
        self.assertEqual(settings.plate_text_2, "Gain 105 ft")
        self.assertEqual(settings.plate_text_5, "2026")
        self.assertEqual((settings.x_offset, settings.y_offset), (1.25, -0.5))
        self.assertEqual(settings.max_terrain_relief_mm, 15.0)
        self.assertEqual(settings.plate_recess_depth, 1.5)
        self.assertTrue(settings.dot_magnet_recess)
        self.assertEqual((settings.city, settings.state, settings.country),
                         ("Volcano", "Hawaii", "United States"))
        self.assertTrue((self.root / "prints/build-me/location.json").is_file())

    def test_project_page_surfaces_emgi_defaults(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        save_location(project, "Volcano", "Hawaii", "United States")
        page = self.client.get("/projects/build-me").data
        for expected in (b'name="plate_title" value="Build Me"',
                         b'name="plate_text_1" value="0.41 mi"',
                         b'name="plate_text_2" value=""',
                         b'name="plate_text_3" value=""',
                         b'name="plate_text_4" value=""',
                         b'name="plate_text_5" value=""',
                         b'name="city" type="hidden" value="Volcano"',
                         b'name="state" type="hidden" value="Hawaii"',
                         b'name="country" type="hidden" value="USA"',
                         b'name="x_offset" type="number" value="0.0"',
                         b'name="y_offset" type="number" value="0.0"'):
            self.assertIn(expected, page)
        self.assertIn(b'id="resolution-value">8', page)
        self.assertIn(b'name="resolution" type="range" min="1" max="10" value="8"', page)
        self.assertIn(b'id="text-orientation"', page)
        self.assertIn(b'id="plate-text-settings"', page)
        self.assertIn(b'data-cached="true"', page)
        self.assertNotIn(b"Trail location", page)
        self.assertIn(b'id="use-metric" type="checkbox" name="use_metric"', page)
        self.assertIn(b'name="plate_recess_depth" type="number" value="1.5"', page)
        self.assertIn(b'id="dot-magnet-recess" type="checkbox"', page)
        self.assertIn(b'name="dot_magnet_recess" value="on" checked', page)
        self.assertNotIn(b'name="include_water"', page)
        self.assertNotIn(b'name="include_ocean"', page)
        self.assertNotIn(b'name="fixed_elevation_scale"', page)
        self.assertIn(b"Generated terrain relief capped at 15 mm", page)
        self.assertIn(b"complete downloaded terrain", page)
        self.assertNotIn(b"{length}", page)

    def test_tall_route_is_automatically_limited(self):
        project = create_project(self.root / "prints", "Tall", GPX)
        (project / "route.gpx").write_text('''<gpx><trk><trkseg>
          <trkpt lat="45" lon="-122"><ele>0</ele></trkpt>
          <trkpt lat="45.01" lon="-122.01"><ele>1000</ele></trkpt>
        </trkseg></trk></gpx>''', encoding="utf-8")
        page = self.client.get("/projects/tall").data
        self.assertIn(b"Generated terrain relief capped at 15 mm", page)

        self.client.post("/projects/tall/build", data={
            "shape": "HEXAGON", "shape_extra": "NONE", "resolution": "8",
            "plate_title": "Tall"})

        self.assertEqual(self.build_manager.started.settings.max_terrain_relief_mm, 15.0)

    def test_project_page_surfaces_literal_gpx_trip_date(self):
        project = create_project(self.root / "prints", "Dated Trip", GPX)
        (project / "route.gpx").write_text(
            '<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>2025-07-04T08:30:00Z</time>'
            '</trkpt><trkpt lat="1.1" lon="2.1"/></trkseg></trk></gpx>', encoding="utf-8")
        page = self.client.get("/projects/dated-trip").data
        self.assertIn(b'name="plate_text_2" value="2025.07.04"', page)

    def test_metric_build_resolves_measurement_tokens(self):
        create_project(self.root / "prints", "Metric", GPX)
        self.client.post("/projects/metric/build", data={
            "shape": "HEXAGON", "shape_extra": "OUTER TEXT", "resolution": "8",
            "plate_title": "Metric", "plate_text_1": "{length}",
            "plate_text_2": "{elevation}", "plate_text_3": "{speed}",
            "plate_text_4": "", "plate_text_5": "", "use_metric": "on"})
        settings = self.build_manager.started.settings
        self.assertEqual(settings.unit_system, "METRIC")
        self.assertEqual(settings.plate_text_1, "0.66 km")
        self.assertEqual(settings.plate_text_2, "32 m")
        self.assertEqual(settings.plate_text_3, "0.00 km/h")

    def test_project_script_defaults_outer_text_and_hides_plate_text_for_none(self):
        response = self.client.get("/static/project.js")
        self.addCleanup(response.close)
        script = response.data
        self.assertIn(b"extras.includes('OUTER TEXT') ? 'OUTER TEXT' : 'NONE'", script)
        self.assertIn(b"plateTextSettings.hidden = !hasText", script)
        self.assertIn(b"field.hidden = !usesFive", script)
        self.assertIn(b"plateRecessSetting.hidden = !hasPlate", script)
        self.assertIn(b"dotMagnetSetting.hidden = !hasPlate", script)

    def test_dot_magnet_checkbox_can_disable_recesses(self):
        create_project(self.root / "prints", "No Magnets", GPX)
        self.client.post("/projects/no-magnets/build", data={
            "shape": "HEXAGON", "shape_extra": "OUTER TEXT", "resolution": "8",
            "plate_title": "No Magnets", "dot_magnet_recess": "off"})
        self.assertFalse(self.build_manager.started.settings.dot_magnet_recess)

    def test_dot_magnets_are_suppressed_without_a_plate(self):
        create_project(self.root / "prints", "No Plate Magnets", GPX)
        self.client.post("/projects/no-plate-magnets/build", data={
            "shape": "HEXAGON", "shape_extra": "INNER TEXT", "resolution": "8",
            "plate_title": "No Plate", "dot_magnet_recess": "on"})
        self.assertFalse(self.build_manager.started.settings.dot_magnet_recess)

    def test_recess_is_ignored_for_layout_without_a_plate(self):
        create_project(self.root / "prints", "No Plate", GPX)
        self.client.post("/projects/no-plate/build", data={
            "shape": "HEXAGON", "shape_extra": "INNER TEXT", "resolution": "8",
            "plate_title": "No Plate", "plate_recess_depth": "3"})
        self.assertEqual(self.build_manager.started.settings.plate_recess_depth, 0)

    def test_running_build_replaces_visible_settings_with_embedded_status(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        self.build_manager.started = SimpleNamespace(
            id="running-job", project=project.resolve(), settings=BuildSettings(),
            state="running", message="Generating terrain")
        page = self.client.get("/projects/build-me").data
        self.assertIn(b'class="panel settings-panel building"', page)
        self.assertIn(b'id="build-status"', page)
        self.assertIn(b"Parameters are locked while Blender builds your model.", page)
        self.assertIn(b'id="cancel-build"', page)
        self.assertIn(b'/builds/running-job/cancel', page)
        self.assertIn(b'id="build-form" hidden', page)

    def test_location_endpoint_returns_detected_values(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        detected = {"city": "Volcano", "state": "Hawaii", "country": "United States"}
        with patch("trailprint.web.detect_location", return_value=detected) as lookup:
            response = self.client.post("/projects/build-me/location")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "resolved", **detected})
        lookup.assert_called_once_with(project)

    def test_location_endpoint_failure_is_nonfatal(self):
        create_project(self.root / "prints", "Build Me", GPX)
        with patch("trailprint.web.detect_location", side_effect=OSError("offline")):
            response = self.client.post("/projects/build-me/location")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["status"], "failed")

    def test_preview_page_and_image_are_project_scoped(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        stl = project / "deliverables/build-me.stl"
        stl.write_bytes(b"preview data")
        (project / "build/preview.png").write_bytes(b"png preview")
        customer = project / "deliverables/Customer Route-preview.png"
        customer.write_bytes(b"customer png")
        (project / "build/validation.json").write_text(json.dumps({
            "valid": True,
            "preview": {"valid": True, "customer_filename": customer.name},
        }), encoding="utf-8")
        page = self.client.get("/projects/build-me/preview")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"VALIDATED MODEL PREVIEW", page.data)
        self.assertIn(b"build-me.stl", page.data)
        self.assertIn(b'class="preview-sidebar panel preview-parts"', page.data)
        self.assertIn(b'class="preview-sidebar panel preview-validation"', page.data)
        self.assertNotIn(b"Reset view", page.data)
        self.assertNotIn(b"preview.js", page.data)
        self.assertIn(b"Map and DIY kit setup", page.data)
        self.assertIn(b'name="generate_diy_kit"', page.data)
        self.assertNotIn(b'name="generate_diy_kit" checked', page.data)
        self.assertIn(b"Data credit for packaging and product listing", page.data)
        self.assertIn(b"Map data \xc2\xa9 OpenStreetMap contributors", page.data)
        self.assertIn(b"Export unavailable", page.data)
        self.assertIn(b"Download customer preview", page.data)
        self.assertIn(b"Customer%20Route-preview.png", page.data)
        download = self.client.get(
            "/projects/build-me/files/Customer%20Route-preview.png")
        self.assertEqual(download.data, b"customer png")
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()
        self.assertEqual(
            self.client.get("/projects/build-me/files/not-allowed.txt").status_code,
            404)
        response = self.client.get("/projects/build-me/preview-image")
        self.assertEqual(response.data, b"png preview")
        response.close()
        (project / "build/preview.png").unlink()
        self.assertEqual(self.client.get("/projects/build-me/preview-image").status_code, 404)

    def test_bambu_export_starts_with_terrain_band_assignments(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        for name in ("build-me.stl", "build-me_Trail.stl", "build-me_Plate.stl",
                     "build-me_Text.stl"):
            (project / "deliverables" / name).write_bytes(b"stl")
        (project / "build/preview.png").write_bytes(b"png")
        (project / "build/elevation.json").write_text(json.dumps({
            "source_min_m": 500, "source_max_m": 2500,
            "model_surface_min_z_mm": 5.5, "model_surface_max_z_mm": 25.5,
            "min_thickness_mm": 2, "unit_system": "IMPERIAL",
        }), encoding="utf-8")

        page = self.client.get("/projects/build-me/preview")
        self.assertIn(b"Bambu Lab P1S 0.4 nozzle", page.data)
        self.assertNotIn(b"Export unavailable", page.data)
        response = self.client.post("/projects/build-me/bambu", data={
            "trail_filament": "3", "land_filament": "4",
            "mountain_filament": "1", "summit_filament": "2",
            "plate_filament": "1", "text_filament": "2",
            "grey_start": "4000", "white_start": "7500",
            "generate_diy_kit": "on"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.bambu_manager.started.settings.by_role(),
                         {"trail": 3, "terrain": 4, "plate": 1, "text": 2,
                          "attribution": 1})
        self.assertEqual(self.bambu_manager.started.settings.terrain_filaments(),
                         {"land": 4, "mountain": 1, "summit": 2})
        self.assertTrue(self.bambu_manager.started.settings.generate_diy_kit)
        self.assertEqual(self.bambu_manager.started.settings.diy_case_filament(), 1)

    def test_osm_palette_is_default_and_uses_server_owned_bands(self):
        project = create_project(self.root / "prints", "OSM Map", GPX)
        for name in ("osm-map.stl", "osm-map_Trail.stl", "osm-map_Plate.stl",
                     "osm-map_Text.stl"):
            (project / "deliverables" / name).write_bytes(b"stl")
        (project / "build/preview.png").write_bytes(b"png")
        (project / "build/elevation.json").write_text(json.dumps({
            "source_min_m": 0, "source_max_m": 1000,
            "model_surface_min_z_mm": 2, "model_surface_max_z_mm": 12,
            "min_thickness_mm": 2, "unit_system": "METRIC",
        }), encoding="utf-8")
        bands = [
            {"role": "green", "min_z": 2, "max_z": 6,
             "min_elevation_m": 0, "max_elevation_m": 400},
            {"role": "grey", "min_z": 6, "max_z": 9,
             "min_elevation_m": 400, "max_elevation_m": 700},
            {"role": "white", "min_z": 9, "max_z": 12,
             "min_elevation_m": 700, "max_elevation_m": 1000},
        ]
        (project / "build/terrain-palette.json").write_text(json.dumps({
            "version": 2, "available": True, "source": "OpenStreetMap",
            "attribution": "© OpenStreetMap contributors", "mapped_coverage": .4,
            "confidence": .8, "bands": bands,
        }), encoding="utf-8")

        page = self.client.get("/projects/osm-map/preview").data
        self.assertIn(b'value="osm" checked', page)
        self.assertIn(b"OpenStreetMap-inspired elevation palette", page)
        self.assertIn(b"Mapped evidence covers 40%", page)
        self.assertNotIn(b"One-plate export unavailable", page)
        self.assertIn(b'type="submit" >Create Bambu 3MF</button>', page)
        self.assertNotIn(b"Satellite", page)
        self.assertNotIn(b"Sentinel", page)

        response = self.client.post("/projects/osm-map/bambu", data={
            "terrain_mode": "osm", "trail_filament": "3", "land_filament": "4",
            "mountain_filament": "1", "summit_filament": "2",
            "plate_filament": "1", "text_filament": "2",
            "black_filament": "1", "green_filament": "4",
            "grey_filament": "1", "white_filament": "2",
            "grey_start": "400", "white_start": "800",
        })
        self.assertEqual(response.status_code, 302)
        settings = self.bambu_manager.started.settings
        self.assertEqual(settings.terrain_mode, "osm")
        self.assertEqual(settings.automatic_bands, tuple(bands))
        self.assertEqual(settings.by_role()["terrain"], 4)

    def test_water_build_uses_combined_terrain_object_and_blue_filament(self):
        project = create_project(self.root / "prints", "Wet Route", GPX)
        for name in ("wet-route.stl", "wet-route_Trail.stl", "wet-route_Plate.stl",
                     "wet-route_Text.stl", "wet-route_WATER.stl"):
            (project / "deliverables" / name).write_bytes(b"stl")
        (project / "build/preview.png").write_bytes(b"png")
        (project / "build/elevation.json").write_text(json.dumps({
            "source_min_m": 100, "source_max_m": 200,
            "model_surface_min_z_mm": 2, "model_surface_max_z_mm": 8,
            "min_thickness_mm": 2, "unit_system": "METRIC",
        }), encoding="utf-8")

        page = self.client.get("/projects/wet-route/preview")

        self.assertIn(b"Map and DIY kit setup", page.data)
        self.assertIn(b"Terrain + Water", page.data)
        self.assertNotIn(b"One-plate export unavailable", page.data)
        self.assertIn(b'name="water_filament"', page.data)
        self.assertNotIn(b'name="water_placement"', page.data)

        response = self.client.post("/projects/wet-route/bambu", data={
            "terrain_mode": "manual", "trail_filament": "3",
            "land_filament": "4", "mountain_filament": "1",
            "summit_filament": "2", "plate_filament": "1",
            "text_filament": "2", "water_filament": "4",
            "grey_start": "1200", "white_start": "2300",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.bambu_manager.started.settings.water_filament, 4)

    def test_preview_shows_combined_water_layer_estimate(self):
        project = create_project(self.root / "prints", "Mixed Water", GPX)
        for name in ("mixed-water.stl", "mixed-water_Trail.stl",
                     "mixed-water_Plate.stl", "mixed-water_Text.stl",
                     "mixed-water_WATER.stl"):
            (project / "deliverables" / name).write_bytes(b"stl")
        (project / "deliverables/mixed-water.3mf").write_bytes(b"3mf")
        (project / "build/preview.png").write_bytes(b"png")
        (project / "build/elevation.json").write_text(json.dumps({
            "source_min_m": 100, "source_max_m": 200,
            "model_surface_min_z_mm": 2, "model_surface_max_z_mm": 8,
            "min_thickness_mm": 2, "unit_system": "METRIC",
        }), encoding="utf-8")
        (project / "build/bambu-validation.json").write_text(json.dumps({
            "water_filament": 4,
            "water_layer_estimate": {
                "mixed_filament_layer_count": 13,
                "mixed_filament_layer_ranges": [[23, 35]],
                "nominal_layer_height_mm": 0.2,
                "manual_swaps_max": 26,
            },
        }), encoding="utf-8")

        page = self.client.get("/projects/mixed-water/preview").data

        self.assertIn(b"Map and DIY kit setup", page)
        self.assertIn(b"Terrain + Water", page)
        self.assertNotIn(b"One-plate export unavailable", page)
        self.assertIn(b"Estimated mixed-color water layers: 13", page)
        self.assertNotIn(b'name="water_placement"', page)

    def test_bambu_script_opens_output_folder_after_success(self):
        response = self.client.get("/static/bambu.js")
        self.addCleanup(response.close)
        script = response.data
        self.assertIn(b"await fetch(bambuStatus.dataset.openFolderUrl", script)
        self.assertIn(b"job.state === 'success'", script)
        self.assertIn(b"location.href = bambuStatus.dataset.homeUrl", script)

    def test_deleted_projects_page_restores_project(self):
        project = create_project(self.root / "prints", "Restore Me", GPX)
        self.client.post("/projects/restore-me/delete", data={"confirmation": "restore-me"})
        dashboard = self.client.get("/").data
        self.assertIn(b"Deleted projects (1)", dashboard)
        page = self.client.get("/deleted-projects")
        self.assertIn(b"Restore Me", page.data)
        deleted = next((self.root / ".trailprint-trash").iterdir())

        response = self.client.post(
            f"/deleted-projects/{deleted.name}/restore", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Restored &#39;restore-me&#39;", response.data)
        self.assertTrue(project.is_dir())

    def test_deleted_projects_page_bulk_permanently_deletes_selection(self):
        for name in ("Delete One", "Delete Two"):
            create_project(self.root / "prints", name, GPX)
            slug = name.lower().replace(" ", "-")
            self.client.post(f"/projects/{slug}/delete", data={"confirmation": slug})
        deleted = sorted((self.root / ".trailprint-trash").iterdir())
        page = self.client.get("/deleted-projects").data
        self.assertIn(b'id="select-all-deleted"', page)
        self.assertIn(b"Delete permanently", page)

        response = self.client.post("/deleted-projects/permanent-delete", data={
            "entries": [deleted[0].name, deleted[1].name], "confirmation": "DELETE",
        }, follow_redirects=True)

        self.assertIn(b"Permanently deleted 2 project(s)", response.data)
        self.assertEqual(list((self.root / ".trailprint-trash").iterdir()), [])

    def test_bulk_permanent_delete_requires_confirmation(self):
        create_project(self.root / "prints", "Keep Me", GPX)
        self.client.post("/projects/keep-me/delete", data={"confirmation": "keep-me"})
        deleted = next((self.root / ".trailprint-trash").iterdir())

        response = self.client.post("/deleted-projects/permanent-delete", data={
            "entries": deleted.name, "confirmation": "delete",
        }, follow_redirects=True)

        self.assertIn(b"Type DELETE exactly", response.data)
        self.assertTrue(deleted.is_dir())

    def test_dashboard_production_controls_move_project_through_funnel(self):
        project = create_project(self.root / "prints", "Print Me", GPX)
        (project / "deliverables/print-me.3mf").write_bytes(b"3mf")
        page = self.client.get("/").data
        self.assertIn(b"Start printing", page)

        self.client.post("/projects/print-me/production-status", data={"action": "start"})
        page = self.client.get("/").data
        self.assertIn(b"Mark completed", page)
        self.client.post("/projects/print-me/production-status", data={"action": "complete"})
        page = self.client.get("/").data
        self.assertIn(b"Completed ", page)
        self.assertIn(b"Move back", page)

    def test_dashboard_uses_live_count_tabs_and_folder_controls(self):
        create_project(self.root / "prints", "Created", GPX)
        built = create_project(self.root / "prints", "Built", GPX)
        (built / "deliverables/built.stl").write_bytes(b"stl")

        page = self.client.get("/").data

        self.assertEqual(page.count(b'class="funnel-tab"'), 5)
        self.assertIn(b'data-stage="created"', page)
        self.assertIn(b'data-stage="stl"', page)
        self.assertIn(b'Project created <span>1</span>', page)
        self.assertIn(b'STL generated <span>1</span>', page)
        self.assertIn(b"dashboard.js", page)
        self.assertEqual(page.count(b"Project folder"), 2)

    def test_open_project_folder_uses_resolved_project_path(self):
        project = create_project(self.root / "prints", "Open Me", GPX)
        with patch("trailprint.web.subprocess.Popen") as opener:
            response = self.client.post("/projects/open-me/open-project-folder")
        self.assertEqual(response.status_code, 302)
        opener.assert_called_once_with(["open", str(project)], start_new_session=True)

    def test_cancel_build_endpoint_uses_build_manager(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        self.build_manager.started = SimpleNamespace(
            id="cancel-me", project=project.resolve(), settings=BuildSettings(),
            state="running", message="Generating terrain")

        response = self.client.post("/builds/cancel-me/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.build_manager.started.state, "cancelling")

    def test_open_output_folder_uses_resolved_deliverables_path(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        with patch("trailprint.web.subprocess.Popen") as opener:
            response = self.client.post("/projects/build-me/open-output-folder")
        self.assertEqual(response.status_code, 302)
        opener.assert_called_once_with(
            ["open", str(project / "deliverables")], start_new_session=True)

    def test_open_output_folder_failure_is_nonfatal(self):
        project = create_project(self.root / "prints", "Build Me", GPX)
        (project / "deliverables/build-me.stl").write_bytes(b"stl")
        (project / "build/preview.png").write_bytes(b"png")
        with patch("trailprint.web.subprocess.Popen", side_effect=OSError("Finder unavailable")):
            response = self.client.post(
                "/projects/build-me/open-output-folder", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Could not open the output folder", response.data)

    def test_build_rejects_incompatible_extra(self):
        create_project(self.root / "prints", "Build Me", GPX)
        response = self.client.post("/projects/build-me/build", data={
            "shape": "SQUARE", "shape_extra": "OUTER TEXT", "resolution": "4"},
            follow_redirects=True)
        self.assertIn(b"unavailable for Rectangle", response.data)
        self.assertIsNone(self.build_manager.started)

    def test_build_rejects_title_that_does_not_fit(self):
        create_project(self.root / "prints", "Build Me", GPX)
        response = self.client.post("/projects/build-me/build", data={
            "shape": "HEXAGON", "shape_extra": "OUTER TEXT", "resolution": "8",
            "plate_title": "W" * 15}, follow_redirects=True)
        self.assertIn(b"Shorten the title before building", response.data)
        self.assertIsNone(self.build_manager.started)

    def test_build_accepts_grindstone_title(self):
        create_project(self.root / "prints", "Build Me", GPX)
        response = self.client.post("/projects/build-me/build", data={
            "shape": "HEXAGON", "shape_extra": "OUTER TEXT", "resolution": "4",
            "plate_title": "UTMB Grindstone"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.build_manager.started.settings.plate_title, "UTMB Grindstone")

    def test_dashboard_shows_delete_control_except_for_example(self):
        create_project(self.root / "prints", "Example Trail", GPX)
        create_project(self.root / "prints", "Coastal Run", GPX)

        page = self.client.get("/").data

        self.assertIn(b'data-delete-slug="coastal-run"', page)
        self.assertNotIn(b'data-delete-slug="example-trail"', page)

    def test_delete_moves_project_after_exact_confirmation(self):
        project = create_project(self.root / "prints", "Coastal Run", GPX)

        response = self.client.post(
            "/projects/coastal-run/delete", data={"confirmation": "coastal-run"},
            follow_redirects=True)

        self.assertIn(b"Moved &#39;coastal-run&#39; to local trash", response.data)
        self.assertFalse(project.exists())
        trashed = list((self.root / ".trailprint-trash").iterdir())
        self.assertEqual(len(trashed), 1)
        self.assertTrue((trashed[0] / "route.gpx").is_file())

    def test_delete_rejects_wrong_confirmation_and_active_build(self):
        project = create_project(self.root / "prints", "Coastal Run", GPX)
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

    def cancel(self, job_id):
        job = self.get(job_id)
        if job is None:
            raise ValueError("Build job was not found.")
        job.state, job.message = "cancelling", "Stopping Blender safely…"
        return job

    def serialize(self, job):
        return {"id": job.id, "state": job.state, "message": job.message, "phase": job.message,
                "percent": None, "elapsed_seconds": 0, "outputs": [],
                "can_cancel": job.state in {"queued", "running"}}


class FakeBambuManager(FakeBuildManager):
    def start(self, project, settings):
        self.started = SimpleNamespace(id="bambu-job", project=project.resolve(), settings=settings,
                                       state="queued", message="Waiting to start", output="")
        return self.started


if __name__ == "__main__":
    unittest.main()
