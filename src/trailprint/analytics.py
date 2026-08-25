"""Small, durable CSV analytics store for TrailPrint's local workflow."""

import csv
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .projects import TRASH_PATTERN, list_deleted_projects, read_workflow
from .naming import canonical_3mf_name, format_project_id, human_name, logical_slug


PACIFIC = ZoneInfo("America/Los_Angeles")
STATUS_ROWS = (
    (1, "project created"), (2, "stl generated"), (3, "3mf generated"),
    (4, "print started"), (5, "print completed"),
    (6, "project soft deleted"), (7, "project permanently deleted"),
)
STATUS_NAMES = tuple(name for _identifier, name in STATUS_ROWS)
PROJECT_FIELDS = ("id_project", "dim_project_name", "project_slug", "ts_created",
                  "ts_permanently_deleted")
SNAPSHOT_FIELDS = ("id", "id_project", "dim_project_name", "ds", "dim_status")
FACT_FIELDS = ("id", "id_project", "dim_project_name", "status_pre", "status_post", "ds", "ts")
META_FILE = "project_analytics.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pacific_ds(moment: datetime) -> str:
    return moment.astimezone(PACIFIC).date().isoformat()


def project_name(project: Path) -> str:
    return human_name(project)


def effective_status(project: Path) -> str:
    workflow = read_workflow(project)
    if workflow.get("status") == "completed":
        return "print completed"
    if workflow.get("status") == "printing":
        return "print started"
    deliverables = project / "deliverables"
    if (deliverables / canonical_3mf_name(project)).is_file():
        return "3mf generated"
    if deliverables.is_dir() and any(deliverables.glob("*.stl")):
        return "stl generated"
    return "project created"


def _read_csv(path: Path) -> list:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".csv", dir=str(path.parent))
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AnalyticsStore:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.root = self.repo_root / "data/analytics"
        self.internal = self.root / "internal"
        self.lock_path = self.internal / "analytics.lock"
        self.projects_path = self.root / "dim_project.csv"
        self.status_path = self.root / "dim_status.csv"
        self.snapshots_path = self.root / "dim_project_status.csv"
        self.facts_path = self.root / "fct_project_status.csv"
        self.state_path = self.internal / "pipeline_state.json"

    @contextmanager
    def locked(self):
        self.internal.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self, prints_dir: Path, trash_dir: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.locked():
            if not self.status_path.exists():
                _write_csv(self.status_path, ("id", "dim_status"),
                           ({"id": identifier, "dim_status": name}
                            for identifier, name in STATUS_ROWS))
            for path, fields in ((self.projects_path, PROJECT_FIELDS),
                                 (self.snapshots_path, SNAPSHOT_FIELDS),
                                 (self.facts_path, FACT_FIELDS)):
                if not path.exists():
                    _write_csv(path, fields, [])
            rows = _read_csv(self.projects_path)
            known_ids = {int(row["id_project"]) for row in rows if row.get("id_project", "").isdigit()}
            next_id = max(known_ids, default=0) + 1
            candidates = []
            if prints_dir.is_dir():
                candidates.extend((path, False) for path in prints_dir.iterdir()
                                  if path.is_dir() and not path.name.startswith("."))
            if trash_dir.is_dir():
                candidates.extend((path, True) for path in trash_dir.iterdir()
                                  if path.is_dir() and not path.is_symlink()
                                  and TRASH_PATTERN.fullmatch(path.name))
            candidates.sort(key=lambda item: (getattr(item[0].stat(), "st_birthtime", item[0].stat().st_ctime),
                                               item[0].name))
            by_id = {row["id_project"]: row for row in rows}
            for folder, deleted in candidates:
                meta = self._read_meta(folder)
                if not meta:
                    identifier = next_id; next_id += 1
                    slug = (TRASH_PATTERN.fullmatch(folder.name).group("slug") if deleted
                            else logical_slug(folder))
                    created = datetime.fromtimestamp(
                        getattr(folder.stat(), "st_birthtime", folder.stat().st_ctime), timezone.utc)
                    status = "project soft deleted" if deleted else effective_status(folder)
                    meta = {"id_project": identifier, "dim_project_name": project_name(folder),
                            "project_slug": slug, "ts_created": format_ts(created),
                            "latest_status": status, "baseline_ds": pacific_ds(utc_now()),
                            "baseline_status": status}
                    self._write_meta(folder, meta)
                key = str(meta["id_project"])
                by_id[key] = {"id_project": key, "dim_project_name": meta["dim_project_name"],
                              "project_slug": meta["project_slug"], "ts_created": meta["ts_created"],
                              "ts_permanently_deleted": by_id.get(key, {}).get("ts_permanently_deleted", "")}
            _write_csv(self.projects_path, PROJECT_FIELDS,
                       sorted(by_id.values(), key=lambda row: int(row["id_project"])))

    def status_warnings(self) -> list:
        configured = {row.get("dim_status", "") for row in _read_csv(self.status_path)}
        return [name for name in STATUS_NAMES if name not in configured]

    def visualization_data(self, prints_dir: Path = None, trash_dir: Path = None) -> dict:
        """Return current status counts and Pacific-day project creation totals."""
        metadata_status = {}
        candidates = []
        for root in (prints_dir, trash_dir):
            if root and root.is_dir():
                candidates.extend(path for path in root.iterdir()
                                  if path.is_dir() and not path.is_symlink())
        for project in candidates:
            meta = self._read_meta(project)
            if meta.get("id_project") and meta.get("latest_status") in STATUS_NAMES:
                metadata_status[str(meta["id_project"])] = meta["latest_status"]

        with self.locked():
            projects = _read_csv(self.projects_path)
            facts = _read_csv(self.facts_path)
            snapshots = _read_csv(self.snapshots_path)

        latest_fact = {}
        for row in facts:
            project_id, status = row.get("id_project", ""), row.get("status_post", "")
            if project_id and status in STATUS_NAMES:
                candidate = (row.get("ts", ""), row.get("id", ""), status)
                if candidate[:2] > latest_fact.get(project_id, ("", "", ""))[:2]:
                    latest_fact[project_id] = candidate
        latest_snapshot = {}
        for row in snapshots:
            project_id, status = row.get("id_project", ""), row.get("dim_status", "")
            if project_id and status in STATUS_NAMES:
                candidate = (row.get("ds", ""), status)
                if candidate[0] > latest_snapshot.get(project_id, ("", ""))[0]:
                    latest_snapshot[project_id] = candidate

        counts = Counter({status: 0 for status in STATUS_NAMES})
        daily = Counter()
        for project in projects:
            project_id = str(project.get("id_project", ""))
            if project.get("ts_permanently_deleted"):
                status = "project permanently deleted"
            elif project_id in latest_fact:
                status = latest_fact[project_id][2]
            elif project_id in metadata_status:
                status = metadata_status[project_id]
            elif project_id in latest_snapshot:
                status = latest_snapshot[project_id][1]
            else:
                status = "project created"
            counts[status] += 1
            try:
                created = datetime.fromisoformat(
                    project["ts_created"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                daily[created.astimezone(PACIFIC).date()] += 1
            except (KeyError, ValueError, TypeError):
                continue

        created_daily = []
        if daily:
            cursor, final_date = min(daily), max(daily)
            while cursor <= final_date:
                created_daily.append({"date": cursor.isoformat(),
                                      "count": daily.get(cursor, 0)})
                cursor += timedelta(days=1)
        status_counts = [{"id": identifier, "status": status,
                          "label": status.title().replace("Stl", "STL").replace("3Mf", "3MF"),
                          "count": counts[status]}
                         for identifier, status in STATUS_ROWS]
        return {"status_counts": status_counts, "created_daily": created_daily,
                "total_projects": len(projects)}

    def register_project(self, project: Path, name: str, moment: datetime = None) -> dict:
        moment = moment or utc_now()
        with self.locked():
            rows = _read_csv(self.projects_path)
            identifier = max((int(row["id_project"]) for row in rows), default=0) + 1
            format_project_id(identifier)
            meta = {"id_project": identifier, "dim_project_name": name.strip(),
                    "project_slug": logical_slug(project), "ts_created": format_ts(moment),
                    "latest_status": "project created", "baseline_ds": pacific_ds(moment),
                    "baseline_status": "project created"}
            self._write_meta(project, meta)
            rows.append({"id_project": identifier, "dim_project_name": name.strip(),
                         "project_slug": logical_slug(project), "ts_created": format_ts(moment),
                         "ts_permanently_deleted": ""})
            _write_csv(self.projects_path, PROJECT_FIELDS, rows)
            self._append_fact_locked(meta, "", "project created", moment)
            return meta

    def next_project_id(self) -> int:
        """Return the next available ID, enforcing the sortable-name capacity."""
        with self.locked():
            rows = _read_csv(self.projects_path)
            identifier = max((int(row["id_project"]) for row in rows), default=0) + 1
            format_project_id(identifier)
            return identifier

    def sync_project(self, project: Path, moment: datetime = None) -> bool:
        moment = moment or utc_now()
        with self.locked():
            meta = self._read_meta(project) or self._ensure_meta_locked(project)
            post = effective_status(project)
            latest_name = project_name(project)
            if meta.get("latest_status") == post and meta.get("dim_project_name") == latest_name:
                return False
            if meta.get("latest_status") != post:
                event_meta = {**meta, "dim_project_name": latest_name}
                self._append_fact_locked(event_meta, meta.get("latest_status", ""), post, moment)
            meta["latest_status"] = post
            meta["dim_project_name"] = latest_name
            self._write_meta(project, meta)
            self._refresh_project_row_locked(meta)
            return True

    def transition(self, project: Path, post: str, moment: datetime = None) -> None:
        moment = moment or utc_now()
        with self.locked():
            meta = self._read_meta(project) or self._ensure_meta_locked(project)
            pre = meta.get("latest_status", "")
            if pre == post:
                return
            meta["dim_project_name"] = project_name(project)
            self._append_fact_locked(meta, pre, post, moment)
            meta["latest_status"] = post
            self._write_meta(project, meta)
            self._refresh_project_row_locked(meta)

    def mark_permanently_deleted(self, project: Path, moment: datetime = None) -> None:
        moment = moment or utc_now()
        with self.locked():
            meta = self._read_meta(project)
            if not meta:
                return
            meta["dim_project_name"] = project_name(project)
            if meta.get("latest_status") != "project permanently deleted":
                self._append_fact_locked(meta, meta.get("latest_status", ""),
                                         "project permanently deleted", moment)
            meta["latest_status"] = "project permanently deleted"
            self._refresh_project_row_locked(meta, permanently_deleted=format_ts(moment))

    def run_daily(self, prints_dir: Path, trash_dir: Path,
                  snapshot_date: date = None, moment: datetime = None) -> str:
        moment = moment or utc_now()
        self.initialize(prints_dir, trash_dir)
        if prints_dir.is_dir():
            for project in prints_dir.iterdir():
                if project.is_dir() and not project.name.startswith("."):
                    self.sync_project(project, moment)
        final_target = snapshot_date or (moment.astimezone(PACIFIC).date() - timedelta(days=1))
        if snapshot_date:
            targets = [snapshot_date]
        else:
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                first_target = date.fromisoformat(state["last_snapshot_ds"]) + timedelta(days=1)
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                first_target = final_target
            targets = []
            cursor = first_target
            while cursor <= final_target:
                targets.append(cursor); cursor += timedelta(days=1)
        if not targets:
            return final_target.isoformat()
        candidates = []
        if prints_dir.is_dir():
            candidates.extend(path for path in prints_dir.iterdir()
                              if path.is_dir() and not path.name.startswith("."))
        if trash_dir.is_dir():
            candidates.extend(path for path in trash_dir.iterdir()
                              if path.is_dir() and not path.is_symlink()
                              and TRASH_PATTERN.fullmatch(path.name))
        metadata = [self._read_meta(project) for project in candidates]
        with self.locked():
            facts = _read_csv(self.facts_path)
            existing = [row for row in _read_csv(self.snapshots_path)
                        if row.get("ds") not in {target.isoformat() for target in targets}]
            rows = []
            for target in targets:
                cutoff = datetime.combine(target + timedelta(days=1), datetime.min.time(), PACIFIC)
                cutoff_utc = cutoff.astimezone(timezone.utc)
                for meta in metadata:
                    if not meta or target < date.fromisoformat(meta.get("baseline_ds", target.isoformat())):
                        continue
                    status = meta.get("baseline_status", "project created")
                    name = meta["dim_project_name"]
                    project_facts = sorted(
                        (row for row in facts if str(row.get("id_project")) == str(meta["id_project"])),
                        key=lambda row: row.get("ts", ""))
                    for fact in project_facts:
                        try:
                            fact_time = datetime.fromisoformat(fact["ts"].replace("Z", "+00:00"))
                        except (KeyError, ValueError):
                            continue
                        if fact_time >= cutoff_utc:
                            break
                        status = fact["status_post"]
                        name = fact.get("dim_project_name") or name
                    if status == "project permanently deleted":
                        continue
                    rows.append({"id": f"{meta['id_project']}-{target.isoformat()}",
                                 "id_project": meta["id_project"], "dim_project_name": name,
                                 "ds": target.isoformat(), "dim_status": status})
            _write_csv(self.snapshots_path, SNAPSHOT_FIELDS,
                       sorted(existing + rows, key=lambda row: (row["ds"], int(row["id_project"]))))
            self.state_path.write_text(json.dumps({"last_snapshot_ds": final_target.isoformat()}, indent=2) + "\n",
                                       encoding="utf-8")
        return final_target.isoformat()

    def _append_fact_locked(self, meta: dict, pre: str, post: str, moment: datetime) -> None:
        rows = _read_csv(self.facts_path)
        timestamp = format_ts(moment)
        base = f"{meta['id_project']}-{timestamp}"
        used = {row["id"] for row in rows}
        identifier = base
        suffix = 2
        while identifier in used:
            identifier = f"{base}-{suffix}"; suffix += 1
        rows.append({"id": identifier, "id_project": meta["id_project"],
                     "dim_project_name": meta["dim_project_name"], "status_pre": pre,
                     "status_post": post, "ds": pacific_ds(moment), "ts": timestamp})
        _write_csv(self.facts_path, FACT_FIELDS, rows)

    def _refresh_project_row_locked(self, meta: dict, permanently_deleted=None) -> None:
        rows = _read_csv(self.projects_path)
        for row in rows:
            if str(row["id_project"]) == str(meta["id_project"]):
                row["dim_project_name"] = meta["dim_project_name"]
                row["project_slug"] = meta["project_slug"]
                if permanently_deleted is not None:
                    row["ts_permanently_deleted"] = permanently_deleted
                break
        _write_csv(self.projects_path, PROJECT_FIELDS, rows)

    def _ensure_meta_locked(self, project: Path) -> dict:
        rows = _read_csv(self.projects_path)
        identifier = max((int(row["id_project"]) for row in rows), default=0) + 1
        match = TRASH_PATTERN.fullmatch(project.name)
        slug = match.group("slug") if match else logical_slug(project)
        created = datetime.fromtimestamp(
            getattr(project.stat(), "st_birthtime", project.stat().st_ctime), timezone.utc)
        status = "project soft deleted" if match else effective_status(project)
        meta = {"id_project": identifier, "dim_project_name": project_name(project),
                "project_slug": slug, "ts_created": format_ts(created),
                "latest_status": status, "baseline_ds": pacific_ds(utc_now()),
                "baseline_status": status}
        self._write_meta(project, meta)
        rows.append({"id_project": identifier, "dim_project_name": meta["dim_project_name"],
                     "project_slug": slug, "ts_created": meta["ts_created"],
                     "ts_permanently_deleted": ""})
        _write_csv(self.projects_path, PROJECT_FIELDS,
                   sorted(rows, key=lambda row: int(row["id_project"])))
        return meta

    @staticmethod
    def _read_meta(project: Path) -> dict:
        try:
            value = json.loads((project / META_FILE).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and "id_project" in value else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_meta(project: Path, value: dict) -> None:
        target = project / META_FILE
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)


def install_launch_agent(repo_root: Path, executable: Path) -> Path:
    label = "com.trailprint.analytics"
    target = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "data/analytics/internal").mkdir(parents=True, exist_ok=True)
    payload = {"Label": label,
               "ProgramArguments": [str(executable), "analytics-refresh", "--repo-root", str(repo_root)],
               "WorkingDirectory": str(repo_root), "RunAtLoad": True,
               "StartCalendarInterval": {"Hour": 0, "Minute": 5},
               "StandardOutPath": str(repo_root / "data/analytics/internal/launchd.log"),
               "StandardErrorPath": str(repo_root / "data/analytics/internal/launchd-error.log")}
    target.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)],
                   capture_output=True, check=False)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "launchctl could not install the analytics schedule.")
    return target
