import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import trimesh

from trailprint.blender_bridge import (BuildCancelled, BuildSettings, _failure_detail,
                                       _run_command, run_blender_build)
from trailprint.blender_worker import (classify_osm_samples, configure_water,
                                       fetch_lava_osm,
                                       fixed_elevation_multiplier, lava_osm_query, resolve_text,
                                       with_plate_clearance)
from trailprint.gpx import read_gpx_trip_date


class SettingsTests(unittest.TestCase):
    def test_fixed_elevation_target_compensates_for_latitude(self):
        self.assertAlmostEqual(fixed_elevation_multiplier(15, 0), 1.5)
        self.assertAlmostEqual(fixed_elevation_multiplier(15, 60), 0.75)

    def test_failure_detail_prefers_exception_over_stdout(self):
        result = subprocess.CompletedProcess(
            [], 1, "First name: Square Stand\n",
            "Traceback (most recent call last):\n  File x\nAttributeError: missing function\n",
        )
        self.assertEqual(_failure_detail(result), "AttributeError: missing function")

    def test_cancelled_build_preserves_previous_outputs_and_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "build").mkdir()
            (project / "route.gpx").write_text("<gpx/>", encoding="utf-8")
            previous = {
                project / "deliverables/previous.stl": b"previous stl",
                project / "deliverables/keep.3mf": b"previous 3mf",
                project / "build/preview.png": b"previous preview",
                project / "build/validation.json": b'{"valid": true}',
                project / "build/elevation.json": b'{"source_min_m": 1}',
            }
            for path, data in previous.items():
                path.write_bytes(data)
            blender = Path(temp) / "blender"; blender.touch()
            cancelled = threading.Event(); cancelled.set()

            with self.assertRaises(BuildCancelled):
                run_blender_build(project, BuildSettings(), blender=blender,
                                  cancel_event=cancelled)

            self.assertEqual({path: path.read_bytes() for path in previous}, previous)
            self.assertEqual(list((project / "build").glob("trailprint3d-*")), [])

    def test_running_process_group_honors_cancellation(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(BuildCancelled):
                _run_command(["/bin/sh", "-c", "sleep 20"], 30, cancelled)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 4)

    def test_defaults_and_compatibility(self):
        self.assertEqual(BuildSettings.validated(),
                         BuildSettings("HEXAGON", "NONE", 8, plate_recess_depth=0))
        self.assertEqual(BuildSettings.validated("circle", "outer text", "8"),
                         BuildSettings("CIRCLE", "OUTER TEXT", 8))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            BuildSettings.validated("SQUARE", "OUTER TEXT", 4)
        with self.assertRaisesRegex(ValueError, "1 to 10"):
            BuildSettings.validated("HEXAGON", "NONE", 11)
        custom = BuildSettings.validated(
            "hexagon", "inner text", 5, "Summit", "12 km", "900 m", "2 hr", "", "",
            "1.5", "-2", True)
        self.assertEqual(custom.plate_title, "Summit")
        self.assertEqual((custom.x_offset, custom.y_offset), (1.5, -2.0))
        self.assertTrue(custom.fixed_elevation_scale)
        with self.assertRaisesRegex(ValueError, "must be numbers"):
            BuildSettings.validated(x_offset="left")
        recessed = BuildSettings.validated("hexagon", "outer text", plate_recess_depth="1.5")
        self.assertEqual(recessed.plate_recess_depth, 1.5)
        with self.assertRaisesRegex(ValueError, "0 to 4"):
            BuildSettings.validated("hexagon", "outer text", plate_recess_depth=4.1)

    def test_plate_clearance_override_is_scoped_to_the_cutter(self):
        class Props:
            tolerance = 0.2
        props = Props()
        seen = []

        def cutter(_plate, _terrain):
            seen.append(props.tolerance)
            return "cut"

        wrapped = with_plate_clearance(cutter, props, 0)
        self.assertEqual(wrapped(object(), object()), "cut")
        self.assertEqual(seen, [0.0])
        self.assertEqual(props.tolerance, 0.2)

    def test_unified_water_configures_all_large_water_sources(self):
        class Props:
            pass

        enabled = Props()
        configure_water(enabled, True, 25)
        self.assertTrue(enabled.col_wPondsActive)
        self.assertTrue(enabled.col_wBigRiversActive)
        self.assertTrue(enabled.el_oActive)
        self.assertFalse(enabled.col_wSmallRiversActive)
        self.assertEqual(enabled.col_wArea, 25.0)
        self.assertEqual(enabled.elementMode, "SINGLECOLORMODE_REMESH")

        disabled = Props()
        configure_water(disabled, False, 25)
        self.assertFalse(disabled.col_wPondsActive)
        self.assertFalse(disabled.col_wBigRiversActive)
        self.assertFalse(disabled.el_oActive)
        self.assertFalse(disabled.col_wSmallRiversActive)
        self.assertEqual(disabled.elementMode, "PAINT")

    def test_osm_evidence_priority_and_unmapped_green_default(self):
        class Shape:
            def __init__(self, points):
                self.points = points

            def covers(self, point):
                return point in self.points

        polygons = {
            "green": Shape({(1, 1)}), "grey": Shape({(1, 1), (2, 2)}),
            "black": Shape({(2, 2), (3, 3)}), "white": Shape({(3, 3)}),
        }
        samples = [[1, 1, 100], [2, 2, 200], [3, 3, 300], [4, 4, 400]]
        classified, counts = classify_osm_samples(samples, polygons,
                                                  lambda lon, lat: (lon, lat))
        self.assertEqual([item[1] for item in classified],
                         ["grey", "black", "white", "green"])
        self.assertEqual([item[2] for item in classified],
                         [True, True, True, False])
        self.assertEqual(counts, {"grey": 1, "black": 1, "white": 1})

    def test_lava_query_requests_explicit_lava_tags(self):
        query = lava_osm_query((19.0, -156.0, 20.0, -155.0))
        self.assertIn('"geological"~"^(volcanic_lava_field|volcanic_lava_flow)$"',
                      query)
        self.assertIn('"surface"="lava"', query)
        self.assertIn('"natural"="lava"', query)
        self.assertIn("[bbox:19.0,-156.0,20.0,-155.0]", query)

    def test_lava_fetch_requires_successful_overpass_response(self):
        class Osm:
            @staticmethod
            def _overpass_request(*_args, **_kwargs):
                return None

        with self.assertRaisesRegex(RuntimeError, "lava request did not complete"):
            fetch_lava_osm(Osm, (19, -156, 20, -155))

        calls = []

        class SuccessfulOsm:
            @staticmethod
            def _overpass_request(*args, **kwargs):
                calls.append((args, kwargs))
                return {"elements": []}

        self.assertEqual(fetch_lava_osm(
            SuccessfulOsm, (19, -156, 20, -155)), {"elements": []})
        self.assertEqual(calls[0][1]["method"], "POST")
        self.assertEqual(calls[0][1]["timeout"], 120)

    def test_success_replaces_only_prior_stls(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").write_text(
                '<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>2022-09-21T20:23:23-06:00</time>'
                '</trkpt></trkseg></trk></gpx>', encoding="utf-8")
            (project / "deliverables/old.stl").write_text("old", encoding="ascii")
            (project / "deliverables/keep.3mf").write_text("keep", encoding="ascii")
            blender = Path(temp) / "blender"
            blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                if Path(command[-3]).name == "preview_worker.py":
                    Path(config["output_path"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
                    return subprocess.CompletedProcess(command, 0, "preview ok", "")
                self.assertEqual(config["plate_title"], "Project")
                self.assertEqual(config["plate_text_2"], "{date}")
                self.assertEqual(config["plate_text_3"], "{duration}")
                self.assertEqual(config["trip_date"], "2022.09.21")
                self.assertEqual(config["x_offset"], 0.0)
                self.assertFalse(config["fixed_elevation_scale"])
                self.assertEqual(config["fixed_elevation_target_mm"], 15.0)
                self.assertAlmostEqual(config["center_latitude"], 0.0)
                self.assertEqual(config["plate_recess_depth"], 0.0)
                self.assertEqual(config["plate_recess_clearance"], 0.0)
                self.assertTrue(config["include_water"])
                self.assertEqual(config["water_min_area_mm2"], 25.0)
                terrain = trimesh.creation.box(extents=[100, 80, 5])
                trail = trimesh.creation.box(extents=[60, 3, 2]).subdivide()
                terrain.export(Path(config["export_path"], "project.stl"))
                trail.export(Path(config["export_path"], "project_Trail.stl"))
                Path(config["export_path"], "terrain-elevation.json").write_text(json.dumps({
                    "source_min_m": 100, "source_max_m": 200,
                    "min_thickness_mm": 2, "unit_system": "IMPERIAL",
                }), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                outputs = run_blender_build(
                    project, BuildSettings(),
                    blender=blender)
            self.assertEqual({path.name for path in outputs}, {"project.stl", "project_Trail.stl"})
            self.assertFalse((project / "deliverables/old.stl").exists())
            self.assertTrue((project / "deliverables/keep.3mf").exists())
            self.assertGreater((project / "build/preview.png").stat().st_size, 1000)
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertTrue(report["preview"]["valid"])
            elevation = json.loads((project / "build/elevation.json").read_text(encoding="utf-8"))
            self.assertEqual((elevation["source_min_m"], elevation["source_max_m"]), (100, 200))
            self.assertEqual(elevation["unit_system"], "IMPERIAL")

    def test_trip_date_and_location_tokens_are_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            route = Path(temp) / "route.gpx"
            route.write_text(
                '<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>bad</time></trkpt>'
                '<trkpt lat="2" lon="3"><time>2024-01-03T08:00:00Z</time></trkpt>'
                '</trkseg></trk></gpx>', encoding="utf-8")
            self.assertEqual(read_gpx_trip_date(route), "2024.01.03")
            config = {"trip_date": "2024.01.03", "city": "Volcano", "state": "Hawaii",
                      "country": "United States"}
            self.assertEqual(resolve_text("{date} · {city}, {state}", config),
                             "2024.01.03 · Volcano, Hawaii")
            route.write_text("<gpx/>", encoding="utf-8")
            self.assertEqual(read_gpx_trip_date(route), "")

    def test_fragmented_trail_retries_without_shape_extras(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").write_text("<gpx/>", encoding="utf-8")
            blender = Path(temp) / "blender"
            blender.touch()
            attempts = []

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                if Path(command[-3]).name == "preview_worker.py":
                    self.assertEqual({Path(item["path"]).name for item in config["stl_files"]},
                                     {"project.stl", "project_Trail.stl", "project_Plate.stl"})
                    Path(config["output_path"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
                    return subprocess.CompletedProcess(command, 0, "preview ok", "")
                attempts.append(config["shape_extra"])
                output = Path(config["export_path"])
                terrain = trimesh.creation.box(extents=[100, 80, 5])
                if config["shape_extra"] != "NONE":
                    terrain.apply_translation([0, 0, 5])
                terrain.export(output / "project.stl")
                if config["shape_extra"] == "NONE":
                    trail = trimesh.creation.box(extents=[60, 3, 2]).subdivide()
                else:
                    left = trimesh.creation.box(extents=[20, 3, 2])
                    right = left.copy(); right.apply_translation([40, 0, 0])
                    trail = trimesh.util.concatenate([left, right])
                    trimesh.creation.box(extents=[110, 90, 2]).export(output / "project_Plate.stl")
                trail.export(output / "project_Trail.stl")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                outputs = run_blender_build(
                    project, BuildSettings(shape_extra="OUTER TEXT"), blender=blender)

            self.assertEqual(attempts, ["OUTER TEXT", "NONE"])
            self.assertEqual({path.name for path in outputs},
                             {"project.stl", "project_Trail.stl", "project_Plate.stl"})
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertTrue(report["fallback_used"])
            self.assertTrue(report["valid"])
            self.assertTrue(report["preview"]["valid"])
            self.assertEqual(report["fallback_alignment"]["translation_mm"], [0.0, 0.0, 5.0])
            terrain = trimesh.load(project / "deliverables/project.stl", force="mesh")
            trail = trimesh.load(project / "deliverables/project_Trail.stl", force="mesh")
            self.assertAlmostEqual(terrain.bounds[0][2], 2.5)
            self.assertAlmostEqual(trail.bounds[0][2], 4.0)

    def test_preview_failure_keeps_new_stls_and_removes_stale_image(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "build").mkdir()
            (project / "build/preview.png").write_bytes(b"old preview")
            (project / "route.gpx").write_text("<gpx/>", encoding="utf-8")
            blender = Path(temp) / "blender"; blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                if Path(command[-3]).name == "preview_worker.py":
                    return subprocess.CompletedProcess(command, 1, "", "render failed")
                output = Path(config["export_path"])
                trimesh.creation.box(extents=[100, 80, 5]).export(output / "project.stl")
                trimesh.creation.box(extents=[60, 3, 2]).subdivide().export(
                    output / "project_Trail.stl")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                outputs = run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual(len(outputs), 2)
            self.assertFalse((project / "build/preview.png").exists())
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertFalse(report["preview"]["valid"])
            self.assertIn("render failed", report["preview"]["error"])

    def test_failed_validation_preserves_prior_deliverables(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").write_text("<gpx/>", encoding="utf-8")
            previous = project / "deliverables/previous.stl"
            previous.write_text("old", encoding="ascii")
            blender = Path(temp) / "blender"; blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                output = Path(config["export_path"])
                trimesh.creation.box(extents=[100, 80, 5]).export(output / "project.stl")
                first = trimesh.creation.box(extents=[20, 3, 2])
                second = first.copy(); second.apply_translation([40, 0, 0])
                trimesh.util.concatenate([first, second]).export(output / "project_Trail.stl")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(ValueError, "fragmented"):
                    run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual(previous.read_text(encoding="ascii"), "old")
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])

    def test_failure_preserves_prior_stls(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").touch()
            old = project / "deliverables/old.stl"
            old.write_text("old", encoding="ascii")
            blender = Path(temp) / "blender"
            blender.touch()
            failed = subprocess.CompletedProcess([], 1, "", "generation failed")
            with patch("trailprint.blender_bridge.subprocess.run", return_value=failed):
                with self.assertRaisesRegex(ValueError, "generation failed"):
                    run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual(old.read_text(encoding="ascii"), "old")


if __name__ == "__main__":
    unittest.main()
