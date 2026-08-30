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
