import tempfile
import unittest
from pathlib import Path

import trimesh

from trailprint.mesh_validation import inspect_stl, mesh_role, validate_stls


class MeshValidationTests(unittest.TestCase):
    def test_water_and_ocean_outputs_share_water_role(self):
        self.assertEqual(mesh_role(Path("0019-route_WATER.stl"), "0019-route"), "water")
        self.assertEqual(mesh_role(Path("0019-Ocean.stl"), "0019-route"), "water")

    def test_back_plate_inlay_has_attribution_role(self):
        self.assertEqual(
            mesh_role(Path("0019-route_Attribution.stl"), "0019-route"),
            "attribution")

    def test_water_in_project_name_does_not_override_core_roles(self):
        slug = "0032-water-combined"
        self.assertEqual(mesh_role(Path(f"{slug}.stl"), slug), "terrain")
        self.assertEqual(mesh_role(Path(f"{slug}_Trail.stl"), slug), "trail")
        self.assertEqual(mesh_role(Path(f"{slug}_WATER.stl"), slug), "water")

    def test_one_layer_ocean_and_two_layer_inland_water_are_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for depth in (0.2, 0.4):
                path = root / f"route-{depth}_WATER.stl"
                trimesh.creation.box(extents=(10, 10, depth)).export(path)
                result = inspect_stl(path, f"route-{depth}")
                self.assertTrue(result["valid"], result["errors"])

    def test_valid_set_and_fragmented_trail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "route.stl"
            trail = root / "route_Trail.stl"
            trimesh.creation.box(extents=[100, 80, 5]).export(terrain)
            trimesh.creation.box(extents=[60, 3, 2]).subdivide().export(trail)
            self.assertTrue(validate_stls([terrain, trail], "route")["valid"])

            left = trimesh.creation.box(extents=[20, 3, 2])
            right = left.copy()
            right.apply_translation([40, 0, 0])
            trimesh.util.concatenate([left, right]).export(trail)
            report = validate_stls([terrain, trail], "route")
            self.assertFalse(report["valid"])
            self.assertIn("fragmented", " ".join(report["errors"]))

    def test_plate_attribution_is_centered_and_one_layer_deep(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            terrain = root / "route.stl"
            trail = root / "route_Trail.stl"
            plate = root / "route_Plate.stl"
            attribution = root / "route_Attribution.stl"
            trimesh.creation.box(extents=[100, 80, 5]).export(terrain)
            trimesh.creation.box(extents=[60, 3, 2]).subdivide().export(trail)
            plate_mesh = trimesh.creation.box(extents=[120, 100, 5])
            plate_mesh.apply_translation([0, 0, 2.5])
            plate_mesh.export(plate)
            inlay = trimesh.creation.box(extents=[70, 10, 0.2])
            inlay.apply_translation([0, 0, 0.1])
            inlay.export(attribution)

            report = validate_stls(
                [terrain, trail, plate, attribution], "route", repair=False)
            self.assertTrue(report["valid"])

            too_deep = trimesh.creation.box(extents=[70, 10, 0.4])
            too_deep.apply_translation([0, 0, 0.2])
            too_deep.export(attribution)
            report = validate_stls(
                [terrain, trail, plate, attribution], "route", repair=False)
            self.assertFalse(report["valid"])
            self.assertIn("one 0.20 mm first layer", " ".join(report["errors"]))

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
