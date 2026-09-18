"""Create and validate reviewable Bambu Studio projects."""

import json
import math
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable

import trimesh

from .diy_case import generate_diy_case
from .mesh_validation import mesh_role
from .naming import canonical_3mf_name
from .terrain_colors import (
    DEFAULT_GREY_START_M,
    DEFAULT_WHITE_START_M,
    active_terrain_bands,
    color_matched_defaults,
    elevation_metadata,
)

BAMBU_APP = Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
BAMBU_DATA = Path.home() / "Library/Application Support/BambuStudio"
ROLE_ORDER = ("trail", "terrain", "plate", "text", "attribution")
FIRST_LAYER_HEIGHT_MM = 0.2
PURGE_MULTIPLIER = 0.6
PRINT_SEQUENCE = "by object"
OBJECT_CLEARANCE_MM = 2.0  # P1S nozzle/gantry safety margin for sequential printing.
ONE_PLATE_LAYOUT = {
    "trail": {
        "center": (47.217545, 200.106315), "rotation_deg": 157.240910,
        "envelope": ((10.117610, 187.127680), (84.317480, 213.084950)),
    },
    "terrain": {
        "center": (203.316610, 196.011910), "rotation_deg": 0.0,
        "envelope": ((153.316610, 152.710640), (253.316610, 239.313180)),
    },
    "plate_text": {
        "center": (85.062450, 58.903250), "rotation_deg": 60.0,
        "envelope": ((25.062460, 6.941720), (145.062440, 110.864780)),
    },
}


@dataclass(frozen=True)
class BambuEnvironment:
    binary: Path
    data_dir: Path
    machine: str
    process: str
    filaments: tuple
    flush_multiplier: float = PURGE_MULTIPLIER
    flush_volumes_matrix: tuple = ()
    flush_volumes_vector: tuple = ()


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
    black_filament: int = 1
    green_filament: int = 1
    grey_filament: int = 1
    white_filament: int = 1
    terrain_mode: str = "manual"
    automatic_bands: tuple = ()
    generate_diy_kit: bool = False

    @classmethod
    def validated(cls, filament_count, trail=1, land=1, mountain=1, summit=1,
                  plate=1, text=1, water=1, grey_start_m=DEFAULT_GREY_START_M,
                  white_start_m=DEFAULT_WHITE_START_M, black=1, green=1, grey=1,
                  white=1, terrain_mode="manual", automatic_bands=(),
                  generate_diy_kit=False):
        try:
            values = [int(value) for value in
                      (trail, land, mountain, summit, plate, text, water,
                       black, green, grey, white)]
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
        terrain_mode = str(terrain_mode).lower()
        if terrain_mode not in {"manual", "osm"}:
            raise ValueError("Terrain color mode must be manual or OSM-inspired.")
        bands = tuple(dict(item) for item in automatic_bands) if automatic_bands else ()
        if terrain_mode == "osm" and not bands:
            raise ValueError("OSM-inspired terrain bands are unavailable.")
        return cls(
            trail_filament=values[0], land_filament=values[1],
            mountain_filament=values[2], summit_filament=values[3],
            plate_filament=values[4], text_filament=values[5],
            water_filament=values[6], grey_start_m=grey_start_m,
            white_start_m=white_start_m, black_filament=values[7],
            green_filament=values[8], grey_filament=values[9],
            white_filament=values[10], terrain_mode=terrain_mode,
            automatic_bands=bands,
            generate_diy_kit=bool(generate_diy_kit))

    def by_role(self) -> dict:
        terrain = self.land_filament
        if self.terrain_mode == "osm" and self.automatic_bands:
            terrain = self.terrain_filaments()[self.automatic_bands[0]["role"]]
        attribution = (self.grey_filament if self.terrain_mode == "osm"
                       else self.mountain_filament)
        return dict(zip(ROLE_ORDER, (self.trail_filament, terrain,
                                     self.plate_filament, self.text_filament,
                                     attribution)))

    def terrain_filaments(self) -> dict:
        values = {"land": self.land_filament, "mountain": self.mountain_filament,
                  "summit": self.summit_filament}
        if self.terrain_mode == "osm":
            values.update(black=self.black_filament, green=self.green_filament,
                          grey=self.grey_filament, white=self.white_filament)
        return values

    def diy_case_filament(self) -> int:
        """Reuse the filament assigned to the terrain's grey rock band."""
        return (self.grey_filament if self.terrain_mode == "osm"
                else self.mountain_filament)


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
    count = len(filaments)

    def flush_values(key, expected):
        raw = presets.get(key, "")
        values = str(raw).split("|") if isinstance(raw, str) else list(raw)
        try:
            parsed = tuple(float(value) for value in values)
        except (TypeError, ValueError) as exc:
            raise ValueError("Bambu Studio's flushing volumes are invalid.") from exc
        if len(parsed) != expected or any(not math.isfinite(value) or value < 0
                                          for value in parsed):
            raise ValueError("Bambu Studio's flushing volumes do not match its filaments.")
        return parsed

    matrix = flush_values("flush_volumes_matrix", count * count)
    vector = flush_values("flush_volumes_vector", count * 2)
    return BambuEnvironment(binary, data_dir, machine, process, filaments,
                            PURGE_MULTIPLIER, matrix, vector)


def default_filament_mapping(filaments: Iterable[dict]) -> dict:
    filaments = list(filaments)
    targets = {"trail": (220, 25, 20), "terrain": (25, 145, 45),
               "plate": (0, 0, 0), "text": (255, 255, 255),
               "attribution": (128, 128, 128),
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
        defaults["plate"] = 1
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
    required = ("trail", "terrain", "plate", "text")
    missing = [role for role in required if role not in files]
    if missing:
        raise ValueError(
            "Bambu export requires validated Terrain, Trail, Plate, and Text STL files.")
    files["water"] = sorted(water)
    return files


def _object(path: Path, filament: int, assembly=0, position=(0.0, 0.0),
            position_z=0.0) -> dict:
    return {"path": str(path), "count": 1, "filaments": [filament],
            "assemble_index": [assembly], "pos_x": [position[0]],
            "pos_y": [position[1]], "pos_z": [position_z],
            "print_params": {}}


def _resting_z(path: Path) -> float:
    """Return the Z translation that places an STL's lowest point on the bed."""
    try:
        mesh = trimesh.load(path, force="mesh", process=False)
        if isinstance(mesh, trimesh.Trimesh) and not mesh.is_empty:
            return -float(mesh.bounds[0][2])
    except (OSError, ValueError, TypeError):
        pass
    return 0.0


def assemble_manifest(files: dict, settings: BambuSettings, filament_count: int,
                      positions=None, diy_case: Path = None) -> dict:
    positions = positions or {role: (0.0, 0.0)
                              for role in ("trail", "terrain", "plate_text")}
    values = settings.by_role()
    terrain_assembly = 2 if files.get("water") else 0
    terrain_objects = [
        _object(files["terrain"], values["terrain"], terrain_assembly,
                positions["terrain"]),
    ]
    terrain_objects.extend(
        _object(path, settings.water_filament, terrain_assembly,
                positions["terrain"])
        for path in files.get("water", ()))
    plate_objects = [
        _object(files["plate"], values["plate"], 1, positions["plate_text"]),
        _object(files["text"], values["text"], 1, positions["plate_text"]),
    ]
    if files.get("attribution"):
        plate_objects.append(_object(
            files["attribution"], values["attribution"], 1,
            positions["plate_text"]))
    plates = [{
        "plate_name": "TrailPrint", "need_arrange": False,
        "filaments_count": filament_count, "plate_params": {},
        "objects": [
            _object(files["trail"], values["trail"],
                    position=positions["trail"],
                    position_z=_resting_z(files["trail"])),
            *terrain_objects,
            *plate_objects,
        ],
    }]
    if diy_case is not None:
        mesh = trimesh.load(diy_case, force="mesh", process=False)
        center = mesh.bounds[:, :2].mean(axis=0)
        plates.append({
            "plate_name": "DIY Case", "need_arrange": False,
            "filaments_count": filament_count,
            "plate_params": {"enable_support": "0"},
            "objects": [_object(
                diy_case, settings.diy_case_filament(),
                position=(128.0 - float(center[0]), 128.0 - float(center[1]))
            )],
        })
    return {"plates": plates}


def prepare_one_plate_layout(files: dict, temp_root: Path) -> tuple:
    """Rotate temporary mesh copies and place their bounds at calibrated centers."""
    prepared = dict(files)
    positions = {}
    report = {}

    def rotate_copy(role, path, rotation_deg):
        mesh = trimesh.load(path, force="mesh", process=False)
        if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
            raise ValueError(f"{role.title()} STL could not be prepared for one plate.")
        angle = math.radians(rotation_deg)
        transform = [
            [math.cos(angle), -math.sin(angle), 0, 0],
            [math.sin(angle), math.cos(angle), 0, 0],
            [0, 0, 1, 0], [0, 0, 0, 1],
        ]
        mesh.apply_transform(transform)
        # Bambu Studio may rewrite assembly Z transforms while applying
        # --ensure-on-bed. Normalize the standalone Trail mesh itself so its
        # lowest exported point is exactly on the print bed regardless of that
        # rewrite. Terrain keeps its original Z reference.
        if role == "trail":
            mesh.apply_translation([0.0, 0.0, -float(mesh.bounds[0][2])])
        target = temp_root / f"layout-{Path(path).name}"
        mesh.export(target)
        return target, mesh.bounds

    rotated_bounds = {}
    for role in ROLE_ORDER:
        if role not in files:
            continue
        layout_role = ("plate_text" if role in {"plate", "text", "attribution"}
                       else role)
        target, bounds = rotate_copy(
            role, files[role], ONE_PLATE_LAYOUT[layout_role]["rotation_deg"])
        prepared[role] = target
        rotated_bounds[role] = bounds

    prepared_water = []
    water_bounds = []
    for path in files.get("water", ()):
        target, bounds = rotate_copy(
            "water", path, ONE_PLATE_LAYOUT["terrain"]["rotation_deg"])
        prepared_water.append(target)
        water_bounds.append(bounds)
    prepared["water"] = prepared_water

    groups = {
        "trail": [rotated_bounds["trail"]],
        "terrain": [rotated_bounds["terrain"], *water_bounds],
        "plate_text": [rotated_bounds["plate"], rotated_bounds["text"],
                       *([rotated_bounds["attribution"]]
                         if "attribution" in rotated_bounds else [])],
    }
    placed_rectangles = {}
    for layout_role, bounds_group in groups.items():
        minimum = [min(float(bounds[0][axis]) for bounds in bounds_group)
                   for axis in (0, 1)]
        maximum = [max(float(bounds[1][axis]) for bounds in bounds_group)
                   for axis in (0, 1)]
        center = tuple((minimum[axis] + maximum[axis]) / 2 for axis in (0, 1))
        target_center = ONE_PLATE_LAYOUT[layout_role]["center"]
        position = tuple(target_center[axis] - center[axis] for axis in (0, 1))
        positions[layout_role] = position
        placed = ((minimum[0] + position[0], minimum[1] + position[1]),
                  (maximum[0] + position[0], maximum[1] + position[1]))
        if any(placed[0][axis] < 0 or placed[1][axis] > 256 for axis in (0, 1)):
            raise ValueError(
                f"{layout_role.replace('_', ' ').title()} is outside the print bed.")
        placed_rectangles[layout_role] = placed
        report[layout_role] = {
            "center_mm": [round(value, 6) for value in target_center],
            "rotation_deg": ONE_PLATE_LAYOUT[layout_role]["rotation_deg"],
            "bounds_mm": [[round(value, 6) for value in point] for point in placed],
        }
    report["clearance_mm"] = OBJECT_CLEARANCE_MM

    roles = tuple(placed_rectangles)
    for index, left_role in enumerate(roles):
        raw_left = placed_rectangles[left_role]
        left = ((raw_left[0][0] - OBJECT_CLEARANCE_MM,
                 raw_left[0][1] - OBJECT_CLEARANCE_MM),
                (raw_left[1][0] + OBJECT_CLEARANCE_MM,
                 raw_left[1][1] + OBJECT_CLEARANCE_MM))
        for right_role in roles[index + 1:]:
            raw_right = placed_rectangles[right_role]
            right = ((raw_right[0][0] - OBJECT_CLEARANCE_MM,
                      raw_right[0][1] - OBJECT_CLEARANCE_MM),
                     (raw_right[1][0] + OBJECT_CLEARANCE_MM,
                      raw_right[1][1] + OBJECT_CLEARANCE_MM))
            overlap = (left[0][0] < right[1][0] and left[1][0] > right[0][0]
                       and left[0][1] < right[1][1] and left[1][1] > right[0][1])
            if overlap:
                raise ValueError("Calibrated one-plate objects overlap after applying "
                                 f"the {OBJECT_CLEARANCE_MM:g} mm P1S clearance margin: "
                                 f"{left_role} and {right_role}.")
    return prepared, positions, report


def estimate_water_layer_changes(water_bounds, terrain_base_z, nominal_layer_height,
                                 initial_layer_height, settings, terrain_bands) -> dict:
    """Estimate unsliced layers where water and terrain use different filaments."""
    nominal = float(nominal_layer_height)
    initial = float(initial_layer_height)
    if not math.isfinite(nominal) or nominal <= 0:
        nominal = 0.2
    if not math.isfinite(initial) or initial <= 0:
        initial = nominal
    base = float(terrain_base_z)

    def layer_at(z_value):
        relative = max(0.0, float(z_value) - base)
        if relative <= initial:
            return 1
        return 1 + math.ceil((relative - initial) / nominal - 1e-9)

    water_layers = set()
    for minimum, maximum in water_bounds:
        if not all(math.isfinite(float(value)) for value in (minimum, maximum)):
            continue
        first, last = layer_at(minimum), layer_at(maximum)
        water_layers.update(range(first, last + 1))

    def layer_midpoint(layer):
        if layer == 1:
            return base + initial / 2
        bottom = initial + (layer - 2) * nominal
        return base + bottom + nominal / 2

    terrain_filaments = settings.terrain_filaments()
    base_filament = settings.by_role()["terrain"]

    def terrain_filament(z_value):
        filament = base_filament
        for band in terrain_bands:
            if float(band["min_z"]) <= z_value < float(band["max_z"]):
                filament = terrain_filaments[band["role"]]
        return filament

    mixed_layers = sorted(layer for layer in water_layers
                          if terrain_filament(layer_midpoint(layer))
                          != settings.water_filament)

    def ranges(layers):
        output = []
        for layer in layers:
            if not output or layer != output[-1][1] + 1:
                output.append([layer, layer])
            else:
                output[-1][1] = layer
        return output

    return {
        "method": "unsliced_nominal_layer_height",
        "is_exact": False,
        "nominal_layer_height_mm": nominal,
        "initial_layer_height_mm": initial,
        "water_layer_count": len(water_layers),
        "water_layer_ranges": ranges(sorted(water_layers)),
        "mixed_filament_layer_count": len(mixed_layers),
        "mixed_filament_layer_ranges": ranges(mixed_layers),
        "manual_swaps_max": len(mixed_layers) * 2,
    }


def _validated_mesh_bounds(project: Path) -> tuple:
    """Return terrain base Z and water Z intervals from the validated STL report."""
    try:
        report = json.loads((project / "build/validation.json").read_text(encoding="utf-8"))
        files = report["final"]["files"]
    except (OSError, KeyError, TypeError, ValueError):
        return 0.0, []
    terrain_base = 0.0
    water_bounds = []
    for item in files:
        bounds = item.get("bounds_mm", ())
        if len(bounds) != 2 or len(bounds[0]) != 3 or len(bounds[1]) != 3:
            continue
        if item.get("role") == "terrain":
            terrain_base = float(bounds[0][2])
        elif item.get("role") == "water":
            water_bounds.append((float(bounds[0][2]), float(bounds[1][2])))
    return terrain_base, water_bounds


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
    for terrain_object in root.findall("object"):
        for part in terrain_object.findall("part"):
            source_file = next((node.attrib.get("value", "").lower()
                                for node in part.findall("metadata")
                                if node.attrib.get("key") == "source_file"), "")
            source_stem = Path(source_file).stem.lower()
            if (not source_file or source_stem.endswith(
                    ("_trail", "_plate", "_text", "_attribution", "_water",
                     "ocean", "diy-case"))):
                continue
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
                              nominal_layer_height=0.2, layout_report=None) -> None:
    """Make safety and display settings explicit in the finished Bambu archive."""
    replacement = path.with_suffix(".settings.3mf")
    try:
        with zipfile.ZipFile(path, "r") as source:
            settings = json.loads(
                source.read("Metadata/project_settings.config").decode("utf-8"))
            terrain_model_index = _terrain_model_index(
                source.read("Metadata/model_settings.config"))
            count = len(environment.filaments)
            matrix = list(environment.flush_volumes_matrix)
            vector = list(environment.flush_volumes_vector)
            if len(matrix) != count * count or len(vector) != count * 2:
                raise ValueError("Bambu Studio's flushing volumes do not match its filaments.")
            settings.update({
                "enable_prime_tower": "0",
                "initial_layer_print_height": str(FIRST_LAYER_HEIGHT_MM),
                "prime_tower_enable_framework": "0",
                "enable_tower_interface_features": "0",
                "flush_multiplier": [str(PURGE_MULTIPLIER)],
                "flush_volumes_matrix": [str(value) for value in matrix],
                "flush_volumes_vector": [str(value) for value in vector],
                "print_sequence": PRINT_SEQUENCE,
                "first_layer_print_sequence": ["0"],
                "other_layers_print_sequence": ["0"],
                "other_layers_print_sequence_nums": "0",
                "filament_colour": [item["color"] for item in environment.filaments],
            })
            with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target:
                for item in source.infolist():
                    if item.filename in {"Metadata/layer_config_ranges.xml",
                                         "Metadata/plate_1.json"}:
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
                if layout_report:
                    target.writestr("Metadata/plate_1.json", json.dumps({
                        "is_seq_print": True,
                        "bbox_objects": [
                            {"name": role, "bbox": [
                                bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1]]}
                            for role, item in layout_report.items()
                            if isinstance(item, dict) and "bounds_mm" in item
                            for bounds in [item["bounds_mm"]]
                        ],
                    }, separators=(",", ":")).encode("utf-8"))
        os.replace(replacement, path)
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        raise ValueError("Generated Bambu project settings could not be finalized.") from exc
    finally:
        replacement.unlink(missing_ok=True)


def validate_3mf(path: Path, expected_filaments: dict, expected_bands=None,
                 terrain_filaments=None, water_filament=None,
                 filament_count=None, diy_case_filament=None) -> dict:
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
                missing = ", ".join(sorted(required - names))
                raise ValueError(
                    f"Generated 3MF is missing required Bambu project metadata: {missing}.")
            project_settings = json.loads(
                archive.read("Metadata/project_settings.config").decode("utf-8"))
            model_settings = archive.read("Metadata/model_settings.config").decode("utf-8")
            plate_metadata = None
            if "Metadata/plate_1.json" in names:
                plate_metadata = json.loads(
                    archive.read("Metadata/plate_1.json").decode("utf-8"))
            ranges_xml = (archive.read("Metadata/layer_config_ranges.xml").decode("utf-8")
                          if "Metadata/layer_config_ranges.xml" in names else "")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Generated Bambu project could not be validated.") from exc
    for key in ("enable_prime_tower", "prime_tower_enable_framework",
                "enable_tower_interface_features"):
        if str(project_settings.get(key)) not in {"0", "False", "false"}:
            raise ValueError("Generated Bambu project did not disable the prime tower.")
    initial_height = project_settings.get("initial_layer_print_height")
    initial_height = (initial_height[0]
                      if isinstance(initial_height, list) and initial_height
                      else initial_height)
    try:
        if abs(float(initial_height) - FIRST_LAYER_HEIGHT_MM) > 1e-9:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Generated Bambu project did not preserve the 0.20 mm first layer.")
    multiplier = project_settings.get("flush_multiplier")
    multiplier = multiplier[0] if isinstance(multiplier, list) and multiplier else multiplier
    try:
        if abs(float(multiplier) - PURGE_MULTIPLIER) > 1e-9:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Generated Bambu project did not preserve the 0.6 purge multiplier.")
    if project_settings.get("print_sequence") != PRINT_SEQUENCE:
        raise ValueError("Generated Bambu project is not configured to print by object.")
    if (project_settings.get("first_layer_print_sequence") != ["0"]
            or project_settings.get("other_layers_print_sequence") != ["0"]
            or str(project_settings.get("other_layers_print_sequence_nums")) != "0"):
        raise ValueError("Generated Bambu project has inconsistent object sequence settings.")
    matrix = project_settings.get("flush_volumes_matrix", [])
    vector = project_settings.get("flush_volumes_vector", [])
    filament_count = filament_count or int(round(math.sqrt(len(matrix))))
    try:
        matrix_values = [float(value) for value in matrix]
        vector_values = [float(value) for value in vector]
    except (TypeError, ValueError):
        raise ValueError("Generated Bambu project has invalid purge volumes.")
    if (len(matrix_values) != filament_count * filament_count
            or len(vector_values) != filament_count * 2
            or any(value < 0 or not math.isfinite(value)
                   for value in matrix_values + vector_values)
            or (filament_count > 1 and not any(value > 0 for value in matrix_values))):
        raise ValueError("Generated Bambu project did not preserve purge volumes.")
    try:
        root = ET.fromstring(model_settings)
    except ET.ParseError as exc:
        raise ValueError("Generated Bambu project has invalid model metadata.") from exc
    objects = {node.attrib["id"]: node for node in root.findall("object")}
    terrain_model_index = _terrain_model_index(model_settings)
    plates = root.findall("plate")

    def metadata(node, key):
        item = next((child for child in node.findall("metadata")
                     if child.attrib.get("key") == key), None)
        return item.attrib.get("value", "") if item is not None else ""

    expected_plate_names = (["TrailPrint", "DIY Case"] if diy_case_filament is not None
                            else ["TrailPrint"])
    plate_names = [metadata(plate, "plater_name") for plate in plates]
    if plate_names != expected_plate_names:
        raise ValueError(
            f"Generated Bambu project plates are {plate_names}, expected {expected_plate_names}.")
    map_plate = plates[0]
    if metadata(map_plate, "plater_name") != "TrailPrint":
        raise ValueError("Generated Bambu project has an unexpected plate layout.")
    ids = [metadata(instance, "object_id")
           for instance in map_plate.findall("model_instance")]
    plate_objects = [objects[item] for item in ids if item in objects]
    if len(plate_objects) != 3:
        raise ValueError(
            "One plate must contain Trail, Terrain, and Plate + Text.")

    def object_sources(obj):
        parts = obj.findall("part")
        return [metadata(part, "source_file").lower() for part in parts]

    trail_object = next((obj for obj in plate_objects
                         if any(Path(source).stem.endswith("_trail")
                                for source in object_sources(obj))), None)
    plate_object = next((obj for obj in plate_objects
                         if any(Path(source).stem.endswith("_plate")
                                for source in object_sources(obj))
                         and any(Path(source).stem.endswith("_text")
                                 for source in object_sources(obj))), None)
    terrain_object = next((obj for obj in plate_objects
                           if obj not in {trail_object, plate_object}), None)
    if None in {trail_object, terrain_object, plate_object}:
        raise ValueError("Generated Bambu project has unexpected one-plate objects.")
    def object_filament(obj):
        # Bambu Studio versions differ: extruder may be stored on the object
        # or on its individual part metadata.
        value = metadata(obj, "extruder")
        if value:
            return value
        return next((metadata(part, "extruder") for part in obj.findall("part")
                     if metadata(part, "extruder")), "")
    trail_filament = object_filament(trail_object)
    terrain_parts = terrain_object.findall("part")
    water_parts = [part for part in terrain_parts
                   if Path(metadata(part, "source_file")).stem.lower().endswith("_water")
                   or Path(metadata(part, "source_file")).stem.lower().endswith("ocean")]
    land_parts = [part for part in terrain_parts if part not in water_parts]
    terrain_filament = (metadata(land_parts[0], "extruder")
                        if water_filament is not None and land_parts
                        else object_filament(terrain_object))
    if trail_filament != str(expected_filaments["trail"]):
        raise ValueError("Trail filament assignment was not preserved "
                         f"(expected {expected_filaments['trail']}, "
                         f"got {trail_filament}).")
    if terrain_filament != str(expected_filaments["terrain"]):
        raise ValueError("Terrain filament assignment was not preserved.")
    if water_filament is not None:
        if not water_parts or not land_parts:
            raise ValueError("Terrain and Water were not combined into one multipart object.")
        if any(metadata(part, "extruder") != str(water_filament)
               for part in water_parts):
            raise ValueError("Water filament assignment was not preserved.")
    parts = plate_object.findall("part")
    if len(parts) not in {2, 3}:
        raise ValueError(
            "Plate 3 is not one object containing Plate and Text parts.")
    part_filaments = {}
    for part in parts:
        source = metadata(part, "source_file").lower()
        source_stem = Path(source).stem
        role = ("attribution" if source_stem.endswith("_attribution")
                else "text" if source_stem.endswith("_text")
                else "plate" if source_stem.endswith("_plate") else "")
        if role:
            part_filaments[role] = metadata(part, "extruder")
    expected_parts = {"plate": str(expected_filaments["plate"]),
                      "text": str(expected_filaments["text"])}
    if "attribution" in part_filaments:
        expected_parts["attribution"] = str(expected_filaments["attribution"])
    if part_filaments != expected_parts:
        raise ValueError(
            "Plate or Text filament assignment was not preserved "
            f"(expected {expected_parts}, got {part_filaments}).")
    if diy_case_filament is not None:
        case_ids = [metadata(instance, "object_id")
                    for instance in plates[1].findall("model_instance")]
        case_objects = [objects[item] for item in case_ids if item in objects]
        if len(case_objects) != 1:
            raise ValueError("The DIY Case plate must contain exactly one case object.")
        sources = object_sources(case_objects[0])
        if not any("diy-case" in Path(source).stem for source in sources):
            raise ValueError("The DIY Case plate does not contain the generated case.")
        if object_filament(case_objects[0]) != str(diy_case_filament):
            raise ValueError("The DIY case did not reuse the grey terrain-rock filament.")
    if plate_metadata is not None:
        plate_json = plate_metadata.get("bbox_objects", [])
        if not plate_metadata.get("is_seq_print") or len(plate_json) != 3:
            raise ValueError("Generated plate metadata is not object-by-object.")
        for item in plate_json:
            bounds = item.get("bbox", [])
            if (len(bounds) != 4 or bounds[0] < 0 or bounds[1] < 0
                    or bounds[2] > 256 or bounds[3] > 256):
                raise ValueError("Generated one-plate object is outside the print bed.")
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
            "attribution_included": "attribution" in part_filaments,
            "terrain_bands": actual_bands,
            "water_filament": water_filament,
            "prime_tower": False, "purge_multiplier": PURGE_MULTIPLIER,
            "print_sequence": PRINT_SEQUENCE,
            "plate_metadata": plate_metadata is not None,
            "diy_kit_included": diy_case_filament is not None,
            "diy_case_filament": diy_case_filament}


def create_bambu_project(project: Path, settings: BambuSettings,
                         environment: BambuEnvironment = None, timeout=600) -> Path:
    project = project.resolve()
    environment = environment or discover_environment()
    settings = BambuSettings.validated(
        len(environment.filaments), trail=settings.trail_filament,
        land=settings.land_filament, mountain=settings.mountain_filament,
        summit=settings.summit_filament, plate=settings.plate_filament,
        text=settings.text_filament, water=settings.water_filament,
        grey_start_m=settings.grey_start_m,
        white_start_m=settings.white_start_m, black=settings.black_filament,
        green=settings.green_filament, grey=settings.grey_filament,
        white=settings.white_filament, terrain_mode=settings.terrain_mode,
        automatic_bands=settings.automatic_bands,
        generate_diy_kit=settings.generate_diy_kit)
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
            initial_layer_height = float(process.get(
                "initial_layer_print_height", nominal_layer_height))
        except (TypeError, ValueError):
            initial_layer_height = nominal_layer_height
        try:
            elevation = elevation_metadata(project)
            if not elevation:
                raise ValueError("missing elevation metadata")
            terrain_bands = (list(settings.automatic_bands[1:])
                             if settings.terrain_mode == "osm"
                             else active_terrain_bands(
                                 elevation, settings.grey_start_m,
                                 settings.white_start_m, nominal_layer_height))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Rebuild the terrain before creating elevation colors.") from exc
        count = len(environment.filaments)
        colors = [item["color"] for item in environment.filaments]
        matrix = list(environment.flush_volumes_matrix)
        vector = list(environment.flush_volumes_vector)
        if len(matrix) != count * count or len(vector) != count * 2:
            raise ValueError("Bambu Studio's flushing volumes do not match its filaments.")
        process.update({"enable_prime_tower": "0",
                        "initial_layer_print_height": str(
                            FIRST_LAYER_HEIGHT_MM),
                        "flush_multiplier": [str(PURGE_MULTIPLIER)],
                        "flush_volumes_matrix": [str(value) for value in matrix],
                        "flush_volumes_vector": [str(value) for value in vector],
                        "print_sequence": PRINT_SEQUENCE,
                        "first_layer_print_sequence": ["0"],
                        "other_layers_print_sequence": ["0"],
                        "other_layers_print_sequence_nums": "0",
                        "prime_tower_enable_framework": "0",
                        "enable_tower_interface_features": "0",
                        "filament_colour": colors})
        initial_layer_height = FIRST_LAYER_HEIGHT_MM
        machine_path, process_path = temp_root / "machine.json", temp_root / "process.json"
        _write_json(machine_path, machine)
        _write_json(process_path, process)
        filament_paths = []
        for index_number, filament in enumerate(environment.filaments, 1):
            value = _flatten_profile(filament["name"], profile_root, index)
            target = temp_root / f"filament-{index_number}.json"
            _write_json(target, value)
            filament_paths.append(target)
        layout_files, positions, layout_report = prepare_one_plate_layout(files, temp_root)
        diy_case_path = None
        diy_case_report = None
        if settings.generate_diy_kit:
            diy_case_path = temp_root / "diy-case.stl"
            diy_case_report = generate_diy_case(
                files["trail"], diy_case_path, temp_root / "diy-case-work")
        manifest_path = temp_root / "assemble.json"
        _write_json(manifest_path, assemble_manifest(
            layout_files, settings, count, positions, diy_case_path))
        command = [str(environment.binary), "--debug", "2", "--ensure-on-bed",
                   "--allow-multicolor-oneplate",
                   "--load-assemble-list", str(manifest_path),
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
                                  settings.terrain_filaments(), nominal_layer_height,
                                  layout_report)
        water_filament = settings.water_filament if files.get("water") else None
        report = validate_3mf(staged, settings.by_role(), terrain_bands,
                              settings.terrain_filaments(), water_filament,
                              count,
                              (settings.diy_case_filament()
                               if settings.generate_diy_kit else None))
        if water_filament is not None:
            terrain_base, water_bounds = _validated_mesh_bounds(project)
            report["water_layer_estimate"] = estimate_water_layer_changes(
                water_bounds, terrain_base, nominal_layer_height,
                initial_layer_height, settings, terrain_bands)
        report["layout"] = layout_report
        if diy_case_report is not None:
            report["diy_case"] = diy_case_report
        _write_json(build_dir / "bambu-validation.json", report)
        destination = project / "deliverables" / canonical_3mf_name(project)
        os.replace(staged, destination)
        return destination
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
