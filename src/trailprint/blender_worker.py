"""Executed by Blender's Python interpreter; keep this module dependency-free."""

import importlib
import json
import os
import sys
import traceback
import math
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def attribution_layout(bounds):
    """Deprecated compatibility helper; attribution generation is removed."""
    minimum, maximum = bounds
    width = float(maximum[0]) - float(minimum[0])
    height = float(maximum[1]) - float(minimum[1])
    if width <= 0 or height <= 0:
        raise ValueError("Plate bounds are invalid for attribution text.")
    font_height = min(ATTRIBUTION_FONT_HEIGHT_MM, height * 0.04)
    if font_height < 1.8:
        raise ValueError("Plate is too small for legible attribution text.")
    line_spacing = font_height * 1.27
    edge_margin = max(4.0, min(6.0, height * 0.06))
    center_x = (float(minimum[0]) + float(maximum[0])) / 2.0
    # Text 2 is the canonical bottom edge in the physical plate layout. The
    # plate is viewed from its back when this insert is printed, so native
    # minimum-Y maps to the opposite/front edge. Use maximum-Y here to keep the
    # attribution on the actual Text 2 edge after the calibrated rotation.
    second_y = float(maximum[1]) - edge_margin - font_height / 2.0
    return {
        "center_x": center_x,
        "line_y": (second_y + line_spacing, second_y),
        "font_height_mm": font_height,
        "max_width_mm": width * 0.7,
        "edge_margin_mm": edge_margin,
    }


def _create_attribution_insert(bpy, plate):
    """Create the separate one-layer grey attribution insert."""
    import bmesh
    from mathutils import Matrix, Vector

    def recalculate_normals(obj):
        mesh = obj.data
        edit = bmesh.new()
        edit.from_mesh(mesh)
        bmesh.ops.recalc_face_normals(edit, faces=list(edit.faces))
        edit.to_mesh(mesh)
        edit.free()
        mesh.update()

    def mesh_volume(obj):
        edit = bmesh.new()
        edit.from_mesh(obj.data)
        volume = abs(float(edit.calc_volume(signed=True)))
        edit.free()
        return volume

    # TrailPrint3D may leave mesh coordinates millions of millimetres from the
    # origin. Rebase the mesh under an equivalent object transform so Blender's
    # boolean operates with printable-scale floating-point values.
    local_bounds = [Vector(corner) for corner in plate.bound_box]
    local_anchor = Vector(tuple(
        (min(point[index] for point in local_bounds)
         + max(point[index] for point in local_bounds)) / 2.0
        for index in range(3)))
    world_offset = plate.matrix_world.to_3x3() @ local_anchor
    plate.data.transform(Matrix.Translation(-local_anchor))
    plate.location += world_offset
    plate.data.update()
    bpy.context.view_layer.update()
    world_bounds = [plate.matrix_world @ Vector(corner) for corner in plate.bound_box]
    minimum = [min(point[index] for point in world_bounds) for index in range(3)]
    maximum = [max(point[index] for point in world_bounds) for index in range(3)]
    layout = attribution_layout((minimum, maximum))
    cutters = []
    cutter_names = []
    try:
        if not os.path.isfile(ATTRIBUTION_FONT_PATH):
            raise RuntimeError(f"Attribution font was not found: {ATTRIBUTION_FONT_PATH}")
        attribution_font = bpy.data.fonts.load(ATTRIBUTION_FONT_PATH)
        for index, (body, y_position) in enumerate(zip(
                ATTRIBUTION_LINES, layout["line_y"]), 1):
            curve = bpy.data.curves.new(f"TrailPrint Attribution {index}", type="FONT")
            curve.font = attribution_font
            curve.body = body
            curve.align_x = "CENTER"
            curve.align_y = "CENTER"
            curve.dimensions = "2D"
            curve.fill_mode = "BOTH"
            curve.size = layout["font_height_mm"]
            curve.extrude = ATTRIBUTION_DEPTH_MM
            text = bpy.data.objects.new(f"TrailPrint Attribution {index}", curve)
            bpy.context.scene.collection.objects.link(text)
            cutter_names.append(text.name)
            # Mirror X so it reads normally from below after a left-to-right flip.
            text.scale.x = -1.0
            text.location = (layout["center_x"], y_position,
                             minimum[2] + ATTRIBUTION_DEPTH_MM / 2.0)
            cutters.append(text)
        bpy.context.view_layer.update()
        widest = max(abs(text.dimensions.x) for text in cutters)
        fit_scale = min(1.0, layout["max_width_mm"] / widest) if widest else 1.0
        if layout["font_height_mm"] * fit_scale < 1.8:
            raise RuntimeError("Plate attribution would be too small to print legibly.")
        centered_fallback = fit_scale < 1.0
        if centered_fallback:
            center_y = (minimum[1] + maximum[1]) / 2.0
            centered_lines = (center_y - layout["font_height_mm"] * 1.27 / 2.0,
                              center_y + layout["font_height_mm"] * 1.27 / 2.0)
            for text, y_position in zip(cutters, centered_lines):
                text.location.y = y_position
        for text in cutters:
            text.scale.x *= fit_scale
            text.scale.y *= fit_scale
            bpy.ops.object.select_all(action="DESELECT")
            text.select_set(True)
            bpy.context.view_layer.objects.active = text
            bpy.ops.object.convert(target="MESH")
            bpy.context.view_layer.update()
            cutter_height = abs(text.dimensions.z)
            if cutter_height <= 0:
                raise RuntimeError("Plate attribution produced empty cutter geometry.")
            target_height = ATTRIBUTION_DEPTH_MM + 0.1
            text.scale.z *= target_height / cutter_height
            bpy.context.view_layer.update()
            text_bounds = [text.matrix_world @ Vector(corner) for corner in text.bound_box]
            text.location.z += minimum[2] - 0.1 - min(point.z for point in text_bounds)

        for index, y_position in enumerate(layout["line_y"], 1):
            bpy.ops.mesh.primitive_cube_add(
                location=(layout["center_x"], y_position, minimum[2] + 0.05))
            carrier = bpy.context.object
            carrier.name = f"TrailPrint Attribution Carrier {index}"
            carrier.dimensions = (layout["max_width_mm"] * fit_scale, 0.45, 0.3)
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            cutters.append(carrier)
            cutter_names.append(carrier.name)

        bpy.ops.object.select_all(action="DESELECT")
        for cutter in cutters:
            cutter.select_set(True)
        bpy.context.view_layer.objects.active = cutters[0]
        bpy.ops.object.join()
        cutter = cutters[0]
        recalculate_normals(cutter)
        plate_backup = plate.data.copy()
        original_mesh = plate.data.copy()
        before_faces = len(original_mesh.polygons)
        before_dimensions = tuple(float(value) for value in plate.dimensions)
        before_volume = mesh_volume(plate)

        def apply_difference(reverse_normals=False):
            prior = plate.data
            plate.data = original_mesh.copy()
            if prior != original_mesh:
                bpy.data.meshes.remove(prior)
            if reverse_normals:
                edit = bmesh.new()
                edit.from_mesh(plate.data)
                bmesh.ops.reverse_faces(edit, faces=list(edit.faces))
                edit.to_mesh(plate.data)
                edit.free()
                plate.data.update()
            modifier = plate.modifiers.new(name="TrailPrint Attribution", type="BOOLEAN")
            modifier.operation = "DIFFERENCE"
            modifier.solver = "EXACT"
            modifier.object = cutter
            bpy.ops.object.select_all(action="DESELECT")
            plate.select_set(True)
            bpy.context.view_layer.objects.active = plate
            bpy.ops.object.modifier_apply(modifier=modifier.name)
            bpy.context.view_layer.update()
            after_dimensions = tuple(float(value) for value in plate.dimensions)
            after_volume = mesh_volume(plate)
            return (len(plate.data.polygons) > before_faces
                    and after_volume < before_volume
                    and all(after >= before * 0.99 for after, before in zip(
                        after_dimensions, before_dimensions)))

        applied = apply_difference()
        if not applied:
            applied = apply_difference(reverse_normals=True)
        if not applied:
            # A failed Boolean can remove the working mesh. Restore a backup
            # before exporting the separate grey insert.
            plate.data = plate_backup
        else:
            recalculate_normals(plate)
        result = {
            "applied": bool(applied), "recess_applied": bool(applied),
            "mode": "insert",
            "lines": list(ATTRIBUTION_LINES),
            "depth_mm": ATTRIBUTION_DEPTH_MM,
            "font_height_mm": round(layout["font_height_mm"] * fit_scale, 3),
            "edge_margin_mm": round(layout["edge_margin_mm"], 3),
            "centered_fallback": centered_fallback,
            "placement": "text_2_edge",
            "orientation": "upright_after_side_flip",
            "_normalization_translation": [
                -((minimum[0] + maximum[0]) / 2.0),
                -((minimum[1] + maximum[1]) / 2.0),
                -minimum[2],
            ],
        }
        cutter.name = "TrailPrint_Attribution"
        cutter_names.clear()
        result["_insert_object"] = cutter
        cutters = []
        return result
    finally:
        for name in cutter_names:
            cutter = bpy.data.objects.get(name)
            if cutter is not None:
                bpy.data.objects.remove(cutter, do_unlink=True)


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


def _feature_subset(data, predicate):
    """Keep tagged OSM features plus the ways/nodes needed for their geometry."""
    elements = data.get("elements", [])
    ways = {item.get("id"): item for item in elements if item.get("type") == "way"}
    nodes = {item.get("id"): item for item in elements if item.get("type") == "node"}
    selected = [item for item in elements
                if item.get("type") in {"way", "relation"}
                and predicate(item.get("tags", {}))]
    selected_ways = {item.get("id"): item for item in selected if item.get("type") == "way"}
    for relation in (item for item in selected if item.get("type") == "relation"):
        for member in relation.get("members", []):
            if member.get("type") == "way" and member.get("ref") in ways:
                selected_ways[member["ref"]] = ways[member["ref"]]
    node_ids = {node_id for way in selected_ways.values()
                for node_id in way.get("nodes", [])}
    return {"elements": ([nodes[node_id] for node_id in node_ids if node_id in nodes]
                          + list(selected_ways.values())
                          + [item for item in selected if item.get("type") == "relation"])}


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
        if plate is not None:
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
        # assembly near the origin first so small attribution features survive
        # binary STL's float precision.
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
