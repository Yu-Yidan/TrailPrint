import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh
from shapely import get_precision, set_precision
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union

import trailprint.water_geometry as water

FIXTURE = Path(__file__).parent / "fixtures/high-country-water-polygons.json"


class FlatSurface:
    def z(self, point):
        return 5.0


def shells(polygon):
    samples = np.array([[0, 0, 5], [1, 0, 5], [0, 1, 5]])
    return water._water_body_meshes(polygon, FlatSurface(), samples, None, .4, 9, 0)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("precision", [0, .01])
def test_actual_lake_contacts_become_closed_without_expanding(index, precision, tmp_path):
    polygon = set_precision(shape(json.loads(FIXTURE.read_text())[index]), precision)
    original, _ = shells(polygon)
    assert polygon.is_valid
    assert not water._water_shells_valid(original)
    parts, omitted, correction = water._regularize_water_polygon(polygon)
    assert not omitted
    assert correction["inset_mm"] == .001
    assert correction["removed_area_mm2"] > 0
    assert len(parts) == 1
    for part in parts:
        assert get_precision(part) == 0
        assert part.difference(set_precision(polygon, 0)).area < 1e-8
        meshes, body = shells(part)
        assert water._water_shells_valid(meshes)
        assert body["depth_mm"] == .4
        assert body["surface_max_z_mm"] == 5
        for role, mesh in zip(("water", "cutter", "support"), meshes):
            target = tmp_path / f"{role}.stl"
            mesh.export(target)
            actual = trimesh.load_mesh(target, process=True)
            assert actual.is_watertight and actual.is_winding_consistent
        cap = meshes[0]
        assert cap.bounds[1, 2] - cap.bounds[0, 2] == pytest.approx(.4)


def test_correction_splits_numerical_necks_and_records_tiny_fragments():
    connected = unary_union((box(0, 0, 2, 2), box(3, 0, 5, 2),
                             box(2, 1, 3, 1.0005), box(6, 0, 6.5, .5),
                             box(5, .2, 6, .2005)))
    parts, omitted, report = water._regularize_water_polygon(connected, enforce_width=True)
    assert len(parts) == 2
    assert report["resulting_body_count"] == 2
    assert len(omitted) == 1
    assert omitted[0]["replacement"] == "existing terrain"


def inputs(tmp_path):
    terrain = trimesh.creation.box((100, 100, 4))
    terrain.apply_translation((0, 0, 2))
    terrain.export(tmp_path / "terrain.stl")
    lake = trimesh.creation.box((10, 10, .4))
    lake.apply_translation((0, 0, 3.8))
    lake.export(tmp_path / "raw.stl")
    return (tmp_path / "terrain.stl", [tmp_path / "raw.stl"], tmp_path / "water.stl",
            tmp_path / "cutter.stl", tmp_path / "support.stl")


def test_normal_water_does_not_use_correction(tmp_path):
    with patch.object(water, "_regularize_water_polygon", side_effect=AssertionError("unexpected")):
        report = water.prepare_integrated_water(*inputs(tmp_path))
    assert report["available"]
    assert report["water_topology_corrections"] == []


def test_full_preparation_repairs_point_contact_and_reports_adjustment(tmp_path):
    polygon = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(0, 2), (1, 1), (1, 3)]])
    with patch.object(water, "_stable_polygon", return_value=set_precision(polygon, .01)):
        report = water.prepare_integrated_water(*inputs(tmp_path))
    assert report["body_count"] == 1
    assert report["water_topology_corrections"][0]["source_body"] == 1
    assert report["water_topology_corrections"][0]["removed_area_mm2"] > 0
    for name in ("water", "cutter", "support"):
        assert trimesh.load_mesh(tmp_path / f"{name}.stl", process=True).is_watertight


def test_failed_single_correction_remains_blocking(tmp_path):
    polygon = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(0, 2), (1, 1), (1, 3)]])
    with patch.object(water, "_stable_polygon", return_value=polygon), patch.object(
            water, "_regularize_water_polygon",
            return_value=([polygon], [], {"inset_mm": .001})) as correction:
        with pytest.raises(ValueError, match="body 1.*not watertight after"):
            water.prepare_integrated_water(*inputs(tmp_path))
    assert correction.call_count == 1
    assert not (tmp_path / "water.stl").exists()


def test_exported_support_is_validated_too(tmp_path):
    original = trimesh.load_mesh
    broken = trimesh.creation.box()
    broken.update_faces(np.arange(len(broken.faces)) != 0)

    def reload(path, *args, **kwargs):
        if isinstance(path, Path) and path.name == "support.stl":
            return broken
        return original(path, *args, **kwargs)

    with patch.object(trimesh, "load_mesh", side_effect=reload):
        with pytest.raises(ValueError, match="STL export: support"):
            water.prepare_integrated_water(*inputs(tmp_path))
