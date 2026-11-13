import json

from trailprint.blender_bridge import initial_preview_colors


def test_initial_preview_keeps_saved_terrain_color_and_elevation_bands(tmp_path):
    project = tmp_path / "prints/demo"
    project.mkdir(parents=True)
    saved = dict(
        terrain="green",
        advanced=True,
        bands=[dict(upper_m=500, color="brown"), dict(upper_m=1000, color="ivory")],
    )
    (project / "terrain-bands.json").write_text(json.dumps(saved))
    elevation = dict(
        source_min_m=0,
        source_max_m=1000,
        model_surface_min_z_mm=5,
        model_surface_max_z_mm=15,
        min_thickness_mm=2,
    )
    colors = initial_preview_colors(project, elevation)
    assert colors["role_colors"]["terrain"] == "#7D6556"
    assert colors["terrain_bands"][0]["color"] == "#FFFFFF"
    assert colors["terrain_bands"][0]["min_z"] == 7
    assert colors["terrain_bands"][0]["max_z"] == 12


def test_invalid_saved_bands_do_not_block_stl_preview(tmp_path):
    project = tmp_path / "prints/demo"
    project.mkdir(parents=True)
    (project / "terrain-bands.json").write_text(
        json.dumps(dict(terrain="nardo", advanced=True, bands=[]))
    )
    colors = initial_preview_colors(
        project,
        dict(
            source_min_m=0,
            source_max_m=100,
            model_surface_min_z_mm=5,
            model_surface_max_z_mm=15,
            min_thickness_mm=2,
        ),
    )
    assert colors["role_colors"]["terrain"] == "#757575"
    assert colors["terrain_bands"] == []
