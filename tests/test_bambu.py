import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import trimesh

from trailprint.bambu import (
    ONE_PLATE_LAYOUT,
    WALL_GENERATOR,
    BambuEnvironment,
    BambuSettings,
    _enforce_project_settings,
    _terrain_model_index,
    assemble_manifest,
    default_filament_mapping,
    discover_environment,
    estimate_water_layer_changes,
    validate_3mf,
)


class BambuProjectTests(unittest.TestCase):
    def bambu_environment(self, vector, matrix=None):
        temp = tempfile.TemporaryDirectory()
        data_dir = Path(temp.name)
        binary = data_dir / "BambuStudio"
        binary.touch()
        count = 5
        presets = {
            "machine": "P1S",
            "process": "0.20mm Standard",
            "filaments": [f"Filament {number}" for number in range(1, count + 1)],
            "filament_colors": ",".join(["#000000"] * count),
            "flush_volumes_matrix": "|".join(
                map(str, matrix if matrix is not None else range(count * count))),
            "flush_volumes_vector": "|".join(map(str, vector)),
        }
        (data_dir / "BambuStudio.conf").write_text(json.dumps({"presets": presets}))
        return temp, data_dir, binary

    def test_discovery_pads_stale_short_flush_vector(self):
        temp, data_dir, binary = self.bambu_environment(range(8))
        with temp:
            environment = discover_environment(data_dir, binary)
        self.assertEqual(environment.flush_volumes_vector,
                         tuple(map(float, range(8))) + (140.0, 140.0))

    def test_discovery_preserves_current_flush_vector(self):
        temp, data_dir, binary = self.bambu_environment(range(10))
        with temp:
            environment = discover_environment(data_dir, binary)
        self.assertEqual(environment.flush_volumes_vector, tuple(map(float, range(10))))

    def test_discovery_trims_stale_long_flush_vector(self):
        temp, data_dir, binary = self.bambu_environment(range(12))
        with temp:
            environment = discover_environment(data_dir, binary)
        self.assertEqual(environment.flush_volumes_vector, tuple(map(float, range(10))))

    def test_discovery_rejects_invalid_or_unpaired_flush_values(self):
        for vector in ((0, -1), (0, float("inf")), (0, 1, 2)):
            with self.subTest(vector=vector):
                temp, data_dir, binary = self.bambu_environment(vector)
                with temp, self.assertRaises(ValueError):
                    discover_environment(data_dir, binary)

    def test_discovery_keeps_flush_matrix_strict(self):
        temp, data_dir, binary = self.bambu_environment(range(8), matrix=range(24))
        with temp, self.assertRaisesRegex(ValueError, "do not match"):
            discover_environment(data_dir, binary)

    def test_terrain_model_index_comes_from_source_object_id(self):
        model = '''<config>
          <object id="2"><part><metadata key="source_object_id" value="0"/></part></object>
          <object id="4"><part><metadata key="source_file" value="route.stl"/><metadata key="source_object_id" value="1"/></part></object>
          <plate><metadata key="plater_name" value="TrailPrint"/><model_instance><metadata key="object_id" value="2"/></model_instance><model_instance><metadata key="object_id" value="4"/></model_instance></plate>
        </config>'''
        self.assertEqual(_terrain_model_index(model), "2")

    def test_default_colors_and_one_plate_manifest(self):
        filaments = (
            {"number": 1, "name": "black", "color": "#000000"},
            {"number": 2, "name": "white", "color": "#ffffff"},
            {"number": 3, "name": "red", "color": "#ff0000"},
            {"number": 4, "name": "green", "color": "#00a020"},
        )
        self.assertEqual(default_filament_mapping(filaments),
                         {"trail": 3, "terrain": 1, "plate": 1, "text": 2,
                          "attribution": 4,
                          "water": 4, "land": 4, "mountain": 4, "summit": 2,
                          "black": 1, "green": 4, "grey": 4, "white": 2})
        files = {role: Path(f"/{role}.stl")
                 for role in ("trail", "terrain", "plate", "text")}
        manifest = assemble_manifest(files, BambuSettings(3, 4, 1, 2, 1, 2), 4)
        self.assertEqual([plate["plate_name"] for plate in manifest["plates"]],
                         ["TrailPrint"])
        self.assertFalse(manifest["plates"][0]["need_arrange"])
        self.assertEqual(manifest["plates"][0]["plate_params"]["print_sequence"],
                         "by object")
        self.assertEqual(len(manifest["plates"][0]["objects"]), 4)
        self.assertEqual([item["assemble_index"] for item in manifest["plates"][0]["objects"]],
                         [[0], [0], [1], [1]])
        self.assertEqual([item["print_params"] for item in
                          manifest["plates"][0]["objects"]],
                         [{}, {}, {}, {}])

    def test_water_is_always_combined_with_terrain_on_one_plate(self):
        files = {role: Path(f"/{role}.stl")
                 for role in ("trail", "terrain", "plate", "text")}
        files["water"] = [Path("/route_WATER.stl"), Path("/Ocean.stl")]
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=4)
        manifest = assemble_manifest(files, settings, 4)
        objects = manifest["plates"][0]["objects"]
        self.assertEqual(len(manifest["plates"]), 1)
        self.assertEqual([item["filaments"] for item in objects],
                         [[3], [4], [4], [4], [1], [2]])
        self.assertEqual([item["assemble_index"] for item in objects],
                         [[0], [2], [2], [2], [1], [1]])

    def test_diy_case_adds_second_plate_with_terrain_rock_filament(self):
        files = {role: Path(f"/{role}.stl")
                 for role in ("trail", "terrain", "plate", "text")}
        with tempfile.TemporaryDirectory() as temp:
            case = Path(temp) / "diy-case.stl"
            trimesh.creation.box(extents=(80, 190, 8)).export(case)
            manual = BambuSettings(
                3, 4, 2, 1, 1, 2, generate_diy_kit=True)
            manifest = assemble_manifest(files, manual, 4, diy_case=case)
            self.assertEqual([item["plate_name"] for item in manifest["plates"]],
                             ["TrailPrint", "DIY Case"])
            self.assertEqual(manifest["plates"][1]["objects"][0]["filaments"], [2])
            self.assertEqual(manifest["plates"][1]["plate_params"]["enable_support"], "0")
            osm = BambuSettings(
                3, 4, 2, 1, 1, 2, grey_filament=3, terrain_mode="osm",
                automatic_bands=({"role": "green"},), generate_diy_kit=True)
            self.assertEqual(osm.diy_case_filament(), 3)

    def test_estimates_mixed_water_layers_and_manual_swaps(self):
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=2)
        estimate = estimate_water_layer_changes(
            [(0.15, 0.55), (0.45, 0.75)], 0, 0.2, 0.2, settings, [])

        self.assertEqual(estimate["water_layer_ranges"], [[1, 4]])
        self.assertEqual(estimate["mixed_filament_layer_count"], 4)
        self.assertEqual(estimate["manual_swaps_max"], 8)
        self.assertFalse(estimate["is_exact"])

    def test_matching_water_and_terrain_filaments_need_no_swaps(self):
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=4)
        estimate = estimate_water_layer_changes(
            [(0.2, 0.6)], 0, 0.2, 0.3, settings, [])
        self.assertEqual(estimate["water_layer_count"], 3)
        self.assertEqual(estimate["mixed_filament_layer_count"], 0)
        self.assertEqual(estimate["manual_swaps_max"], 0)

    def test_two_layer_water_on_layer_boundaries_is_not_overcounted(self):
        settings = BambuSettings(3, 4, 1, 2, 1, 2, water_filament=2)
        estimate = estimate_water_layer_changes(
            [(19.8, 20.2)], 3.8, 0.2, 0.2, settings, [])

        self.assertEqual(estimate["water_layer_count"], 2)
        self.assertEqual(estimate["water_layer_ranges"], [[81, 82]])

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
        self.assertEqual(manifest["plates"][0]["objects"][1]["filaments"], [1])
        self.assertEqual(settings.terrain_filaments()["green"], 4)
        self.assertEqual(settings.by_role()["attribution"], 3)

    def test_manual_attribution_reuses_mountain_filament(self):
        settings = BambuSettings.validated(
            4, trail=3, land=4, mountain=2, summit=1, plate=1, text=2)
        self.assertEqual(settings.by_role()["attribution"], 2)

    def test_validates_and_enforces_bambu_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "model.3mf"
            settings = {
                "enable_prime_tower": "1",
                "flush_volumes_matrix": ["12"],
                "different_settings_to_system": ["existing_override", "filament_override"],
            }
            model = '''<config>
              <object id="2"><metadata key="extruder" value="3"/><part><metadata key="source_file" value="model_Trail.stl"/><metadata key="source_object_id" value="0"/></part></object>
              <object id="4"><part><metadata key="source_file" value="model.stl"/><metadata key="source_object_id" value="1"/><metadata key="extruder" value="4"/></part><part><metadata key="source_file" value="model_WATER.stl"/><metadata key="source_object_id" value="2"/><metadata key="extruder" value="2"/></part></object>
              <object id="7"><part id="1"><metadata key="source_file" value="model_Plate.stl"/><metadata key="source_object_id" value="2"/><metadata key="extruder" value="1"/></part><part id="2"><metadata key="source_file" value="model_Text.stl"/><metadata key="source_object_id" value="3"/><metadata key="extruder" value="2"/></part><part id="3"><metadata key="source_file" value="model_Attribution.stl"/><metadata key="source_object_id" value="4"/><metadata key="extruder" value="1"/></part></object>
              <plate><metadata key="plater_name" value="TrailPrint"/><model_instance><metadata key="object_id" value="2"/></model_instance><model_instance><metadata key="object_id" value="4"/></model_instance><model_instance><metadata key="object_id" value="7"/></model_instance></plate>
            </config>'''
            plate_json = {"is_seq_print": True, "bbox_objects": [
                {"name": "model_Trail", "bbox": [10, 187, 84, 213]},
                {"name": "model", "bbox": [153, 152, 253, 239]},
                {"name": "assemble", "bbox": [25, 6, 145, 111]},
            ]}
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", model)
                archive.writestr("Metadata/plate_1.json", json.dumps(plate_json))
            filaments = tuple({"number": n, "name": f"F{n}", "color": color}
                              for n, color in enumerate(("#000000", "#FFFFFF", "#FF0000", "#00AA00"), 1))
            matrix = tuple(0 if row == column else 100
                           for row in range(4) for column in range(4))
            environment = BambuEnvironment(
                Path("bambu"), Path("data"), "machine", "process", filaments,
                0.2, matrix, (0,) * 8)

            bands = [{"role": "mountain", "min_z": 8.0, "max_z": 17.0},
                     {"role": "summit", "min_z": 17.0, "max_z": 20.0}]
            terrain_filaments = {"land": 4, "mountain": 1, "summit": 2}
            _enforce_project_settings(output, environment, bands, terrain_filaments)
            _enforce_project_settings(output, environment, bands, terrain_filaments)
            report = validate_3mf(
                output, {"trail": 3, "terrain": 4, "plate": 1, "text": 2,
                         "attribution": 1},
                bands, terrain_filaments, water_filament=2, filament_count=4)

            self.assertTrue(report["valid"])
            with zipfile.ZipFile(output) as archive:
                final = json.loads(archive.read("Metadata/project_settings.config"))
                ranges = ET.fromstring(archive.read("Metadata/layer_config_ranges.xml"))
            self.assertEqual(final["filament_colour"], [item["color"] for item in filaments])
            self.assertEqual(float(final["flush_multiplier"][0]), 0.6)
            self.assertTrue(any(float(value) > 0
                                for value in final["flush_volumes_matrix"]))
            self.assertEqual(final["print_sequence"], "by object")
            self.assertEqual(final["wall_generator"], "arachne")
            self.assertEqual(
                final["different_settings_to_system"][0],
                "existing_override;wall_generator;enable_prime_tower")
            self.assertEqual(final["different_settings_to_system"][1], "filament_override")
            self.assertEqual(len(final["different_settings_to_system"]), 6)
            self.assertEqual(report["wall_generator"], WALL_GENERATOR)
            self.assertEqual(final["enable_prime_tower"], "0")
            self.assertEqual(final["prime_tower_enable_framework"], "0")
            self.assertEqual(final["enable_tower_interface_features"], "0")
            self.assertEqual(float(final["initial_layer_print_height"]), 0.2)
            self.assertEqual(final["first_layer_print_sequence"], ["0"])
            self.assertEqual(final["other_layers_print_sequence"], ["0"])
            self.assertEqual(final["other_layers_print_sequence_nums"], "0")
            self.assertEqual([item["filament"] for item in report["terrain_bands"]], [1, 2])
            self.assertEqual([item["layer_height"] for item in report["terrain_bands"]],
                             [0.2, 0.2])
            self.assertEqual(report["water_filament"], 2)
            self.assertEqual(ranges.find("object").attrib["id"], "2")

    def test_validates_diy_case_as_second_plate(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "diy.3mf"
            settings = {
                "enable_prime_tower": "0",
                "prime_tower_enable_framework": "0",
                "enable_tower_interface_features": "0",
                "initial_layer_print_height": "0.2",
                "flush_multiplier": ["0.6"],
                "flush_volumes_matrix": [
                    str(0 if row == column else 100)
                    for row in range(4) for column in range(4)
                ],
                "flush_volumes_vector": ["0"] * 8,
                "print_sequence": "by object",
                "wall_generator": "arachne",
                "different_settings_to_system": [
                    "wall_generator;enable_prime_tower"
                ],
                "first_layer_print_sequence": ["0"],
                "other_layers_print_sequence": ["0"],
                "other_layers_print_sequence_nums": "0",
            }
            model = '''<config>
              <object id="2"><metadata key="extruder" value="3"/><part><metadata key="source_file" value="model_Trail.stl"/></part></object>
              <object id="4"><metadata key="extruder" value="4"/><part><metadata key="source_file" value="model.stl"/><metadata key="source_object_id" value="1"/></part></object>
              <object id="7"><part><metadata key="source_file" value="model_Plate.stl"/><metadata key="extruder" value="1"/></part><part><metadata key="source_file" value="model_Text.stl"/><metadata key="extruder" value="2"/></part><part><metadata key="source_file" value="model_Attribution.stl"/><metadata key="extruder" value="3"/></part></object>
              <object id="9"><metadata key="extruder" value="3"/><part><metadata key="source_file" value="diy-case.stl"/></part></object>
              <plate><metadata key="plater_name" value="TrailPrint"/><model_instance><metadata key="object_id" value="2"/></model_instance><model_instance><metadata key="object_id" value="4"/></model_instance><model_instance><metadata key="object_id" value="7"/></model_instance></plate>
              <plate><metadata key="plater_name" value="DIY Case"/><model_instance><metadata key="object_id" value="9"/></model_instance></plate>
            </config>'''
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", model)

            report = validate_3mf(
                output,
                {"trail": 3, "terrain": 4, "plate": 1, "text": 2,
                 "attribution": 3},
                terrain_filaments={"land": 4, "mountain": 3, "summit": 2},
                filament_count=4, diy_case_filament=3)

            self.assertTrue(report["diy_kit_included"])
            self.assertEqual(report["plates"], 2)
            self.assertEqual(report["diy_case_filament"], 3)

    def test_validation_rejects_non_arachne_wall_generator(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "classic.3mf"
            settings = {
                "enable_prime_tower": "0",
                "prime_tower_enable_framework": "0",
                "enable_tower_interface_features": "0",
                "initial_layer_print_height": "0.2",
                "flush_multiplier": ["0.6"],
                "print_sequence": "by object",
                "wall_generator": "classic",
                "different_settings_to_system": [
                    "wall_generator;enable_prime_tower"
                ],
            }
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr(
                    "Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", "<config/>")

            with self.assertRaisesRegex(ValueError, "Arachne"):
                validate_3mf(output, {}, filament_count=1)

    def test_validation_rejects_inactive_arachne_wall_generator(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "inactive-arachne.3mf"
            settings = {
                "enable_prime_tower": "0",
                "prime_tower_enable_framework": "0",
                "enable_tower_interface_features": "0",
                "initial_layer_print_height": "0.2",
                "flush_multiplier": ["0.6"],
                "flush_volumes_matrix": ["0"],
                "flush_volumes_vector": ["0", "0"],
                "print_sequence": "by object",
                "wall_generator": "arachne",
                "different_settings_to_system": ["enable_prime_tower"],
                "first_layer_print_sequence": ["0"],
                "other_layers_print_sequence": ["0"],
                "other_layers_print_sequence_nums": "0",
            }
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr(
                    "Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", "<config/>")

            with self.assertRaisesRegex(ValueError, "process override"):
                validate_3mf(output, {}, filament_count=1)

    def test_validation_rejects_inactive_disabled_prime_tower(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "inactive-prime-tower.3mf"
            settings = {
                "enable_prime_tower": "0",
                "prime_tower_enable_framework": "0",
                "enable_tower_interface_features": "0",
                "initial_layer_print_height": "0.2",
                "flush_multiplier": ["0.6"],
                "flush_volumes_matrix": ["0"],
                "flush_volumes_vector": ["0", "0"],
                "print_sequence": "by object",
                "wall_generator": "arachne",
                "different_settings_to_system": ["wall_generator"],
                "first_layer_print_sequence": ["0"],
                "other_layers_print_sequence": ["0"],
                "other_layers_print_sequence_nums": "0",
            }
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("3D/3dmodel.model", b"x" * 3000)
                archive.writestr(
                    "Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Metadata/model_settings.config", "<config/>")

            with self.assertRaisesRegex(ValueError, "prime tower setting"):
                validate_3mf(output, {}, filament_count=1)

    def test_layout_constants_are_independent_calibration_parameters(self):
        self.assertEqual(ONE_PLATE_LAYOUT["terrain"]["rotation_deg"], 0.0)
        self.assertEqual(ONE_PLATE_LAYOUT["plate_text"]["rotation_deg"], 60.0)
        self.assertAlmostEqual(ONE_PLATE_LAYOUT["trail"]["center"][0], 47.217545)

    def test_filament_numbers_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "from 1 to 4"):
            BambuSettings.validated(4, 1, 2, 3, 5)

    def test_plate_defaults_to_slot_one(self):
        filaments = (
            {"number": 1, "name": "red", "color": "#ff0000"},
            {"number": 2, "name": "white", "color": "#ffffff"},
        )
        defaults = default_filament_mapping(filaments)
        self.assertEqual(defaults["terrain"], 1)
        self.assertEqual(defaults["plate"], 1)


if __name__ == "__main__":
    unittest.main()
