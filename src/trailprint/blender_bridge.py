"""Run the installed TrailPrint3D Blender extension as TrailPrint's engine."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


@dataclass(frozen=True)
class BuildSettings:
    shape: str = "HEXAGON"
    shape_extra: str = "NONE"
    resolution: int = 4

    @classmethod
    def validated(cls, shape="HEXAGON", shape_extra="NONE", resolution=4):
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
        return cls(shape, shape_extra, resolution)


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


def run_blender_build(project: Path, settings: BuildSettings, blender: Optional[Path] = None,
                      timeout: int = 3600) -> List[Path]:
    project = project.resolve()
    route = project / "route.gpx"
    if not route.is_file():
        raise ValueError(f"missing GPX input: {route}")
    settings = BuildSettings.validated(settings.shape, settings.shape_extra, settings.resolution)
    blender_path = find_blender(blender)
    build_dir = project / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    stage = build_dir / f"trailprint3d-{uuid.uuid4().hex}"
    stage.mkdir()
    config_path = stage / "job.json"
    config_path.write_text(json.dumps({
        "gpx_file": str(route), "export_path": str(stage), "project_name": project.name,
        "shape": settings.shape, "shape_extra": settings.shape_extra,
        "resolution": settings.resolution,
    }), encoding="utf-8")
    worker = Path(__file__).with_name("blender_worker.py")
    log_path = build_dir / "trailprint3d.log"
    PROGRESS_FILE.unlink(missing_ok=True)
    command = [str(blender_path), "--background", "--python", str(worker), "--", str(config_path)]
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text((exc.stdout or "") + "\n" + (exc.stderr or ""), encoding="utf-8")
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError(f"TrailPrint3D build timed out after {timeout // 60} minutes.") from exc
    log_path.write_text(
        f"Duration: {time.monotonic() - started:.1f}s\nCommand: {' '.join(command)}\n\n"
        f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}", encoding="utf-8")
    if result.returncode != 0:
        shutil.rmtree(stage, ignore_errors=True)
        detail = next((line for line in reversed((result.stderr + result.stdout).splitlines()) if line.strip()), "unknown error")
        raise ValueError(f"TrailPrint3D failed: {detail}")
    staged = [path for path in stage.rglob("*.stl") if path.stat().st_size > 0]
    if not staged:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError("TrailPrint3D finished without producing an STL. See build/trailprint3d.log.")
    outputs = _promote_stls(staged, project / "deliverables", build_dir)
    shutil.rmtree(stage, ignore_errors=True)
    return outputs


def read_progress() -> dict:
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
