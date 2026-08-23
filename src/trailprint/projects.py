"""Safe creation and discovery of print projects."""

import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, List

from .config import load_config
from .gpx import read_gpx


MAX_GPX_BYTES = 10 * 1024 * 1024
TRASH_PATTERN = re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)--(?P<stamp>\d{8}T\d{6}Z)--[a-f0-9]{8}$")


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


def trash_project(prints_dir: Path, trash_dir: Path, slug: str, confirmation: str,
                  now: datetime = None) -> Path:
    """Move a confirmed project to recoverable local trash."""
    if slug == "example-trail":
        raise ValueError("The example project is protected and cannot be deleted.")
    if confirmation != slug:
        raise ValueError(f"Type '{slug}' exactly to confirm deletion.")
    root = prints_dir.resolve()
    raw_project = root / slug
    if raw_project.is_symlink():
        raise ValueError("Symbolic-link projects cannot be deleted from the app.")
    project = resolve_project(prints_dir, slug)
    if not project.is_dir():
        raise ValueError(f"Project '{slug}' does not exist.")
    trash_dir.mkdir(parents=True, exist_ok=True)
    trash_root = trash_dir.resolve()
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    destination = trash_root / f"{slug}--{moment.strftime('%Y%m%dT%H%M%SZ')}--{uuid.uuid4().hex[:8]}"
    if destination.parent != trash_root:
        raise ValueError("Invalid trash destination.")
    os.rename(project, destination)
    return destination


def purge_trash(trash_dir: Path, retention_days: int = 30, now: datetime = None) -> List[Path]:
    """Remove recognized app trash entries older than the retention window."""
    if not trash_dir.is_dir():
        return []
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = moment - timedelta(days=retention_days)
    removed = []
    for entry in trash_dir.iterdir():
        match = TRASH_PATTERN.fullmatch(entry.name)
        if not match or not entry.is_dir() or entry.is_symlink():
            continue
        try:
            created = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created < cutoff:
            shutil.rmtree(entry)
            removed.append(entry)
    return removed
