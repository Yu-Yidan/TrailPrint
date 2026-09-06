"""Generate the adaptive, print-in-place DIY kit case."""

import json
import math
import subprocess
from pathlib import Path

import numpy as np
import trimesh
from shapely import affinity, concave_hull
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

from .blender_bridge import find_blender
from .mesh_validation import inspect_stl

FLOOR_MM = 0.8
VERTICAL_FREE_SPACE_MM = 0.8
LAYER_INCREMENT_MM = 0.2
MINIMUM_HALF_HEIGHT_MM = 4.0
HINGE_HEIGHT_ALLOWANCE_MM = 0.2
MINIMUM_RADIAL_WALL_MM = 2.0
LATERAL_CLEARANCE_MM = 0.5
MINIMUM_BODY_RADIUS_MM = 44.0
BODY_TO_HINGE_CLEARANCE_MM = 0.4
HINGE_LENGTH_MM = 40.0
HINGE_CLEARANCE_MM = 0.4
CONCAVE_HULL_RATIO = 0.02


def _load_trail(path: Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(path, file_type="stl", force="mesh", process=False)
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("The Trail STL is empty or unreadable, so the DIY case cannot be made.")
    if not np.isfinite(mesh.vertices).all():
        raise ValueError("The Trail STL contains invalid coordinates.")
    return mesh


def _centered_pocket(mesh: trimesh.Trimesh):
    bbox_center = mesh.bounds[:, :2].mean(axis=0)
    vertices = mesh.vertices[:, :2] - bbox_center
    triangles = [Polygon(vertices[face]) for face in mesh.faces]
    triangles = [triangle for triangle in triangles if triangle.area > 1e-8]
    if not triangles:
        raise ValueError("The Trail STL has no usable footprint for the DIY case.")
    footprint = unary_union(triangles).buffer(0)
    if footprint.geom_type == "MultiPolygon":
        footprint = max(footprint.geoms, key=lambda item: item.area)
    points = list(footprint.exterior.coords)[::4]
    hull = concave_hull(MultiPoint(points), ratio=CONCAVE_HULL_RATIO,
                        allow_holes=False)
    combined = hull.union(footprint).buffer(0)
    if combined.geom_type == "MultiPolygon":
        combined = max(combined.geoms, key=lambda item: item.area)
    pocket = Polygon(combined.exterior).buffer(
        LATERAL_CLEARANCE_MM, quad_segs=6).simplify(0.02, preserve_topology=True)
    visual_center = np.array((pocket.centroid.x, pocket.centroid.y))
    pocket = affinity.translate(pocket, -visual_center[0], -visual_center[1])
    return pocket


def _hinge_swing_radius(half_height: float) -> float:
    flat_half = 0.42 * half_height
    shoulder = 0.58 * half_height
    height = 2 * half_height - HINGE_HEIGHT_ALLOWANCE_MM
    profile = [(-flat_half, 0), (flat_half, 0), (half_height, shoulder),
               (half_height, height - shoulder), (flat_half, height),
               (-flat_half, height), (-half_height, height - shoulder),
               (-half_height, shoulder)]
    return max(math.hypot(y, z - half_height) for y, z in profile)


def case_dimensions(trail: trimesh.Trimesh, pocket) -> dict:
    trail_height = float(trail.extents[2])
    raw_half = FLOOR_MM + (trail_height + VERTICAL_FREE_SPACE_MM) / 2
    half_height = max(MINIMUM_HALF_HEIGHT_MM,
                      math.ceil((raw_half - 1e-9) / LAYER_INCREMENT_MM)
                      * LAYER_INCREMENT_MM)
    maximum_radius = max(np.linalg.norm(point)
                         for point in np.asarray(pocket.exterior.coords))
    body_radius = max(MINIMUM_BODY_RADIUS_MM,
                      math.ceil((maximum_radius + MINIMUM_RADIAL_WALL_MM - 1e-9)
                                / LAYER_INCREMENT_MM) * LAYER_INCREMENT_MM)
    swing_radius = _hinge_swing_radius(half_height)
    center_offset = body_radius + swing_radius + BODY_TO_HINGE_CLEARANCE_MM
    return {
        "source_dimensions_mm": [round(float(value), 4) for value in trail.extents],
        "body_diameter_mm": 2 * body_radius,
        "body_radius_mm": body_radius,
        "body_center_offset_mm": center_offset,
        "open_footprint_mm": [2 * body_radius, 2 * (center_offset + body_radius)],
        "half_height_mm": half_height,
        "closed_height_mm": 2 * half_height,
        "raw_half_height_mm": round(raw_half, 4),
        "pocket_depth_mm": half_height - FLOOR_MM,
        "total_vertical_free_space_mm": round(
            2 * (half_height - FLOOR_MM) - trail_height, 4),
        "minimum_radial_wall_mm": round(body_radius - maximum_radius, 4),
        "hinge": {
            "type": "five_segment_integrated_interlocking_knuckle",
            "axis_height_mm": half_height,
            "envelope_mm": 2 * half_height - HINGE_HEIGHT_ALLOWANCE_MM,
            "height_allowance_mm": HINGE_HEIGHT_ALLOWANCE_MM,
            "length_mm": HINGE_LENGTH_MM,
            "moving_clearance_mm": HINGE_CLEARANCE_MM,
            "separate_pin_or_insert": False,
        },
    }


def _blank(radius: float, half_height: float, center_y: float):
    mesh = trimesh.creation.cylinder(radius=radius, height=half_height, sections=192)
    mesh.apply_translation((0, center_y, half_height / 2))
    return mesh


def _cutter(pocket, center_y: float, depth: float):
    mesh = trimesh.creation.extrude_polygon(
        affinity.translate(pocket, 0, center_y), height=depth + 1)
    mesh.apply_translation((0, 0, FLOOR_MM))
    return mesh


def generate_diy_case(trail_path: Path, output_path: Path, work_dir: Path,
                      bed_size_mm=256.0, blender: Path = None,
                      timeout=600) -> dict:
    """Generate and validate a two-shell open case STL for a Bambu plate."""
    trail = _load_trail(trail_path)
    pocket = _centered_pocket(trail)
    dimensions = case_dimensions(trail, pocket)
    width, depth = dimensions["open_footprint_mm"]
    if width > bed_size_mm or depth > bed_size_mm:
        raise ValueError(
            f"The DIY case is {width:.1f} × {depth:.1f} mm and does not fit "
            f"the {bed_size_mm:g} × {bed_size_mm:g} mm print bed.")
    work_dir.mkdir(parents=True, exist_ok=True)
    radius = dimensions["body_radius_mm"]
    half = dimensions["half_height_mm"]
    center = dimensions["body_center_offset_mm"]
    depth = dimensions["pocket_depth_mm"]
    _blank(radius, half, center).export(work_dir / "bottom-blank.stl")
    _blank(radius, half, -center).export(work_dir / "top-blank.stl")
    _cutter(pocket, center, depth).export(work_dir / "bottom-cutter.stl")
    mirrored = affinity.scale(pocket, xfact=1, yfact=-1, origin=(0, 0))
    _cutter(mirrored, -center, depth).export(work_dir / "top-cutter.stl")
    config = work_dir / "case-job.json"
    config.write_text(json.dumps({
        "work_dir": str(work_dir), "output_path": str(output_path),
        **dimensions,
    }, indent=2), encoding="utf-8")
    worker = Path(__file__).with_name("diy_case_worker.py")
    command = [str(find_blender(blender)), "--background", "--python",
               str(worker), "--", str(config)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout, check=False)
    (work_dir / "blender.log").write_text(
        f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}", encoding="utf-8")
    if result.returncode != 0:
        detail = next((line for line in reversed(
            (result.stderr + result.stdout).splitlines()) if line.strip()),
            "Blender exited without details.")
        raise ValueError(f"DIY case generation failed: {detail}")
    validation = inspect_stl(output_path, "diy-case")
    validation["role"] = "diy_case"
    if (not validation.get("valid") or not validation.get("watertight")
            or validation.get("components") != 2):
        raise ValueError("The generated DIY case was not two watertight printable halves.")
    return {**dimensions, "mesh": validation}
