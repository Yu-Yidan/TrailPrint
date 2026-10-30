import json
from unittest.mock import patch

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon, box

from trailprint.blender_worker import coastal_source_metadata, write_terrain_snapshot
from trailprint.water_geometry import (
    _coastal_source,
    _separated_support_floor,
    prepare_integrated_water,
    projected_footprint,
)


def prism(polygon, bottom, top):
    if polygon.geom_type == "MultiPolygon":
        return trimesh.util.concatenate([prism(p, bottom, top) for p in polygon.geoms])
    mesh = trimesh.creation.extrude_polygon(polygon, top - bottom, engine="earcut")
    mesh.apply_translation([0, 0, bottom])
    return mesh


def metadata(tmp_path, polygons, levels):
    sources = []
    for polygon, level in zip(polygons, levels):
        mesh = prism(polygon, level - .2, level)
        sources.append({"vertices": mesh.vertices.tolist(), "faces": mesh.faces.tolist()})
    path = tmp_path / "coastal-water-source.json"
    path.write_text(json.dumps({"version": 1, "expected": bool(sources), "sources": sources}))
    return path


def prepare(tmp_path, generic, coast, *, trail=None):
    terrain = tmp_path / "terrain.stl"
    prism(box(-20, -20, 20, 20), 0, 4).export(terrain)
    raw = tmp_path / "water.stl"
    prism(generic, 3.5, 4.5).export(raw)
    cap, cutter, support = [tmp_path / f"{name}.stl" for name in ("cap", "cutter", "support")]
    kwargs = {"trail_path": raw, "protected_corridor": trail} if trail is not None else {}
    report = prepare_integrated_water(
        terrain, [raw], cap, cutter, support, coastal_source_path=coast, **kwargs)
    for path in (cap, cutter, support):
        mesh = trimesh.load_mesh(path, process=True)
        assert mesh.is_watertight and mesh.is_winding_consistent
    return report, trimesh.load_mesh(cap, process=True)


def test_generic_bay_uses_sea_level_even_without_surviving_ocean_mesh(tmp_path):
    coast = metadata(tmp_path, [box(-12, -12, 0, 12)], [5])
    report, mesh = prepare(tmp_path, box(-10, -6, -2, 6), coast)
    assert report["body_count"] == 1
    assert report["bodies"][0]["surface_mode"] == "ocean-flat"
    np.testing.assert_allclose(mesh.bounds[:, 2], [4.8, 5], atol=1e-6)
    assert report["coastal_classification"]["reclassified_area_mm2"] == pytest.approx(96)
    np.testing.assert_allclose(
        report["coastal_classification"]["exported_surface_range_mm"], [5, 5])


def test_trail_separated_marine_fragments_share_one_level(tmp_path):
    coast = metadata(tmp_path, [box(-15, -15, 15, 15)], [5])
    report, _ = prepare(tmp_path, box(-10, -5, 10, 5), coast,
                        trail=box(-1, -10, 1, 10))
    assert report["body_count"] == 2
    assert {b["surface_min_z_mm"] for b in report["bodies"]} == {5}
    assert all(b["surface_mode"] == "ocean-flat" for b in report["bodies"])


def test_nearby_lake_and_upstream_river_are_not_flattened(tmp_path):
    coast = metadata(tmp_path, [box(-15, -15, 0, 15)], [5])
    # A river crosses the coastline; a nearby lake is separated by real land.
    generic = box(-10, -5, 8, -2).union(box(.2, 2, 5, 6))
    report, mesh = prepare(tmp_path, generic, coast)
    assert any(b["surface_mode"] == "shoreline-flat" for b in report["bodies"])
    triangles = mesh.triangles[mesh.face_normals[:, 2] > .99]
    inland = triangles[triangles[:, :, 0].min(axis=1) > .1]
    assert len(inland) and inland[:, :, 2].max() < 5
    assert report["coastal_classification"]["reclassified_area_mm2"] == pytest.approx(30)


def test_island_hole_is_retained(tmp_path):
    polygon = Polygon(box(-10, -10, 10, 10).exterior.coords,
                      [box(-2, -2, 2, 2).exterior.coords])
    coast = metadata(tmp_path, [polygon], [5])
    _, mesh = prepare(tmp_path, polygon, coast)
    top = mesh.triangles_center[mesh.face_normals[:, 2] > .99]
    assert not ((np.abs(top[:, 0]) < 1.9) & (np.abs(top[:, 1]) < 1.9)).any()


@pytest.mark.parametrize("failure", ["missing", "version", "empty", "nonfinite"])
def test_invalid_metadata_blocks_generation(tmp_path, failure):
    path = metadata(tmp_path, [box(-5, -5, 5, 5)], [5])
    data = json.loads(path.read_text())
    if failure == "missing":
        path.unlink()
    else:
        if failure == "version":
            data["version"] = 99
        elif failure == "empty":
            data["sources"] = []
        else:
            data["sources"][0]["vertices"][0][0] = float("nan")
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="coastal water metadata"):
        _coastal_source(path, box(-20, -20, 20, 20))


def test_conflicting_sea_planes_are_blocked(tmp_path):
    path = metadata(tmp_path, [box(-10, -5, -1, 5), box(1, -5, 10, 5)], [5, 5.2])
    with pytest.raises(ValueError, match="conflicting coastal sea levels"):
        _coastal_source(path, box(-20, -20, 20, 20))


def test_empty_coastal_metadata_preserves_independent_water(tmp_path):
    path = metadata(tmp_path, [], [])
    report, _ = prepare(tmp_path, box(-5, -5, 5, 5), path)
    assert report["bodies"][0]["surface_mode"] == "shoreline-flat"
    assert not report["coastal_classification"]["available"]


def test_coastal_snapshot_follows_final_assembly_normalization(tmp_path):
    mesh = prism(box(-5, -5, 5, 5), 6.8, 7)
    mesh.apply_translation([200000, -100000, 0])
    sources = [{"vertices": mesh.vertices.tolist(), "faces": mesh.faces.tolist()}]
    normalized = coastal_source_metadata(sources, True, [-200000, 100000, -1.2])
    assert sources[0]["vertices"][0][0] > 100000  # Capture remains untouched.
    path = tmp_path / "source.json"
    path.write_text(json.dumps(normalized))
    coverage, level, _ = _coastal_source(path, box(-20, -20, 20, 20))
    assert coverage.equals(box(-5, -5, 5, 5))
    assert level == pytest.approx(5.8)


def test_overlapping_bay_and_ocean_become_one_surface(tmp_path):
    coast = metadata(tmp_path, [box(-10, -5, 10, 5)], [5])
    terrain, raw, ocean = [tmp_path / name for name in
                           ("terrain.stl", "water.stl", "Ocean.stl")]
    prism(box(-20, -20, 20, 20), 0, 4).export(terrain)
    prism(box(-10, -5, 2, 5), 3.5, 4.5).export(raw)
    prism(box(-2, -5, 10, 5), 4.8, 5).export(ocean)
    cap, cutter = tmp_path / "cap.stl", tmp_path / "cutter.stl"
    report = prepare_integrated_water(terrain, [raw, ocean], cap, cutter,
                                      coastal_source_path=coast)
    mesh = trimesh.load_mesh(cap, process=True)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert report["body_count"] == 1
    assert mesh.volume == pytest.approx(200 * .2, abs=.001)
    assert report["bodies"][0]["surface_min_z_mm"] == 5


def test_unexpected_ocean_without_source_is_blocked(tmp_path):
    coast = metadata(tmp_path, [], [])
    terrain, ocean = tmp_path / "terrain.stl", tmp_path / "Ocean.stl"
    prism(box(-20, -20, 20, 20), 0, 4).export(terrain)
    prism(box(-5, -5, 5, 5), 4.8, 5).export(ocean)
    with pytest.raises(ValueError, match="no original coastal water metadata"):
        prepare_integrated_water(terrain, [ocean], tmp_path / "cap.stl",
                                 tmp_path / "cutter.stl", coastal_source_path=coast)


def test_exported_marine_height_mismatch_is_blocked(tmp_path):
    coast = metadata(tmp_path, [box(-10, -10, 10, 10)], [5])
    original = trimesh.Trimesh.export

    def shifted_cap(mesh, file_obj=None, **kwargs):
        if file_obj == tmp_path / "cap.stl":
            mesh = mesh.copy()
            mesh.apply_translation([0, 0, .2])
        return original(mesh, file_obj=file_obj, **kwargs)

    with patch.object(trimesh.Trimesh, "export", new=shifted_cap):
        with pytest.raises(ValueError, match="disagree on sea level"):
            prepare(tmp_path, box(-5, -5, 5, 5), coast)


def test_priority_clearance_seam_is_closed_in_actual_water_mesh(tmp_path):
    coast = metadata(tmp_path, [box(-10, -5, 10, 5)], [5])
    terrain, raw, ocean = [tmp_path / name for name in
                           ("terrain.stl", "water.stl", "Ocean.stl")]
    prism(box(-20, -20, 20, 20), 0, 4).export(terrain)
    prism(box(-10, -5, -.06, 5), 3.5, 4.5).export(raw)
    prism(box(.06, -5, 10, 5), 4.8, 5).export(ocean)
    cap, cutter = tmp_path / "cap.stl", tmp_path / "cutter.stl"
    report = prepare_integrated_water(terrain, [raw, ocean], cap, cutter,
                                      coastal_source_path=coast)
    water = trimesh.load_mesh(cap, process=True)
    footprint = projected_footprint(water)
    assert water.is_watertight and water.is_winding_consistent
    assert footprint.geom_type == "Polygon"
    assert footprint.covers(box(-.05, -4, .05, 4))
    assert report["body_count"] == 1
    cleanup = report["coastal_classification"]["seam_cleanup"]
    assert cleanup["input_component_count"] == 2
    assert cleanup["output_component_count"] == 1
    assert cleanup["restored_area_mm2"] > 1
    separation = report["coastal_classification"]["support_floor_separation"]
    assert 0 < separation["separation_mm"] <= .000001


def test_same_width_real_land_barrier_is_preserved(tmp_path):
    left, right = box(-10, -5, -.06, 5), box(.06, -5, 10, 5)
    coast = metadata(tmp_path, [left, right], [5, 5])
    report, water = prepare(tmp_path, left.union(right), coast)
    footprint = projected_footprint(water)
    assert not footprint.intersects(box(-.05, -4, .05, 4))
    assert report["body_count"] == 2


def test_seam_cleanup_preserves_island_and_trail_opening(tmp_path):
    island = box(-1, -1, 1, 1)
    coverage = box(-10, -8, 10, 8).difference(island)
    coast = metadata(tmp_path, [coverage], [5])
    generic = box(-10, -8, -.06, 8).union(box(.06, -8, 10, 8)).difference(island)
    trail = box(-10, 3, 10, 4)
    report, water = prepare(tmp_path, generic, coast, trail=trail)
    footprint = projected_footprint(water)
    # The new smoothing may round the island's corners; retain its land core.
    assert footprint.intersection(island.buffer(-.5)).area < 1e-8
    assert footprint.intersection(trail.buffer(-.02)).area < 1e-8
    assert footprint.covers(box(-.04, -6, .04, -3))
    assert all(b["surface_min_z_mm"] == 5 for b in report["bodies"])


@pytest.mark.parametrize("base", [0., 2., float(np.float32(3.8)), -2.])
def test_support_separation_survives_binary_stl_without_changing_water(tmp_path, base):
    floor = _separated_support_floor(base)
    mesh = prism(box(-2, -2, 2, 2), floor, base + 1)
    path = tmp_path / "support.stl"
    mesh.export(path)
    actual = trimesh.load_mesh(path, process=True)
    assert actual.is_watertight and actual.is_winding_consistent
    assert actual.bounds[0, 2] == floor > base
    assert actual.bounds[1, 2] == float(np.float32(base + 1))
    assert floor - base <= .000001


def test_unsafe_support_separation_is_rejected():
    with pytest.raises(ValueError, match="STL precision"):
        _separated_support_floor(100000.)


def test_smoothed_marine_pier_is_not_reintroduced_by_coastal_clipping(tmp_path):
    outline = box(-20, -20, 20, 20)
    land = box(-20, -20, 20, 0).union(box(-.3, 0, .3, 3))
    original_water = outline.difference(land)
    coast = metadata(tmp_path, [original_water], [5])
    report, water = prepare(tmp_path, original_water, coast)
    footprint = projected_footprint(water)
    assert footprint.covers(box(-.1, 1.5, .1, 2))
    assert report["shoreline_smoothing"][0]["removed_land_area_mm2"] > 1
    assert report["coastal_classification"]["exported_surface_range_mm"] == [5, 5]


def test_original_terrain_snapshot_is_normalized_binary_stl(tmp_path):
    original = prism(box(-5, -5, 5, 5), 0, 4)
    original.apply_translation([200000, -100000, 2])
    source = {"vertices": original.vertices.tolist(), "faces": original.faces.tolist()}
    path = tmp_path / "source/terrain.stl"
    write_terrain_snapshot(path, source, [-200000, 100000, -2])
    actual = trimesh.load_mesh(path, process=True)
    assert actual.is_watertight and actual.is_winding_consistent
    np.testing.assert_array_equal(actual.bounds, [[-5, -5, 0], [5, 5, 4]])


def test_removed_inlet_requests_rebuild_and_verifiable_original_heights(tmp_path):
    original_water = box(-10, -10, 10, 0).union(box(-.3, 0, .3, 5))
    coast = metadata(tmp_path, [original_water], [5])
    original, terrain, raw = [tmp_path / name for name in
                              ("original.stl", "terrain.stl", "water.stl")]
    prism(box(-20, -20, 20, 20), 0, 4).export(original)
    prism(box(-20, -20, 20, 20), 0, 4).export(terrain)
    prism(original_water, 3.5, 4.5).export(raw)
    cap, cutter = [tmp_path / name for name in ("cap.stl", "cutter.stl")]
    report = prepare_integrated_water(
        terrain, [raw], cap, cutter, coastal_source_path=coast,
        original_terrain_path=original)
    water = projected_footprint(trimesh.load_mesh(cap, process=True))
    assert not water.intersects(box(-.1, 2, .1, 3))
    assert report["land_restoration"]["available"]
    assert report["land_restoration"]["area_mm2"] > 2
    assert report["land_restoration"]["mode"] == "rebuild-from-original-terrain"
    assert report["land_restoration"]["samples"]
    assert all(p["original_z_mm"] == 4 for p in report["land_restoration"]["samples"])

