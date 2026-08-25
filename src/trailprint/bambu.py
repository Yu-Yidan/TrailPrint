"""Create and validate reviewable Bambu Studio projects."""

import json
import math
import os
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from .mesh_validation import mesh_role
from .naming import canonical_3mf_name
from .terrain_colors import (DEFAULT_GREY_START_M, DEFAULT_WHITE_START_M,
                             active_terrain_bands, color_matched_defaults,
                             elevation_metadata)


BAMBU_APP = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
BAMBU_DATA = Path.home() / "Library/Application Support/BambuStudio"
ROLE_ORDER = ("trail", "terrain", "plate", "text")


@dataclass(frozen=True)
class BambuEnvironment:
    binary: Path
    data_dir: Path
    machine: str
    process: str
    filaments: tuple


@dataclass(frozen=True)
class BambuSettings:
    trail_filament: int
    land_filament: int
    mountain_filament: int
    summit_filament: int
    plate_filament: int
    text_filament: int
    water_filament: int = 1
    grey_start_m: float = DEFAULT_GREY_START_M
    white_start_m: float = DEFAULT_WHITE_START_M

    @classmethod
    def validated(cls, filament_count, trail=1, land=1, mountain=1, summit=1,
                  plate=1, text=1, water=1, grey_start_m=DEFAULT_GREY_START_M,
                  white_start_m=DEFAULT_WHITE_START_M):
        try:
            values = [int(value) for value in (trail, land, mountain, summit, plate, text, water)]
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a valid filament number for every model part.") from exc
        if not filament_count or any(value < 1 or value > filament_count for value in values):
            raise ValueError(f"Filament numbers must be from 1 to {filament_count}.")
        try:
            grey_start_m, white_start_m = float(grey_start_m), float(white_start_m)
        except (TypeError, ValueError) as exc:
            raise ValueError("Terrain elevation cutoffs must be numbers.") from exc
        if not math.isfinite(grey_start_m) or not math.isfinite(white_start_m):
            raise ValueError("Terrain elevation cutoffs must be finite numbers.")
        if white_start_m <= grey_start_m:
            raise ValueError("Snow elevation must be higher than mountain elevation.")
        return cls(*values, grey_start_m, white_start_m)

    def by_role(self) -> dict:
        return dict(zip(ROLE_ORDER, (self.trail_filament, self.land_filament,
                                     self.plate_filament, self.text_filament)))

    def terrain_filaments(self) -> dict:
        return {"land": self.land_filament, "mountain": self.mountain_filament,
                "summit": self.summit_filament}


def discover_environment(data_dir: Path = BAMBU_DATA, binary: Path = BAMBU_APP) -> BambuEnvironment:
    if not binary.is_file():
        raise ValueError("Bambu Studio was not found in Applications.")
    config_path = data_dir / "BambuStudio.conf"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        presets = config["presets"]
        machine = str(presets["machine"])
        process = str(presets["process"])
        names = list(presets["filaments"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Bambu Studio's current printer and filament presets could not be read.") from exc
    colors = str(presets.get("filament_colors", "")).split(",")
    filaments = tuple({"number": index + 1, "name": str(name),
                       "color": colors[index] if index < len(colors) else "#777777"}
                      for index, name in enumerate(names))
    if not filaments:
        raise ValueError("Configure at least one filament in Bambu Studio first.")
    return BambuEnvironment(binary, data_dir, machine, process, filaments)


def default_filament_mapping(filaments: Iterable[dict]) -> dict:
    filaments = list(filaments)
    targets = {"trail": (220, 25, 20), "terrain": (25, 145, 45),
               "plate": (0, 0, 0), "text": (255, 255, 255),
               "water": (20, 90, 220)}

    def rgb(value):
        value = str(value).lstrip("#")
        try:
            return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
        except (ValueError, TypeError):
            return (119, 119, 119)

    defaults = {role: min(filaments,
                      key=lambda item: sum((channel - target) ** 2
                                           for channel, target in zip(rgb(item["color"]), color)))["number"]
            for role, color in targets.items()} if filaments else {}
    if defaults:
        defaults["terrain"] = 1
        defaults["plate"] = min(3, len(filaments))
        terrain = color_matched_defaults(filaments)
        defaults.update(terrain)
    return defaults


def _profile_index(root: Path) -> Dict[str, Path]:
    index = {}
    for path in root.rglob("*.json"):
        index.setdefault(path.stem, path)
        try:
            name = json.loads(path.read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError):
            continue
        if name:
            index.setdefault(str(name), path)
    return index


def _flatten_profile(name: str, profile_root: Path, index=None, stack=()) -> dict:
    index = index or _profile_index(profile_root)
    path = index.get(name)
    if path is None:
        raise ValueError(f"Bambu preset '{name}' was not found locally.")
    if name in stack:
        raise ValueError(f"Bambu preset inheritance is circular at '{name}'.")
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Bambu preset '{name}' could not be read.") from exc
    parent = current.get("inherits")
    merged = _flatten_profile(str(parent), profile_root, index, stack + (name,)) if parent else {}
    merged.update(current)
    merged.pop("inherits", None)
    return merged


def _role_files(project: Path) -> dict:
    files = {}
    water = []
    for path in (project / "deliverables").glob("*.stl"):
        role = mesh_role(path, project.name)
        if role == "water":
            water.append(path.resolve())
            continue
        if role in ROLE_ORDER:
            files[role] = path.resolve()
    missing = [role for role in ROLE_ORDER if role not in files]
    if missing:
        raise ValueError("Bambu export requires validated Terrain, Trail, Plate, and Text STL files.")
    files["water"] = sorted(water)
    return files


def _object(path: Path, filament: int, assembly=0) -> dict:
    return {"path": str(path), "count": 1, "filaments": [filament],
            "assemble_index": [assembly], "pos_x": [0], "pos_y": [0], "pos_z": [0],
            "print_params": {}}


def assemble_manifest(files: dict, settings: BambuSettings, filament_count: int) -> dict:
    values = settings.by_role()
    plates = [
        {"plate_name": "Trail", "need_arrange": True, "filaments_count": filament_count,
         "plate_params": {}, "objects": [_object(files["trail"], values["trail"])]},
        {"plate_name": "Terrain", "need_arrange": True, "filaments_count": filament_count,
         "plate_params": {}, "objects": [_object(files["terrain"], values["terrain"])]},
        {"plate_name": "Plate + Text", "need_arrange": True, "filaments_count": filament_count,
         "plate_params": {}, "objects": [_object(files["plate"], values["plate"], 1),
                                           _object(files["text"], values["text"], 1)]},
    ]
    if files.get("water"):
        plates.append({
            "plate_name": "Water", "need_arrange": True,
            "filaments_count": filament_count, "plate_params": {},
            "objects": [_object(path, settings.water_filament, 2)
                        for path in files["water"]],
        })
    return {"plates": plates}


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _terrain_ranges_xml(bands: list, filaments: dict, model_index="2",
                        layer_height=0.2) -> bytes:
    root = ET.Element("objects")
    terrain = ET.SubElement(root, "object", {"id": str(model_index)})
    for band in bands:
        item = ET.SubElement(terrain, "range", {
            "min_z": f"{band['min_z']:.6f}", "max_z": f"{band['max_z']:.6f}",
        })
        option = ET.SubElement(item, "option", {"opt_key": "extruder"})
        option.text = str(filaments[band["role"]])
        height = ET.SubElement(item, "option", {"opt_key": "layer_height"})
        height.text = str(layer_height)
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="utf-8") + b"\n")


def _terrain_model_index(model_settings) -> str:
    """Resolve Terrain's one-based Bambu model index for layer ranges."""
    try:
        root = ET.fromstring(model_settings)
    except ET.ParseError as exc:
        raise ValueError("Generated Bambu project has invalid model metadata.") from exc
    terrain_object_id = None
    for plate in root.findall("plate"):
        name = next((node.attrib.get("value") for node in plate.findall("metadata")
                     if node.attrib.get("key") == "plater_name"), None)
        if name != "Terrain":
            continue
        instance = plate.find("model_instance")
        if instance is not None:
            object_id = next((node.attrib.get("value")
                              for node in instance.findall("metadata")
                              if node.attrib.get("key") == "object_id"), None)
            if object_id:
                terrain_object_id = object_id
                break
    terrain_object = next((node for node in root.findall("object")
                           if node.attrib.get("id") == terrain_object_id), None)
    if terrain_object is not None:
        part = terrain_object.find("part")
        if part is not None:
            source_object_id = next((node.attrib.get("value")
                                     for node in part.findall("metadata")
                                     if node.attrib.get("key") == "source_object_id"), None)
            try:
                if source_object_id is not None and int(source_object_id) >= 0:
                    return str(int(source_object_id) + 1)
            except ValueError:
                pass
    raise ValueError("Generated Bambu project does not identify Terrain's model index.")


def _enforce_project_settings(path: Path, environment: BambuEnvironment,
                              terrain_bands=None, terrain_filaments=None,
                              nominal_layer_height=0.2) -> None:
    """Make safety and display settings explicit in the finished Bambu archive."""
    replacement = path.with_suffix(".settings.3mf")
    try:
        with zipfile.ZipFile(path, "r") as source:
            settings = json.loads(
                source.read("Metadata/project_settings.config").decode("utf-8"))
            terrain_model_index = _terrain_model_index(
                source.read("Metadata/model_settings.config"))
            count = len(environment.filaments)
            settings.update({
                "enable_prime_tower": "0",
                "flush_multiplier": "0",
                "flush_volumes_matrix": ["0" for _ in range(count * count)],
                "flush_volumes_vector": ["0" for _ in range(count * 2)],
                "filament_colour": [item["color"] for item in environment.filaments],
            })
            with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target:
                for item in source.infolist():
                    if item.filename == "Metadata/layer_config_ranges.xml":
                        continue
                    data = (json.dumps(settings, separators=(",", ":")).encode("utf-8")
                            if item.filename == "Metadata/project_settings.config"
                            else source.read(item.filename))
                    target.writestr(item, data)
                if terrain_bands:
                    target.writestr("Metadata/layer_config_ranges.xml",
                                    _terrain_ranges_xml(
                                        terrain_bands, terrain_filaments,
                                        terrain_model_index,
                                        nominal_layer_height))
        os.replace(replacement, path)
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        raise ValueError("Generated Bambu project settings could not be finalized.") from exc
    finally:
        replacement.unlink(missing_ok=True)


def validate_3mf(path: Path, expected_filaments: dict, expected_bands=None,
                 terrain_filaments=None, water_filament=None) -> dict:
    if not path.is_file() or path.stat().st_size < 1000:
        raise ValueError("Bambu Studio did not produce a usable 3MF project.")
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            names = set(archive.namelist())
            if bad:
                raise ValueError(f"Generated 3MF contains a corrupt member: {bad}")
            required = {"3D/3dmodel.model", "Metadata/project_settings.config",
                        "Metadata/model_settings.config"}
            if not required.issubset(names):
                raise ValueError("Generated 3MF is missing required Bambu project metadata.")
            project_settings = json.loads(
                archive.read("Metadata/project_settings.config").decode("utf-8"))
            model_settings = archive.read("Metadata/model_settings.config").decode("utf-8")
            ranges_xml = (archive.read("Metadata/layer_config_ranges.xml").decode("utf-8")
                          if "Metadata/layer_config_ranges.xml" in names else "")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Generated Bambu project could not be validated.") from exc
    if str(project_settings.get("enable_prime_tower")) not in {"0", "False", "false"}:
        raise ValueError("Generated Bambu project did not disable the prime tower.")
    matrix = project_settings.get("flush_volumes_matrix", [])
    if not matrix or any(float(value) != 0 for value in matrix):
        raise ValueError("Generated Bambu project did not preserve zero purge volumes.")
    try:
        root = ET.fromstring(model_settings)
    except ET.ParseError as exc:
        raise ValueError("Generated Bambu project has invalid model metadata.") from exc
    objects = {node.attrib["id"]: node for node in root.findall("object")}
    terrain_model_index = _terrain_model_index(model_settings)
    plates = root.findall("plate")
    expected_plate_count = 4 if water_filament is not None else 3
    if len(plates) != expected_plate_count:
        raise ValueError(
            f"Generated Bambu project has {len(plates)} plates instead of "
            f"{expected_plate_count}.")

    def metadata(node, key):
        item = next((child for child in node.findall("metadata")
                     if child.attrib.get("key") == key), None)
        return item.attrib.get("value", "") if item is not None else ""

    expected_plates = (("Trail", "Terrain", "Plate + Text", "Water")
                       if water_filament is not None
                       else ("Trail", "Terrain", "Plate + Text"))
    if tuple(metadata(plate, "plater_name") for plate in plates) != expected_plates:
        raise ValueError("Generated Bambu project has an unexpected plate layout.")
    plate_objects = []
    for plate in plates:
        ids = [metadata(instance, "object_id") for instance in plate.findall("model_instance")]
        plate_objects.append([objects[item] for item in ids if item in objects])
    if [len(items) for items in plate_objects] != [1] * expected_plate_count:
        raise ValueError("Plate and Text were not combined into one multipart object.")
    if metadata(plate_objects[0][0], "extruder") != str(expected_filaments["trail"]):
        raise ValueError("Trail filament assignment was not preserved.")
    if metadata(plate_objects[1][0], "extruder") != str(expected_filaments["terrain"]):
        raise ValueError("Terrain filament assignment was not preserved.")
    parts = plate_objects[2][0].findall("part")
    if len(parts) != 2:
        raise ValueError("Plate 3 is not one object containing Plate and Text parts.")
    part_filaments = {}
    for part in parts:
        source = metadata(part, "source_file").lower()
        role = "text" if "text" in source else "plate" if "plate" in source else ""
        if role:
            part_filaments[role] = metadata(part, "extruder")
    if part_filaments != {"plate": str(expected_filaments["plate"]),
                         "text": str(expected_filaments["text"])}:
        raise ValueError("Plate or Text filament assignment was not preserved.")
    if water_filament is not None:
        water_object = plate_objects[3][0]
        water_parts = water_object.findall("part")
        assigned = ([metadata(part, "extruder") for part in water_parts]
                    if water_parts else [metadata(water_object, "extruder")])
        if not assigned or any(value != str(water_filament) for value in assigned):
            raise ValueError("Water filament assignment was not preserved.")
    expected_bands = expected_bands or []
    actual_bands = []
    if ranges_xml:
        try:
            ranges_root = ET.fromstring(ranges_xml)
            terrain_ranges = next((item for item in ranges_root.findall("object")
                                   if item.attrib.get("id") == terrain_model_index), None)
            for item in terrain_ranges.findall("range") if terrain_ranges is not None else []:
                option = next((node for node in item.findall("option")
                               if node.attrib.get("opt_key") == "extruder"), None)
                height = next((node for node in item.findall("option")
                               if node.attrib.get("opt_key") == "layer_height"), None)
                actual_bands.append({
                    "min_z": float(item.attrib["min_z"]),
                    "max_z": float(item.attrib["max_z"]),
                    "filament": int(option.text) if option is not None else 0,
                    "layer_height": float(height.text) if height is not None else 0,
                })
        except (ET.ParseError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Generated terrain color ranges are invalid.") from exc
    if len(actual_bands) != len(expected_bands):
        raise ValueError("Generated terrain color ranges were not preserved.")
    for actual, expected in zip(actual_bands, expected_bands):
        if (abs(actual["min_z"] - expected["min_z"]) > 1e-5
                or abs(actual["max_z"] - expected["max_z"]) > 1e-5
                or actual["min_z"] >= actual["max_z"]
                or actual["layer_height"] <= 0
                or actual["filament"] != terrain_filaments[expected["role"]]):
            raise ValueError("Generated terrain color ranges were not preserved.")
    return {"valid": True, "plates": len(plates), "filaments": expected_filaments,
            "terrain_bands": actual_bands,
            "water_filament": water_filament,
            "prime_tower": False, "purge_volumes": "zero"}


def create_bambu_project(project: Path, settings: BambuSettings,
                         environment: BambuEnvironment = None, timeout=600) -> Path:
    project = project.resolve()
    environment = environment or discover_environment()
    settings = BambuSettings.validated(
        len(environment.filaments), settings.trail_filament, settings.land_filament,
        settings.mountain_filament, settings.summit_filament,
        settings.plate_filament, settings.text_filament, settings.water_filament,
        settings.grey_start_m, settings.white_start_m)
    files = _role_files(project)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="bambu-", dir=str(build_dir)))
    staged = temp_root / canonical_3mf_name(project)
    try:
        profile_root = environment.data_dir / "system/BBL"
        index = _profile_index(profile_root)
        machine = _flatten_profile(environment.machine, profile_root, index)
        process = _flatten_profile(environment.process, profile_root, index)
        try:
            nominal_layer_height = float(process.get("layer_height", 0.2))
        except (TypeError, ValueError):
            nominal_layer_height = 0.2
        try:
            elevation = elevation_metadata(project)
            if not elevation:
                raise ValueError("missing elevation metadata")
            terrain_bands = active_terrain_bands(
                elevation, settings.grey_start_m, settings.white_start_m,
                nominal_layer_height)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Rebuild the terrain before creating elevation colors.") from exc
        count = len(environment.filaments)
        colors = [item["color"] for item in environment.filaments]
        process.update({"enable_prime_tower": "0", "flush_multiplier": "0",
                        "flush_volumes_matrix": ["0" for _ in range(count * count)],
                        "flush_volumes_vector": ["0" for _ in range(count * 2)],
                        "filament_colour": colors})
        machine_path, process_path = temp_root / "machine.json", temp_root / "process.json"
        _write_json(machine_path, machine); _write_json(process_path, process)
        filament_paths = []
        for index_number, filament in enumerate(environment.filaments, 1):
            value = _flatten_profile(filament["name"], profile_root, index)
            target = temp_root / f"filament-{index_number}.json"
            _write_json(target, value); filament_paths.append(target)
        manifest_path = temp_root / "assemble.json"
        _write_json(manifest_path, assemble_manifest(files, settings, count))
        command = [str(environment.binary), "--debug", "2", "--ensure-on-bed",
                   "--allow-multicolor-oneplate", "--load-assemble-list", str(manifest_path),
                   "--load-settings", f"{machine_path};{process_path}",
                   "--load-filaments", ";".join(map(str, filament_paths)),
                   "--export-3mf", str(staged)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        (build_dir / "bambu-studio.log").write_text(
            f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}", encoding="utf-8")
        if result.returncode != 0:
            detail = next((line for line in reversed((result.stderr + result.stdout).splitlines())
                           if line.strip()), "Bambu Studio exited without details.")
            raise ValueError(f"Bambu Studio project export failed: {detail}")
        _enforce_project_settings(staged, environment, terrain_bands,
                                  settings.terrain_filaments(), nominal_layer_height)
        water_filament = settings.water_filament if files.get("water") else None
        report = validate_3mf(staged, settings.by_role(), terrain_bands,
                              settings.terrain_filaments(), water_filament)
        _write_json(build_dir / "bambu-validation.json", report)
        destination = project / "deliverables" / canonical_3mf_name(project)
        os.replace(staged, destination)
        return destination
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
