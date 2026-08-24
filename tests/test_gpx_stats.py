import tempfile
import unittest
from pathlib import Path

from trailprint.gpx import format_trip_stats, read_gpx_trip_stats


class GpxStatsTests(unittest.TestCase):
    def test_literal_stats_and_unit_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            route = Path(temp) / "route.gpx"
            route.write_text(
                '<gpx><trk><trkseg><trkpt lat="0" lon="0"><ele>100</ele>'
                '<time>2026-08-01T10:00:00Z</time></trkpt>'
                '<trkpt lat="0" lon="0.01"><ele>150</ele>'
                '<time>2026-08-01T12:30:00Z</time></trkpt></trkseg></trk></gpx>',
                encoding="utf-8")
            stats = read_gpx_trip_stats(route)
            imperial = format_trip_stats(stats, "IMPERIAL")
            metric = format_trip_stats(stats, "METRIC")
            self.assertEqual(metric["date"], "2026.08.01")
            self.assertEqual(metric["duration"], "2h 30m")
            self.assertEqual(metric["elevation"], "50 m")
            self.assertEqual(imperial["elevation"], "164 ft")
            self.assertTrue(imperial["length"].endswith(" mi"))
            self.assertTrue(metric["speed"].endswith(" km/h"))

    def test_missing_timestamps_leave_date_and_duration_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            route = Path(temp) / "route.gpx"
            route.write_text(
                '<gpx><trk><trkseg><trkpt lat="0" lon="0"/>'
                '<trkpt lat="0" lon="0.01"/></trkseg></trk></gpx>', encoding="utf-8")
            values = format_trip_stats(read_gpx_trip_stats(route))
            self.assertEqual(values["date"], "")
            self.assertEqual(values["duration"], "")


if __name__ == "__main__":
    unittest.main()
