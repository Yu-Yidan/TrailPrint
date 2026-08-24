import tempfile
import unittest
from pathlib import Path

import trimesh

from trailprint.mesh_validation import inspect_stl, validate_stls


class MeshValidationTests(unittest.TestCase):
    def test_valid_set_and_fragmented_trail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "route.stl"
            trail = root / "route_Trail.stl"
            trimesh.creation.box(extents=[100, 80, 5]).export(terrain)
            trimesh.creation.box(extents=[60, 3, 2]).subdivide().export(trail)
            self.assertTrue(validate_stls([terrain, trail], "route")["valid"])

            left = trimesh.creation.box(extents=[20, 3, 2])
            right = left.copy(); right.apply_translation([40, 0, 0])
            trimesh.util.concatenate([left, right]).export(trail)
            report = validate_stls([terrain, trail], "route")
            self.assertFalse(report["valid"])
            self.assertIn("fragmented", " ".join(report["errors"]))

    def test_anomalous_assembly_is_centered_without_losing_z_offsets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "route.stl"
            trail = root / "route_Trail.stl"
            terrain_mesh = trimesh.creation.box(extents=[100, 80, 5])
            terrain_mesh.apply_translation([-666832, 85035, 7.5])
            terrain_mesh.export(terrain)
            offset = trimesh.creation.box(extents=[60, 3, 2]).subdivide()
            offset.apply_translation([-666832, 85035, 11])
            offset.export(trail)

            report = validate_stls([terrain, trail], "route")

            self.assertTrue(report["valid"])
            self.assertEqual(len(report["corrections"]), 1)
            self.assertEqual(report["corrections"][0]["type"], "assembly")
            self.assertEqual(set(report["corrections"][0]["files"]),
                             {"route.stl", "route_Trail.stl"})
            inspected = inspect_stl(trail, "route", 100)
            self.assertAlmostEqual(inspected["center_mm"][0], 0, places=3)
            self.assertAlmostEqual(inspected["center_mm"][1], 0, places=3)
            self.assertAlmostEqual(inspected["bounds_mm"][0][2], 5, places=3)

    def test_one_offset_part_is_rejected_instead_of_moved_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "route.stl"
            trail = root / "route_Trail.stl"
            trimesh.creation.box(extents=[100, 80, 5]).export(terrain)
            offset = trimesh.creation.box(extents=[60, 3, 2]).subdivide()
            offset.apply_translation([-666832, 85035, 4])
            offset.export(trail)
            report = validate_stls([terrain, trail], "route")
            self.assertFalse(report["valid"])
            self.assertIn("coordinate system", " ".join(report["errors"]))

    def test_empty_stl_fails_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "route.stl"
            path.write_text("solid route\nendsolid route\n", encoding="ascii")
            result = inspect_stl(path, "route")
            self.assertFalse(result["valid"])
            self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
