import tempfile
import unittest
from pathlib import Path

import numpy as np
import trimesh

from trailprint.mesh_validation import inspect_stl
from trailprint.water_geometry import (
    WATER_DEPTH_MM,
    WATER_MIN_WIDTH_MM,
    _shoreline_surface,
    prepare_integrated_water,
    projected_footprint,
    repair_terrain_for_water_boolean,
    trail_corridor,
    validate_trail_fit,
)


class IntegratedWaterTests(unittest.TestCase):
    def test_water_geometry_preserves_buffered_trail_corridor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "terrain.stl"
            raw = root / "raw_WATER.stl"
            trail = root / "Trail.stl"
            cap = root / "integrated_WATER.stl"
            cutter = root / "cutter.stl"
            support = root / "support.stl"
            trimesh.creation.box(
                extents=(30, 30, 4),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 2))).export(terrain)
            trimesh.creation.box(
                extents=(12, 20, 2),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 4))).export(raw)
            trimesh.creation.box(
                extents=(2, 24, 2),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 4))).export(trail)

            report = prepare_integrated_water(
                terrain, [raw], cap, cutter, support,
                trail_path=trail, trail_clearance_mm=0.1)
            corridor = trail_corridor(trail, 0.1)

            self.assertTrue(report["available"])
            self.assertEqual(report["trail_clearance_mm"], 0.1)
            self.assertGreater(report["trail_excluded_water_area_mm2"], 0)
            for path in (cap, cutter, support):
                footprint = projected_footprint(
                    trimesh.load_mesh(path, process=True))
                self.assertLessEqual(footprint.intersection(corridor).area, 1e-6)

    def test_trail_cut_does_not_pull_water_down_to_groove_floor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "terrain-with-groove.stl"
            raw = root / "raw_WATER.stl"
            trail = root / "Trail.stl"
            cap = root / "integrated_WATER.stl"
            cutter = root / "cutter.stl"
            support = root / "support.stl"
            floor = trimesh.creation.box(
                extents=(30, 30, 3),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 1.5)))
            left = trimesh.creation.box(
                extents=(13.9, 30, 1),
                transform=trimesh.transformations.translation_matrix(
                    (-8.05, 0, 3.5)))
            right = left.copy()
            right.apply_translation((16.1, 0, 0))
            trimesh.util.concatenate((floor, left, right)).export(terrain)
            trimesh.creation.box(
                extents=(3.2, 20, 2),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 4))).export(raw)
            trimesh.creation.box(
                extents=(2, 24, 2),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 4))).export(trail)

            report = prepare_integrated_water(
                terrain, [raw], cap, cutter, support,
                trail_path=trail, trail_clearance_mm=0.1)

            self.assertTrue(report["available"])
            self.assertGreaterEqual(report["body_count"], 2)
            self.assertTrue(all(
                body["surface_min_z_mm"] == 4.0
                and body["surface_max_z_mm"] == 4.0
                for body in report["bodies"]))

    def test_trail_fit_validation_rejects_refilled_water_groove(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trail = root / "Trail.stl"
            valid_terrain = root / "valid-terrain.stl"
            invalid_terrain = root / "invalid-terrain.stl"
            valid_water = root / "valid-water.stl"
            invalid_water = root / "invalid-water.stl"
            trimesh.creation.box(
                extents=(2, 20, 2),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 4))).export(trail)
            floor = trimesh.creation.box(
                extents=(30, 30, 3),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 1.5)))
            left = trimesh.creation.box(
                extents=(13.9, 30, 1),
                transform=trimesh.transformations.translation_matrix(
                    (-8.05, 0, 3.5)))
            right = left.copy()
            right.apply_translation((16.1, 0, 0))
            trimesh.util.concatenate((floor, left, right)).export(valid_terrain)
            trimesh.creation.box(
                extents=(30, 30, 4),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 2))).export(invalid_terrain)
            water_left = trimesh.creation.box(
                extents=(4.9, 20, 0.4),
                transform=trimesh.transformations.translation_matrix(
                    (-3.55, 0, 3.8)))
            water_right = water_left.copy()
            water_right.apply_translation((7.1, 0, 0))
            trimesh.util.concatenate((water_left, water_right)).export(valid_water)
            trimesh.creation.box(
                extents=(12, 20, 0.4),
                transform=trimesh.transformations.translation_matrix(
                    (0, 0, 3.8))).export(invalid_water)

            valid = validate_trail_fit(
                valid_terrain, trail, valid_water, clearance_mm=0.1)
            invalid = validate_trail_fit(
                invalid_terrain, trail, invalid_water, clearance_mm=0.1)

            self.assertTrue(valid["valid"], valid["errors"])
            self.assertLessEqual(valid["maximum_terrain_intrusion_mm"], 0.05)
            self.assertLessEqual(valid["water_overlap_area_mm2"], 0.01)
            self.assertFalse(invalid["valid"])
            self.assertGreater(invalid["maximum_terrain_intrusion_mm"], 0.05)
            self.assertGreater(invalid["water_overlap_area_mm2"], 0.01)

    def test_terrain_repair_removes_opposed_duplicate_sheet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "terrain-with-sheet.stl"
            repaired = root / "terrain-repaired.stl"
            box = trimesh.creation.box(extents=(10, 10, 4))
            sheet = trimesh.Trimesh(
                vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
                faces=((0, 1, 2), (2, 1, 0)), process=False)
            trimesh.util.concatenate((box, sheet)).export(source)

            report = repair_terrain_for_water_boolean(source, repaired)
            mesh = trimesh.load_mesh(repaired, process=True)

            self.assertTrue(report["output_watertight"])
            self.assertEqual(report["duplicate_groups"], 1)
            self.assertEqual(report["removed_faces"], 2)
            self.assertTrue(mesh.is_watertight)
            self.assertEqual(len(mesh.faces), len(box.faces))

    def test_water_is_two_layer_printable_inlay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "sample.stl"
            raw = root / "sample_WATER.stl"
            cap = root / "integrated_WATER.stl"
            cutter = root / "cutter.stl"
            trimesh.creation.box(extents=(30, 30, 4), transform=trimesh.transformations.translation_matrix((0, 0, 2))).export(terrain)
            trimesh.creation.box(extents=(0.2, 12, 2), transform=trimesh.transformations.translation_matrix((0, 0, 4))).export(raw)

            report = prepare_integrated_water(terrain, [raw], cap, cutter)
            mesh = trimesh.load_mesh(cap, process=True)
            validation = inspect_stl(cap, "integrated")

            self.assertTrue(report["available"])
            self.assertEqual(report["depth_mm"], WATER_DEPTH_MM)
            self.assertEqual(report["minimum_width_mm"], WATER_MIN_WIDTH_MM)
            self.assertTrue(mesh.is_watertight)
            self.assertAlmostEqual(float(mesh.extents[2]), 0.4, places=3)
            self.assertGreaterEqual(float(min(mesh.extents[:2])), 1.0)
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(validation["local_depth_min_mm"], 0.4)
            self.assertEqual(validation["local_depth_max_mm"], 0.4)

    def test_disconnected_low_edge_samples_do_not_pull_water_down(self):
        vertices = np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0)), dtype=float)
        high = [(float(i), 0.0, 20.1 + (i % 3) * 0.1) for i in range(12)]
        edge = [(float(i), 1.0, 8.0) for i in range(4)]

        levels, mode, report = _shoreline_surface(
            vertices, np.asarray(high + edge, dtype=float))

        self.assertEqual(mode, "shoreline-flat")
        self.assertTrue(np.allclose(levels, 20.2))
        self.assertEqual(report["shoreline_discarded_samples"], 4)

    def test_water_follows_sloped_terrain_with_matching_cutter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain_path = root / "slope.stl"
            raw = root / "slope_WATER.stl"
            cap_path = root / "integrated_WATER.stl"
            cutter_path = root / "cutter.stl"
            terrain = trimesh.creation.box(
                extents=(30, 30, 4),
                transform=trimesh.transformations.translation_matrix((0, 0, 2)))
            top = terrain.vertices[:, 2] > 2
            terrain.vertices[top, 2] += terrain.vertices[top, 0] * 0.15
            terrain.export(terrain_path)
            trimesh.creation.box(
                extents=(8, 10, 4),
                transform=trimesh.transformations.translation_matrix((0, 0, 4))).export(raw)

            report = prepare_integrated_water(
                terrain_path, [raw], cap_path, cutter_path)
            cap = trimesh.load_mesh(cap_path, process=True)
            cutter = trimesh.load_mesh(cutter_path, process=True)
            validation = inspect_stl(cap_path, "integrated")

            cap_columns = {}
            for x_value, y_value, z_value in cap.vertices:
                cap_columns.setdefault((round(float(x_value), 4),
                                        round(float(y_value), 4)), []).append(float(z_value))
            cutter_columns = {}
            for x_value, y_value, z_value in cutter.vertices:
                cutter_columns.setdefault((round(float(x_value), 4),
                                           round(float(y_value), 4)), []).append(float(z_value))
            top_errors = [abs(max(values) - (4.0 + x_value * 0.05))
                          for (x_value, _), values in cap_columns.items()]

            self.assertEqual(report["surface_mode"], "shoreline-driven")
            self.assertEqual(report["bodies"][0]["surface_mode"],
                             "shoreline-stepped")
            self.assertGreater(report["bodies"][0]["surface_max_z_mm"],
                               report["bodies"][0]["surface_min_z_mm"])
            self.assertGreater(float(cap.extents[2]), WATER_DEPTH_MM)
            self.assertLessEqual(max(top_errors), 0.61)
            self.assertTrue(all(
                abs((max(values) / 0.2) - round(max(values) / 0.2)) < 1e-6
                for values in cap_columns.values()))
            self.assertTrue(cap.is_watertight)
            self.assertTrue(cutter.is_watertight)
            self.assertTrue(validation["valid"], validation["errors"])
            self.assertEqual(validation["local_depth_min_mm"], 0.4)
            self.assertEqual(validation["local_depth_max_mm"], 0.4)
            for key, values in cap_columns.items():
                self.assertAlmostEqual(min(values), min(cutter_columns[key]), places=3)

    def test_ocean_is_clipped_to_visible_terrain_intersection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "coast.stl"
            ocean = root / "Ocean.stl"
            cap = root / "coast_WATER.stl"
            cutter = root / "cutter.stl"
            trimesh.creation.box(extents=(20, 20, 4), transform=trimesh.transformations.translation_matrix((0, 0, 2))).export(terrain)
            trimesh.creation.box(extents=(100, 100, 1), transform=trimesh.transformations.translation_matrix((0, 0, 4.5))).export(ocean)

            report = prepare_integrated_water(terrain, [ocean], cap, cutter)
            mesh = trimesh.load_mesh(cap, process=True)

            self.assertTrue(report["available"])
            self.assertLessEqual(float(mesh.extents[0]), 20.01)
            self.assertLessEqual(float(mesh.extents[1]), 20.01)
            self.assertAlmostEqual(float(mesh.extents[2]), 0.2, places=3)
            self.assertEqual(report["bodies"][0]["surface_mode"], "ocean-flat")


if __name__ == "__main__":
    unittest.main()
