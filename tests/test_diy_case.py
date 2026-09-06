import unittest

import trimesh
from shapely.geometry import Point

from trailprint.diy_case import case_dimensions


class DiyCaseTests(unittest.TestCase):
    def test_short_trail_uses_minimum_height_and_hinge_allowance(self):
        trail = trimesh.creation.box(extents=(70, 20, 4))
        dimensions = case_dimensions(trail, Point(0, 0).buffer(30))

        self.assertEqual(dimensions["half_height_mm"], 4.0)
        self.assertEqual(dimensions["closed_height_mm"], 8.0)
        self.assertEqual(dimensions["hinge"]["envelope_mm"], 7.8)
        self.assertEqual(dimensions["hinge"]["height_allowance_mm"], 0.2)

    def test_tall_trail_rounds_up_and_expands_body_for_wall(self):
        trail = trimesh.creation.box(extents=(79, 18, 11.4906))
        dimensions = case_dimensions(trail, Point(0, 0).buffer(44))

        self.assertEqual(dimensions["half_height_mm"], 7.0)
        self.assertEqual(dimensions["closed_height_mm"], 14.0)
        self.assertEqual(dimensions["body_radius_mm"], 46.0)
        self.assertGreaterEqual(dimensions["minimum_radial_wall_mm"], 2.0)
        self.assertFalse(dimensions["hinge"]["separate_pin_or_insert"])


if __name__ == "__main__":
    unittest.main()
