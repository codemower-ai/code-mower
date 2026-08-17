from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from code_mower.doctor_checks.runtime import check_macos_runner_launchagent


class DoctorRuntimeTests(unittest.TestCase):
    def _write_runner_plist(self, home: Path, payload: dict[str, object]) -> Path:
        launch_agents = home / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        path = launch_agents / "actions.runner.owner.repo.mac.plist"
        with path.open("wb") as handle:
            plistlib.dump(payload, handle)
        return path

    def test_macos_runner_launchagent_warns_on_session_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = self._write_runner_plist(home, {"SessionCreate": True})

            check = check_macos_runner_launchagent(home=home, platform="darwin")

        self.assertEqual(check.status, "warn")
        self.assertIn("SessionCreate=true", check.message)
        self.assertEqual(check.detail["session_create_plists"], [str(plist_path)])
        self.assertIn("Remove the `SessionCreate` key", str(check.remediation))

    def test_macos_runner_launchagent_passes_without_session_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_runner_plist(home, {"Label": "actions.runner.owner.repo.mac"})

            check = check_macos_runner_launchagent(home=home, platform="darwin")

        self.assertEqual(check.status, "pass")

    def test_macos_runner_launchagent_skips_off_macos(self) -> None:
        check = check_macos_runner_launchagent(platform="linux")

        self.assertEqual(check.status, "skip")


if __name__ == "__main__":
    unittest.main()
