import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh
from shapely.geometry import Point, box

from trailprint.blender_bridge import BuildSettings, run_blender_build
from trailprint.water_geometry import (
    _material_above_floor,
    _stitch_collinear_boundaries,
    _validate_wall_clearance,
    export_groove_cutter,
    generation_trail_corridor,
    prepare_integrated_water,
    projected_footprint,
    trail_corridor,
    validate_trail_fit,
)


def extrusion(polygon, bottom=3, height=1):
    mesh = trimesh.creation.extrude_polygon(polygon, height, engine="earcut")
    mesh.apply_translation((0, 0, bottom))
    return mesh


def model(outer_gap=.3, inner_gap=.3, water=True):
    opening = box(-1, -1, 1, 1)
    outer = box(-4, -4, 4, 4)
    route = outer.difference(opening)
    trail = extrusion(route)
    floor = trimesh.creation.box((20, 20, 3))
    floor.apply_translation((0, 0, 1.5))
    walls = box(-10, -10, 10, 10).difference(outer.buffer(outer_gap, join_style=2))
    island = opening.buffer(-inner_gap)
    terrain = trimesh.util.concatenate((floor, extrusion(walls), extrusion(island)))
    cap = extrusion(island, bottom=3.6, height=.4) if water else None
    return terrain, trail, cap


@pytest.mark.parametrize("gap,valid", [(.11, True), (.096, True), (.094, False), (.04, False)])
def test_outer_wall_threshold_and_numerical_tolerance(gap, valid):
    terrain, trail, water = model(outer_gap=gap)
    report = _validate_wall_clearance(terrain, trail, water, .1)
    assert report["valid"] == valid
    outer = next(b for b in report["boundaries"] if b["type"] == "outer")
    assert outer["measured_clearance_mm"] == pytest.approx(gap, abs=1e-6)
    assert outer["tightest_xy_mm"] is not None


@pytest.mark.parametrize("gap,valid", [(.26, True), (.246, True), (.244, False), (.114, False)])
def test_small_water_opening_uses_stricter_clearance(gap, valid):
    terrain, trail, water = model(inner_gap=gap)
    report = _validate_wall_clearance(terrain, trail, water, .1)
    assert report["valid"] == valid
    inner = next(b for b in report["boundaries"] if b["type"] == "inner")
    assert inner["required_clearance_mm"] == .25
    assert inner["small_water_opening"]


def test_ordinary_opening_without_water_uses_nominal_clearance():
    terrain, trail, water = model(inner_gap=.11, water=False)
    report = _validate_wall_clearance(terrain, trail, water, .1)
    assert report["valid"]
    assert all(b["required_clearance_mm"] == .1 for b in report["boundaries"])


def test_large_water_opening_uses_nominal_clearance():
    terrain, trail, _ = model(inner_gap=.11, water=False)
    trail.apply_scale((2, 2, 1))
    terrain.apply_scale((2, 2, 1))
    cap = extrusion(box(-1.78, -1.78, 1.78, 1.78), bottom=3.6, height=.4)
    report = _validate_wall_clearance(terrain, trail, cap, .1)
    assert report["valid"]
    inner = next(b for b in report["boundaries"] if b["type"] == "inner")
    assert inner["opening_area_mm2"] == pytest.approx(16)
    assert inner["required_clearance_mm"] == .1


def test_disconnected_routes_are_checked_independently():
    terrain, trail, water = model()
    second = trail.copy()
    second.apply_translation((30, 0, 0))
    terrain2 = terrain.copy()
    terrain2.apply_translation((30, 0, 0))
    report = _validate_wall_clearance(trimesh.util.concatenate((terrain, terrain2)),
                                     trimesh.util.concatenate((trail, second)), water, .1)
    assert report["valid"]
    assert {b["component"] for b in report["boundaries"]} == {1, 2}


def test_floor_is_not_mistaken_for_a_wall_and_unreadable_geometry_fails():
    terrain, trail, water = model()
    report = _validate_wall_clearance(terrain, trail, water, .1)
    assert report["valid"]
    broken = terrain.copy()
    broken.vertices[0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        _validate_wall_clearance(broken, trail, water, .1)


def test_exported_geometry_uses_the_same_gate():
    with tempfile.TemporaryDirectory() as temp:
        paths = [Path(temp) / name for name in ("terrain.stl", "trail.stl", "water.stl")]
        for mesh, path in zip(model(inner_gap=.11), paths):
            mesh.export(path)
        report = validate_trail_fit(*paths, clearance_mm=.1)
        assert not report["valid"]
        assert "small enclosed water opening" in report["errors"][0]


def test_failed_fit_preserves_previous_outputs_and_records_location(tmp_path):
    project = tmp_path / "project"
    (project / "deliverables").mkdir(parents=True)
    (project / "route.gpx").write_text(
        '<gpx><trk><trkseg><trkpt lat="1" lon="2"/>'
        '<trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>')
    old = project / "deliverables/previous.stl"
    old.write_bytes(b"keep this")
    blender = tmp_path / "blender"
    blender.touch()

    def generate(command, **kwargs):
        config = json.loads(Path(command[-1]).read_text())
        if "cutter_path" in config:
            import shutil
            shutil.copy2(config["terrain_path"], config["output_path"])
            return subprocess.CompletedProcess(command, 0, "uncorrected", "")
        terrain, trail, _ = model(outer_gap=.04, water=False)
        terrain.export(Path(config["export_path"]) / "project.stl")
        trail.subdivide().export(Path(config["export_path"]) / "project_Trail.stl")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with patch("trailprint.blender_bridge.subprocess.run", side_effect=generate):
        with pytest.raises(ValueError, match="outer wall"):
            run_blender_build(project, BuildSettings(), blender=blender)
    assert old.read_bytes() == b"keep this"
    report = json.loads((project / "build/failed-validation.json").read_text())
    assert not report["valid"]
    assert report["trail_fit"]["wall_clearance"]["boundaries"][0]["tightest_xy_mm"]
    assert not list((project / "build").glob("trailprint3d-*"))


def test_fallback_cannot_bypass_failed_wall_clearance(tmp_path):
    project = tmp_path / "project"
    (project / "deliverables").mkdir(parents=True)
    (project / "route.gpx").write_text(
        '<gpx><trk><trkseg><trkpt lat="1" lon="2"/>'
        '<trkpt lat="1.01" lon="2.01"/></trkseg></trk></gpx>')
    old = project / "deliverables/previous.stl"
    old.write_bytes(b"preserved")
    blender = tmp_path / "blender"
    blender.touch()
    attempts = []

    def generate(command, **kwargs):
        config = json.loads(Path(command[-1]).read_text())
        if "cutter_path" in config:
            import shutil
            shutil.copy2(config["terrain_path"], config["output_path"])
            return subprocess.CompletedProcess(command, 0, "uncorrected", "")
        attempts.append(config["shape_extra"])
        output = Path(config["export_path"])
        if config["shape_extra"] == "NONE":
            terrain, trail, _ = model(outer_gap=.04, water=False)
        else:
            terrain = trimesh.creation.box((100, 80, 5))
            first = trimesh.creation.box((20, 3, 2))
            second = first.copy()
            second.apply_translation((40, 0, 0))
            trail = trimesh.util.concatenate((first, second))
        terrain.export(output / "project.stl")
        trail.export(output / "project_Trail.stl")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with patch("trailprint.blender_bridge.subprocess.run", side_effect=generate):
        with pytest.raises(ValueError, match="Fallback: Trail fit validation failed"):
            run_blender_build(project, BuildSettings(shape_extra="OUTER TEXT"), blender=blender)
    report = json.loads((project / "build/failed-validation.json").read_text())
    assert attempts == ["OUTER TEXT", "NONE"]
    assert not report["fallback"]["trail_fit"]["valid"]
    assert old.read_bytes() == b"preserved"


def test_sloping_faces_are_clipped_at_the_floor_tolerance():
    mesh = trimesh.Trimesh(vertices=((0, 0, 0), (1, 0, .1), (0, 1, .1)),
                           faces=((0, 1, 2),), process=False)
    material = _material_above_floor(mesh, 0)
    assert material.area == pytest.approx(.375)
    assert material.distance(box(0, 0, .1, .1)) > 0


def test_tiny_wall_protrusion_is_not_lost_between_samples():
    terrain, trail, water = model()
    protrusion = extrusion(box(4.04, .1234, 4.35, .1236))
    report = _validate_wall_clearance(trimesh.util.concatenate((terrain, protrusion)),
                                     trail, water, .1)
    assert not report["valid"]
    outer = next(b for b in report["boundaries"] if b["type"] == "outer")
    assert outer["measured_clearance_mm"] == pytest.approx(.04)
    assert .1234 <= outer["tightest_xy_mm"][1] <= .1236


def test_floor_only_facets_do_not_create_false_walls():
    terrain, trail, water = model()
    contact = trimesh.Trimesh(vertices=((-9, -9, 3), (9, -9, 3), (0, 9, 3)),
                             faces=((0, 1, 2),), process=False)
    report = _validate_wall_clearance(terrain, trimesh.util.concatenate((trail, contact)),
                                     water, .1)
    assert report["valid"]
    assert len(report["boundaries"]) == 2


@pytest.mark.parametrize("water,expected", [(False, .12), (True, .27)])
def test_generation_corridor_uses_actual_walls_and_small_water_policy(tmp_path, water, expected):
    terrain, trail, cap = model()
    trail_path = tmp_path / "trail.stl"
    trail.export(trail_path)
    waters = []
    if water:
        cap.export(tmp_path / "water.stl")
        waters = [tmp_path / "water.stl"]
    protected, report = generation_trail_corridor(trail_path, waters)
    remaining = box(-1, -1, 1, 1).difference(protected)
    assert remaining.distance(Point(-1, 0)) == pytest.approx(expected, abs=.001)
    assert protected.bounds[0] == pytest.approx(-4.12)
    assert bool(report["small_water_openings"]) == water


def test_floor_contact_facets_do_not_expand_generation_corridor(tmp_path):
    _, trail, _ = model()
    facet = trimesh.Trimesh(vertices=[(6, 0, 3), (9, 0, 3), (7, 3, 3)],
                            faces=[(0, 1, 2)], process=False)
    path = tmp_path / "trail.stl"
    trimesh.util.concatenate((trail, facet)).export(path)
    assert not trail_corridor(path, .1).covers(Point(7, 1))
    protected, _ = generation_trail_corridor(path)
    assert not protected.covers(Point(7, 1))


def test_exported_cutter_preserves_floor_and_clearance_at_corners(tmp_path):
    _, trail, _ = model()
    path = tmp_path / "trail.stl"
    trail.export(path)
    protected, report = generation_trail_corridor(path)
    cutter = tmp_path / "cutter.stl"
    export_groove_cutter(protected, report["floor_z_mm"], 8, cutter)
    mesh = trimesh.load_mesh(cutter, process=True)
    assert mesh.is_watertight
    assert mesh.bounds[0, 2] == pytest.approx(3)
    assert mesh.bounds[1, 2] == pytest.approx(8)
    boundary = projected_footprint(mesh).boundary
    assert boundary.distance(Point(-4, -4)) == pytest.approx(.12, abs=.001)


def test_unprintable_water_is_omitted_without_modifying_terrain(tmp_path):
    terrain = tmp_path / "terrain.stl"
    trail = tmp_path / "trail.stl"
    water = tmp_path / "water.stl"
    extrusion(box(-10, -10, 10, 10), bottom=0, height=4).export(terrain)
    opening = box(-.7, -.7, .7, .7)
    extrusion(box(-4, -4, 4, 4).difference(opening)).export(trail)
    extrusion(opening, bottom=3.6, height=.4).export(water)
    original = terrain.read_bytes()
    protected, _ = generation_trail_corridor(trail, [water])
    report = prepare_integrated_water(terrain, [water], tmp_path / "cap.stl",
                                     tmp_path / "cutter.stl", trail_path=trail,
                                     trail_clearance_mm=.1, protected_corridor=protected)
    assert not report["available"]
    assert report["omitted_fragments"][0]["replacement"] == "existing terrain"
    assert terrain.read_bytes() == original


def test_correction_is_bounded_and_never_rewrites_trail(tmp_path):
    from trailprint.blender_bridge import _correct_staged_groove

    terrain, trail, _ = model(outer_gap=.04, inner_gap=.04, water=False)
    terrain.export(tmp_path / "project.stl")
    trail.export(tmp_path / "project_Trail.stl")
    original = hashlib.sha256((tmp_path / "project_Trail.stl").read_bytes()).hexdigest()
    calls = []

    def no_effect(command, **kwargs):
        import shutil
        job = json.loads(Path(command[-1]).read_text())
        calls.append(job)
        shutil.copy2(job["terrain_path"], job["output_path"])
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with patch("trailprint.blender_bridge.subprocess.run", side_effect=no_effect):
        _correct_staged_groove(tmp_path, "project", Path("blender"), 60)
    assert len(calls) == 2
    assert hashlib.sha256((tmp_path / "project_Trail.stl").read_bytes()).hexdigest() == original
    report = json.loads((tmp_path / "groove-correction.json").read_text())
    assert not report["after"]["valid"]
    assert report["after"]["wall_clearance"]["boundaries"][0]["required_clearance_mm"] == .1



def test_stitch_repairs_multiple_collinear_vertices_without_capping_holes():
    mesh = trimesh.creation.box((2, 2, 2))
    original_volume = mesh.volume
    a, b, c = mesh.faces[0]
    vertices = np.vstack((mesh.vertices, (2 * mesh.vertices[a] + mesh.vertices[b]) / 3,
                          (mesh.vertices[a] + 2 * mesh.vertices[b]) / 3))
    middle1, middle2 = len(vertices) - 2, len(vertices) - 1
    faces = np.vstack((mesh.faces[1:], ((a, middle1, c),
                                       (middle1, middle2, c), (middle2, b, c))))
    broken = trimesh.Trimesh(vertices, faces, process=True)
    assert not broken.is_watertight
    repaired, count = _stitch_collinear_boundaries(broken)
    assert count == 1
    assert repaired.is_watertight
    assert repaired.volume == pytest.approx(original_volume)
    hole = trimesh.creation.box((2, 2, 2))
    hole.update_faces(np.arange(len(hole.faces)) != 0)
    unrepaired, count = _stitch_collinear_boundaries(hole)
    assert count == 0
    assert not unrepaired.is_watertight



def test_disconnected_routes_receive_only_their_local_water_clearance(tmp_path):
    _, trail, cap = model()
    other = trail.copy()
    other.apply_translation((12, 0, 0))
    trail_path = tmp_path / "trail.stl"
    water_path = tmp_path / "water.stl"
    trimesh.util.concatenate((trail, other)).export(trail_path)
    cap.export(water_path)
    protected, report = generation_trail_corridor(trail_path, [water_path])
    assert len(report["small_water_openings"]) == 1
    first = box(-1, -1, 1, 1).difference(protected)
    second = box(11, -1, 13, 1).difference(protected)
    assert first.distance(Point(-1, 0)) == pytest.approx(.27, abs=.001)
    assert second.distance(Point(11, 0)) == pytest.approx(.12, abs=.001)



def test_stitch_aligns_both_sides_with_different_edge_subdivisions():
    mesh = trimesh.creation.box((2, 2, 2))
    a, b, c = mesh.faces[0]
    neighboring = next(index for index, face in enumerate(mesh.faces[1:], 1)
                       if a in face and b in face)
    vertices = np.vstack((mesh.vertices, (2 * mesh.vertices[a] + mesh.vertices[b]) / 3,
                          (mesh.vertices[a] + 2 * mesh.vertices[b]) / 3))
    first, second = len(vertices) - 2, len(vertices) - 1
    faces = []
    for index, face in enumerate(mesh.faces):
        point = first if index == 0 else second
        if index not in (0, neighboring):
            faces.append(face)
            continue
        for position in range(3):
            x, y, z = face[position], face[(position + 1) % 3], face[(position + 2) % 3]
            if {x, y} == {a, b}:
                faces.extend(((x, point, z), (point, y, z)))
                break
    broken = trimesh.Trimesh(vertices, faces, process=True)
    assert not broken.is_watertight
    repaired, count = _stitch_collinear_boundaries(broken)
    assert count >= 2
    assert repaired.is_watertight
    assert repaired.volume == pytest.approx(mesh.volume)



def test_stitch_repairs_straight_seams_meeting_at_a_bend():
    mesh = trimesh.creation.box((2, 2, 2))
    a, b, c = mesh.faces[0]
    vertices = np.vstack((mesh.vertices, (mesh.vertices[a] + mesh.vertices[b]) / 2,
                          (mesh.vertices[b] + mesh.vertices[c]) / 2))
    first, second = len(vertices) - 2, len(vertices) - 1
    faces = np.vstack((mesh.faces[1:], ((a, first, c), (first, second, c),
                                       (first, b, second))))
    broken = trimesh.Trimesh(vertices, faces, process=True)
    assert not broken.is_watertight
    repaired, count = _stitch_collinear_boundaries(broken)
    assert count >= 2
    assert repaired.is_watertight
    assert repaired.volume == pytest.approx(mesh.volume)
