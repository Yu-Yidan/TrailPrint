"""Create a two-plate Bambu Studio project for a flat coaster."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .bambu import (BambuEnvironment, _flatten_profile, _object, _profile_index,
                    _write_json, discover_environment)


TERRAIN_ROLES = ("black", "green", "grey", "white", "blue")
TARGET_COLORS = {
    "trail": (220, 25, 20), "black": (10, 10, 10), "green": (25, 145, 45),
    "grey": (128, 128, 128), "white": (255, 255, 255), "blue": (20, 90, 220),
}


def default_coaster_mapping(filaments) -> dict:
    filaments = list(filaments)
    if not filaments:
        return {}

    def rgb(value):
        value = str(value).lstrip("#")
        try:
            return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
        except (TypeError, ValueError):
            return (119, 119, 119)

    return {role: min(filaments, key=lambda item: sum(
        (channel - target) ** 2
        for channel, target in zip(rgb(item.get("color")), wanted)
    ))["number"] for role, wanted in TARGET_COLORS.items()}


@dataclass(frozen=True)
class CoasterBambuSettings:
    trail_filament: int
    black_filament: int
    green_filament: int
    grey_filament: int
    white_filament: int
    blue_filament: int

    @classmethod
    def validated(cls, filament_count, trail=1, black=1, green=1, grey=1,
                  white=1, blue=1):
        try:
            values = [int(value) for value in (trail, black, green, grey, white, blue)]
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a valid filament for every coaster color.") from exc
        if not filament_count or any(value < 1 or value > filament_count for value in values):
            raise ValueError(f"Coaster filament numbers must be from 1 to {filament_count}.")
        return cls(*values)

    def by_role(self):
        return {
            "trail": self.trail_filament, "black": self.black_filament,
            "green": self.green_filament, "grey": self.grey_filament,
            "white": self.white_filament, "blue": self.blue_filament,
        }


def coaster_3mf_name(project: Path) -> str:
    return f"{project.name}-coaster.3mf"


def _coaster_files(project: Path) -> dict:
    directory = project / "deliverables" / "coaster"
    files = {role: [] for role in (*TERRAIN_ROLES, "trail")}
    for path in directory.glob("*.stl"):
        lower = path.stem.lower()
        role = next((item for item in (*TERRAIN_ROLES, "trail")
                     if f"_{item}" in lower), None)
        if role:
            files[role].append(path.resolve())
    missing = [role for role in ("black", "green", "trail") if not files[role]]
    if missing:
        raise ValueError("Build the flat coaster before creating its Bambu project.")
    return files


def assemble_coaster_manifest(files: dict, settings: CoasterBambuSettings,
                              filament_count: int) -> dict:
    roles = settings.by_role()
    terrain = [_object(path, roles[role], 1)
               for role in TERRAIN_ROLES for path in files.get(role, [])]
    return {"plates": [
        {"plate_name": "Coaster Trail", "need_arrange": True,
         "filaments_count": filament_count, "plate_params": {},
         "objects": [_object(path, roles["trail"]) for path in files["trail"]]},
        {"plate_name": "Coaster Terrain", "need_arrange": True,
         "filaments_count": filament_count, "plate_params": {},
         "objects": terrain},
    ]}


def _metadata(node, key):
    item = next((child for child in node.findall("metadata")
                 if child.attrib.get("key") == key), None)
    return item.attrib.get("value", "") if item is not None else ""


def validate_coaster_3mf(path: Path, expected: dict, present_roles: set) -> dict:
    if not path.is_file() or path.stat().st_size < 1000:
        raise ValueError("Bambu Studio did not create a usable coaster 3MF.")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip():
                raise ValueError("The coaster 3MF archive is corrupt.")
            names = set(archive.namelist())
            required = {"3D/3dmodel.model", "Metadata/project_settings.config",
                        "Metadata/model_settings.config"}
            if not required.issubset(names):
                raise ValueError("The coaster 3MF is missing Bambu project metadata.")
            settings = json.loads(archive.read("Metadata/project_settings.config"))
            root = ET.fromstring(archive.read("Metadata/model_settings.config"))
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError,
            json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("The coaster"):
            raise
        raise ValueError("The coaster 3MF could not be validated.") from exc
    if str(settings.get("enable_prime_tower")) not in {"0", "False", "false"}:
        raise ValueError("The coaster 3MF did not disable the prime tower.")
    if str(settings.get("wall_generator", "")).lower() != "arachne":
        raise ValueError("The coaster 3MF did not preserve the Arachne wall generator.")
    plates = root.findall("plate")
    if [_metadata(item, "plater_name") for item in plates] != [
            "Coaster Trail", "Coaster Terrain"]:
        raise ValueError("The coaster 3MF does not contain the expected two plates.")
    objects = {node.attrib["id"]: node for node in root.findall("object")}
    plate_objects = []
    for plate in plates:
        ids = [_metadata(instance, "object_id") for instance in plate.findall("model_instance")]
        plate_objects.append([objects[item] for item in ids if item in objects])
    if [len(items) for items in plate_objects] != [1, 1]:
        raise ValueError("The coaster terrain was not kept as one aligned multipart object.")
    if _metadata(plate_objects[0][0], "extruder") != str(expected["trail"]):
        raise ValueError("The coaster trail filament assignment was not preserved.")
    actual = {}
    terrain = plate_objects[1][0]
    parts = terrain.findall("part") or [terrain]
    for part in parts:
        source = _metadata(part, "source_file").lower()
        role = next((item for item in TERRAIN_ROLES if item in source), None)
        if role:
            actual[role] = int(_metadata(part, "extruder") or 0)
    wanted = {role: expected[role] for role in present_roles if role in TERRAIN_ROLES}
    if actual != wanted:
        raise ValueError("The coaster terrain filament assignments were not preserved.")
    return {"valid": True, "plates": 2, "filaments": expected,
            "terrain_roles": sorted(wanted), "prime_tower": False,
            "purge_volumes": "zero", "wall_generator": "arachne"}


def _enforce_coaster_settings(path: Path, environment: BambuEnvironment):
    replacement = path.with_suffix(".settings.3mf")
    try:
        with zipfile.ZipFile(path) as source:
            settings = json.loads(source.read("Metadata/project_settings.config"))
            count = len(environment.filaments)
            settings.update({
                "enable_prime_tower": "0", "flush_multiplier": "0",
                "wall_generator": "arachne", "detect_thin_wall": "1",
                "flush_volumes_matrix": ["0" for _ in range(count * count)],
                "flush_volumes_vector": ["0" for _ in range(count * 2)],
                "filament_colour": [item["color"] for item in environment.filaments],
            })
            with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target:
                for item in source.infolist():
                    data = (json.dumps(settings, separators=(",", ":")).encode()
                            if item.filename == "Metadata/project_settings.config"
                            else source.read(item.filename))
                    target.writestr(item, data)
        os.replace(replacement, path)
    finally:
        replacement.unlink(missing_ok=True)


def create_coaster_bambu_project(project: Path, settings: CoasterBambuSettings,
                                  environment: BambuEnvironment = None,
                                  timeout=600) -> Path:
    project = project.resolve()
    environment = environment or discover_environment()
    settings = CoasterBambuSettings.validated(len(environment.filaments),
        settings.trail_filament, settings.black_filament, settings.green_filament,
        settings.grey_filament, settings.white_filament, settings.blue_filament)
    files = _coaster_files(project)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="coaster-bambu-", dir=str(build_dir)))
    staged = temp_root / coaster_3mf_name(project)
    try:
        profile_root = environment.data_dir / "system/BBL"
        index = _profile_index(profile_root)
        machine = _flatten_profile(environment.machine, profile_root, index)
        process = _flatten_profile(environment.process, profile_root, index)
        count = len(environment.filaments)
        colors = [item["color"] for item in environment.filaments]
        process.update({"enable_prime_tower": "0", "flush_multiplier": "0",
                        "wall_generator": "arachne", "detect_thin_wall": "1",
                        "flush_volumes_matrix": ["0" for _ in range(count * count)],
                        "flush_volumes_vector": ["0" for _ in range(count * 2)],
                        "filament_colour": colors})
        machine_path, process_path = temp_root / "machine.json", temp_root / "process.json"
        _write_json(machine_path, machine)
        _write_json(process_path, process)
        filament_paths = []
        for number, filament in enumerate(environment.filaments, 1):
            target = temp_root / f"filament-{number}.json"
            _write_json(target, _flatten_profile(filament["name"], profile_root, index))
            filament_paths.append(target)
        manifest_path = temp_root / "assemble.json"
        _write_json(manifest_path, assemble_coaster_manifest(files, settings, count))
        command = [str(environment.binary), "--debug", "2", "--ensure-on-bed",
                   "--allow-multicolor-oneplate", "--load-assemble-list", str(manifest_path),
                   "--load-settings", f"{machine_path};{process_path}",
                   "--load-filaments", ";".join(map(str, filament_paths)),
                   "--export-3mf", str(staged)]
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
        (build_dir / "coaster-bambu-studio.log").write_text(
            f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}", encoding="utf-8")
        if result.returncode != 0:
            detail = next((line for line in reversed(
                (result.stderr + result.stdout).splitlines()) if line.strip()),
                "Bambu Studio exited without details.")
            raise ValueError(f"Bambu Studio coaster export failed: {detail}")
        _enforce_coaster_settings(staged, environment)
        report = validate_coaster_3mf(
            staged, settings.by_role(), {role for role, paths in files.items() if paths})
        (build_dir / "coaster-bambu-validation.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
        destination = project / "deliverables" / "coaster" / coaster_3mf_name(project)
        os.replace(staged, destination)
        return destination
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
