from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from code_mower.doctor_checks.self_hosted_runner import (
    RunnerListenerProcess,
    check_runner_actionlint_available,
    check_runner_cli_auth,
    check_runner_generated_workflows_actionlint,
    check_runner_launchagent,
    check_runner_listener_env_freshness,
    check_runner_required_env,
    check_runner_workflow_labels,
    _runner_labels_from_github,
    _runner_root_from_command,
)

ROOT = Path(__file__).resolve().parents[1]


def _runner_config(label: str = "bridge-pro-audit") -> dict[str, object]:
    return {
        "version": 1,
        "project": {"name": "test", "state_dir": ".code-mower"},
        "repositories": [{"slug": "owner/repo", "default_branch": "main"}],
        "owner_surface": {
            "owner_login": "owner",
            "status_issue": "1",
            "local_audit_runner_label": label,
        },
        "lanes": {
            "codex": {
                "type": "audit",
                "driver": "local_cli",
                "provider": "codex",
                "merge_authority": True,
                "labels": {
                    "needs": "needs-codex-audit",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                },
                "token_env": ["DISPATCH_TOKEN", "GITHUB_TOKEN"],
                "provider_config": {"command": "codex"},
            },
            "claude_audit": {
                "type": "audit",
                "driver": "local_cli",
                "provider": "claude",
                "trailer_lane": "claude",
                "merge_authority": True,
                "labels": {
                    "needs": "needs-claude-audit",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                },
                "token_env": ["DISPATCH_TOKEN", "GITHUB_TOKEN"],
                "provider_config": {
                    "command": "claude",
                    "doctor_probe_args": [
                        "--print",
                        "--output-format",
                        "json",
                        "Reply with exactly: ok",
                    ],
                    "doctor_probe_expect_json": True,
                    "doctor_probe_expect_json_field": "result",
                    "doctor_probe_expect_json_value": "ok",
                    "doctor_probe_error_fields": ["is_error", "api_error_status"],
                    "doctor_probe_auth_status_fields": ["api_error_status"],
                },
            },
        },
        "profiles": {
            "recommended": {
                "description": "recommended",
                "lanes": ["codex", "claude_audit"],
            }
        },
    }


def _write_runner_plist(home: Path, payload: dict[str, object]) -> Path:
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    path = launch_agents / "actions.runner.owner.repo.mac.plist"
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)
    return path


class SelfHostedRunnerDoctorTests(unittest.TestCase):
    def test_runner_launchagent_fails_on_session_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = _write_runner_plist(home, {"SessionCreate": True})

            check = check_runner_launchagent(home=home, platform="darwin")

        self.assertEqual(check.status, "fail")
        self.assertIn("SessionCreate=true", check.message)
        self.assertEqual(check.detail["session_create_plists"], [str(plist_path)])

    def test_runner_required_env_fails_when_launchd_env_is_missing(self) -> None:
        check = check_runner_required_env(
            environ={
                "USER": "runner",
                "LOGNAME": "",
                "SHELL": "/bin/zsh",
            }
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.detail["missing"], ["LOGNAME", "LANG"])

    def test_runner_cli_auth_fails_when_codex_probe_returns_nonzero(self) -> None:
        config = _runner_config()
        codex_lane = config["lanes"]["codex"]

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            codex = bin_dir / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex.chmod(0o755)

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(command, 1, "", "not logged in")

            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=False):
                checks = check_runner_cli_auth(
                    [("codex", codex_lane)],
                    run=fake_run,
                    http_timeout=5,
                )

        codex_check = next(check for check in checks if check.lane == "codex")
        self.assertEqual(codex_check.status, "fail")
        self.assertIn("codex auth prompt probe", codex_check.message)

    def test_runner_workflow_labels_fail_when_custom_label_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config_path = repo_root / "code-mower.yml"
            check = check_runner_workflow_labels(
                config=_runner_config(),
                profile="recommended",
                config_path=config_path,
                repo_root=repo_root,
                source_root=ROOT,
                actual_labels=("self-hosted", "macOS", "wrong-label"),
            )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.detail["missing_labels"], ["bridge-pro-audit"])

    def test_runner_labels_from_github_paginates_runner_inventory(self) -> None:
        calls: list[str] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command[-1])
            if command[-1].endswith("page=1"):
                runners = [{"name": f"other-{index}", "labels": []} for index in range(100)]
            else:
                runners = [
                    {
                        "name": "mac-runner",
                        "labels": [
                            {"name": "self-hosted"},
                            {"name": "bridge-pro-audit"},
                        ],
                    }
                ]
            return subprocess.CompletedProcess(command, 0, json.dumps({"runners": runners}), "")

        with tempfile.TemporaryDirectory() as tmp:
            labels = _runner_labels_from_github(
                config=_runner_config(),
                repo_root=Path(tmp),
                environ={"RUNNER_NAME": "mac-runner"},
                run=fake_run,
                which=lambda _command: "/usr/local/bin/gh",
            )

        self.assertEqual(labels, ("self-hosted", "bridge-pro-audit"))
        self.assertEqual(
            calls,
            [
                "repos/owner/repo/actions/runners?per_page=100&page=1",
                "repos/owner/repo/actions/runners?per_page=100&page=2",
            ],
        )

    def test_runner_actionlint_available_fails_when_binary_is_missing(self) -> None:
        check = check_runner_actionlint_available(
            actionlint_bin="missing-actionlint",
            which=lambda _command: None,
        )

        self.assertEqual(check.status, "fail")
        self.assertIn("not found", check.message)

    def test_runner_generated_workflows_fail_when_actionlint_fails(self) -> None:
        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "invalid workflow")

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = check_runner_generated_workflows_actionlint(
                config=_runner_config(),
                profile="recommended",
                config_path=repo_root / "code-mower.yml",
                source_root=ROOT,
                actionlint_bin="actionlint",
                run=fake_run,
                which=lambda _command: "/usr/local/bin/actionlint",
            )

        self.assertEqual(check.status, "fail")
        self.assertIn("actionlint", check.detail["error"])

    def test_runner_listener_env_fails_when_listener_started_before_env_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner_root = Path(tmp) / "actions-runner"
            runner_root.mkdir()
            env_path = runner_root / ".env"
            env_path.write_text("USER=runner\n", encoding="utf-8")
            env_mtime = datetime.now()
            os.utime(env_path, (env_mtime.timestamp(), env_mtime.timestamp()))
            process = RunnerListenerProcess(
                pid=123,
                start_time=env_mtime - timedelta(minutes=5),
                command=f"{runner_root}/bin/Runner.Listener run",
            )

            check = check_runner_listener_env_freshness(
                environ={},
                processes=(process,),
            )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.detail["stale_listener_pids"], [123])

    def test_runner_listener_env_compares_each_process_to_its_own_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner_a = root / "runner-a"
            runner_b = root / "runner-b"
            runner_a.mkdir()
            runner_b.mkdir()
            env_a = runner_a / ".env"
            env_b = runner_b / ".env"
            env_a.write_text("USER=runner-a\n", encoding="utf-8")
            env_b.write_text("USER=runner-b\n", encoding="utf-8")
            now = datetime.now()
            os.utime(env_a, (now.timestamp(), now.timestamp()))
            older_env = now - timedelta(hours=2)
            os.utime(env_b, (older_env.timestamp(), older_env.timestamp()))
            processes = (
                RunnerListenerProcess(
                    pid=101,
                    start_time=now + timedelta(minutes=1),
                    command=f"{runner_a}/bin/Runner.Listener run",
                ),
                RunnerListenerProcess(
                    pid=202,
                    start_time=now - timedelta(hours=1),
                    command=f"{runner_b}/bin/Runner.Listener run",
                ),
            )

            check = check_runner_listener_env_freshness(
                environ={},
                processes=processes,
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(
            check.detail["listener_env_files"],
            {
                "101": str(env_a),
                "202": str(env_b),
            },
        )

    def test_runner_root_from_command_allows_spaces_in_runner_path(self) -> None:
        root = Path("/tmp/John Doe/actions-runner")

        resolved = _runner_root_from_command(f"{root}/bin/Runner.Listener run")

        self.assertEqual(resolved, root)


if __name__ == "__main__":
    unittest.main()
