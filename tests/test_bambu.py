import json
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from trailprint.bambu import (BambuEnvironment, BambuSettings, _enforce_project_settings,
                              _terrain_model_index, assemble_manifest,
                              default_filament_mapping, estimate_water_layer_changes,
                              validate_3mf)


class BambuProjectTests(unittest.TestCase):
    def test_terrain_model_index_comes_from_source_object_id(self):
        model = '''<config>
          <object id="2"><part><metadata key="source_object_id" value="0"/></part></object>
          <object id="4"><part><metadata key="source_object_id" value="1"/></part></object>
          <plate><metadata key="plater_name" value="Trail"/><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
          <plate><metadata key="plater_name" value="Terrain"/><model_instance><metadata key="object_id" value="4"/></model_instance></plate>
        </config>'''
        self.assertEqual(_terrain_model_index(model), "2")

    def test_default_colors_and_three_plate_manifest(self):
        filaments = (
            {"number": 1, "name": "black", "color": "#000000"},
            {"number": 2, "name": "white", "color": "#ffffff"},
            {"number": 3, "name": "red", "color": "#ff0000"},
            {"number": 4, "name": "green", "color": "#00a020"},
        )
        self.assertEqual(default_filament_mapping(filaments),
                         {"trail": 3, "terrain": 1, "plate": 3, "text": 2,
                          "water": 4, "land": 4, "mountain": 4, "summit": 2,
                          "black": 1, "green": 4, "grey": 4, "white": 2})
        files = {role: Path(f"/{role}.stl") for role in ("trail", "terrain", "plate", "text")}
        manifest = assemble_manifest(files, BambuSettings(3, 4, 1, 2, 1, 2), 4)
        self.assertEqual([plate["plate_name"] for plate in manifest["plates"]],
                         ["Trail", "Terrain", "Plate + Text"])
        self.assertEqual(len(manifest["plates"][2]["objects"]), 2)
        self.assertEqual([item["assemble_index"] for item in manifest["plates"][2]["objects"]],
                         [[1], [1]])

    def test_water_files_add_blue_only_fourth_plate(self):
        files = {role: Path(f"/{role}.stl")
                 for role in ("trail", "terrain", "plate", "text")}
        files["water"] = [Path("/route_WATER.stl"), Path("/Ocean.stl")]
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=4)
        manifest = assemble_manifest(files, settings, 4)
        water = manifest["plates"][3]
        self.assertEqual(water["plate_name"], "Water")
        self.assertEqual([item["filaments"] for item in water["objects"]], [[4], [4]])
        self.assertEqual([item["assemble_index"] for item in water["objects"]], [[2], [2]])

    def test_water_can_share_the_terrain_assembly(self):
        files = {role: Path(f"/{role}.stl")
                 for role in ("trail", "terrain", "plate", "text")}
        files["water"] = [Path("/route_WATER.stl"), Path("/Ocean.stl")]
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=2,
                                 water_placement="terrain")

        manifest = assemble_manifest(files, settings, 4)

        self.assertEqual([plate["plate_name"] for plate in manifest["plates"]],
                         ["Trail", "Terrain", "Plate + Text"])
        terrain = manifest["plates"][1]
        self.assertEqual([item["filaments"] for item in terrain["objects"]],
                         [[4], [2], [2]])
        self.assertEqual([item["assemble_index"] for item in terrain["objects"]],
                         [[2], [2], [2]])

    def test_estimates_mixed_water_layers_and_manual_swaps(self):
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=2,
                                 water_placement="terrain")
        estimate = estimate_water_layer_changes(
            [(0.15, 0.55), (0.45, 0.75)], 0, 0.2, 0.2, settings, [])

        self.assertEqual(estimate["water_layer_ranges"], [[1, 4]])
        self.assertEqual(estimate["mixed_filament_layer_count"], 4)
        self.assertEqual(estimate["manual_swaps_max"], 8)
        self.assertFalse(estimate["is_exact"])

    def test_matching_water_and_terrain_filaments_need_no_swaps(self):
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=4,
                                 water_placement="terrain")
        estimate = estimate_water_layer_changes(
            [(0.2, 0.6)], 0, 0.2, 0.3, settings, [])
        self.assertEqual(estimate["water_layer_count"], 3)
        self.assertEqual(estimate["mixed_filament_layer_count"], 0)
        self.assertEqual(estimate["manual_swaps_max"], 0)

    def test_osm_mode_uses_lowest_band_as_terrain_base(self):
        bands = ({"role": "black", "min_z": 2.0, "max_z": 5.0},
                 {"role": "green", "min_z": 5.0, "max_z": 8.0})
        settings = BambuSettings.validated(
            4, trail=3, land=4, mountain=3, summit=2, plate=1, text=2,
            black=1, green=4, grey=3, white=2,
            terrain_mode="osm", automatic_bands=bands)
        files = {role: Path(f"/{role}.stl")
                 for role in ("trail", "terrain", "plate", "text")}
        manifest = assemble_manifest(files, settings, 4)
        self.assertEqual(manifest["plates"][1]["objects"][0]["filaments"], [1])
        self.assertEqual(settings.terrain_filaments()["green"], 4)

    def test_validates_and_enforces_bambu_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "model.3mf"
            settings = {"enable_prime_tower": "1", "flush_volumes_matrix": ["12"]}
            model = '''<config>
              <object id="2"><metadata key="extruder" value="3"/><part><metadata key="source_object_id" value="0"/></part></object>
              <object id="4"><metadata key="extruder" value="4"/><part><metadata key="source_object_id" value="1"/></part></object>
              <object id="7"><part id="1"><metadata key="source_file" value="model_Plate.stl"/><metadata key="source_object_id" value="2"/><metadata key="extruder" value="1"/></part><part id="2"><metadata key="source_file" value="model_Text.stl"/><metadata key="source_object_id" value="3"/><metadata key="extruder" value="2"/></part></object>
              <plate><metadata key="plater_name" value="Trail"/><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
              <plate><metadata key="plater_name" value="Terrain"/><model_instance><metadata key="object_id" value="4"/></model_instance></plate>
              <plate><metadata key="plater_name" value="Plate + Text"/><model_instance><metadata key="object_id" value="7"/></model_instance></plate>
            </config>'''
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", model)
            filaments = tuple({"number": n, "name": f"F{n}", "color": color}
                              for n, color in enumerate(("#000000", "#FFFFFF", "#FF0000", "#00AA00"), 1))
            environment = BambuEnvironment(Path("bambu"), Path("data"), "machine", "process", filaments)

            bands = [{"role": "mountain", "min_z": 8.0, "max_z": 17.0},
                     {"role": "summit", "min_z": 17.0, "max_z": 20.0}]
            terrain_filaments = {"land": 4, "mountain": 1, "summit": 2}
            _enforce_project_settings(output, environment, bands, terrain_filaments)
            report = validate_3mf(
                output, {"trail": 3, "terrain": 4, "plate": 1, "text": 2},
                bands, terrain_filaments)

            self.assertTrue(report["valid"])
            with zipfile.ZipFile(output) as archive:
                final = json.loads(archive.read("Metadata/project_settings.config"))
                ranges = ET.fromstring(archive.read("Metadata/layer_config_ranges.xml"))
            self.assertEqual(final["filament_colour"], [item["color"] for item in filaments])
            self.assertEqual(set(final["flush_volumes_matrix"]), {"0"})
            self.assertEqual(final["enable_prime_tower"], "0")
            self.assertEqual([item["filament"] for item in report["terrain_bands"]], [1, 2])
            self.assertEqual([item["layer_height"] for item in report["terrain_bands"]],
                             [0.2, 0.2])
            self.assertEqual(ranges.find("object").attrib["id"], "2")

    def test_validates_combined_terrain_and_water_object(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "combined.3mf"
            settings = {"enable_prime_tower": "0", "flush_volumes_matrix": ["0"]}
            model = '''<config>
              <object id="2"><metadata key="extruder" value="3"/><part><metadata key="source_object_id" value="0"/></part></object>
              <object id="4"><part><metadata key="source_file" value="route.stl"/><metadata key="source_object_id" value="1"/><metadata key="extruder" value="4"/></part><part><metadata key="source_file" value="route_WATER.stl"/><metadata key="source_object_id" value="2"/><metadata key="extruder" value="2"/></part></object>
              <object id="7"><part><metadata key="source_file" value="route_Plate.stl"/><metadata key="extruder" value="1"/></part><part><metadata key="source_file" value="route_Text.stl"/><metadata key="extruder" value="2"/></part></object>
              <plate><metadata key="plater_name" value="Trail"/><model_instance><metadata key="object_id" value="2"/></model_instance></plate>
              <plate><metadata key="plater_name" value="Terrain"/><model_instance><metadata key="object_id" value="4"/></model_instance></plate>
              <plate><metadata key="plater_name" value="Plate + Text"/><model_instance><metadata key="object_id" value="7"/></model_instance></plate>
            </config>'''
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", model)

            report = validate_3mf(
                output, {"trail": 3, "terrain": 4, "plate": 1, "text": 2},
                terrain_filaments={"land": 4, "mountain": 1, "summit": 2},
                water_filament=2, water_placement="terrain")

            self.assertEqual(report["plates"], 3)
            self.assertEqual(report["water_placement"], "terrain")

    def test_filament_numbers_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "from 1 to 4"):
            BambuSettings.validated(4, 1, 2, 3, 5)

    def test_water_placement_is_validated(self):
        with self.assertRaisesRegex(ValueError, "separate or with terrain"):
            BambuSettings.validated(4, water_placement="somewhere")

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
