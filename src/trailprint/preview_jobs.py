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

    def start(self, project, settings, environment, before_start=lambda: None):
        identifier = uuid.uuid4().hex
        OPERATIONS.acquire(project, "rendering previews", identifier)
        settings, environment = deepcopy(settings), deepcopy(environment)
        job = dict(
            id=identifier,
            project=str(project.resolve()),
            state="queued",
            message="Waiting to render",
        )
        self.jobs[identifier] = job
        try:
            before_start()
            threading.Thread(
                target=self.run, args=(job, project, settings, environment), daemon=True
            ).start()
        except Exception:
            OPERATIONS.release(project, identifier)
            self.jobs.pop(identifier, None)
            raise
        return job

    def run(self, job, project, settings, environment):
        stage = None
        try:
            job.update(state="running", message="Rendering top and angled previews")
            stage = Path(tempfile.mkdtemp(prefix="preview-", dir=project / "build"))
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
            publish_files(changes, project / "build")
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
