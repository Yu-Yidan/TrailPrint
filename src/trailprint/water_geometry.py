"""Convert generated water solids into shallow, printable terrain inlays."""

import io
import json
import math
from pathlib import Path

import numpy as np
import trimesh
from shapely import contains_xy, polygons, set_precision, union_all
from shapely.errors import ShapelyError
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import nearest_points, unary_union

from .shoreline import smooth_shoreline

WATER_TOPOLOGY_INSET_MM = 0.001
WATER_DEPTH_MM = 0.4
OCEAN_DEPTH_MM = 0.2
WATER_MIN_WIDTH_MM = 1.0
WATER_MIN_COMPONENT_AREA_MM2 = 1.0
WATER_LEVEL_TOLERANCE_MM = 0.05
WATER_COORDINATE_GRID_MM = 0.01
WATER_SURFACE_SAMPLE_MM = 0.25
WATER_BOOLEAN_BOUNDS_TOLERANCE_MM = 0.01
WATER_LAYER_HEIGHT_MM = 0.2
WATER_SHORE_INNER_MM = 0.2
WATER_SHORE_OUTER_MM = 0.8
WATER_OUTLINE_MARGIN_MM = 0.4
WATER_FLAT_RANGE_MM = 0.6
TRAIL_FIT_AREA_TOLERANCE_MM2 = 0.01
TRAIL_FIT_Z_TOLERANCE_MM = 0.05
TRAIL_WATER_PRECISION_MARGIN_MM = 0.02
TRAIL_WALL_NUMERICAL_TOLERANCE_MM = 0.005
TRAIL_SMALL_WATER_OPENING_MM2 = 10.0
TRAIL_SMALL_WATER_CLEARANCE_MM = 0.25
TRAIL_NUMERICAL_OPENING_MM2 = 0.01
TRAIL_PROJECTION_GRID_MM = 1e-9


def polygon_items(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if not hasattr(geometry, "geoms"):
        return []
    return [item for item in geometry.geoms if item.geom_type == "Polygon"]


def projected_footprint(mesh):
    polygons = []
    for triangle in mesh.vertices[mesh.faces][:, :, :2]:
        polygon = Polygon(triangle).buffer(0)
        if not polygon.is_empty and polygon.area > 1e-8:
            polygons.append(polygon)
    return unary_union(polygons).buffer(0) if polygons else Polygon()


def trail_corridor(trail_path: Path, clearance_mm: float):
    """Return the exact trail footprint plus its configured side clearance."""
    trail = trimesh.load_mesh(trail_path, process=True)
    if trail.is_empty:
        raise ValueError("Trail mesh is empty.")
    footprint = _material_above_floor(trail, float(trail.bounds[0, 2]))
    if footprint.is_empty:
        raise ValueError("Trail mesh has no usable projected footprint.")
    return footprint.buffer(float(clearance_mm), join_style=1).buffer(0)


def generation_trail_corridor(trail_path, water_paths=(), clearance_mm=0.1):
    """Protect actual raised trail walls, including small enclosed water islands."""
    trail = trimesh.load_mesh(trail_path, process=True)
    floor = float(trail.bounds[0, 2])
    footprint = _material_above_floor(trail, floor)
    if footprint.is_empty:
        raise ValueError("Trail has no raised wall footprint for groove correction.")
    water = unary_union([projected_footprint(trimesh.load_mesh(path, process=True))
                         for path in water_paths])
    ordinary = float(clearance_mm) + TRAIL_WATER_PRECISION_MARGIN_MM
    corridor = footprint.buffer(ordinary, quad_segs=16)
    small = []
    for polygon in polygon_items(footprint):
        for ring in polygon.interiors:
            opening = Polygon(ring)
            if (TRAIL_NUMERICAL_OPENING_MM2 <= opening.area
                    <= TRAIL_SMALL_WATER_OPENING_MM2
                    and opening.intersection(water).area > TRAIL_NUMERICAL_OPENING_MM2):
                target = max(clearance_mm, TRAIL_SMALL_WATER_CLEARANCE_MM)
                target += TRAIL_WATER_PRECISION_MARGIN_MM
                corridor = corridor.union(ring.buffer(target, quad_segs=16))
                small.append({"opening_area_mm2": float(opening.area),
                              "target_clearance_mm": target})
    return corridor.buffer(0), {"ordinary_target_clearance_mm": ordinary,
                               "small_water_openings": small, "floor_z_mm": floor}


def export_groove_cutter(corridor, floor_z, top_z, output_path):
    """Extrude the wall footprint with a one-float32-step floor separation.

    Coincident cutter and pocket floors can leave opposed sheets in Blender's
    Boolean output. A representable downward step avoids those sheets without
    filling holes or changing the trail. Near zero, use float32 epsilon at 1 mm
    so mesh transforms do not treat the translation as identity. Bound the
    separation to one micron.
    """
    cutter_floor = float(np.nextafter(np.float32(floor_z), np.float32(-np.inf)))
    if abs(cutter_floor) < np.finfo(np.float32).eps:
        cutter_floor = -float(np.finfo(np.float32).eps)
    separation = float(floor_z) - cutter_floor
    if not math.isfinite(separation) or not 0 < separation <= 0.001:
        raise ValueError("Groove cutter floor cannot be separated safely at STL precision.")
    pieces = []
    for polygon in polygon_items(corridor):
        # STL stores float32 coordinates; avoid sub-micron slivers in buffered rings.
        polygon = set_precision(polygon, 0.001).simplify(0.001, preserve_topology=True)
        for part in polygon_items(polygon):
            mesh = trimesh.creation.extrude_polygon(part, top_z - cutter_floor, engine="earcut")
            mesh.apply_translation((0, 0, cutter_floor))
            pieces.append(mesh)
    if not pieces:
        raise ValueError("Groove correction produced no cutter geometry.")
    cutter = trimesh.util.concatenate(pieces)
    cutter.export(output_path)
    exported = trimesh.load_mesh(output_path, process=True)
    if not exported.is_watertight or not exported.is_winding_consistent:
        raise ValueError("Groove cutter did not remain manifold after STL export.")
    actual_floor = float(exported.bounds[0, 2])
    if actual_floor != cutter_floor:
        raise ValueError("Groove cutter floor separation was lost during STL export.")
    return {"reference_floor_z_mm": float(floor_z), "cutter_floor_z_mm": actual_floor,
            "floor_separation_mm": separation}


def groove_material_overlap(terrain_path, corridor, floor_z):
    terrain = trimesh.load_mesh(terrain_path, process=True)
    return float(_material_above_floor(terrain, floor_z).intersection(corridor).area)


def _stitch_collinear_boundaries(mesh):
    """Split existing edges at numerical T junctions; never cap actual holes."""
    splits = 0
    for _ in range(10):
        counts = np.bincount(mesh.edges_unique_inverse)
        graph = {}
        for a, b in mesh.edges_unique[counts != 2]:
            graph.setdefault(int(a), set()).add(int(b))
            graph.setdefault(int(b), set()).add(int(a))
        groups = []
        while graph:
            pending = [next(iter(graph))]
            group = set()
            while pending:
                vertex = pending.pop()
                if vertex in group:
                    continue
                group.add(vertex)
                pending.extend(graph.get(vertex, set()) - group)
            for vertex in group:
                graph.pop(vertex, None)
            groups.append(group)
        seen_groups = {frozenset(group) for group in groups}
        faces = mesh.faces.copy()
        removed, added = set(), []
        for group in groups:
            if not 3 <= len(group) <= 1024:
                continue
            vertices = np.asarray(sorted(group))
            coordinates = mesh.vertices[vertices]
            axis = int(np.argmax(np.ptp(coordinates, axis=0)))
            a = int(vertices[np.argmin(coordinates[:, axis])])
            c = int(vertices[np.argmax(coordinates[:, axis])])
            delta = mesh.vertices[c] - mesh.vertices[a]
            length_squared = np.dot(delta, delta)
            if length_squared < 1e-16:
                continue
            fractions = (coordinates - mesh.vertices[a]) @ delta / length_squared
            distances = np.linalg.norm(
                coordinates - mesh.vertices[a] - fractions[:, None] * delta, axis=1)
            # Only repair one-dimensional numerical seams, never surface holes.
            if distances.max() > 1e-5:
                # Several T junctions can meet at a bend in the terrain roof.
                # Split each straight edge independently rather than filling
                # the enclosing two-dimensional boundary.
                for x, y in mesh.edges_unique[counts != 2]:
                    if int(x) not in group or int(y) not in group:
                        continue
                    edge = mesh.vertices[y] - mesh.vertices[x]
                    length = np.dot(edge, edge)
                    if length < 1e-16:
                        continue
                    along = (coordinates - mesh.vertices[x]) @ edge / length
                    away = np.linalg.norm(
                        coordinates - mesh.vertices[x] - along[:, None] * edge, axis=1)
                    middle = vertices[(along > 0) & (along < 1) & (away <= 5e-6)]
                    local = frozenset([int(x), int(y), *map(int, middle)])
                    if len(local) >= 3 and local not in seen_groups:
                        seen_groups.add(local)
                        groups.append(set(local))
                continue
            ordered = vertices[np.argsort(fractions)].tolist()
            positions = {vertex: index for index, vertex in enumerate(ordered)}
            adjacent = np.flatnonzero(np.count_nonzero(np.isin(faces, vertices), axis=1) >= 2)
            for index in adjacent:
                if index in removed:
                    continue
                face = faces[index]
                if all(int(vertex) in group for vertex in face):
                    # A face whose three vertices lie on this edge is a
                    # quantization sliver, with no physical surface area.
                    removed.add(int(index))
                    continue
                for position in range(3):
                    x, y, z = (int(face[position]), int(face[(position + 1) % 3]),
                               int(face[(position + 2) % 3]))
                    if x not in positions or y not in positions:
                        continue
                    left, right = positions[x], positions[y]
                    if abs(right - left) <= 1:
                        continue
                    chain = (ordered[left:right + 1] if left < right
                             else ordered[right:left + 1][::-1])
                    added.extend((first, second, z) for first, second in zip(chain, chain[1:]))
                    removed.add(int(index))
                    break
        if not removed:
            break
        splits += len(removed)
        mesh = trimesh.Trimesh(
            mesh.vertices, np.concatenate((np.delete(faces, list(removed), axis=0),
                                           np.asarray(added, dtype=np.int64).reshape(-1, 3))),
            process=True)
    return mesh, splits


def _cancel_opposed_horizontal_facets(mesh, levels):
    """Cancel zero-thickness groove floor sheets with different triangulations."""
    repaired = 0
    for level in levels:
        triangles = mesh.triangles
        selected = np.all(np.abs(triangles[:, :, 2] - level) < 1e-8, axis=1)
        up, down = [], []
        for triangle, normal in zip(triangles[selected], mesh.face_normals[selected]):
            polygon = Polygon(triangle[:, :2])
            if polygon.area > 0:
                (up if normal[2] > 0 else down).append(polygon)
        if not up or not down:
            continue
        upward, downward = unary_union(up), unary_union(down)
        if upward.intersection(downward).area <= 0:
            continue
        parts = [trimesh.Trimesh(mesh.vertices, mesh.faces[~selected], process=False)]
        for region, reverse in ((upward.difference(downward), False),
                                (downward.difference(upward), True)):
            for polygon in polygon_items(region):
                vertices, faces = trimesh.creation.triangulate_polygon(polygon, engine="earcut")
                vertices = np.column_stack((vertices, np.full(len(vertices), level)))
                parts.append(trimesh.Trimesh(vertices, faces[:, ::-1] if reverse else faces,
                                            process=False))
        repaired += int(selected.sum())
        mesh = trimesh.util.concatenate(parts)
        mesh.merge_vertices()
    return mesh, repaired


class TerrainRepairError(ValueError):
    def __init__(self, message, report):
        super().__init__(message)
        self.repair_report = report


def _terrain_repair_pass(mesh):
    """Clean one generation of coincident facets and numerical edge seams."""
    counts = np.bincount(mesh.edges_unique_inverse)
    edges = mesh.edges_unique[counts > 2]
    levels = np.unique([mesh.vertices[a, 2] for a, b in edges
                        if abs(mesh.vertices[a, 2] - mesh.vertices[b, 2]) < 1e-8])
    canonical = np.sort(np.asarray(mesh.faces, dtype=np.int64), axis=1)
    _, inverse, counts = np.unique(
        canonical, axis=0, return_inverse=True, return_counts=True)
    keep = np.ones(len(mesh.faces), dtype=bool)
    duplicate_groups = np.where(counts > 1)[0]
    for group in duplicate_groups:
        indices = np.where(inverse == group)[0]
        reference = indices[0]
        direction = np.asarray(mesh.face_normals[reference], dtype=float)
        signs = np.array([
            1 if float(np.dot(direction, mesh.face_normals[index])) >= 0 else -1
            for index in indices
        ])
        keep[indices] = False
        net = int(signs.sum())
        if net:
            desired = 1 if net > 0 else -1
            keep[indices[np.where(signs == desired)[0][0]]] = True
    removed_faces = int(np.count_nonzero(~keep))
    if removed_faces:
        mesh.update_faces(keep)
        mesh.remove_unreferenced_vertices()
    degenerate_faces = 0
    planar_faces = 0
    edge_splits = 0
    if not mesh.is_watertight:
        usable = mesh.nondegenerate_faces()
        degenerate_faces = int(np.count_nonzero(~usable))
        mesh.update_faces(usable)
        mesh, planar_faces = _cancel_opposed_horizontal_facets(mesh, levels)
        # Match STL's float32 coordinates before joining triangulated boundaries.
        mesh.vertices = mesh.vertices.astype(np.float32).astype(float)
        mesh.merge_vertices()
        usable = mesh.nondegenerate_faces()
        degenerate_faces += int(np.count_nonzero(~usable))
        mesh.update_faces(usable)
        mesh, edge_splits = _stitch_collinear_boundaries(mesh)
    return mesh, {
        "duplicate_groups": int(len(duplicate_groups)), "removed_faces": removed_faces,
        "degenerate_faces_removed": degenerate_faces,
        "horizontal_faces_retriangulated": planar_faces, "collinear_edge_splits": edge_splits,
    }


def _terrain_repair_defects(mesh):
    counts = np.bincount(mesh.edges_unique_inverse)
    edges = mesh.edges_unique[counts != 2]
    return {"watertight": bool(mesh.is_watertight),
            "winding_consistent": bool(mesh.is_winding_consistent),
            "remaining_non_manifold_edges": len(edges),
            "defect_locations_mm": mesh.vertices[edges[:5]].mean(axis=1).tolist()}


def _collapse_tiny_overfull_edge(mesh):
    """Try one sub-micron numerical seam; never close boundary holes."""
    counts = np.bincount(mesh.edges_unique_inverse)
    bad = np.flatnonzero(counts != 2)
    if (len(bad) != 1 or counts[bad[0]] != 4
            or not mesh.is_winding_consistent):
        return mesh, None
    a, b = mesh.edges_unique[bad[0]]
    endpoints = mesh.vertices[[a, b]]
    length = float(np.linalg.norm(endpoints[1] - endpoints[0]))
    if not np.isfinite(endpoints).all() or not 0 < length <= .001:
        return mesh, None
    midpoint = endpoints.mean(axis=0).astype(np.float32).astype(float)
    vertices = mesh.vertices.copy()
    vertices[a] = midpoint
    faces = mesh.faces.copy()
    faces[faces == b] = a
    keep = ((faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2])
            & (faces[:, 0] != faces[:, 2]))
    candidate = trimesh.Trimesh(vertices, faces[keep], process=True)
    # Validate the actual binary STL representation before accepting the edit.
    candidate = trimesh.load_mesh(io.BytesIO(candidate.export(file_type="stl")),
                                  file_type="stl", process=True)
    volume_delta = float(candidate.volume - mesh.volume)
    if (not candidate.is_watertight or not candidate.is_winding_consistent
            or not np.array_equal(candidate.bounds, mesh.bounds)
            or not math.isfinite(volume_delta) or abs(volume_delta) > .00001
            or candidate.volume <= 0):
        return mesh, None
    return candidate, {
        "edge_length_mm": length,
        "original_endpoints_mm": endpoints.tolist(),
        "replacement_vertex_mm": midpoint.tolist(),
        "maximum_vertex_displacement_mm": float(
            np.linalg.norm(endpoints - midpoint, axis=1).max()),
        "collapsed_triangles": int(np.count_nonzero(~keep)),
        "volume_change_mm3": volume_delta,
    }


def repair_terrain_for_water_boolean(terrain_path: Path, output_path: Path) -> dict:
    """Repair numerical sheets without filling holes or changing solid volume."""
    mesh = trimesh.load_mesh(terrain_path, process=True)
    if mesh.is_empty:
        raise ValueError("Terrain mesh is empty.")
    input_watertight = bool(mesh.is_watertight)
    input_bounds = np.asarray(mesh.bounds, dtype=float)
    input_volume = float(mesh.volume)
    passes = []
    tiny_edge_corrections = []
    for number in range(1, 4):
        mesh, counts = _terrain_repair_pass(mesh)
        correction = None
        if number == 3 and not mesh.is_watertight:
            mesh, correction = _collapse_tiny_overfull_edge(mesh)
        counts["tiny_edge_collapses"] = int(correction is not None)
        if correction is not None:
            tiny_edge_corrections.append(correction)
        passes.append({"pass": number, **counts, **_terrain_repair_defects(mesh)})
        if mesh.is_watertight and mesh.is_winding_consistent:
            break
    diagnostics = {"valid": False, "passes": passes, "source": terrain_path.name,
                   "tiny_edge_corrections": tiny_edge_corrections}
    if not mesh.is_watertight or not mesh.is_winding_consistent:
        defects = passes[-1]
        raise TerrainRepairError(
            "Terrain could not be made manifold for integrated water basins "
            f"after {len(passes)} repair passes "
            f"({defects['remaining_non_manifold_edges']} non-manifold edges; "
            f"winding consistent: {defects['winding_consistent']}).",
            diagnostics)
    if float(mesh.volume) <= 0 or abs(float(mesh.volume) - input_volume) > .01:
        raise TerrainRepairError("Terrain repair unexpectedly changed its volume.", diagnostics)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(output_path)
    exported = trimesh.load_mesh(output_path, process=True)
    if not exported.is_watertight or not exported.is_winding_consistent:
        raise TerrainRepairError("Repaired Terrain did not remain manifold after STL export.", diagnostics)
    output_bounds = np.asarray(exported.bounds, dtype=float)
    if not np.allclose(input_bounds, output_bounds,
                       atol=WATER_BOOLEAN_BOUNDS_TOLERANCE_MM, rtol=0):
        raise TerrainRepairError("Terrain repair unexpectedly changed its bounds.", diagnostics)
    return {
        "input_watertight": input_watertight,
        "output_watertight": True,
        **{key: sum(item[key] for item in passes) for key in counts},
        "passes": passes,
        "tiny_edge_corrections": tiny_edge_corrections,
        "bounds_mm": input_bounds.tolist(),
        "volume_mm3": float(abs(exported.volume)),
    }


class UpperSurface:
    """Small dependency-free XY index for sampling a mesh's upper surface."""

    def __init__(self, mesh, cell_size=2.0):
        triangles = mesh.vertices[mesh.faces]
        normals = np.cross(triangles[:, 1] - triangles[:, 0],
                           triangles[:, 2] - triangles[:, 0])
        usable = normals[:, 2] > 1e-8
        self.triangles = triangles[usable]
        self.cell_size = float(cell_size)
        self.minimum = self.triangles[:, :, :2].min(axis=(0, 1))
        self.cells = {}
        for index, triangle in enumerate(self.triangles):
            low = np.floor((triangle[:, :2].min(axis=0) - self.minimum) /
                           self.cell_size).astype(int)
            high = np.floor((triangle[:, :2].max(axis=0) - self.minimum) /
                            self.cell_size).astype(int)
            for x_value in range(low[0], high[0] + 1):
                for y_value in range(low[1], high[1] + 1):
                    self.cells.setdefault((x_value, y_value), []).append(index)

    def z(self, point):
        point = np.asarray(point, dtype=float)
        cell = tuple(np.floor((point - self.minimum) / self.cell_size).astype(int))
        indices = sorted({index
                          for x_value in range(cell[0] - 1, cell[0] + 2)
                          for y_value in range(cell[1] - 1, cell[1] + 2)
                          for index in self.cells.get((x_value, y_value), ())})
        if not indices:
            return None
        triangles = self.triangles[np.asarray(indices, dtype=int)]
        x, y = point
        x0, y0 = triangles[:, 0, 0], triangles[:, 0, 1]
        x1, y1 = triangles[:, 1, 0], triangles[:, 1, 1]
        x2, y2 = triangles[:, 2, 0], triangles[:, 2, 1]
        denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        usable = np.abs(denominator) > 1e-12
        a = np.zeros(len(triangles))
        b = np.zeros(len(triangles))
        a[usable] = ((y1[usable] - y2[usable]) * (x - x2[usable]) +
                     (x2[usable] - x1[usable]) * (y - y2[usable])) / denominator[usable]
        b[usable] = ((y2[usable] - y0[usable]) * (x - x2[usable]) +
                     (x0[usable] - x2[usable]) * (y - y2[usable])) / denominator[usable]
        c = 1.0 - a - b
        inside = usable & (a >= -1e-7) & (b >= -1e-7) & (c >= -1e-7)
        if not inside.any():
            return None
        values = (a[inside] * triangles[inside, 0, 2] +
                  b[inside] * triangles[inside, 1, 2] +
                  c[inside] * triangles[inside, 2, 2])
        return float(values.max())


def _visible_ocean_footprint(mesh, terrain_surface, terrain_outline):
    triangles = mesh.vertices[mesh.faces]
    normals = np.cross(triangles[:, 1] - triangles[:, 0],
                       triangles[:, 2] - triangles[:, 0])
    polygons = []
    for triangle in triangles[normals[:, 2] > 1e-8]:
        polygon = Polygon(triangle[:, :2]).buffer(0).intersection(terrain_outline).buffer(0)
        if polygon.is_empty:
            continue
        center = np.asarray(polygon.representative_point().coords[0])
        terrain_z = terrain_surface.z(center)
        if terrain_z is None or float(triangle[:, 2].mean()) < terrain_z - WATER_LEVEL_TOLERANCE_MM:
            continue
        if not polygon.is_empty and polygon.area > 1e-8:
            polygons.append(polygon)
    return unary_union(polygons).buffer(0) if polygons else Polygon()


def _printable_water(geometry, terrain_outline):
    radius = WATER_MIN_WIDTH_MM / 2.0
    widened = geometry.buffer(radius, join_style=1).intersection(terrain_outline).buffer(0)
    output = []
    for polygon in polygon_items(widened):
        core = polygon.buffer(-radius, join_style=1)
        if core.is_empty:
            continue
        printable = core.buffer(radius, join_style=1).intersection(terrain_outline).buffer(0)
        for item in polygon_items(printable):
            if item.area < WATER_MIN_COMPONENT_AREA_MM2:
                continue
            printable_holes = []
            for ring in item.interiors:
                hole = Polygon(ring)
                if (hole.area >= WATER_MIN_COMPONENT_AREA_MM2
                        and not hole.buffer(-radius, join_style=1).is_empty):
                    printable_holes.append(list(ring.coords))
            output.append(Polygon(item.exterior.coords, printable_holes).buffer(0))
    return unary_union(output).buffer(0) if output else Polygon()


def _stable_polygon(polygon, cleanup=None):
    """Normalize STL coordinates; return None if no printable fragment remains."""
    maximum = max(abs(value) for value in polygon.bounds)
    float32_spacing = float(np.spacing(np.float32(maximum)))
    grid = max(WATER_COORDINATE_GRID_MM, float32_spacing * 2.0)
    stable = set_precision(polygon, grid, mode="valid_output")
    # Slightly exceed the storage grid so Earcut does not retain nearly
    # collinear shoreline vertices which collapse into an open edge in STL.
    stable = stable.simplify(grid * 1.5,
                             preserve_topology=True).buffer(0)
    items = [item for item in polygon_items(stable)
             if item.area >= WATER_MIN_COMPONENT_AREA_MM2]
    if cleanup is not None:
        cleanup.update(source_area_mm2=float(polygon.area), cleaned_area_mm2=float(stable.area),
                       grid_mm=grid)
    if not items:
        return None
    if len(items) > 1:
        raise ValueError("Water precision cleanup unexpectedly split a connected body.")
    return items[0]


def _printable_smoothed_inland(geometry, outline):
    """Keep smoothed shorelines; widen standalone undersized water as before."""
    parts = polygon_items(geometry)
    output = []
    for index, polygon in enumerate(parts):
        printable = _printable_water(polygon, outline)
        if not polygon.buffer(-WATER_MIN_WIDTH_MM / 2).is_empty:
            printable = printable.intersection(polygon)
        else:
            neighbors = unary_union(parts[:index] + parts[index + 1:])
            printable = printable.difference(neighbors.buffer(WATER_COORDINATE_GRID_MM))
        output.append(printable)
    return unary_union(output).buffer(0)


def _normalized_water_entries(entries, omitted, overlap_corrections, enforce_width=False):
    """Normalize first, then keep duplicate ocean solids out of inland water.

    Inland water retains its shoreline-driven heights. Ocean fragments keep
    their existing sea level, but only cover areas not already supplied by an
    inland body. The existing topology inset separates shared floor edges.
    """
    normalized = []
    for source_index, (source, level, depth, samples) in enumerate(entries, 1):
        cleanup = {}
        polygon = _stable_polygon(source, cleanup=cleanup)
        if polygon is None:
            omitted.append({"source_body": source_index, **cleanup,
                            "area_mm2": cleanup["cleaned_area_mm2"],
                            "reason": "below printable area after precision cleanup",
                            "replacement": "existing terrain"})
        else:
            normalized.append((source_index, polygon, level, depth, samples))
    inland = set_precision(unary_union([p for _, p, level, _, _ in normalized
                                       if level is None]), 0)
    exclusion = inland.buffer(WATER_TOPOLOGY_INSET_MM, join_style=2)
    result = []
    for source_index, polygon, level, depth, samples in normalized:
        if level is None or inland.is_empty or not polygon.intersects(inland):
            result.append((source_index, polygon, level, depth, samples))
            continue
        # A retained .01 mm precision model would erase the .001 mm separation.
        original = set_precision(polygon, 0)
        clipped = original.difference(exclusion)
        if clipped.difference(original).area > 1e-8:
            raise ValueError("Ocean overlap correction unexpectedly expanded the shoreline.")
        kept = []
        for part in polygon_items(clipped):
            if (part.area < WATER_MIN_COMPONENT_AREA_MM2
                    or (enforce_width and part.buffer(-WATER_MIN_WIDTH_MM / 2).is_empty)):
                omitted.append({"source_body": source_index, "area_mm2": float(part.area),
                                "reason": "below printable area or width after ocean overlap cleanup",
                                "replacement": "existing terrain"})
            else:
                kept.append(part)
                result.append((source_index, part, level, depth, samples))
        overlap_corrections.append({
            "source_body": source_index, "retained_water": "inland",
            "overlap_area_mm2": float(original.intersection(inland).area),
            "removed_area_mm2": float(original.area - clipped.area),
            "separation_mm": WATER_TOPOLOGY_INSET_MM, "resulting_body_count": len(kept),
        })
    return result


def _surface_triangulation(polygon, surface):
    if polygon.is_empty or polygon.geom_type != "Polygon":
        raise ValueError("A water body lies outside the terrain surface.")
    # trimesh's Earcut adapter includes each ring's repeated closing vertex.
    # At clipped shorelines Earcut can reference both copies, producing
    # geometrically coincident edges that become non-manifold in STL.
    from mapbox_earcut import triangulate_float64
    rings = [np.asarray(polygon.exterior.coords[:-1], dtype=float)]
    rings.extend(np.asarray(ring.coords[:-1], dtype=float)
                 for ring in polygon.interiors)
    vertices = np.vstack(rings)
    ring_ends = np.cumsum([len(ring) for ring in rings], dtype=np.uint32)
    faces = triangulate_float64(vertices, ring_ends).reshape((-1, 3))
    flat = np.column_stack((np.asarray(vertices, dtype=float),
                            np.zeros(len(vertices))))
    faces = np.asarray(faces, dtype=np.int64)
    center = np.asarray(polygon.representative_point().coords[0], dtype=float)
    heights = []
    for point in flat[:, :2]:
        value = surface.z(point)
        if value is None:
            direction = center - point
            length = float(np.linalg.norm(direction))
            if length > 1e-12:
                direction /= length
                for distance in (0.01, 0.03, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0):
                    value = surface.z(point + direction * distance)
                    if value is not None:
                        break
        heights.append(value)
    if any(value is None or not math.isfinite(value) for value in heights):
        raise ValueError("A water body lies outside the terrain surface.")
    flat[:, 2] = np.asarray(heights, dtype=float)
    distinct = ((faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2])
                & (faces[:, 0] != faces[:, 2]))
    faces = faces[distinct]
    canonical = np.sort(faces, axis=1)
    _, unique_indices = np.unique(canonical, axis=0, return_index=True)
    faces = faces[np.sort(unique_indices)]
    first = flat[faces[:, 1], :2] - flat[faces[:, 0], :2]
    second = flat[faces[:, 2], :2] - flat[faces[:, 0], :2]
    signed_area = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
    usable = np.abs(signed_area) > 1e-10
    faces, signed_area = faces[usable], signed_area[usable]
    reversed_faces = signed_area < 0
    faces[reversed_faces] = faces[reversed_faces][:, [0, 2, 1]]
    edge_counts = np.unique(
        np.sort(np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])),
                axis=1), axis=0, return_counts=True)[1]
    if len(edge_counts) == 0 or edge_counts.max() > 2:
        raise ValueError("Terrain-following water surface is non-manifold.")
    return flat, np.asarray(faces, dtype=np.int64)


def _sample_shoreline(polygon, surface, terrain_outline):
    """Sample only nearby land, excluding the model perimeter and water itself."""
    safe_outline = terrain_outline.buffer(-WATER_OUTLINE_MARGIN_MM)
    ring = (polygon.buffer(WATER_SHORE_OUTER_MM, join_style=1)
            .difference(polygon.buffer(WATER_SHORE_INNER_MM, join_style=1))
            .intersection(safe_outline).buffer(0))
    if ring.is_empty:
        ring = (polygon.buffer(WATER_SHORE_OUTER_MM, join_style=1)
                .difference(polygon.buffer(WATER_SHORE_INNER_MM, join_style=1))
                .intersection(terrain_outline).buffer(0))
    min_x, min_y, max_x, max_y = ring.bounds
    grid_x = np.arange(min_x, max_x + WATER_SURFACE_SAMPLE_MM,
                       WATER_SURFACE_SAMPLE_MM)
    grid_y = np.arange(min_y, max_y + WATER_SURFACE_SAMPLE_MM,
                       WATER_SURFACE_SAMPLE_MM)
    xx, yy = np.meshgrid(grid_x, grid_y)
    selected = contains_xy(ring, xx.ravel(), yy.ravel())
    points = np.column_stack((xx.ravel()[selected], yy.ravel()[selected]))
    samples = []
    for point in points:
        value = surface.z(point)
        if value is not None and math.isfinite(value):
            samples.append((float(point[0]), float(point[1]), float(value)))
    if len(samples) < 3:
        raise ValueError("Could not sample terrain immediately beside a water body.")
    return np.asarray(samples, dtype=float)


def _natural_shoreline_samples(samples, polygon, trail_exclusion):
    """Keep nearby original-shore samples while ignoring the carved trail."""
    clean = samples
    if not trail_exclusion.is_empty:
        outside_trail = ~contains_xy(
            trail_exclusion, samples[:, 0], samples[:, 1])
        clean = samples[outside_trail]
    if len(clean) < 3:
        raise ValueError(
            "Could not sample the natural shoreline outside the trail groove.")
    for margin in (WATER_SHORE_OUTER_MM + WATER_SURFACE_SAMPLE_MM,
                   2.0, 4.0, 8.0):
        nearby = polygon.buffer(margin, join_style=1)
        selected = contains_xy(nearby, clean[:, 0], clean[:, 1])
        if np.count_nonzero(selected) >= 3:
            return clean[selected]
    raise ValueError("Could not associate natural shoreline samples with water.")


def _expanded_shoreline_samples(original, fragment, surface, terrain_outline,
                                water_exclusion, trail_exclusion):
    """Find nearby natural land when the route occupies the narrow shore band."""
    safe_outline = terrain_outline.buffer(-WATER_OUTLINE_MARGIN_MM)
    for radius in (2.0, 4.0, 8.0):
        ring = (original.buffer(radius, join_style=1)
                .difference(original.buffer(WATER_SHORE_INNER_MM, join_style=1))
                .difference(water_exclusion)
                .difference(trail_exclusion)
                .intersection(safe_outline)
                .intersection(fragment.buffer(radius, join_style=1)).buffer(0))
        if ring.is_empty:
            continue
        min_x, min_y, max_x, max_y = ring.bounds
        xs = np.arange(min_x, max_x + WATER_SURFACE_SAMPLE_MM, WATER_SURFACE_SAMPLE_MM)
        ys = np.arange(min_y, max_y + WATER_SURFACE_SAMPLE_MM, WATER_SURFACE_SAMPLE_MM)
        xx, yy = np.meshgrid(xs, ys)
        selected = contains_xy(ring, xx.ravel(), yy.ravel())
        points = np.column_stack((xx.ravel()[selected], yy.ravel()[selected]))
        samples = []
        for point in points:
            value = surface.z(point)
            if value is not None and math.isfinite(value):
                samples.append((float(point[0]), float(point[1]), float(value)))
        if len(samples) >= 3:
            return np.asarray(samples, dtype=float), radius
    raise ValueError("Could not sample reliable natural shoreline outside water, "
                     "the trail groove, and the model perimeter within 8 mm.")


def _snap_down(values, step=WATER_LAYER_HEIGHT_MM):
    values = np.asarray(values, dtype=float)
    return np.floor((values + 1e-7) / step) * step


def _snap_up(values, step=WATER_LAYER_HEIGHT_MM):
    values = np.asarray(values, dtype=float)
    return np.ceil((values - 1e-7) / step) * step


def _dominant_shoreline_samples(samples):
    """Drop disconnected height clusters caused by the base/model perimeter."""
    order = np.argsort(samples[:, 2])
    ordered = samples[order]
    gaps = np.diff(ordered[:, 2])
    split_at = np.where(gaps > WATER_FLAT_RANGE_MM)[0] + 1
    clusters = np.split(ordered, split_at)
    dominant = max(clusters, key=len)
    return dominant, int(len(samples) - len(dominant))


def _shoreline_surface(top_vertices, samples):
    """Return a flat or locally quantized surface based only on nearby land."""
    samples, discarded = _dominant_shoreline_samples(samples)
    heights = samples[:, 2]
    low, median, high = np.percentile(heights, (10, 50, 90))
    spread = float(high - low)
    if spread <= WATER_FLAT_RANGE_MM:
        # Use the first printable layer at or above the robust low shoreline.
        # This avoids a visible trench without letting high banks lift a lake.
        level = float(_snap_up([low])[0])
        return np.full(len(top_vertices), level), "shoreline-flat", {
            "shoreline_p10_z_mm": float(low),
            "shoreline_median_z_mm": float(median),
            "shoreline_p90_z_mm": float(high),
            "shoreline_range_mm": spread,
            "shoreline_discarded_samples": discarded,
        }

    # Interpolate from nearby shoreline samples. The samples have already been
    # robustly filtered, so inverse-distance weighting can bridge both banks
    # without letting one isolated DEM value control an interior point.
    xy = samples[:, :2]
    output = []
    nearest_count = min(32, len(samples))
    for point in top_vertices[:, :2]:
        distances = np.sum((xy - point) ** 2, axis=1)
        nearest = np.argpartition(distances, nearest_count - 1)[:nearest_count]
        design = np.column_stack((xy[nearest], np.ones(nearest_count)))
        coefficients, _, rank, _ = np.linalg.lstsq(
            design, heights[nearest], rcond=None)
        if rank == 3:
            output.append(float(np.dot((*point, 1.0), coefficients)))
            continue
        weights = 1.0 / np.maximum(distances[nearest], 0.01)
        output.append(float(np.average(heights[nearest], weights=weights)))
    return _snap_down(output), "shoreline-stepped", {
        "shoreline_p10_z_mm": float(low),
        "shoreline_median_z_mm": float(median),
        "shoreline_p90_z_mm": float(high),
        "shoreline_range_mm": spread,
        "shoreline_discarded_samples": discarded,
    }


def _ocean_surface_level(mesh):
    triangles = mesh.vertices[mesh.faces]
    normals = np.cross(triangles[:, 1] - triangles[:, 0],
                       triangles[:, 2] - triangles[:, 0])
    upward = triangles[normals[:, 2] > 1e-8]
    if len(upward) == 0:
        raise ValueError("Ocean geometry has no printable upper surface.")
    return float(_snap_down([np.median(upward[:, :, 2])])[0])


def _coastal_source(path, outline):
    """Read the ocean before layer-priority cuts remove bays and estuaries."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("expected"), bool)):
            raise ValueError("unsupported coastal metadata")
        sources = data["sources"]
        if not isinstance(sources, list) or bool(sources) != data["expected"]:
            raise ValueError("missing coastal source geometry")
        footprints, levels = [], []
        for source in sources:
            vertices = np.asarray(source["vertices"], dtype=float)
            faces = np.asarray(source["faces"], dtype=np.int64)
            if (vertices.ndim != 2 or vertices.shape[1] != 3 or not len(vertices)
                    or not np.isfinite(vertices).all() or faces.ndim != 2
                    or faces.shape[1] != 3 or not len(faces)
                    or faces.min() < 0 or faces.max() >= len(vertices)):
                raise ValueError("invalid coastal source mesh")
            mesh = trimesh.Trimesh(vertices, faces, process=True)
            levels.append(_ocean_surface_level(mesh))
            mesh.update_faces(mesh.face_normals[:, 2] > 1e-8)
            footprint = projected_footprint(mesh).intersection(outline)
            if footprint.is_empty:
                raise ValueError("coastal source is outside the terrain")
            footprints.append(footprint)
        if not levels:
            return Polygon(), None, {"available": False, "source_count": 0}
        if max(levels) - min(levels) > WATER_LEVEL_TOLERANCE_MM:
            raise ValueError("conflicting coastal sea levels")
        level = float(_snap_down([np.median(levels)])[0])
        coverage = unary_union(footprints).buffer(0)
        return coverage, level, {"available": True, "source_count": len(sources),
                                 "sea_level_mm": level, "coverage_area_mm2": coverage.area}
    except (OSError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"Invalid coastal water metadata: {exc}") from exc


def _boundary_edges(faces):
    directed = np.vstack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    canonical = np.sort(directed, axis=1)
    _, inverse, counts = np.unique(canonical, axis=0, return_inverse=True,
                                   return_counts=True)
    return directed[counts[inverse] == 1]


def _terrain_shell(top_vertices, faces, bottom_z=None, top_z=None,
                   depth=WATER_DEPTH_MM):
    """Create a closed shell between matching XY surfaces."""
    top = np.asarray(top_vertices, dtype=float).copy()
    bottom = top.copy()
    bottom[:, 2] = (top[:, 2] - depth if bottom_z is None
                    else np.asarray(bottom_z, dtype=float))
    if top_z is not None:
        top[:, 2] = float(top_z)
    count = len(top)
    boundary = _boundary_edges(faces)
    sides = []
    for first, second in boundary:
        sides.extend(((int(first), int(first + count), int(second + count)),
                      (int(first), int(second + count), int(second))))
    shell_faces = np.vstack((faces, faces[:, ::-1] + count,
                             np.asarray(sides, dtype=np.int64)))
    mesh = trimesh.Trimesh(vertices=np.vstack((top, bottom)), faces=shell_faces,
                           process=False)
    mesh.merge_vertices(digits_vertex=6)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    return mesh



def _water_body_meshes(polygon, surface, samples, ocean_level, depth, cutter_top, support_base):
    top, faces = _surface_triangulation(polygon, surface)
    shoreline = None
    if ocean_level is None:
        levels, mode, shoreline = _shoreline_surface(top, samples)
        top[:, 2] = levels
    else:
        mode = "ocean-flat"
        top[:, 2] = ocean_level
    bottom = top[:, 2] - depth
    cap = _terrain_shell(top, faces, depth=depth)
    cutter = _terrain_shell(top, faces, bottom, top_z=cutter_top, depth=depth)
    support_top = top.copy()
    support_top[:, 2] = bottom
    support = _terrain_shell(support_top, faces, support_base, depth=depth)
    layers = set()
    for value in top[:, 2]:
        top_layer = int(round(value / WATER_LAYER_HEIGHT_MM))
        bottom_layer = int(round((value - depth) / WATER_LAYER_HEIGHT_MM))
        layers.update(range(bottom_layer + 1, top_layer + 1))
    body = {"area_mm2": float(polygon.area), "surface_min_z_mm": float(top[:, 2].min()),
            "surface_max_z_mm": float(top[:, 2].max()), "depth_mm": float(depth),
            "surface_mode": mode, "layer_bands": sorted(layers)}
    if shoreline:
        body.update(shoreline)
    return (cap, cutter, support), body


def _water_shells_valid(meshes):
    """Check the real float32 STL representation as well as the in-memory shell."""
    for mesh in meshes:
        if (mesh.is_empty or not np.isfinite(mesh.vertices).all()
                or not mesh.is_watertight or not mesh.is_winding_consistent):
            return False
        exported = trimesh.load_mesh(io.BytesIO(mesh.export(file_type="stl")),
                                     file_type="stl", process=True)
        if not exported.is_watertight or not exported.is_winding_consistent:
            return False
    return True


def _separated_support_floor(base):
    """Avoid coincident support/terrain bottom sheets after marine joins."""
    separated = float(np.nextafter(np.float32(base), np.float32(np.inf)))
    if abs(separated) < np.finfo(np.float32).eps:
        separated = float(np.finfo(np.float32).eps)
    if not math.isfinite(separated) or not 0 < separated - base <= .000001:
        raise ValueError("Coastal support floor cannot be separated safely at STL precision.")
    return separated


def _regularize_water_polygon(polygon, enforce_width=False):
    # A coarse Shapely precision model would snap this tiny inset back onto
    # itself. Retain full precision after the initial shoreline cleanup.
    original = set_precision(polygon, 0)
    # Straight joins avoid adding sub-micron arc segments at this tiny scale,
    # which can create overlapping floor facets in the subsequent Boolean.
    corrected = original.buffer(-WATER_TOPOLOGY_INSET_MM, join_style=2)
    if corrected.difference(original).area > 1e-8:
        raise ValueError("Water topology correction unexpectedly expanded the shoreline.")
    kept, omitted = [], []
    for part in polygon_items(corrected):
        if (part.area < WATER_MIN_COMPONENT_AREA_MM2
                or (enforce_width and part.buffer(-WATER_MIN_WIDTH_MM / 2).is_empty)):
            omitted.append({"area_mm2": float(part.area),
                            "reason": "below printable area or width after topology correction",
                            "replacement": "existing terrain"})
        else:
            kept.append(part)
    return kept, omitted, {"inset_mm": WATER_TOPOLOGY_INSET_MM,
                           "removed_area_mm2": float(original.area - corrected.area),
                           "resulting_body_count": len(kept)}

def prepare_integrated_water(terrain_path: Path, water_paths, cap_path: Path,
                             cutter_path: Path, support_path: Path = None,
                             trail_path: Path = None,
                             trail_clearance_mm: float = 0.0,
                             protected_corridor=None, coastal_source_path=None,
                             original_terrain_path=None) -> dict:
    """Create shoreline-driven water inlays and matching terrain geometry."""
    terrain = trimesh.load_mesh(terrain_path, process=True)
    if terrain.is_empty:
        raise ValueError("Terrain mesh is empty.")
    surface = UpperSurface(terrain)
    outline = MultiPoint(terrain.vertices[:, :2]).convex_hull.buffer(0)
    coverage, sea_level, coastal_report = (
        _coastal_source(coastal_source_path, outline) if coastal_source_path is not None
        else (Polygon(), None, {"available": False, "reason": "legacy input without provenance"}))
    if original_terrain_path is not None:
        # Keep two-sided overlays on one sub-print-scale grid; mixing unsnapped
        # snapshots with rounded shoreline edits creates almost coincident edges.
        coverage = set_precision(coverage, WATER_TOPOLOGY_INSET_MM)
    inland_footprints = []
    ocean_footprints = []
    for path in water_paths:
        mesh = trimesh.load_mesh(path, process=True)
        if mesh.is_empty:
            continue
        if path.stem.lower().endswith("ocean"):
            footprint = _visible_ocean_footprint(mesh, surface, outline)
            if not footprint.is_empty:
                ocean_footprints.append((footprint, _ocean_surface_level(mesh)))
        else:
            footprint = projected_footprint(mesh)
            if not footprint.is_empty:
                inland_footprints.append(footprint)
    if not inland_footprints and not ocean_footprints:
        return {"available": False, "reason": "no visible water footprint"}
    if coastal_source_path is not None and ocean_footprints and sea_level is None:
        raise ValueError("Ocean geometry has no original coastal water metadata.")
    entries = []
    smoothing_reports = []
    restoration_regions = []
    restore_land = original_terrain_path is not None
    smoothing_protection = protected_corridor
    if smoothing_protection is None and trail_path is not None:
        smoothing_protection = trail_corridor(
            trail_path, float(trail_clearance_mm) + TRAIL_WATER_PRECISION_MARGIN_MM)
    if sea_level is not None:
        inland_raw = unary_union(inland_footprints)
        marine_raw = unary_union([p for p, _ in ocean_footprints])
        if restore_land:
            inland_raw = set_precision(inland_raw, WATER_TOPOLOGY_INSET_MM)
            marine_raw = set_precision(marine_raw, WATER_TOPOLOGY_INSET_MM)
        coastal_water = inland_raw.intersection(coverage)
        coastal_report["reclassified_area_mm2"] = float(coastal_water.area)
        # Classify before width cleanup so nearby lakes cannot become marine
        # simply because their printable outlines touch after rounding.
        independent_raw = inland_raw.difference(coverage)
        # STL rounding can leave hairline residues just outside the coast.
        # Do not let width cleanup inflate these into spurious inland water.
        independent_parts = polygon_items(independent_raw)
        independent_raw = unary_union([p for p in independent_parts
                                       if p.area >= WATER_MIN_COMPONENT_AREA_MM2])
        coastal_report["unprintable_inland_area_mm2"] = float(sum(
            p.area for p in independent_parts if p.area < WATER_MIN_COMPONENT_AREA_MM2))
        printable_coverage, smoothing = smooth_shoreline(
            coverage, outline, smoothing_protection, independent_raw, smooth_water=restore_land)
        restoration_regions.append(coverage.difference(printable_coverage))
        smoothing_reports.append({"kind": "marine", **smoothing})
        smoothed_inland, smoothing = smooth_shoreline(
            independent_raw, outline, smoothing_protection, printable_coverage,
            smooth_water=restore_land)
        restoration_regions.append(independent_raw.difference(smoothed_inland))
        smoothing_reports.append({"kind": "inland", **smoothing})
        independent = _printable_smoothed_inland(smoothed_inland, outline)
        independent = independent.difference(printable_coverage).buffer(0)
        entries.extend((p, None, WATER_DEPTH_MM, None) for p in polygon_items(independent))
        marine_raw = (marine_raw.union(coastal_water)
                      .union(printable_coverage.difference(coverage))
                      .intersection(printable_coverage).buffer(0))
        # Layer-priority cuts leave clearance gaps between ocean and bay meshes.
        # Keep the cleanup's restored connections inside the original ocean,
        # rather than clipping them back to those already-separated inputs.
        marine = _printable_water(marine_raw, outline).intersection(printable_coverage).buffer(0)
        coastal_report["seam_cleanup"] = {
            "radius_mm": WATER_MIN_WIDTH_MM / 2.0,
            "restored_area_mm2": float(marine.difference(marine_raw).area),
            "input_component_count": len(polygon_items(marine_raw)),
            "output_component_count": len(polygon_items(marine)),
        }
        entries.extend((p, sea_level, OCEAN_DEPTH_MM, None) for p in polygon_items(marine))
    elif inland_footprints:
        smoothed_inland, smoothing = smooth_shoreline(
            unary_union(inland_footprints).buffer(0), outline, smoothing_protection,
            smooth_water=restore_land)
        restoration_regions.append(unary_union(inland_footprints).difference(smoothed_inland))
        smoothing_reports.append({"kind": "inland", **smoothing})
        inland = _printable_smoothed_inland(smoothed_inland, outline)
        entries.extend((polygon, None, WATER_DEPTH_MM, None)
                       for polygon in polygon_items(inland))
    for ocean, level in ocean_footprints if sea_level is None else []:
        printable = _printable_water(ocean, outline)
        entries.extend((polygon, level, OCEAN_DEPTH_MM, None)
                       for polygon in polygon_items(printable))
    trail_exclusion = Polygon()
    excluded_water_area = 0.0
    shoreline_fallbacks = []
    omitted_fragments = []
    water_exclusion = unary_union([entry[0] for entry in entries])
    if trail_path is not None:
        trail_exclusion = protected_corridor if protected_corridor is not None else trail_corridor(
            trail_path,
            float(trail_clearance_mm) + TRAIL_WATER_PRECISION_MARGIN_MM)
        protected_entries = []
        for polygon, level, depth, _samples in entries:
            clipped = polygon.difference(trail_exclusion).buffer(0)
            excluded_water_area += max(0.0, float(polygon.area - clipped.area))
            natural_samples = None
            if level is None:
                try:
                    natural_samples = _sample_shoreline(polygon, surface, outline)
                except ValueError:
                    # The bounded fallback below still requires actual nearby land.
                    pass
            for item in polygon_items(clipped):
                if (item.area < WATER_MIN_COMPONENT_AREA_MM2
                        or (protected_corridor is not None
                            and item.buffer(-WATER_MIN_WIDTH_MM / 2).is_empty)):
                    omitted_fragments.append({"area_mm2": float(item.area),
                                              "reason": "below printable area or width",
                                              "replacement": "existing terrain"})
                    continue
                local_samples = None
                if level is None:
                    try:
                        if natural_samples is None:
                            raise ValueError("No narrow-band shoreline samples.")
                        local_samples = _natural_shoreline_samples(
                            natural_samples, item, trail_exclusion)
                    except ValueError:
                        local_samples, radius = _expanded_shoreline_samples(
                            polygon, item, surface, outline, water_exclusion, trail_exclusion)
                        shoreline_fallbacks.append({
                            "radius_mm": radius, "sample_count": len(local_samples),
                            "water_fragment_area_mm2": float(item.area),
                        })
                protected_entries.append((item, level, depth, local_samples))
        entries = protected_entries
    if not entries:
        return {"available": False, "reason": "no printable water footprint",
                "trail_clearance_mm": float(trail_clearance_mm),
                "trail_precision_margin_mm": TRAIL_WATER_PRECISION_MARGIN_MM,
                "trail_excluded_water_area_mm2": excluded_water_area,
                "omitted_fragments": omitted_fragments,
                "shoreline_fallbacks": shoreline_fallbacks}
    caps, cutters, supports, bodies = [], [], [], []
    cutter_top = float(terrain.bounds[1, 2] + 1.0)
    support_base = float(terrain.bounds[0, 2])
    seam = coastal_report.get("seam_cleanup", {})
    if seam.get("input_component_count", 0) > seam.get("output_component_count", 0):
        separated_base = _separated_support_floor(support_base)
        coastal_report["support_floor_separation"] = {
            "terrain_base_z_mm": support_base, "support_base_z_mm": separated_base,
            "separation_mm": separated_base - support_base,
        }
        support_base = separated_base
    topology_corrections = []
    overlap_corrections = []
    normalized_entries = _normalized_water_entries(
        entries, omitted_fragments, overlap_corrections,
        enforce_width=protected_corridor is not None)
    for source_index, polygon, ocean_level, depth, natural_samples in normalized_entries:
        samples = None
        if ocean_level is None:
            samples = (natural_samples if natural_samples is not None else
                       _sample_shoreline(polygon, surface, outline))
        try:
            meshes, body = _water_body_meshes(
                polygon, surface, samples, ocean_level, depth, cutter_top, support_base)
            valid = _water_shells_valid(meshes)
        except ValueError as exc:
            if "non-manifold" not in str(exc):
                raise
            valid = False
        if valid:
            cap, cutter, support = meshes
            caps.append(cap)
            cutters.append(cutter)
            supports.append(support)
            bodies.append(body)
            continue
        parts, omitted, correction = _regularize_water_polygon(
            polygon, enforce_width=protected_corridor is not None)
        correction["source_body"] = source_index
        topology_corrections.append(correction)
        omitted_fragments.extend(omitted)
        for part in parts:
            meshes, body = _water_body_meshes(
                part, surface, samples, ocean_level, depth, cutter_top, support_base)
            if not _water_shells_valid(meshes):
                raise ValueError(
                    f"Integrated water body {source_index} is not watertight after "
                    f"a {WATER_TOPOLOGY_INSET_MM:.3f} mm topology correction.")
            cap, cutter, support = meshes
            caps.append(cap)
            cutters.append(cutter)
            supports.append(support)
            body["topology_correction_source_body"] = source_index
            bodies.append(body)
    if not caps:
        return {"available": False, "reason": "no printable water footprint",
                "omitted_fragments": omitted_fragments,
                "water_topology_corrections": topology_corrections,
                "water_overlap_corrections": overlap_corrections,
                "shoreline_fallbacks": shoreline_fallbacks}
    cap_mesh = trimesh.util.concatenate(caps)
    cutter_mesh = trimesh.util.concatenate(cutters)
    support_mesh = trimesh.util.concatenate(supports)
    if (not cap_mesh.is_watertight or not cutter_mesh.is_watertight
            or not support_mesh.is_watertight):
        failures = []
        if not cap_mesh.is_watertight:
            failures.append("water")
        if not cutter_mesh.is_watertight:
            failures.append("cutter")
        if not support_mesh.is_watertight:
            failures.append("support")
        raise ValueError("Integrated water geometry is not watertight: "
                         + ", ".join(failures) + ".")
    cap_mesh.export(cap_path)
    cutter_mesh.export(cutter_path)
    if support_path is not None:
        support_mesh.export(support_path)
    failures = []
    exports = [("water", cap_path), ("cutter", cutter_path)]
    if support_path is not None:
        exports.append(("support", support_path))
    for label, path in exports:
        exported = trimesh.load_mesh(path, process=True)
        if not exported.is_watertight or not exported.is_winding_consistent:
            failures.append(label)
    if failures:
        raise ValueError(
            "Integrated water geometry did not remain watertight after STL export: "
            + ", ".join(failures) + ".")
    restoration = {"available": False, "area_mm2": 0.0}
    if restore_land:
        mask = unary_union(restoration_regions).buffer(0)
        if not mask.is_empty:
            original = trimesh.load_mesh(original_terrain_path, process=True)
            if (original.is_empty or not np.isfinite(original.vertices).all()
                    or not np.allclose(original.bounds[:, :2], terrain.bounds[:, :2],
                                       atol=WATER_BOOLEAN_BOUNDS_TOLERANCE_MM, rtol=0)
                    or abs(original.bounds[0, 2] - terrain.bounds[0, 2]) > .01
                    or original.bounds[1, 2] < terrain.bounds[1, 2] - .01):
                raise ValueError("Original shoreline terrain is not aligned with this model.")
            original_surface = UpperSurface(original)
            samples = []
            interior = mask.buffer(-WATER_COORDINATE_GRID_MM * 2)
            for polygon in sorted(polygon_items(interior), key=lambda p: p.area, reverse=True)[:128]:
                point = polygon.representative_point()
                height = original_surface.z((point.x, point.y))
                if height is None:
                    raise ValueError("Original terrain does not cover a smoothed inlet.")
                samples.append({"xy_mm": [point.x, point.y], "original_z_mm": float(height)})
            restoration = {"available": True, "area_mm2": float(mask.area),
                           "mode": "rebuild-from-original-terrain", "samples": samples}
    if sea_level is not None:
        # Inspect the binary STL, preserving face order to identify each cap.
        exported = trimesh.load_mesh(cap_path, process=False)
        offset = 0
        marine_heights = []
        for cap, body in zip(caps, bodies):
            end = offset + len(cap.faces)
            if body["surface_mode"] == "ocean-flat":
                selected = exported.faces[offset:end]
                normals = exported.face_normals[offset:end]
                heights = exported.vertices[selected[normals[:, 2] > .99], 2]
                if not len(heights) or not np.allclose(heights, sea_level, atol=1e-5, rtol=0):
                    raise ValueError("Exported coastal water surfaces disagree on sea level.")
                marine_heights.extend([float(heights.min()), float(heights.max())])
            offset = end
        coastal_report["exported_surface_range_mm"] = (
            [min(marine_heights), max(marine_heights)] if marine_heights else None)
    return {"available": True, "body_count": len(bodies), "bodies": bodies,
            "minimum_width_mm": WATER_MIN_WIDTH_MM,
            "depth_mm": WATER_DEPTH_MM,
            "surface_sample_mm": WATER_SURFACE_SAMPLE_MM,
            "surface_mode": "shoreline-driven",
            "coastal_classification": coastal_report,
            "shoreline_smoothing": smoothing_reports,
            "land_restoration": restoration,
            "trail_clearance_mm": float(trail_clearance_mm),
            "trail_precision_margin_mm": TRAIL_WATER_PRECISION_MARGIN_MM,
            "trail_excluded_water_area_mm2": excluded_water_area,
            "omitted_fragments": omitted_fragments,
            "shoreline_fallbacks": shoreline_fallbacks,
            "cap_watertight": True,
            "cutter_watertight": True,
            "support_watertight": True,
            "water_topology_corrections": topology_corrections,
            "water_overlap_corrections": overlap_corrections}


def _union_projected_material(pieces, mesh):
    """Retry numerical overlay failures at bounded, sub-STL precision.

    The sum of input boundary lengths bounds the area affected by snapping;
    checking inputs first keeps this from disguising invalid source polygons.
    STL coordinates and clearance thresholds are never changed.
    """
    if not pieces:
        return Polygon()
    try:
        return unary_union(pieces).buffer(0)
    except ShapelyError as error:
        if any(not piece.is_valid or not np.isfinite(piece.bounds).all() for piece in pieces):
            raise ValueError("Trail projection contains invalid input polygons.") from error
        displacement = math.sqrt(2) * TRAIL_PROJECTION_GRID_MM
        area_bound = (2 * sum(piece.length for piece in pieces) * displacement
                      + 2 * len(pieces) * math.pi * displacement ** 2)
        if not math.isfinite(area_bound) or area_bound > TRAIL_FIT_AREA_TOLERANCE_MM2:
            raise ValueError("Trail projection precision repair exceeds its area budget.") from error
        projected = union_all(pieces, grid_size=TRAIL_PROJECTION_GRID_MM)
        if projected.is_empty or not projected.is_valid or not math.isfinite(projected.area):
            raise ValueError("Trail projection precision repair did not produce valid material.")
        mesh.metadata["projection_precision_correction"] = {
            "grid_mm": TRAIL_PROJECTION_GRID_MM,
            "maximum_coordinate_displacement_mm": displacement,
            "area_error_bound_mm2": area_bound,
            "input_polygons": len(pieces), "original_error": str(error),
        }
        return projected


def _material_above_floor(mesh, floor_z):
    """Project material above the pocket floor, including clipped sloping faces."""
    if mesh.is_empty or not np.isfinite(mesh.vertices).all():
        raise ValueError("Wall clearance received empty or non-finite geometry.")
    triangles = mesh.triangles
    high = triangles[:, :, 2] > floor_z + TRAIL_FIT_Z_TOLERANCE_MM
    complete = triangles[np.all(high, axis=1), :, :2]
    if len(complete):
        a, b = complete[:, 1] - complete[:, 0], complete[:, 2] - complete[:, 0]
        area = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])
        pieces = list(polygons(complete[area > 1e-10]))
    else:
        pieces = []
    level = floor_z + TRAIL_FIT_Z_TOLERANCE_MM
    for triangle in triangles[np.any(high, axis=1) & ~np.all(high, axis=1)]:
        vertices = []
        for index, a in enumerate(triangle):
            b = triangle[(index + 1) % 3]
            if a[2] > level:
                vertices.append(a[:2])
            if (a[2] > level) != (b[2] > level):
                vertices.append((a + (b - a) * (level - a[2]) / (b[2] - a[2]))[:2])
        if len(vertices) >= 3:
            polygon = Polygon(vertices)
            if polygon.area > 1e-10:
                pieces.append(polygon)
    return _union_projected_material(pieces, mesh)


def _validate_wall_clearance(terrain, trail, water, clearance_mm):
    if trail.is_empty or not np.isfinite(trail.vertices).all():
        raise ValueError("Could not evaluate the trail wall footprint reliably.")
    floor_z = float(trail.bounds[0, 2])
    # Flat contact facets at the floor are not inserted walls. Some exported
    # meshes contain floor-only triangulation extending beyond the raised strip.
    footprint = _material_above_floor(trail, floor_z)
    if footprint.is_empty:
        raise ValueError("Could not evaluate the trail wall footprint reliably.")
    footprint = unary_union([
        Polygon(polygon.exterior, [ring for ring in polygon.interiors
                                  if Polygon(ring).area >= TRAIL_NUMERICAL_OPENING_MM2])
        for polygon in polygon_items(footprint)
        if polygon.area >= TRAIL_NUMERICAL_OPENING_MM2
    ])
    terrain_material = _material_above_floor(terrain, floor_z)
    water_material = (_material_above_floor(water, floor_z)
                      if water is not None else Polygon())
    material = terrain_material.union(water_material)
    boundaries, errors = [], []
    for component, polygon in enumerate(polygon_items(footprint), 1):
        rings = [("outer", polygon.exterior, None)]
        rings.extend(("inner", ring, Polygon(ring)) for ring in polygon.interiors)
        for index, (kind, ring, opening) in enumerate(rings):
            small_water = (opening is not None
                           and opening.area <= TRAIL_SMALL_WATER_OPENING_MM2
                           and opening.intersection(water_material).area
                           > TRAIL_NUMERICAL_OPENING_MM2)
            required = max(float(clearance_mm), TRAIL_SMALL_WATER_CLEARANCE_MM
                           if small_water else float(clearance_mm))
            # No raised material means this is an exposed edge, without a wall
            # restricting insertion. Keep that distinction explicit in the report.
            distance = float(ring.distance(material)) if not material.is_empty else None
            location = (list(nearest_points(ring, material)[0].coords[0])
                        if distance is not None else None)
            valid = (distance is None or distance + TRAIL_WALL_NUMERICAL_TOLERANCE_MM
                     >= required)
            boundary = {
                "component": component, "boundary": index, "type": kind,
                "small_water_opening": bool(small_water),
                "opening_area_mm2": float(opening.area) if opening is not None else None,
                "required_clearance_mm": required,
                "measured_clearance_mm": distance, "tightest_xy_mm": location,
                "valid": bool(valid),
            }
            boundaries.append(boundary)
            if not valid:
                label = "small enclosed water opening" if small_water else f"{kind} wall"
                errors.append(
                    f"Trail component {component} {label} {index} has {distance:.3f} mm "
                    f"clearance; requires {required:.3f} mm near "
                    f"({location[0]:.3f}, {location[1]:.3f}) mm.")
    if not boundaries:
        raise ValueError("Could not evaluate any trail walls reliably.")
    return {
        "valid": not errors, "boundaries": boundaries,
        "minimum_clearance_mm": min((item["measured_clearance_mm"] for item in boundaries
                                     if item["measured_clearance_mm"] is not None), default=None),
        "numerical_tolerance_mm": TRAIL_WALL_NUMERICAL_TOLERANCE_MM,
        "floor_contact_tolerance_mm": TRAIL_FIT_Z_TOLERANCE_MM,
        "numerical_footprint_area_mm2": TRAIL_NUMERICAL_OPENING_MM2,
        "small_water_opening_max_area_mm2": TRAIL_SMALL_WATER_OPENING_MM2,
        "small_water_required_clearance_mm": TRAIL_SMALL_WATER_CLEARANCE_MM,
        "projection_precision_corrections": [
            {"role": role, **part.metadata["projection_precision_correction"]}
            for role, part in (("terrain", terrain), ("trail", trail), ("water", water))
            if part is not None and "projection_precision_correction" in part.metadata],
        "errors": errors,
    }


def validate_trail_fit(terrain_path: Path, trail_path: Path,
                       water_path: Path = None, clearance_mm: float = 0.0) -> dict:
    """Check that integrated terrain and water leave the trail pocket open."""
    terrain = trimesh.load_mesh(terrain_path, process=True)
    trail = trimesh.load_mesh(trail_path, process=True)
    if terrain.is_empty or trail.is_empty:
        raise ValueError("Trail snap-fit validation received an empty mesh.")
    if not np.isfinite(terrain.vertices).all() or not np.isfinite(trail.vertices).all():
        raise ValueError("Trail snap-fit validation received non-finite geometry.")
    if not math.isfinite(clearance_mm) or clearance_mm < 0:
        raise ValueError("Trail clearance must be finite and non-negative.")

    corridor = trail_corridor(trail_path, clearance_mm)
    water_overlap = 0.0
    water = None
    if water_path is not None:
        water = trimesh.load_mesh(water_path, process=True)
        if not water.is_empty:
            water_overlap = float(projected_footprint(water).intersection(corridor).area)

    triangles = trail.vertices[trail.faces]
    normals = np.cross(triangles[:, 1] - triangles[:, 0],
                       triangles[:, 2] - triangles[:, 0])
    floor_z = float(trail.bounds[0, 2])
    top_faces = triangles[(normals[:, 2] > 1e-8)
                          & (triangles[:, :, 2].mean(axis=1)
                             > floor_z + TRAIL_FIT_Z_TOLERANCE_MM)]
    if not len(top_faces):
        raise ValueError("Trail mesh has no usable upper surface for fit validation.")
    points = np.unique(np.round(top_faces[:, :, :2].mean(axis=1), 4), axis=0)
    if len(points) > 5000:
        indices = np.linspace(0, len(points) - 1, 5000, dtype=int)
        points = points[indices]
    surface = UpperSurface(terrain)
    sampled = [surface.z(point) for point in points]
    sampled = [value for value in sampled if value is not None]
    if not sampled:
        raise ValueError("Terrain has no surface beneath the trail.")
    maximum_surface_z = float(max(sampled))
    intrusion = max(0.0, maximum_surface_z - floor_z)
    errors = []
    if intrusion > TRAIL_FIT_Z_TOLERANCE_MM:
        errors.append(
            f"Terrain rises {intrusion:.3f} mm above the trail pocket floor.")
    if water_overlap > TRAIL_FIT_AREA_TOLERANCE_MM2:
        errors.append(
            f"Water overlaps {water_overlap:.3f} mm² of the protected trail corridor.")
    walls = _validate_wall_clearance(terrain, trail, water, clearance_mm)
    errors.extend(walls["errors"])
    return {
        "valid": not errors,
        "wall_clearance": walls,
        "clearance_mm": float(clearance_mm),
        "trail_floor_z_mm": floor_z,
        "maximum_terrain_z_in_trail_mm": maximum_surface_z,
        "maximum_terrain_intrusion_mm": intrusion,
        "water_overlap_area_mm2": water_overlap,
        "sample_count": len(sampled),
        "errors": errors,
    }
