"""Application-owned project filaments, independent of the active slicer project."""

import hashlib
import json
import math
import re
import threading
import zipfile
import shutil
from dataclasses import asdict, replace
from pathlib import Path

from .bambu import BambuEnvironment
from .terrain_colors import _atomic_json, _read_json, project_settings_path, settings_path

_LOCK = threading.RLock()
ROLES = ("trail", "plate", "text", "land", "mountain", "summit", "water",
         "black", "green", "grey", "white")
OBJECT_DEFAULTS = {"plate": 1, "black": 1, "land": 2, "terrain": 2,
                   "green": 2, "mountain": 3, "grey": 3, "attribution": 3,
                   "text": 3, "summit": 4, "white": 4, "water": 5, "trail": 6}
OBJECT_PALETTE = (
    ("PLA Matte Charcoal", "#000000", "Matte"),
    ("PLA Matte Dark Green", "#68724D", "Matte"),
    ("PLA Matte Ash Gray", "#9B9EA0", "Matte"),
    ("PLA Basic White", "#FFFFFF", "Basic"),
    ("PLA Matte Marine Blue", "#0078BF", "Matte"),
    ("PLA Matte Scarlet Red", "#DE4343", "Matte"),
)


def load_object_palette(root, discover, force=False):
    """Install the application defaults once, retaining later project overrides."""
    with _LOCK:
        path = palette_path(root)
        saved = _read_json(path)
        if saved.get("object_defaults_version") in (1, 2) and not force:
            if saved["object_defaults_version"] == 1:
                backup = path.parent / "before-ash-gray-text"
                backup.mkdir(parents=True, exist_ok=True)
                preferences = [settings_path(root), *sorted((Path(root)/"prints").glob("*/terrain_colors.json"))]
                for preference in preferences:
                    value = _read_json(preference)
                    name = "global-bambu.json" if preference == settings_path(root) else preference.parent.name + "-terrain-colors.json"
                    if preference.exists() and not (backup/name).exists():
                        shutil.copy2(preference, backup/name)
                    value["text"] = OBJECT_DEFAULTS["text"]
                    _atomic_json(preference, value)
                saved["object_defaults_version"] = 2
                _atomic_json(path, saved)
            return _environment(saved["active"])
        live = discover()
        profiles = {}
        for family in ("Matte", "Basic"):
            matches = [item["name"] for item in live.filaments
                       if f"PLA {family}" in item["name"]]
            if not matches:
                printer = live.machine.removeprefix("Bambu Lab ")
                if "nozzle" not in printer:
                    printer += " 0.4 nozzle"
                matches = [f"Bambu PLA {family} @BBL {printer}"]
            profiles[family] = matches[0]
        filaments = tuple({"number": i + 1, "name": profiles[family],
                           "label": label, "color": color}
                          for i, (label, color, family) in enumerate(OBJECT_PALETTE))
        old = list(live.filaments)
        identities = {(item["name"], item["color"].upper()): i
                      for i, item in enumerate(old)}
        count = len(filaments)
        fallback = max(500., *live.flush_volumes_matrix)
        vector_fallback = max(140., *live.flush_volumes_vector)
        matrix = []
        vector = []
        for i, item in enumerate(filaments):
            source = identities.get((item["name"], item["color"]))
            vector.extend(live.flush_volumes_vector[source*2:source*2+2]
                          if source is not None else [vector_fallback]*2)
            for j, other in enumerate(filaments):
                target = identities.get((other["name"], other["color"]))
                value = (live.flush_volumes_matrix[source*len(old)+target]
                         if source is not None and target is not None else fallback)
                matrix.append(0. if i == j else value)
        environment = replace(live, filaments=filaments,
                              flush_volumes_matrix=tuple(matrix),
                              flush_volumes_vector=tuple(vector))
        revision = palette_revision(environment)
        backup = path.parent / "before-object-defaults"
        backup.mkdir(parents=True, exist_ok=True)
        if path.exists() and not (backup/path.name).exists():
            shutil.copy2(path, backup/path.name)
        preferences = [settings_path(root), *sorted((Path(root)/"prints").glob("*/terrain_colors.json"))]
        for preference in preferences:
            value = _read_json(preference)
            if preference.exists():
                destination = backup / ("global-bambu.json" if preference == settings_path(root)
                                        else preference.parent.name + "-terrain-colors.json")
                if not destination.exists():
                    shutil.copy2(preference, destination)
            value.update(OBJECT_DEFAULTS, palette_revision=revision)
            _atomic_json(preference, value)
        saved.update(active=_payload(environment), object_defaults_version=2)
        saved.setdefault("history", {})[revision] = _payload(environment)
        saved.setdefault("legacy_revision", revision)
        _atomic_json(path, saved)
        return environment


def restore_object_palette(root, discover, project=None):
    with _LOCK:
        environment = load_object_palette(root, discover)
        path = project_settings_path(project) if project is not None else settings_path(root)
        preferences = _read_json(path)
        preferences.update(OBJECT_DEFAULTS, palette_revision=palette_revision(environment))
        _atomic_json(path, preferences)
        return environment


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
    if value.get("object_defaults_version") in (1, 2):
        result.update(OBJECT_DEFAULTS)
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
