import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from trailprint.bambu import (BambuEnvironment, BambuSettings, _enforce_project_settings,
                              assemble_manifest, default_filament_mapping, validate_3mf)


class BambuProjectTests(unittest.TestCase):
    def test_default_colors_and_three_plate_manifest(self):
        filaments = (
            {"number": 1, "name": "black", "color": "#000000"},
            {"number": 2, "name": "white", "color": "#ffffff"},
            {"number": 3, "name": "red", "color": "#ff0000"},
            {"number": 4, "name": "green", "color": "#00a020"},
        )
        self.assertEqual(default_filament_mapping(filaments),
                         {"trail": 3, "terrain": 1, "plate": 3, "text": 2})
        files = {role: Path(f"/{role}.stl") for role in ("trail", "terrain", "plate", "text")}
        manifest = assemble_manifest(files, BambuSettings(3, 4, 1, 2), 4)
        self.assertEqual([plate["plate_name"] for plate in manifest["plates"]],
                         ["Trail", "Terrain", "Plate + Text"])
        self.assertEqual(len(manifest["plates"][2]["objects"]), 2)
        self.assertEqual([item["assemble_index"] for item in manifest["plates"][2]["objects"]],
                         [[1], [1]])

    def test_validates_and_enforces_bambu_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "model.3mf"
            settings = {"enable_prime_tower": "1", "flush_volumes_matrix": ["12"]}
            model = '''<config>
              <object id="1"><metadata key="extruder" value="3"/></object>
              <object id="2"><metadata key="extruder" value="4"/></object>
              <object id="3"><part id="1"><metadata key="source_file" value="model_Plate.stl"/><metadata key="extruder" value="1"/></part><part id="2"><metadata key="source_file" value="model_Text.stl"/><metadata key="extruder" value="2"/></part></object>
              <plate><metadata key="plater_name" value="Trail"/><model_instance><metadata key="object_id" value="1"/></model_instance></plate>
              <plate><metadata key="plater_name" value="Terrain"/><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
              <plate><metadata key="plater_name" value="Plate + Text"/><model_instance><metadata key="object_id" value="3"/></model_instance></plate>
            </config>'''
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", model)
            filaments = tuple({"number": n, "name": f"F{n}", "color": color}
                              for n, color in enumerate(("#000000", "#FFFFFF", "#FF0000", "#00AA00"), 1))
            environment = BambuEnvironment(Path("bambu"), Path("data"), "machine", "process", filaments)

            _enforce_project_settings(output, environment)
            report = validate_3mf(output, {"trail": 3, "terrain": 4, "plate": 1, "text": 2})

            self.assertTrue(report["valid"])
            with zipfile.ZipFile(output) as archive:
                final = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertEqual(final["filament_colour"], [item["color"] for item in filaments])
            self.assertEqual(set(final["flush_volumes_matrix"]), {"0"})
            self.assertEqual(final["enable_prime_tower"], "0")

    def test_filament_numbers_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "from 1 to 4"):
            BambuSettings.validated(4, 1, 2, 3, 5)

    def test_plate_default_falls_back_when_slot_three_is_unavailable(self):
        filaments = (
            {"number": 1, "name": "red", "color": "#ff0000"},
            {"number": 2, "name": "white", "color": "#ffffff"},
        )
        defaults = default_filament_mapping(filaments)
        self.assertEqual(defaults["terrain"], 1)
        self.assertEqual(defaults["plate"], 2)


if __name__ == "__main__":
    unittest.main()
