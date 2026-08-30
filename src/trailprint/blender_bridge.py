"""Run the installed TrailPrint3D Blender extension as TrailPrint's engine."""

import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gpx import (
    AUTO_RELIEF_LIMIT_MM,
    format_trip_stats,
    gpx_center_latitude,
    read_gpx_trip_date,
    read_gpx_trip_stats,
)
from .mesh_validation import align_core_to_terrain, mesh_role, validate_stls, write_report
from .naming import deliverable_prefix, human_name
from .osm_colors import analyze_project
from .title import resolve_project_title, validate_title_fit


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


@dataclass(frozen=True)
class BuildSettings:
    shape: str = "HEXAGON"
    shape_extra: str = "NONE"
    resolution: int = 8
    plate_title: str = "{name}"
    plate_text_1: str = "{length}"
    plate_text_2: str = "{date}"
    plate_text_3: str = "{duration}"
    plate_text_4: str = "{country}"
    plate_text_5: str = "{state}"
    x_offset: float = 0.0
    y_offset: float = 0.0
    fixed_elevation_scale: bool = False
    city: str = ""
    state: str = ""
    country: str = ""
    unit_system: str = "IMPERIAL"
    plate_recess_depth: float = 1.5

    @classmethod
    def validated(cls, shape="HEXAGON", shape_extra="NONE", resolution=8,
                  plate_title="{name}", plate_text_1="{length}",
                  plate_text_2="{date}", plate_text_3="{duration}",
                  plate_text_4="{country}", plate_text_5="{state}", x_offset=0, y_offset=0,
                  fixed_elevation_scale=False, city="", state="", country="",
                  unit_system="IMPERIAL", plate_recess_depth=1.5):
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
        return cls(
            shape=shape, shape_extra=shape_extra, resolution=resolution,
            plate_title=text_values[0], plate_text_1=text_values[1],
            plate_text_2=text_values[2], plate_text_3=text_values[3],
            plate_text_4=text_values[4], plate_text_5=text_values[5],
            x_offset=x_offset, y_offset=y_offset,
            fixed_elevation_scale=bool(fixed_elevation_scale),
            city=locations[0], state=locations[1], country=locations[2],
            unit_system=unit_system, plate_recess_depth=plate_recess_depth)


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


def _promote_stls(staged: List[Path], deliverables: Path, build_dir: Path) -> List[Path]:
    deliverables.mkdir(parents=True, exist_ok=True)
    backup = build_dir / f"previous-stls-{uuid.uuid4().hex}"
    backup.mkdir()
    old = list(deliverables.glob("*.stl"))
    promoted: List[Path] = []
    try:
        for path in old:
            os.replace(path, backup / path.name)
        for path in staged:
            destination = deliverables / path.name
            os.replace(path, destination)
            promoted.append(destination)
    except Exception:
        for path in promoted:
            path.unlink(missing_ok=True)
        for path in backup.glob("*.stl"):
            os.replace(path, deliverables / path.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
    return promoted


def _prefix_staged_outputs(project: Path, stage: Path) -> None:
    prefix = deliverable_prefix(project)
    for path in list(stage.glob("*.stl")):
        if prefix and not path.name.startswith(prefix):
            path.rename(path.with_name(prefix + path.name))


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
    return {
        "source_min_m": minimum, "source_max_m": maximum,
        "model_surface_min_z_mm": surface_min,
        "model_surface_max_z_mm": surface_max,
        "min_thickness_mm": thickness,
        "unit_system": str(value.get("unit_system", "IMPERIAL")).upper(),
    }


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
    trip_values = format_trip_stats(read_gpx_trip_stats(route), settings.unit_system)
    config_path.write_text(json.dumps({
        "gpx_file": str(route), "export_path": str(stage), "project_name": project.name,
        "shape": settings.shape, "shape_extra": settings.shape_extra,
        "resolution": settings.resolution,
        "plate_title": settings.plate_title, "plate_text_1": settings.plate_text_1,
        "plate_text_2": settings.plate_text_2, "plate_text_3": settings.plate_text_3,
        "plate_text_4": settings.plate_text_4, "plate_text_5": settings.plate_text_5,
        "x_offset": settings.x_offset, "y_offset": settings.y_offset,
        "fixed_elevation_scale": settings.fixed_elevation_scale,
        "fixed_elevation_target_mm": AUTO_RELIEF_LIMIT_MM,
        "center_latitude": gpx_center_latitude(route),
        "trip_date": read_gpx_trip_date(route), "city": settings.city,
        "state": settings.state, "country": settings.country,
        "unit_system": settings.unit_system, **trip_values,
        "plate_recess_depth": settings.plate_recess_depth,
        "plate_recess_clearance": 0.0,
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
                    blender_path: Path, timeout: int, cancel_event=None) -> subprocess.CompletedProcess:
    config_path.write_text(json.dumps({
        "output_path": str(output),
        "stl_files": [{"path": str(path), "role": mesh_role(path, project_slug)}
                      for path in stls],
    }), encoding="utf-8")
    worker = Path(__file__).with_name("preview_worker.py")
    command = [str(blender_path), "--background", "--python", str(worker), "--", str(config_path)]
    return _run_command(command, timeout, cancel_event)


def run_blender_build(project: Path, settings: BuildSettings, blender: Optional[Path] = None,
                      timeout: int = 3600, cancel_event=None) -> List[Path]:
    project = project.resolve()
    route = project / "route.gpx"
    if not route.is_file():
        raise ValueError(f"missing GPX input: {route}")
    resolved_title = resolve_project_title(settings.plate_title, human_name(project))
    validate_title_fit(resolved_title, settings.shape, settings.shape_extra)
    settings = BuildSettings.validated(
        shape=settings.shape, shape_extra=settings.shape_extra,
        resolution=settings.resolution, plate_title=resolved_title,
        plate_text_1=settings.plate_text_1, plate_text_2=settings.plate_text_2,
        plate_text_3=settings.plate_text_3, plate_text_4=settings.plate_text_4,
        plate_text_5=settings.plate_text_5, x_offset=settings.x_offset,
        y_offset=settings.y_offset,
        fixed_elevation_scale=settings.fixed_elevation_scale,
        city=settings.city, state=settings.state, country=settings.country,
        unit_system=settings.unit_system,
        plate_recess_depth=settings.plate_recess_depth)
    blender_path = find_blender(blender)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    attempt_root = build_dir / f"trailprint3d-{uuid.uuid4().hex}"
    attempt_root.mkdir()
    primary_stage = attempt_root / "primary"
    fallback_stage = attempt_root / "no-shape-extras"
    log_path = build_dir / "trailprint3d.log"
    report_path = build_dir / "validation.json"
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
        shutil.rmtree(attempt_root, ignore_errors=True)
        detail = _failure_detail(result)
        raise ValueError(f"TrailPrint3D failed: {detail}")
    _prefix_staged_outputs(project, primary_stage)
    primary_paths = list(primary_stage.glob("*.stl"))
    primary_validation = validate_stls(primary_paths, project.name)
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
                _prefix_staged_outputs(project, fallback_stage)
                fallback_paths = list(fallback_stage.glob("*.stl"))
                fallback_validation = validate_stls(fallback_paths, project.name)
                if fallback_validation["valid"]:
                    fallback_alignment = align_core_to_terrain(
                        fallback_paths, primary_paths, project.name)
                    for path in list(primary_stage.glob("*.stl")):
                        if mesh_role(path, project.name) in {"terrain", "trail", "water"}:
                            path.unlink()
                    for path in fallback_paths:
                        if mesh_role(path, project.name) in {"terrain", "trail", "water"}:
                            shutil.copy2(path, primary_stage / path.name)
                    final_validation = validate_stls(list(primary_stage.glob("*.stl")), project.name)
                    fallback_used = final_validation["valid"]
                    if fallback_used:
                        elevation_stage = fallback_stage
            else:
                detail = next((line for line in reversed(
                    (fallback_result.stderr + fallback_result.stdout).splitlines()) if line.strip()),
                    "unknown error")
                fallback_validation = {"valid": False, "files": [], "corrections": [],
                                       "errors": [f"Fallback generation failed: {detail}"]}
    # TrailPrint3D's STL exporter can discard the Blender-side joined key
    # geometry. Reinsert the calibrated cylinders into the final Trail STL so
    # the requested keys are present in the deliverable.
    report = {
        "valid": final_validation["valid"], "fallback_used": fallback_used,
        "primary": primary_validation, "fallback": fallback_validation,
        "fallback_alignment": fallback_alignment, "final": final_validation,
    }
    if cancel_event is not None and cancel_event.is_set():
        log_path.write_text("Build cancelled before outputs were committed.\n", encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise BuildCancelled("Blender build cancelled.")
    if not final_validation["valid"]:
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
        if preview_result.returncode == 0 and preview_stage.is_file() and preview_stage.stat().st_size >= 1000:
            preview = {"valid": True, "filename": "preview.png"}
        else:
            detail = next((line for line in reversed(
                (preview_result.stderr + preview_result.stdout).splitlines()) if line.strip()),
                "Blender did not create a usable preview image.")
            preview = {"valid": False, "error": detail}
    report["preview"] = preview
    report["duration_seconds"] = round(time.monotonic() - started, 1)
    if cancel_event is not None and cancel_event.is_set():
        log_path.write_text("Build cancelled before outputs were committed.\n", encoding="utf-8")
        shutil.rmtree(attempt_root, ignore_errors=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        raise BuildCancelled("Blender build cancelled.")
    outputs = _promote_stls(staged, project / "deliverables", build_dir)
    preview_path = build_dir / "preview.png"
    if preview["valid"]:
        os.replace(preview_stage, preview_path)
    else:
        preview_path.unlink(missing_ok=True)
    elevation = _read_elevation_metadata(elevation_stage, final_validation)
    elevation_path = build_dir / "elevation.json"
    if elevation:
        temporary_elevation = elevation_path.with_suffix(".tmp")
        temporary_elevation.write_text(json.dumps(elevation, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_elevation, elevation_path)
    else:
        elevation_path.unlink(missing_ok=True)
    samples_path = build_dir / "terrain-samples.json"
    staged_samples = elevation_stage / "terrain-samples.json"
    evidence_path = build_dir / "terrain-osm-evidence.json"
    staged_evidence = elevation_stage / "terrain-osm-evidence.json"
    (build_dir / "terrain-palette.json").unlink(missing_ok=True)
    # Remove the thumbnail left by pre-v2 ESA builds.
    (build_dir / "terrain-reference.png").unlink(missing_ok=True)
    if elevation and staged_samples.is_file() and staged_evidence.is_file():
        shutil.copy2(staged_samples, samples_path)
        shutil.copy2(staged_evidence, evidence_path)
        try:
            palette = analyze_project(project)
            report["terrain_palette"] = {
                "available": True, "bands": len(palette["bands"]),
                "confidence": palette["confidence"],
                "mapped_coverage": palette["mapped_coverage"],
            }
        except Exception as exc:
            report["terrain_palette"] = {"available": False, "error": str(exc)}
    else:
        samples_path.unlink(missing_ok=True)
        evidence_path.unlink(missing_ok=True)
        report["terrain_palette"] = {
            "available": False, "error": "Terrain OSM evidence is unavailable."
        }
    write_report(report_path, report)
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
