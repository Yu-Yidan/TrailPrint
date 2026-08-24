"""Render validated STL files as a static bird's-eye PNG inside Blender."""

import json
import sys
import traceback
from pathlib import Path


COLORS = {
    "terrain": (0.12, 0.50, 0.18, 1.0),
    "trail": (0.80, 0.03, 0.02, 1.0),
    "plate": (0.01, 0.01, 0.01, 1.0),
    "text": (1.0, 1.0, 1.0, 1.0),
    "other": (0.35, 0.50, 0.44, 1.0),
}


def main():
    try:
        import bpy
        from mathutils import Vector

        argv = sys.argv[sys.argv.index("--") + 1:]
        if len(argv) != 1:
            raise RuntimeError("Preview renderer expected one configuration path")
        config = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        output = Path(config["output_path"])

        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        objects = []
        for item in config["stl_files"]:
            bpy.ops.wm.stl_import(filepath=item["path"])
            imported = list(bpy.context.selected_objects)
            if not imported:
                raise RuntimeError(f"Could not import {Path(item['path']).name}")
            material = bpy.data.materials.new(f"TrailPrint {item['role']}")
            material.diffuse_color = COLORS.get(item["role"], COLORS["other"])
            material.roughness = 0.75
            for obj in imported:
                obj.data.materials.clear()
                obj.data.materials.append(material)
                objects.append(obj)

        if not objects:
            raise RuntimeError("No STL objects were supplied for preview")
        bpy.context.view_layer.update()
        corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
        minimum = Vector(tuple(min(point[index] for point in corners) for index in range(3)))
        maximum = Vector(tuple(max(point[index] for point in corners) for index in range(3)))
        center = (minimum + maximum) / 2
        width, height = maximum.x - minimum.x, maximum.y - minimum.y
        span = max(width, height, maximum.z - minimum.z, 10.0)

        camera_data = bpy.data.cameras.new("TrailPrint Preview Camera")
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = max(width, height * (4 / 3)) * 1.12
        camera = bpy.data.objects.new("TrailPrint Preview Camera", camera_data)
        bpy.context.collection.objects.link(camera)
        camera.location = (center.x, center.y, maximum.z + span * 2)
        camera.rotation_euler = (0, 0, 0)

        scene = bpy.context.scene
        scene.camera = camera
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL"
        scene.display.shading.show_shadows = True
        scene.display.shading.show_cavity = True
        scene.display.shading.cavity_type = "WORLD"
        if hasattr(scene.display.shading, "show_object_outline"):
            scene.display.shading.show_object_outline = True
        scene.display.shading.background_type = "VIEWPORT"
        scene.display.shading.background_color = (0.18, 0.18, 0.18)
        scene.view_settings.view_transform = "Standard"
        scene.render.resolution_x = 1200
        scene.render.resolution_y = 900
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(output)
        bpy.ops.render.render(write_still=True)
        if not output.is_file() or output.stat().st_size < 1000:
            raise RuntimeError("Blender did not produce a usable preview image")
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
