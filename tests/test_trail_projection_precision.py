"""Order-sensitive GEOS overlays must not disable trail fit validation."""
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh
from shapely.errors import GEOSException
from shapely.geometry import Polygon, box

from trailprint.water_geometry import (
    TRAIL_FIT_AREA_TOLERANCE_MM2,
    _material_above_floor,
    _union_projected_material,
    _validate_wall_clearance,
)


def test_captured_ring_edge_failure_and_bounded_retry():
    fixture = Path(__file__).parent / 'fixtures/trail-projection-ring-edge.npz'
    with np.load(fixture, allow_pickle=False) as data:
        mesh = trimesh.Trimesh(data['vertices'], data['faces'], process=False)
        floor = float(data['floor_z'])
    vertices, faces = mesh.vertices.copy(), mesh.faces.copy()

    def legacy(pieces, _mesh):
        from shapely.ops import unary_union
        return unary_union(pieces).buffer(0)

    with patch('trailprint.water_geometry._union_projected_material', side_effect=legacy):
        with pytest.raises(GEOSException, match='Ring edge missing'):
            _material_above_floor(mesh, floor)
    projected = _material_above_floor(mesh, floor)
    assert projected.is_valid and not projected.is_empty
    report = mesh.metadata['projection_precision_correction']
    assert report['grid_mm'] == 1e-9
    assert report['area_error_bound_mm2'] < TRAIL_FIT_AREA_TOLERANCE_MM2
    assert report['maximum_coordinate_displacement_mm'] < 2e-9
    np.testing.assert_array_equal(mesh.vertices, vertices)
    np.testing.assert_array_equal(mesh.faces, faces)
    reordered = trimesh.Trimesh(vertices, faces[::-1], process=False)
    other = _material_above_floor(reordered, floor)
    assert projected.symmetric_difference(other).area < report['area_error_bound_mm2']


def test_normal_projection_does_not_snap():
    mesh = trimesh.creation.box()
    pieces = [box(0, 0, 1.0000000001, 1)]
    result = _union_projected_material(pieces, mesh)
    assert result.equals(pieces[0])
    assert set(result.exterior.coords) == set(pieces[0].exterior.coords)
    assert 'projection_precision_correction' not in mesh.metadata


@pytest.mark.parametrize('pieces, message', [
    ([Polygon([(0, 0), (1, 1), (0, 1), (1, 0)])], 'invalid input'),
    ([box(0, 0, 1e9, 1e9)], 'area budget'),
])
def test_unsafe_retries_are_blocked(pieces, message):
    with patch('trailprint.water_geometry.unary_union', side_effect=GEOSException('overlay')):
        with pytest.raises(ValueError, match=message):
            _union_projected_material(pieces, trimesh.creation.box())


def test_failed_retry_still_blocks():
    with patch('trailprint.water_geometry.unary_union', side_effect=GEOSException('original')):
        with patch('trailprint.water_geometry.union_all', side_effect=GEOSException('retry')):
            with pytest.raises(GEOSException, match='retry'):
                _union_projected_material([box(0, 0, 1, 1)], trimesh.creation.box())


def test_repaired_projection_still_rejects_insufficient_clearance():
    terrain = trimesh.creation.extrude_polygon(box(5.09, -1, 7, 1), 3, engine='earcut')
    trail = trimesh.creation.extrude_polygon(box(-5, -1, 5, 1), 2, engine='earcut')

    def fallback(pieces, mesh):
        with patch('trailprint.water_geometry.unary_union', side_effect=GEOSException('overlay')):
            return _union_projected_material(pieces, mesh)

    with patch('trailprint.water_geometry._union_projected_material', side_effect=fallback):
        report = _validate_wall_clearance(terrain, trail, None, .1)
    assert not report['valid']
    assert report['minimum_clearance_mm'] == pytest.approx(.09)
    assert report['boundaries'][0]['required_clearance_mm'] == .1
    assert {c['role'] for c in report['projection_precision_corrections']} == {'terrain', 'trail'}
