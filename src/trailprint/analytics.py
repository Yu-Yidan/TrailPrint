"""Local, normalized CSV analytics for projects and product lines."""

import csv
import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .naming import human_name, logical_slug, read_project_meta
from .projects import TRASH_PATTERN

PACIFIC = ZoneInfo("America/Los_Angeles")
PROJECT_SCOPE = "project"
RAISED_MAP = "fridge_magnet_raised_map"
PRODUCT_TYPES = (RAISED_MAP,)
STATUS_ROWS = (
    (PROJECT_SCOPE, 1, "project created"),
    (PROJECT_SCOPE, 2, "project soft deleted"),
    (PROJECT_SCOPE, 3, "project permanently deleted"),
    (RAISED_MAP, 1, "product created"),
    (RAISED_MAP, 2, "stl generated"),
    (RAISED_MAP, 3, "3mf generated"),
    (RAISED_MAP, 4, "print started"),
    (RAISED_MAP, 5, "print completed"),
)
PROJECT_FIELDS = ("id_project", "dim_project_name", "project_slug", "dim_is_test",
                  "ts_created", "ts_permanently_deleted")
PRODUCT_FIELDS = ("id_product", "id_project", "product_type", "dim_product_name",
                  "ds_created", "ds_archived")
STATUS_FIELDS = ("status_scope", "id_status", "dim_status")
PROJECT_FACT_FIELDS = ("id", "id_project", "id_status_pre", "id_status_post", "ds", "ts")
PROJECT_SNAPSHOT_FIELDS = ("id", "id_project", "ds", "id_status")
PRODUCT_FACT_FIELDS = ("id", "id_product", "id_status_pre", "id_status_post", "ds", "ts")
PRODUCT_SNAPSHOT_FIELDS = ("id", "id_product", "ds", "id_status")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pacific_ds(moment: datetime) -> str:
    return moment.astimezone(PACIFIC).date().isoformat()


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
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _headers(path: Path) -> tuple:
    if not path.is_file():
        return ()
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple(next(csv.reader(handle), ()))


class AnalyticsStore:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.root = self.repo_root / "data/analytics"
        self.internal = self.root / "internal"
        self.lock_path = self.internal / "analytics.lock"
        self.projects_path = self.root / "dim_project.csv"
        self.products_path = self.root / "dim_product.csv"
        self.status_path = self.root / "dim_status.csv"
        self.project_snapshots_path = self.root / "dim_project_status_snapshot.csv"
        self.product_snapshots_path = self.root / "dim_product_status_snapshot.csv"
        self.project_facts_path = self.root / "fct_project_status.csv"
        self.product_facts_path = self.root / "fct_product_status.csv"
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
        expected = {
            self.projects_path: PROJECT_FIELDS, self.products_path: PRODUCT_FIELDS,
            self.status_path: STATUS_FIELDS,
            self.project_snapshots_path: PROJECT_SNAPSHOT_FIELDS,
            self.product_snapshots_path: PRODUCT_SNAPSHOT_FIELDS,
            self.project_facts_path: PROJECT_FACT_FIELDS,
            self.product_facts_path: PRODUCT_FACT_FIELDS,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.locked():
            mismatch = any(_headers(path) != fields for path, fields in expected.items())
            if mismatch:
                active = [path for path in prints_dir.iterdir()
                          if path.is_dir() and not path.name.startswith(".")] if prints_dir.is_dir() else []
                deleted = [path for path in trash_dir.iterdir()
                           if path.is_dir() and TRASH_PATTERN.fullmatch(path.name)] if trash_dir.is_dir() else []
                if active or deleted:
                    raise ValueError("Analytics schema reset requires empty project and trash folders.")
                for path, fields in expected.items():
                    rows = ({"status_scope": scope, "id_status": identifier,
                             "dim_status": name}
                            for scope, identifier, name in STATUS_ROWS) if path == self.status_path else []
                    _write_csv(path, fields, rows)
                (self.root / "dim_project_status.csv").unlink(missing_ok=True)
                self.state_path.unlink(missing_ok=True)

    def status_warnings(self) -> list:
        configured = {(row.get("status_scope"), row.get("id_status"), row.get("dim_status"))
                      for row in _read_csv(self.status_path)}
        return [f"{scope}:{identifier} {name}" for scope, identifier, name in STATUS_ROWS
                if (scope, str(identifier), name) not in configured]

    def next_project_id(self) -> int:
        with self.locked():
            return max((int(row["id_project"]) for row in _read_csv(self.projects_path)),
                       default=0) + 1

    def register_project(self, project: Path, name: str, moment: datetime = None,
                         is_test: bool = False) -> dict:
        moment = moment or utc_now()
        with self.locked():
            rows = _read_csv(self.projects_path)
            identifier = max((int(row["id_project"]) for row in rows), default=0) + 1
            meta = {"id_project": identifier, "dim_project_name": name.strip(),
                    "project_slug": logical_slug(project), "dim_is_test": bool(is_test),
                    "ts_created": format_ts(moment), "latest_project_status": 1}
            self._write_meta(project, meta)
            rows.append({"id_project": identifier, "dim_project_name": name.strip(),
                         "project_slug": meta["project_slug"],
                         "dim_is_test": "1" if is_test else "0",
                         "ts_created": meta["ts_created"], "ts_permanently_deleted": ""})
            _write_csv(self.projects_path, PROJECT_FIELDS, rows)
            self._append_fact_locked(self.project_facts_path, PROJECT_FACT_FIELDS,
                                     identifier, "", 1, moment)
            return meta

    def ensure_product(self, project: Path, product_type: str,
                       moment: datetime = None) -> dict:
        if product_type not in PRODUCT_TYPES:
            raise ValueError("Unknown product type.")
        moment = moment or utc_now()
        if not self._read_meta(project).get("id_project"):
            self.register_project(project, human_name(project), moment)
        with self.locked():
            meta = self._read_meta(project)
            rows = _read_csv(self.products_path)
            existing = next((row for row in rows
                             if row["id_project"] == str(meta["id_project"])
                             and row["product_type"] == product_type
                             and not row.get("ds_archived")), None)
            if existing:
                return existing
            identifier = max((int(row["id_product"]) for row in rows), default=0) + 1
            product = {"id_product": identifier, "id_project": meta["id_project"],
                       "product_type": product_type, "dim_product_name": product_type,
                       "ds_created": pacific_ds(moment), "ds_archived": ""}
            rows.append(product)
            _write_csv(self.products_path, PRODUCT_FIELDS, rows)
            directory = project / "products" / product_type
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "product.json").write_text(
                json.dumps({**product, "schema_version": 1}, indent=2) + "\n",
                encoding="utf-8")
            self._append_fact_locked(self.product_facts_path, PRODUCT_FACT_FIELDS,
                                     identifier, "", 1, moment)
            return product

    def transition(self, project: Path, post: str, moment: datetime = None) -> None:
        mapping = {name: identifier for scope, identifier, name in STATUS_ROWS
                   if scope == PROJECT_SCOPE}
        if post not in mapping:
            raise ValueError("Invalid project analytics status.")
        moment = moment or utc_now()
        with self.locked():
            meta = self._read_meta(project)
            if not meta.get("id_project"):
                return
            pre, post_id = int(meta.get("latest_project_status", 1)), mapping[post]
            if pre != post_id:
                self._append_fact_locked(self.project_facts_path, PROJECT_FACT_FIELDS,
                                         meta["id_project"], pre, post_id, moment)
            meta["latest_project_status"] = post_id
            self._write_meta(project, meta)

    def record_build_requested(self, project: Path, product_type: str,
                               build_id: str, settings: dict,
                               moment: datetime = None) -> None:
        """Persist immutable input metadata for a formal product build."""
        moment = moment or utc_now()
        product = self.ensure_product(project, product_type, moment)
        directory = project / "products" / product_type / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_json(directory / f"{build_id}.json", {
            "schema_version": 1, "id_build": build_id,
            "id_product": int(product["id_product"]),
            "product_type": product_type, "state": "queued",
            "ts_requested": format_ts(moment), "settings": settings,
        })

    def record_build_result(self, project: Path, product_type: str, build_id: str,
                            state: str, message: str, outputs: list,
                            validation: dict, started_at=None,
                            finished_at=None) -> None:
        """Update one run outcome and point current.json at successful output."""
        directory = project / "products" / product_type
        path = directory / "runs" / f"{build_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        value.update({
            "state": state, "message": message, "outputs": list(outputs),
            "validation": validation,
            "ts_started": (format_ts(datetime.fromtimestamp(started_at, timezone.utc))
                           if started_at else None),
            "ts_finished": (format_ts(datetime.fromtimestamp(finished_at, timezone.utc))
                            if finished_at else None),
        })
        self._atomic_json(path, value)
        if state == "success":
            self._atomic_json(directory / "current.json", {
                "schema_version": 1, "id_build": build_id,
                "artifact_root": "../../../deliverables",
                "preview": "../../../build/preview.png",
                "validation": "../../../build/validation.json",
            })

    def sync_project(self, project: Path, moment: datetime = None) -> bool:
        """Synchronize names and the formal raised-map product lifecycle."""
        moment = moment or utc_now()
        meta = self._read_meta(project)
        if not meta.get("id_project"):
            return False
        changed = False
        with self.locked():
            projects = _read_csv(self.projects_path)
            for row in projects:
                if row["id_project"] == str(meta["id_project"]):
                    latest_name = human_name(project)
                    if row["dim_project_name"] != latest_name:
                        row["dim_project_name"] = latest_name
                        changed = True
                    break
            if changed:
                _write_csv(self.projects_path, PROJECT_FIELDS, projects)
        products = _read_csv(self.products_path)
        product = next((row for row in products
                        if row["id_project"] == str(meta["id_project"])
                        and row["product_type"] == RAISED_MAP
                        and not row.get("ds_archived")), None)
        if not product:
            return changed
        workflow = self._workflow(project)
        deliverables = project / "deliverables"
        if workflow == "completed":
            target = 5
        elif workflow == "printing":
            target = 4
        elif (deliverables / f"{project.name}.3mf").is_file():
            target = 3
        elif deliverables.is_dir() and any(deliverables.glob("*.stl")):
            target = 2
        else:
            target = 1
        return self._transition_product(int(product["id_product"]), target, moment) or changed

    def mark_permanently_deleted(self, project: Path, moment: datetime = None) -> None:
        moment = moment or utc_now()
        self.transition(project, "project permanently deleted", moment)
        meta = self._read_meta(project)
        if not meta.get("id_project"):
            return
        with self.locked():
            rows = _read_csv(self.projects_path)
            for row in rows:
                if row["id_project"] == str(meta["id_project"]):
                    row["ts_permanently_deleted"] = format_ts(moment)
            _write_csv(self.projects_path, PROJECT_FIELDS, rows)
            products = _read_csv(self.products_path)
            for row in products:
                if row["id_project"] == str(meta["id_project"]) and not row.get("ds_archived"):
                    row["ds_archived"] = pacific_ds(moment)
            _write_csv(self.products_path, PRODUCT_FIELDS, products)

    def visualization_data(self, prints_dir: Path = None, trash_dir: Path = None,
                           include_tests: bool = False) -> dict:
        projects = _read_csv(self.projects_path)
        visible = {row["id_project"] for row in projects
                   if include_tests or row.get("dim_is_test") != "1"}
        excluded = sum(row.get("dim_is_test") == "1" for row in projects)
        daily = Counter()
        for row in projects:
            if row["id_project"] not in visible:
                continue
            try:
                daily[datetime.fromisoformat(row["ts_created"].replace("Z", "+00:00"))
                      .astimezone(PACIFIC).date()] += 1
            except (KeyError, ValueError):
                pass
        products = [row for row in _read_csv(self.products_path)
                    if row["id_project"] in visible]
        latest = self._latest_product_statuses()
        definitions = defaultdict(list)
        for row in _read_csv(self.status_path):
            if row["status_scope"] in PRODUCT_TYPES:
                definitions[row["status_scope"]].append(row)
        status_rows = []
        for product_type in PRODUCT_TYPES:
            relevant = [row for row in products if row["product_type"] == product_type]
            active = [row for row in relevant if not row.get("ds_archived")]
            counts = Counter(latest.get(row["id_product"], 1) for row in active)
            stages = [{"id_status": int(row["id_status"]), "label": row["dim_status"],
                       "count": counts[int(row["id_status"])]}
                      for row in sorted(definitions[product_type],
                                        key=lambda item: int(item["id_status"]))]
            status_rows.append({"product_type": product_type, "dim_product_name": product_type,
                                "total": len(relevant), "archived": len(relevant) - len(active),
                                "stages": stages})
        created = defaultdict(Counter)
        for row in products:
            try:
                created[row["product_type"]][date.fromisoformat(row["ds_created"])] += 1
            except (KeyError, ValueError):
                pass
        dates = sorted({day for values in created.values() for day in values})
        cumulative = []
        for product_type in PRODUCT_TYPES:
            running, points = 0, []
            for day in dates:
                running += created[product_type][day]
                points.append({"date": day.isoformat(), "count": running})
            cumulative.append({"product_type": product_type,
                               "dim_product_name": product_type, "points": points})
        return {"total_projects": len(visible), "excluded_test_projects": excluded,
                "include_tests": include_tests,
                "project_created_daily": self._daily_points(daily),
                "product_status_rows": status_rows,
                "product_created_cumulative": cumulative}

    def run_daily(self, prints_dir: Path, trash_dir: Path,
                  snapshot_date: date = None, moment: datetime = None) -> str:
        moment = moment or utc_now()
        self.initialize(prints_dir, trash_dir)
        if prints_dir.is_dir():
            for project in prints_dir.iterdir():
                if project.is_dir() and not project.name.startswith("."):
                    self.sync_project(project, moment)
        target = snapshot_date or (moment.astimezone(PACIFIC).date() - timedelta(days=1))
        ds = target.isoformat()
        latest_products = self._latest_product_statuses()
        project_rows = []
        for row in _read_csv(self.projects_path):
            if row.get("ts_permanently_deleted"):
                continue
            meta = self._find_project_meta(prints_dir, trash_dir, row["id_project"])
            status = int(meta.get("latest_project_status", 1)) if meta else 1
            project_rows.append({"id": f"{row['id_project']}-{ds}",
                                 "id_project": row["id_project"], "ds": ds,
                                 "id_status": status})
        product_rows = [{"id": f"{row['id_product']}-{ds}",
                         "id_product": row["id_product"], "ds": ds,
                         "id_status": latest_products.get(row["id_product"], 1)}
                        for row in _read_csv(self.products_path)
                        if not row.get("ds_archived")]
        with self.locked():
            prior_projects = [row for row in _read_csv(self.project_snapshots_path)
                              if row.get("ds") != ds]
            prior_products = [row for row in _read_csv(self.product_snapshots_path)
                              if row.get("ds") != ds]
            _write_csv(self.project_snapshots_path, PROJECT_SNAPSHOT_FIELDS,
                       prior_projects + project_rows)
            _write_csv(self.product_snapshots_path, PRODUCT_SNAPSHOT_FIELDS,
                       prior_products + product_rows)
            self.state_path.write_text(json.dumps({"last_snapshot_ds": ds}, indent=2) + "\n",
                                       encoding="utf-8")
        return ds

    def _transition_product(self, product_id: int, post: int, moment: datetime) -> bool:
        with self.locked():
            latest = self._latest_product_statuses().get(str(product_id), 1)
            if latest == post:
                return False
            self._append_fact_locked(self.product_facts_path, PRODUCT_FACT_FIELDS,
                                     product_id, latest, post, moment)
            return True

    def _latest_product_statuses(self) -> dict:
        latest = {}
        for row in _read_csv(self.product_facts_path):
            latest[row["id_product"]] = int(row["id_status_post"])
        return latest

    @staticmethod
    def _daily_points(counts: Counter) -> list:
        if not counts:
            return []
        cursor, final = min(counts), max(counts)
        output = []
        while cursor <= final:
            output.append({"date": cursor.isoformat(), "count": counts[cursor]})
            cursor += timedelta(days=1)
        return output

    @staticmethod
    def _workflow(project: Path) -> str:
        try:
            return json.loads((project / "workflow.json").read_text())["status"]
        except (OSError, ValueError, KeyError):
            return ""

    @staticmethod
    def _read_meta(project: Path) -> dict:
        return read_project_meta(project)

    @staticmethod
    def _write_meta(project: Path, value: dict) -> None:
        target = project / "project.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    @staticmethod
    def _atomic_json(target: Path, value: dict) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    @staticmethod
    def _append_fact_locked(path: Path, fields, entity_id, pre, post,
                            moment: datetime) -> None:
        rows = _read_csv(path)
        timestamp = format_ts(moment)
        identifier = base = f"{entity_id}-{timestamp}"
        suffix, used = 2, {row["id"] for row in rows}
        while identifier in used:
            identifier = f"{base}-{suffix}"
            suffix += 1
        entity = "id_product" if "id_product" in fields else "id_project"
        rows.append({"id": identifier, entity: entity_id,
                     "id_status_pre": pre, "id_status_post": post,
                     "ds": pacific_ds(moment), "ts": timestamp})
        _write_csv(path, fields, rows)

    @staticmethod
    def _find_project_meta(prints_dir: Path, trash_dir: Path, identifier: str) -> dict:
        for root in (prints_dir, trash_dir):
            if root and root.is_dir():
                for path in root.iterdir():
                    if path.is_dir():
                        meta = read_project_meta(path)
                        if str(meta.get("id_project")) == str(identifier):
                            return meta
        return {}


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
