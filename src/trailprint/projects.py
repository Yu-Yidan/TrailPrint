"""Safe creation and discovery of print projects."""

import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, List, Optional

from .gpx import read_gpx
from .naming import canonical_3mf_name, human_name, logical_slug, read_project_meta, storage_name

MAX_GPX_BYTES = 10 * 1024 * 1024
TRASH_PATTERN = re.compile(r"^(?P<slug>[a-z0-9][a-z0-9-]*)--(?P<stamp>\d{8}T\d{6}Z)--[a-f0-9]{8}$")
FUNNEL_STAGES = (("created", "Project created"), ("stl", "STL generated"),
                 ("3mf", "3MF generated"), ("printing", "Printing started"),
                 ("completed", "Print completed"))


@dataclass(frozen=True)
class ProjectSummary:
    slug: str
    name: str
    has_gpx: bool
    has_stl: bool
    has_3mf: bool
    stage: str
    is_test: bool = False
    printing_started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class DeletedProjectSummary:
    entry: str
    slug: str
    deleted_at: datetime
    expires_at: datetime
    days_remaining: int


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


def _active_project_for_slug(prints_dir: Path, slug: str) -> Optional[Path]:
    if not prints_dir.is_dir():
        return None
    return next((path for path in prints_dir.iterdir()
                 if path.is_dir() and not path.name.startswith(".")
                 and logical_slug(path) == slug), None)


def list_projects(prints_dir: Path) -> List[ProjectSummary]:
    if not prints_dir.is_dir():
        return []
    projects = []
    for path in sorted(prints_dir.iterdir(), key=lambda item: item.name.lower()):
        if path.is_dir() and not path.name.startswith("."):
            deliverables = path / "deliverables"
            has_stl = any(deliverables.glob("*.stl")) if deliverables.is_dir() else False
            has_3mf = (deliverables / canonical_3mf_name(path)).is_file()
            workflow = read_workflow(path)
            manual = workflow.get("status")
            stage = (manual if manual in {"printing", "completed"} else
                     "3mf" if has_3mf else "stl" if has_stl else "created")
            projects.append(ProjectSummary(
                path.name, human_name(path),
                (path / "route.gpx").is_file(),
                has_stl, has_3mf, stage,
                bool(read_project_meta(path).get("dim_is_test", False)),
                _parse_timestamp(workflow.get("printing_started_at")),
                _parse_timestamp(workflow.get("completed_at")),
            ))
    return projects


def _parse_timestamp(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_workflow(project: Path) -> dict:
    try:
        value = json.loads((project / "workflow.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("status") in {"printing", "completed"} else {}


def update_production_status(project: Path, action: str, now: datetime = None) -> dict:
    project = project.resolve()
    workflow = read_workflow(project)
    current = workflow.get("status")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp = moment.isoformat().replace("+00:00", "Z")
    path = project / "workflow.json"
    if action == "start":
        if current is not None:
            raise ValueError("This project has already entered printing.")
        if not (project / "deliverables" / canonical_3mf_name(project)).is_file():
            raise ValueError("Create the project 3MF before starting printing.")
        workflow = {"status": "printing", "printing_started_at": timestamp, "completed_at": None}
    elif action == "complete":
        if current != "printing":
            raise ValueError("Only a printing project can be marked completed.")
        workflow = {**workflow, "status": "completed", "completed_at": timestamp}
    elif action == "back":
        if current == "completed":
            workflow = {**workflow, "status": "printing", "completed_at": None}
        elif current == "printing":
            path.unlink(missing_ok=True)
            return {}
        else:
            raise ValueError("This project cannot move back from its current stage.")
    else:
        raise ValueError("Invalid production status action.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".workflow-", suffix=".json", dir=str(project))
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return workflow


def read_upload(stream: BinaryIO, filename: str, limit: int = MAX_GPX_BYTES) -> bytes:
    if not filename or Path(filename).suffix.lower() != ".gpx":
        raise ValueError("Choose a file with a .gpx extension.")
    data = stream.read(limit + 1)
    if not data:
        raise ValueError("The GPX file is empty.")
    if len(data) > limit:
        raise ValueError("The GPX file must be 10 MB or smaller.")
    return data


def create_project(prints_dir: Path, name: str, gpx_data: bytes) -> Path:
    slug = slugify(name)
    prints_dir.mkdir(parents=True, exist_ok=True)
    destination = resolve_project(prints_dir, slug)
    if destination.exists() or _active_project_for_slug(prints_dir, slug):
        raise ValueError(f"A project named '{slug}' already exists.")

    temporary = Path(tempfile.mkdtemp(prefix=".trailprint-", dir=str(prints_dir)))
    try:
        route = temporary / "route.gpx"
        route.write_bytes(gpx_data)
        read_gpx(route)
        (temporary / "README.md").write_text(
            f"# {name.strip()}\n\nCreated with the TrailPrint web interface.\n", encoding="utf-8")
        (temporary / "build").mkdir()
        (temporary / "build" / ".gitkeep").touch()
        (temporary / "deliverables").mkdir()
        (temporary / "deliverables" / ".gitkeep").touch()
        os.rename(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def migrate_active_project(project: Path) -> Path:
    """Apply the sortable name to one active project and all binary deliverables."""
    project = project.resolve()
    meta = read_project_meta(project)
    if not meta.get("id_project"):
        return project
    slug = str(meta.get("project_slug") or logical_slug(project))
    expected = project.parent / storage_name(meta["id_project"], slug)
    if expected != project and expected.exists():
        raise ValueError(f"Cannot rename '{project.name}': '{expected.name}' already exists.")
    prefix = f"{storage_name(meta['id_project'], slug).split('-', 1)[0]}-"
    deliverables = project / "deliverables"
    if deliverables.is_dir():
        renames = []
        for path in deliverables.iterdir():
            if path.is_file() and path.suffix.lower() in {".stl", ".3mf"} and not path.name.startswith(prefix):
                target = path.with_name(prefix + path.name)
                if target.exists():
                    raise ValueError(f"Cannot rename deliverable: '{target.name}' already exists.")
                renames.append((path, target))
        for source, target in renames:
            os.rename(source, target)
    if expected != project:
        os.rename(project, expected)
    return expected


def migrate_active_projects(prints_dir: Path) -> List[Path]:
    projects = [path for path in prints_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")] if prints_dir.is_dir() else []
    targets = {}
    for project in projects:
        meta = read_project_meta(project)
        if not meta.get("id_project"):
            continue
        target = project.parent / storage_name(meta["id_project"], meta.get("project_slug") or logical_slug(project))
        if target in targets or (target.exists() and target != project):
            raise ValueError(f"Cannot migrate projects: '{target.name}' is not unique.")
        targets[target] = project
    return [migrate_active_project(project) for project in projects]


def trash_project(prints_dir: Path, trash_dir: Path, slug: str, confirmation: str,
                  now: datetime = None) -> Path:
    """Move a confirmed project to recoverable local trash."""
    if logical_slug(raw_project := (prints_dir.resolve() / slug)) == "example-trail":
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


def purge_trash(trash_dir: Path, retention_days: int = 30, now: datetime = None,
                before_remove=None) -> List[Path]:
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
            if before_remove:
                before_remove(entry)
            shutil.rmtree(entry)
            removed.append(entry)
    return removed


def list_deleted_projects(trash_dir: Path, retention_days: int = 30,
                          now: datetime = None) -> List[DeletedProjectSummary]:
    """List safe, recognized trash entries that are still recoverable."""
    if not trash_dir.is_dir():
        return []
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    projects = []
    for entry in trash_dir.iterdir():
        match = TRASH_PATTERN.fullmatch(entry.name)
        if not match or not entry.is_dir() or entry.is_symlink():
            continue
        try:
            deleted_at = datetime.strptime(
                match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        expires_at = deleted_at + timedelta(days=retention_days)
        if expires_at <= moment:
            continue
        seconds = max(0, (expires_at - moment).total_seconds())
        projects.append(DeletedProjectSummary(
            entry.name, match.group("slug"), deleted_at, expires_at,
            max(1, int((seconds + 86399) // 86400))))
    return sorted(projects, key=lambda item: item.deleted_at, reverse=True)


def restore_project(prints_dir: Path, trash_dir: Path, entry_name: str,
                    retention_days: int = 30, now: datetime = None) -> Path:
    """Restore one recognized trash entry without overwriting active work."""
    match = TRASH_PATTERN.fullmatch(entry_name)
    if not match:
        raise ValueError("Invalid deleted project entry.")
    try:
        deleted_at = datetime.strptime(
            match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Invalid deleted project timestamp.") from exc
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if deleted_at + timedelta(days=retention_days) <= moment:
        raise ValueError("This deleted project has expired and can no longer be restored.")
    trash_root = trash_dir.resolve()
    source = trash_root / entry_name
    if source.parent != trash_root or source.is_symlink() or not source.is_dir():
        raise ValueError("Deleted project could not be found safely.")
    prints_dir.mkdir(parents=True, exist_ok=True)
    destination = resolve_project(prints_dir, match.group("slug"))
    if destination.exists() or _active_project_for_slug(prints_dir, logical_slug(source)):
        raise ValueError(f"A project named '{destination.name}' already exists.")
    os.rename(source, destination)
    return destination


def permanently_delete_projects(trash_dir: Path, entry_names, confirmation: str,
                                retention_days: int = 30,
                                now: datetime = None, before_remove=None) -> List[Path]:
    """Permanently remove a fully validated selection of recoverable trash entries."""
    if confirmation != "DELETE":
        raise ValueError("Type DELETE exactly to confirm permanent deletion.")
    entries = list(dict.fromkeys(str(name) for name in entry_names if name))
    if not entries:
        raise ValueError("Select at least one deleted project.")
    recoverable = {item.entry for item in list_deleted_projects(
        trash_dir, retention_days=retention_days, now=now)}
    trash_root = trash_dir.resolve()
    paths = []
    for name in entries:
        if not TRASH_PATTERN.fullmatch(name) or name not in recoverable:
            raise ValueError("One or more selected projects are not safely recoverable trash entries.")
        path = trash_root / name
        if path.parent != trash_root or path.is_symlink() or not path.is_dir():
            raise ValueError("One or more selected projects could not be found safely.")
        paths.append(path)
    for path in paths:
        if before_remove:
            before_remove(path)
        shutil.rmtree(path)
    return paths
