"""Executed by Blender's Python interpreter; keep this module dependency-free."""

import importlib
import json
import os
import sys
import traceback
from pathlib import Path


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
        props.singleColorMode = True
        props.disable_auto_export = False
        props.disable_3mf_export = True
        props.exportformat = "STL"
        utils.runGeneration(0)
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
