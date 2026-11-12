from pathlib import Path

import numpy as np
import pytest
import trimesh

from trailprint.water_geometry import TerrainRepairError, repair_terrain_for_water_boolean


def test_captured_water_basin_sliver_is_stitched_without_changing_solid(tmp_path):
    with np.load(Path(__file__).parent/'fixtures/water-overfull-sliver-edge.npz') as data:
        mesh = trimesh.Trimesh(data['vertices'], data['faces'], process=False)
    source, output = tmp_path/'source.stl', tmp_path/'repaired.stl'
    mesh.export(source)
    assert not mesh.is_watertight
    report = repair_terrain_for_water_boolean(source, output)
    actual = trimesh.load_mesh(output, process=True)
    assert actual.is_watertight and actual.is_winding_consistent
    np.testing.assert_array_equal(actual.bounds, mesh.bounds)
    assert abs(actual.volume-mesh.volume) < .00001
    assert report['passes'][-1]['overfull_sliver_splits'] > 0
    assert report['tiny_edge_collapses'] == 0


@pytest.mark.parametrize('corruption', ['hole', 'bounds', 'volume'])
def test_sliver_repair_rejects_a_corrupted_stl_round_trip(tmp_path, monkeypatch, corruption):
    with np.load(Path(__file__).parent/'fixtures/water-overfull-sliver-edge.npz') as data:
        mesh = trimesh.Trimesh(data['vertices'], data['faces'], process=False)
    source, output = tmp_path/'source.stl', tmp_path/'repaired.stl'
    mesh.export(source)
    load = trimesh.load_mesh

    def corrupt(*args, **kwargs):
        result = load(*args, **kwargs)
        if kwargs.get('file_type') == 'stl':
            if corruption == 'hole':
                result.update_faces(np.arange(len(result.faces)) != 0)
            elif corruption == 'bounds':
                result.apply_translation((1, 0, 0))
            else:
                result.invert()
        return result

    monkeypatch.setattr(trimesh, 'load_mesh', corrupt)
    with pytest.raises(TerrainRepairError):
        repair_terrain_for_water_boolean(source, output)
    assert not output.exists()
