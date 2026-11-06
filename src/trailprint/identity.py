"""Installation-local project identity, independent of reporting history."""
import csv
import fcntl
import json
import os
import plistlib
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .naming import format_project_id, read_project_meta, storage_name


class ProjectIdentity:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'data/settings/identity.json'

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid local identity settings; restore the settings backup.')
        return value

    def write(self, value):
        fd, name = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(value, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def configure(self, prefix):
        prefix = prefix.strip().upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9]{1,7}', prefix):
            raise ValueError('Use 2–8 letters or numbers, starting with a letter.')
        with self.locked():
            value = self.read()
            value['prefix'] = prefix
            self.write(value)
        return prefix

    def reserve(self):
        with self.locked():
            value = self.read()
            if not value.get('prefix'):
                raise ValueError('Set your local project prefix before creating a project.')
            maximum = int(value.get('last_number', 0))
            # Include legacy folders, recoverable trash and historical IDs, read-only.
            for root in (self.root / 'prints', self.root / '.trailprint-trash'):
                for p in root.iterdir() if root.is_dir() else []:
                    if not p.is_dir():
                        continue
                    number = read_project_meta(p).get('id_project', 0)
                    match = re.match(r'^(?:[A-Z][A-Z0-9]{1,7}-)?(\d{4})-', p.name)
                    maximum = max(maximum, int(number), int(match[1]) if match else 0)
            history = self.root / 'data/analytics/dim_project.csv'
            if history.is_file():
                with history.open() as handle:
                    maximum = max(maximum, max((int(r['id_project']) for r in csv.DictReader(handle)), default=0))
            identifier = maximum + 1
            format_project_id(identifier)
            value['last_number'] = identifier
            self.write(value)
            return value['prefix'], identifier

    def register(self, project, name, is_test=False, prints_dir=None):
        prefix, number = self.reserve()
        slug = project.name.replace('-', '_')
        destination = (prints_dir or project.parent) / storage_name(number, slug, prefix)
        if destination.exists():
            raise ValueError('Project destination already exists.')
        meta = {'id_project': number, 'creator_prefix': prefix, 'project_slug': slug,
                'dim_project_name': name.strip(), 'dim_is_test': bool(is_test)}
        (project / 'project.json').write_text(json.dumps(meta, indent=2) + '\n')
        project.rename(destination)
        return destination


    def create_project(self, prints_dir, name, sources, is_test=False):
        from .projects import create_project_from_uploads
        prints_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.upload-', dir=prints_dir) as temp:
            project = create_project_from_uploads(Path(temp), name, sources)
            return self.register(project, name, is_test, prints_dir)


def disable_legacy_scheduler(root):
    """Disable only the old job belonging to this checkout, preserving its plist."""
    target = Path.home() / 'Library/LaunchAgents/com.trailprint.analytics.plist'
    if not target.is_file():
        return
    payload = plistlib.loads(target.read_bytes())
    if Path(payload.get('WorkingDirectory', '')).resolve() != Path(root).resolve():
        return
    result = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(target)],
                            capture_output=True, text=True)
    # launchctl reports 3/5 when the job is already absent.
    if result.returncode not in (0, 3, 5):
        raise OSError(result.stderr.strip() or 'Could not disable the old analytics scheduler.')
    target.rename(target.with_suffix('.plist.disabled'))

    
