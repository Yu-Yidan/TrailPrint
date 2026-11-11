import json
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from trailprint.bambu import BambuEnvironment, BambuSettings
from trailprint.bambu_jobs import BambuJobManager
from trailprint.filament_palette import (
    OBJECT_DEFAULTS,
    import_palette,
    load_object_palette,
    load_palette,
    palette_path,
    palette_revision,
    role_defaults,
)
from trailprint.terrain_colors import save_terrain_preferences


def test_object_defaults_migrate_once_and_preserve_overrides(tmp_path):
    project = tmp_path / 'prints/example'
    project.mkdir(parents=True)
    preferences = project / 'terrain_colors.json'
    preferences.write_text(json.dumps({'trail': 1, 'plate': 3, 'grey_start_m': 900, 'white_start_m': 2200}))
    selected = load_object_palette(tmp_path, environment)
    assert len(selected.filaments) == 6
    assert selected.filaments[OBJECT_DEFAULTS['trail']-1]['color'] == '#DE4343'
    assert selected.filaments[OBJECT_DEFAULTS['plate']-1]['color'] == '#000000'
    migrated = json.loads(preferences.read_text())
    assert migrated['trail'] == 6 and migrated['plate'] == 1
    assert migrated['grey_start_m'] == 900
    assert (palette_path(tmp_path).parent/'before-object-defaults/example-terrain-colors.json').exists()
    migrated['trail'] = 5
    preferences.write_text(json.dumps(migrated))
    assert load_object_palette(tmp_path, lambda: None) == selected
    assert role_defaults(tmp_path, project, selected, {})['trail'] == 5
    assert len(selected.flush_volumes_matrix) == 36
    assert len(selected.flush_volumes_vector) == 12

def test_existing_object_palette_updates_only_text_to_ash_gray(tmp_path):
    project = tmp_path / 'prints/example'
    project.mkdir(parents=True)
    selected = load_object_palette(tmp_path, environment)
    saved = json.loads(palette_path(tmp_path).read_text())
    saved['object_defaults_version'] = 1
    palette_path(tmp_path).write_text(json.dumps(saved))
    preferences = project / 'terrain_colors.json'
    preferences.write_text(json.dumps({'text': 4, 'trail': 5, 'grey_start_m': 900}))
    assert load_object_palette(tmp_path, lambda: None) == selected
    migrated = json.loads(preferences.read_text())
    assert migrated == {'text': 3, 'trail': 5, 'grey_start_m': 900}
    assert selected.filaments[migrated['text']-1]['label'] == 'PLA Matte Ash Gray'
    assert (palette_path(tmp_path).parent/'before-ash-gray-text/example-terrain-colors.json').exists()


def environment(colors=("#000000", "#FFFFFF", "#FF0000", "#00AA00")):
    count = len(colors)
    return BambuEnvironment(
        Path("/Applications/BambuStudio"), Path("/profiles"), "P1S", "Standard",
        tuple({"number": i+1, "name": "PLA", "color": color}
              for i, color in enumerate(colors)),
        flush_volumes_matrix=tuple(0. if i == j else 140.
                                  for i in range(count) for j in range(count)),
        flush_volumes_vector=(140.,)*(count*2))


def test_saved_palette_survives_active_project_changes_and_restart(tmp_path):
    original = environment()
    assert load_palette(tmp_path, lambda: original) == original
    assert load_palette(tmp_path, lambda: environment(("#00AA00",))) == original
    # The saved palette does not need to read Bambu's mutable configuration.
    with patch("trailprint.filament_palette._recover", side_effect=AssertionError):
        assert load_palette(tmp_path, lambda: None) == original
    assert import_palette(tmp_path, lambda: environment(("#00AA00",))).filaments[0]["color"] == "#00AA00"
    assert len(load_palette(tmp_path, lambda: original).filaments) == 1


def test_import_remaps_saved_choices_and_requires_missing_color_reselection(tmp_path):
    before = environment()
    load_palette(tmp_path, lambda: before)
    project = tmp_path/"prints/test"
    save_terrain_preferences(tmp_path, project, {
        "land": 4, "mountain": 1, "summit": 2, "text": 2, "trail": 3,
        "grey_start_m": 1200, "white_start_m": 2300,
        "palette_revision": palette_revision(before)})
    after = import_palette(tmp_path, lambda: environment(("#FFFFFF", "#000000", "#00AA00")))
    defaults = role_defaults(tmp_path, project, after, {"trail": 1})
    assert defaults == {"trail": None, "land": 3, "mountain": 2, "summit": 1, "text": 1}
    # Legacy preferences use the original palette, even after multiple imports.
    preferences = json.loads((project/"terrain_colors.json").read_text())
    del preferences["palette_revision"]
    (project/"terrain_colors.json").write_text(json.dumps(preferences))
    assert role_defaults(tmp_path, project, after, {})["trail"] is None


def test_bootstrap_recovers_last_compatible_multicolor_project(tmp_path):
    old = environment()
    archive = tmp_path/"prints/old/deliverables/old.3mf"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("Metadata/project_settings.config", json.dumps({
            "printer_settings_id": "P1S",
            "filament_settings_id": [item["name"] for item in old.filaments],
            "filament_colour": [item["color"] for item in old.filaments],
            "flush_volumes_matrix": old.flush_volumes_matrix,
            "flush_volumes_vector": old.flush_volumes_vector}))
    recovered = load_palette(tmp_path, lambda: environment(("#00AA00",)))
    assert recovered == old


def test_invalid_palette_is_not_silently_replaced_but_explicit_import_recovers(tmp_path):
    palette_path(tmp_path).parent.mkdir(parents=True)
    palette_path(tmp_path).write_text("broken")
    with pytest.raises(ValueError, match="saved filament palette is invalid"):
        load_palette(tmp_path, environment)
    assert import_palette(tmp_path, environment) == environment()


def test_queued_job_uses_a_private_snapshot_without_rediscovery(tmp_path):
    chosen = environment()
    manager = BambuJobManager()
    with patch("trailprint.bambu_jobs.threading.Thread"):
        job = manager.start(tmp_path, BambuSettings.validated(4), environment=chosen)
    chosen.filaments[0]["color"] = "#123456"
    with patch("trailprint.bambu_jobs.discover_environment", side_effect=AssertionError), \
            patch("trailprint.bambu_jobs.create_bambu_project", return_value=tmp_path/"model.3mf") as build:
        manager._run(job)
    assert job.state == "success"
    assert build.call_args.kwargs["environment"].filaments[0]["color"] == "#000000"
    assert palette_revision(replace(job.environment, flush_volumes_matrix=tuple(
        int(v) for v in job.environment.flush_volumes_matrix))) == palette_revision(job.environment)
