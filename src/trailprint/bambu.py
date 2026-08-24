"""Create and validate reviewable Bambu Studio projects."""

import json
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
    terrain_filament: int
    plate_filament: int
    text_filament: int

    @classmethod
    def validated(cls, filament_count, trail=1, terrain=1, plate=1, text=1):
        try:
            values = [int(value) for value in (trail, terrain, plate, text)]
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a valid filament number for every model part.") from exc
        if not filament_count or any(value < 1 or value > filament_count for value in values):
            raise ValueError(f"Filament numbers must be from 1 to {filament_count}.")
        return cls(*values)

    def by_role(self) -> dict:
        return dict(zip(ROLE_ORDER, (self.trail_filament, self.terrain_filament,
                                     self.plate_filament, self.text_filament)))


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
               "plate": (0, 0, 0), "text": (255, 255, 255)}

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
    for path in (project / "deliverables").glob("*.stl"):
        role = mesh_role(path, project.name)
        if role in ROLE_ORDER:
            files[role] = path.resolve()
    missing = [role for role in ROLE_ORDER if role not in files]
    if missing:
        raise ValueError("Bambu export requires validated Terrain, Trail, Plate, and Text STL files.")
    return files


def _object(path: Path, filament: int, assembly=0) -> dict:
    return {"path": str(path), "count": 1, "filaments": [filament],
            "assemble_index": [assembly], "pos_x": [0], "pos_y": [0], "pos_z": [0],
            "print_params": {}}


def assemble_manifest(files: dict, settings: BambuSettings, filament_count: int) -> dict:
    values = settings.by_role()
    return {"plates": [
        {"plate_name": "Trail", "need_arrange": True, "filaments_count": filament_count,
         "plate_params": {}, "objects": [_object(files["trail"], values["trail"])]},
        {"plate_name": "Terrain", "need_arrange": True, "filaments_count": filament_count,
         "plate_params": {}, "objects": [_object(files["terrain"], values["terrain"])]},
        {"plate_name": "Plate + Text", "need_arrange": True, "filaments_count": filament_count,
         "plate_params": {}, "objects": [_object(files["plate"], values["plate"], 1),
                                           _object(files["text"], values["text"], 1)]},
    ]}


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _enforce_project_settings(path: Path, environment: BambuEnvironment) -> None:
    """Make safety and display settings explicit in the finished Bambu archive."""
    replacement = path.with_suffix(".settings.3mf")
    try:
        with zipfile.ZipFile(path, "r") as source:
            settings = json.loads(
                source.read("Metadata/project_settings.config").decode("utf-8"))
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
                    data = (json.dumps(settings, separators=(",", ":")).encode("utf-8")
                            if item.filename == "Metadata/project_settings.config"
                            else source.read(item.filename))
                    target.writestr(item, data)
        os.replace(replacement, path)
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        raise ValueError("Generated Bambu project settings could not be finalized.") from exc
    finally:
        replacement.unlink(missing_ok=True)


def validate_3mf(path: Path, expected_filaments: dict) -> dict:
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
    plates = root.findall("plate")
    if len(plates) != 3:
        raise ValueError(f"Generated Bambu project has {len(plates)} plates instead of 3.")

    def metadata(node, key):
        item = next((child for child in node.findall("metadata")
                     if child.attrib.get("key") == key), None)
        return item.attrib.get("value", "") if item is not None else ""

    expected_plates = ("Trail", "Terrain", "Plate + Text")
    if tuple(metadata(plate, "plater_name") for plate in plates) != expected_plates:
        raise ValueError("Generated Bambu project has an unexpected plate layout.")
    plate_objects = []
    for plate in plates:
        ids = [metadata(instance, "object_id") for instance in plate.findall("model_instance")]
        plate_objects.append([objects[item] for item in ids if item in objects])
    if [len(items) for items in plate_objects] != [1, 1, 1]:
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
    return {"valid": True, "plates": len(plates), "filaments": expected_filaments,
            "prime_tower": False, "purge_volumes": "zero"}


def create_bambu_project(project: Path, settings: BambuSettings,
                         environment: BambuEnvironment = None, timeout=600) -> Path:
    project = project.resolve()
    environment = environment or discover_environment()
    settings = BambuSettings.validated(len(environment.filaments), **settings.by_role())
    files = _role_files(project)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="bambu-", dir=str(build_dir)))
    staged = temp_root / f"{project.name}.3mf"
    try:
        profile_root = environment.data_dir / "system/BBL"
        index = _profile_index(profile_root)
        machine = _flatten_profile(environment.machine, profile_root, index)
        process = _flatten_profile(environment.process, profile_root, index)
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
        _enforce_project_settings(staged, environment)
        report = validate_3mf(staged, settings.by_role())
        _write_json(build_dir / "bambu-validation.json", report)
        destination = project / "deliverables" / f"{project.name}.3mf"
        os.replace(staged, destination)
        return destination
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
