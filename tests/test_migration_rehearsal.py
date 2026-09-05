#!/usr/bin/env python3
"""Tests for the closed two-stage TestPyPI candidate install flow.

Pip does not prioritize --index-url over --extra-index-url, so a single
combined install command cannot prove a candidate came exclusively from one
index -- an identical version on the other configured index could silently
satisfy it. These tests cover the command sequence that instead (1)
downloads the candidate with a single index and --no-deps, verifies exactly
one matching artifact came back, then (2) installs that local artifact with
dependencies resolved from a separate index.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import migration_install
from code_mower import migration_rehearsal


class CandidateCommandBuilderTests(unittest.TestCase):
    """Pure pip-command construction for the two-stage candidate flow."""

    def test_download_command_names_only_the_candidate_index(self) -> None:
        command = migration_install._pip_download_candidate_command(
            Path("/venv/bin/python"),
            "code-mower==1.0.0",
            index_url="https://test.pypi.org/simple/",
            dest_dir=Path("/tmp/candidate"),
        )
        self.assertEqual(
            command,
            [
                "/venv/bin/python",
                "-m",
                "pip",
                "--isolated",
                "download",
                "--no-deps",
                "--index-url",
                "https://test.pypi.org/simple/",
                "--dest",
                "/tmp/candidate",
                "code-mower==1.0.0",
            ],
        )
        # No --extra-index-url anywhere: this is the whole point of the fix.
        self.assertNotIn("--extra-index-url", command)

    def test_candidate_environment_ignores_ambient_pip_indexes(self) -> None:
        with mock.patch.dict(
            migration_install.os.environ,
            {
                "PIP_INDEX_URL": "https://ambient.invalid/simple/",
                "PIP_EXTRA_INDEX_URL": "https://extra.invalid/simple/",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            env = migration_install._isolated_pip_environment()
        self.assertNotIn("PIP_INDEX_URL", env)
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)
        self.assertEqual(env["PIP_CONFIG_FILE"], migration_install.os.devnull)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_download_command_honors_no_cache(self) -> None:
        command = migration_install._pip_download_candidate_command(
            Path("/venv/bin/python"),
            "code-mower==1.0.0",
            index_url="https://test.pypi.org/simple/",
            dest_dir=Path("/tmp/candidate"),
            pip_no_cache=True,
        )
        self.assertIn("--no-cache-dir", command)

    def test_install_local_artifact_command_uses_a_path_not_a_spec(self) -> None:
        command = migration_install._pip_install_local_artifact_command(
            Path("/venv/bin/python"),
            Path("/tmp/candidate/code_mower-1.0.0-py3-none-any.whl"),
            dependency_index_url="https://pypi.org/simple/",
        )
        self.assertEqual(
            command,
            [
                "/venv/bin/python",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--index-url",
                "https://pypi.org/simple/",
                "/tmp/candidate/code_mower-1.0.0-py3-none-any.whl",
            ],
        )
        # The candidate's own identity is a local file, not a name==version
        # spec subject to index resolution.
        self.assertNotIn("code-mower==1.0.0", command)
        self.assertIn("--isolated", command)

    def test_install_local_artifact_command_without_dependency_index_has_no_override(
        self,
    ) -> None:
        command = migration_install._pip_install_local_artifact_command(
            Path("/venv/bin/python"),
            Path("/tmp/candidate/code_mower-1.0.0-py3-none-any.whl"),
        )
        self.assertNotIn("--index-url", command)


class ParseExactNameVersionSpecTests(unittest.TestCase):
    def test_parses_name_and_version(self) -> None:
        self.assertEqual(
            migration_install._parse_exact_name_version_spec("code-mower==1.0.0"),
            ("code-mower", "1.0.0"),
        )

    def test_normalizes_pep503_identity(self) -> None:
        identity, version = migration_install._parse_exact_name_version_spec(
            "Code_Mower==1.0.0"
        )
        self.assertEqual(identity, "code-mower")
        self.assertEqual(version, "1.0.0")

    def test_rejects_version_range(self) -> None:
        with self.assertRaises(ValueError):
            migration_install._parse_exact_name_version_spec("code-mower>=1.0.0")

    def test_rejects_extras(self) -> None:
        with self.assertRaises(ValueError):
            migration_install._parse_exact_name_version_spec("code-mower[extra]==1.0.0")

    def test_rejects_local_path(self) -> None:
        with self.assertRaises(ValueError):
            migration_install._parse_exact_name_version_spec("./dist/code_mower-1.0.0.whl")


class ParseDownloadedArtifactIdentityTests(unittest.TestCase):
    def test_parses_wheel_filename(self) -> None:
        self.assertEqual(
            migration_install._parse_downloaded_artifact_identity(
                "code_mower-1.0.0-py3-none-any.whl"
            ),
            ("code-mower", "1.0.0"),
        )

    def test_parses_sdist_filename(self) -> None:
        self.assertEqual(
            migration_install._parse_downloaded_artifact_identity(
                "code_mower-1.0.0.tar.gz"
            ),
            ("code-mower", "1.0.0"),
        )

    def test_parses_prerelease_wheel_version(self) -> None:
        self.assertEqual(
            migration_install._parse_downloaded_artifact_identity(
                "code_mower-1.0.0rc1-py3-none-any.whl"
            ),
            ("code-mower", "1.0.0rc1"),
        )

    def test_rejects_unrecognized_filename(self) -> None:
        with self.assertRaises(ValueError):
            migration_install._parse_downloaded_artifact_identity("not-a-package-file.txt")

    def test_rejects_empty_filename(self) -> None:
        with self.assertRaises(ValueError):
            migration_install._parse_downloaded_artifact_identity("")


def _fake_run_rehearsal_step_factory(*, artifacts=None):
    """Build a fake `_run_rehearsal_step` that writes `artifacts` on download."""

    def _fake(command, *, cwd, env, steps, timeout):
        steps.append(
            {
                "command": list(command),
                "cwd": str(cwd),
                "returncode": 0,
                "stdout_preview": "",
                "stderr_preview": "",
            }
        )
        if "download" in command and artifacts is not None:
            dest = Path(command[command.index("--dest") + 1])
            for name in artifacts:
                (dest / name).write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return _fake


class TwoStageCandidateInstallTests(unittest.TestCase):
    """`_run_two_stage_candidate_install`: command sequence and fail-closed cases."""

    def _run(self, *, artifacts, work_dir: Path) -> list[dict]:
        steps: list[dict] = []
        fake = _fake_run_rehearsal_step_factory(artifacts=artifacts)
        with mock.patch.object(migration_install, "_run_rehearsal_step", side_effect=fake):
            migration_rehearsal._run_two_stage_candidate_install(
                venv_python=Path("/venv/bin/python"),
                package_spec="code-mower==1.0.0",
                candidate_index_url="https://test.pypi.org/simple/",
                dependency_index_url="https://pypi.org/simple/",
                candidate_dir=work_dir / "testpypi-candidate",
                work_dir=work_dir,
                steps=steps,
                timeout=60,
                attempts=1,
                retry_delay_seconds=0,
                pip_no_cache=False,
            )
        return steps

    def test_success_runs_download_then_local_install_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            steps = self._run(
                artifacts=["code_mower-1.0.0-py3-none-any.whl"],
                work_dir=Path(tmp),
            )
        self.assertEqual(len(steps), 2)
        download_command = steps[0]["command"]
        install_command = steps[1]["command"]

        self.assertIn("download", download_command)
        self.assertIn("--index-url", download_command)
        self.assertIn("https://test.pypi.org/simple/", download_command)
        self.assertNotIn("--extra-index-url", download_command)
        self.assertIn("--no-deps", download_command)

        self.assertIn("install", install_command)
        self.assertIn("--index-url", install_command)
        self.assertIn("https://pypi.org/simple/", install_command)
        self.assertNotIn("--extra-index-url", install_command)
        self.assertTrue(install_command[-1].endswith("code_mower-1.0.0-py3-none-any.whl"))

    def test_fails_closed_on_zero_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(migration_rehearsal.RehearsalError) as ctx:
                self._run(artifacts=[], work_dir=Path(tmp))
        self.assertIn("no candidate artifact", str(ctx.exception))

    def test_fails_closed_on_multiple_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(migration_rehearsal.RehearsalError) as ctx:
                self._run(
                    artifacts=[
                        "code_mower-1.0.0-py3-none-any.whl",
                        "code_mower-1.0.0.tar.gz",
                    ],
                    work_dir=Path(tmp),
                )
        self.assertIn("exactly one candidate artifact", str(ctx.exception))

    def test_fails_closed_on_malformed_artifact_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(migration_rehearsal.RehearsalError) as ctx:
                self._run(artifacts=["not-a-package-file.txt"], work_dir=Path(tmp))
        self.assertIn("unrecognized filename", str(ctx.exception))

    def test_fails_closed_on_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(migration_rehearsal.RehearsalError) as ctx:
                self._run(
                    artifacts=["code_mower-9.9.9-py3-none-any.whl"],
                    work_dir=Path(tmp),
                )
        self.assertIn("does not match the requested", str(ctx.exception))

    def test_fails_closed_on_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(migration_rehearsal.RehearsalError) as ctx:
                self._run(
                    artifacts=["other-package-1.0.0-py3-none-any.whl"],
                    work_dir=Path(tmp),
                )
        self.assertIn("does not match the requested", str(ctx.exception))

    def test_rejects_non_exact_package_spec_before_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            steps: list[dict] = []
            fake = _fake_run_rehearsal_step_factory(artifacts=None)
            with mock.patch.object(migration_install, "_run_rehearsal_step", side_effect=fake):
                with self.assertRaises(ValueError):
                    migration_rehearsal._run_two_stage_candidate_install(
                        venv_python=Path("/venv/bin/python"),
                        package_spec="code-mower>=1.0.0",
                        candidate_index_url="https://test.pypi.org/simple/",
                        dependency_index_url="https://pypi.org/simple/",
                        candidate_dir=work_dir / "testpypi-candidate",
                        work_dir=work_dir,
                        steps=steps,
                        timeout=60,
                        attempts=1,
                        retry_delay_seconds=0,
                        pip_no_cache=False,
                    )
            # Fails before any command runs: no download of an inexact spec.
            self.assertEqual(steps, [])


class RunPackageInstallRehearsalCandidateWiringTests(unittest.TestCase):
    """`run_package_install_rehearsal` routes to the two-stage flow only when asked."""

    def test_candidate_index_url_routes_to_two_stage_install(self) -> None:
        with mock.patch.object(
            migration_rehearsal, "_run_two_stage_candidate_install"
        ) as mock_two_stage:
            with mock.patch.object(migration_rehearsal, "_run_rehearsal_step") as mock_step:
                mock_step.return_value.stdout = "code-mower 1.0.0"
                with mock.patch.object(
                    migration_rehearsal, "_write_public_rehearsal_toy_repo"
                ):
                    with mock.patch.object(
                        migration_rehearsal, "_first_user_readiness_scorecard"
                    ) as mock_readiness:
                        mock_readiness.return_value = {"status": "pass"}
                        with tempfile.TemporaryDirectory() as tmp:
                            try:
                                migration_rehearsal.run_package_install_rehearsal(
                                    package_spec="code-mower==1.0.0",
                                    work_dir=Path(tmp) / "work",
                                    allow_package_index=True,
                                    candidate_index_url="https://test.pypi.org/simple/",
                                    candidate_dependency_index_url="https://pypi.org/simple/",
                                )
                            except Exception:
                                # The rest of the rehearsal (toy-repo steps, CLI
                                # invocations) is out of scope for this test --
                                # only the routing to the two-stage install matters.
                                pass
        mock_two_stage.assert_called_once()
        self.assertEqual(
            mock_two_stage.call_args.kwargs["candidate_index_url"],
            "https://test.pypi.org/simple/",
        )
        self.assertEqual(
            mock_two_stage.call_args.kwargs["dependency_index_url"],
            "https://pypi.org/simple/",
        )

    def test_no_candidate_index_url_does_not_use_two_stage_install(self) -> None:
        with mock.patch.object(
            migration_rehearsal, "_run_two_stage_candidate_install"
        ) as mock_two_stage:
            with mock.patch.object(migration_rehearsal, "_run_rehearsal_step") as mock_step:
                mock_step.return_value.stdout = "code-mower 1.0.0"
                with mock.patch.object(
                    migration_rehearsal, "_write_public_rehearsal_toy_repo"
                ):
                    with mock.patch.object(
                        migration_rehearsal, "_first_user_readiness_scorecard"
                    ) as mock_readiness:
                        mock_readiness.return_value = {"status": "pass"}
                        with tempfile.TemporaryDirectory() as tmp:
                            try:
                                migration_rehearsal.run_package_install_rehearsal(
                                    package_spec="code-mower==1.0.0",
                                    work_dir=Path(tmp) / "work",
                                    allow_package_index=True,
                                )
                            except Exception:
                                pass
        mock_two_stage.assert_not_called()

    def test_candidate_index_url_requires_allow_package_index(self) -> None:
        with self.assertRaises(ValueError):
            migration_rehearsal.run_package_install_rehearsal(
                package_spec="code-mower==1.0.0",
                candidate_index_url="https://test.pypi.org/simple/",
                allow_package_index=False,
            )

    def test_candidate_index_url_requires_exact_package_index_spec(self) -> None:
        with self.assertRaises(ValueError):
            migration_rehearsal.run_package_install_rehearsal(
                package_spec="./local/path",
                candidate_index_url="https://test.pypi.org/simple/",
                allow_package_index=True,
            )

    def test_work_dir_must_be_clean_of_candidate_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "work"
            (work_dir / "testpypi-candidate").mkdir(parents=True)
            with self.assertRaises(ValueError) as ctx:
                migration_rehearsal.run_package_install_rehearsal(
                    package_spec="code-mower==1.0.0",
                    work_dir=work_dir,
                    allow_package_index=True,
                    candidate_index_url="https://test.pypi.org/simple/",
                )
        self.assertIn("not clean", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
