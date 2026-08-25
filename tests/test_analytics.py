import csv
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from trailprint.analytics import AnalyticsStore, STATUS_ROWS


def rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def make_project(root, slug, name):
    project = root / "prints" / slug
    (project / "deliverables").mkdir(parents=True)
    (project / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return project


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prints").mkdir()
        self.store = AnalyticsStore(self.root)
        self.store.initialize(self.root / "prints", self.root / ".trailprint-trash")

    def tearDown(self):
        self.temp.cleanup()

    def test_registers_sequential_ids_and_creation_facts(self):
        first = make_project(self.root, "first", "First Trail")
        second = make_project(self.root, "second", "Second Trail")
        moment = datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc)

        first_meta = self.store.register_project(first, "First Trail", moment)
        second_meta = self.store.register_project(second, "Second Trail", moment)

        self.assertEqual((first_meta["id_project"], second_meta["id_project"]), (1, 2))
        self.assertEqual([row["dim_project_name"] for row in rows(self.store.projects_path)],
                         ["First Trail", "Second Trail"])
        facts = rows(self.store.facts_path)
        self.assertEqual(facts[0]["status_pre"], "")
        self.assertEqual(facts[0]["status_post"], "project created")
        self.assertEqual(facts[0]["ts"], "2026-08-23T20:00:00Z")
        self.assertEqual(facts[1]["id"], "2-2026-08-23T20:00:00Z")

    def test_fact_ids_get_short_collision_suffix(self):
        project = make_project(self.root, "route", "Route")
        moment = datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc)
        self.store.register_project(project, "Route", moment)
        (project / "deliverables/route.stl").write_bytes(b"stl")
        self.store.sync_project(project, moment)

        identifiers = [row["id"] for row in rows(self.store.facts_path)]
        self.assertEqual(identifiers, ["1-2026-08-23T20:00:00Z",
                                       "1-2026-08-23T20:00:00Z-2"])

    def test_snapshot_replays_events_through_pacific_day_end(self):
        project = make_project(self.root, "route", "Route")
        created = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)
        self.store.register_project(project, "Route", created)
        (project / "deliverables/route.stl").write_bytes(b"stl")
        self.store.sync_project(project, datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc))
        (project / "deliverables/route.3mf").write_bytes(b"3mf")

        self.store.run_daily(
            self.root / "prints", self.root / ".trailprint-trash",
            snapshot_date=date(2026, 8, 23),
            moment=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc))

        snapshot = rows(self.store.snapshots_path)
        self.assertEqual(snapshot, [{"id": "1-2026-08-23", "id_project": "1",
                                     "dim_project_name": "Route", "ds": "2026-08-23",
                                     "dim_status": "stl generated"}])
        self.assertEqual(rows(self.store.facts_path)[-1]["status_post"], "3mf generated")

    def test_permanently_deleted_same_day_is_absent_from_snapshot(self):
        project = make_project(self.root, "short-lived", "Short Lived")
        moment = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)
        self.store.register_project(project, "Short Lived", moment)
        self.store.mark_permanently_deleted(
            project, datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc))
        shutil.rmtree(project)

        self.store.run_daily(
            self.root / "prints", self.root / ".trailprint-trash",
            snapshot_date=date(2026, 8, 23),
            moment=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc))

        self.assertEqual(rows(self.store.snapshots_path), [])
        self.assertEqual(rows(self.store.facts_path)[-1]["status_post"],
                         "project permanently deleted")
        self.assertEqual(rows(self.store.projects_path)[0]["ts_permanently_deleted"],
                         "2026-08-23T19:00:00Z")

    def test_dim_status_is_seeded_once_and_missing_values_warn(self):
        self.assertEqual([(int(row["id"]), row["dim_status"])
                          for row in rows(self.store.status_path)], list(STATUS_ROWS))
        self.store.status_path.write_text(
            "id,dim_status\n1,project created\n", encoding="utf-8")
        warnings = self.store.status_warnings()
        self.assertIn("stl generated", warnings)
        self.store.initialize(self.root / "prints", self.root / ".trailprint-trash")
        self.assertEqual(rows(self.store.status_path),
                         [{"id": "1", "dim_status": "project created"}])

    def test_visualization_counts_statuses_and_pacific_creation_days(self):
        first = make_project(self.root, "first", "First")
        second = make_project(self.root, "second", "Second")
        self.store.register_project(
            first, "First", datetime(2026, 8, 24, 6, 30, tzinfo=timezone.utc))
        self.store.register_project(
            second, "Second", datetime(2026, 8, 25, 7, 30, tzinfo=timezone.utc))
        (first / "deliverables/first.stl").write_bytes(b"stl")
        self.store.sync_project(
            first, datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc))
        self.store.mark_permanently_deleted(
            second, datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc))
        shutil.rmtree(second)

        data = self.store.visualization_data(
            self.root / "prints", self.root / ".trailprint-trash")

        counts = {item["status"]: item["count"] for item in data["status_counts"]}
        self.assertEqual(len(data["status_counts"]), 7)
        self.assertNotIn("max_status_count", data)
        self.assertEqual(counts["stl generated"], 1)
        self.assertEqual(counts["project permanently deleted"], 1)
        self.assertEqual(sum(counts.values()), data["total_projects"])
        self.assertEqual(data["created_daily"], [
            {"date": "2026-08-23", "count": 1},
            {"date": "2026-08-24", "count": 0},
            {"date": "2026-08-25", "count": 1},
        ])

    def test_visualization_empty_store(self):
        data = self.store.visualization_data(
            self.root / "prints", self.root / ".trailprint-trash")
        self.assertEqual(data["total_projects"], 0)
        self.assertEqual(data["created_daily"], [])
        self.assertEqual([item["count"] for item in data["status_counts"]], [0] * 7)

    def test_visualization_ignores_malformed_creation_timestamp(self):
        with self.store.projects_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=(
                "id_project", "dim_project_name", "project_slug", "ts_created",
                "ts_permanently_deleted"))
            writer.writerow({"id_project": 1, "dim_project_name": "Broken Date",
                             "project_slug": "broken-date", "ts_created": "not-a-date",
                             "ts_permanently_deleted": ""})
        data = self.store.visualization_data()
        self.assertEqual(data["total_projects"], 1)
        self.assertEqual(data["created_daily"], [])
        self.assertEqual(data["status_counts"][0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
