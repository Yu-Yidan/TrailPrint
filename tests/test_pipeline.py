import tempfile
import unittest
from pathlib import Path

from trailprint.cli import build_ribbon
from trailprint.config import load_config
from trailprint.gpx import read_gpx


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def test_example_builds_watertight_ascii_stl_structure(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "sample"
            project.mkdir()
            (project / "route.gpx").write_text(
                (ROOT / "prints/example-trail/route.gpx").read_text(), encoding="utf-8")
            (project / "print.yaml").write_text("preset: standard\n", encoding="utf-8")
            output = build_ribbon(project, ROOT / "presets")
            text = output.read_text(encoding="ascii")
            self.assertTrue(text.startswith("solid sample\n"))
            self.assertIn("facet normal", text)
            self.assertTrue(text.endswith("endsolid sample\n"))

    def test_local_values_override_preset(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "print.yaml").write_text(
                "preset: standard\ntrack_width_mm: 7\n", encoding="utf-8")
            config = load_config(project, ROOT / "presets")
            self.assertEqual(config["track_width_mm"], 7)
            self.assertEqual(config["max_size_mm"], 120)

    def test_gpx_requires_two_points(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "short.gpx"
            path.write_text('<gpx><trk><trkseg><trkpt lat="1" lon="2"/></trkseg></trk></gpx>')
            with self.assertRaisesRegex(ValueError, "at least two"):
                read_gpx(path)


if __name__ == "__main__":
    unittest.main()
