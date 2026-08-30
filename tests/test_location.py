import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trailprint.location import detect_location, has_location_cache, read_location, save_location

GPX = b'''<gpx><trk><trkseg>
<trkpt lat="19.4200" lon="-155.2880"><ele>1000</ele></trkpt>
<trkpt lat="19.4210" lon="-155.2870"><ele>1001</ele></trkpt>
</trkseg></trk></gpx>'''


class LocationTests(unittest.TestCase):
    def test_detects_fallback_locality_and_caches_result(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "route.gpx").write_bytes(GPX)
            response = {"address": {"village": "Volcano", "state": "Hawaii",
                                    "country": "United States"}}
            with patch("trailprint.location._request_json", return_value=response), \
                    patch("trailprint.location._LAST_REQUEST", 0.0):
                location = detect_location(project)
            self.assertEqual(location, {"city": "Volcano", "state": "Hawaii",
                                        "country": "USA"})
            self.assertTrue(has_location_cache(project))
            self.assertEqual(read_location(project), location)
            metadata = json.loads((project / "location.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["location"]["latitude"], 19.42)
            self.assertEqual(metadata["location"]["source"], "OpenStreetMap Nominatim")

    def test_manual_values_are_trimmed_and_invalid_cache_is_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            saved = save_location(project, " Volcano ", " Hawaii ", " US ")
            self.assertEqual(saved, {"city": "Volcano", "state": "Hawaii", "country": "USA"})
            (project / "location.json").write_text("bad", encoding="utf-8")
            self.assertEqual(read_location(project), {"city": "", "state": "", "country": ""})
            self.assertFalse(has_location_cache(project))


if __name__ == "__main__":
    unittest.main()
