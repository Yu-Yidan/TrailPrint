"""Executed by Blender's Python interpreter; keep this module dependency-free."""

import importlib
import json
import math
import os
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from .fonts import require_model_font
except ImportError:  # Worker is executed as a standalone script by Blender.
    from fonts import require_model_font


def configure_model_font(props, path=None):
    props.textFont = str(require_model_font(path) if path is not None else require_model_font())


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


def terrain_relief_scaling(elevations, mercator_factors, horizontal_scale,
                           max_relief_mm):
    """Choose TrailPrint3D scale settings from the complete DEM footprint."""
    elevations = [float(value) for value in elevations]
    mercator_factors = [float(value) for value in mercator_factors]
    if not elevations or len(elevations) != len(mercator_factors):
        raise ValueError("Terrain relief scaling requires matching DEM samples.")
    if not all(math.isfinite(value) for value in elevations + mercator_factors):
        raise ValueError("Terrain relief scaling received non-finite DEM samples.")
    horizontal_scale = float(horizontal_scale)
    maximum = float(max_relief_mm)
    if not math.isfinite(horizontal_scale) or horizontal_scale <= 0:
        raise ValueError("Terrain horizontal scale must be positive.")
    if not math.isfinite(maximum) or maximum <= 0:
        raise ValueError("Maximum terrain relief must be positive.")

    natural = [elevation / 1000.0 * horizontal_scale * mercator
               for elevation, mercator in zip(elevations, mercator_factors)]
    uncapped_relief = max(natural) - min(natural)
    result = {
        "uncapped_model_relief_mm": float(uncapped_relief),
        "final_model_relief_mm": float(uncapped_relief),
        "max_terrain_relief_mm": maximum,
        "cap_applied": False,
        "elevation_multiplier": 1.0,
    }
    if uncapped_relief <= maximum + 1e-9:
        return result

    multiplier = maximum / uncapped_relief
    result.update({
        "final_model_relief_mm": maximum,
        "cap_applied": True,
        "elevation_multiplier": float(multiplier),
    })
    return result


ATTRIBUTION_LINES = (
    "© OpenStreetMap contributors",
    "Terrain: © Mapterhorn",
)
ATTRIBUTION_DEPTH_MM = 0.2
ATTRIBUTION_TEXT_HEIGHT_MM = 4.5
ATTRIBUTION_SPACE_MM = 1.4
ATTRIBUTION_LINE_GAP_MM = 1.2
ATTRIBUTION_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
COPYRIGHT_MARK_DIAMETER_MM = 5.0
COPYRIGHT_MIN_FEATURE_MM = 0.6
DOT_MAGNET_DIAMETER_MM = 18.2
DOT_MAGNET_DEPTH_MM = 3.0
DOT_MAGNET_CENTER_RADIUS_MM = 36.0
DOT_MAGNET_MIN_FLOOR_MM = 0.8
DOT_MAGNET_EDGE_ALLOWANCE_MM = 2.0
DOT_MAGNET_PHYSICAL_HEIGHT_MM = 3.048
DOT_MAGNET_CYLINDER_SEGMENTS = 128


def dot_magnet_centers(center_x=0.0, center_y=0.0,
                       radius=DOT_MAGNET_CENTER_RADIUS_MM):
    """Return a centered, upright equilateral triangle in back-view order."""
    radius = float(radius)
    half_width = math.sqrt(3.0) * radius / 2.0
    return (
        (float(center_x), float(center_y) + radius),
        (float(center_x) - half_width, float(center_y) - radius / 2.0),
        (float(center_x) + half_width, float(center_y) - radius / 2.0),
    )


def dot_magnet_backing_extension(plate_thickness, terrain_recess_depth):
    """Add only enough backing to retain the required pocket floor."""
    available = float(plate_thickness) - float(terrain_recess_depth)
    return max(0.0, DOT_MAGNET_DEPTH_MM + DOT_MAGNET_MIN_FLOOR_MM - available)


def _convex_hull_xy(points):
    points = sorted(set((float(x), float(y)) for x, y in points))
    if len(points) < 3:
        raise RuntimeError("backing plate has no usable perimeter")

    def cross(origin, first, second):
        return ((first[0] - origin[0]) * (second[1] - origin[1])
                - (first[1] - origin[1]) * (second[0] - origin[0]))

    lower = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _point_segment_distance(point, first, second):
    dx, dy = second[0] - first[0], second[1] - first[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return math.hypot(point[0] - first[0], point[1] - first[1])
    ratio = max(0.0, min(1.0, (
        (point[0] - first[0]) * dx + (point[1] - first[1]) * dy
    ) / denominator))
    return math.hypot(point[0] - (first[0] + ratio * dx),
                      point[1] - (first[1] + ratio * dy))


def _plate_prism(bpy, name, points, bottom_z, top_z):
    count = len(points)
    vertices = ([(x, y, bottom_z) for x, y in points]
                + [(x, y, top_z) for x, y in points])
    faces = [tuple(reversed(range(count))), tuple(range(count, count * 2))]
    for index in range(count):
        following = (index + 1) % count
        faces.append((index, following, following + count, index + count))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    result = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(result)
    return result


def _apply_plate_boolean(bpy, plate, operand, operation, name):
    bpy.ops.object.select_all(action="DESELECT")
    plate.select_set(True)
    bpy.context.view_layer.objects.active = plate
    modifier = plate.modifiers.new(name, "BOOLEAN")
    modifier.operation = operation
    modifier.solver = "EXACT"
    modifier.object = operand
    try:
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    except Exception as exc:
        raise RuntimeError(f"could not apply {name.lower()}") from exc
    finally:
        bpy.data.objects.remove(operand, do_unlink=True)


def prepare_dot_magnet_backing(bpy, plate, terrain_recess_depth):
    """Extend a generated plate and describe its centered pocket layout."""
    world_vertices = [plate.matrix_world @ vertex.co for vertex in plate.data.vertices]
    minimum_z = min(point.z for point in world_vertices)
    maximum_z = max(point.z for point in world_vertices)
    minimum_x = min(point.x for point in world_vertices)
    maximum_x = max(point.x for point in world_vertices)
    minimum_y = min(point.y for point in world_vertices)
    maximum_y = max(point.y for point in world_vertices)
    hull = _convex_hull_xy((point.x, point.y) for point in world_vertices)
    thickness = maximum_z - minimum_z
    extension = dot_magnet_backing_extension(thickness, terrain_recess_depth)
    if extension > 1e-8:
        backing = _plate_prism(
            bpy, "TrailPrint dot-magnet backing", hull,
            minimum_z - extension, minimum_z + 0.04)
        _apply_plate_boolean(
            bpy, plate, backing, "UNION", "TrailPrint dot-magnet backing")
    backing_minimum_z = minimum_z - extension
    centers = dot_magnet_centers(
        (minimum_x + maximum_x) / 2.0, (minimum_y + maximum_y) / 2.0)
    pocket_radius = DOT_MAGNET_DIAMETER_MM / 2.0
    edge_walls = []
    for center in centers:
        center_edge_distance = min(
            _point_segment_distance(center, hull[index], hull[(index + 1) % len(hull)])
            for index in range(len(hull)))
        wall = center_edge_distance - pocket_radius
        if wall < DOT_MAGNET_EDGE_ALLOWANCE_MM - 1e-6:
            raise RuntimeError(
                f"dot magnet at {center} leaves only {wall:.3f} mm at the plate edge")
        edge_walls.append(wall)
    return {
        "enabled": True,
        "diameter_mm": DOT_MAGNET_DIAMETER_MM,
        "depth_mm": DOT_MAGNET_DEPTH_MM,
        "physical_magnet_height_mm": DOT_MAGNET_PHYSICAL_HEIGHT_MM,
        "nominal_proud_mm": DOT_MAGNET_PHYSICAL_HEIGHT_MM - DOT_MAGNET_DEPTH_MM,
        "center_radius_mm": DOT_MAGNET_CENTER_RADIUS_MM,
        "centers_xy_mm": [list(center) for center in centers],
        "triangle_side_mm": math.sqrt(3.0) * DOT_MAGNET_CENTER_RADIUS_MM,
        "triangle_centroid_xy_mm": [
            sum(center[0] for center in centers) / 3.0,
            sum(center[1] for center in centers) / 3.0,
        ],
        "edge_walls_mm": edge_walls,
        "minimum_edge_wall_mm": min(edge_walls),
        "minimum_floor_mm": DOT_MAGNET_MIN_FLOOR_MM,
        "actual_floor_mm": thickness + extension - float(terrain_recess_depth)
                           - DOT_MAGNET_DEPTH_MM,
        "original_plate_thickness_mm": thickness,
        "backing_extension_mm": extension,
        "final_plate_thickness_mm": thickness + extension,
        "backing_minimum_z": backing_minimum_z,
        "orientation": "upright from back with title edge at top",
    }


def cut_dot_magnet_recesses(bpy, plate, metadata):
    """Cut the three bed-facing pockets into the prepared backing plate."""
    radius = DOT_MAGNET_DIAMETER_MM / 2.0
    bottom = float(metadata["backing_minimum_z"])
    for index, center in enumerate(metadata["centers_xy_mm"], 1):
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=DOT_MAGNET_CYLINDER_SEGMENTS, radius=radius,
            depth=DOT_MAGNET_DEPTH_MM + 0.04,
            location=(center[0], center[1],
                      bottom + DOT_MAGNET_DEPTH_MM / 2.0 - 0.02))
        cutter = bpy.context.active_object
        cutter.name = f"TrailPrint dot-magnet pocket {index}"
        _apply_plate_boolean(
            bpy, plate, cutter, "DIFFERENCE", f"TrailPrint dot-magnet pocket {index}")
    metadata.pop("backing_minimum_z", None)
    return metadata


def copyright_mark_geometry(depth=ATTRIBUTION_DEPTH_MM, segments=64):
    """Return a printable © mesh with 0.4 mm-nozzle-safe features."""
    vertices = []
    faces = []

    def add_annular_segment(inner_radius, outer_radius, start, end, count,
                            closed=False):
        offset = len(vertices)
        steps = count if closed else count + 1
        for index in range(steps):
            ratio = index / count
            angle = start + (end - start) * ratio
            cosine, sine = math.cos(angle), math.sin(angle)
            vertices.extend([
                (outer_radius * cosine, outer_radius * sine, 0.0),
                (inner_radius * cosine, inner_radius * sine, 0.0),
                (outer_radius * cosine, outer_radius * sine, depth),
                (inner_radius * cosine, inner_radius * sine, depth),
            ])
        interval_count = count if closed else count
        for index in range(interval_count):
            next_index = (index + 1) % steps
            current = offset + index * 4
            following = offset + next_index * 4
            faces.extend([
                (current, following, following + 1, current + 1),
                (current + 2, current + 3, following + 3, following + 2),
                (current, current + 2, following + 2, following),
                (current + 1, following + 1, following + 3, current + 3),
            ])
        if not closed:
            first = offset
            last = offset + count * 4
            faces.extend([
                (first, first + 1, first + 3, first + 2),
                (last, last + 2, last + 3, last + 1),
            ])

    outer_radius = COPYRIGHT_MARK_DIAMETER_MM / 2.0
    outer_ring_inner = outer_radius - (COPYRIGHT_MIN_FEATURE_MM + 0.05)
    add_annular_segment(outer_ring_inner, outer_radius,
                        0.0, math.tau, segments, closed=True)
    # The inner C opens to the right. Its 0.6 mm stroke and 0.6 mm clearance
    # from the outer ring remain printable with the configured 0.4 mm nozzle.
    c_outer = outer_ring_inner - COPYRIGHT_MIN_FEATURE_MM
    c_inner = c_outer - COPYRIGHT_MIN_FEATURE_MM
    add_annular_segment(c_inner, c_outer, math.radians(45), math.radians(315),
                        max(12, round(segments * 0.75)))
    return vertices, [tuple(reversed(face)) for face in faces]


def attribution_text_scale(reference_height):
    """Match TrailPrint3D's regular edge-text scale from its Name reference."""
    reference_height = float(reference_height)
    if not math.isfinite(reference_height) or reference_height <= 0:
        raise ValueError("attribution font reference height must be positive")
    return ATTRIBUTION_TEXT_HEIGHT_MM / reference_height


def _world_xy_bounds(obj, vector_type):
    bounds = [obj.matrix_world @ vector_type(corner) for corner in obj.bound_box]
    return ([min(point[index] for point in bounds) for index in range(2)],
            [max(point[index] for point in bounds) for index in range(2)])


def create_plate_attribution(bpy, module_root, plate):
    """Cut a flush, bed-facing attribution inlay into a generated plate."""
    from mathutils import Vector

    text_utils = importlib.import_module(module_root + ".utils.text_objects")
    world_bounds = [plate.matrix_world @ Vector(corner) for corner in plate.bound_box]
    minimum = [min(point[index] for point in world_bounds) for index in range(3)]
    maximum = [max(point[index] for point in world_bounds) for index in range(3)]
    center_x = (minimum[0] + maximum[0]) / 2.0
    center_y = (minimum[1] + maximum[1]) / 2.0
    available_width = (maximum[0] - minimum[0]) * 0.95
    attribution_objects = []

    reference = text_utils.create_text(
        "trailprint_attribution_reference", "Name", (0.0, 0.0, 0.0), 1.0,
        (0.0, 0.0, 0.0), ATTRIBUTION_DEPTH_MM / 2.0,
        font_path=ATTRIBUTION_FONT_PATH)
    bpy.context.view_layer.update()
    shared_text_scale = attribution_text_scale(reference.dimensions.y)
    reference_data = reference.data
    bpy.data.objects.remove(reference, do_unlink=True)
    bpy.data.curves.remove(reference_data)

    def create_text_segment(name, body):
        segment = text_utils.create_text(
            name, body, (0.0, 0.0, minimum[2] + 1.0), 1.0,
            (0.0, 0.0, 0.0), ATTRIBUTION_DEPTH_MM / 2.0,
            font_path=ATTRIBUTION_FONT_PATH)
        segment.location.z = minimum[2] + ATTRIBUTION_DEPTH_MM / 2.0
        bpy.context.view_layer.update()
        segment.scale.x = shared_text_scale
        segment.scale.y = shared_text_scale
        bpy.context.view_layer.objects.active = segment
        segment.select_set(True)
        bpy.ops.object.convert(target="MESH")
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        segment.select_set(False)
        return segment

    def create_copyright_mark(name):
        vertices, faces = copyright_mark_geometry()
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        mark = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(mark)
        mark.location.z = minimum[2]
        return mark

    line_specs = (
        (("mark", ""), ("text", "OpenStreetMap contributors")),
        (("text", "Terrain:"), ("mark", ""), ("text", "Mapterhorn")),
    )
    for line_index, spec in enumerate(line_specs):
        items = []
        for item_index, (kind, body) in enumerate(spec):
            name = f"trailprint_attribution_{line_index}_{item_index}"
            item = (create_copyright_mark(name) if kind == "mark"
                    else create_text_segment(name, body))
            bounds = _world_xy_bounds(item, Vector)
            items.append((item, bounds[1][0] - bounds[0][0]))
        total_width = (sum(width for _item, width in items)
                       + ATTRIBUTION_SPACE_MM * (len(items) - 1))
        if total_width > available_width:
            raise RuntimeError(
                "back-plate attribution does not fit at its configured printable size")
        cursor = center_x - total_width / 2.0
        for item, width in items:
            bounds = _world_xy_bounds(item, Vector)
            item.location.x += cursor + width / 2.0 - (
                bounds[0][0] + bounds[1][0]) / 2.0
            item.location.y += center_y - (bounds[0][1] + bounds[1][1]) / 2.0
            cursor += width + ATTRIBUTION_SPACE_MM
            attribution_objects.append(item)
        bpy.context.view_layer.update()
        line_bounds = [_world_xy_bounds(item, Vector) for item, _width in items]
        line_min_y = min(bounds[0][1] for bounds in line_bounds)
        line_max_y = max(bounds[1][1] for bounds in line_bounds)
        target = (center_y + ATTRIBUTION_LINE_GAP_MM / 2.0
                  if line_index == 0
                  else center_y - ATTRIBUTION_LINE_GAP_MM / 2.0)
        translation_y = (target - line_min_y if line_index == 0
                         else target - line_max_y)
        for item, _width in items:
            item.location.y += translation_y

    bpy.ops.object.select_all(action="DESELECT")
    for item in attribution_objects:
        item.select_set(True)
    bpy.context.view_layer.objects.active = attribution_objects[0]
    bpy.ops.object.join()
    attribution = attribution_objects[0]
    attribution.name = plate.name.removesuffix("_Plate") + "_Attribution"
    attribution.data.name = attribution.name
    joined_bounds = [attribution.matrix_world @ Vector(corner)
                     for corner in attribution.bound_box]
    attribution.location.x += center_x - (
        min(point.x for point in joined_bounds) + max(point.x for point in joined_bounds)) / 2.0
    attribution.location.y += center_y - (
        min(point.y for point in joined_bounds) + max(point.y for point in joined_bounds)) / 2.0
    # Mirror the complete layout, rather than individual segments, so both
    # character order and glyph shapes read normally when viewed from below.
    original_cursor = bpy.context.scene.cursor.location.copy()
    bpy.context.scene.cursor.location = (center_x, center_y, minimum[2])
    bpy.context.view_layer.objects.active = attribution
    attribution.select_set(True)
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
    bpy.context.scene.cursor.location = original_cursor
    attribution.scale.x = -1.0
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.context.view_layer.update()

    cutter = attribution.copy()
    cutter.data = attribution.data.copy()
    cutter.name = "trailprint_attribution_cutter"
    bpy.context.collection.objects.link(cutter)
    cutter_min = min(vertex.co.z for vertex in cutter.data.vertices)
    cutter_max = max(vertex.co.z for vertex in cutter.data.vertices)
    epsilon = 0.02
    for vertex in cutter.data.vertices:
        if abs(vertex.co.z - cutter_min) <= 1e-6:
            vertex.co.z -= epsilon
        if abs(vertex.co.z - cutter_max) <= 1e-6:
            vertex.co.z += epsilon

    bpy.ops.object.select_all(action="DESELECT")
    plate.select_set(True)
    bpy.context.view_layer.objects.active = plate
    modifier = plate.modifiers.new("TrailPrint attribution recess", "BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.solver = "EXACT"
    modifier.object = cutter
    try:
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    except Exception as exc:
        raise RuntimeError("could not cut the back-plate attribution recess") from exc
    finally:
        bpy.data.objects.remove(cutter, do_unlink=True)

    return attribution, {
        "lines": list(ATTRIBUTION_LINES),
        "depth_mm": ATTRIBUTION_DEPTH_MM,
        "target_text_height_mm": ATTRIBUTION_TEXT_HEIGHT_MM,
        "shared_text_scale": shared_text_scale,
        "font_path": ATTRIBUTION_FONT_PATH,
        "line_gap_mm": ATTRIBUTION_LINE_GAP_MM,
        "copyright_mark_diameter_mm": COPYRIGHT_MARK_DIAMETER_MM,
        "copyright_min_feature_mm": COPYRIGHT_MIN_FEATURE_MM,
        "orientation": "upright from back with title edge at top",
    }


def configure_water(props, enabled, min_area_mm2=25.0):
    """Keep every supported water source behind one printability control."""
    enabled = bool(enabled)
    props.col_wPondsActive = enabled
    props.col_wBigRiversActive = enabled
    props.col_wSmallRiversActive = False
    props.el_oActive = enabled
    props.col_wArea = float(min_area_mm2)
    if enabled:
        props.elementMode = "SINGLECOLORMODE_REMESH"
        props.toleranceElements = 0.4
    else:
        props.elementMode = "PAINT"


def lava_osm_query(bounds):
    """Build the explicit-lava Overpass query missing from TrailPrint3D."""
    south, west, north, east = (float(value) for value in bounds)
    return (
        f"[out:json][timeout:120][bbox:{south},{west},{north},{east}];\n"
        "(\n"
        '  nwr["geological"~"^(volcanic_lava_field|volcanic_lava_flow)$"];\n'
        '  nwr["surface"="lava"];\n'
        '  nwr["natural"="lava"];\n'
        ");\n"
        "out body;\n"
        ">;\n"
        "out skel qt;\n"
    )


def fetch_lava_osm(osm_utils, bounds):
    """Fetch explicit lava polygons using the add-on's retrying HTTP client."""
    request = getattr(osm_utils, "_overpass_request", None)
    if request is None:
        raise RuntimeError("Installed TrailPrint3D cannot request lava evidence.")
    data = request(lava_osm_query(bounds),
                   "https://overpass-api.de/api/interpreter",
                   method="POST", timeout=120, max_retries=3)
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise RuntimeError("OpenStreetMap lava request did not complete.")
    return data


def _osm_polygon(data, osm_utils, geometry_utils):
    nodes = osm_utils.build_osm_nodes(data)
    bodies, negatives = osm_utils.extract_multipolygon_bodies(data.get("elements", []), nodes)
    relation_way_ids = {member.get("ref")
                        for item in data.get("elements", []) if item.get("type") == "relation"
                        for member in item.get("members", []) if member.get("type") == "way"}
    rings = [geometry_utils.Polygon([(lon, lat) for lat, lon, _ in body])
             for body in bodies if len(body) >= 4]
    holes = [geometry_utils.Polygon([(lon, lat) for lat, lon, _ in body])
             for body in negatives if len(body) >= 4]
    for item in data.get("elements", []):
        if item.get("type") != "way" or item.get("id") in relation_way_ids:
            continue
        coords = [(nodes[node_id]["lon"], nodes[node_id]["lat"])
                  for node_id in item.get("nodes", []) if node_id in nodes]
        if len(coords) >= 4 and coords[0] == coords[-1]:
            rings.append(geometry_utils.Polygon(coords))
    valid = [geometry_utils.validate(item) for item in rings if not item.is_empty]
    valid_holes = [geometry_utils.validate(item) for item in holes if not item.is_empty]
    shape = geometry_utils.union([item for item in valid if item is not None])
    negative = geometry_utils.union([item for item in valid_holes if item is not None])
    return geometry_utils.subtract(shape, negative)


def classify_osm_samples(samples, polygons, point_factory):
    """Apply deterministic land-cover priority to geographic terrain samples."""
    classified = []
    counts = Counter()
    for lat, lon, elevation in samples:
        point = point_factory(float(lon), float(lat))
        role = "green"
        mapped = False
        for candidate in ("white", "black", "grey", "green"):
            shape = polygons.get(candidate)
            if shape is not None and shape.covers(point):
                role, mapped = candidate, True
                break
        if mapped:
            counts[role] += 1
        classified.append([float(elevation), role, mapped])
    return classified, dict(counts)


def collect_osm_evidence(module_root, bounds, samples):
    """Classify terrain samples without creating physical OSM overlay objects."""
    osm_utils = importlib.import_module(module_root + ".utils.osm")
    geometry_utils = importlib.import_module(module_root + ".utils.geometry2d")
    bbox = (bounds["min_lat"], bounds["min_lon"],
            bounds["max_lat"], bounds["max_lon"])
    kinds = ("FOREST", "SCREE", "GLACIER")
    fetched = osm_utils.fetch_osm_combined(bbox, kinds)
    if any(kind not in fetched for kind in kinds):
        raise RuntimeError("OpenStreetMap land-cover request did not complete.")
    data = {kind: fetched[kind][0] for kind in kinds}
    lava = fetch_lava_osm(osm_utils, bbox)
    polygons = {
        "green": _osm_polygon(data["FOREST"], osm_utils, geometry_utils),
        "grey": _osm_polygon(data["SCREE"], osm_utils, geometry_utils),
        "black": _osm_polygon(lava, osm_utils, geometry_utils),
        "white": _osm_polygon(data["GLACIER"], osm_utils, geometry_utils),
    }
    prepared = {role: geometry_utils.prep(shape) for role, shape in polygons.items()
                if shape is not None and not shape.is_empty}
    classified, counts = classify_osm_samples(samples, prepared, geometry_utils.Point)
    return {"version": 2, "available": True, "source": "OpenStreetMap",
            "samples": classified, "evidence_counts": counts}


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
        elevation_utils = importlib.import_module(module_root + ".utils.elevation")
        if not hasattr(utils, "runGeneration"):
            raise RuntimeError("Installed TrailPrint3D version has no compatible generator")

        props.generation_mode = "GENERATION"
        configure_model_font(props, config.get("model_font_path"))
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
        props.fixedElevationScale = False
        props.scaleElevation = 1.0
        props.plateInsertValue = float(config.get("plate_recess_depth", 0))
        props.tolerance = float(config.get("trail_clearance_mm", 0.1))
        print(f"Trail clearance per side: {props.tolerance:.3f} mm")
        props.singleColorMode = True
        configure_water(props, True, config.get("water_min_area_mm2", 25.0))
        props.disable_auto_export = False
        props.disable_3mf_export = True
        props.exportformat = "STL"
        terrain_utils = importlib.import_module(module_root + ".utils.terrain")
        original_plate_insert = terrain_utils.plateInsert
        original_elevation = elevation_utils.get_tile_elevation
        elevation_values = []
        elevation_scaling = {}
        terrain_samples = []
        terrain_bounds = {}

        def capture_elevation(*args, **kwargs):
            values, difference = original_elevation(*args, **kwargs)
            elevation_values.extend(float(value) for value in values
                                    if math.isfinite(float(value)))
            try:
                obj = args[0]
                geo_utils = importlib.import_module(module_root + ".utils.geo")
                vertices = list(obj.data.vertices)
                paired_count = min(len(vertices), len(values))
                mercator_factors = []
                stride = max(1, math.ceil(len(vertices) / 20000))
                for index in range(paired_count):
                    world = obj.matrix_world @ vertices[index].co
                    lat, lon = geo_utils.convert_to_geo(world.x, world.y)
                    elevation = float(values[index])
                    mercator_factors.append(
                        1.0 / max(0.01, math.cos(math.radians(float(lat)))))
                    if index % stride != 0:
                        continue
                    if all(math.isfinite(item) for item in (lat, lon, elevation)):
                        terrain_samples.append([lat, lon, elevation])
                decision = terrain_relief_scaling(
                    values[:paired_count], mercator_factors,
                    float(props.get("sScaleHor", props.sScaleHor)),
                    float(config.get("max_terrain_relief_mm", 15.0)))
                print("[terrain relief cap] " + json.dumps(decision, sort_keys=True))
                props.fixedElevationScale = False
                props.scaleElevation = 1.0
                elevation_scaling.update(decision)
                terrain_bounds.update({
                    "min_lat": float(props.minLat), "max_lat": float(props.maxLat),
                    "min_lon": float(props.minLon), "max_lon": float(props.maxLon),
                })
            except (AttributeError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    "Could not calculate the DEM-based terrain relief cap.") from exc
            if elevation_scaling.get("cap_applied"):
                multiplier = elevation_scaling["elevation_multiplier"]
                scaled_values = [float(value) * multiplier for value in values]
                scaled_difference = max(scaled_values) - min(scaled_values)
                return scaled_values, scaled_difference
            return values, difference

        terrain_utils.plateInsert = with_plate_clearance(
            original_plate_insert, props, config.get("plate_recess_clearance", 0))
        elevation_utils.get_tile_elevation = capture_elevation
        try:
            utils.runGeneration(0)
        finally:
            terrain_utils.plateInsert = original_plate_insert
            elevation_utils.get_tile_elevation = original_elevation
        export_dir = Path(config["export_path"])
        normalization_translation = None
        plate = bpy.data.objects.get(config["project_name"] + "_Plate")
        reference = plate or bpy.data.objects.get(config["project_name"])
        if reference is not None:
            # TrailPrint3D builds at projected map coordinates that can be
            # hundreds of kilometres from Blender's origin. Always move the
            # complete assembly before STL export so terrain, water and small
            # magnet features retain printable-scale numerical precision.
            from mathutils import Vector
            initial_bounds = [reference.matrix_world @ Vector(corner)
                              for corner in reference.bound_box]
            initial_minimum = [min(point[index] for point in initial_bounds)
                               for index in range(3)]
            initial_maximum = [max(point[index] for point in initial_bounds)
                               for index in range(3)]
            early_translation = Vector((
                -((initial_minimum[0] + initial_maximum[0]) / 2.0),
                -((initial_minimum[1] + initial_maximum[1]) / 2.0),
                -initial_minimum[2],
            ))
            for generated in bpy.data.objects:
                if generated.type in {"MESH", "CURVE", "FONT"}:
                    generated.location += early_translation
            bpy.context.view_layer.update()
            # A non-None value also makes the export path replace the add-on's
            # pre-normalization STL files with the relocated Blender objects.
            normalization_translation = [0.0, 0.0, 0.0]
            dot_magnet_metadata = None
            if plate is not None and config.get("dot_magnet_recess", True):
                dot_magnet_metadata = prepare_dot_magnet_backing(
                    bpy, plate, config.get("plate_recess_depth", 0.0))
            if dot_magnet_metadata is not None:
                dot_magnet_metadata = cut_dot_magnet_recesses(
                    bpy, plate, dot_magnet_metadata)
                (export_dir / "plate-dot-magnets.json").write_text(
                    json.dumps(dot_magnet_metadata, indent=2) + "\n", encoding="utf-8")
            final_bounds = [reference.matrix_world @ Vector(corner)
                            for corner in reference.bound_box]
            final_minimum = [min(point[index] for point in final_bounds)
                             for index in range(3)]
            final_maximum = [max(point[index] for point in final_bounds)
                             for index in range(3)]
            normalization_translation = [
                -((final_minimum[0] + final_maximum[0]) / 2.0),
                -((final_minimum[1] + final_maximum[1]) / 2.0),
                -final_minimum[2],
            ]
        if elevation_values:
            (export_dir / "terrain-elevation.json").write_text(json.dumps({
                "source_min_m": min(elevation_values),
                "source_max_m": max(elevation_values),
                "min_thickness_mm": float(props.minThickness),
                "unit_system": config.get("unit_system", "IMPERIAL"),
                **elevation_scaling,
            }, indent=2) + "\n", encoding="utf-8")
        if terrain_samples and terrain_bounds:
            (export_dir / "terrain-samples.json").write_text(json.dumps({
                "version": 1, "bounds": terrain_bounds, "samples": terrain_samples,
            }, separators=(",", ":")) + "\n", encoding="utf-8")
            try:
                osm_evidence = collect_osm_evidence(module_root, terrain_bounds,
                                                    terrain_samples)
            except Exception as exc:
                osm_evidence = {"version": 2, "available": False,
                                "error": str(exc)}
            (export_dir / "terrain-osm-evidence.json").write_text(
                json.dumps(osm_evidence, separators=(",", ":")) + "\n",
                encoding="utf-8")
        # TrailPrint3D may export OBJ despite an STL request. Re-export exact
        # generated objects as STL, and when a plate exists move the complete
        # assembly near the origin first so the binary STL retains printable-
        # scale floating-point precision.
        obj_exports = list(export_dir.glob("*.obj"))
        source_exports = (obj_exports + list(export_dir.glob("*.stl"))
                          if normalization_translation is not None else obj_exports)
        if source_exports:
            exporter = importlib.import_module(module_root + ".export")
            objects_by_export_name = {
                exporter._sanitize_filename(obj.name): obj for obj in bpy.data.objects
            }
            generated_objects = {}
            for source_path in source_exports:
                generated = objects_by_export_name.get(source_path.stem)
                if generated is None:
                    raise RuntimeError(
                        f"could not locate generated Blender object {source_path.stem}")
                generated_objects[source_path.stem] = generated
            if normalization_translation is not None:
                from mathutils import Vector
                translation = Vector(normalization_translation)
                for generated in generated_objects.values():
                    generated.location += translation
            for generated in generated_objects.values():
                exporter.export_to_STL(generated, "STL")
            for obj_path in export_dir.glob("*.obj"):
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
