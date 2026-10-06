import unittest

from trailprint.title import (
    measure_title_width_mm,
    readable_project_name,
    resolve_project_title,
    title_limit_mm,
    validate_title_fit,
)


class TitleTests(unittest.TestCase):
    def test_readable_name_and_token_resolution(self):
        self.assertEqual(readable_project_name("kilauea-iki-trail"), "Kilauea Iki Trail")
        self.assertEqual(resolve_project_title("Map: {name}", "coastal-run"),
                         "Map: Coastal Run")
        self.assertEqual(resolve_project_title("  Harding Icefield  ", "unused"),
                         "Harding Icefield")

    def test_width_uses_font_metrics_instead_of_character_count(self):
        self.assertGreater(measure_title_width_mm("WWWWWWWWWW"),
                           measure_title_width_mm("iiiiiiiiii") * 2)

    def test_layout_limits_and_overflow(self):
        self.assertEqual(title_limit_mm("HEXAGON", "INNER TEXT"), 45.0)
        self.assertEqual(title_limit_mm("HEXAGON", "OUTER TEXT"), 60.5)
        self.assertEqual(title_limit_mm("OCTAGON", "OUTER TEXT"), 41.3)
        self.assertEqual(title_limit_mm("CIRCLE", "OUTER TEXT"), 77.8)
        self.assertIsNone(title_limit_mm("HEXAGON", "NONE"))
        self.assertLess(validate_title_fit("Short", "HEXAGON", "OUTER TEXT"), 60.5)
        self.assertLess(validate_title_fit("Harding Icefield", "HEXAGON", "OUTER TEXT"), 60.5)
        self.assertLess(validate_title_fit("Harding Icefield", "HEXAGON", "FRONT TEXT"), 60.5)
        with self.assertRaisesRegex(ValueError, "Shorten the title"):
            validate_title_fit("W" * 15, "HEXAGON", "OUTER TEXT")
        validate_title_fit("A Very Long Trail Name", "HEXAGON", "NONE")

    def test_grindstone_matches_generated_title_width(self):
        width = validate_title_fit("UTMB Grindstone", "HEXAGON", "OUTER TEXT")
        # Actual installed-generator mesh is 53.48 mm; advance width includes
        # glyph bearings and remains a slightly conservative estimate.
        self.assertGreaterEqual(width, 53.48)
        self.assertLess(width, 54.5)

    def test_titles_on_either_side_of_hexagon_limit(self):
        self.assertLess(validate_title_fit("W" * 10, "HEXAGON", "OUTER TEXT"), 60.5)
        with self.assertRaisesRegex(ValueError, "Shorten the title"):
            validate_title_fit("W" * 11, "HEXAGON", "OUTER TEXT")


if __name__ == "__main__":
    unittest.main()
