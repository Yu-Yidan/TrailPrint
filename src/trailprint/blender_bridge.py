"""Run the installed TrailPrint3D Blender extension as TrailPrint's engine."""

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import trimesh
from shapely.errors import ShapelyError
from shapely.wkb import loads as load_wkb

from .fonts import require_model_font
from .gpx import (
    AUTO_RELIEF_LIMIT_MM,
    read_gpx_segments,
)
from .mesh_validation import (
    align_core_to_terrain,
    cleanup_fragmented_trails,
    inspect_stl,
    mesh_role,
    normalize_anomalous_assembly,
    validate_stls,
    write_report,
)
from .naming import canonical_3mf_name, customer_preview_name, deliverable_prefix, human_name
from .osm_colors import analyze_project
from .print_font import prepare_print_font
from .publishing import publish_files
from .title import validate_title_fit
from .water_geometry import (
    WATER_BOOLEAN_BOUNDS_TOLERANCE_MM,
    TerrainRepairError,
    UpperSurface,
    export_groove_cutter,
    generation_trail_corridor,
    groove_material_overlap,
    prepare_integrated_water,
    repair_terrain_for_water_boolean,
    validate_trail_fit,
)


class BuildCancelled(Exception):
    """Raised after a requested Blender build cancellation is cleaned up."""


SHAPE_EXTRAS: Dict[str, Tuple[str, ...]] = {
    "HEXAGON": ("NONE", "INNER TEXT", "OUTER TEXT", "FRONT TEXT"),
    "SQUARE": ("NONE",),
    "CIRCLE": ("NONE", "OUTER TEXT"),
    "OCTAGON": ("NONE", "OUTER TEXT"),
    "ELLIPSE": ("NONE",),
    "HEART": ("NONE",),
}
SHAPE_LABELS = {
    "HEXAGON": "Hexagon", "SQUARE": "Rectangle", "CIRCLE": "Circle",
    "OCTAGON": "Octagon", "ELLIPSE": "Ellipse", "HEART": "Heart",
}
EXTRA_LABELS = {
    "NONE": "None", "INNER TEXT": "Inner text", "OUTER TEXT": "Outer text",
    "FRONT TEXT": "Front text",
}
DEFAULT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")
PROGRESS_FILE = Path(tempfile.gettempdir()) / "trailprint_progress.json"
WATER_MIN_AREA_MM2 = 25.0
TRAIL_CLEARANCE_MM = 0.1


@dataclass(frozen=True)
class BuildSettings:
    shape: str = "HEXAGON"
    shape_extra: str = "NONE"
    resolution: int = 8
    plate_title: str = ""
    plate_text_1: str = ""
    plate_text_2: str = ""
    plate_text_3: str = ""
    plate_text_4: str = ""
    plate_text_5: str = ""
    x_offset: float = 0.0
    y_offset: float = 0.0
    max_terrain_relief_mm: float = AUTO_RELIEF_LIMIT_MM
    city: str = ""
    state: str = ""
    country: str = ""
    unit_system: str = "IMPERIAL"
    plate_recess_depth: float = 1.5
    dot_magnet_recess: bool = True
    terrain_elevation_multiplier: float = 1.0

    @classmethod
    def validated(cls, shape="HEXAGON", shape_extra="NONE", resolution=8,
                  plate_title="", plate_text_1="",
                  plate_text_2="", plate_text_3="",
                  plate_text_4="", plate_text_5="", x_offset=0, y_offset=0,
                  max_terrain_relief_mm=AUTO_RELIEF_LIMIT_MM,
                  city="", state="", country="",
                  unit_system="IMPERIAL", plate_recess_depth=1.5,
                  dot_magnet_recess=True,
                  terrain_elevation_multiplier=1.0):
        shape = str(shape).upper()
        shape_extra = str(shape_extra).upper()
        try:
            resolution = int(resolution)
        except (TypeError, ValueError) as exc:
            raise ValueError("Resolution must be a whole number from 1 to 10.") from exc
        if shape not in SHAPE_EXTRAS:
            raise ValueError("Unsupported map shape.")
        if shape_extra not in SHAPE_EXTRAS[shape]:
            raise ValueError(f"{EXTRA_LABELS.get(shape_extra, shape_extra)} is unavailable for {SHAPE_LABELS[shape]}.")
        if not 1 <= resolution <= 10:
            raise ValueError("Resolution must be from 1 to 10.")
        text_values = [plate_title, plate_text_1, plate_text_2, plate_text_3,
                       plate_text_4, plate_text_5]
        text_values = [str(value) for value in text_values]
        if any(len(value) > 200 for value in text_values):
            raise ValueError("Plate text fields must be 200 characters or fewer.")
        try:
            x_offset = float(x_offset)
            y_offset = float(y_offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("X and Y offsets must be numbers.") from exc
        if not math.isfinite(x_offset) or not math.isfinite(y_offset):
            raise ValueError("X and Y offsets must be finite numbers.")
        try:
            max_terrain_relief_mm = float(max_terrain_relief_mm)
        except (TypeError, ValueError) as exc:
            raise ValueError("Maximum terrain relief must be a positive number.") from exc
        if not math.isfinite(max_terrain_relief_mm) or max_terrain_relief_mm <= 0:
            raise ValueError("Maximum terrain relief must be a positive number.")
        try:
            terrain_elevation_multiplier = float(terrain_elevation_multiplier)
        except (TypeError, ValueError) as exc:
            raise ValueError("Terrain elevation multiplier must be a positive finite number.") from exc
        if not math.isfinite(terrain_elevation_multiplier) or terrain_elevation_multiplier <= 0:
            raise ValueError("Terrain elevation multiplier must be a positive finite number.")
        locations = [str(value).strip() for value in (city, state, country)]
        if any(len(value) > 200 for value in locations):
            raise ValueError("Location fields must be 200 characters or fewer.")
        unit_system = str(unit_system).upper()
        if unit_system not in {"IMPERIAL", "METRIC"}:
            raise ValueError("Units must be imperial or metric.")
        try:
            plate_recess_depth = float(plate_recess_depth)
        except (TypeError, ValueError) as exc:
            raise ValueError("Plate recess depth must be a number from 0 to 4 mm.") from exc
        if not math.isfinite(plate_recess_depth) or not 0 <= plate_recess_depth <= 4:
            raise ValueError("Plate recess depth must be from 0 to 4 mm.")
        if not has_separate_plate(shape, shape_extra):
            plate_recess_depth = 0.0
            dot_magnet_recess = False
        return cls(
            shape=shape, shape_extra=shape_extra, resolution=resolution,
            plate_title=text_values[0], plate_text_1=text_values[1],
            plate_text_2=text_values[2], plate_text_3=text_values[3],
            plate_text_4=text_values[4], plate_text_5=text_values[5],
            x_offset=x_offset, y_offset=y_offset,
            max_terrain_relief_mm=max_terrain_relief_mm,
            terrain_elevation_multiplier=terrain_elevation_multiplier,
            city=locations[0], state=locations[1], country=locations[2],
            unit_system=unit_system, plate_recess_depth=plate_recess_depth,
            dot_magnet_recess=bool(dot_magnet_recess))


def has_separate_plate(shape: str, shape_extra: str) -> bool:
    return (str(shape).upper(), str(shape_extra).upper()) in {
        ("HEXAGON", "OUTER TEXT"), ("HEXAGON", "FRONT TEXT"),
        ("OCTAGON", "OUTER TEXT"), ("CIRCLE", "OUTER TEXT"),
    }


def find_blender(explicit: Optional[Path] = None) -> Path:
    candidates = [explicit, Path(os.environ["TRAILPRINT_BLENDER"]) if os.environ.get("TRAILPRINT_BLENDER") else None,
                  DEFAULT_BLENDER]
    command = shutil.which("blender")
    if command:
        candidates.append(Path(command))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise ValueError("Blender was not found. Install Blender 5.2 or set TRAILPRINT_BLENDER.")


def _prefix_staged_outputs(project: Path, stage: Path) -> None:
    trails = [path for path in stage.glob("*.stl")
              if mesh_role(path, project.name) == "trail"]
    if len(trails) > 1:
        raise ValueError("Generator exported multiple Trail STLs instead of one combined trail.")
    if trails:
        # Joined GPX splines can leave Blender's numeric suffix on the object.
        # Keep one stable Trail filename for 3MF role detection.
        canonical = stage / f"{project.name}_Trail.stl"
        if trails[0] != canonical:
            trails[0].rename(canonical)
    prefix = deliverable_prefix(project)
    for path in list(stage.glob("*.stl")):
        if prefix and not path.name.startswith(prefix):
            path.rename(path.with_name(prefix + path.name))


def _integrate_generated_water(stage: Path, project_slug: str, blender_path: Path,
                               timeout: int, cancel_event=None) -> tuple:
    paths = list(stage.glob("*.stl"))
    terrain = next((path for path in paths
                    if mesh_role(path, project_slug) == "terrain"), None)
    trail = next((path for path in paths
                  if mesh_role(path, project_slug) == "trail"), None)
    water = [path for path in paths if mesh_role(path, project_slug) == "water"]
    if terrain is None or trail is None:
        return None, None
    corridor, groove_report = generation_trail_corridor(trail, water, TRAIL_CLEARANCE_MM)
    (stage / "protected-trail-corridor.wkb").write_bytes(corridor.wkb)
    (stage / "groove-correction.json").write_text(json.dumps(groove_report), encoding="utf-8")
    if not water:
        return None, None
    work = stage / "water-integration"
    work.mkdir()
    cap = work / f"{project_slug}_WATER.stl"
    cutter = work / "water-cutter.stl"
    support = work / "water-terrain-support.stl"
    coastal_source = stage / "coastal-water-source.json"
    original_terrain = stage / "shoreline-source/terrain.stl"
    restore_land = False
    if coastal_source.is_file():
        metadata = json.loads(coastal_source.read_text(encoding="utf-8"))
        restore_land = isinstance(metadata, dict) and metadata.get("terrain_source") is True
        if restore_land and not original_terrain.is_file():
            raise ValueError("Missing original terrain for shoreline restoration; regenerate.")
    if (any(path.stem.lower().endswith("ocean") for path in water)
            and not coastal_source.is_file()):
        raise ValueError("Missing original coastal water metadata; regenerate the model.")
    report = prepare_integrated_water(
        terrain, water, cap, cutter, support,
        trail_path=trail, trail_clearance_mm=TRAIL_CLEARANCE_MM,
        protected_corridor=corridor,
        coastal_source_path=coastal_source if coastal_source.is_file() else None,
        original_terrain_path=original_terrain if restore_land else None)
    if not report.get("available"):
        for path in water:
            path.unlink(missing_ok=True)
        return report, None
    repaired_terrain = work / "terrain-manifold.stl"
    rebuilding = report.get("land_restoration", {}).get("available", False)
    integration_source = original_terrain if rebuilding else terrain
    repair = repair_terrain_for_water_boolean(integration_source, repaired_terrain)
    report["terrain_repair"] = repair
    if rebuilding:
        report["land_restoration"]["source_repair"] = repair
    output = work / "terrain-with-water-basins.stl"
    job = work / "job.json"
    job.write_text(json.dumps({
        "terrain_path": str(repaired_terrain), "cutter_path": str(cutter),
        "support_path": str(support),
        "output_path": str(output),
    }), encoding="utf-8")
    worker = Path(__file__).with_name("water_worker.py")
    result = _run_command(
        [str(blender_path), "--background", "--python", str(worker), "--", str(job)],
        timeout, cancel_event)
    combined_log = (result.stdout or "") + "\n" + (result.stderr or "")
    if (result.returncode != 0 or not output.is_file()
            or "Cannot execute" in combined_log):
        detail = _failure_detail(result)
        raise ValueError(f"Could not integrate shallow water into Terrain: {detail}")
    manifold_output = work / "terrain-with-water-basins-manifold.stl"
    # Reuse the verified base only when cuts stop above it and support
    # stays within its footprint, preserving any intended base openings.
    base_z = float(trimesh.load_mesh(repaired_terrain, process=True).bounds[0, 2])
    reference_mesh = trimesh.load_mesh(repaired_terrain, process=True)
    base_faces = np.all(np.abs(reference_mesh.triangles[:, :, 2] - base_z) < 1e-7, axis=1)
    from shapely import covers, points, polygons, union_all
    base_region = union_all(polygons(reference_mesh.triangles[base_faces, :, :2]))
    support_mesh = trimesh.load_mesh(support, process=True)
    tools_above_base = (
        float(trimesh.load_mesh(cutter, process=True).bounds[0, 2]) > base_z + 0.001
        and float(support_mesh.bounds[0, 2]) >= base_z
        and bool(np.all(covers(base_region.buffer(1e-7), points(support_mesh.vertices[:, :2])))))
    report["terrain_output_repair"] = repair_terrain_for_water_boolean(
        output, manifold_output,
        base_reference_path=repaired_terrain if tools_above_base else None)
    output = manifold_output
    integrated = trimesh.load_mesh(output, process=True)
    if rebuilding:
        restored_surface = UpperSurface(integrated)
        errors = []
        for sample in report["land_restoration"]["samples"]:
            actual = restored_surface.z(sample["xy_mm"])
            if actual is None or abs(actual - sample["original_z_mm"]) > .05:
                raise ValueError(f"Original terrain was not restored at {sample['xy_mm']}.")
            errors.append(abs(actual - sample["original_z_mm"]))
        report["land_restoration"]["verified_samples"] = len(errors)
        report["land_restoration"]["maximum_height_error_mm"] = max(errors, default=0.)
    original_bounds = np.asarray(repair["bounds_mm"], dtype=float)
    integrated_bounds = np.asarray(integrated.bounds, dtype=float)
    tolerance = WATER_BOOLEAN_BOUNDS_TOLERANCE_MM
    preserved = np.allclose(
        integrated_bounds[[0, 1], :2], original_bounds[[0, 1], :2],
        atol=tolerance, rtol=0)
    base_preserved = abs(integrated_bounds[0, 2] - original_bounds[0, 2]) <= tolerance
    roof_absent = integrated_bounds[1, 2] <= original_bounds[1, 2] + tolerance
    removed_volume = float(repair["volume_mm3"] - abs(integrated.volume))
    volume_changed = abs(removed_volume) > 0.01
    if not preserved or not base_preserved or not roof_absent or not volume_changed:
        raise ValueError(
            "Water basin Boolean validation failed "
            f"(xy_preserved={preserved}, base_preserved={base_preserved}, "
            f"cutter_roof_absent={roof_absent}, volume_delta_mm3={removed_volume:.4f}).")
    report["terrain_boolean"] = {
        "solver": "MANIFOLD", "bounds_preserved": True,
        "cutter_roof_absent": True, "net_removed_volume_mm3": removed_volume,
        "output_bounds_mm": integrated_bounds.tolist(),
    }
    os.replace(output, terrain)
    for path in water:
        path.unlink(missing_ok=True)
    destination = stage / f"{project_slug}_WATER.stl"
    os.replace(cap, destination)
    return report, _attempt_log("integrated shallow water", result)


def _correct_staged_groove(stage, slug, blender_path, timeout, cancel_event=None,
                           expected_trail_segments=1):
    """Widen terrain walls from the actual trail; never rewrite the Trail STL."""
    roles = {mesh_role(path, slug): path for path in stage.glob("*.stl")}
    if not {"terrain", "trail"}.issubset(roles):
        return
    if not inspect_stl(roles["terrain"], slug)["valid"]:
        return
    if not inspect_stl(roles["trail"], slug,
                       expected_trail_segments=expected_trail_segments)["valid"]:
        return
    try:
        before = validate_trail_fit(roles["terrain"], roles["trail"], roles.get("water"),
                                    clearance_mm=TRAIL_CLEARANCE_MM)
    except (ValueError, ShapelyError):
        return  # The final blocking validator reports unreadable geometry.
    report_path = stage / "groove-correction.json"
    if (stage / "protected-trail-corridor.wkb").exists():
        corridor = load_wkb((stage / "protected-trail-corridor.wkb").read_bytes())
        report = json.loads(report_path.read_text())
    else:
        corridor, report = generation_trail_corridor(
            roles["trail"], [roles["water"]] if "water" in roles else [], TRAIL_CLEARANCE_MM)
    report.update({"before": before, "attempts": [], "trail_unchanged": True})
    report["trail_sha256"] = hashlib.sha256(roles["trail"].read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    if (before["valid"] and groove_material_overlap(
            roles["terrain"], corridor, report["floor_z_mm"]) <= 0.01):
        report["after"] = before
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return
    work = stage / "groove-correction"
    work.mkdir(exist_ok=True)
    started = time.monotonic()
    for attempt in range(2):
        terrain = trimesh.load_mesh(roles["terrain"], process=True)
        cutter = work / "cutter.stl"
        # A second pass adds only a bounded construction margin, never relaxes validation.
        protected = corridor.buffer(attempt * 0.02, quad_segs=16)
        cutter_report = export_groove_cutter(protected, report["floor_z_mm"],
                                             float(terrain.bounds[1, 2]) + 1, cutter)
        report["cutter"] = cutter_report
        report_path.write_text(json.dumps(report), encoding="utf-8")
        source = work / "terrain-manifold.stl"
        repair = repair_terrain_for_water_boolean(roles["terrain"], source)
        output = work / "terrain-corrected.stl"
        job = work / "job.json"
        job.write_text(json.dumps({"terrain_path": str(source), "cutter_path": str(cutter),
                                   "output_path": str(output)}), encoding="utf-8")
        result = _run_command(
            [str(blender_path), "--background", "--python",
             str(Path(__file__).with_name("water_worker.py")), "--", str(job)],
            max(1, timeout - int(time.monotonic() - started)), cancel_event)
        if result.returncode != 0 or not output.is_file():
            raise ValueError(f"Groove correction failed: {_failure_detail(result)}")
        repaired_output = work / "terrain-corrected-manifold.stl"
        try:
            output_repair = repair_terrain_for_water_boolean(output, repaired_output)
        except TerrainRepairError as exc:
            report["attempts"].append({"pass": attempt + 1, "valid": False,
                                       "export_repair": exc.repair_report})
            report_path.write_text(json.dumps(report), encoding="utf-8")
            if attempt == 0:
                continue  # Retry from the untouched terrain with the existing .02 mm margin.
            raise
        output = repaired_output
        corrected = trimesh.load_mesh(output, process=True)
        if (corrected.is_empty or not corrected.is_watertight
                or not corrected.is_winding_consistent
                or abs(corrected.bounds[0, 2] - terrain.bounds[0, 2]) > 0.01
                or not np.allclose(corrected.bounds[:, :2], terrain.bounds[:, :2],
                                   atol=0.01, rtol=0)
                or abs(corrected.volume) > abs(terrain.volume) + 0.01):
            raise ValueError("Groove correction produced invalid terrain geometry.")
        os.replace(output, roles["terrain"])
        after = validate_trail_fit(roles["terrain"], roles["trail"], roles.get("water"),
                                   clearance_mm=TRAIL_CLEARANCE_MM)
        report["attempts"].append({"pass": attempt + 1, "terrain_repair": repair,
                                   "export_repair": output_repair,
                                   "valid": after["valid"]})
        report["after"] = after
        report["trail_unchanged"] = (
            hashlib.sha256(roles["trail"].read_bytes()).hexdigest() == report["trail_sha256"])
        if not report["trail_unchanged"]:
            raise ValueError("Groove correction unexpectedly changed the trail.")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        if after["valid"]:
            break


def _validate_staged_trail_fit(stage: Path, slug: str, validation: dict) -> None:
    """Apply the same blocking fit gate to every publishable generation attempt."""
    if not validation["valid"]:
        return
    paths = list(stage.glob("*.stl"))
    roles = {mesh_role(path, slug): path for path in paths}
    try:
        fit = validate_trail_fit(roles["terrain"], roles["trail"], roles.get("water"),
                                 clearance_mm=TRAIL_CLEARANCE_MM)
    except (KeyError, ValueError, ShapelyError) as exc:
        fit = {"valid": False, "errors": [f"Could not evaluate trail fit reliably: {exc}"]}
    validation["trail_fit"] = fit
    if not fit["valid"]:
        validation["valid"] = False
        validation["errors"].extend(f"Trail fit validation failed: {error}"
                                    for error in fit["errors"])


def _read_elevation_metadata(stage: Path, validation: dict) -> dict:
    try:
        value = json.loads((stage / "terrain-elevation.json").read_text(encoding="utf-8"))
        terrain = next(item for item in validation.get("files", [])
                       if item.get("role") == "terrain" and item.get("valid"))
        bounds = terrain["bounds_mm"]
        minimum = float(value["source_min_m"])
        maximum = float(value["source_max_m"])
        thickness = float(value["min_thickness_mm"])
        surface_min = float(bounds[0][2]) + thickness
        surface_max = float(bounds[1][2])
    except (OSError, ValueError, KeyError, StopIteration, TypeError, json.JSONDecodeError):
        return {}
    if not all(math.isfinite(item) for item in
               (minimum, maximum, thickness, surface_min, surface_max)):
        return {}
    if maximum <= minimum or surface_max <= surface_min or thickness < 0:
        return {}
    result = {
        "source_min_m": minimum, "source_max_m": maximum,
        "model_surface_min_z_mm": surface_min,
        "model_surface_max_z_mm": surface_max,
        "min_thickness_mm": thickness,
        "unit_system": str(value.get("unit_system", "IMPERIAL")).upper(),
    }
    for name in ("uncapped_model_relief_mm", "final_model_relief_mm",
                 "max_terrain_relief_mm", "elevation_multiplier", "requested_elevation_multiplier"):
        try:
            number = float(value[name])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(number):
            result[name] = number
    result["cap_applied"] = bool(value.get("cap_applied", False))
    return result


def _validate_terrain_relief(stage: Path, validation: dict,
                             maximum_relief_mm: float) -> dict:
    elevation = _read_elevation_metadata(stage, validation)
    if not elevation:
        return {}
    actual = (elevation["model_surface_max_z_mm"]
              - elevation["model_surface_min_z_mm"])
    maximum = float(maximum_relief_mm)
    if actual > maximum + 0.05:
        raise ValueError(
            f"Generated terrain relief is {actual:.2f} mm; the maximum is "
            f"{maximum:.2f} mm.")
    elevation["actual_model_relief_mm"] = actual
    elevation["max_terrain_relief_mm"] = maximum
    return elevation


def _run_command(command, timeout: int, cancel_event=None) -> subprocess.CompletedProcess:
    if cancel_event is None:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if cancel_event.is_set():
        raise BuildCancelled("Blender build cancelled.")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    while True:
        if cancel_event.is_set():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise BuildCancelled("Blender build cancelled.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        try:
            stdout, stderr = process.communicate(timeout=min(0.25, remaining))
        except subprocess.TimeoutExpired:
            continue
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_attempt(project: Path, settings: BuildSettings, stage: Path, blender_path: Path,
                 timeout: int, cancel_event=None) -> subprocess.CompletedProcess:
    stage.mkdir()
    route = project / "route.gpx"
    config_path = stage / "job.json"
    config_path.write_text(json.dumps({
        "gpx_file": str(route), "export_path": str(stage), "project_name": project.name,
        "elevation_cache_dir": str(project / "build" / "verified-elevation-v1"),
        "shape": settings.shape, "shape_extra": settings.shape_extra,
        "model_font_path": str(prepare_print_font(stage / "Arial-Bold-print.ttf")),
        "resolution": settings.resolution,
        "plate_title": settings.plate_title, "plate_text_1": settings.plate_text_1,
        "plate_text_2": settings.plate_text_2, "plate_text_3": settings.plate_text_3,
        "plate_text_4": settings.plate_text_4, "plate_text_5": settings.plate_text_5,
        "x_offset": settings.x_offset, "y_offset": settings.y_offset,
        "max_terrain_relief_mm": settings.max_terrain_relief_mm,
        "terrain_elevation_multiplier": settings.terrain_elevation_multiplier,
        "unit_system": settings.unit_system,
        "plate_recess_depth": settings.plate_recess_depth,
        "plate_recess_clearance": 0.0,
        "dot_magnet_recess": settings.dot_magnet_recess,
        "trail_clearance_mm": TRAIL_CLEARANCE_MM,
        "water_min_area_mm2": WATER_MIN_AREA_MM2,
    }), encoding="utf-8")
    worker = Path(__file__).with_name("blender_worker.py")
    command = [str(blender_path), "--background", "--python", str(worker), "--", str(config_path)]
    return _run_command(command, timeout, cancel_event)


def _attempt_log(label: str, result: subprocess.CompletedProcess) -> str:
    return f"\n[{label}]\nSTDOUT\n{result.stdout}\nSTDERR\n{result.stderr}\n"


def _failure_detail(result: subprocess.CompletedProcess) -> str:
    """Return the useful exception line instead of unrelated trailing output."""
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    exception_line = next(
        (line for line in reversed(stderr_lines)
         if line.startswith(("Error:", "RuntimeError:", "ValueError:",
                             "AttributeError:", "TypeError:", "KeyError:",
                             "ImportError:", "ModuleNotFoundError:"))),
        None,
    )
    if exception_line:
        return exception_line
    return next(
        (line.strip() for line in reversed(stderr_lines + result.stdout.splitlines())
         if line.strip()),
        "unknown error",
    )


def _render_preview(stls: List[Path], project_slug: str, output: Path, config_path: Path,
                    blender_path: Path, timeout: int, cancel_event=None,
                    color_settings=None) -> subprocess.CompletedProcess:
    config_path.write_text(json.dumps({
        "output_path": str(output),
        "color_settings": color_settings or {},
        "stl_files": [{"path": str(path), "role": mesh_role(path, project_slug)}
                      for path in stls],
    }), encoding="utf-8")
    worker = Path(__file__).with_name("preview_worker.py")
    command = [str(blender_path), "--background", "--python", str(worker), "--", str(config_path)]
    return _run_command(command, timeout, cancel_event)


def _normalize_generated_stage(project, stage):
    _prefix_staged_outputs(project, stage)
    files = list(stage.glob("*.stl"))
    return normalize_anomalous_assembly(
        files, [inspect_stl(path, project.name) for path in files], project.name)


def _validate_generated_stage(paths, slug, corrections, expected_trail_segments):
    validation = validate_stls(paths, slug, expected_trail_segments=expected_trail_segments)
    if corrections:
        validation["corrections"] = corrections + validation.get("corrections", [])
    return validation


def _process_generated_stage(stage, slug, corrections, blender_path, timeout,
                             started, logs, cancel_event, expected_trail_segments):
    """Apply the same geometry processing to primary and fallback builds."""
    corrections.extend(cleanup_fragmented_trails(
        list(stage.glob("*.stl")), slug, expected_trail_segments))
    remaining = max(1, timeout - int(time.monotonic() - started))
    water, water_log = _integrate_generated_water(
        stage, slug, blender_path, remaining, cancel_event)
    if water_log:
        logs.append(water_log)
    _correct_staged_groove(
        stage, slug, blender_path,
        max(1, timeout - int(time.monotonic() - started)), cancel_event,
        expected_trail_segments=expected_trail_segments)
    return water


def run_blender_build(project: Path, settings: BuildSettings, blender: Optional[Path] = None,
                      timeout: int = 3600, cancel_event=None) -> List[Path]:
    project = project.resolve()
    require_model_font()
    route = project / "route.gpx"
    if not route.is_file():
        raise ValueError(f"missing GPX input: {route}")
    expected_trail_segments = len(read_gpx_segments(route))
    resolved_title = settings.plate_title
    validate_title_fit(resolved_title, settings.shape, settings.shape_extra)
    settings = BuildSettings.validated(
        shape=settings.shape, shape_extra=settings.shape_extra,
        resolution=settings.resolution, plate_title=resolved_title,
        plate_text_1=settings.plate_text_1, plate_text_2=settings.plate_text_2,
        plate_text_3=settings.plate_text_3, plate_text_4=settings.plate_text_4,
        plate_text_5=settings.plate_text_5, x_offset=settings.x_offset,
        y_offset=settings.y_offset,
        max_terrain_relief_mm=settings.max_terrain_relief_mm,
        terrain_elevation_multiplier=settings.terrain_elevation_multiplier,
        city=settings.city, state=settings.state, country=settings.country,
        unit_system=settings.unit_system,
        plate_recess_depth=settings.plate_recess_depth,
        dot_magnet_recess=settings.dot_magnet_recess)
    blender_path = find_blender(blender)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    attempt_root = build_dir / f"trailprint3d-{uuid.uuid4().hex}"
    attempt_root.mkdir()
    primary_stage = attempt_root / "primary"
    fallback_stage = attempt_root / "no-shape-extras"
    log_path = build_dir / "trailprint3d.log"
    report_path = build_dir / "validation.json"
    previous_customer_preview = ""
    try:
        previous_report = json.loads(report_path.read_text(encoding="utf-8"))
        previous_customer_preview = str(
            previous_report.get("preview", {}).get("customer_filename", ""))
        if (Path(previous_customer_preview).name != previous_customer_preview
                or not previous_customer_preview.endswith("-preview.png")):
            previous_customer_preview = ""
    except (OSError, ValueError, TypeError):
        pass
    report_path = build_dir / "failed-validation.json"
    report_path.unlink(missing_ok=True)
    PROGRESS_FILE.unlink(missing_ok=True)
    started = time.monotonic()
    logs = []
    try:
        result = _run_attempt(project, settings, primary_stage, blender_path, timeout, cancel_event)
    except BuildCancelled:
        log_path.write_text("Build cancelled before outputs were committed.\n", encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise
    except subprocess.TimeoutExpired as exc:
        log_path.write_text((exc.stdout or "") + "\n" + (exc.stderr or ""), encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        raise ValueError(f"TrailPrint3D build timed out after {timeout // 60} minutes.") from exc
    logs.append(_attempt_log("requested settings", result))
    if result.returncode != 0:
        log_path.write_text("".join(logs), encoding="utf-8")
        coverage_path = primary_stage / "terrain-data-validation.json"
        if coverage_path.is_file():
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            if not coverage.get("valid"):
                report_path.write_text(json.dumps({
                    "valid": False, "terrain_data": coverage,
                    "errors": coverage.get("errors", []),
                    "preview": {"customer_filename": previous_customer_preview},
                }, indent=2), encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        detail = _failure_detail(result)
        raise ValueError(f"TrailPrint3D failed: {detail}")
    pre_water_corrections = _normalize_generated_stage(project, primary_stage)
    water_integration = None
    try:
        water_integration = _process_generated_stage(
            primary_stage, project.name, pre_water_corrections, blender_path, timeout,
            started, logs, cancel_event, expected_trail_segments)
    except (BuildCancelled, subprocess.TimeoutExpired, ValueError) as exc:
        log_path.write_text("".join(logs) + f"\n[water integration]\n{exc}\n", encoding="utf-8")
        if not isinstance(exc, BuildCancelled):
            failure_report = {"valid": False, "errors": [str(exc)],
                              "corrections": pre_water_corrections,
                              "duration_seconds": round(time.monotonic() - started, 1)}
            if hasattr(exc, "repair_report"):
                failure_report["terrain_repair"] = exc.repair_report
            if (primary_stage / "groove-correction.json").exists():
                failure_report["groove_correction"] = json.loads(
                    (primary_stage / "groove-correction.json").read_text())
            write_report(report_path, failure_report)
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        if isinstance(exc, BuildCancelled):
            raise
        raise ValueError(f"Terrain/water integration failed: {exc}") from exc
    primary_paths = list(primary_stage.glob("*.stl"))
    primary_validation = _validate_generated_stage(
        primary_paths, project.name, pre_water_corrections, expected_trail_segments)
    _validate_staged_trail_fit(primary_stage, project.name, primary_validation)
    final_validation = primary_validation
    fallback_validation = None
    fallback_used = False
    fallback_alignment = None
    elevation_stage = primary_stage
    valid_roles = {item["role"] for item in primary_validation["files"] if item["valid"]}
    invalid_roles = {item["role"] for item in primary_validation["files"] if not item["valid"]}
    needs_core_fallback = not {"terrain", "trail"}.issubset(valid_roles)
    fallback_allowed = (settings.shape_extra != "NONE" and needs_core_fallback
                        and invalid_roles.issubset({"terrain", "trail"}))
    if fallback_allowed:
        PROGRESS_FILE.write_text(json.dumps({
            "phase": "Retrying terrain and trail without Shape Extras", "progress": 90,
        }), encoding="utf-8")
        fallback_settings = replace(settings, shape_extra="NONE")
        remaining = max(1, timeout - int(time.monotonic() - started))
        try:
            fallback_result = _run_attempt(
                project, fallback_settings, fallback_stage, blender_path, remaining, cancel_event)
        except BuildCancelled:
            log_path.write_text("Build cancelled during automatic fallback.\n", encoding="utf-8")
            shutil.rmtree(attempt_root, ignore_errors=True)
            PROGRESS_FILE.unlink(missing_ok=True)
            raise
        except subprocess.TimeoutExpired:
            logs.append("\n[no Shape Extras fallback]\nTimed out\n")
            fallback_validation = {"valid": False, "files": [], "corrections": [],
                                   "errors": ["Fallback build timed out."]}
        else:
            logs.append(_attempt_log("no Shape Extras fallback", fallback_result))
            if fallback_result.returncode == 0:
                fallback_pre_water_corrections = _normalize_generated_stage(project, fallback_stage)
                try:
                    fallback_water = _process_generated_stage(
                        fallback_stage, project.name, fallback_pre_water_corrections,
                        blender_path, timeout, started, logs, cancel_event,
                        expected_trail_segments)
                except BuildCancelled:
                    log_path.write_text(
                        "Build cancelled during fallback water integration.\n", encoding="utf-8")
                    shutil.rmtree(attempt_root, ignore_errors=True)
                    PROGRESS_FILE.unlink(missing_ok=True)
                    raise
                except (subprocess.TimeoutExpired, ValueError) as exc:
                    fallback_validation = {
                        "valid": False, "files": [],
                        "corrections": fallback_pre_water_corrections,
                        "errors": [f"Fallback water integration failed: {exc}"],
                    }
                    if hasattr(exc, "repair_report"):
                        fallback_validation["terrain_repair"] = exc.repair_report
                    fallback_water = None
                else:
                    if fallback_water is not None:
                        water_integration = fallback_water
                fallback_paths = list(fallback_stage.glob("*.stl"))
                if fallback_validation is None:
                    fallback_validation = _validate_generated_stage(
                        fallback_paths, project.name, fallback_pre_water_corrections,
                        expected_trail_segments)
                _validate_staged_trail_fit(fallback_stage, project.name, fallback_validation)
                if fallback_validation["valid"]:
                    fallback_alignment = align_core_to_terrain(
                        fallback_paths, primary_paths, project.name)
                    for path in list(primary_stage.glob("*.stl")):
                        if mesh_role(path, project.name) in {"terrain", "trail", "water"}:
                            path.unlink()
                    for path in fallback_paths:
                        if mesh_role(path, project.name) in {"terrain", "trail", "water"}:
                            shutil.copy2(path, primary_stage / path.name)
                    final_validation = validate_stls(list(primary_stage.glob("*.stl")), project.name,
                                                     expected_trail_segments=expected_trail_segments)
                    _validate_staged_trail_fit(primary_stage, project.name, final_validation)
                    fallback_used = final_validation["valid"]
                    if fallback_used:
                        elevation_stage = fallback_stage
            else:
                detail = next((line for line in reversed(
                    (fallback_result.stderr + fallback_result.stdout).splitlines()) if line.strip()),
                    "unknown error")
                fallback_validation = {"valid": False, "files": [], "corrections": [],
                                       "errors": [f"Fallback generation failed: {detail}"]}
    try:
        elevation = (_validate_terrain_relief(
            elevation_stage, final_validation, settings.max_terrain_relief_mm)
            if final_validation["valid"] else {})
    except ValueError as exc:
        log_path.write_text("".join(logs) + f"\n[terrain relief validation]\n{exc}\n",
                            encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise
    # TrailPrint3D's STL exporter can discard the Blender-side joined key
    # geometry. Reinsert the calibrated cylinders into the final Trail STL so
    # the requested keys are present in the deliverable.
    report = {
        "valid": final_validation["valid"], "fallback_used": fallback_used,
        "primary": primary_validation, "fallback": fallback_validation,
        "fallback_alignment": fallback_alignment, "final": final_validation,
        "trail_clearance_mm": TRAIL_CLEARANCE_MM,
        "water_integration": water_integration,
        "terrain_elevation": elevation,
        "terrain_data": (json.loads((elevation_stage / "terrain-data-validation.json").read_text())
                         if (elevation_stage / "terrain-data-validation.json").is_file() else None),
        "trail_fit": final_validation.get("trail_fit"),
    }
    if water_integration is not None:
        water_integration["trail_fit"] = report["trail_fit"]
    correction_stage = elevation_stage
    if (correction_stage / "groove-correction.json").exists():
        report["groove_correction"] = json.loads(
            (correction_stage / "groove-correction.json").read_text())
    try:
        report["boolean_origin"] = json.loads(
            (primary_stage / "generation-local-origin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        report["dot_magnets"] = json.loads(
            (primary_stage / "plate-dot-magnets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if cancel_event is not None and cancel_event.is_set():
        log_path.write_text("Build cancelled before outputs were committed.\n", encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise BuildCancelled("Blender build cancelled.")
    if not final_validation["valid"]:
        previous_customer_path = project / "deliverables" / previous_customer_preview
        if (previous_customer_preview
                and (build_dir / "preview.png").is_file()
                and previous_customer_path.is_file()):
            report["preview"] = {
                "valid": True,
                "filename": "preview.png",
                "customer_filename": previous_customer_preview,
                "preserved": True,
            }
        report["duration_seconds"] = round(time.monotonic() - started, 1)
        write_report(report_path, report)
        log_path.write_text(f"Duration: {time.monotonic() - started:.1f}s\n" + "".join(logs), encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        errors = final_validation["errors"]
        detail = next((error for error in errors if not error.startswith("No valid ")), None)
        detail = detail or (errors[0] if errors else "STL validation failed.")
        if fallback_validation and fallback_validation.get("errors"):
            detail += f" Fallback: {fallback_validation['errors'][0]}"
        raise ValueError(f"STL validation failed: {detail}")
    staged = list(primary_stage.glob("*.stl"))
    preview_stage = attempt_root / "preview.png"
    preview = {"valid": False, "error": "Preview rendering did not run."}
    PROGRESS_FILE.write_text(json.dumps({
        "phase": "Rendering bird's-eye preview", "progress": 96,
    }), encoding="utf-8")
    remaining = max(1, timeout - int(time.monotonic() - started))
    try:
        preview_result = _render_preview(
            staged, project.name, preview_stage, attempt_root / "preview-job.json",
            blender_path, remaining, cancel_event)
    except BuildCancelled:
        log_path.write_text("Build cancelled during preview rendering.\n", encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise
    except subprocess.TimeoutExpired:
        logs.append("\n[bird's-eye preview]\nTimed out\n")
        preview = {"valid": False, "error": "Preview rendering timed out."}
    else:
        logs.append(_attempt_log("bird's-eye preview", preview_result))
        if preview_stage.is_file() and preview_stage.stat().st_size >= 1000:
            preview = {
                "valid": True,
                "filename": "preview.png",
                "customer_filename": customer_preview_name(
                    resolved_title, human_name(project)),
            }
        else:
            detail = next((line for line in reversed(
                (preview_result.stderr + preview_result.stdout).splitlines()) if line.strip()),
                "Blender did not create a usable preview image.")
            preview = {"valid": False, "error": detail}
    side_stage = attempt_root / "preview-side.png"
    side_preview = {"valid": side_stage.is_file() and side_stage.stat().st_size >= 1000}
    if side_preview["valid"]:
        side_preview.update(filename="preview-side.png",
                            customer_filename=customer_preview_name(resolved_title, human_name(project)).replace("-preview.png", "-side-preview.png"))
    else:
        side_preview["error"] = "Angled preview rendering failed."
    report["preview_side"] = side_preview
    report["preview"] = preview
    report["duration_seconds"] = round(time.monotonic() - started, 1)
    if cancel_event is not None and cancel_event.is_set():
        log_path.write_text("Build cancelled before outputs were committed.\n", encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise BuildCancelled("Blender build cancelled.")
    publication = attempt_root / "publication"
    staged_build = publication / "build"
    staged_build.mkdir(parents=True)
    deliverables = project / "deliverables"
    changes = {path: None for path in deliverables.glob("*.stl")}
    outputs = []
    for path in staged:
        destination = deliverables / path.name
        changes[destination] = path
        outputs.append(destination)
    managed = ("preview-side.png", "preview.png", "elevation.json", "terrain-samples.json",
               "terrain-osm-evidence.json", "coastal-water-source.json",
               "terrain-palette.json", "terrain-reference.png", "bambu-validation.json",
               "failed-validation.json")
    changes.update({build_dir / name: None for name in managed})
    changes[deliverables / canonical_3mf_name(project)] = None
    if previous_customer_preview:
        changes[deliverables / previous_customer_preview] = None
    previous_side = previous_customer_preview.replace("-preview.png", "-side-preview.png")
    if previous_side:
        changes[deliverables / previous_side] = None
    if side_preview["valid"]:
        changes[build_dir / "preview-side.png"] = side_stage
        side_customer = publication / "customer-side-preview.png"
        shutil.copy2(side_stage, side_customer)
        changes[deliverables / side_preview["customer_filename"]] = side_customer
    if preview["valid"]:
        changes[build_dir / "preview.png"] = preview_stage
        customer_stage = publication / "customer-preview.png"
        shutil.copy2(preview_stage, customer_stage)
        changes[deliverables / preview["customer_filename"]] = customer_stage
    if elevation:
        (staged_build / "elevation.json").write_text(json.dumps(elevation, indent=2)+"\n")
    for name in ("terrain-samples.json", "terrain-osm-evidence.json", "coastal-water-source.json"):
        path = elevation_stage / name
        if path.is_file() and (name == "coastal-water-source.json" or elevation):
            shutil.copy2(path, staged_build / name)
    if (elevation and (staged_build / "terrain-samples.json").is_file()
            and (staged_build / "terrain-osm-evidence.json").is_file()):
        try:
            palette = analyze_project(publication)
            report["terrain_palette"] = {
                "available": True, "bands": len(palette["bands"]),
                "confidence": palette["confidence"], "mapped_coverage": palette["mapped_coverage"]}
        except Exception as exc:
            report["terrain_palette"] = {"available": False, "error": str(exc)}
    else:
        report["terrain_palette"] = {
            "available": False, "error": "Terrain OSM evidence is unavailable."}
    (staged_build / "current-settings.json").write_text(json.dumps(asdict(settings), indent=2) + "\n")
    for path in staged_build.iterdir():
        changes[build_dir / path.name] = path
    staged_report = staged_build / "validation.json"
    write_report(staged_report, report)
    changes[build_dir / "validation.json"] = staged_report

    def check_publish_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise BuildCancelled("Blender build cancelled before outputs were committed.")

    try:
        publish_files(changes, build_dir, before_commit=check_publish_cancelled)
    except BaseException:
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise
    log_path.write_text(f"Duration: {time.monotonic() - started:.1f}s\n" + "".join(logs), encoding="utf-8")
    shutil.rmtree(attempt_root, ignore_errors=True)
    PROGRESS_FILE.unlink(missing_ok=True)
    return outputs


def read_progress() -> dict:
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
