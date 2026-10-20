"""Captured curve-export debris must not weaken real fragmentation checks."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh

from trailprint.mesh_validation import (
    _component_face_groups,
    cleanup_fragmented_trails,
    inspect_stl,
)

FIXTURE = Path(__file__).parent / 'fixtures/fragmented-trail-artifacts.npz'


def captured(path):
    with np.load(FIXTURE, allow_pickle=False) as data:
        trimesh.Trimesh(vertices=data['vertices'], faces=data['faces'], process=False).export(path)
    return trimesh.load_mesh(path, process=True)


def triangles(mesh):
    return {tuple(triangle.flatten()) for triangle in mesh.triangles}


def test_captured_cleanup_preserves_main_trail_and_is_idempotent():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'route_Trail.stl'
        original = captured(path)
        assert inspect_stl(path, 'route')['components'] == 749
        main = original.triangles[_component_face_groups(original)[0]]
        report, = cleanup_fragmented_trails([path], 'route')
        fixed = trimesh.load_mesh(path, process=True)
        assert report['removed_components'] == 745
        assert report['removed_faces'] == 1164
        assert report['after']['valid'] and report['after']['components'] == 1
        assert fixed.is_watertight and fixed.is_winding_consistent
        assert report['bounds_delta_mm'] == report['volume_delta_mm3'] == 0
        assert report['raised_footprint_difference_mm2'] < .01
        assert {tuple(t.flatten()) for t in main} <= triangles(fixed)
        # Every retained positive-area sheet remains, including nonidentical pairs.
        for group in _component_face_groups(original):
            if np.all(original.area_faces[group] == 0):
                continue
            if (len(group) == 2 and np.array_equal(np.sort(original.faces[group[0]]),
                                                  np.sort(original.faces[group[1]]))):
                continue
            assert {tuple(t.flatten()) for t in original.triangles[group]} <= triangles(fixed)
        contents = path.read_bytes()
        assert cleanup_fragmented_trails([path], 'route') == []
        assert path.read_bytes() == contents


def test_genuine_fragments_are_kept_and_multiple_gpx_segments_allowed():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'route_Trail.stl'
        left = trimesh.creation.box().subdivide()
        right = left.copy()
        right.apply_translation([5, 0, 0])
        trimesh.util.concatenate([left, right]).export(path)
        contents = path.read_bytes()
        assert cleanup_fragmented_trails([path], 'route') == []
        assert not inspect_stl(path, 'route')['valid']
        assert inspect_stl(path, 'route', expected_trail_segments=2)['valid']
        assert cleanup_fragmented_trails([path], 'route', 2) == []
        assert path.read_bytes() == contents


def test_failed_footprint_guard_preserves_original():
    from shapely.geometry import box

    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'route_Trail.stl'
        captured(path)
        contents = path.read_bytes()
        with patch('trailprint.water_geometry._material_above_floor',
                   side_effect=[box(0, 0, 1, 1), box(0, 0, 2, 2)]):
            with pytest.raises(ValueError, match='preservation'):
                cleanup_fragmented_trails([path], 'route')
        assert path.read_bytes() == contents
        assert not list(path.parent.glob('.trail-cleanup-*'))
