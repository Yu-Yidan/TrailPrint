import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trailprint.bambu import BambuEnvironment, BambuSettings
from trailprint.filament_palette import (
    ensure_terrain_palette,
    load_object_palette,
    palette_revision,
)
from trailprint.projects import create_project
from trailprint.terrain_bands import export_ranges, validate_ranges
from trailprint.web import create_app

ELEVATION = dict(
    source_min_m=100,
    source_max_m=1100,
    model_surface_min_z_mm=5,
    model_surface_max_z_mm=15,
    min_thickness_mm=2,
    unit_system="METRIC",
)


def test_band_conversion_preserves_elevations_and_two_layer_transition():
    ranges = [
        dict(upper_m=500, color="green"),
        dict(upper_m=1000, color="brown"),
        dict(upper_m=1040, color="nardo"),
        dict(upper_m=1100, color="ivory"),
    ]
    bands = export_ranges(ranges, ELEVATION)
    assert bands[2]["max_z"] - bands[2]["min_z"] == pytest.approx(0.4)
    assert bands[1]["min_elevation_m"] == 500
    assert bands[-1]["max_z"] == 12


@pytest.mark.parametrize(
    "ranges",
    [
        [],
        [dict(upper_m=1100, color="red")],
        [dict(upper_m=500, color="green")],
        [dict(upper_m=float("nan"), color="green")],
        [dict(upper_m=101, color="green"), dict(upper_m=1100, color="brown")],
        [dict(upper_m=700, color="green"), dict(upper_m=600, color="brown")],
        [dict(upper_m=1200, color="green")],
    ],
)
def test_invalid_ranges_are_rejected(ranges):
    with pytest.raises(ValueError):
        validate_ranges(ranges, ELEVATION)


def test_full_height_single_band_does_not_require_an_extra_layer():
    assert len(validate_ranges([dict(upper_m=1100, color="green")], ELEVATION)) == 1


def test_custom_export_uses_each_band_filament():
    ranges = [dict(upper_m=500, color="green"), dict(upper_m=1100, color="brown")]
    settings = BambuSettings.validated(
        7, terrain_mode="custom", custom_bands=ranges, band_filaments=[2, 7]
    )
    assert settings.by_role()["terrain"] == 2
    assert settings.terrain_filaments()["band_1"] == 7
    with pytest.raises(ValueError):
        BambuSettings.validated(7, terrain_mode="custom", custom_bands=ranges, band_filaments=[2])


def test_new_editor_persists_choices_and_never_writes_global_defaults(tmp_path):
    environment = BambuEnvironment(
        Path("bambu"),
        tmp_path / "bambu",
        "Bambu Lab P1S 0.4 nozzle",
        "process",
        tuple(
            dict(number=i + 1, name="Bambu PLA Matte", color=c)
            for i, c in enumerate(
                ["#000000", "#68724D", "#9B9EA0", "#FFFFFF", "#0078BF", "#DE4343"]
            )
        ),
        0.6,
        tuple(0 if i == j else 500 for i in range(6) for j in range(6)),
        (140,) * 12,
    )
    env = load_object_palette(tmp_path, lambda: environment)
    env, slots = ensure_terrain_palette(tmp_path, env)
    assert env.filaments[:6] == load_object_palette(tmp_path, lambda: environment).filaments[:6]
    assert len(set(slots.values())) == 5
    captured = []
    manager = SimpleNamespace(
        for_project=lambda p: None,
        is_active=lambda p: False,
        start=lambda p, s, environment: captured.append(s) or SimpleNamespace(id="test"),
    )
    p = create_project(
        tmp_path / "prints",
        "Bands",
        (Path(__file__).parent / "fixtures/gpx/hawaii-test.gpx").read_bytes(),
    )
    for name in ["bands.stl", "bands_Trail.stl", "bands_Plate.stl", "bands_Text.stl"]:
        (p / "deliverables" / name).write_bytes(b"stl")
    (p / "build/preview.png").write_bytes(b"png")
    (p / "build/elevation.json").write_text(json.dumps(ELEVATION))
    app = create_app(tmp_path, bambu_manager=manager)
    with patch("trailprint.web.discover_environment", return_value=env):
        client = app.test_client()
        html = client.get("/projects/bands/preview").data
        assert b"Advanced terrain colors" in html
        text_select = html.split(b'name="text_filament"', 1)[1].split(b'</select>', 1)[0]
        assert ('value="'+str(slots['nardo'])+'" data-color="#757575" selected').encode() in text_select
        assert b"Save terrain" not in html and b"Generate DIY" not in html
        defaults = tmp_path / "data/settings/bambu.json"
        before = defaults.read_bytes() if defaults.exists() else None
        ranges = [dict(upper_m=500, color="green"), dict(upper_m=1100, color="brown")]
        data = dict(
            palette_revision=palette_revision(env),
            trail_filament=6,
            plate_filament=1,
            text_filament=3,
            terrain_color="green",
            advanced_terrain="on",
            terrain_bands=json.dumps(ranges),
            save_terrain_defaults="on",
        )
        assert client.post("/projects/bands/bambu", data=data).status_code == 302
        assert captured[-1].terrain_mode == "custom"
        assert captured[-1].by_role()["attribution"] == 3
        assert captured[-1].band_filaments == (slots["green"], slots["brown"])
        assert json.loads((p / "terrain-bands.json").read_text())["bands"] == ranges
        assert (defaults.read_bytes() if defaults.exists() else None) == before
        del data["advanced_terrain"]
        del data["terrain_bands"]
        (p / "build/elevation.json").unlink()
        client.post("/projects/bands/bambu", data=data)
        assert captured[-1].terrain_mode == "single"
        assert captured[-1].by_role()["terrain"] == slots["green"]
