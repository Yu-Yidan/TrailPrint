"""Cut prepared shallow water basins into a generated terrain STL."""

import json
import sys
import traceback
from pathlib import Path


def main():
    try:
        import bpy

        config = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.wm.stl_import(filepath=config["terrain_path"])
        terrain = bpy.context.active_object
        terrain.name = "Terrain with shallow water basins"
        bpy.ops.wm.stl_import(filepath=config["support_path"])
        support = bpy.context.active_object
        support.name = "Terrain support beneath shallow water"
        modifier = terrain.modifiers.new(name="Integrated water support", type="BOOLEAN")
        modifier.operation = "UNION"
        modifier.solver = "MANIFOLD"
        modifier.object = support
        bpy.context.view_layer.objects.active = terrain
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        bpy.data.objects.remove(support, do_unlink=True)
        bpy.ops.wm.stl_import(filepath=config["cutter_path"])
        cutter = bpy.context.active_object
        cutter.name = "Shallow water basin cutter"
        modifier = terrain.modifiers.new(name="Integrated water basins", type="BOOLEAN")
        modifier.operation = "DIFFERENCE"
        modifier.solver = "MANIFOLD"
        modifier.object = cutter
        bpy.context.view_layer.objects.active = terrain
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        bpy.data.objects.remove(cutter, do_unlink=True)
        bpy.ops.object.select_all(action="DESELECT")
        terrain.select_set(True)
        bpy.context.view_layer.objects.active = terrain
        bpy.ops.wm.stl_export(filepath=config["output_path"],
                              export_selected_objects=True)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
