"""Convert generated water solids into shallow, printable terrain inlays."""

import math
from pathlib import Path

import numpy as np
import trimesh
from shapely import contains_xy, set_precision
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

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
    footprint = projected_footprint(trail)
    if footprint.is_empty:
        raise ValueError("Trail mesh has no usable projected footprint.")
    return footprint.buffer(float(clearance_mm), join_style=1).buffer(0)


def _stitch_collinear_boundaries(mesh):
    """Split existing edges at numerical T junctions; never cap actual holes."""
    splits = 0
    for _ in range(10):
        counts = np.bincount(mesh.edges_unique_inverse)
        graph = {}
        for a, b in mesh.edges_unique[counts == 1]:
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
        faces = mesh.faces.copy()
        removed, added = set(), []
        for group in groups:
            if len(group) != 3:
                continue
            vertices = list(group)
            pairs = [(vertices[i], vertices[j])
                     for i in range(3) for j in range(i + 1, 3)]
            a, c = max(pairs, key=lambda pair: np.linalg.norm(
                mesh.vertices[pair[0]] - mesh.vertices[pair[1]]))
            b = next(vertex for vertex in group if vertex not in (a, c))
            delta = mesh.vertices[c] - mesh.vertices[a]
            fraction = np.dot(mesh.vertices[b] - mesh.vertices[a], delta) / np.dot(delta, delta)
            distance = np.linalg.norm(mesh.vertices[b] - mesh.vertices[a] - fraction * delta)
            if not 0 < fraction < 1 or distance > 1e-6:
                continue
            adjacent = np.flatnonzero(np.any(faces == a, axis=1)
                                      & np.any(faces == c, axis=1))
            for index in adjacent:
                if index in removed:
                    continue
                face = faces[index]
                for position in range(3):
                    x, y, z = (face[position], face[(position + 1) % 3],
                               face[(position + 2) % 3])
                    if {x, y} == {a, c}:
                        added.extend(((x, b, z), (b, y, z)))
                        removed.add(int(index))
                        break
        if not removed:
            break
        splits += len(removed)
        mesh = trimesh.Trimesh(
            mesh.vertices, np.concatenate((np.delete(faces, list(removed), axis=0), added)),
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


def repair_terrain_for_water_boolean(terrain_path: Path, output_path: Path) -> dict:
    """Remove coincident zero-thickness facets before Blender's manifold Boolean."""
    mesh = trimesh.load_mesh(terrain_path, process=True)
    if mesh.is_empty:
        raise ValueError("Terrain mesh is empty.")
    input_watertight = bool(mesh.is_watertight)
    input_bounds = np.asarray(mesh.bounds, dtype=float)
    input_volume = float(mesh.volume)
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
    if not mesh.is_watertight or not mesh.is_winding_consistent:
        raise ValueError(
            "Terrain could not be made manifold for integrated water basins.")
    if abs(float(mesh.volume) - input_volume) > .01:
        raise ValueError("Terrain repair unexpectedly changed its volume.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(output_path)
    exported = trimesh.load_mesh(output_path, process=True)
    if not exported.is_watertight or not exported.is_winding_consistent:
        raise ValueError("Repaired Terrain did not remain manifold after STL export.")
    output_bounds = np.asarray(exported.bounds, dtype=float)
    if not np.allclose(input_bounds, output_bounds,
                       atol=WATER_BOOLEAN_BOUNDS_TOLERANCE_MM, rtol=0):
        raise ValueError("Terrain repair unexpectedly changed its bounds.")
    return {
        "input_watertight": input_watertight,
        "output_watertight": True,
        "duplicate_groups": int(len(duplicate_groups)),
        "removed_faces": removed_faces,
        "degenerate_faces_removed": degenerate_faces,
        "horizontal_faces_retriangulated": planar_faces,
        "collinear_edge_splits": edge_splits,
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


def _stable_polygon(polygon):
    """Remove coordinates which collapse when STL stores them as float32."""
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
    if len(items) != 1:
        raise ValueError("Water precision cleanup unexpectedly split a connected body.")
    return items[0]


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


def prepare_integrated_water(terrain_path: Path, water_paths, cap_path: Path,
                             cutter_path: Path, support_path: Path = None,
                             trail_path: Path = None,
                             trail_clearance_mm: float = 0.0) -> dict:
    """Create shoreline-driven water inlays and matching terrain geometry."""
    terrain = trimesh.load_mesh(terrain_path, process=True)
    if terrain.is_empty:
        raise ValueError("Terrain mesh is empty.")
    surface = UpperSurface(terrain)
    outline = MultiPoint(terrain.vertices[:, :2]).convex_hull.buffer(0)
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
    entries = []
    if inland_footprints:
        inland = _printable_water(unary_union(inland_footprints).buffer(0), outline)
        entries.extend((polygon, None, WATER_DEPTH_MM, None)
                       for polygon in polygon_items(inland))
    for ocean, level in ocean_footprints:
        printable = _printable_water(ocean, outline)
        entries.extend((polygon, level, OCEAN_DEPTH_MM, None)
                       for polygon in polygon_items(printable))
    trail_exclusion = Polygon()
    excluded_water_area = 0.0
    shoreline_fallbacks = []
    water_exclusion = unary_union([entry[0] for entry in entries])
    if trail_path is not None:
        trail_exclusion = trail_corridor(
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
                if item.area < WATER_MIN_COMPONENT_AREA_MM2:
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
                "shoreline_fallbacks": shoreline_fallbacks}
    caps, cutters, supports, bodies = [], [], [], []
    cutter_top = float(terrain.bounds[1, 2] + 1.0)
    support_base = float(terrain.bounds[0, 2])
    for source_polygon, ocean_level, depth, natural_samples in entries:
        polygon = _stable_polygon(source_polygon)
        top_vertices, faces = _surface_triangulation(polygon, surface)
        shoreline = None
        if ocean_level is None:
            samples = (natural_samples if natural_samples is not None else
                       _sample_shoreline(polygon, surface, outline))
            levels, mode, shoreline = _shoreline_surface(top_vertices, samples)
            top_vertices[:, 2] = levels
        else:
            mode = "ocean-flat"
            top_vertices[:, 2] = ocean_level
        bottom = top_vertices[:, 2] - depth
        cap = _terrain_shell(top_vertices, faces, depth=depth)
        cutter = _terrain_shell(top_vertices, faces, bottom, top_z=cutter_top,
                                depth=depth)
        support_top = top_vertices.copy()
        support_top[:, 2] = bottom
        support = _terrain_shell(support_top, faces, support_base, depth=depth)
        caps.append(cap)
        cutters.append(cutter)
        supports.append(support)
        occupied_layers = set()
        for value in top_vertices[:, 2]:
            top_layer = int(round(value / WATER_LAYER_HEIGHT_MM))
            bottom_layer = int(round((value - depth) / WATER_LAYER_HEIGHT_MM))
            occupied_layers.update(range(bottom_layer + 1, top_layer + 1))
        body = {
            "area_mm2": float(polygon.area),
            "surface_min_z_mm": float(top_vertices[:, 2].min()),
            "surface_max_z_mm": float(top_vertices[:, 2].max()),
            "depth_mm": float(depth),
            "surface_mode": mode,
            "layer_bands": sorted(occupied_layers),
        }
        if shoreline:
            body.update(shoreline)
        bodies.append(body)
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
    exported_cap = trimesh.load_mesh(cap_path, process=True)
    exported_cutter = trimesh.load_mesh(cutter_path, process=True)
    if not exported_cap.is_watertight or not exported_cutter.is_watertight:
        failures = []
        if not exported_cap.is_watertight:
            failures.append("water")
        if not exported_cutter.is_watertight:
            failures.append("cutter")
        raise ValueError(
            "Integrated water geometry did not remain watertight after STL export: "
            + ", ".join(failures) + ".")
    return {"available": True, "body_count": len(bodies), "bodies": bodies,
            "minimum_width_mm": WATER_MIN_WIDTH_MM,
            "depth_mm": WATER_DEPTH_MM,
            "surface_sample_mm": WATER_SURFACE_SAMPLE_MM,
            "surface_mode": "shoreline-driven",
            "trail_clearance_mm": float(trail_clearance_mm),
            "trail_precision_margin_mm": TRAIL_WATER_PRECISION_MARGIN_MM,
            "trail_excluded_water_area_mm2": excluded_water_area,
            "shoreline_fallbacks": shoreline_fallbacks,
            "cap_watertight": True,
            "cutter_watertight": True,
            "support_watertight": True}


def validate_trail_fit(terrain_path: Path, trail_path: Path,
                       water_path: Path = None, clearance_mm: float = 0.0) -> dict:
    """Check that integrated terrain and water leave the trail pocket open."""
    terrain = trimesh.load_mesh(terrain_path, process=True)
    trail = trimesh.load_mesh(trail_path, process=True)
    if terrain.is_empty or trail.is_empty:
        raise ValueError("Trail snap-fit validation received an empty mesh.")

    corridor = trail_corridor(trail_path, clearance_mm)
    water_overlap = 0.0
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
    return {
        "valid": not errors,
        "clearance_mm": float(clearance_mm),
        "trail_floor_z_mm": floor_z,
        "maximum_terrain_z_in_trail_mm": maximum_surface_z,
        "maximum_terrain_intrusion_mm": intrusion,
        "water_overlap_area_mm2": water_overlap,
        "sample_count": len(sampled),
        "errors": errors,
    }
