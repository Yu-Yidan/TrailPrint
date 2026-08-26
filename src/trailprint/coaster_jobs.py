"""Background coordination for flat-coaster builds and exports."""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .blender_bridge import BuildCancelled
from .coaster import CoasterSettings, run_coaster_build
from .coaster_bambu import CoasterBambuSettings, create_coaster_bambu_project


@dataclass
class CoasterJob:
    id: str
    project: Path
    settings: CoasterSettings
    state: str = "queued"
    message: str = "Waiting to start"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    outputs: List[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass
class CoasterBambuJob:
    id: str
    project: Path
    settings: CoasterBambuSettings
    state: str = "queued"
    message: str = "Waiting to start"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    output: str = ""


class CoasterJobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = None

    def start(self, project, settings):
        with self._lock:
            if self._active and self._jobs[self._active].state in {"queued", "running"}:
                raise ValueError("Another coaster is currently building.")
            job = CoasterJob(uuid.uuid4().hex, project.resolve(), settings)
            self._jobs[job.id] = job
            self._active = job.id
        threading.Thread(target=self._run, args=(job,), daemon=True,
                         name=f"coaster-{job.id[:8]}").start()
        return job

    def _run(self, job):
        job.started_at = time.time()
        job.state, job.message = "running", "Acquiring terrain with TrailPrint3D"
        try:
            outputs = run_coaster_build(job.project, job.settings,
                                        cancel_event=job.cancel_event)
        except BuildCancelled:
            job.state, job.message = "cancelled", "Coaster build cancelled; prior output preserved."
        except Exception as exc:
            job.state, job.message = "error", str(exc)
        else:
            job.outputs = [path.name for path in outputs]
            job.state, job.message = "success", "Flat coaster geometry and preview generated."
        finally:
            job.finished_at = time.time()
            with self._lock:
                if self._active == job.id:
                    self._active = None

    def get(self, job_id):
        return self._jobs.get(job_id)

    def for_project(self, project):
        project = project.resolve()
        matches = [job for job in self._jobs.values() if job.project == project]
        return max(matches, key=lambda job: job.started_at or 0) if matches else None

    def is_active(self, project):
        job = self.for_project(project)
        return bool(job and job.state in {"queued", "running", "cancelling"})

    def cancel(self, job_id):
        job = self._jobs.get(job_id)
        if job is None or job.state not in {"queued", "running"}:
            raise ValueError("This coaster build can no longer be cancelled.")
        job.cancel_event.set()
        job.state, job.message = "cancelling", "Stopping Blender safely…"
        return job

    @staticmethod
    def serialize(job):
        now = job.finished_at or time.time()
        return {"id": job.id, "state": job.state, "message": job.message,
                "elapsed_seconds": round(now - (job.started_at or now), 1),
                "outputs": job.outputs,
                "preview_url": (f"/projects/{job.project.name}/coaster"
                                if job.state == "success" else None)}


class CoasterBambuJobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def start(self, project, settings):
        project = project.resolve()
        with self._lock:
            if any(job.project == project and job.state in {"queued", "running"}
                   for job in self._jobs.values()):
                raise ValueError("A coaster 3MF is already being created.")
            job = CoasterBambuJob(uuid.uuid4().hex, project, settings)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True,
                         name=f"coaster-bambu-{job.id[:8]}").start()
        return job

    @staticmethod
    def _run(job):
        job.started_at = time.time()
        job.state, job.message = "running", "Creating two-plate Bambu project"
        try:
            output = create_coaster_bambu_project(job.project, job.settings)
        except Exception as exc:
            job.state, job.message = "error", str(exc)
        else:
            job.state, job.message, job.output = (
                "success", "Validated two-plate coaster 3MF created.", output.name)
        job.finished_at = time.time()

    def get(self, job_id):
        return self._jobs.get(job_id)

    def for_project(self, project):
        project = project.resolve()
        matches = [job for job in self._jobs.values() if job.project == project]
        return max(matches, key=lambda job: job.started_at or 0) if matches else None

    def is_active(self, project):
        project = project.resolve()
        return any(job.project == project and job.state in {"queued", "running"}
                   for job in self._jobs.values())

    @staticmethod
    def serialize(job):
        now = job.finished_at or time.time()
        return {"id": job.id, "state": job.state, "message": job.message,
                "elapsed_seconds": round(now - (job.started_at or now), 1),
                "output": job.output}
