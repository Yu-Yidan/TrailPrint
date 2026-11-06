import io
import json
import plistlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trailprint.blender_bridge import BuildSettings
from trailprint.build_settings import current_model_settings
from trailprint.identity import ProjectIdentity, disable_legacy_scheduler
from trailprint.naming import deliverable_prefix, logical_slug
from trailprint.projects import (
    list_projects,
    restore_project,
    trash_project,
    update_production_status,
)
from trailprint.web import create_app

GPX = (Path(__file__).parent / 'fixtures/example-route.gpx').read_bytes()


def test_first_use_requires_prefix_and_does_not_create_partial_project(tmp_path):
    client = create_app(tmp_path).test_client()
    assert client.get('/projects/new').headers['Location'] == '/settings/identity'
    result = client.post('/projects', data={'name': 'Route', 'gpx': (io.BytesIO(GPX), 'route.gpx')})
    assert result.status_code == 400
    assert not (tmp_path/'prints').exists()
    assert client.post('/settings/identity', data={'prefix': '../bad'}).status_code == 400
    assert client.post('/settings/identity', data={'prefix': 'yd'}).status_code == 302
    assert 'YD-&lt;####&gt;' in client.get('/projects/new').text


@pytest.mark.parametrize('prefix', ['', 'A', '1A', '../YD', 'A B', 'ABCDEFGHI'])
def test_invalid_prefix(tmp_path, prefix):
    with pytest.raises(ValueError):
        ProjectIdentity(tmp_path).configure(prefix)


def test_numbering_reads_legacy_history_and_survives_deletion(tmp_path):
    legacy = tmp_path/'prints/0037-old-route'
    legacy.mkdir(parents=True)
    (legacy/'project_analytics.json').write_text(json.dumps({'id_project': 37}))
    history = tmp_path/'data/analytics/dim_project.csv'
    history.parent.mkdir(parents=True)
    history.write_text('id_project\n40\n')
    identity = ProjectIdentity(tmp_path)
    identity.configure('yd')
    project = identity.create_project(tmp_path/'prints', 'Maple Pass', [('route.gpx', GPX)])
    assert project.name == 'YD-0041-maple_pass'
    assert deliverable_prefix(project) == 'YD-0041-'
    assert logical_slug(project) == 'maple_pass'
    assert list_projects(tmp_path/'prints')[1].name == 'Maple Pass'
    trashed = trash_project(tmp_path/'prints', tmp_path/'.trailprint-trash', project.name, project.name)
    restored = restore_project(tmp_path/'prints', tmp_path/'.trailprint-trash', trashed.name)
    assert restored == project
    for p in restored.rglob('*'):
        if p.is_file():
            p.unlink()
    import shutil
    shutil.rmtree(restored)
    assert identity.reserve() == ('YD', 42)
    identity.configure('AB')
    assert identity.reserve() == ('AB', 43)
    assert legacy.is_dir()
    assert history.read_text() == 'id_project\n40\n'


def test_concurrent_allocations_and_independent_installations(tmp_path):
    identity = ProjectIdentity(tmp_path/'one')
    identity.configure('YD')
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ProjectIdentity(tmp_path/'one').reserve(), range(20)))
    assert sorted(number for _, number in results) == list(range(1, 21))
    other = ProjectIdentity(tmp_path/'two')
    other.configure('AB')
    assert other.reserve() == ('AB', 1)


def test_failed_upload_leaves_no_folder_or_consumed_id(tmp_path):
    identity = ProjectIdentity(tmp_path)
    identity.configure('YD')
    with pytest.raises(ValueError):
        identity.create_project(tmp_path/'prints', 'Bad Route', [('bad.gpx', b'bad')])
    assert list((tmp_path/'prints').iterdir()) == []
    assert identity.reserve() == ('YD', 1)


def test_dashboard_counts_current_status_and_excludes_tests(tmp_path):
    identity = ProjectIdentity(tmp_path)
    identity.configure('YD')
    project = identity.create_project(tmp_path/'prints', 'Route', [('route.gpx', GPX)])
    identity.create_project(tmp_path/'prints', 'Calibration', [('route.gpx', GPX)], is_test=True)
    (project/'deliverables'/f'{project.name}.3mf').write_bytes(b'3mf')
    client = create_app(tmp_path).test_client()
    assert '1 not printed' in client.get('/').text
    assert 'Calibration' not in client.get('/').text
    assert '2 not printed' in client.get('/?include_tests=1').text
    update_production_status(project, 'start')
    assert 'Printing <span>1' in client.get('/').text
    update_production_status(project, 'complete')
    assert '0 not printed' in client.get('/').text
    update_production_status(project, 'back')
    assert '1 not printed' in client.get('/').text
    assert not (tmp_path/'data/analytics').exists()
    assert client.get('/analytics').status_code == 404


def test_settings_snapshot_takes_precedence_over_legacy_record(tmp_path):
    (tmp_path/'build').mkdir()
    (tmp_path/'build/current-settings.json').write_text(json.dumps({'plate_title': 'Newest'}))
    assert current_model_settings(tmp_path, BuildSettings()).plate_title == 'Newest'


def test_scheduler_only_disables_matching_checkout_and_preserves_plist(tmp_path):
    home = tmp_path/'home'
    target = home/'Library/LaunchAgents/com.trailprint.analytics.plist'
    target.parent.mkdir(parents=True)
    content = plistlib.dumps({'WorkingDirectory': str(tmp_path/'repo')})
    target.write_bytes(content)
    with patch('trailprint.identity.Path.home', return_value=home), patch(
            'trailprint.identity.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
        disable_legacy_scheduler(tmp_path/'other')
        run.assert_not_called()
        disable_legacy_scheduler(tmp_path/'repo')
        run.assert_called_once()
        disable_legacy_scheduler(tmp_path/'repo')
        assert not target.exists()
        assert target.with_suffix('.plist.disabled').read_bytes() == content
