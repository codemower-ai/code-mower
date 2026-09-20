from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase, mock

from code_mower import doctor as doctor_cli
from code_mower.doctor_checks import DoctorCheck, DoctorReport, render_doctor_text
from code_mower.doctor_checks.runner import run_doctor


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src/code_mower/templates/code-mower.example.yml"
PROVIDERS = ROOT / "src/code_mower/templates/providers.yml"


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _observer_report(*, checkout_present: bool, github: bool = False) -> DoctorReport:
    return run_doctor(
        config_path=STARTER,
        provider_templates_path=PROVIDERS,
        profile="recommended",
        repo_slug="owner/repo",
        repo_source="explicit",
        config_source="packaged_starter",
        adoption=True,
        adoption_posture="orchestrator-only",
        github=github,
        checkout_present=checkout_present,
    )


class RemoteObserverDoctorTests(TestCase):
    def test_checkout_free_plan_omits_local_and_unselected_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _cwd(Path(tmp)):
            report = _observer_report(checkout_present=False)

        source = next(
            check
            for check in report.checks
            if check.name == "doctor.adoption.config_source"
        )
        self.assertEqual(source.status, "pass")
        self.assertEqual(source.detail["plan"], "packaged_starter_remote_observer")
        self.assertFalse(source.detail["checkout_present"])
        self.assertEqual(source.detail["checkout_checks"], "not_applicable")
        self.assertNotIn("next_steps", source.detail)
        self.assertEqual(report.config_path, "packaged-starter")
        self.assertEqual(report.provider_templates_path, "packaged-provider-catalog")
        self.assertEqual(
            [stage["id"] for stage in report.run_plan],
            ["load-inputs", "select-profile", "adoption"],
        )

        names = {check.name for check in report.checks}
        self.assertNotIn("provider.review_hygiene", names)
        self.assertFalse(any(name.startswith("runtime.local_") for name in names))
        self.assertFalse(any(name.startswith("doctor.campaign.") for name in names))
        self.assertNotIn("doctor.campaign.cloud_upload", names)
        self.assertNotIn("runtime.pytest", names)
        self.assertFalse(any(name.startswith("tracker.jira.") for name in names))

    def test_checkout_plan_checks_workflows_without_publishing_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            workflows = checkout / ".github/workflows"
            workflows.mkdir(parents=True)
            for name in ("codex-clear-stale.yml", "claude-clear-stale.yml"):
                (workflows / name).write_text("name: clear stale\n", encoding="utf-8")
            with _cwd(checkout):
                report = _observer_report(checkout_present=True)
                serialized = json.dumps(report.as_dict(), sort_keys=True)

        hygiene = [
            check for check in report.checks if check.name == "provider.review_hygiene"
        ]
        self.assertEqual([check.status for check in hygiene], ["pass", "pass"])
        self.assertTrue(all("workflow_path" not in (check.detail or {}) for check in hygiene))
        self.assertNotIn(str(checkout), serialized)
        self.assertNotIn(str(STARTER.parent), serialized)

    def test_inaccessible_remote_has_one_exact_repository_remediation(self) -> None:
        inaccessible = DoctorCheck(
            name="github.repo.metadata",
            status="warn",
            message="could not read GitHub repository metadata for owner/repo",
            detail={"repo": "owner/repo", "returncode": 1, "output_redacted": True},
            remediation=(
                "Verify gh auth can read this repo. Private repos need a token or "
                "GitHub App installation with repository access."
            ),
        )
        variables = {
            "statuses": {
                "CODE_MOWER_OWNER_LOGIN": "not_confirmed",
                "CODE_MOWER_DECISION_AUTHORITIES": "not_confirmed",
                "CODE_MOWER_TRUSTED_AUTHORS_JSON": "not_confirmed",
                "CLAUDE_AUDIT_BOT_AUTHORS": "not_confirmed",
                "CODEX_BOT_AUTHORS": "not_confirmed",
            },
            "read_errors": {},
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            _cwd(Path(tmp)),
            mock.patch(
                "code_mower.doctor_checks.github.shutil.which",
                return_value="/private/observer/bin/gh",
            ),
            mock.patch(
                "code_mower.doctor_checks.runner.trusted_author_variable_probe",
                return_value=variables,
            ),
            mock.patch(
                "code_mower.doctor_checks.github.check_repo_metadata",
                return_value=(inaccessible, None),
            ),
        ):
            report = _observer_report(checkout_present=False, github=True)

        metadata = [
            check for check in report.checks if check.name == "github.repo.metadata"
        ]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].remediation, inaccessible.remediation)
        serialized = json.dumps(report.as_dict(), sort_keys=True)
        self.assertNotIn("/private/observer", serialized)
        self.assertEqual(
            report.warnings,
            sum(1 for check in report.checks if check.status == "warn"),
        )
        rendered = render_doctor_text(report)
        self.assertEqual(rendered.count("- WARN "), report.warnings)

    def test_cli_keeps_cloud_and_runtime_probe_quiet_unless_selected(self) -> None:
        captured: dict[str, object] = {}

        def fake_run_doctor(**kwargs: object) -> DoctorReport:
            captured.update(kwargs)
            return DoctorReport(
                config_path="packaged-starter",
                provider_templates_path="packaged-provider-catalog",
                profile="recommended",
                checks=(),
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            _cwd(Path(tmp)),
            mock.patch.object(doctor_cli, "run_doctor", side_effect=fake_run_doctor),
            mock.patch.object(doctor_cli, "board_startup_grace", return_value=None),
            redirect_stdout(StringIO()),
        ):
            code = doctor_cli.main(
                [
                    "--adoption",
                    "--orchestrator-only",
                    "--repo",
                    "owner/repo",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(captured["github"])
        self.assertFalse(captured["cloud"])
        self.assertFalse(captured["probe_runtime"])

        captured.clear()
        with (
            tempfile.TemporaryDirectory() as tmp,
            _cwd(Path(tmp)),
            mock.patch.object(doctor_cli, "run_doctor", side_effect=fake_run_doctor),
            mock.patch.object(doctor_cli, "board_startup_grace", return_value=None),
            redirect_stdout(StringIO()),
        ):
            code = doctor_cli.main(
                [
                    "--adoption",
                    "--orchestrator-only",
                    "--repo",
                    "owner/repo",
                    "--cloud",
                    "--probe-runtime",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(captured["cloud"])
        self.assertTrue(captured["probe_runtime"])

        captured.clear()
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / "code-mower.yml").write_text("version: 1\n", encoding="utf-8")
            with (
                _cwd(checkout),
                mock.patch.object(
                    doctor_cli, "run_doctor", side_effect=fake_run_doctor
                ),
                mock.patch.object(
                    doctor_cli, "board_startup_grace", return_value=None
                ),
                redirect_stdout(StringIO()),
            ):
                code = doctor_cli.main(
                    [
                        "--adoption",
                        "--orchestrator-only",
                        "--repo",
                        "owner/repo",
                        "--json",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertTrue(captured["cloud"])
        self.assertTrue(captured["probe_runtime"])
