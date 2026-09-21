from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from unittest import TestCase, mock

from code_mower import doctor
from code_mower.doctor_checks import (
    LOCAL_PATH_REDACTION,
    DoctorCheck,
    DoctorReport,
    doctor_report_payload,
    redact_local_path_text,
)


def _local_root() -> str:
    # Assemble the synthetic home prefix so the repository privacy scanner does
    # not mistake a regression fixture for a real developer path.
    return "/" + "Users/example-person/Private Project"


def _report() -> DoctorReport:
    root = _local_root()
    return DoctorReport(
        config_path=f"{root}/repo/code-mower.yml",
        provider_templates_path=f"{root}/package/templates/providers.yml",
        profile="recommended",
        checks=(
            DoctorCheck(
                name="runtime.python",
                status="pass",
                message=f"Python found at {root}/venv/bin/python: ready",
                detail={
                    "executable": f"{root}/venv/bin/python",
                    "workflow_paths": [
                        f"{root}/repo/.github/workflows/audit.yml",
                        r"C:\Users\example-person\repo\workflow.yml",
                    ],
                    "repository": "example-org/example-repo",
                    "documentation": "https://example.test/docs/install",
                    f"{root}/repo/private-keyed-path": "present",
                },
                remediation=f"inspect file://{root}/repo/doctor.log",
            ),
        ),
    )


class DoctorShareSafeTests(TestCase):
    def test_recursive_payload_preserves_schema_and_removes_local_paths(self) -> None:
        payload = doctor_report_payload(_report(), include_local_paths=False)
        rendered = json.dumps(payload)

        self.assertEqual(payload["local_paths"], "redacted")
        self.assertEqual(payload["config_path"], LOCAL_PATH_REDACTION)
        self.assertEqual(payload["provider_templates_path"], LOCAL_PATH_REDACTION)
        self.assertNotIn("example-person", rendered)
        self.assertNotIn("Private Project", rendered)
        self.assertNotIn("file://", rendered)
        self.assertNotIn("private-keyed-path", rendered)
        self.assertIn("example-org/example-repo", rendered)
        self.assertIn("https://example.test/docs/install", rendered)
        self.assertEqual(payload["checks"][0]["id"], "runtime.python")

    def test_include_local_paths_preserves_legacy_values(self) -> None:
        report = _report()
        payload = doctor_report_payload(report, include_local_paths=True)

        self.assertEqual(payload, report.as_dict())
        self.assertNotIn("local_paths", payload)
        self.assertEqual(payload["config_path"], report.config_path)
        self.assertEqual(
            payload["checks"][0]["detail"]["executable"],
            report.checks[0].detail["executable"],
        )

    def test_text_redaction_keeps_urls_and_repository_slugs(self) -> None:
        text = redact_local_path_text(
            "See https://example.test/a/b for example-org/example-repo; "
            f"failed at {_local_root()}/repo/config.yml: denied"
        )

        self.assertIn("https://example.test/a/b", text)
        self.assertIn("example-org/example-repo", text)
        self.assertIn(LOCAL_PATH_REDACTION, text)
        self.assertNotIn("example-person", text)

    def test_text_redaction_covers_authority_root_and_relative_path_matrix(self) -> None:
        private_paths = (
            "file://server/share/private/workflow.yml",
            "file://localhost/" + "Users/alice/private.yml",
            "file:///" + "Users/alice/private.yml",
            "FILE:/" + "Users/alice/private.yml",
            "confidential/config.yml",
            "./confidential/config.yml",
            "../confidential/config.yml",
            ".github/workflows/private-audit.yml",
            "private/workflows/audit",
            r"C:\Users\alice\private.yml",
            r"C:Users\alice\private.yml",
            r"\\server\share\private\workflow.yml",
            r"\Users\alice\private.yml",
            r"confidential\config.yml",
            "/秘密/config.yml",
            "~/confidential/config.yml",
            "~alice/confidential/config.yml",
        )
        for private_path in private_paths:
            with self.subTest(path=private_path):
                redacted = redact_local_path_text(f"failed at {private_path}: denied")
                self.assertIn(LOCAL_PATH_REDACTION, redacted)
                self.assertNotIn(private_path, redacted)

    def test_text_redaction_preserves_nonlocal_identifiers_and_urls(self) -> None:
        public_values = (
            "example-org/example-repo",
            "OWNER/REPO",
            "https://example.test/a/b",
            "http://example.test/a/b?next=/c/d",
            "ssh://git@example.test/owner/repo",
            "git@example.test:owner/repo",
        )
        for public_value in public_values:
            with self.subTest(value=public_value):
                self.assertEqual(redact_local_path_text(public_value), public_value)

    def test_text_redaction_preserves_diagnostics_after_an_embedded_path(self) -> None:
        text = redact_local_path_text(
            "workflow at .github/workflows/private-audit.yml failed after validation"
        )

        self.assertEqual(
            text,
            f"workflow at {LOCAL_PATH_REDACTION} failed after validation",
        )

    def test_space_containing_paths_are_hidden_without_truncating_following_text(self) -> None:
        unquoted = redact_local_path_text(
            f"failed at {_local_root()}/repo/config.yml because it was unreadable"
        )
        quoted = redact_local_path_text(
            f"checkout '{_local_root()}' is unavailable"
        )

        for redacted in (unquoted, quoted):
            self.assertNotIn("example-person", redacted)
            self.assertNotIn("Private Project", redacted)
        self.assertIn("because it was unreadable", unquoted)
        self.assertEqual(
            quoted,
            f"checkout {LOCAL_PATH_REDACTION} is unavailable",
        )

    def test_every_report_text_surface_uses_the_complete_path_matrix(self) -> None:
        report = DoctorReport(
            config_path="code-mower.yml",
            provider_templates_path="private/templates",
            profile="recommended",
            checks=(
                DoctorCheck(
                    name="path.matrix",
                    status="fail",
                    message="workflow at .github/workflows/private-audit.yml failed",
                    detail={
                        "executable": "bin/private-python",
                        "workflow_path": "confidential/workflow",
                        "template_paths": ["private/template"],
                        "nested": [
                            {"template": "/秘密/config.yml"},
                            "file://localhost/" + "Users/alice/private.yml",
                        ],
                        "repository": "OWNER/REPO",
                        "documentation": "https://example.test/a/b",
                    },
                    remediation=r"inspect C:\Users\alice\private.log",
                ),
            ),
        )

        payload = doctor_report_payload(report, include_local_paths=False)
        rendered = json.dumps(payload, ensure_ascii=False)

        for private_fragment in (
            "confidential",
            "server",
            ".github",
            "alice",
            "秘密",
            "file://",
        ):
            self.assertNotIn(private_fragment, rendered)
        self.assertIn("OWNER/REPO", rendered)
        self.assertIn("https://example.test/a/b", rendered)
        self.assertIn("workflow at", rendered)
        self.assertIn("failed", rendered)

    def test_adoption_json_defaults_to_share_safe_and_has_debug_opt_in(self) -> None:
        def run(extra: list[str]) -> dict[str, object]:
            stdout = StringIO()
            with (
                mock.patch.object(doctor, "run_doctor", return_value=_report()),
                mock.patch.object(
                    doctor,
                    "resolve_doctor_config_path",
                    return_value=Path(f"{_local_root()}/repo/code-mower.yml"),
                ),
                mock.patch.object(
                    doctor,
                    "resolve_doctor_provider_templates_path",
                    return_value=Path(f"{_local_root()}/package/templates/providers.yml"),
                ),
                mock.patch.object(doctor, "board_startup_grace", return_value=None),
                redirect_stdout(stdout),
            ):
                code = doctor.main(
                    [
                        "--adoption",
                        "--hosted-builders",
                        "--repo",
                        "example-org/example-repo",
                        "--json",
                        *extra,
                    ]
                )
            self.assertEqual(code, 0)
            return json.loads(stdout.getvalue())

        safe = run([])
        local = run(["--include-local-paths"])

        self.assertEqual(safe["local_paths"], "redacted")
        self.assertNotIn("example-person", json.dumps(safe))
        self.assertNotIn("local_paths", local)
        self.assertIn("example-person", json.dumps(local))

    def test_share_safe_flag_redacts_non_adoption_json(self) -> None:
        stdout = StringIO()
        with (
            mock.patch.object(doctor, "run_doctor", return_value=_report()),
            mock.patch.object(
                doctor,
                "resolve_doctor_config_path",
                return_value=Path(f"{_local_root()}/repo/code-mower.yml"),
            ),
            mock.patch.object(
                doctor,
                "resolve_doctor_provider_templates_path",
                return_value=Path(f"{_local_root()}/package/templates/providers.yml"),
            ),
            mock.patch.object(doctor, "board_startup_grace", return_value=None),
            redirect_stdout(stdout),
        ):
            code = doctor.main(["--json", "--share-safe"])

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["local_paths"], "redacted")
        self.assertNotIn("example-person", json.dumps(payload))

    def test_share_safe_config_error_hides_requested_path_and_cwd(self) -> None:
        message = doctor._doctor_config_error_message(
            ValueError(f"cannot read {_local_root()}/repo/code-mower.yml"),
            config_arg=f"{_local_root()}/repo/code-mower.yml",
            include_local_paths=False,
        )

        self.assertIn(LOCAL_PATH_REDACTION, message)
        self.assertNotIn("example-person", message)
