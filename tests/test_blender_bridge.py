import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trailprint.blender_bridge import BuildSettings, run_blender_build


class SettingsTests(unittest.TestCase):
    def test_defaults_and_compatibility(self):
        self.assertEqual(BuildSettings.validated(), BuildSettings("HEXAGON", "NONE", 4))
        self.assertEqual(BuildSettings.validated("circle", "outer text", "8"),
                         BuildSettings("CIRCLE", "OUTER TEXT", 8))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            BuildSettings.validated("SQUARE", "OUTER TEXT", 4)
        with self.assertRaisesRegex(ValueError, "1 to 10"):
            BuildSettings.validated("HEXAGON", "NONE", 11)

    def test_success_replaces_only_prior_stls(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "deliverables").mkdir(parents=True)
            (project / "route.gpx").write_text("<gpx/>", encoding="utf-8")
            (project / "deliverables/old.stl").write_text("old", encoding="ascii")
            (project / "deliverables/keep.3mf").write_text("keep", encoding="ascii")
            blender = Path(temp) / "blender"
            blender.touch()

            def fake_run(command, **_kwargs):
                config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                Path(config["export_path"], "Map.stl").write_text("solid map", encoding="ascii")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("trailprint.blender_bridge.subprocess.run", side_effect=fake_run):
                outputs = run_blender_build(project, BuildSettings(), blender=blender)
            self.assertEqual([path.name for path in outputs], ["Map.stl"])
            self.assertFalse((project / "deliverables/old.stl").exists())
            self.assertTrue((project / "deliverables/keep.3mf").exists())

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
