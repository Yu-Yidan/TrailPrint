"""Executed by Blender's Python interpreter; keep this module dependency-free."""

import importlib
import json
import os
import sys
import traceback
from pathlib import Path


def resolve_text(value, config):
    replacements = {
        "{date}": config.get("trip_date", ""), "{city}": config.get("city", ""),
        "{state}": config.get("state", ""), "{country}": config.get("country", ""),
        "{length}": config.get("length", ""), "{elevation}": config.get("elevation", ""),
        "{duration}": config.get("duration", ""), "{speed}": config.get("speed", ""),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value


def with_plate_clearance(original, props, clearance):
    """Wrap TrailPrint3D's cutter without changing its trail tolerance."""
    def wrapped(plate, terrain):
        original_tolerance = props.tolerance
        try:
            props.tolerance = float(clearance)
            return original(plate, terrain)
        finally:
            props.tolerance = original_tolerance
    return wrapped


def main():
    try:
        import bpy
        argv = sys.argv[sys.argv.index("--") + 1:]
        if len(argv) != 1:
            raise RuntimeError("TrailPrint bridge expected one job configuration path")
        config = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        props = getattr(bpy.context.scene, "tp3d", None)
        if props is None:
            raise RuntimeError("TrailPrint3D add-on is not installed or enabled")
        module_root = type(props).__module__.rsplit(".", 1)[0]
        utils = importlib.import_module(module_root + ".utils")
        if not hasattr(utils, "runGeneration"):
            raise RuntimeError("Installed TrailPrint3D version has no compatible generator")

        props.generation_mode = "GENERATION"
        props.file_path = config["gpx_file"]
        props.export_path = config["export_path"] + os.sep
        props.trailName = config["project_name"]
        props.shape = config["shape"]
        props.shapeTextStyle = config["shape_extra"]
        props.num_subdivisions = int(config["resolution"])
        props.titlefield = resolve_text(config["plate_title"], config)
        props.textfield1 = resolve_text(config["plate_text_1"], config)
        props.textfield2 = resolve_text(config["plate_text_2"], config)
        props.textfield3 = resolve_text(config["plate_text_3"], config)
        props.textfield4 = resolve_text(config["plate_text_4"], config)
        props.textfield5 = resolve_text(config["plate_text_5"], config)
        props.xTerrainOffset = float(config["x_offset"])
        props.yTerrainOffset = float(config["y_offset"])
        props.fixedElevationScale = bool(config["fixed_elevation_scale"])
        props.plateInsertValue = float(config.get("plate_recess_depth", 0))
        props.singleColorMode = True
        props.disable_auto_export = False
        props.disable_3mf_export = True
        props.exportformat = "STL"
        terrain_utils = importlib.import_module(module_root + ".utils.terrain")
        original_plate_insert = terrain_utils.plateInsert

        terrain_utils.plateInsert = with_plate_clearance(
            original_plate_insert, props, config.get("plate_recess_clearance", 0))
        try:
            utils.runGeneration(0)
        finally:
            terrain_utils.plateInsert = original_plate_insert
        export_dir = Path(config["export_path"])
        # TrailPrint3D chooses OBJ whenever its hidden element mode is PAINT,
        # even when no elements are active. Preserve that default generation
        # behavior, then re-export the exact generated objects as requested STL.
        obj_exports = list(export_dir.glob("*.obj"))
        if obj_exports:
            exporter = importlib.import_module(module_root + ".export")
            objects_by_export_name = {
                exporter._sanitize_filename(obj.name): obj for obj in bpy.data.objects
            }
            for obj_path in obj_exports:
                generated = objects_by_export_name.get(obj_path.stem)
                if generated is None:
                    raise RuntimeError(f"could not locate generated Blender object {obj_path.stem}")
                exporter.export_to_STL(generated, "STL")
            for obj_path in obj_exports:
                obj_path.unlink(missing_ok=True)
            for material_path in export_dir.glob("*.mtl"):
                material_path.unlink(missing_ok=True)
        if not any(export_dir.rglob("*.stl")):
            raise RuntimeError("generator returned without exporting STL geometry")
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
