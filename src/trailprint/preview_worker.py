"""Render validated STL files as a static bird's-eye PNG inside Blender."""

import json
import sys
import traceback
from pathlib import Path

COLORS = {
    "terrain": "#68724D", "trail": "#DE4343", "plate": "#000000",
    "text": "#757575", "attribution": "#757575", "water": "#0078BF",
    "other": "#757575",
}


def rgba(hex_color):
    """Convert stored sRGB filament colors to Blender's linear material colors."""
    values = [int(hex_color[i:i+2], 16)/255 for i in (1, 3, 5)]
    return tuple(v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values) + (1.,)


def main():
    try:
        import bmesh
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
            selected = config.get("color_settings", {})
            role_colors = selected.get("role_colors", {})
            color = role_colors.get(item["role"])
            material.diffuse_color = rgba(color or COLORS.get(item["role"], COLORS["other"]))
            material.roughness = 0.75
            for obj in imported:
                obj.data.materials.clear()
                obj.data.materials.append(material)
                if item["role"] == "terrain" and selected.get("terrain_bands"):
                    mesh = bmesh.new()
                    mesh.from_mesh(obj.data)
                    bottom = min(vertex.co.z for vertex in mesh.verts)
                    bands = selected["terrain_bands"]
                    heights = sorted({bottom + band[key] for band in bands for key in ("min_z", "max_z")})
                    for height in heights:
                        bmesh.ops.bisect_plane(mesh, geom=list(mesh.verts)+list(mesh.edges)+list(mesh.faces),
                                              plane_co=(0, 0, height), plane_no=(0, 0, 1), dist=1e-6)
                    for band in bands:
                        band_material = bpy.data.materials.new("Terrain " + band["role"])
                        band_material.diffuse_color = rgba(band["color"])
                        band_material.roughness = .75
                        obj.data.materials.append(band_material)
                    for face in mesh.faces:
                        z = face.calc_center_median().z - bottom
                        face.material_index = 0
                        for index, band in enumerate(bands, 1):
                            # Include the top surface; the later band wins on shared boundaries.
                            if band["min_z"] - 1e-5 <= z <= band["max_z"] + 1e-5:
                                face.material_index = index
                    mesh.to_mesh(obj.data)
                    mesh.free()
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
        studio = bpy.context.preferences.studio_lights.load(
            str(Path(__file__).with_name("preview-neutral.sl")), 'STUDIO')
        scene.display.shading.light = "STUDIO"
        scene.display.shading.studio_light = studio.name
        scene.display.shading.show_specular_highlight = True
        scene.display.shading.shadow_intensity = .15
        scene.display.shading.curvature_ridge_factor = .3
        scene.display.shading.curvature_valley_factor = .3
        scene.display.shading.cavity_ridge_factor = .3
        scene.display.shading.cavity_valley_factor = .3
        scene.display.shading.color_type = "MATERIAL"
        scene.display.shading.show_shadows = True
        scene.display.shading.show_cavity = True
        scene.display.shading.cavity_type = "WORLD"
        if hasattr(scene.display.shading, "show_object_outline"):
            scene.display.shading.show_object_outline = True
        scene.display.shading.background_type = "VIEWPORT"
        scene.display.shading.background_color = (0.18, 0.18, 0.18)
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0
        scene.view_settings.gamma = 1
        scene.render.resolution_x = 1200
        scene.render.resolution_y = 900
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(output)
        bpy.ops.render.render(write_still=True)
        # Frame the complete assembly in camera space for the angled view.
        import math
        camera.location = center + Vector((0, -span * 2, span * 2 * math.tan(math.radians(30))))
        camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
        rotation = camera.rotation_euler.to_matrix().transposed()
        projected = [rotation @ (point - center) for point in corners]
        projected_width = max(p.x for p in projected) - min(p.x for p in projected)
        projected_height = max(p.y for p in projected) - min(p.y for p in projected)
        camera_data.ortho_scale = max(projected_width, projected_height * 4 / 3) * 1.12
        scene.render.filepath = str(output.with_name("preview-side.png"))
        bpy.ops.render.render(write_still=True)
        if not output.is_file() or output.stat().st_size < 1000:
            raise RuntimeError("Blender did not produce a usable preview image")
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
