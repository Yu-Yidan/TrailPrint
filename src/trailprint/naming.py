"""Canonical project identities and sortable filesystem names."""

import json
import re
from pathlib import Path


ID_WIDTH = 4
MAX_PROJECT_ID = 10 ** ID_WIDTH - 1
STORAGE_PATTERN = re.compile(r"^(?P<id>\d{4})-(?P<slug>[a-z0-9][a-z0-9-]*)$")
META_FILE = "project.json"


def format_project_id(value) -> str:
    try:
        identifier = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Project ID must be an integer.") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("Project ID must be an integer.")
    if identifier < 1 or identifier > MAX_PROJECT_ID:
        raise ValueError(f"Project ID must be between 1 and {MAX_PROJECT_ID}.")
    return f"{identifier:0{ID_WIDTH}d}"


def storage_name(identifier, slug: str) -> str:
    return f"{format_project_id(identifier)}-{slug}"


def read_project_meta(project: Path) -> dict:
    try:
        value = json.loads((project / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            value = json.loads((project / "project_analytics.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def logical_slug(project: Path) -> str:
    meta = read_project_meta(project)
    if meta.get("project_slug"):
        return str(meta["project_slug"])
    match = STORAGE_PATTERN.fullmatch(project.name)
    return match.group("slug") if match else project.name


def human_name(project: Path) -> str:
    meta = read_project_meta(project)
    if meta.get("dim_project_name"):
        return str(meta["dim_project_name"])
    try:
        first = (project / "README.md").read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        first = ""
    return first.lstrip("#").strip() or logical_slug(project).replace("-", " ").title()


def deliverable_prefix(project: Path) -> str:
    match = STORAGE_PATTERN.fullmatch(project.name)
    if match:
        return f"{match.group('id')}-"
    meta = read_project_meta(project)
    return f"{format_project_id(meta['id_project'])}-" if meta.get("id_project") else ""


def canonical_3mf_name(project: Path) -> str:
    return f"{project.name}.3mf"
