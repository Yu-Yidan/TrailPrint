import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trimesh

from trailprint.blender_bridge import _correct_staged_groove
from trailprint.water_geometry import TerrainRepairError, repair_terrain_for_water_boolean


class GrooveRetryTests(unittest.TestCase):
    def exercise(self, persistent):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            terrain = trimesh.creation.box((10, 10, 3))
            terrain.apply_translation((0, 0, 1.5))
            terrain.export(stage / "route.stl")
            trail = trimesh.creation.box((2, 8, 1.5)).subdivide()
            trail.apply_translation((0, 0, 2.25))
            trail.export(stage / "route_Trail.stl")
            original = hashlib.sha256((stage / "route.stl").read_bytes()).hexdigest()
            calls = []
            input_hashes = []

            def boolean(command, *args):
                input_hashes.append(hashlib.sha256((stage / "route.stl").read_bytes()).hexdigest())
                job = json.loads(Path(command[-1]).read_text())
                shutil.copy2(job["terrain_path"], job["output_path"])
                return subprocess.CompletedProcess(command, 0, "", "")

            def repair(source, target):
                if source.name == "terrain-corrected.stl":
                    calls.append(source)
                    if persistent or len(calls) == 1:
                        raise TerrainRepairError("test topology defect", {"valid": False})
                return repair_terrain_for_water_boolean(source, target)

            with patch("trailprint.blender_bridge._run_command", side_effect=boolean), \
                 patch("trailprint.blender_bridge.repair_terrain_for_water_boolean", side_effect=repair), \
                 patch("trailprint.blender_bridge.validate_trail_fit",
                       side_effect=[{"valid": False}, {"valid": True}]):
                if persistent:
                    with self.assertRaisesRegex(TerrainRepairError, "topology defect"):
                        _correct_staged_groove(stage, "route", Path("blender"), 60)
                else:
                    _correct_staged_groove(stage, "route", Path("blender"), 60)
            self.assertEqual(len(calls), 2)
            self.assertEqual(input_hashes, [original, original])
            if persistent:
                self.assertEqual(original, hashlib.sha256((stage / "route.stl").read_bytes()).hexdigest())
            report = json.loads((stage / "groove-correction.json").read_text())
            self.assertEqual(len(report["attempts"]), 2)
            self.assertEqual(report["attempts"][-1]["valid"], not persistent)

    def test_topology_failure_uses_bounded_retry(self):
        self.exercise(False)

    def test_persistent_topology_failure_stays_blocked(self):
        self.exercise(True)
