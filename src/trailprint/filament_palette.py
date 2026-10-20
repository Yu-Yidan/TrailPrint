"""Application-owned project filaments, independent of the active slicer project."""

import hashlib
import json
import math
import re
import threading
import zipfile
from dataclasses import asdict, replace
from pathlib import Path

from .bambu import BambuEnvironment
from .terrain_colors import _atomic_json, _read_json, project_settings_path, settings_path

_LOCK = threading.RLock()
ROLES = ("trail", "plate", "text", "land", "mountain", "summit", "water",
         "black", "green", "grey", "white")


def _payload(environment):
    value = json.loads(json.dumps(asdict(environment), default=str))
    for key in ("flush_volumes_matrix", "flush_volumes_vector"):
        value[key] = [float(v) for v in value[key]]
    value["flush_multiplier"] = float(value["flush_multiplier"])
    return value


def palette_revision(environment):
    return hashlib.sha256(json.dumps(_payload(environment), sort_keys=True).encode()).hexdigest()


def _environment(value):
    try:
        filaments = tuple(dict(item) for item in value["filaments"])
        matrix = tuple(float(v) for v in value["flush_volumes_matrix"])
        vector = tuple(float(v) for v in value["flush_volumes_vector"])
        count = len(filaments)
        if (not count or len(matrix) != count*count or len(vector) != count*2
                or any(not math.isfinite(v) or v < 0 for v in (*matrix, *vector))
                or any(item["number"] != i+1 or not item["name"]
                       or not re.fullmatch(r"#[0-9a-fA-F]{6}", item["color"])
                       for i, item in enumerate(filaments))):
            raise ValueError()
        return BambuEnvironment(Path(value["binary"]), Path(value["data_dir"]),
                                value["machine"], value["process"], filaments,
                                float(value["flush_multiplier"]), matrix, vector)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The saved filament palette is invalid. Import it from Bambu Studio.") from exc


def palette_path(root):
    return Path(root)/"data/settings/filament_palette.json"


def _recover(root, live):
    """Bootstrap from the newest compatible multicolor export when live has one color."""
    if len(live.filaments) > 1:
        return live
    candidates = sorted((Path(root)/"prints").glob("*/deliverables/*.3mf"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            with zipfile.ZipFile(path) as archive:
                settings = json.loads(archive.read("Metadata/project_settings.config"))
            names, colors = settings["filament_settings_id"], settings["filament_colour"]
            if (len(names) < 2 or len(names) != len(colors)
                    or settings["printer_settings_id"] != live.machine):
                continue
            value = _payload(replace(live, filaments=tuple(
                {"number": i+1, "name": name, "color": color}
                for i, (name, color) in enumerate(zip(names, colors))),
                flush_volumes_matrix=tuple(settings["flush_volumes_matrix"]),
                flush_volumes_vector=tuple(settings["flush_volumes_vector"])))
            return _environment(value)
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
            continue
    return live


def load_palette(root, discover):
    with _LOCK:
        path = palette_path(root)
        if path.exists():
            value = _read_json(path)
            return _environment(value.get("active", {}))
        environment = _environment(_payload(_recover(root, discover())))
        revision = palette_revision(environment)
        _atomic_json(path, {"active": _payload(environment), "legacy_revision": revision,
                            "history": {revision: _payload(environment)}})
        return environment


def import_palette(root, discover):
    with _LOCK:
        if not palette_path(root).exists():
            load_palette(root, discover)
        environment = _environment(_payload(discover()))
        value = _read_json(palette_path(root))
        value["active"] = _payload(environment)
        revision = palette_revision(environment)
        value.setdefault("history", {})[revision] = _payload(environment)
        value.setdefault("legacy_revision", revision)
        _atomic_json(palette_path(root), value)
        return environment


def role_defaults(root, project, environment, defaults):
    """Map saved slot choices by profile/color rather than by a changed slot number."""
    value = _read_json(palette_path(root))
    current = {(item["name"], item["color"].upper()): item["number"]
               for item in reversed(environment.filaments)}
    result = dict(defaults)
    for path in (settings_path(root), project_settings_path(project)):
        preferences = _read_json(path)
        revision = preferences.get("palette_revision", value.get("legacy_revision"))
        previous = value.get("history", {}).get(revision, {}).get("filaments", [])
        for role in ROLES:
            if role not in preferences:
                continue
            try:
                slot = int(preferences[role])
                item = previous[slot-1] if slot > 0 else {}
                result[role] = current.get((item["name"], item["color"].upper()))
            except (KeyError, IndexError, TypeError, ValueError):
                result[role] = None
    return result
