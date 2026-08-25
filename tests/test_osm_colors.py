import json
import tempfile
import unittest
from pathlib import Path

from trailprint.osm_colors import (analyze_project, load_palette,
                                   optimize_elevation_bands, print_bands)


class OsmColorTests(unittest.TestCase):
    def test_optimizer_limits_changes_and_omits_weak_noise(self):
        samples = []
        for elevation, role in ((100, "green"), (200, "black"),
                                (300, "grey"), (400, "white")):
            samples.extend((elevation + index / 100, role) for index in range(100))
        bands = optimize_elevation_bands(samples)
        self.assertEqual([band["role"] for band in bands],
                         ["green", "black", "grey", "white"])
        self.assertLessEqual(len(bands) - 1, 3)

        mostly_green = [(index, "green") for index in range(100)] + [(101, "grey")]
        self.assertEqual([item["role"] for item in
                          optimize_elevation_bands(mostly_green)], ["green"])

    def test_print_bands_remove_thin_colors_without_z_gaps(self):
        source = [
            {"role": "green", "min_elevation_m": 0, "max_elevation_m": 49},
            {"role": "grey", "min_elevation_m": 49, "max_elevation_m": 50},
            {"role": "white", "min_elevation_m": 50, "max_elevation_m": 100},
        ]
        elevation = {
            "source_min_m": 0, "source_max_m": 100,
            "model_surface_min_z_mm": 2, "model_surface_max_z_mm": 12,
            "min_thickness_mm": 2,
        }
        bands = print_bands(source, elevation, layer_height=0.2)
        self.assertEqual([band["role"] for band in bands], ["green", "white"])
        self.assertEqual(bands[0]["min_z"], 2)
        self.assertEqual(bands[-1]["max_z"], 12)
        self.assertEqual(bands[0]["max_z"], bands[1]["min_z"])

    def test_successful_empty_osm_evidence_produces_all_green(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "build").mkdir()
            samples = [[index, "green", False] for index in range(100)]
            (project / "build/terrain-osm-evidence.json").write_text(json.dumps({
                "available": True, "samples": samples, "evidence_counts": {},
            }), encoding="utf-8")
            (project / "build/elevation.json").write_text(json.dumps({
                "source_min_m": 0, "source_max_m": 99,
                "model_surface_min_z_mm": 2, "model_surface_max_z_mm": 12,
                "min_thickness_mm": 2,
            }), encoding="utf-8")
            result = analyze_project(project)
            self.assertEqual([band["role"] for band in result["bands"]], ["green"])
            self.assertEqual(result["mapped_coverage"], 0)
            self.assertEqual(result["version"], 2)

    def test_failed_evidence_and_legacy_palette_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "build").mkdir()
            (project / "build/terrain-osm-evidence.json").write_text(
                json.dumps({"available": False, "error": "Overpass unavailable"}),
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Overpass unavailable"):
                analyze_project(project)
            (project / "build/terrain-palette.json").write_text(json.dumps({
                "version": 1, "available": True,
                "source": "ESA WorldCover / Sentinel-2", "bands": [{}],
            }), encoding="utf-8")
            self.assertEqual(load_palette(project), {})


if __name__ == "__main__":
    unittest.main()
