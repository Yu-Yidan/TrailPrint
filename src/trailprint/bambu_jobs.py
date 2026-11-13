"""Background coordination for Bambu Studio project exports."""

import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .bambu import BambuEnvironment, BambuSettings, create_bambu_project, discover_environment
from .project_operations import OPERATIONS, release_operation


@dataclass
class BambuJob:
    id: str
    project: Path
    settings: BambuSettings
    environment: BambuEnvironment
    state: str = "queued"
    message: str = "Waiting to start"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    output: str = ""


class BambuJobManager:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def start(self, project, settings, environment=None):
        project = project.resolve()
        with self._lock:
            if any(job.project == project and job.state in {"queued", "running"}
                   for job in self._jobs.values()):
                raise ValueError("A Bambu project is already being created for this model.")
            job = BambuJob(uuid.uuid4().hex, project, settings,
                           deepcopy(environment if environment is not None else discover_environment()))
            OPERATIONS.acquire(job.project, "exporting", job.id)
            self._jobs[job.id] = job
        try:
            threading.Thread(target=self._run, args=(job,), daemon=True).start()
        except Exception:
            OPERATIONS.release(job.project, job.id)
            with self._lock:
                self._jobs.pop(job.id, None)
            raise
        return job

    @release_operation
    def _run(self, job):
        job.state, job.message, job.started_at = "running", "Creating Bambu project", time.time()
        try:
            output = create_bambu_project(job.project, job.settings, environment=job.environment)
        except Exception as exc:
            job.state, job.message = "error", str(exc)
        else:
            message = "Validated Bambu project created."
            job.state, job.message, job.output = "success", message, output.name
        job.finished_at = time.time()

    def get(self, job_id):
        return self._jobs.get(job_id)

    def for_project(self, project):
        matches = [job for job in self._jobs.values() if job.project == project.resolve()]
        return max(matches, key=lambda job: job.started_at or 0) if matches else None

    def is_active(self, project):
        project = project.resolve()
        return any(job.project == project and job.state in {"queued", "running"}
                   for job in self._jobs.values())

    def serialize(self, job):
        now = job.finished_at or time.time()
        return {"id": job.id, "state": job.state, "message": job.message,
                "elapsed_seconds": round(now - (job.started_at or now), 1),
                "output": job.output,
                "download_url": (f"/projects/{job.project.name}/files/{job.output}"
                                 if job.state == "success" else None)}
