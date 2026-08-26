"""Generate a flat, multipart coaster from TrailPrint3D acquisition output."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import trimesh
from PIL import Image, ImageDraw
from shapely.geometry import LineString, MultiPoint, Polygon
from shapely.ops import triangulate, unary_union

from .blender_bridge import (BuildCancelled, BuildSettings, _failure_detail,
                             _run_attempt, find_blender)
from .gpx import read_gpx


COASTER_SIZE_MM = 100.0
COASTER_HEIGHT_MM = 4.0
TOP_SKIN_MM = 0.4
CONTOUR_LAYER_MM = 0.2
TRAIL_WIDTH_MM = 1.2
TRAIL_DEPTH_MM = 1.0
TRAIL_CLEARANCE_MM = 0.2
DEFAULT_CONTOUR_WIDTH_MM = 0.2
CONTOUR_WIDTH_OPTIONS_MM = (0.2, 0.4)
CONTOUR_MIN_GAP_MM = 0.4
MIN_FEATURE_AREA_MM2 = 1.0
ROLE_COLORS = {
    "black": "#111111", "green": "#18912d", "grey": "#808080",
    "white": "#ffffff", "blue": "#145adc", "trail": "#dc1914",
}
SURFACE_ROLES = ("black", "green", "grey", "white", "blue")


@dataclass(frozen=True)
class CoasterSettings:
    resolution: int = 8
    snow_cutoff_m: float = 2300.0
    contour_interval_m: Optional[float] = None
    contour_width_mm: float = DEFAULT_CONTOUR_WIDTH_MM

    @classmethod
    def validated(cls, resolution=8, snow_cutoff_m=2300,
                  contour_interval_m=None,
                  contour_width_mm=DEFAULT_CONTOUR_WIDTH_MM):
        try:
            resolution = int(resolution)
            snow_cutoff_m = float(snow_cutoff_m)
            interval = (None if contour_interval_m in (None, "")
                        else float(contour_interval_m))
            contour_width = float(contour_width_mm)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Coaster resolution, snow elevation, contour interval, and contour width must be numbers."
            ) from exc
        if not 1 <= resolution <= 10:
            raise ValueError("Coaster resolution must be from 1 to 10.")
        if not math.isfinite(snow_cutoff_m):
            raise ValueError("Snow elevation must be finite.")
        if interval is not None and (not math.isfinite(interval) or interval <= 0):
            raise ValueError("Contour interval must be a positive number.")
        if (not math.isfinite(contour_width)
                or not any(abs(contour_width - option) < 1e-9
                           for option in CONTOUR_WIDTH_OPTIONS_MM)):
            raise ValueError("Contour width must be 0.2 mm or 0.4 mm.")
        return cls(resolution, snow_cutoff_m, interval, contour_width)


def _hexagon(size=COASTER_SIZE_MM) -> Polygon:
    radius = size / 2
    return Polygon([(radius * math.cos(math.radians(index * 60)),
                     radius * math.sin(math.radians(index * 60)))
                    for index in range(6)])


def _mercator(lat: float, lon: float) -> tuple[float, float]:
    latitude = min(89.0, max(-89.0, float(lat)))
    return (math.radians(float(lon)),
            math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2)))


def _projector(samples: list) -> callable:
    neutral = [_mercator(item[0], item[1]) for item in samples]
    min_x, max_x = min(p[0] for p in neutral), max(p[0] for p in neutral)
    min_y, max_y = min(p[1] for p in neutral), max(p[1] for p in neutral)
    if max_x <= min_x or max_y <= min_y:
        raise ValueError("Downloaded terrain samples have no geographic extent.")
    center_x, center_y = (min_x + max_x) / 2, (min_y + max_y) / 2
    scale = ((COASTER_SIZE_MM / (max_x - min_x)
              + (COASTER_SIZE_MM * math.sqrt(3) / 2) / (max_y - min_y)) / 2)

    def project(lat, lon):
        x, y = _mercator(lat, lon)
        return ((x - center_x) * scale, (y - center_y) * scale)
    return project


def _unique_samples(samples: list, evidence: list, project) -> tuple[dict, list]:
    grouped = defaultdict(list)
    for index, item in enumerate(samples):
        x, y = project(item[0], item[1])
        role, mapped = "green", False
        if index < len(evidence) and len(evidence[index]) >= 3:
            role, mapped = str(evidence[index][1]), bool(evidence[index][2])
        grouped[(round(x, 6), round(y, 6))].append((float(item[2]), role, mapped))
    values = {}
    points = []
    for xy, records in grouped.items():
        mapped_roles = [role for _elevation, role, mapped in records if mapped]
        role = Counter(mapped_roles).most_common(1)[0][0] if mapped_roles else "green"
        values[xy] = {
            "elevation": sum(record[0] for record in records) / len(records),
            "role": role, "mapped": bool(mapped_roles),
        }
        points.append(xy)
    if len(points) < 3:
        raise ValueError("TrailPrint3D did not capture enough terrain samples for a coaster.")
    return values, points


def _triangle_records(values: dict, points: list) -> list:
    output = []
    for polygon in triangulate(MultiPoint(points)):
        coords = [(round(x, 6), round(y, 6)) for x, y in list(polygon.exterior.coords)[:3]]
        if all(coord in values for coord in coords):
            output.append((Polygon(coords), [values[coord] for coord in coords]))
    return output


def choose_contour_interval(minimum: float, maximum: float) -> float:
    target = max(0.1, (maximum - minimum) / 10)
    magnitude = 10 ** math.floor(math.log10(target))
    return min((value * magnitude for value in (1, 2, 2.5, 5, 10)
                if value * magnitude >= target), default=10 * magnitude)


def _contour_segment(coords: list, elevations: list, level: float):
    points = []
    for index in range(3):
        a, b = index, (index + 1) % 3
        ea, eb = elevations[a], elevations[b]
        if (ea < level <= eb) or (eb < level <= ea):
            ratio = (level - ea) / (eb - ea)
            points.append((coords[a][0] + ratio * (coords[b][0] - coords[a][0]),
                           coords[a][1] + ratio * (coords[b][1] - coords[a][1])))
    if len(points) == 2 and points[0] != points[1]:
        return LineString(points)
    return None


def _declutter_contour_lines(level_lines: list, footprint: Polygon,
                             contour_width_mm: float):
    """Keep locally printable contour fragments and suppress crowded repeats."""
    accepted = []
    occupied = None
    minimum_center_spacing = contour_width_mm + CONTOUR_MIN_GAP_MM
    for line in level_lines:
        candidate = line.intersection(footprint)
        if occupied is not None and not occupied.is_empty:
            candidate = candidate.difference(
                occupied.buffer(minimum_center_spacing, cap_style=1, join_style=1))
        if not candidate.is_empty:
            accepted.append(candidate)
            occupied = unary_union(accepted)
    return unary_union(accepted) if accepted else LineString()


def _contours(triangles: list, interval: float, footprint: Polygon,
              contour_width_mm: float):
    elevations = [record["elevation"] for _polygon, records in triangles for record in records]
    minimum, maximum = min(elevations), max(elevations)
    first = math.ceil(minimum / interval) * interval
    level_lines = []
    level = first
    while level < maximum:
        lines = []
        for polygon, records in triangles:
            line = _contour_segment(list(polygon.exterior.coords)[:3],
                                    [record["elevation"] for record in records], level)
            if line is not None:
                lines.append(line)
        if lines:
            level_lines.append(unary_union(lines))
        level += interval
    lines = _declutter_contour_lines(level_lines, footprint, contour_width_mm)
    if lines.is_empty:
        return Polygon()
    return lines.buffer(contour_width_mm / 2, cap_style=1,
                        join_style=1).intersection(footprint)


def _above_cutoff(triangles: list, cutoff: float, footprint: Polygon):
    pieces = []
    for polygon, records in triangles:
        coords = list(polygon.exterior.coords)[:3]
        values = [record["elevation"] for record in records]
        clipped = []
        for index in range(3):
            current, following = index, (index + 1) % 3
            inside, next_inside = values[current] >= cutoff, values[following] >= cutoff
            if inside:
                clipped.append(coords[current])
            if inside != next_inside:
                ratio = (cutoff - values[current]) / (values[following] - values[current])
                clipped.append((coords[current][0] + ratio * (coords[following][0] - coords[current][0]),
                                coords[current][1] + ratio * (coords[following][1] - coords[current][1])))
        if len(clipped) >= 3:
            pieces.append(Polygon(clipped))
    return unary_union(pieces).intersection(footprint) if pieces else Polygon()


def _mapped_regions(triangles: list) -> dict:
    pieces = defaultdict(list)
    priority = ("white", "black", "grey", "green")
    for polygon, records in triangles:
        mapped = [record["role"] for record in records if record["mapped"]]
        if not mapped:
            continue
        counts = Counter(mapped)
        role = min(counts, key=lambda item: (-counts[item], priority.index(item)))
        if counts[role] >= 2 or len(mapped) == 1:
            pieces[role].append(polygon)
    return {role: unary_union(items) for role, items in pieces.items()}


def _mesh_footprint(paths: Iterable[Path], footprint: Polygon):
    triangles = []
    for path in paths:
        loaded = trimesh.load_mesh(path, process=False)
        meshes = list(loaded.geometry.values()) if isinstance(loaded, trimesh.Scene) else [loaded]
        for mesh in meshes:
            for face in np.asarray(mesh.faces):
                coords = np.asarray(mesh.vertices)[face, :2]
                polygon = Polygon(coords)
                if polygon.is_valid and polygon.area > 1e-8:
                    triangles.append(polygon)
    return unary_union(triangles).intersection(footprint) if triangles else Polygon()


def _clean(geometry, footprint: Polygon):
    if geometry is None or geometry.is_empty:
        return Polygon()
    geometry = geometry.buffer(0).intersection(footprint)
    parts = [part for part in getattr(geometry, "geoms", [geometry])
             if isinstance(part, Polygon) and part.area >= MIN_FEATURE_AREA_MM2]
    return unary_union(parts) if parts else Polygon()


def _surface_regions(triangles, water, snow_cutoff, contour_interval,
                     contour_width_mm, trail_recess, footprint):
    mapped = _mapped_regions(triangles)
    elevations = [record["elevation"] for _poly, records in triangles for record in records]
    interval = contour_interval or choose_contour_interval(min(elevations), max(elevations))
    snow = _above_cutoff(triangles, snow_cutoff, footprint).union(mapped.get("white", Polygon()))
    candidates = {
        "black": mapped.get("black", Polygon()),
        "blue": water,
        "white": snow,
        "grey": mapped.get("grey", Polygon()),
    }
    available = footprint.difference(trail_recess)
    occupied = Polygon()
    result = {}
    for role in ("black", "blue", "white", "grey"):
        result[role] = _clean(candidates[role].difference(occupied).intersection(available), footprint)
        occupied = occupied.union(result[role])
    result["green"] = _clean(available.difference(occupied), footprint)
    contours = _clean(_contours(triangles, interval, footprint,
                                contour_width_mm).intersection(available), footprint)
    return result, contours, interval, (min(elevations), max(elevations))


def _extrude(geometry, bottom: float, top: float):
    meshes = []
    for polygon in getattr(geometry, "geoms", [geometry]):
        if not isinstance(polygon, Polygon) or polygon.is_empty or polygon.area <= 1e-8:
            continue
        mesh = trimesh.creation.extrude_polygon(polygon, height=top - bottom,
                                                engine="earcut")
        mesh.apply_translation((0, 0, bottom))
        meshes.append(mesh)
    return trimesh.util.concatenate(meshes) if meshes else None


def _write_role(path: Path, meshes: Iterable) -> Optional[Path]:
    valid = [mesh for mesh in meshes if mesh is not None and len(mesh.faces)]
    if not valid:
        return None
    combined = trimesh.util.concatenate(valid)
    combined.export(path)
    return path


def _draw_geometry(draw, geometry, color, transform):
    for polygon in getattr(geometry, "geoms", [geometry]):
        if not isinstance(polygon, Polygon) or polygon.is_empty:
            continue
        draw.polygon([transform(x, y) for x, y in polygon.exterior.coords], fill=color)
        for ring in polygon.interiors:
            draw.polygon([transform(x, y) for x, y in ring.coords], fill=ROLE_COLORS["black"])


def _preview(path: Path, regions: dict, trail, footprint):
    size, margin = 1000, 60
    image = Image.new("RGB", (size, size), "#eeeeea")
    draw = ImageDraw.Draw(image)
    scale = (size - margin * 2) / COASTER_SIZE_MM

    def point(x, y):
        return (margin + (x + COASTER_SIZE_MM / 2) * scale,
                size - margin - (y + COASTER_SIZE_MM * math.sqrt(3) / 4) * scale)

    draw.polygon([point(x, y) for x, y in footprint.exterior.coords], fill=ROLE_COLORS["black"])
    for role in ("green", "grey", "white", "blue", "black"):
        _draw_geometry(draw, regions.get(role, Polygon()), ROLE_COLORS[role], point)
    _draw_geometry(draw, trail, ROLE_COLORS["trail"], point)
    image.save(path)


def _validate_outputs(paths: list[Path]) -> list[dict]:
    results = []
    for path in paths:
        mesh = trimesh.load_mesh(path, process=True)
        if isinstance(mesh, trimesh.Scene):
            mesh = trimesh.util.concatenate(tuple(mesh.geometry.values()))
        bounds = np.asarray(mesh.bounds, dtype=float)
        if len(mesh.faces) == 0 or bounds.shape != (2, 3):
            raise ValueError(f"Generated coaster part {path.name} is empty.")
        if not mesh.is_watertight:
            raise ValueError(f"Generated coaster part {path.name} is not watertight.")
        if bounds[0][2] < -1e-5 or bounds[1][2] > COASTER_HEIGHT_MM + 1e-5:
            raise ValueError(
                f"Generated coaster part {path.name} exceeds the {COASTER_HEIGHT_MM:g} mm body.")
        lower = path.stem.lower()
        terrain_top = COASTER_HEIGHT_MM - CONTOUR_LAYER_MM
        if lower.endswith("_trail") and (
                abs(bounds[0][2] - (terrain_top - TRAIL_DEPTH_MM)) > 1e-4
                or abs(bounds[1][2] - terrain_top) > 1e-4):
            raise ValueError(f"Generated trail part {path.name} has the wrong depth.")
        if lower.endswith("_black_contours") and (
                abs(bounds[0][2] - (COASTER_HEIGHT_MM - CONTOUR_LAYER_MM)) > 1e-4
                or abs(bounds[1][2] - COASTER_HEIGHT_MM) > 1e-4):
            raise ValueError(f"Generated contour part {path.name} is not one layer tall.")
        if any(f"_{role}" in lower for role in ("green", "grey", "white", "blue")):
            expected_bottom = terrain_top - TOP_SKIN_MM
            if abs(bounds[0][2] - expected_bottom) > 1e-4:
                raise ValueError(f"Generated color part {path.name} extends below the top skin.")
            if abs(bounds[1][2] - terrain_top) > 1e-4:
                raise ValueError(f"Generated color part {path.name} exceeds the terrain surface.")
        results.append({"name": path.name, "watertight": True,
                        "bounds_mm": bounds.tolist(), "faces": int(len(mesh.faces))})
    if not any(item["name"].endswith("Black_Body.stl")
               and abs(item["bounds_mm"][0][2]) < 1e-5 for item in results):
        raise ValueError("Generated coaster is missing its black structural body.")
    return results


def _load_acquisition(stage: Path, project: Path):
    try:
        sample_data = json.loads((stage / "terrain-samples.json").read_text(encoding="utf-8"))
        evidence_data = json.loads((stage / "terrain-osm-evidence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("TrailPrint3D did not provide usable terrain samples.") from exc
    samples = sample_data.get("samples", [])
    evidence = evidence_data.get("samples", []) if evidence_data.get("available") else []
    project_point = _projector(samples)
    values, points = _unique_samples(samples, evidence, project_point)
    triangles = _triangle_records(values, points)
    if not triangles:
        raise ValueError("Terrain samples could not be triangulated.")
    trail_points = [project_point(lat, lon) for lat, lon, _elevation in read_gpx(project / "route.gpx")]
    trail = LineString(trail_points).buffer(TRAIL_WIDTH_MM / 2, cap_style=1, join_style=1)
    footprint = _hexagon()
    trail = trail.intersection(footprint)
    recess = trail.buffer(TRAIL_CLEARANCE_MM, cap_style=1, join_style=1).intersection(footprint)
    water_paths = [path for path in stage.glob("*.stl")
                   if "water" in path.name.lower() or "ocean" in path.name.lower()]
    water = _mesh_footprint(water_paths, footprint)
    warnings = []
    if not evidence_data.get("available"):
        warnings.append(evidence_data.get("error") or "OpenStreetMap land-cover data was unavailable.")
    if water.is_empty:
        warnings.append("No printable water geometry was available.")
    return triangles, trail, recess, water, footprint, warnings


def build_coaster_geometry(project: Path, stage: Path, settings: CoasterSettings,
                           output_dir: Path) -> list[Path]:
    triangles, trail, recess, water, footprint, warnings = _load_acquisition(stage, project)
    regions, contours, interval, elevation_range = _surface_regions(
        triangles, water, settings.snow_cutoff_m, settings.contour_interval_m,
        settings.contour_width_mm, recess, footprint)
    output_dir.mkdir(parents=True, exist_ok=True)
    terrain_top = COASTER_HEIGHT_MM - CONTOUR_LAYER_MM
    skin_bottom = terrain_top - TOP_SKIN_MM
    trail_bottom = terrain_top - TRAIL_DEPTH_MM
    lower = _extrude(footprint, 0, trail_bottom)
    upper_body = _extrude(footprint.difference(recess), trail_bottom, skin_bottom)
    outputs = []
    for label, mesh in (
        ("Black_Body", lower), ("Black_Upper", upper_body),
        ("Black_Terrain", _extrude(regions["black"], skin_bottom, terrain_top)),
        ("Black_Contours", _extrude(contours, terrain_top, COASTER_HEIGHT_MM)),
    ):
        path = _write_role(output_dir / f"{project.name}_Coaster_{label}.stl", [mesh])
        if path:
            outputs.append(path)
    for role in ("green", "grey", "white", "blue"):
        path = _write_role(
            output_dir / f"{project.name}_Coaster_{role.title()}_Terrain.stl",
            [_extrude(regions[role], skin_bottom, terrain_top)])
        if path:
            outputs.append(path)
    trail_path = _write_role(output_dir / f"{project.name}_Coaster_Trail.stl",
                             [_extrude(trail, trail_bottom, terrain_top)])
    if trail_path:
        outputs.append(trail_path)
    mesh_results = _validate_outputs(outputs)
    preview_regions = dict(regions)
    preview_regions["black"] = regions["black"].union(contours)
    _preview(output_dir / "preview.png", preview_regions, trail, footprint)
    report = {
        "version": 2, "build_mode": "coaster", "valid": True,
        "settings": asdict(settings), "size_mm": COASTER_SIZE_MM,
        "height_mm": COASTER_HEIGHT_MM, "top_skin_mm": TOP_SKIN_MM,
        "terrain_surface_height_mm": terrain_top,
        "contour_width_mm": settings.contour_width_mm,
        "contour_layer_mm": CONTOUR_LAYER_MM,
        "contour_min_gap_mm": CONTOUR_MIN_GAP_MM,
        "trail_depth_mm": TRAIL_DEPTH_MM, "contour_interval_m": interval,
        "elevation_range_m": list(elevation_range), "warnings": warnings,
        "files": mesh_results,
        "roles": sorted({next((role for role in (*SURFACE_ROLES, "trail")
                                    if f"_{role}" in path.stem.lower()), "other")
                         for path in outputs}),
    }
    (output_dir / "validation.json").write_text(json.dumps(report, indent=2) + "\n",
                                                 encoding="utf-8")
    return outputs


def run_coaster_build(project: Path, settings: CoasterSettings, blender=None,
                      timeout=3600, cancel_event=None) -> list[Path]:
    project = project.resolve()
    settings = CoasterSettings.validated(settings.resolution, settings.snow_cutoff_m,
                                         settings.contour_interval_m,
                                         settings.contour_width_mm)
    blender_path = find_blender(blender)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="coaster-", dir=str(build_dir)))
    stage, generated = work / "trailprint3d", work / "generated"
    acquire = BuildSettings.validated("HEXAGON", "NONE", settings.resolution,
                                      plate_title="", plate_text_1="", plate_text_2="",
                                      plate_text_3="", plate_text_4="", plate_text_5="",
                                      plate_recess_depth=0, include_water=True)
    try:
        result = _run_attempt(project, acquire, stage, blender_path, timeout, cancel_event)
        if result.returncode != 0:
            raise ValueError(f"TrailPrint3D failed: {_failure_detail(result)}")
        outputs = build_coaster_geometry(project, stage, settings, generated)
        destination = project / "deliverables" / "coaster"
        replacement = project / "deliverables" / f".coaster-{os.getpid()}"
        replacement.parent.mkdir(parents=True, exist_ok=True)
        if replacement.exists():
            shutil.rmtree(replacement)
        shutil.copytree(generated, replacement)
        prior = destination.with_name(".coaster-prior")
        if prior.exists():
            shutil.rmtree(prior)
        if destination.exists():
            os.replace(destination, prior)
        os.replace(replacement, destination)
        shutil.rmtree(prior, ignore_errors=True)
        return [destination / path.name for path in outputs]
    except BuildCancelled:
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
