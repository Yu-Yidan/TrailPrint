"""Rollback-safe replacement of one generation's owned output files."""

import os
import shutil
import tempfile
from pathlib import Path


def publish_files(changes, backup_root, before_commit=None):
    """Apply destination -> staged file (or None for deletion), restoring on error.

    Callers serialize project operations. Backups are retained if rollback fails.
    """
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="publish-backup-", dir=backup_root))
    originals = {}
    applied = []
    try:
        for index, destination in enumerate(changes):
            destination = Path(destination)
            if destination.exists():
                saved = backup / str(index)
                shutil.copy2(destination, saved)
                originals[destination] = saved
        if before_commit is not None:
            before_commit()
        for destination, staged in changes.items():
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            applied.append(destination)
            if staged is None:
                destination.unlink(missing_ok=True)
            else:
                os.replace(staged, destination)
    except BaseException:
        for destination in reversed(applied):
            if destination in originals:
                os.replace(originals[destination], destination)
            else:
                destination.unlink(missing_ok=True)
        shutil.rmtree(backup, ignore_errors=True)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)
