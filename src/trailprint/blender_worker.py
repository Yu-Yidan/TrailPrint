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


def fixed_elevation_multiplier(target_relief_mm, center_latitude):
    """Convert TrailPrint3D's equatorial 10 mm scale to a latitude-safe target."""
    return (float(target_relief_mm) / 10.0) * max(
        0.01, math.cos(math.radians(float(center_latitude))))


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
        if props.fixedElevationScale:
            target_relief = float(config.get("fixed_elevation_target_mm", 15.0))
            center_latitude = float(config.get("center_latitude", 0.0))
            props.scaleElevation = fixed_elevation_multiplier(
                target_relief, center_latitude)
        else:
            props.scaleElevation = 1.0
        props.plateInsertValue = float(config.get("plate_recess_depth", 0))
        props.singleColorMode = True
        configure_water(props, True, config.get("water_min_area_mm2", 25.0))
        props.disable_auto_export = False
        props.disable_3mf_export = True
        props.exportformat = "STL"
        terrain_utils = importlib.import_module(module_root + ".utils.terrain")
        original_plate_insert = terrain_utils.plateInsert
        original_elevation = elevation_utils.get_tile_elevation
        elevation_values = []
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
                stride = max(1, math.ceil(len(vertices) / 20000))
                for index in range(0, min(len(vertices), len(values)), stride):
                    world = obj.matrix_world @ vertices[index].co
                    lat, lon = geo_utils.convert_to_geo(world.x, world.y)
                    elevation = float(values[index])
                    if all(math.isfinite(item) for item in (lat, lon, elevation)):
                        terrain_samples.append([lat, lon, elevation])
                terrain_bounds.update({
                    "min_lat": float(props.minLat), "max_lat": float(props.maxLat),
                    "min_lon": float(props.minLon), "max_lon": float(props.maxLon),
                })
            except (AttributeError, IndexError, TypeError, ValueError):
                pass
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
        attribution = None
        if plate is not None:
            attribution, attribution_metadata = create_plate_attribution(
                bpy, module_root, plate)
            (export_dir / "plate-attribution.json").write_text(
                json.dumps(attribution_metadata, indent=2) + "\n", encoding="utf-8")
            from mathutils import Vector
            bounds = [plate.matrix_world @ Vector(corner) for corner in plate.bound_box]
            minimum = [min(point[index] for point in bounds) for index in range(3)]
            maximum = [max(point[index] for point in bounds) for index in range(3)]
            normalization_translation = [
                -((minimum[0] + maximum[0]) / 2.0),
                -((minimum[1] + maximum[1]) / 2.0),
                -minimum[2],
            ]
        if elevation_values:
            (export_dir / "terrain-elevation.json").write_text(json.dumps({
                "source_min_m": min(elevation_values),
                "source_max_m": max(elevation_values),
                "min_thickness_mm": float(props.minThickness),
                "unit_system": config.get("unit_system", "IMPERIAL"),
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
            if attribution is not None:
                generated_objects[attribution.name] = attribution
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
