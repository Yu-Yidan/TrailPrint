"""Render captured color settings without creating a slicer project."""

import hashlib
import json
import shutil
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from .bambu import nominal_layer_height
from .export_previews import stage_export_previews
from .project_operations import OPERATIONS
from .publishing import publish_files
from .terrain_bands import export_ranges
from .terrain_colors import elevation_metadata


class PreviewJobs:
    def __init__(self):
        self.jobs = {}

    def start(self, project, settings, environment, before_start=lambda: None, scale=None):
        identifier = uuid.uuid4().hex
        OPERATIONS.acquire(project, "rendering previews", identifier)
        settings, environment = deepcopy(settings), deepcopy(environment)
        job = dict(
            id=identifier,
            project=str(project.resolve()),
            state="queued",
            message="Waiting to render",
            scale=scale,
        )
        self.jobs[identifier] = job
        try:
            if scale is None:
                before_start()
            threading.Thread(
                target=self.run, args=(job, project, settings, environment, before_start if scale is not None else None), daemon=True
            ).start()
        except Exception:
            OPERATIONS.release(project, identifier)
            self.jobs.pop(identifier, None)
            raise
        return job

    def run(self, job, project, settings, environment, save_preferences=None):
        stage = None
        try:
            job.update(state="running", message="Rendering top and angled previews")
            stage = Path(tempfile.mkdtemp(prefix="preview-", dir=project / "build"))
            original = project
            geometry_changes = {}
            if job.get('scale') is not None:
                from .blender_bridge import BuildSettings, run_blender_build
                saved = json.loads((project / 'build/current-settings.json').read_text())
                if float(saved.get('terrain_elevation_multiplier', 1)) != job['scale']:
                    job.update(message='Rebuilding terrain and trail at the selected elevation scale')
                    project = stage / project.name
                    project.mkdir()
                    shutil.copy2(original / 'route.gpx', project / 'route.gpx')
                    for name in ('project.json',):
                        if (original / name).exists():
                            shutil.copy2(original / name, project / name)
                    saved['terrain_elevation_multiplier'] = job['scale']
                    run_blender_build(project, BuildSettings.validated(**saved))
                    geometry_changes.update({path: None for path in (original / 'deliverables').glob('*.stl')})
                    for folder in ('build', 'deliverables'):
                        for path in (project / folder).iterdir():
                            if path.is_file():
                                geometry_changes[original / folder / path.name] = path
                    from .naming import canonical_3mf_name
                    geometry_changes[original / 'deliverables' / canonical_3mf_name(original)] = None
                    job['geometry_changed'] = True
            bands = (
                export_ranges(
                    settings.custom_bands,
                    elevation_metadata(project),
                    nominal_layer_height(environment),
                )[1:]
                if settings.terrain_mode == "custom"
                else []
            )
            changes, colors = stage_export_previews(project, stage, settings, environment, bands)
            report_path = project / "build/validation.json"
            report = json.loads(changes[report_path].read_text())
            report["preview_only"] = True
            changes[report_path].write_text(json.dumps(report, indent=2))
            changes = {original / path.relative_to(project): value for path, value in changes.items()}
            publish_files({**geometry_changes, **changes}, original / "build")
            if save_preferences is not None:
                save_preferences()
            project = original
            revision = hashlib.sha256((project / "build/preview.png").read_bytes()).hexdigest()
            job.update(
                state="success",
                message="Preview refreshed. Generate a 3MF to export these colors.",
                revision=revision,
                color_revision=colors["color_revision"],
            )
        except Exception as exc:
            job.update(state="error", message=str(exc))
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            OPERATIONS.release(project, job["id"])
