"""Blender worker for Boolean construction of the integrated-knuckle case."""

import json
import math
import sys
from pathlib import Path

import bpy

PIVOT_RADIUS_MM = 2.5
PIVOT_DEPTH_MM = 2.5


def import_stl(path, name):
    bpy.ops.wm.stl_import(filepath=str(path))
    obj = bpy.context.active_object
    obj.name = name
    return obj


def box(name, x0, x1, y0, y1, z0, z1):
    bpy.ops.mesh.primitive_cube_add(
        location=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
        scale=((x1 - x0) / 2, (y1 - y0) / 2, (z1 - z0) / 2))
    obj = bpy.context.active_object
    obj.name = name
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


def knuckle_prism(name, x0, x1, half_height, envelope):
    flat_half = 0.42 * half_height
    shoulder = 0.58 * half_height
    profile = [(-flat_half, 0), (flat_half, 0), (half_height, shoulder),
               (half_height, envelope - shoulder), (flat_half, envelope),
               (-flat_half, envelope), (-half_height, envelope - shoulder),
               (-half_height, shoulder)]
    vertices = [(x, y, z) for x in (x0, x1) for y, z in profile]
    faces = [tuple(range(7, -1, -1)), tuple(range(8, 16))]
    for index in range(8):
        nxt = (index + 1) % 8
        faces.append((index, nxt, 8 + nxt, 8 + index))
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def cone_x(name, x0, x1, radius, half_height, tip_radius=0.1):
    bpy.ops.mesh.primitive_cone_add(
        vertices=48, radius1=radius, radius2=tip_radius, depth=x1 - x0,
        location=((x0 + x1) / 2, 0, half_height),
        rotation=(0, math.pi / 2, 0))
    obj = bpy.context.active_object
    obj.name = name
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


def boolean(target, operand, operation):
    modifier = target.modifiers.new(name=operation, type="BOOLEAN")
    modifier.operation = operation
    modifier.solver = "EXACT"
    modifier.object = operand
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(operand, do_unlink=True)


def clean(obj):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.0001)
    bpy.ops.mesh.dissolve_degenerate(threshold=0.0001)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)


def main():
    config = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    work = Path(config["work_dir"])
    half = float(config["half_height_mm"])
    envelope = float(config["hinge"]["envelope_mm"])
    length = float(config["hinge"]["length_mm"])
    clearance = float(config["hinge"]["moving_clearance_mm"])
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bottom = import_stl(work / "bottom-blank.stl", "DIY Case Bottom")
    top = import_stl(work / "top-blank.stl", "DIY Case Top")
    width = (length - 4 * clearance) / 5
    cursor = -length / 2
    bounds = []
    for _ in range(5):
        bounds.append((cursor, cursor + width))
        cursor += width + clearance
    for index, (x0, x1) in enumerate(bounds):
        owner = top if index % 2 == 0 else bottom
        boolean(owner, knuckle_prism(
            f"Knuckle {index + 1}", x0, x1, half, envelope), "UNION")
        root = (box(f"Top root {index + 1}", x0, x1, -20, -half + 0.8, 0, half)
                if index % 2 == 0 else
                box(f"Bottom root {index + 1}", x0, x1, half - 0.8, 20, 0, half))
        boolean(owner, root, "UNION")
        if index < 4:
            boolean(owner, cone_x(
                f"Pivot {index + 1}", x1 - 0.16, x1 + PIVOT_DEPTH_MM,
                PIVOT_RADIUS_MM, half), "UNION")
    for index in range(1, 5):
        x0, _ = bounds[index]
        cutter = cone_x(f"Socket {index}", x0 - 0.1, x0 + PIVOT_DEPTH_MM,
                        PIVOT_RADIUS_MM + 0.02, half, 0.07)
        boolean(top if index % 2 == 0 else bottom, cutter, "DIFFERENCE")
    boolean(bottom, import_stl(work / "bottom-cutter.stl", "Bottom pocket"),
            "DIFFERENCE")
    boolean(top, import_stl(work / "top-cutter.stl", "Top pocket"),
            "DIFFERENCE")
    clean(bottom)
    clean(top)
    bpy.ops.object.select_all(action="DESELECT")
    bottom.select_set(True)
    top.select_set(True)
    bpy.context.view_layer.objects.active = bottom
    bpy.ops.wm.stl_export(filepath=config["output_path"],
                          export_selected_objects=True)


if __name__ == "__main__":
    main()
