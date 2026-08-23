"""Single-process background job coordination for local Blender builds."""

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .blender_bridge import BuildSettings, read_progress, run_blender_build


@dataclass
class BuildJob:
    id: str
    project: Path
    settings: BuildSettings
    state: str = "queued"
    message: str = "Waiting to start"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    outputs: List[str] = field(default_factory=list)


class BuildBusy(ValueError):
    pass


class BuildManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, BuildJob] = {}
        self._active: Optional[str] = None

    def start(self, project: Path, settings: BuildSettings) -> BuildJob:
        with self._lock:
            if self._active and self._jobs[self._active].state in {"queued", "running"}:
                raise BuildBusy("Another map is currently building. Wait for it to finish.")
            job = BuildJob(uuid.uuid4().hex, project.resolve(), settings)
            self._jobs[job.id] = job
            self._active = job.id
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"trailprint-{job.id[:8]}").start()
        return job

    def _run(self, job: BuildJob):
        job.state, job.message, job.started_at = "running", "Starting Blender", time.time()
        try:
            outputs = run_blender_build(job.project, job.settings)
        except Exception as exc:
            job.state, job.message = "error", str(exc)
        else:
            job.outputs = [path.name for path in outputs]
            job.state, job.message = "success", f"Generated {len(outputs)} STL file(s)."
        finally:
            job.finished_at = time.time()
            with self._lock:
                if self._active == job.id:
                    self._active = None

    def get(self, job_id: str) -> Optional[BuildJob]:
        return self._jobs.get(job_id)

    def for_project(self, project: Path) -> Optional[BuildJob]:
        matches = [job for job in self._jobs.values() if job.project == project.resolve()]
        return max(matches, key=lambda job: job.started_at or 0) if matches else None

    def is_active(self, project: Path) -> bool:
        job = self.for_project(project)
        return bool(job and job.state in {"queued", "running"})

    def serialize(self, job: BuildJob) -> dict:
        now = job.finished_at or time.time()
        progress = read_progress() if job.state == "running" else {}
        percent = progress.get("progress", progress.get("percent"))
        phase = progress.get("phase", progress.get("message", job.message))
        return {
            "id": job.id, "state": job.state, "message": job.message, "phase": phase,
            "percent": percent, "elapsed_seconds": round(now - (job.started_at or now), 1),
            "outputs": job.outputs,
        }
