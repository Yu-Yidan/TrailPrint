import json
import tempfile
import unittest
from pathlib import Path

from trailprint.naming import (
    customer_preview_name,
    format_project_id,
    human_name,
    logical_slug,
    storage_name,
)
from trailprint.projects import migrate_active_project


class ProjectNamingTests(unittest.TestCase):
    def test_customer_preview_name_preserves_readable_safe_title(self):
        self.assertEqual(
            customer_preview_name("  Mānoa   Falls  "),
            "Mānoa Falls-preview.png")
        self.assertEqual(
            customer_preview_name('Alaska: North/West?*'),
            "Alaska- North-West-preview.png")
        self.assertEqual(customer_preview_name("", "Evening Ride"),
                         "Evening Ride-preview.png")

    def test_customer_preview_name_limits_utf8_filename_length(self):
        name = customer_preview_name("山" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), 240)
        self.assertTrue(name.endswith("-preview.png"))

    def test_integer_ids_are_padded_only_for_storage(self):
        self.assertEqual(format_project_id(1), "0001")
        self.assertEqual(format_project_id("16"), "0016")
        self.assertEqual(storage_name(9999, "last-project"), "9999-last-project")
        for value in (0, 10000, "not-an-id", 1.5):
            with self.assertRaises(ValueError):
                format_project_id(value)

    def test_metadata_separates_storage_slug_and_human_name(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "0016-ruoxi-alaska"
            project.mkdir()
            (project / "project_analytics.json").write_text(json.dumps({
                "id_project": 16, "project_slug": "ruoxi-alaska",
                "dim_project_name": "Ruoxi Alaska",
            }), encoding="utf-8")
            self.assertEqual(logical_slug(project), "ruoxi-alaska")
            self.assertEqual(human_name(project), "Ruoxi Alaska")

    def test_active_migration_prefixes_folder_and_deliverables_once(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "route"
            deliverables = project / "deliverables"
            deliverables.mkdir(parents=True)
            (project / "project_analytics.json").write_text(json.dumps({
                "id_project": 7, "project_slug": "route", "dim_project_name": "Route",
            }), encoding="utf-8")
            (deliverables / "route.stl").write_bytes(b"stl")
            (deliverables / "Plate.stl").write_bytes(b"plate")
            (deliverables / "route.3mf").write_bytes(b"3mf")

            migrated = migrate_active_project(project)
            migrated_again = migrate_active_project(migrated)

            self.assertEqual(migrated_again.name, "0007-route")
            self.assertEqual(
                sorted(path.name for path in (migrated / "deliverables").iterdir()),
                ["0007-Plate.stl", "0007-route.3mf", "0007-route.stl"])


if __name__ == "__main__":
    unittest.main()
