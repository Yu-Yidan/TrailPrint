import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import trimesh

from shapely.geometry import LineString, box

from trailprint.coaster import (COASTER_HEIGHT_MM, CoasterSettings,
                                _declutter_contour_lines,
                                build_coaster_geometry,
                                choose_contour_interval)
from trailprint.coaster_bambu import (CoasterBambuSettings,
                                      _enforce_coaster_settings,
                                      assemble_coaster_manifest,
                                      default_coaster_mapping)
from trailprint.bambu import BambuEnvironment


def fixture_project(root: Path):
    project = root / "0001-coaster"
    stage = project / "stage"
    stage.mkdir(parents=True)
    samples = [
        [0.0, 0.0, 0.0], [0.0, 0.01, 100.0],
        [0.01, 0.0, 100.0], [0.01, 0.01, 200.0],
    ]
    (stage / "terrain-samples.json").write_text(
        json.dumps({"samples": samples}), encoding="utf-8")
    (stage / "terrain-osm-evidence.json").write_text(json.dumps({
        "available": True,
        "samples": [[item[2], "green", False] for item in samples],
    }), encoding="utf-8")
    (project / "route.gpx").write_text(
        '<gpx><trk><trkseg><trkpt lat=".002" lon=".002"/>'
        '<trkpt lat=".008" lon=".008"/></trkseg></trk></gpx>',
        encoding="utf-8")
    return project, stage


class CoasterGeometryTests(unittest.TestCase):
    def test_settings_and_conventional_interval(self):
        self.assertEqual(choose_contour_interval(100, 935), 100)
        self.assertEqual(CoasterSettings.validated("8", "2300", "25").contour_interval_m, 25)
        self.assertEqual(CoasterSettings.validated(
            contour_width_mm="0.4").contour_width_mm, 0.4)
        with self.assertRaisesRegex(ValueError, "positive"):
            CoasterSettings.validated(contour_interval_m=0)
        with self.assertRaisesRegex(ValueError, "0.2 mm or 0.4 mm"):
            CoasterSettings.validated(contour_width_mm=0.3)
        with self.assertRaisesRegex(ValueError, "0.2 mm or 0.4 mm"):
            CoasterSettings.validated(contour_width_mm=float("nan"))
        with self.assertRaisesRegex(ValueError, "must be numbers"):
            CoasterSettings.validated(contour_width_mm="thin")

    def test_flat_parts_are_watertight_and_use_only_top_skin(self):
        with tempfile.TemporaryDirectory() as temp:
            project, stage = fixture_project(Path(temp))
            output = project / "output"
            paths = build_coaster_geometry(
                project, stage, CoasterSettings(snow_cutoff_m=100), output)
            meshes = {path.name: trimesh.load_mesh(path) for path in paths}
            self.assertTrue(all(mesh.is_watertight for mesh in meshes.values()))
            self.assertEqual(max(mesh.bounds[1][2] for mesh in meshes.values()),
                             COASTER_HEIGHT_MM)
            colored = [mesh for name, mesh in meshes.items()
                       if any(role in name for role in ("Green_Terrain.stl",
                                                       "Grey_Terrain.stl",
                                                       "White_Terrain.stl",
                                                       "Blue_Terrain.stl"))]
            self.assertTrue(colored)
            self.assertTrue(all(abs(mesh.bounds[0][2] - 3.4) < 1e-5 for mesh in colored))
            self.assertTrue(all(abs(mesh.bounds[1][2] - 3.8) < 1e-5 for mesh in colored))
            trail = next(mesh for name, mesh in meshes.items() if "Trail" in name)
            self.assertAlmostEqual(trail.bounds[0][2], 2.8)
            self.assertAlmostEqual(trail.bounds[1][2], 3.8)
            contours = next(mesh for name, mesh in meshes.items() if "Contours" in name)
            self.assertAlmostEqual(contours.bounds[0][2], 3.8)
            self.assertAlmostEqual(contours.bounds[1][2], 4.0)
            report = json.loads((output / "validation.json").read_text())
            self.assertEqual(report["build_mode"], "coaster")
            self.assertEqual(report["contour_width_mm"], 0.2)
            self.assertEqual(report["contour_layer_mm"], 0.2)
            self.assertEqual(report["terrain_surface_height_mm"], 3.8)
            self.assertTrue((output / "preview.png").is_file())

    def test_crowded_contours_are_suppressed_only_where_too_close(self):
        footprint = box(0, 0, 10, 10)
        first = LineString([(2, 0), (2, 10)])
        crowded_then_open = LineString([(2.5, 0), (2.5, 5), (5, 10)])
        result = _declutter_contour_lines(
            [first, crowded_then_open], footprint, 0.2)
        self.assertTrue(result.intersection(box(2.4, 0.5, 2.6, 4.5)).is_empty)
        self.assertFalse(result.intersection(box(3.5, 7, 5.1, 10)).is_empty)

    def test_selected_width_changes_contour_geometry(self):
        with tempfile.TemporaryDirectory() as temp:
            project, stage = fixture_project(Path(temp))
            volumes = {}
            for width in (0.2, 0.4):
                paths = build_coaster_geometry(
                    project, stage,
                    CoasterSettings(snow_cutoff_m=100, contour_width_mm=width),
                    project / f"output-{width}")
                contour = next(path for path in paths if "Contours" in path.name)
                volumes[width] = trimesh.load_mesh(contour).volume
            self.assertGreater(volumes[0.4], volumes[0.2])


class CoasterBambuTests(unittest.TestCase):
    def test_export_enforces_arachne_for_thin_contours(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "coaster.3mf"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Metadata/project_settings.config", json.dumps({
                    "wall_generator": "classic", "detect_thin_wall": "0",
                    "enable_prime_tower": "1",
                }))
            environment = BambuEnvironment(
                Path("bambu"), Path("data"), "machine", "process",
                ({"color": "#111111"},))
            _enforce_coaster_settings(path, environment)
            with zipfile.ZipFile(path) as archive:
                settings = json.loads(archive.read(
                    "Metadata/project_settings.config"))
            self.assertEqual(settings["wall_generator"], "arachne")
            self.assertEqual(settings["detect_thin_wall"], "1")

    def test_manifest_has_two_plates_and_aligned_terrain(self):
        files = {
            "trail": [Path("trail.stl")], "black": [Path("black-a.stl"), Path("black-b.stl")],
            "green": [Path("green.stl")], "grey": [], "white": [], "blue": [],
        }
        settings = CoasterBambuSettings.validated(5, 4, 1, 5, 2, 1, 3)
        manifest = assemble_coaster_manifest(files, settings, 5)
        self.assertEqual([plate["plate_name"] for plate in manifest["plates"]],
                         ["Coaster Trail", "Coaster Terrain"])
        terrain = manifest["plates"][1]["objects"]
        self.assertEqual(len(terrain), 3)
        self.assertEqual({item["assemble_index"][0] for item in terrain}, {1})

    def test_defaults_choose_nearest_configured_colors(self):
        filaments = [
            {"number": 1, "color": "#050505"},
            {"number": 2, "color": "#ffffff"},
            {"number": 3, "color": "#ff0000"},
        ]
        defaults = default_coaster_mapping(filaments)
        self.assertEqual(defaults["black"], 1)
        self.assertEqual(defaults["white"], 2)
        self.assertEqual(defaults["trail"], 3)


if __name__ == "__main__":
    unittest.main()
