import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trailprint.bambu import BambuSettings
from trailprint.export_previews import preview_colors, stage_export_previews

PALETTE = ('#000000', '#68724D', '#9B9EA0', '#FFFFFF', '#0078BF', '#DE4343')


def environment():
    return SimpleNamespace(filaments=tuple({'color': color} for color in PALETTE))


def test_manual_preview_uses_export_band_heights_and_selected_filaments():
    settings = BambuSettings.validated(6, trail=6, land=2, mountain=3, summit=4,
                                       plate=1, text=3, water=5)
    bands = [{'role': 'mountain', 'min_z': 2.12345678, 'max_z': 3.5},
             {'role': 'summit', 'min_z': 3.5, 'max_z': 4.0}]
    result = preview_colors(settings, environment(), bands)
    assert result['role_colors'] == dict(trail=PALETTE[5], terrain=PALETTE[1],
                                        plate=PALETTE[0], text=PALETTE[2],
                                        attribution=PALETTE[2], water=PALETTE[4])
    assert result['terrain_bands'] == [
        dict(role='mountain', min_z=2.123457, max_z=3.5, color=PALETTE[2]),
        dict(role='summit', min_z=3.5, max_z=4.0, color=PALETTE[3])]
    bands[0]['min_z'] = 99
    assert result['terrain_bands'][0]['min_z'] == 2.123457
    assert preview_colors(settings, environment(), [])['terrain_bands'] == []


def test_osm_preview_uses_first_band_for_base_and_export_bands_for_overrides():
    bands = [dict(role=role, min_z=i, max_z=i+1)
             for i, role in enumerate(('black', 'green', 'grey', 'white'))]
    settings = BambuSettings.validated(6, terrain_mode='osm', automatic_bands=bands,
                                       black=1, green=2, grey=3, white=4)
    colors = preview_colors(settings, environment(), bands[1:])
    assert colors['role_colors']['terrain'] == PALETTE[0]
    assert [band['color'] for band in colors['terrain_bands']] == list(PALETTE[1:4])


@pytest.mark.parametrize('failure', ['renderer', 'missing-side'])
def test_preview_failure_preserves_existing_files(tmp_path, failure):
    project = tmp_path/'project'
    build = project/'build'
    build.mkdir(parents=True)
    (project/'deliverables').mkdir()
    (build/'preview.png').write_bytes(b'old image')
    (build/'validation.json').write_text('{"valid": true}')
    stage = tmp_path/'stage'
    stage.mkdir()

    def render(*args, **kwargs):
        (stage/'preview.png').write_bytes(b'x'*1200)
        return SimpleNamespace(returncode=int(failure == 'renderer'), stderr='failed', stdout='')

    with patch('trailprint.export_previews.find_blender', return_value=Path('blender')), \
            patch('trailprint.export_previews._render_preview', side_effect=render):
        with pytest.raises(ValueError, match='preview rendering'):
            stage_export_previews(project, stage, BambuSettings.validated(6), environment(), [])
    assert (build/'preview.png').read_bytes() == b'old image'
    assert json.loads((build/'validation.json').read_text()) == {'valid': True}


def test_both_views_and_customer_images_are_staged_together(tmp_path):
    project = tmp_path/'project'
    build = project/'build'
    build.mkdir(parents=True)
    (project/'deliverables').mkdir()
    (build/'validation.json').write_text(json.dumps({'valid': True,
        'preview': {'customer_filename': 'Hawaii-preview.png'},
        'preview_side': {'customer_filename': 'Hawaii-side-preview.png'}}))
    stage = tmp_path/'stage'
    stage.mkdir()

    def render(*args, **kwargs):
        (stage/'preview.png').write_bytes(b't'*1200)
        (stage/'preview-side.png').write_bytes(b's'*1200)
        assert kwargs['color_settings']['role_colors']['text'] == PALETTE[2]
        return SimpleNamespace(returncode=0)

    with patch('trailprint.export_previews.find_blender', return_value=Path('blender')), \
            patch('trailprint.export_previews._render_preview', side_effect=render):
        changes, colors = stage_export_previews(project, stage,
            BambuSettings.validated(6, text=3), environment(), [])
    assert len(changes) == 5
    for view, customer in [('preview.png', 'Hawaii-preview.png'),
                           ('preview-side.png', 'Hawaii-side-preview.png')]:
        assert changes[build/view].read_bytes() == changes[project/'deliverables'/customer].read_bytes()
        assert not (build/view).exists()
    staged_report = json.loads(changes[build/'validation.json'].read_text())
    assert staged_report['preview']['color_revision'] == colors['color_revision']
    assert staged_report['preview_side']['color_revision'] == colors['color_revision']
