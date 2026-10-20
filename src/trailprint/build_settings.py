"""Read the settings belonging to a project's current successful model."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .analytics import RAISED_MAP
from .blender_bridge import SHAPE_EXTRAS, BuildSettings


def current_model_settings(project: Path, defaults: BuildSettings) -> Optional[BuildSettings]:
    directory = project / "products" / RAISED_MAP
    try:
        current = json.loads((directory / "current.json").read_text(encoding="utf-8"))
        identifier = current["id_build"]
        if not isinstance(identifier, str) or not identifier or Path(identifier).name != identifier:
            return None
        run = json.loads((directory / "runs" / f"{identifier}.json").read_text(encoding="utf-8"))
        saved = run["settings"]
        if run.get("state") != "success" or not isinstance(saved, dict) or not saved:
            return None
        values = asdict(defaults)
        for key in values:
            if key != "max_terrain_relief_mm" and key in saved:
                if saved[key] is None:
                    return None
                if isinstance(values[key], str) and not isinstance(saved[key], str):
                    return None
                values[key] = saved[key]
        if ("shape_extra" not in saved and values["shape"] in SHAPE_EXTRAS
                and values["shape_extra"] not in SHAPE_EXTRAS[values["shape"]]):
            values["shape_extra"] = "NONE"
        if not isinstance(values["dot_magnet_recess"], bool):
            return None
        return BuildSettings.validated(**values)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
