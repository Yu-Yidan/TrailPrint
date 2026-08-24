import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trailprint.blender_bridge import BuildCancelled, BuildSettings
from trailprint.jobs import BuildManager


class BuildCancellationTests(unittest.TestCase):
    def test_manager_moves_running_job_to_cancelled(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()

            def cancellable(_project, _settings, cancel_event=None):
                while not cancel_event.wait(0.01):
                    pass
                raise BuildCancelled()

            manager = BuildManager()
            with patch("trailprint.jobs.run_blender_build", side_effect=cancellable):
                job = manager.start(project, BuildSettings())
                deadline = time.time() + 2
                while job.state == "queued" and time.time() < deadline:
                    time.sleep(0.01)
                manager.cancel(job.id)
                while job.state != "cancelled" and time.time() < deadline:
                    time.sleep(0.01)

            self.assertEqual(job.state, "cancelled")
            self.assertFalse(manager.is_active(project))
            self.assertFalse(manager.serialize(job)["can_cancel"])
            with self.assertRaisesRegex(ValueError, "no longer"):
                manager.cancel(job.id)


if __name__ == "__main__":
    unittest.main()
