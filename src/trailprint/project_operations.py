"""Coordinate mutating operations within the local app process."""

import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class ProjectOperations:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}

    def acquire(self, project, operation, owner):
        project = Path(project).resolve()
        with self._lock:
            if project in self._active:
                raise ValueError(f"This project is currently {self._active[project][0]}. Wait for it to finish.")
            self._active[project] = (operation, owner)

    def release(self, project, owner):
        with self._lock:
            key = Path(project).resolve()
            if key in self._active and self._active[key][1] == owner:
                del self._active[key]

    @contextmanager
    def hold(self, project, operation):
        owner = object()
        self.acquire(project, operation, owner)
        try:
            yield
        finally:
            self.release(project, owner)


OPERATIONS = ProjectOperations()


def release_operation(function):
    @wraps(function)
    def wrapped(self, job):
        try:
            return function(self, job)
        finally:
            OPERATIONS.release(job.project, job.id)
    return wrapped
