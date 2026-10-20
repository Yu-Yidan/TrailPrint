import json
import math
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import trimesh

from trailprint.blender_bridge import (
    BuildCancelled,
    BuildSettings,
    _failure_detail,
    _run_command,
    _validate_terrain_relief,
    run_blender_build,
)
from trailprint.blender_worker import (
    ATTRIBUTION_FONT_PATH,
    ATTRIBUTION_LINE_GAP_MM,
    ATTRIBUTION_TEXT_HEIGHT_MM,
    COPYRIGHT_MARK_DIAMETER_MM,
    COPYRIGHT_MIN_FEATURE_MM,
    attribution_text_scale,
    classify_osm_samples,
    configure_model_font,
    configure_plate_text,
    configure_water,
    copyright_mark_geometry,
    dot_magnet_backing_extension,
    dot_magnet_centers,
    fetch_lava_osm,
    lava_osm_query,
    terrain_relief_scaling,
    with_plate_clearance,
)
from trailprint.fonts import MODEL_FONT_PATH
from trailprint.gpx import read_gpx_trip_date


class SettingsTests(unittest.TestCase):
    def test_model_font_overrides_addon_default_and_rejects_missing_font(self):
        props = SimpleNamespace(textFont="Arial Black.ttf")
        configure_model_font(props)
        self.assertEqual(props.textFont, str(MODEL_FONT_PATH))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "Model font was not found"):
                configure_model_font(props, Path(temp) / "missing.ttf")
        self.assertEqual(props.textFont, str(MODEL_FONT_PATH))

    def test_attribution_uses_one_edge_text_reference_scale(self):
        self.assertAlmostEqual(attribution_text_scale(0.5), 9.0)
        self.assertTrue(ATTRIBUTION_FONT_PATH.endswith("Arial Bold.ttf"))
        self.assertEqual(ATTRIBUTION_LINE_GAP_MM, 1.2)
        with self.assertRaisesRegex(ValueError, "reference height"):
            attribution_text_scale(0)

    def test_copyright_mark_is_printable_one_layer_geometry(self):
        vertices, faces = copyright_mark_geometry()
        mark = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
        self.assertTrue(mark.is_watertight)
        self.assertGreater(mark.volume, 0)
        self.assertEqual(tuple(mark.extents),
                         (COPYRIGHT_MARK_DIAMETER_MM,
                          COPYRIGHT_MARK_DIAMETER_MM, 0.2))
        self.assertEqual(ATTRIBUTION_TEXT_HEIGHT_MM, 4.5)
        self.assertGreaterEqual(COPYRIGHT_MIN_FEATURE_MM, 0.6)

    def test_dem_relief_scaling_caps_actual_height_without_stretching_flat_maps(self):
        flat = terrain_relief_scaling([1000, 1100], [1, 1], 40, 15)
        self.assertFalse(flat["cap_applied"])
        self.assertAlmostEqual(flat["final_model_relief_mm"], 4)

        tall = terrain_relief_scaling(
            [1010.25, 1219.0, 1368.5], [2.10, 2.11, 2.12], 33.96, 15)
        self.assertTrue(tall["cap_applied"])
        self.assertGreater(tall["uncapped_model_relief_mm"], 15)
        self.assertAlmostEqual(tall["final_model_relief_mm"], 15)
        self.assertGreater(tall["elevation_multiplier"], 0)

    def test_final_terrain_relief_guard_uses_exported_surface_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            (stage / "terrain-elevation.json").write_text(json.dumps({
                "source_min_m": 100, "source_max_m": 500,
                "min_thickness_mm": 2, "unit_system": "IMPERIAL",
                "uncapped_model_relief_mm": 24, "final_model_relief_mm": 15,
                "max_terrain_relief_mm": 15, "cap_applied": True,
                "elevation_multiplier": 0.625,
            }), encoding="utf-8")
            validation = {"files": [{
                "role": "terrain", "valid": True,
                "bounds_mm": [[-50, -40, 0], [50, 40, 17]],
            }]}

            result = _validate_terrain_relief(stage, validation, 15)
            self.assertEqual(result["actual_model_relief_mm"], 15)
            self.assertTrue(result["cap_applied"])

            validation["files"][0]["bounds_mm"][1][2] = 17.06
            with self.assertRaisesRegex(ValueError, "maximum is 15.00 mm"):
                _validate_terrain_relief(stage, validation, 15)

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
            (project / "route.gpx").write_text('<gpx><trk><trkseg><trkpt lat="1" lon="2"/><trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>', encoding="utf-8")
            previous = {
                project / "deliverables/previous.stl": b"previous stl",
                project / "deliverables/keep.3mf": b"previous 3mf",
                project / "deliverables/Previous-preview.png": b"previous customer preview",
                project / "build/preview.png": b"previous preview",
                project / "build/validation.json": (
                    b'{"valid": true, "preview": '
                    b'{"customer_filename": "Previous-preview.png"}}'),
                project / "build/elevation.json": b'{"source_min_m": 1}',
            }
            for path, data in previous.items():
                path.write_bytes(data)
            blender = Path(temp) / "blender"
            blender.touch()
            cancelled = threading.Event()
            cancelled.set()

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
        defaults = BuildSettings.validated()
        self.assertEqual(defaults,
                         BuildSettings("HEXAGON", "NONE", 8, plate_recess_depth=0,
                                       dot_magnet_recess=False))
        self.assertEqual((defaults.plate_text_4, defaults.plate_text_5), ("", ""))
        self.assertEqual(BuildSettings.validated("circle", "outer text", "8"),
                         BuildSettings("CIRCLE", "OUTER TEXT", 8))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            BuildSettings.validated("SQUARE", "OUTER TEXT", 4)
        with self.assertRaisesRegex(ValueError, "1 to 10"):
            BuildSettings.validated("HEXAGON", "NONE", 11)
        custom = BuildSettings.validated(
            "hexagon", "inner text", 5, "Summit", "12 km", "900 m", "2 hr", "", "",
            "1.5", "-2", max_terrain_relief_mm=12.5)
        self.assertEqual(custom.plate_title, "Summit")
        self.assertEqual((custom.x_offset, custom.y_offset), (1.5, -2.0))
        self.assertEqual(custom.max_terrain_relief_mm, 12.5)
        with self.assertRaisesRegex(ValueError, "must be numbers"):
            BuildSettings.validated(x_offset="left")
        recessed = BuildSettings.validated("hexagon", "outer text", plate_recess_depth="1.5")
        self.assertEqual(recessed.plate_recess_depth, 1.5)
        with self.assertRaisesRegex(ValueError, "0 to 4"):
            BuildSettings.validated("hexagon", "outer text", plate_recess_depth=4.1)

    def test_dot_magnet_layout_is_centered_and_equilateral(self):
        centers = dot_magnet_centers()
        sides = [math.dist(centers[index], centers[(index + 1) % 3])
                 for index in range(3)]
        centroid = tuple(sum(point[axis] for point in centers) / 3
                         for axis in (0, 1))
        self.assertTrue(all(abs(side - 36 * math.sqrt(3)) < 1e-9
                            for side in sides))
        self.assertEqual(centroid, (0.0, 0.0))
        self.assertEqual(centers[0], (0.0, 36.0))

    def test_dot_magnet_backing_preserves_floor_for_every_recess_depth(self):
        self.assertEqual(dot_magnet_backing_extension(5, 0), 0)
        self.assertAlmostEqual(dot_magnet_backing_extension(5, 1.5), 0.3)
        self.assertAlmostEqual(dot_magnet_backing_extension(5, 4), 2.8)

    def test_dot_magnets_can_be_disabled_and_require_a_plate(self):
        self.assertFalse(BuildSettings.validated(
            "hexagon", "outer text", dot_magnet_recess=False).dot_magnet_recess)
        self.assertTrue(BuildSettings.validated(
            "octagon", "outer text").dot_magnet_recess)
        self.assertFalse(BuildSettings.validated(
            "hexagon", "inner text").dot_magnet_recess)

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
            (project / "build").mkdir()
            (project / "route.gpx").write_text(
                '<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>2022-09-21T20:23:23-06:00</time>'
                '</trkpt><trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>', encoding="utf-8")
            (project / "deliverables/old.stl").write_text("old", encoding="ascii")
            (project / "deliverables/keep.3mf").write_text("keep", encoding="ascii")
            old_customer = project / "deliverables/Old Title-preview.png"
            old_customer.write_bytes(b"old customer preview")
            (project / "build/validation.json").write_text(json.dumps({
                "valid": True,
                "preview": {"valid": True, "customer_filename": old_customer.name},
            }), encoding="utf-8")
            blender = Path(temp) / "blender"
            blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                if Path(command[-3]).name == "preview_worker.py":
                    Path(config["output_path"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1200)
                    return subprocess.CompletedProcess(command, 0, "preview ok", "")
                self.assertEqual(config["plate_title"], "")
                self.assertEqual(Path(config["model_font_path"]).name, "Arial-Bold-print.ttf")
                self.assertTrue(Path(config["model_font_path"]).is_file())
                self.assertEqual(config["plate_text_2"], "")
                self.assertEqual(config["plate_text_3"], "")
                self.assertNotIn("trip_date", config)
                self.assertEqual(config["x_offset"], 0.0)
                self.assertEqual(config["max_terrain_relief_mm"], 15.0)
                self.assertEqual(config["plate_recess_depth"], 0.0)
                self.assertEqual(config["plate_recess_clearance"], 0.0)
                self.assertEqual(config["trail_clearance_mm"], 0.1)
                self.assertNotIn("include_water", config)
                self.assertEqual(config["water_min_area_mm2"], 25.0)
                terrain = trimesh.creation.box(extents=[100, 80, 5])
                trail = trimesh.creation.box(extents=[60, 3, 2]).subdivide()
                terrain.export(Path(config["export_path"], "project.stl"))
                trail.apply_translation([0, 0, terrain.bounds[1, 2] - trail.bounds[0, 2]])
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
            self.assertFalse(old_customer.exists())
            self.assertGreater((project / "build/preview.png").stat().st_size, 1000)
            customer_preview = project / "deliverables/Project-preview.png"
            self.assertEqual(customer_preview.read_bytes(),
                             (project / "build/preview.png").read_bytes())
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertTrue(report["preview"]["valid"])
            self.assertEqual(report["preview"]["customer_filename"],
                             "Project-preview.png")
            self.assertEqual(report["trail_clearance_mm"], 0.1)
            elevation = json.loads((project / "build/elevation.json").read_text(encoding="utf-8"))
            self.assertEqual((elevation["source_min_m"], elevation["source_max_m"]), (100, 200))
            self.assertEqual(elevation["unit_system"], "IMPERIAL")

    def test_gpx_date_remains_readable_but_plate_tokens_are_literal(self):
        with tempfile.TemporaryDirectory() as temp:
            route = Path(temp) / "route.gpx"
            route.write_text(
                '<gpx><trk><trkseg><trkpt lat="1" lon="2"><time>bad</time></trkpt>'
                '<trkpt lat="2" lon="3"><time>2024-01-03T08:00:00Z</time></trkpt>'
                '</trkseg></trk></gpx>', encoding="utf-8")
            self.assertEqual(read_gpx_trip_date(route), "2024.01.03")
            config = {"trip_date": "2024.01.03", "city": "Volcano", "state": "Hawaii",
                      "country": "United States"}
            config.update(plate_title="{name}", **{f"plate_text_{i}": token for i, token in
                          enumerate(("{length}", "{date}", "{duration}", "{city}", ""), 1)})
            props = type("Props", (), {})()
            configure_plate_text(props, config)
            self.assertEqual(props.titlefield, "{name}")
            self.assertEqual(props.textfield1, "{length}")
            self.assertEqual(props.textfield2, "{date}")
            self.assertEqual(props.textfield3, "{duration}")
            self.assertEqual(props.textfield4, "{city}")
            self.assertEqual(props.textfield5, "")
            route.write_text("<gpx/>", encoding="utf-8")
            self.assertEqual(read_gpx_trip_date(route), "")

    def test_fragmented_trail_retries_without_shape_extras(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").write_text('<gpx><trk><trkseg><trkpt lat="1" lon="2"/><trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>', encoding="utf-8")
            blender = Path(temp) / "blender"
            blender.touch()
            attempts = []

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                if Path(command[-3]).name == "preview_worker.py":
                    self.assertEqual({Path(item["path"]).name for item in config["stl_files"]},
                                     {"project.stl", "project_Trail.stl",
                                      "project_Plate.stl"})
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
                    right = left.copy()
                    right.apply_translation([40, 0, 0])
                    trail = trimesh.util.concatenate([left, right])
                    trimesh.creation.box(extents=[110, 90, 2]).export(output / "project_Plate.stl")
                trail.apply_translation([0, 0, terrain.bounds[1, 2] - trail.bounds[0, 2]])
                trail.export(output / "project_Trail.stl")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                outputs = run_blender_build(
                    project, BuildSettings(shape_extra="OUTER TEXT"), blender=blender)

            self.assertEqual(attempts, ["OUTER TEXT", "NONE"])
            self.assertEqual({path.name for path in outputs},
                             {"project.stl", "project_Trail.stl",
                              "project_Plate.stl"})
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertTrue(report["fallback_used"])
            self.assertTrue(report["trail_fit"]["valid"])
            self.assertTrue(report["fallback"]["trail_fit"]["valid"])
            self.assertTrue(report["valid"])
            self.assertTrue(report["preview"]["valid"])
            self.assertEqual(report["fallback_alignment"]["translation_mm"], [0.0, 0.0, 5.0])
            terrain = trimesh.load(project / "deliverables/project.stl", force="mesh")
            trail = trimesh.load(project / "deliverables/project_Trail.stl", force="mesh")
            self.assertAlmostEqual(terrain.bounds[0][2], 2.5)
            self.assertAlmostEqual(trail.bounds[0][2], 7.5)

    def test_preview_failure_keeps_new_stls_and_removes_stale_image(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "build").mkdir()
            (project / "build/preview.png").write_bytes(b"old preview")
            old_customer = project / "deliverables/Old-preview.png"
            old_customer.write_bytes(b"old customer preview")
            (project / "build/validation.json").write_text(json.dumps({
                "valid": True,
                "preview": {"valid": True, "customer_filename": old_customer.name},
            }), encoding="utf-8")
            (project / "route.gpx").write_text('<gpx><trk><trkseg><trkpt lat="1" lon="2"/><trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>', encoding="utf-8")
            blender = Path(temp) / "blender"
            blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                if Path(command[-3]).name == "preview_worker.py":
                    return subprocess.CompletedProcess(command, 1, "", "render failed")
                output = Path(config["export_path"])
                trimesh.creation.box(extents=[100, 80, 5]).export(output / "project.stl")
                trail = trimesh.creation.box(extents=[60, 3, 2]).subdivide()
                trail.apply_translation([0, 0, 3.5])
                trail.export(output / "project_Trail.stl")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                outputs = run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual(len(outputs), 2)
            self.assertFalse((project / "build/preview.png").exists())
            self.assertFalse(old_customer.exists())
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertFalse(report["preview"]["valid"])
            self.assertIn("render failed", report["preview"]["error"])

    def test_failed_validation_preserves_prior_deliverables(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "build").mkdir()
            (project / "route.gpx").write_text('<gpx><trk><trkseg><trkpt lat="1" lon="2"/><trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>', encoding="utf-8")
            previous = project / "deliverables/previous.stl"
            previous.write_text("old", encoding="ascii")
            customer_preview = project / "deliverables/Previous-preview.png"
            customer_preview.write_bytes(b"old customer preview")
            (project / "build/preview.png").write_bytes(b"old internal preview")
            (project / "build/validation.json").write_text(json.dumps({
                "valid": True,
                "preview": {
                    "valid": True,
                    "customer_filename": customer_preview.name,
                },
            }), encoding="utf-8")
            blender = Path(temp) / "blender"
            blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                output = Path(config["export_path"])
                trimesh.creation.box(extents=[100, 80, 5]).export(output / "project.stl")
                first = trimesh.creation.box(extents=[20, 3, 2])
                second = first.copy()
                second.apply_translation([40, 0, 0])
                trimesh.util.concatenate([first, second]).export(output / "project_Trail.stl")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(ValueError, "fragmented"):
                    run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual(previous.read_text(encoding="ascii"), "old")
            self.assertEqual(customer_preview.read_bytes(), b"old customer preview")
            report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            self.assertTrue(report["preview"]["preserved"])
            self.assertEqual(report["preview"]["customer_filename"],
                             customer_preview.name)

    def test_failure_preserves_prior_stls(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").write_text('<gpx><trk><trkseg><trkpt lat="1" lon="2"/><trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>')
            old = project / "deliverables/old.stl"
            old.write_text("old", encoding="ascii")
            blender = Path(temp) / "blender"
            blender.touch()
            failed = subprocess.CompletedProcess([], 1, "", "generation failed")
            with patch("trailprint.blender_bridge.subprocess.run", return_value=failed):
                with self.assertRaisesRegex(ValueError, "generation failed"):
                    run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual(old.read_text(encoding="ascii"), "old")

    def test_missing_elevation_preserves_previous_models_and_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "build").mkdir()
            (project / "route.gpx").write_text(
                '<gpx><trk><trkseg><trkpt lat="1" lon="2"/>'
                '<trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>')
            preserved = [project / "deliverables/old.stl", project / "deliverables/old.3mf",
                         project / "build/preview.png", project / "deliverables/old-preview.png"]
            for path in preserved:
                path.write_bytes(b"previous successful build")
            (project / "build/validation.json").write_text(json.dumps({
                "valid": True, "preview": {"customer_filename": "old-preview.png"}}))
            blender = Path(temp) / "blender"
            blender.touch()

            def fail(project, settings, stage, *args):
                stage.mkdir(parents=True, exist_ok=True)
                (stage / "terrain-data-validation.json").write_text(json.dumps({
                    "valid": False, "errors": ["Missing tile 12/981/1612"]}))
                return subprocess.CompletedProcess([], 1, "", "Missing tile 12/981/1612")

            with patch("trailprint.blender_bridge._run_attempt", side_effect=fail):
                with self.assertRaisesRegex(ValueError, "Missing tile 12/981/1612"):
                    run_blender_build(project, BuildSettings(), blender=blender)
            assert all(path.read_bytes() == b"previous successful build" for path in preserved)
            report = json.loads((project / "build/validation.json").read_text())
            assert not report["valid"] and not report["terrain_data"]["valid"]
            assert report["preview"]["customer_filename"] == "old-preview.png"


if __name__ == "__main__":
    unittest.main()
