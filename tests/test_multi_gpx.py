import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import trimesh

from trailprint.blender_bridge import _prefix_staged_outputs
from trailprint.gpx import (
    TripStats,
    combine_gpx,
    format_trip_stats,
    read_gpx_segments,
    read_gpx_trip_stats,
)
from trailprint.mesh_validation import inspect_stl
from trailprint.projects import MAX_GPX_BYTES, create_project_from_uploads


def recording(lat=0, start="2026-08-01T10:00:00Z", end="2026-08-01T11:00:00Z"):
    return (f'<gpx><trk><trkseg><trkpt lat="{lat}" lon="0"><ele>100</ele>'
            f'<time>{start}</time></trkpt><trkpt lat="{lat}" lon="0.01"><ele>150</ele>'
            f'<time>{end}</time></trkpt></trkseg></trk></gpx>').encode()


class MultiGpxTests(unittest.TestCase):
    def test_joined_trail_filename_is_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp) / "stage"
            stage.mkdir()
            (stage / "triathlon_Trail.002.stl").write_bytes(b"geometry")
            _prefix_staged_outputs(Path(temp) / "triathlon", stage)
            self.assertEqual((stage / "triathlon_Trail.stl").read_bytes(), b"geometry")

    def test_summed_minutes_do_not_lose_a_minute_to_float_rounding(self):
        stats = TripStats(duration_hours=20 / 60 + 20 / 60 + 30 / 60)
        self.assertEqual(format_trip_stats(stats)["duration"], "1h 10m")

    def test_combined_stats_exclude_geographic_and_time_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "route.gpx"
            p.write_bytes(combine_gpx([
                ("bike.gpx", recording(1, "2026-08-01T14:00:00Z", "2026-08-01T15:00:00Z")),
                ("swim.gpx", recording()),
            ]))
            self.assertEqual(len(read_gpx_segments(p)), 2)
            stats = read_gpx_trip_stats(p)
            self.assertAlmostEqual(stats.distance_km, 2.2237, places=3)
            self.assertEqual(stats.elevation_gain_m, 100)
            self.assertEqual(stats.duration_hours, 2)
            self.assertEqual(stats.date, "2026.08.01")

    def test_missing_or_reversed_timestamps_hide_total_duration(self):
        for end in ("", "2026-08-01T09:00:00Z"):
            with self.subTest(end=end), tempfile.TemporaryDirectory() as temp:
                p = Path(temp) / "route.gpx"
                p.write_bytes(combine_gpx([("a.gpx", recording()),
                                          ("b.gpx", recording(1, end=end))]))
                self.assertEqual(read_gpx_trip_stats(p).duration_hours, 0)

    def test_route_namespaces_and_extensions_are_preserved(self):
        for namespace in ("", "http://www.topografix.com/GPX/1/0",
                          "http://www.topografix.com/GPX/1/1"):
            with self.subTest(namespace=namespace):
                data = (f'<gpx xmlns="{namespace}" xmlns:x="urn:test"><rte>'
                        '<rtept lat="0" lon="0"><extensions><x:value>9</x:value></extensions></rtept>'
                        '<rtept lat="0" lon="1"/></rte></gpx>').encode()
                root = ET.fromstring(combine_gpx([("route.gpx", data)]))
                self.assertEqual(len(root.findall(".//{http://www.topografix.com/GPX/1/1}trkpt")), 2)
                self.assertEqual(root.find(".//{urn:test}value").text, "9")

    def test_segments_within_one_file_remain_separate(self):
        source = ET.fromstring(recording())
        source.find("trk").append(ET.fromstring(recording(1)).find("trk/trkseg"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "route.gpx"
            path.write_bytes(combine_gpx([("segmented.gpx", ET.tostring(source))]))
            self.assertEqual(len(read_gpx_segments(path)), 2)
            self.assertLess(read_gpx_trip_stats(path).distance_km, 3)

    def test_originals_duplicate_names_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            sources = [("../../route.gpx", recording()), ("../../route.gpx", recording(1)),
                       ("游泳.gpx", recording(2))]
            project = create_project_from_uploads(Path(temp), "Triathlon", sources)
            manifest = json.loads((project / "gpx_sources.json").read_text())
            self.assertEqual([item["filename"] for item in manifest],
                             ["route.gpx", "route.gpx", "游泳.gpx"])
            self.assertNotEqual(manifest[0]["path"], manifest[1]["path"])
            for item, (_, original) in zip(manifest, sources):
                self.assertEqual((project / item["path"]).read_bytes(), original)

    def test_invalid_later_file_creates_no_partial_project(self):
        for bad in (b"broken", b"<gpx/>", b'<gpx><trk><trkseg><trkpt lat="nan" lon="0"/>'
                    b'<trkpt lat="0" lon="1"/></trkseg></trk></gpx>'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                with self.assertRaisesRegex(ValueError, "bad.gpx"):
                    create_project_from_uploads(Path(temp), "Triathlon",
                                                [("good.gpx", recording()), ("bad.gpx", bad)])
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_combined_upload_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "Combined GPX"):
                create_project_from_uploads(Path(temp), "Large",
                                            [("a.gpx", b" " * MAX_GPX_BYTES), ("b.gpx", b"a")])

    def test_intentional_disconnected_trails_require_segment_context(self):
        with tempfile.TemporaryDirectory() as temp:
            first = trimesh.creation.box((10, 1, 1))
            second = first.copy()
            second.apply_translation((0, 5, 0))
            path = Path(temp) / "test_Trail.stl"
            trimesh.util.concatenate([first, second]).export(path)
            self.assertFalse(inspect_stl(path, "test")["valid"])
            self.assertTrue(inspect_stl(path, "test", expected_trail_segments=2)["valid"])
            third = first.copy()
            third.apply_translation((0, 10, 0))
            trimesh.util.concatenate([first, second, third]).export(path)
            self.assertFalse(inspect_stl(path, "test", expected_trail_segments=2)["valid"])


class MultiGpxWebTests(unittest.TestCase):
    def test_uploads_are_all_saved_and_displayed(self):
        from trailprint.web import create_app

        with tempfile.TemporaryDirectory() as temp:
            app = create_app(Path(temp))
            app.config["TESTING"] = True
            client = app.test_client()
            response = client.post("/projects", data={"name": "Triathlon", "gpx": [
                (io.BytesIO(recording()), "swim.gpx"),
                (io.BytesIO(recording(1)), "bike.gpx"),
            ]}, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 302)
            project = Path(temp) / "prints/0001-triathlon"
            self.assertEqual(len(read_gpx_segments(project / "route.gpx")), 2)
            page = client.get(response.headers["Location"])
            self.assertIn(b"swim.gpx", page.data)
            self.assertIn(b"bike.gpx", page.data)
            self.assertIn(b"2 GPX file(s)", page.data)

    def test_bad_upload_reports_filename(self):
        from trailprint.web import create_app

        with tempfile.TemporaryDirectory() as temp:
            app = create_app(Path(temp))
            app.config["TESTING"] = True
            response = app.test_client().post("/projects", data={"name": "Broken", "gpx": [
                (io.BytesIO(recording()), "good.gpx"),
                (io.BytesIO(b"broken"), "bad.gpx"),
            ]}, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"bad.gpx", response.data)
            self.assertFalse((Path(temp) / "prints/0001-broken").exists())
