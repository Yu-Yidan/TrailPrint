"""Validate and selectively normalize TrailPrint3D STL output."""

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List

import numpy as np
import trimesh


def mesh_role(path: Path, project_slug: str) -> str:
    stem = path.stem.lower()
    slug = project_slug.lower()
    if stem == slug:
        return "terrain"
    if "_trail" in stem or stem.endswith("trail"):
        return "trail"
    if "plate" in stem:
        return "plate"
    if "text" in stem or stem.startswith("t_"):
        return "text"
    if stem.endswith("_water") or stem == "water" or stem.endswith("ocean"):
        return "water"
    return "other"


def _component_sizes(mesh: trimesh.Trimesh) -> List[int]:
    face_count = len(mesh.faces)
    if not face_count:
        return []
    parent = list(range(face_count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for first, second in np.asarray(mesh.face_adjacency, dtype=np.int64):
        first_root, second_root = find(int(first)), find(int(second))
        if first_root != second_root:
            parent[second_root] = first_root
    sizes: Dict[int, int] = {}
    for index in range(face_count):
        root = find(index)
        sizes[root] = sizes.get(root, 0) + 1
    return sorted(sizes.values(), reverse=True)


def _load(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="stl", force="mesh", process=True)
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError("did not contain one mesh")
    return loaded


def inspect_stl(path: Path, project_slug: str, map_span: float = None) -> dict:
    role = mesh_role(path, project_slug)
    result = {"filename": path.name, "role": role, "valid": False, "errors": [], "warnings": []}
    try:
        mesh = _load(path)
    except Exception as exc:
        result["errors"].append(f"Could not parse STL: {exc}")
        return result
    vertices = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.faces)
    if mesh.is_empty or not len(vertices) or not len(faces):
        result["errors"].append("STL contains no mesh data.")
        return result
    if not np.isfinite(vertices).all():
        result["errors"].append("STL contains non-finite coordinates.")
        return result
    bounds = np.asarray(mesh.bounds, dtype=float)
    extents = np.asarray(mesh.extents, dtype=float)
    center = bounds.mean(axis=0)
    max_extent = float(extents.max())
    if max_extent <= 1e-6 or int(np.count_nonzero(extents > 1e-6)) < 2:
        result["errors"].append("STL geometry is collapsed or has no usable area.")
    try:
        nondegenerate = np.asarray(mesh.nondegenerate_faces())
        degenerate_count = int(len(faces) - np.count_nonzero(nondegenerate))
    except Exception:
        degenerate_count = 0
    components = _component_sizes(mesh)
    result.update({
        "bytes": path.stat().st_size,
        "triangles": int(len(faces)),
        "vertices": int(len(vertices)),
        "dimensions_mm": [round(float(value), 4) for value in extents],
        "bounds_mm": [[round(float(value), 4) for value in row] for row in bounds],
        "center_mm": [round(float(value), 4) for value in center],
        "components": len(components),
        "largest_component_triangles": components[0] if components else 0,
        "degenerate_triangles": degenerate_count,
        "watertight": bool(mesh.is_watertight),
    })
    if degenerate_count:
        result["warnings"].append(f"Contains {degenerate_count} degenerate triangle(s).")
    if not mesh.is_watertight:
        result["warnings"].append("Mesh is not fully watertight.")
    if role == "trail":
        if len(faces) < 20:
            result["errors"].append("Trail contains too few triangles to represent a complete route.")
        largest_share = (components[0] / len(faces)) if components else 0
        significant_components = sum(size >= max(10, len(faces) * 0.001) for size in components)
        result["largest_component_share"] = round(largest_share, 6)
        result["significant_components"] = significant_components
        if significant_components != 1 or largest_share < 0.99:
            result["errors"].append(
                f"Trail is fragmented into {len(components)} disconnected components.")
        trail_span = float(max(extents[0], extents[1]))
        if map_span and trail_span < map_span * 0.1:
            result["errors"].append("Trail span is unexpectedly small relative to the terrain.")
    result["valid"] = not result["errors"]
    return result


def _is_anomalous(result: dict) -> bool:
    if result.get("errors") and "center_mm" not in result:
        return False
    center = result.get("center_mm", [0, 0, 0])
    dimensions = result.get("dimensions_mm", [0, 0, 0])
    max_extent = max(dimensions) if dimensions else 0
    distance_xy = math.hypot(center[0], center[1])
    return distance_xy > max(1000.0, max_extent * 10.0)


def normalize_anomalous_assembly(paths: List[Path], inspections: List[dict],
                                 project_slug: str) -> List[dict]:
    """Center an offset multi-part model without destroying part alignment."""
    pairs = [(path, result) for path, result in zip(paths, inspections)
             if "bounds_mm" in result]
    reference = next((pair for pair in pairs if pair[1]["role"] == "plate"), None)
    reference = reference or next((pair for pair in pairs if pair[1]["role"] == "terrain"), None)
    reference = reference or (pairs[0] if pairs else None)
    if reference is None or not _is_anomalous(reference[1]):
        return []
    reference_bounds = np.asarray(reference[1]["bounds_mm"], dtype=float)
    center_xy = reference_bounds[:, :2].mean(axis=0)
    minimum_z = min(float(result["bounds_mm"][0][2]) for _, result in pairs)
    translation = np.array([-center_xy[0], -center_xy[1], -minimum_z])
    moved = []
    for path, _result in pairs:
        mesh = _load(path)
        mesh.merge_vertices()
        mesh.remove_unreferenced_vertices()
        mesh.apply_translation(translation)
        mesh.export(path, file_type="stl")
        moved.append(path.name)
    return [{
        "type": "assembly",
        "reference": reference[0].name,
        "files": moved,
        "translation_mm": [round(float(value), 4) for value in translation],
    }]


def align_core_to_terrain(source_paths: Iterable[Path], reference_paths: Iterable[Path],
                          project_slug: str) -> dict:
    """Align fallback terrain/trail to the primary terrain without separating them."""
    source_paths = list(source_paths)
    reference_paths = list(reference_paths)
    source_terrain = next((path for path in source_paths
                           if mesh_role(path, project_slug) == "terrain"), None)
    reference_terrain = next((path for path in reference_paths
                              if mesh_role(path, project_slug) == "terrain"), None)
    if source_terrain is None or reference_terrain is None:
        raise ValueError("Could not align fallback output without both terrain meshes.")
    source_bounds = np.asarray(_load(source_terrain).bounds, dtype=float)
    reference_bounds = np.asarray(_load(reference_terrain).bounds, dtype=float)
    source_anchor = np.array([*source_bounds[:, :2].mean(axis=0), source_bounds[0, 2]])
    reference_anchor = np.array([*reference_bounds[:, :2].mean(axis=0), reference_bounds[0, 2]])
    translation = reference_anchor - source_anchor
    moved = []
    for path in source_paths:
        if mesh_role(path, project_slug) not in {"terrain", "trail"}:
            continue
        mesh = _load(path)
        mesh.apply_translation(translation)
        mesh.export(path, file_type="stl")
        moved.append(path.name)
    return {"files": moved,
            "translation_mm": [round(float(value), 4) for value in translation]}


def validate_stls(paths: Iterable[Path], project_slug: str, repair=True) -> dict:
    paths = sorted(paths, key=lambda item: item.name.lower())
    first_pass = [inspect_stl(path, project_slug) for path in paths]
    corrections = []
    if repair:
        corrections = normalize_anomalous_assembly(paths, first_pass, project_slug)
    terrain = next((path for path in paths if mesh_role(path, project_slug) == "terrain"), None)
    terrain_result = inspect_stl(terrain, project_slug) if terrain else None
    map_span = None
    if terrain_result and "dimensions_mm" in terrain_result:
        map_span = max(terrain_result["dimensions_mm"][:2])
    files = [inspect_stl(path, project_slug, map_span) for path in paths]
    roles = {item["role"] for item in files if item["valid"]}
    errors = []
    if not corrections and any(_is_anomalous(item) for item in files):
        errors.append("STL parts do not share a consistent printable coordinate system.")
    if "terrain" not in roles:
        errors.append("No valid terrain STL was generated.")
    if "trail" not in roles:
        errors.append("No valid trail STL was generated.")
    for item in files:
        errors.extend(f"{item['filename']}: {error}" for error in item["errors"])
    return {"valid": not errors, "files": files, "corrections": corrections, "errors": errors}


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".validation-", suffix=".json", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def read_report(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
