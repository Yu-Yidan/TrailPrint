"""Local preferences and elevation mapping for terrain filament bands."""

import json
import math
import os
import tempfile
from pathlib import Path

METERS_PER_FOOT = 0.3048
DEFAULT_GREY_START_M = 1200.0
DEFAULT_WHITE_START_M = 2300.0
DEFAULT_TARGETS = {
    "land": (25, 145, 45),
    "mountain": (128, 128, 128),
    "summit": (255, 255, 255),
    "water": (20, 90, 220),
    "black": (10, 10, 10),
    "green": (25, 145, 45),
    "grey": (128, 128, 128),
    "white": (255, 255, 255),
}


def _rgb(value):
    value = str(value).lstrip("#")
    try:
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
    except (ValueError, TypeError):
        return (119, 119, 119)


def color_matched_defaults(filaments) -> dict:
    filaments = list(filaments)
    if not filaments:
        return {}
    defaults = {
        role: min(filaments, key=lambda item: sum(
            (channel - target) ** 2
            for channel, target in zip(_rgb(item.get("color")), color)
        ))["number"]
        for role, color in DEFAULT_TARGETS.items()
    }
    return defaults


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=str(path.parent))
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def elevation_metadata(project: Path) -> dict:
    value = _read_json(project / "build/elevation.json")
    try:
        numbers = [float(value[key]) for key in (
            "source_min_m", "source_max_m", "model_surface_min_z_mm",
            "model_surface_max_z_mm", "min_thickness_mm")]
    except (KeyError, TypeError, ValueError):
        return {}
    if not all(math.isfinite(number) for number in numbers):
        return {}
    if numbers[1] <= numbers[0] or numbers[3] <= numbers[2] or numbers[4] < 0:
        return {}
    return {**value, **dict(zip((
        "source_min_m", "source_max_m", "model_surface_min_z_mm",
        "model_surface_max_z_mm", "min_thickness_mm"), numbers))}


def settings_path(repo_root: Path) -> Path:
    return repo_root / "data/settings/bambu.json"


def project_settings_path(project: Path) -> Path:
    return project / "terrain_colors.json"


def load_terrain_defaults(repo_root: Path, project: Path, filaments) -> dict:
    filaments = list(filaments)
    fallback = {
        **color_matched_defaults(filaments),
        "grey_start_m": DEFAULT_GREY_START_M,
        "white_start_m": DEFAULT_WHITE_START_M,
    }
    count = len(filaments)
    combined = {**fallback, **_read_json(settings_path(repo_root)),
                **_read_json(project_settings_path(project))}
    combined.setdefault("green", combined.get("land"))
    combined.setdefault("grey", combined.get("mountain"))
    combined.setdefault("white", combined.get("summit"))
    for role in ("land", "mountain", "summit", "water",
                 "black", "green", "grey", "white"):
        try:
            slot = int(combined[role])
        except (KeyError, TypeError, ValueError):
            slot = fallback.get(role, 1)
        combined[role] = slot if 1 <= slot <= count else fallback.get(role, 1)
    try:
        grey, white = float(combined["grey_start_m"]), float(combined["white_start_m"])
    except (KeyError, TypeError, ValueError):
        grey, white = DEFAULT_GREY_START_M, DEFAULT_WHITE_START_M
    if not math.isfinite(grey) or not math.isfinite(white) or white <= grey:
        grey, white = DEFAULT_GREY_START_M, DEFAULT_WHITE_START_M
    combined.update(grey_start_m=grey, white_start_m=white)
    return combined


def save_terrain_preferences(repo_root: Path, project: Path, value: dict,
                             save_defaults: bool = False) -> None:
    required = ("land", "mountain", "summit", "grey_start_m", "white_start_m")
    payload = {key: value[key] for key in required}
    for key in ("black", "green", "grey", "white", "trail", "plate", "text", "palette_revision"):
        if key in value:
            payload[key] = value[key]
    if "water" in value:
        payload["water"] = value["water"]
    else:
        prior_water = _read_json(project_settings_path(project)).get("water")
        if prior_water is not None:
            payload["water"] = prior_water
    _atomic_json(project_settings_path(project), payload)
    if save_defaults:
        defaults_payload = dict(payload)
        if "water" not in defaults_payload:
            prior_water = _read_json(settings_path(repo_root)).get("water")
            if prior_water is not None:
                defaults_payload["water"] = prior_water
        _atomic_json(settings_path(repo_root), defaults_payload)


def display_elevation(value_m: float, unit_system: str) -> float:
    return value_m if str(unit_system).upper() == "METRIC" else value_m / METERS_PER_FOOT


def elevation_to_meters(value, unit_system: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Terrain elevation cutoffs must be numbers.") from exc
    if not math.isfinite(number):
        raise ValueError("Terrain elevation cutoffs must be finite numbers.")
    return number if str(unit_system).upper() == "METRIC" else number * METERS_PER_FOOT


def active_terrain_bands(elevation: dict, grey_start_m: float, white_start_m: float,
                         nominal_layer_height_mm: float = 0.2) -> list:
    minimum, maximum = elevation["source_min_m"], elevation["source_max_m"]
    if white_start_m <= grey_start_m:
        raise ValueError("Snow elevation must be higher than mountain elevation.")
    relief_m = maximum - minimum
    relief_mm = elevation["model_surface_max_z_mm"] - elevation["model_surface_min_z_mm"]
    base = elevation["min_thickness_mm"]

    def print_z(source_m):
        clamped = min(maximum, max(minimum, source_m))
        return base + ((clamped - minimum) / relief_m) * relief_mm

    top = base + relief_mm
    bands = []
    if grey_start_m < maximum:
        grey_z = print_z(grey_start_m)
        white_z = print_z(white_start_m) if white_start_m < maximum else top
        if white_z - grey_z >= nominal_layer_height_mm:
            bands.append({"role": "mountain", "min_z": grey_z, "max_z": white_z})
        if white_start_m < maximum and top - white_z >= nominal_layer_height_mm:
            bands.append({"role": "summit", "min_z": white_z, "max_z": top})
    return bands
