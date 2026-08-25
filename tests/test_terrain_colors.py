import tempfile
import unittest
from pathlib import Path

from trailprint.terrain_colors import (active_terrain_bands, elevation_to_meters,
                                       load_terrain_defaults, save_terrain_preferences)


FILAMENTS = (
    {"number": 1, "name": "Black", "color": "#000000"},
    {"number": 2, "name": "White", "color": "#ffffff"},
    {"number": 3, "name": "Grey", "color": "#888888"},
    {"number": 4, "name": "Green", "color": "#00aa22"},
)
ELEVATION = {
    "source_min_m": 500.0, "source_max_m": 2500.0,
    "model_surface_min_z_mm": 5.5, "model_surface_max_z_mm": 25.5,
    "min_thickness_mm": 2.0,
}


class TerrainColorTests(unittest.TestCase):
    def test_automatic_band_counts_and_z_mapping(self):
        bands = active_terrain_bands(ELEVATION, 1200, 2300)
        self.assertEqual([item["role"] for item in bands], ["mountain", "summit"])
        self.assertAlmostEqual(bands[0]["min_z"], 9.0)
        self.assertAlmostEqual(bands[0]["max_z"], 20.0)
        self.assertAlmostEqual(bands[1]["max_z"], 22.0)
        low = {**ELEVATION, "source_max_m": 1000.0}
        self.assertEqual(active_terrain_bands(low, 1200, 2300), [])
        medium = {**ELEVATION, "source_max_m": 2000.0}
        self.assertEqual([item["role"] for item in active_terrain_bands(
            medium, 1200, 2300)], ["mountain"])

    def test_sub_layer_summit_is_omitted(self):
        bands = active_terrain_bands(ELEVATION, 1200, 2490, nominal_layer_height_mm=0.2)
        self.assertEqual([item["role"] for item in bands], ["mountain"])

    def test_units_and_order_validation(self):
        self.assertAlmostEqual(elevation_to_meters(4000, "IMPERIAL"), 1219.2)
        self.assertEqual(elevation_to_meters(1200, "METRIC"), 1200)
        with self.assertRaisesRegex(ValueError, "higher"):
            active_terrain_bands(ELEVATION, 2300, 1200)

    def test_project_preferences_override_saved_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "prints/route"
            project.mkdir(parents=True)
            global_value = {"land": 4, "mountain": 3, "summit": 2,
                            "grey_start_m": 1200, "white_start_m": 2300}
            save_terrain_preferences(root, project, global_value, save_defaults=True)
            project_value = {**global_value, "grey_start_m": 900, "white_start_m": 1800}
            save_terrain_preferences(root, project, project_value)
            loaded = load_terrain_defaults(root, project, FILAMENTS)
            self.assertEqual((loaded["land"], loaded["mountain"], loaded["summit"]),
                             (4, 3, 2))
            self.assertEqual((loaded["grey_start_m"], loaded["white_start_m"]),
                             (900, 1800))


if __name__ == "__main__":
    unittest.main()
