"""Stage assembled previews using the settings captured by a 3MF export."""

import hashlib
import json
import shutil
from pathlib import Path

from .blender_bridge import _render_preview, find_blender
from .mesh_validation import mesh_role
from .naming import customer_preview_name, human_name


def preview_colors(settings, environment, bands):
    colors = [item["color"] for item in environment.filaments]
    roles = {**settings.by_role(), "water": settings.water_filament}
    return {
        "role_colors": {role: colors[index-1] for role, index in roles.items()},
        "terrain_bands": [{"role": band["role"],
                           "min_z": round(band["min_z"], 6),
                           "max_z": round(band["max_z"], 6),
                           "color": colors[settings.terrain_filaments()[band["role"]]-1]}
                          for band in bands],
    }


def stage_export_previews(project, stage, settings, environment, bands):
    config = preview_colors(settings, environment, bands)
    output = stage / "preview.png"
    stls = sorted(path for path in (project/"deliverables").glob("*.stl")
                  if mesh_role(path, project.name) in config["role_colors"])
    result = _render_preview(stls,
                             project.name, output, stage/"preview-job.json",
                             find_blender(), 300, color_settings=config)
    if result.returncode:
        raise ValueError("Export preview rendering failed: " + (result.stderr or result.stdout)[-1500:])
    report_path = project/"build/validation.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("preview_only", None)
    changes = {}
    revision = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    for key, filename in [("preview", "preview.png"), ("preview_side", "preview-side.png")]:
        image = stage/filename
        if not image.is_file() or image.stat().st_size < 1000:
            raise ValueError("Export preview rendering did not produce " + filename)
        previous = report.get(key, {})
        customer = previous.get("customer_filename") or customer_preview_name(human_name(project), human_name(project))
        if key == "preview_side" and not previous.get("customer_filename"):
            customer = customer.replace("-preview.png", "-side-preview.png")
        if Path(customer).name != customer or not customer.endswith("-preview.png"):
            raise ValueError("Invalid saved preview filename")
        copy = stage/("customer-" + filename)
        shutil.copy2(image, copy)
        changes[project/"build"/filename] = image
        changes[project/"deliverables"/customer] = copy
        report[key] = {**previous, "valid": True, "filename": filename,
                       "customer_filename": customer, "color_revision": revision}
    staged_report = stage/"model-validation.json"
    staged_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    changes[report_path] = staged_report
    return changes, {"color_revision": revision, **config}
