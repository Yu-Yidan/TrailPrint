import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh
from shapely import from_wkt, get_precision
from shapely.geometry import box
from shapely.ops import unary_union

from trailprint import water_geometry as water

FIXTURE = Path(__file__).parent / "fixtures/tomales-water-precision.wkt"


def test_actual_tomales_fragment_crossing_minimum_area_is_omitted():
    polygon = from_wkt(FIXTURE.read_text())
    cleanup = {}
    assert polygon.area == pytest.approx(1.0094347101473553)
    assert water._stable_polygon(polygon, cleanup=cleanup) is None
    assert cleanup == pytest.approx({"source_area_mm2": polygon.area,
                                     "cleaned_area_mm2": .9989, "grid_mm": .01})


def test_collapsed_fragment_is_omitted_and_normal_water_is_preserved():
    cleanup = {}
    assert water._stable_polygon(box(0, 0, .001, .001), cleanup) is None
    assert cleanup["cleaned_area_mm2"] == 0
    assert water._stable_polygon(box(0, 0, 2, 2)).equals(box(0, 0, 2, 2))
    assert water._stable_polygon(box(0, 0, 1, 1)).area == 1


def test_unexpected_split_into_printable_bodies_remains_blocking():
    connected = unary_union([box(0, 0, 2, 2), box(3, 0, 5, 2), box(2, .999, 3, 1.001)])
    assert connected.geom_type == "Polygon"
    with pytest.raises(ValueError, match="split a connected body"):
        water._stable_polygon(connected)


@pytest.mark.parametrize("include_normal_body", [False, True])
def test_preparation_reports_omission_and_keeps_existing_terrain(tmp_path, include_normal_body):
    terrain = trimesh.creation.box([100, 100, 4])
    terrain.apply_translation([0, 0, 2])
    terrain_path = tmp_path/"terrain.stl"
    terrain.export(terrain_path)
    terrain_bytes = terrain_path.read_bytes()
    # Exercise the real normalization and integration, substituting only the
    # earlier footprint stages to retain this exact pre-cleanup shoreline.
    polygon = from_wkt(FIXTURE.read_text())
    footprint = polygon.union(box(-4, -4, -2, -2)) if include_normal_body else polygon
    raw_path = tmp_path/"raw.stl"
    terrain.export(raw_path)
    cap, cutter, support = [tmp_path/name for name in ("cap.stl", "cutter.stl", "support.stl")]
    with patch.object(water, "projected_footprint", return_value=footprint), \
            patch.object(water, "_printable_water", side_effect=lambda geometry, _: geometry):
        report = water.prepare_integrated_water(terrain_path, [raw_path], cap, cutter, support)
    assert report["available"] == include_normal_body
    omitted = report["omitted_fragments"]
    assert len(omitted) == 1
    assert omitted[0]["source_area_mm2"] == pytest.approx(polygon.area)
    assert 0 < omitted[0]["cleaned_area_mm2"] < water.WATER_MIN_COMPONENT_AREA_MM2
    assert omitted[0]["reason"] == "below printable area after precision cleanup"
    assert omitted[0]["replacement"] == "existing terrain"
    assert terrain_path.read_bytes() == terrain_bytes
    if include_normal_body:
        assert report["body_count"] == 1
        for path in (cap, cutter, support):
            mesh = trimesh.load_mesh(path, process=True)
            assert mesh.is_watertight and mesh.is_winding_consistent
    else:
        assert not any(path.exists() for path in (cap, cutter, support))


def test_ocean_overlap_keeps_inland_geometry_levels_and_printable_remainder():
    inland = box(0, 0, 4, 4)
    ocean = box(3, 0, 7, 4)
    samples = np.array([[0, 0, 6], [4, 0, 6], [0, 4, 6]])
    omitted, corrections = [], []
    # Order does not decide precedence: the inland shoreline must be retained.
    result = water._normalized_water_entries(
        [(ocean, 5.8, .2, None), (inland, None, .4, samples)], omitted, corrections, True)
    assert len(result) == 2
    ocean_part, inland_part = result
    assert inland_part[1].equals(inland)
    assert inland_part[2:] == (None, .4, samples)
    assert ocean_part[2:] == (5.8, .2, None)
    assert ocean_part[1].difference(ocean).area == 0
    assert ocean_part[1].intersection(inland).is_empty
    assert ocean_part[1].distance(inland) == pytest.approx(.001)
    assert get_precision(ocean_part[1]) == 0
    assert corrections[0]["overlap_area_mm2"] == 4
    assert corrections[0]["resulting_body_count"] == 1
    assert not omitted


def test_covered_ocean_and_small_remainders_are_reported():
    omitted, corrections = [], []
    result = water._normalized_water_entries(
        [(box(0, 0, 4, 4), None, .4, None),
         (box(1, 1, 3, 3), 5.8, .2, None),
         (box(3, 1, 4.1, 2.1), 5.8, .2, None)], omitted, corrections, True)
    assert len(result) == 1
    assert len(corrections) == 2
    assert all(c["resulting_body_count"] == 0 for c in corrections)
    assert len(omitted) == 1
    assert omitted[0]["area_mm2"] < water.WATER_MIN_COMPONENT_AREA_MM2


def test_nonoverlapping_water_is_unchanged_and_boundary_contact_gets_separation():
    omitted, corrections = [], []
    result = water._normalized_water_entries(
        [(box(0, 0, 2, 2), None, .4, None),
         (box(3, 0, 5, 2), 5.8, .2, None)], omitted, corrections)
    assert result[1][1].equals(box(3, 0, 5, 2))
    assert not corrections and not omitted
    touching = water._normalized_water_entries(
        [(box(0, 0, 2, 2), None, .4, None),
         (box(2, 0, 4, 2), 5.8, .2, None)], [], corrections)
    assert touching[1][1].distance(touching[0][1]) == pytest.approx(.001)
    assert corrections[0]["overlap_area_mm2"] == 0


def test_actual_tomales_overlapping_floors_remain_watertight_after_cleanup(tmp_path):
    fixture = json.loads((FIXTURE.parent/"tomales-water-overlap.json").read_text())
    inland, ocean = [from_wkt(fixture[k]) for k in ("inland", "ocean")]
    samples = np.array([[0, 0, 6], [1, 0, 6], [0, 1, 6]])
    entries = [(inland, None, .4, samples),
               (ocean, fixture["ocean_level_mm"], fixture["ocean_depth_mm"], None)]

    class Flat:
        def z(self, _point):
            return 6.

    def shells(values):
        parts = [water._water_body_meshes(p, Flat(), s, level, depth, 9, 0)[0]
                 for _, p, level, depth, s in values]
        assert all(water._water_shells_valid(part) for part in parts)
        return tuple(trimesh.util.concatenate([p[i] for p in parts]) for i in range(3))

    original = shells([(i, *entry) for i, entry in enumerate(entries)])
    assert not water._water_shells_valid(original)
    omitted, corrections = [], []
    normalized = water._normalized_water_entries(entries, omitted, corrections, True)
    assert normalized[0][1].equals(water._stable_polygon(inland))
    assert corrections and corrections[0]["retained_water"] == "inland"
    corrected = shells(normalized)
    assert water._water_shells_valid(corrected)
    for name, mesh in zip(("cap", "cutter", "support"), corrected):
        path = tmp_path/f"{name}.stl"
        mesh.export(path)
        actual = trimesh.load_mesh(path, process=True)
        assert actual.is_watertight and actual.is_winding_consistent
