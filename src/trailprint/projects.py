"""Safe creation and discovery of print projects."""

import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List

from .config import load_config
from .gpx import read_gpx


MAX_GPX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ProjectSummary:
    slug: str
    has_gpx: bool
    has_stl: bool


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        raise ValueError("Enter a project name containing letters or numbers.")
    if slug in {".", "..", "new"}:
        raise ValueError("Choose a different project name.")
    return slug


def resolve_project(prints_dir: Path, slug: str) -> Path:
    if slugify(slug) != slug:
        raise ValueError("Invalid project name.")
    root = prints_dir.resolve()
    project = (root / slug).resolve()
    if project.parent != root:
        raise ValueError("Invalid project path.")
    return project


def list_projects(prints_dir: Path) -> List[ProjectSummary]:
    if not prints_dir.is_dir():
        return []
    projects = []
    for path in sorted(prints_dir.iterdir(), key=lambda item: item.name.lower()):
        if path.is_dir() and not path.name.startswith("."):
            projects.append(ProjectSummary(
                path.name,
                (path / "route.gpx").is_file(),
                any((path / "deliverables").glob("*.stl")) if (path / "deliverables").is_dir() else False,
            ))
    return projects


def read_upload(stream: BinaryIO, filename: str, limit: int = MAX_GPX_BYTES) -> bytes:
    if not filename or Path(filename).suffix.lower() != ".gpx":
        raise ValueError("Choose a file with a .gpx extension.")
    data = stream.read(limit + 1)
    if not data:
        raise ValueError("The GPX file is empty.")
    if len(data) > limit:
        raise ValueError("The GPX file must be 10 MB or smaller.")
    return data


def create_project(prints_dir: Path, presets_dir: Path, name: str, gpx_data: bytes) -> Path:
    slug = slugify(name)
    prints_dir.mkdir(parents=True, exist_ok=True)
    destination = resolve_project(prints_dir, slug)
    if destination.exists():
        raise ValueError(f"A project named '{slug}' already exists.")

    temporary = Path(tempfile.mkdtemp(prefix=".trailprint-", dir=str(prints_dir)))
    try:
        route = temporary / "route.gpx"
        route.write_bytes(gpx_data)
        read_gpx(route)
        (temporary / "print.yaml").write_text("preset: standard\n", encoding="utf-8")
        (temporary / "README.md").write_text(
            f"# {name.strip()}\n\nCreated with the TrailPrint web interface.\n", encoding="utf-8")
        (temporary / "build").mkdir()
        (temporary / "build" / ".gitkeep").touch()
        (temporary / "deliverables").mkdir()
        (temporary / "deliverables" / ".gitkeep").touch()
        load_config(temporary, presets_dir)
        os.rename(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination
