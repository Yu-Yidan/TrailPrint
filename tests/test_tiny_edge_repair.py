from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh

from trailprint.water_geometry import (
    _collapse_tiny_overfull_edge,
    _terrain_repair_pass,
    repair_terrain_for_water_boolean,
)


@pytest.fixture(scope="module")
def captured():
    fixture = Path(__file__).parent / "fixtures/crosstown-tiny-edge.npz"
    with np.load(fixture, allow_pickle=False) as data:
        return trimesh.Trimesh(data["vertices"], data["faces"], process=False)


@pytest.fixture(scope="module")
def remaining_edge(captured):
    mesh = captured.copy()
    for _ in range(3):
        mesh, _ = _terrain_repair_pass(mesh)
    return mesh


def test_captured_defect_is_repaired_and_survives_export(captured, tmp_path):
    source, output = tmp_path / "terrain.stl", tmp_path / "repaired.stl"
    captured.export(source)
    report = repair_terrain_for_water_boolean(source, output)
    repaired = trimesh.load_mesh(output, process=True)
    assert repaired.is_watertight and repaired.is_winding_consistent
    np.testing.assert_array_equal(repaired.bounds, captured.bounds)
    assert abs(repaired.volume - captured.volume) < .00001
    assert report["tiny_edge_collapses"] == 1
    assert report["passes"][-1]["remaining_non_manifold_edges"] == 0
    correction, = report["tiny_edge_corrections"]
    assert 0 < correction["edge_length_mm"] < .001
    assert correction["maximum_vertex_displacement_mm"] < .0005
    assert correction["collapsed_triangles"] == 4


def test_larger_overfull_edge_is_preserved(remaining_edge):
    enlarged = remaining_edge.copy()
    enlarged.apply_scale(10)
    repaired, correction = _collapse_tiny_overfull_edge(enlarged)
    assert repaired is enlarged
    assert correction is None


def test_actual_hole_is_not_closed():
    mesh = trimesh.creation.box()
    mesh.update_faces(np.arange(len(mesh.faces)) != 0)
    repaired, correction = _collapse_tiny_overfull_edge(mesh)
    assert repaired is mesh
    assert correction is None


@pytest.mark.parametrize("corruption", ["hole", "volume", "bounds"])
def test_corrupt_round_trip_is_rejected(remaining_edge, corruption):
    candidate, report = _collapse_tiny_overfull_edge(remaining_edge)
    assert report is not None
    if corruption == "hole":
        candidate.update_faces(np.arange(len(candidate.faces)) != 0)
    elif corruption == "volume":
        # Move an interior roof vertex without changing the mesh's bounds.
        bounds = candidate.bounds.copy()
        eligible = np.flatnonzero(
            (np.abs(candidate.vertices[:, 0]) < 5)
            & (np.abs(candidate.vertices[:, 1]) < 5)
            & (candidate.vertices[:, 2] > bounds[0, 2] + .1)
            & (candidate.vertices[:, 2] < bounds[1, 2] - .2))
        assert len(eligible)
        candidate.vertices[eligible[0], 2] += .1
        np.testing.assert_array_equal(candidate.bounds, bounds)
        assert abs(candidate.volume - remaining_edge.volume) > .00001
        assert candidate.is_watertight and candidate.is_winding_consistent
    else:
        candidate.apply_translation([.001, 0, 0])
    with patch("trailprint.water_geometry.trimesh.load_mesh", return_value=candidate):
        repaired, correction = _collapse_tiny_overfull_edge(remaining_edge)
    assert repaired is remaining_edge
    assert correction is None
