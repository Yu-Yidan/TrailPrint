"""Avoid coplanar cutter floors without capping holes in Boolean output."""
from pathlib import Path

import numpy as np
import pytest
import trimesh
from shapely.geometry import box

from trailprint.water_geometry import (
    TerrainRepairError,
    export_groove_cutter,
    repair_terrain_for_water_boolean,
)


@pytest.mark.parametrize('floor', [0., 2., 5.064769744873047, 15.123456, -2.])
def test_cutter_floor_has_minimal_representable_separation(tmp_path, floor):
    path = tmp_path / 'cutter.stl'
    report = export_groove_cutter(box(-3, -3, 3, 3), floor, 20, path)
    mesh = trimesh.load_mesh(path, process=True)
    expected = float(np.nextafter(np.float32(floor), np.float32(-np.inf)))
    if floor == 0:
        expected = -float(np.finfo(np.float32).eps)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert mesh.bounds[0, 2] == expected < floor
    assert mesh.bounds[1, 2] == 20
    assert 0 < report['floor_separation_mm'] <= .001
    np.testing.assert_array_equal(mesh.bounds[:, :2], [[-3, -3], [3, 3]])


def test_unrepresentable_safe_floor_is_rejected(tmp_path):
    path = tmp_path / 'cutter.stl'
    with pytest.raises(ValueError, match='STL precision'):
        export_groove_cutter(box(0, 0, 1, 1), 100000, 100010, path)
    assert not path.exists()


def test_captured_seven_edge_defect_is_never_blindly_capped(tmp_path):
    fixture = Path(__file__).parent / 'fixtures/groove-coplanar-floor.npz'
    with np.load(fixture, allow_pickle=False) as data:
        mesh = trimesh.Trimesh(data['vertices'], data['faces'], process=False)
    source, output = tmp_path / 'coplanar.stl', tmp_path / 'fixed.stl'
    mesh.export(source)
    with pytest.raises(TerrainRepairError) as error:
        repair_terrain_for_water_boolean(source, output)
    assert error.value.repair_report['passes'][-1]['remaining_non_manifold_edges'] == 7
    assert not output.exists()
