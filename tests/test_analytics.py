import csv
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from trailprint.analytics import (
    PRODUCT_FIELDS,
    PROJECT_FIELDS,
    RAISED_MAP,
    STATUS_ROWS,
    AnalyticsStore,
)


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

    def test_clean_schema_and_scoped_statuses(self):
        self.assertEqual(tuple(rows(self.store.projects_path)[0].keys())
                         if rows(self.store.projects_path) else PROJECT_FIELDS, PROJECT_FIELDS)
        self.assertEqual(tuple(rows(self.store.products_path)[0].keys())
                         if rows(self.store.products_path) else PRODUCT_FIELDS, PRODUCT_FIELDS)
        actual = [(row["status_scope"], int(row["id_status"]), row["dim_status"])
                  for row in rows(self.store.status_path)]
        self.assertEqual(actual, list(STATUS_ROWS))
        self.assertEqual(self.store.status_warnings(), [])

    def test_registers_test_dimension_and_normalized_creation_fact(self):
        project = make_project(self.root, "route", "Route")
        moment = datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc)
        meta = self.store.register_project(project, "Route", moment, is_test=True)
        self.assertEqual(meta["id_project"], 1)
        self.assertTrue(meta["dim_is_test"])
        self.assertEqual(rows(self.store.projects_path)[0]["dim_is_test"], "1")
        fact = rows(self.store.project_facts_path)[0]
        self.assertEqual(fact["id_status_pre"], "")
        self.assertEqual(fact["id_status_post"], "1")
        self.assertNotIn("dim_project_name", fact)

    def test_product_created_on_first_build_and_reused(self):
        project = make_project(self.root, "route", "Route")
        self.store.register_project(project, "Route")
        first = self.store.ensure_product(project, RAISED_MAP)
        second = self.store.ensure_product(project, RAISED_MAP)
        self.assertEqual(str(first["id_product"]), str(second["id_product"]))
        self.assertEqual(len(rows(self.store.products_path)), 1)
        self.assertTrue((project / "products" / RAISED_MAP / "product.json").is_file())
        fact = rows(self.store.product_facts_path)[0]
        self.assertEqual(fact["id_status_post"], "1")
        self.assertNotIn("product_type", fact)

    def test_product_status_advances_from_artifacts(self):
        project = make_project(self.root, "route", "Route")
        self.store.register_project(project, "Route")
        product = self.store.ensure_product(project, RAISED_MAP)
        (project / "deliverables/part.stl").write_bytes(b"stl")
        self.store.sync_project(project)
        self.assertEqual(self.store._latest_product_statuses()[str(product["id_product"])], 2)
        (project / f"deliverables/{project.name}.3mf").write_bytes(b"3mf")
        self.store.sync_project(project)
        self.assertEqual(self.store._latest_product_statuses()[str(product["id_product"])], 3)

    def test_test_filter_and_cumulative_products(self):
        production = make_project(self.root, "production", "Production")
        test = make_project(self.root, "test", "Test")
        first_day = datetime(2026, 8, 24, 6, 30, tzinfo=timezone.utc)
        second_day = datetime(2026, 8, 25, 7, 30, tzinfo=timezone.utc)
        self.store.register_project(production, "Production", first_day)
        self.store.register_project(test, "Test", second_day, is_test=True)
        self.store.ensure_product(production, RAISED_MAP, first_day)
        self.store.ensure_product(test, RAISED_MAP, second_day)

        filtered = self.store.visualization_data()
        included = self.store.visualization_data(include_tests=True)
        self.assertEqual(filtered["total_projects"], 1)
        self.assertEqual(filtered["excluded_test_projects"], 1)
        self.assertEqual(included["total_projects"], 2)
        filtered_totals = {row["product_type"]: row["total"]
                           for row in filtered["product_status_rows"]}
        included_totals = {row["product_type"]: row["total"]
                           for row in included["product_status_rows"]}
        self.assertEqual(filtered_totals, {RAISED_MAP: 1})
        self.assertEqual(included_totals, {RAISED_MAP: 2})

    def test_daily_snapshots_are_id_only(self):
        project = make_project(self.root, "route", "Route")
        self.store.register_project(project, "Route")
        product = self.store.ensure_product(project, RAISED_MAP)
        self.store.run_daily(self.root / "prints", self.root / ".trailprint-trash",
                             snapshot_date=date(2026, 8, 25))
        project_snapshot = rows(self.store.project_snapshots_path)[0]
        product_snapshot = rows(self.store.product_snapshots_path)[0]
        self.assertEqual(set(project_snapshot), {"id", "id_project", "ds", "id_status"})
        self.assertEqual(set(product_snapshot), {"id", "id_product", "ds", "id_status"})
        self.assertEqual(product_snapshot["id_product"], str(product["id_product"]))

    def test_permanent_deletion_archives_products(self):
        project = make_project(self.root, "route", "Route")
        self.store.register_project(project, "Route")
        self.store.ensure_product(project, RAISED_MAP)
        moment = datetime(2026, 8, 25, 19, 0, tzinfo=timezone.utc)
        self.store.mark_permanently_deleted(project, moment)
        self.assertEqual(rows(self.store.projects_path)[0]["ts_permanently_deleted"],
                         "2026-08-25T19:00:00Z")
        self.assertEqual(rows(self.store.products_path)[0]["ds_archived"], "2026-08-25")
        shutil.rmtree(project)


if __name__ == "__main__":
    unittest.main()
