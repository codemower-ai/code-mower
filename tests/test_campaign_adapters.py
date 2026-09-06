#!/usr/bin/env python3
"""Tests for the maintained release-campaign provider adapters.

Every provider invocation runs through an injected ``provider_runner`` mock:
these tests never execute a real provider CLI, spend, or touch the network.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import campaign_adapters, release_campaigns, release_qualify
from code_mower.provider_registry import REFERENCE_PROVIDERS


def _adoption_result(provider: str = "codex", **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": "v1.0.0",
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.0",
        "provider": provider,
        "executor": provider,
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 12.34,
        "outcome": "pass",
        "steps": [
            {
                "id": "package_install",
                "status": "pass",
                "elapsed_seconds": 12.34,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        ],
    }
    result.update(overrides)
    return result


def _schema_keywords(obj: object) -> set[str]:
    """Recursively collect every string key appearing in a nested JSON-like dict."""
    seen: set[str] = set()

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                seen.add(k)
                _walk(v)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(obj)
    return seen


def _fake_bin(tmp: Path, name: str = "provider-bin") -> str:
    bin_path = tmp / name
    bin_path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--help\" ]; then\n"
        "  echo \"  --new-project  Create a new project\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)
    return str(bin_path)


def _ok(
    argv: Any, *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr=stderr)


class ArgvBuilderTests(unittest.TestCase):
    """Each maintained transport builds its verified noninteractive surface."""

    def test_codex_argv_uses_writable_ephemeral_workspace(self) -> None:
        argv = campaign_adapters.build_codex_argv(
            codex_bin="/bin/codex",
            schema_path="/tmp/schema.json",
            last_message_path="/tmp/last.md",
            workdir="/tmp/work",
            model="gpt-5",
        )
        self.assertEqual(argv[0], "/bin/codex")
        self.assertIn("exec", argv)
        self.assertIn("--json", argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("--output-last-message", argv)
        self.assertIn("--model", argv)
        self.assertEqual(argv[-1], "-")  # prompt travels on stdin
        # Disposable writable workspace with narrowly scoped network access:
        # the agent must be able to create a virtualenv and install the
        # release, while the run stays ephemeral and isolated.
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("read-only", argv)
        self.assertNotIn("danger-full-access", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--approve-for-me", argv)
        self.assertIn("-c", argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        self.assertIn("-C", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertNotIn("--ignore-user-config", argv)
        no_model = campaign_adapters.build_codex_argv(
            codex_bin="/bin/codex",
            schema_path="/tmp/schema.json",
            last_message_path="/tmp/last.md",
            workdir="/tmp/work",
        )
        self.assertNotIn("--model", no_model)

    def test_claude_argv_grants_bash_only_inside_strict_sandbox(self) -> None:
        argv = campaign_adapters.build_claude_argv(
            claude_bin="/bin/claude",
            model="sonnet",
            max_budget_usd="5.00",
            schema_json="{}",
            workspace_dir="/tmp/work",
        )
        self.assertEqual(argv[0], "/bin/claude")
        for flag in (
            "--print",
            "--output-format",
            "json",
            "--model",
            "sonnet",
            "--max-budget-usd",
            "5.00",
            "--json-schema",
        ):
            self.assertIn(flag, argv)
        self.assertIn("--no-session-persistence", argv)
        # Exactly one tool (Bash), auto-approved only by the OS-level sandbox;
        # nothing receives a broad permission allow-rule.
        tools_idx = argv.index("--tools")
        self.assertEqual(argv[tools_idx + 1], "Bash")
        self.assertNotIn("", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertIn("--restricted", argv)
        self.assertNotIn("--permission-mode", argv)
        settings_idx = argv.index("--settings")
        settings = json.loads(argv[settings_idx + 1])
        self.assertEqual(settings["permissions"]["allow"], ["Bash"])
        sandbox = settings["sandbox"]
        self.assertIs(sandbox["enabled"], True)
        self.assertIs(sandbox["failIfUnavailable"], True)
        self.assertIs(sandbox["autoAllowBashIfSandboxed"], True)
        self.assertIs(sandbox["allowUnsandboxedCommands"], False)
        self.assertEqual(sandbox["filesystem"]["denyRead"], ["~"])
        self.assertEqual(sandbox["filesystem"]["denyWrite"], ["~"])
        self.assertEqual(
            sandbox["network"]["allowedDomains"],
            ["pypi.org", "files.pythonhosted.org"],
        )
        # Claude's workspace is explicit even though no extra path is granted.
        add_dir_idx = argv.index("--add-dir")
        self.assertEqual(argv[add_dir_idx + 1], "/tmp/work")
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("--strict-mcp-config", argv)
        unscoped = campaign_adapters.build_claude_argv(
            claude_bin="/bin/claude",
            model="sonnet",
            max_budget_usd="5.00",
            schema_json="{}",
        )
        self.assertNotIn("--add-dir", unscoped)

    def test_antigravity_argv_uses_prompt_file_and_timeout(self) -> None:
        argv = campaign_adapters.build_antigravity_argv(
            agy_bin="/bin/agy",
            prompt_file="/tmp/work/ws/campaign.prompt-input.txt",
            workspace_dir="/tmp/work/ws",
            timeout_seconds=870,
            model="gemini-3",
        )
        self.assertEqual(argv[0], "/bin/agy")
        self.assertIn("--print", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--new-project", argv)
        self.assertIn("--print-timeout", argv)
        self.assertIn("870s", argv)
        self.assertIn("--model", argv)
        self.assertIn("/tmp/work/ws/campaign.prompt-input.txt", argv[-1])

    def test_antigravity_argv_isolation_contract_no_continue_or_resume_semantics(self) -> None:
        argv = campaign_adapters.build_antigravity_argv(
            agy_bin="/bin/agy",
            prompt_file="/tmp/work/ws/campaign.prompt-input.txt",
            workspace_dir="/tmp/work/ws",
            timeout_seconds=870,
            model="gemini-3",
        )
        self.assertEqual(argv[0], "/bin/agy")
        self.assertIn("--sandbox", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--new-project", argv)
        add_dir_idx = argv.index("--add-dir")
        self.assertEqual(argv[add_dir_idx + 1], "/tmp/work/ws")
        timeout_idx = argv.index("--print-timeout")
        self.assertEqual(argv[timeout_idx + 1], "870s")
        self.assertIn("--print", argv)

        # Proving every run starts a new project and does not use continue or resume semantics:
        self.assertNotIn("--continue", argv)
        self.assertNotIn("-c", argv)
        self.assertNotIn("--conversation", argv)
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--prompt-interactive", argv)
        self.assertNotIn("-i", argv)

    def test_devin_argv_uses_prompt_file_and_sandbox(self) -> None:
        """Workspace scope comes from subprocess cwd; do not invent a --workspace flag."""
        argv = campaign_adapters.build_devin_argv(
            devin_bin="/bin/devin",
            prompt_file="campaign.prompt-input.txt",
            model="opus",
        )
        self.assertEqual(argv[0], "/bin/devin")
        self.assertIn("--print", argv)
        self.assertIn("--prompt-file", argv)
        self.assertIn("campaign.prompt-input.txt", argv)
        self.assertIn("--respect-workspace-trust", argv)
        self.assertIn("false", argv)
        self.assertIn("--model", argv)
        self.assertIn("opus", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("autonomous", argv)
        # Least-permissive unattended posture; no conversation export, no resume,
        # no interactive prompt, no positional prompt argument, and no unsupported
        # workspace flag.
        self.assertNotIn("--export", argv)
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--workspace", argv)
        self.assertNotIn("--", argv)

    def test_devin_argv_defaults_to_swe_when_no_model_is_given(self) -> None:
        argv = campaign_adapters.build_devin_argv(
            devin_bin="/bin/devin",
            prompt_file="campaign.prompt-input.txt",
        )
        model_idx = argv.index("--model")
        self.assertEqual(argv[model_idx + 1], "swe")

    def test_devin_argv_prefers_code_mower_model_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "CODE_MOWER_DEVIN_CLI_MODEL": "code-mower-devin",
                "DEVIN_CLI_MODEL": "devin-cli",
                "DEVIN_MODEL": "devin",
            },
            clear=False,
        ):
            argv = campaign_adapters.build_devin_argv(
                devin_bin="/bin/devin",
                prompt_file="campaign.prompt-input.txt",
            )
        model_idx = argv.index("--model")
        self.assertEqual(argv[model_idx + 1], "code-mower-devin")

    def test_devin_prompt_adds_shell_only_file_write_constraint(self) -> None:
        """The shell-only file-write workaround is appended only for Devin CLI."""
        prompt = campaign_adapters.build_qualification_prompt(
            provider="devin_cli",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
        )
        self.assertIn("shell commands only", prompt)

    def test_non_devin_prompts_omit_shell_only_file_write_constraint(self) -> None:
        """Do not constrain Codex, Claude, Antigravity, or Muse prompts."""
        for provider in ("codex", "claude", "antigravity", "muse"):
            with self.subTest(provider=provider):
                prompt = campaign_adapters.build_qualification_prompt(
                    provider=provider,
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    package_identity="code-mower",
                    normalized_version="1.0.0",
                    qualification_context="cold_install",
                    starting_version="",
                )
                self.assertNotIn("shell commands only", prompt)

    def test_shared_prompt_pins_virtualenv_interpreter_commands(self) -> None:
        prompt = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
        )
        self.assertIn("python3 -m venv .venv", prompt)
        self.assertIn("- executor: codex", prompt)
        self.assertIn(".venv/bin/python -m pip install", prompt)
        self.assertIn("importlib.metadata.version", prompt)
        self.assertIn(".venv/bin/code-mower doctor --help", prompt)
        self.assertIn("without a leading v", prompt)
        self.assertIn("all-pass steps require outcome pass", prompt)
        # The shell-only file-write constraint is a Devin Autonomous-mode
        # workaround and must not leak into other provider prompts.
        self.assertNotIn("shell commands only", prompt)

    def test_shared_prompt_quotes_target_interpreter_with_spaces_and_metacharacters(self) -> None:
        """Target interpreter path containing spaces or shell metacharacters must be safely quoted."""
        space_py = "/opt/Python 3.12/bin/python3"
        prompt_cold = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
            python_bin=space_py,
        )
        self.assertIn(f"`{shlex.quote(space_py)} -m venv .venv`", prompt_cold)
        self.assertIn("'/opt/Python 3.12/bin/python3' -m venv .venv", prompt_cold)

        prompt_upgrade = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="upgrade",
            starting_version="0.9.0",
            python_bin=space_py,
        )
        self.assertIn(f"`{shlex.quote(space_py)} -m venv .venv`", prompt_upgrade)

        meta_py = '/opt/py$env;`test`/bin/python3'
        prompt_meta = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
            python_bin=meta_py,
        )
        self.assertIn(f"`{shlex.quote(meta_py)} -m venv .venv`", prompt_meta)

    def test_shared_prompt_teaches_step_id_taxonomy(self) -> None:
        prompt = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
        )
        for step_id in ("board", "doctor", "lanes_status", "overhead", "package_install"):
            self.assertIn(step_id, prompt)
        self.assertIn("<namespace>__<name>", prompt)

    def test_guidance_schema_enforces_step_id_taxonomy(self) -> None:
        step_id_schema = campaign_adapters.ADOPTION_RESULT_JSON_SCHEMA[
            "properties"
        ]["steps"]["items"]["properties"]["id"]
        pattern = re.compile(step_id_schema["pattern"])
        for step_id in (
            "board",
            "doctor",
            "lanes_status",
            "overhead",
            "package_install",
            "codex__smoke",
        ):
            self.assertTrue(pattern.match(step_id), step_id)
        for step_id in (
            "install",
            "verify_cli",
            "notabuiltinid",
            "Codex__smoke",
            "__smoke",
            "codex__",
        ):
            self.assertFalse(pattern.match(step_id), step_id)

    def test_guidance_schema_matches_failure_reason_conditions(self) -> None:
        step_schema = campaign_adapters.ADOPTION_RESULT_JSON_SCHEMA["properties"][
            "steps"
        ]["items"]
        self.assertEqual(
            step_schema["if"],
            {
                "properties": {
                    "id": {"const": "package_install"},
                    "status": {"const": "fail"},
                },
                "required": ["id", "status"],
            },
        )
        self.assertEqual(
            step_schema["else"],
            {"properties": {"failure_reason": False}},
        )

    def test_prompt_schema_taxonomy_matches_validator(self) -> None:
        taught = [
            "board",
            "doctor",
            "lanes_status",
            "overhead",
            "package_install",
            "codex__smoke",
        ]
        steps = [
            {
                "id": step_id,
                "status": "pass",
                "elapsed_seconds": 1.0,
                "warning_count": 0,
                "owner_action_count": 0,
            }
            for step_id in taught
        ]
        release_qualify.validate_adoption_result_payload(
            _adoption_result(steps=steps, elapsed_seconds=6.0)
        )
        for step_id in ("install", "verify_cli", "notabuiltinid"):
            with self.subTest(step_id=step_id):
                with self.assertRaisesRegex(ValueError, "namespaced"):
                    release_qualify.validate_adoption_result_payload(
                        _adoption_result(
                            steps=[
                                {
                                    "id": step_id,
                                    "status": "pass",
                                    "elapsed_seconds": 5.0,
                                    "warning_count": 0,
                                    "owner_action_count": 0,
                                }
                            ],
                            elapsed_seconds=5.0,
                        )
                    )

    def test_muse_argv_uses_exec_with_prompt_file_and_workspace(self) -> None:
        argv = campaign_adapters.build_muse_argv(
            muse_bin="/bin/muse",
            prompt_file="/tmp/work/ws/campaign.prompt-input.txt",
            workspace_dir="/tmp/work/ws",
            max_model_steps=12,
            model="muse",
            reasoning_effort="high",
        )
        self.assertEqual(argv[0], "/bin/muse")
        for flag in (
            "exec",
            "--json",
            "--prompt-file",
            "--workspace",
            "--provider",
            "meta",
            "--approval-mode",
            "never",
            "--max-model-steps",
            "12",
            "--model",
            "muse",
            "--reasoning-effort",
            "high",
        ):
            self.assertIn(flag, argv)
        self.assertNotIn("--disable-shell", argv)
        self.assertNotIn("--disable-write", argv)
        self.assertIn("--disable-web-tools", argv)
        self.assertIn("--no-foreign-personal-context", argv)
        self.assertIn("--no-session-log", argv)


class PackageSourceTests(unittest.TestCase):
    """Local adapters receive the same closed `package_source` contract."""

    def test_claude_argv_defaults_to_pypi_domains(self) -> None:
        argv = campaign_adapters.build_claude_argv(
            claude_bin="/bin/claude",
            model="sonnet",
            max_budget_usd="5.00",
            schema_json="{}",
        )
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(
            settings["sandbox"]["network"]["allowedDomains"],
            ["pypi.org", "files.pythonhosted.org"],
        )

    def test_claude_argv_adds_testpypi_domains_for_testpypi_source(self) -> None:
        argv = campaign_adapters.build_claude_argv(
            claude_bin="/bin/claude",
            model="sonnet",
            max_budget_usd="5.00",
            schema_json="{}",
            package_source="testpypi",
        )
        settings = json.loads(argv[argv.index("--settings") + 1])
        allowed = settings["sandbox"]["network"]["allowedDomains"]
        self.assertIn("test.pypi.org", allowed)
        self.assertIn("test-files.pythonhosted.org", allowed)
        # Production PyPI stays allowed too: a TestPyPI candidate's
        # dependencies still resolve from the production extra index.
        self.assertIn("pypi.org", allowed)
        self.assertIn("files.pythonhosted.org", allowed)

    def test_prompt_names_pip_default_index_for_pypi_source(self) -> None:
        prompt = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
        )
        self.assertIn("- package_source: pypi", prompt)
        self.assertNotIn("--index-url", prompt)
        self.assertNotIn("test.pypi.org", prompt)

    def test_prompt_names_canonical_testpypi_index_for_testpypi_source(self) -> None:
        prompt = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
            package_source="testpypi",
        )
        self.assertIn("- package_source: testpypi", prompt)
        self.assertIn("https://test.pypi.org/simple/", prompt)
        self.assertIn("https://pypi.org/simple/", prompt)
        self.assertIn("download --no-deps --no-cache-dir", prompt)
        self.assertIn("exactly one wheel or source archive", prompt)
        self.assertIn("candidate/<verified-artifact>", prompt)
        self.assertNotIn("--extra-index-url \"https://pypi.org/simple/\"", prompt)

    def test_prompt_names_canonical_testpypi_index_for_upgrade_context(self) -> None:
        prompt = campaign_adapters.build_qualification_prompt(
            provider="codex",
            release_tag="v1.0.1",
            package_spec="code-mower==1.0.1",
            package_identity="code-mower",
            normalized_version="1.0.1",
            qualification_context="upgrade",
            starting_version="1.0.0",
            package_source="testpypi",
        )
        self.assertIn("https://test.pypi.org/simple/", prompt)
        self.assertEqual(prompt.count("https://test.pypi.org/simple/"), 1)
        self.assertIn("Install the starting version from production PyPI", prompt)
        self.assertIn("code-mower==1.0.0", prompt)
        self.assertNotIn("--extra-index-url ''", prompt)
        self.assertNotIn("--find-links ''", prompt)
        self.assertIn("Do not provide a non-empty `--extra-index-url`", prompt)

    def test_check_campaign_identity_rejects_unknown_source(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            campaign_adapters._check_campaign_identity(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                qualification_context="cold_install",
                starting_version="",
                package_source="bogus",
            )
        self.assertIn("package_source must be one of", str(ctx.exception))

    def test_cli_accepts_package_source_flag(self) -> None:
        parser = campaign_adapters.build_parser()
        args = parser.parse_args(
            [
                "--provider",
                "codex",
                "--provider-bin",
                "/bin/codex",
                "--release-tag",
                "v1.0.0",
                "--package-spec",
                "code-mower==1.0.0",
                "--qualification-context",
                "cold_install",
                "--output",
                "/tmp/out.json",
                "--package-source",
                "testpypi",
            ]
        )
        self.assertEqual(args.package_source, "testpypi")

    def test_cli_defaults_package_source_to_pypi(self) -> None:
        parser = campaign_adapters.build_parser()
        args = parser.parse_args(
            [
                "--provider",
                "codex",
                "--provider-bin",
                "/bin/codex",
                "--release-tag",
                "v1.0.0",
                "--package-spec",
                "code-mower==1.0.0",
                "--qualification-context",
                "cold_install",
                "--output",
                "/tmp/out.json",
            ]
        )
        self.assertEqual(args.package_source, "pypi")



class SchemaCompatibilityTests(unittest.TestCase):
    """Provider response-format schemas stay inside the structured-output keyword subset."""

    def test_codex_response_format_schema_has_no_unsupported_keywords(self) -> None:
        schema = campaign_adapters.build_codex_response_format_schema()
        keywords = _schema_keywords(schema)
        unsupported = {"if", "then", "else", "not", "oneOf"}
        self.assertFalse(
            keywords & unsupported,
            "Codex response schema contains unsupported structured-output keywords: "
            f"{sorted(keywords & unsupported)}",
        )
        # The relaxed schema still allows failure_reason so a failed package_install
        # can communicate the closed reason; local validation rejects misuse.
        self.assertIn(
            "failure_reason",
            schema["properties"]["steps"]["items"]["properties"],
        )

    def test_full_schema_retains_conditional_failure_reason_rules(self) -> None:
        step_schema = campaign_adapters.ADOPTION_RESULT_JSON_SCHEMA["properties"][
            "steps"
        ]["items"]
        self.assertIn("if", step_schema)
        self.assertIn("then", step_schema)
        self.assertIn("else", step_schema)

class AdapterTransportTests(unittest.TestCase):
    """End-to-end adapter runs with a mocked provider subprocess."""

    def test_provider_child_environment_drops_unrelated_secrets(self) -> None:
        secret_names = (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "CODE_MOWER_CLOUD_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "META_API_KEY",
        )
        ambient = {name: f"secret-{name}" for name in secret_names}
        ambient.update(
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp/provider-home",
                "XDG_CONFIG_HOME": "/tmp/provider-xdg-config",
                "CODEX_HOME": "/tmp/codex-home",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            }
        )

        with mock.patch.dict(os.environ, ambient, clear=True):
            for provider in ("codex", "claude", "antigravity", "muse", "devin_cli"):
                with self.subTest(provider=provider):
                    codex_home = Path("/tmp/code-mower-codex-campaign-home")
                    child_env = campaign_adapters.build_adapter_child_env(
                        provider, codex_home=codex_home
                    )
                    self.assertEqual(child_env["PATH"], ambient["PATH"])
                    self.assertEqual(
                        child_env["DBUS_SESSION_BUS_ADDRESS"],
                        ambient["DBUS_SESSION_BUS_ADDRESS"],
                    )
                    self.assertEqual(child_env["XDG_RUNTIME_DIR"], ambient["XDG_RUNTIME_DIR"])
                    if provider == "codex":
                        # macOS keyring discovery needs the real OS home even
                        # though Codex config and state stay isolated.
                        self.assertEqual(child_env["HOME"], ambient["HOME"])
                        self.assertEqual(child_env["CODEX_HOME"], str(codex_home.resolve()))
                        self.assertNotEqual(child_env["HOME"], child_env["CODEX_HOME"])
                        self.assertNotIn("XDG_CONFIG_HOME", child_env)
                    else:
                        self.assertEqual(child_env["HOME"], ambient["HOME"])
                        self.assertEqual(
                            child_env["XDG_CONFIG_HOME"], ambient["XDG_CONFIG_HOME"]
                        )
                        self.assertNotIn("CODEX_HOME", child_env)
                    for secret_name in secret_names:
                        self.assertNotIn(secret_name, child_env)

    def test_codex_campaign_home_is_restricted_and_contains_no_file_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            prepared = campaign_adapters.prepare_codex_campaign_home(home)

            self.assertEqual(prepared, home.resolve())
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('cli_auth_credentials_store = "keyring"', config)
            self.assertIn('default_permissions = "campaign"', config)
            self.assertIn('\":root\" = \"deny\"', config)
            self.assertIn('\":workspace_roots\" = \"write\"', config)
            self.assertNotIn("token", config.lower())
            self.assertFalse((home / "auth.json").exists())

    def test_codex_campaign_home_refuses_readable_file_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            home.mkdir()
            (home / "auth.json").write_text('{"secret":"canary"}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "readable file credentials"):
                campaign_adapters.prepare_codex_campaign_home(home)

    def _run(
        self,
        provider: str,
        runner: Any,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 870,
        model: str = "",
    ) -> tuple[int, Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        output = tmp / "result.json"
        with mock.patch.dict(os.environ, env or {}, clear=False):
            code = campaign_adapters.run_campaign_adapter(
                provider=provider,
                provider_bin=_fake_bin(tmp),
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                qualification_context="cold_install",
                starting_version="",
                output_path=output,
                timeout_seconds=timeout_seconds,
                codex_home=tmp / "codex-home",
                provider_runner=runner,
                model=model,
            )
        return code, output, tmp

    def test_codex_reads_last_message_file_and_ignores_stdout(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            seen["argv"] = list(argv)
            seen["prompt"] = prompt_input
            seen["timeout"] = timeout
            seen["env"] = child_env
            # Secret-looking stdout must never reach the output file: the
            # candidate comes only from the provider-written message file.
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(json.dumps(_adoption_result("codex")), encoding="utf-8")
            return subprocess.CompletedProcess(
                list(argv), 0, stdout="sk-antigravity SECRET", stderr=""
            )

        code, output, _tmp = self._run("codex", runner)
        self.assertEqual(code, 0)
        self.assertIsNotNone(seen["prompt"])
        self.assertIn("exec", seen["argv"])
        self.assertIn("PATH", seen["env"])
        self.assertTrue(output.is_file())
        stored = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(stored, _adoption_result("codex"))
        self.assertNotIn("SECRET", output.read_text(encoding="utf-8"))

    def test_codex_writes_api_compatible_schema_without_conditionals(self) -> None:
        """The schema file passed to Codex must not use structured-output unsupported keywords."""
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            schema_path = Path(argv[argv.index("--output-schema") + 1])
            seen["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(json.dumps(_adoption_result("codex")), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, output, _tmp = self._run("codex", runner)
        self.assertEqual(code, 0)
        keywords = _schema_keywords(seen["schema"])
        unsupported = {"if", "then", "else", "not", "oneOf"}
        self.assertFalse(
            keywords & unsupported,
            "Schema written for Codex contains unsupported structured-output keywords: "
            f"{sorted(keywords & unsupported)}",
        )
        self.assertTrue(output.is_file())

    def test_claude_reads_structured_output_envelope(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            seen["argv"] = list(argv)
            seen["prompt"] = prompt_input
            seen["timeout"] = timeout
            seen["env"] = child_env
            stdout = json.dumps(
                {
                    "is_error": False,
                    "structured_output": _adoption_result("claude"),
                    "transcript_noise": "sk-test SECRET-NOISE",
                }
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout=stdout, stderr="")

        code, output, _tmp = self._run("claude", runner)
        self.assertEqual(code, 0)
        self.assertIsNotNone(seen["prompt"])
        self.assertIn("--print", seen["argv"])
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), _adoption_result("claude"))
        self.assertNotIn("SECRET-NOISE", output.read_text(encoding="utf-8"))

    def test_claude_rejects_is_error_true_envelope_even_with_valid_fenced_result(self) -> None:
        """A valid Claude error envelope must fail closed even if result contains fenced JSON."""
        valid_payload = _adoption_result("claude")
        fenced_payload = f"Error details:\n```json\n{json.dumps(valid_payload)}\n```"
        envelope = json.dumps({
            "is_error": True,
            "result": fenced_payload,
        })
        extracted = campaign_adapters._extract_claude_result(envelope)
        self.assertIsNone(extracted)

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            return subprocess.CompletedProcess(list(argv), 0, stdout=envelope, stderr="")

        code, output, _tmp = self._run("claude", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.exists())

    def test_claude_fenced_fallback_applies_only_when_not_valid_envelope(self) -> None:
        valid_payload = _adoption_result("claude")
        raw_fenced = f"```json\n{json.dumps(valid_payload)}\n```"
        extracted = campaign_adapters._extract_claude_result(raw_fenced)
        self.assertEqual(extracted, valid_payload)

    def test_antigravity_uses_prompt_file_not_stdin(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            seen["argv"] = list(argv)
            seen["prompt"] = prompt_input
            seen["timeout"] = timeout
            seen["env"] = child_env
            prompt_files = list(Path(workdir).glob("*.txt"))
            self.assertEqual(len(prompt_files), 1)
            self.assertIn(
                "release-qualification", prompt_files[0].read_text(encoding="utf-8").lower()
            )
            return subprocess.CompletedProcess(
                list(argv), 0, stdout=json.dumps(_adoption_result("antigravity")), stderr=""
            )

        env = {campaign_adapters.ANTIGRAVITY_AMBIENT_HOME_ENV: "1"}
        code, output, _tmp = self._run("antigravity", runner, env=env)
        self.assertEqual(code, 0)
        self.assertIsNone(seen["prompt"])
        self.assertIn("--print-timeout", seen["argv"])
        self.assertEqual(seen["timeout"], 870)
        self.assertTrue(output.is_file())

    def test_check_antigravity_readiness_passes_with_new_project_in_help(self) -> None:
        def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv, ["/bin/agy", "--help"])
            return subprocess.CompletedProcess(argv, 0, stdout="  --new-project  Run inside fresh project\n", stderr="")

        res = campaign_adapters.check_antigravity_readiness("/bin/agy", runner=fake_runner)
        self.assertTrue(res["ready"])
        self.assertEqual(res["provider"], "antigravity")
        self.assertEqual(res["capability"], "new_project")
        self.assertEqual(res["required_flag"], "--new-project")
        self.assertFalse(res["actionable"])
        self.assertTrue(campaign_adapters.check_antigravity_new_project_capability("/bin/agy", runner=fake_runner))

    def test_check_antigravity_readiness_fails_without_new_project(self) -> None:
        def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, stdout="  --continue  Resume session\n", stderr="")

        res = campaign_adapters.check_antigravity_readiness("/bin/agy", runner=fake_runner)
        self.assertFalse(res["ready"])
        self.assertEqual(res["provider"], "antigravity")
        self.assertEqual(res["capability"], "new_project")
        self.assertEqual(res["required_flag"], "--new-project")
        self.assertEqual(res["error"], "missing_new_project_capability")
        self.assertTrue(res["actionable"])
        self.assertEqual(
            res["remediation"],
            "Upgrade agy CLI to a version whose --help exposes --new-project.",
        )
        self.assertNotIn("1.1.26", res["remediation"])
        # Privacy: help text, paths, prompts, secrets must not leak in readiness dict:
        serialized = json.dumps(res)
        self.assertNotIn("--continue", serialized)
        self.assertNotIn("/bin/agy", serialized)
        self.assertFalse(campaign_adapters.check_antigravity_new_project_capability("/bin/agy", runner=fake_runner))

    def test_check_antigravity_readiness_rejects_flag_name_substrings(self) -> None:
        def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="  --new-project-template  Select a project template\n",
                stderr="",
            )

        result = campaign_adapters.check_antigravity_readiness(
            "/bin/agy",
            runner=fake_runner,
        )
        self.assertFalse(result["ready"])

    def test_check_antigravity_readiness_production_probe_uses_allowlisted_env(self) -> None:
        seen: dict[str, Any] = {}

        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.update(kwargs)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="  --new-project  Create a new project\n",
                stderr="",
            )

        with mock.patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "secret-github-token",
                "CODE_MOWER_CLOUD_TOKEN": "secret-cloud-token",
                "META_API_KEY": "secret-provider-key",
            },
            clear=False,
        ), mock.patch.object(campaign_adapters.subprocess, "run", side_effect=fake_run):
            result = campaign_adapters.check_antigravity_readiness("/bin/agy")

        self.assertTrue(result["ready"])
        probe_env = seen["env"]
        self.assertNotIn("GITHUB_TOKEN", probe_env)
        self.assertNotIn("CODE_MOWER_CLOUD_TOKEN", probe_env)
        self.assertNotIn("META_API_KEY", probe_env)

    def test_check_antigravity_readiness_fails_on_empty_or_nonexistent_command(self) -> None:
        res_empty = campaign_adapters.check_antigravity_readiness("")
        self.assertFalse(res_empty["ready"])
        self.assertEqual(res_empty["error"], "command_not_found")
        self.assertIn("Install agy CLI on PATH", res_empty["remediation"])

        def failing_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise OSError("No such file or directory")

        res_oserror = campaign_adapters.check_antigravity_readiness("/bin/nonexistent-agy", runner=failing_runner)
        self.assertFalse(res_oserror["ready"])
        self.assertEqual(res_oserror["error"], "capability_probe_failed")
        self.assertIn("agy --help runs successfully", res_oserror["remediation"])

    def test_check_antigravity_readiness_distinguishes_probe_timeout(self) -> None:
        def timeout_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, 10)

        result = campaign_adapters.check_antigravity_readiness(
            "/bin/agy",
            runner=timeout_runner,
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["error"], "capability_probe_timeout")
        self.assertIn("timed out", result["message"])
        self.assertNotIn("upgrade", result["remediation"].lower())

    def test_run_campaign_adapter_antigravity_fails_before_prompt_or_provider_call(self) -> None:
        def failing_cap_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, stdout="help without new project", stderr="")

        provider_invoked = False

        def provider_runner(*args: Any, **kwargs: Any) -> Any:
            nonlocal provider_invoked
            provider_invoked = True
            raise AssertionError("provider_runner should not have been called")

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            output = tmp / "result.json"
            env = {campaign_adapters.ANTIGRAVITY_AMBIENT_HOME_ENV: "1"}
            with mock.patch.dict(os.environ, env, clear=False):
                code = campaign_adapters.run_campaign_adapter(
                    provider="antigravity",
                    provider_bin="/bin/agy",
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    qualification_context="cold_install",
                    starting_version="",
                    output_path=output,
                    provider_runner=provider_runner,
                    capability_runner=failing_cap_runner,
                )
            self.assertEqual(code, 1)
            self.assertFalse(provider_invoked)
            # Ensure prompt file was never written
            prompt_files = list(tmp.glob("campaign.prompt-input.txt"))
            self.assertEqual(len(prompt_files), 0)

    def test_muse_reads_jsonl_events_with_prompt_file(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            seen["argv"] = list(argv)
            seen["stdin"] = prompt_input
            seen["env"] = child_env
            prompt_files = list(Path(workdir).glob("*.txt"))
            self.assertEqual(len(prompt_files), 1)
            event = json.dumps(
                {
                    "payload_type": "run.output.delta",
                    "payload": {"text": json.dumps(_adoption_result("muse"))},
                }
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout=event + "\n", stderr="")

        env = {
            campaign_adapters.MUSE_AMBIENT_HOME_ENV: "1",
            "META_API_KEY": "",
            "META_API_KEY_FILE": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ("META_API_KEY", "META_API_KEY_FILE"):
                os.environ.pop(key, None)
            code, output, _tmp = self._run("muse", runner)
        self.assertEqual(code, 0)
        self.assertIn("--prompt-file", seen["argv"])
        self.assertIn("--workspace", seen["argv"])
        # No API key in this environment, so nothing secret rides stdin.
        self.assertIsNone(seen["stdin"])
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), _adoption_result("muse"))

    def test_muse_explicit_api_key_uses_stdin_not_child_environment(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            seen["stdin"] = prompt_input
            seen["env"] = child_env
            event = json.dumps(
                {
                    "payload_type": "run.output.delta",
                    "payload": {"text": json.dumps(_adoption_result("muse"))},
                }
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout=event + "\n", stderr="")

        with mock.patch.dict(
            os.environ,
            {
                "META_API_KEY": "muse-secret-canary",
                campaign_adapters.MUSE_AMBIENT_HOME_ENV: "0",
            },
            clear=False,
        ):
            code, output, _tmp = self._run("muse", runner)

        self.assertEqual(code, 0)
        self.assertEqual(seen["stdin"], "muse-secret-canary")
        self.assertNotIn("META_API_KEY", seen["env"])
        self.assertNotIn("muse-secret-canary", json.dumps(seen["env"]))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), _adoption_result("muse"))

    def test_inner_timeout_reaches_provider_runner(self) -> None:
        seen: dict[str, Any] = {}

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            seen["timeout"] = timeout
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(json.dumps(_adoption_result("codex")), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, _output, _tmp = self._run("codex", runner, timeout_seconds=600)
        self.assertEqual(code, 0)
        self.assertEqual(seen["timeout"], 600)

    def test_campaign_inner_margin_matches_adapter_contract(self) -> None:
        # The campaign reserves ADAPTER_INNER_TIMEOUT_MARGIN_SECONDS so the
        # adapter's own provider timeout always fires first.
        self.assertEqual(
            release_campaigns.ADAPTER_INNER_TIMEOUT_MARGIN_SECONDS,
            campaign_adapters.INNER_TIMEOUT_MARGIN_SECONDS,
        )


class AdapterFailureTests(unittest.TestCase):
    """Timeouts, non-zero exits, and bad results fail closed with no file."""

    def _run_raw(
        self, provider: str, runner: Any, *, env: dict[str, str] | None = None
    ) -> tuple[int, Path]:
        tmp = Path(tempfile.mkdtemp())
        output = tmp / "result.json"
        with mock.patch.dict(os.environ, env or {}, clear=False):
            code = campaign_adapters.run_campaign_adapter(
                provider=provider,
                provider_bin=_fake_bin(tmp),
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                qualification_context="cold_install",
                starting_version="",
                output_path=output,
                timeout_seconds=870,
                codex_home=tmp / "codex-home",
                provider_runner=runner,
            )
        return code, output

    def test_provider_timeout_fails_closed(self) -> None:
        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            raise subprocess.TimeoutExpired(list(argv), timeout)

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_nonzero_exit_writes_no_result(self) -> None:
        code, output = self._run_raw("claude", lambda *a: _ok(a[0], returncode=1))
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_failed_attempt_removes_a_stale_result(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        output = tmp / "result.json"
        output.write_text(json.dumps(_adoption_result("claude")), encoding="utf-8")

        code = campaign_adapters.run_campaign_adapter(
            provider="claude",
            provider_bin=_fake_bin(tmp),
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            qualification_context="cold_install",
            starting_version="",
            output_path=output,
            timeout_seconds=870,
            provider_runner=lambda *a: _ok(a[0], returncode=1),
        )

        self.assertNotEqual(code, 0)
        self.assertFalse(output.exists())

    def test_garbage_result_is_rejected(self) -> None:
        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            return subprocess.CompletedProcess(
                list(argv), 0, stdout="not json at all {{{", stderr=""
            )

        code, output = self._run_raw(
            "antigravity", runner, env={campaign_adapters.ANTIGRAVITY_AMBIENT_HOME_ENV: "1"}
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_codex_rejects_failure_reason_outside_failed_package_install(self) -> None:
        """Local closed validation rejects failure_reason on a non-package_install step."""

        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            last = Path(argv[argv.index("--output-last-message") + 1])
            result = _adoption_result("codex")
            result["outcome"] = "fail"
            result["steps"] = [
                {
                    "id": "doctor",
                    "status": "fail",
                    "elapsed_seconds": 12.34,
                    "warning_count": 0,
                    "owner_action_count": 0,
                    "failure_reason": "runtime",
                },
            ]
            last.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_mismatched_provider_is_rejected(self) -> None:
        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(json.dumps(_adoption_result("claude")), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_mismatched_executor_is_rejected(self) -> None:
        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(
                json.dumps(_adoption_result("codex", executor="claude")),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_mismatched_tag_is_rejected(self) -> None:
        def runner(
            argv: Any, prompt_input: Any, timeout: int, workdir: Path, child_env: Any
        ) -> Any:
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(
                json.dumps(_adoption_result("codex", release_tag="v9.9.9")), encoding="utf-8"
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_missing_provider_bin_fails_closed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        output = tmp / "result.json"
        code = campaign_adapters.run_campaign_adapter(
            provider="codex",
            provider_bin=str(tmp / "does-not-exist"),
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            qualification_context="cold_install",
            starting_version="",
            output_path=output,
            timeout_seconds=870,
            provider_runner=lambda *a: _ok(a[0]),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_unsupported_provider_fails_closed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        output = tmp / "result.json"
        code = campaign_adapters.run_campaign_adapter(
            provider="devin",
            provider_bin=_fake_bin(tmp),
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            qualification_context="cold_install",
            starting_version="",
            output_path=output,
            timeout_seconds=870,
            provider_runner=lambda *a: _ok(a[0]),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_muse_auth_gate_refuses_without_key_or_opt_in(self) -> None:
        env = {
            campaign_adapters.MUSE_AMBIENT_HOME_ENV: "0",
            "META_API_KEY": "",
            "META_API_KEY_FILE": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ("META_API_KEY", "META_API_KEY_FILE"):
                os.environ.pop(key, None)
            tmp = Path(tempfile.mkdtemp())
            output = tmp / "result.json"
            calls: list[Any] = []

            def runner(
                argv: Any,
                prompt_input: Any,
                timeout: int,
                workdir: Path,
                child_env: Any,
            ) -> Any:
                calls.append(argv)
                return _ok(argv)

            code = campaign_adapters.run_campaign_adapter(
                provider="muse",
                provider_bin=_fake_bin(tmp),
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                qualification_context="cold_install",
                starting_version="",
                output_path=output,
                timeout_seconds=870,
                provider_runner=runner,
            )
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertFalse(output.is_file())


class DevinAdapterTests(unittest.TestCase):
    """Focused tests for the Devin CLI maintained campaign adapter."""

    def _devin_runner(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
        def runner(
            argv: Any,
            prompt_input: Any,
            timeout: int,
            workdir: Path,
            child_env: Any,
        ) -> Any:
            return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr=stderr)

        return runner

    def test_devin_cli_success_writes_validated_result(self) -> None:
        result = _adoption_result("devin_cli")

        def runner(
            argv: Any,
            prompt_input: Any,
            timeout: int,
            workdir: Path,
            child_env: Any,
        ) -> Any:
            prompt_path = workdir / "campaign.prompt-input.txt"
            # The prompt file is created with mode 0600 and is in the workspace.
            self.assertTrue(prompt_path.is_file())
            self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)
            # Autonomous+sandbox cannot approve write/edit tool calls
            # non-interactively; the prompt must require shell-only changes.
            self.assertIn(
                "shell commands only",
                prompt_path.read_text(encoding="utf-8"),
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(result), stderr="")

        code, output, _tmp = AdapterTransportTests()._run("devin_cli", runner)
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(written["provider"], "devin_cli")
        self.assertEqual(written["executor"], "devin_cli")

    def test_devin_cli_warning_result_is_accepted(self) -> None:
        result = _adoption_result("devin_cli", outcome="pass_with_warnings")
        result["steps"][0]["status"] = "warn"

        def runner(
            argv: Any,
            prompt_input: Any,
            timeout: int,
            workdir: Path,
            child_env: Any,
        ) -> Any:
            return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(result), stderr="")

        code, output, _tmp = AdapterTransportTests()._run("devin_cli", runner)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["outcome"],
            "pass_with_warnings",
        )

    def test_devin_cli_timeout_fails_closed(self) -> None:
        def runner(
            argv: Any,
            prompt_input: Any,
            timeout: int,
            workdir: Path,
            child_env: Any,
        ) -> Any:
            raise subprocess.TimeoutExpired(list(argv), timeout)

        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli", runner, timeout_seconds=60
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_nonzero_exit_fails_closed(self) -> None:
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli",
            self._devin_runner(returncode=1, stderr="provider CLI exited 1"),
            timeout_seconds=60,
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_auth_failure_fails_closed(self) -> None:
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli",
            self._devin_runner(
                returncode=1,
                stdout="",
                stderr="Error: not logged in. Run `devin auth login`.\n",
            ),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_malformed_result_fails_closed(self) -> None:
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli",
            self._devin_runner(stdout="not a json result"),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_duplicate_result_fails_closed(self) -> None:
        result = _adoption_result("devin_cli")
        duplicate = json.dumps(result) + "\n" + json.dumps(result)

        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli",
            self._devin_runner(stdout=duplicate),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_wrong_identity_is_rejected(self) -> None:
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli",
            self._devin_runner(stdout=json.dumps(_adoption_result("devin"))),
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_workspace_is_disposable_and_clean(self) -> None:
        """The adapter hands Devin a fresh empty workspace, so a dirty or
        pre-populated workspace cannot occur: the only file present when the
        provider starts is the mode-0600 prompt file. ``--respect-workspace-
        trust false`` bypasses the trust prompt for that disposable directory.
        """
        result = _adoption_result("devin_cli")

        def runner(
            argv: Any,
            prompt_input: Any,
            timeout: int,
            workdir: Path,
            child_env: Any,
        ) -> Any:
            entries = [p.name for p in workdir.iterdir()]
            self.assertEqual(sorted(entries), ["campaign.prompt-input.txt"])
            self.assertNotIn("--export", argv)
            return subprocess.CompletedProcess(
                list(argv), 0, stdout=json.dumps(result), stderr=""
            )

        code, output, _tmp = AdapterTransportTests()._run("devin_cli", runner)
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())

    def test_devin_cli_chatty_output_with_unrelated_json_is_accepted(self) -> None:
        """Tool/progress JSON before the result must not hide the one result."""
        result = _adoption_result("devin_cli")
        chatty = (
            '{"type":"progress","message":"running exec"}\n'
            'prose line with {braces} that is not JSON\n'
            '{"type":"tool","name":"exec","status":"done"}\n'
            + json.dumps(result)
            + "\n"
        )
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli", self._devin_runner(stdout=chatty)
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["provider"],
            "devin_cli",
        )

    def test_devin_cli_nested_prose_and_trailing_json_are_tolerated(self) -> None:
        """Braced prose and trailing tool events do not disturb extraction."""
        result = _adoption_result("devin_cli")
        chatty = (
            "done { step: ok } and {unbalanced\n"
            + json.dumps(result)
            + '\n{"type":"summary","turns":2}\n'
        )
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli", self._devin_runner(stdout=chatty)
        )
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())

    def test_devin_cli_chatty_output_does_not_leak_secrets_or_paths(self) -> None:
        """A success-path run with noisy stdout writes only validated schema fields.

        Unrelated JSON, secret tokens, and local-path-like text in the provider's
        raw stdout must not appear in the persisted adoption result.
        """
        secret = "ghp_leaked_secret_xyz"
        leaked_path = "/home/redacted/.ssh/id_rsa"
        result = _adoption_result("devin_cli")
        unrelated = json.dumps(
            {"type": "tool", "details": {"artifact": leaked_path, "secret": secret}}
        )
        chatty = (
            "progress: still running\n"
            + unrelated
            + "\nprose { with braces and path: "
            + leaked_path
            + " }\n"
            + json.dumps(result)
            + "\ntrailing {not json} and secret "
            + secret
            + "\n"
        )
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli", self._devin_runner(stdout=chatty)
        )
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())
        raw_output = output.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw_output)
        self.assertNotIn(leaked_path, raw_output)
        self.assertNotIn(unrelated, raw_output)
        written = json.loads(raw_output)
        self.assertEqual(written, result)
        self.assertEqual(set(written), release_qualify.ADOPTION_RESULT_FIELDS)

    def test_devin_cli_zero_candidates_fails_closed(self) -> None:
        """Well-formed but schema-mismatched JSON is not an adoption result."""
        chatty = (
            '{"type":"progress","message":"ok"}\n'
            '{"schema":"other.schema.v1","result":"pass"}\n'
        )
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli", self._devin_runner(stdout=chatty)
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_devin_cli_embedded_result_object_is_found(self) -> None:
        """A result nested inside a final-event object is still a candidate."""
        result = _adoption_result("devin_cli")
        wrapped = '{"type":"final","response":' + json.dumps(result) + "}\n"
        code, output, _tmp = AdapterTransportTests()._run(
            "devin_cli", self._devin_runner(stdout=wrapped)
        )
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())

    def test_devin_cli_fixture_exercises_real_subprocess_argv(self) -> None:
        """A content-free fixture CLI validates the real argv over a real
        subprocess (``run_provider_command``), reads the mode-0600 prompt file,
        and emits chatty output plus one schema-bound result."""
        result_fixture = (
            "import json, sys\n"
            "argv = sys.argv[1:]\n"
            "required = ('--print', '--prompt-file', '--respect-workspace-trust', '--model', '--sandbox', '--permission-mode')\n"
            "forbidden = ('--export', '--continue', '--resume')\n"
            "missing = [f for f in required if f not in argv]\n"
            "present = [f for f in forbidden if f in argv]\n"
            "if missing or present:\n"
            "    sys.stderr.write('argv-contract-violation missing=' + ','.join(missing) + ' present=' + ','.join(present))\n"
            "    sys.exit(3)\n"
            "if argv[argv.index('--respect-workspace-trust') + 1] != 'false':\n"
            "    sys.stderr.write('argv-contract-violation trust')\n"
            "    sys.exit(3)\n"
            "if argv[argv.index('--permission-mode') + 1] != 'autonomous':\n"
            "    sys.stderr.write('argv-contract-violation permission')\n"
            "    sys.exit(3)\n"
            "prompt_path = argv[argv.index('--prompt-file') + 1]\n"
            "import os, time\n"
            "assert (os.stat(prompt_path).st_mode & 0o777) == 0o600\n"
            "prompt = open(prompt_path, encoding='utf-8').read()\n"
            "assert 'code_mower.adoptionResult.v1' in prompt\n"
            "result = {\n"
            "    'schema': 'code_mower.adoptionResult.v1',\n"
            "    'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),\n"
            "    'release_tag': 'v1.0.0',\n"
            "    'package_identity': 'code-mower',\n"
            "    'normalized_version': '1.0.0',\n"
            "    'qualification_context': 'cold_install',\n"
            "    'starting_version': '',\n"
            "    'ending_version': '1.0.0',\n"
            "    'provider': 'devin_cli',\n"
            "    'executor': 'devin_cli',\n"
            "    'host_class': 'local',\n"
            "    'runtime_class': 'python_%d.%d' % sys.version_info[:2],\n"
            "    'execution_state': 'executed',\n"
            "    'elapsed_seconds': 0.5,\n"
            "    'outcome': 'pass',\n"
            "    'steps': [{'id': 'doctor', 'status': 'pass', 'elapsed_seconds': 0.5, 'warning_count': 0, 'owner_action_count': 0}],\n"
            "}\n"
            "print(json.dumps({'type': 'progress', 'message': 'exec done'}))\n"
            "print(json.dumps(result))\n"
        )
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "devin_fixture.py"
        script.write_text(result_fixture, encoding="utf-8")
        # The venv interpreter path may contain spaces, which cannot sit in a
        # shebang; launch through /bin/sh instead.
        fixture = tmp / "devin-fixture"
        fixture.write_text(
            '#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, script),
            encoding="utf-8",
        )
        fixture.chmod(0o755)
        output = tmp / "result.json"
        code = campaign_adapters.run_campaign_adapter(
            provider="devin_cli",
            provider_bin=str(fixture),
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            qualification_context="cold_install",
            starting_version="",
            output_path=output,
            timeout_seconds=60,
            provider_runner=campaign_adapters.run_provider_command,
        )
        self.assertEqual(code, 0)
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(written["provider"], "devin_cli")
        self.assertEqual(written["outcome"], "pass")

    @unittest.skipUnless(shutil.which("devin"), "installed devin CLI not on PATH")
    def test_installed_devin_cli_advertises_required_flags(self) -> None:
        """The installed CLI exposes the noninteractive flags the adapter uses."""
        completed = subprocess.run(
            ["devin", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        for flag in (
            "--print",
            "--prompt-file",
            "--respect-workspace-trust",
            "--model",
            "--sandbox",
            "--permission-mode",
        ):
            self.assertIn(flag, completed.stdout)

    def test_devin_cli_redacts_raw_output_and_paths(self) -> None:
        secret = "ghp_leaked_secret_xyz"

        def runner(
            argv: Any,
            prompt_input: Any,
            timeout: int,
            workdir: Path,
            child_env: Any,
        ) -> Any:
            # The child environment must not receive unrelated secrets.
            self.assertNotIn("GITHUB_TOKEN", child_env)
            self.assertNotIn("GH_TOKEN", child_env)
            # The prompt file must be named relative to the workspace and must
            # not be an absolute path. No conversation export, no secrets.
            self.assertNotIn("--export", argv)
            prompt_idx = argv.index("--prompt-file")
            prompt_file_arg = argv[prompt_idx + 1]
            self.assertEqual(prompt_file_arg, "campaign.prompt-input.txt")
            self.assertFalse(prompt_file_arg.startswith("/"))
            self.assertNotIn(secret, argv)
            # Simulate a provider that echoes a secret and a local path. The
            # adapter must not write raw stdout into the result file.
            return subprocess.CompletedProcess(
                list(argv),
                1,
                stdout=f"{secret} /redacted/home/work\n{json.dumps(_adoption_result('devin_cli'))}",
                stderr="",
            )

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": secret, "GH_TOKEN": secret}, clear=False):
            code, output, _tmp = AdapterTransportTests()._run("devin_cli", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())


class MaintainedRegistryTests(unittest.TestCase):
    """The registry ships maintained adapters for the local campaign lanes."""

    def test_five_lanes_carry_maintained_adapter_argv(self) -> None:
        expected = {
            "codex": "codex",
            "claude_audit": "claude",
            "antigravity_cli": "antigravity",
            "muse_cli": "muse",
            "devin_cli": "devin_cli",
        }
        for lane_id, adapter_provider in expected.items():
            with self.subTest(lane=lane_id):
                lane = REFERENCE_PROVIDERS[lane_id]
                template = lane.provider_config.get("campaign_adapter_argv")
                self.assertIsNotNone(template)
                assert template is not None
                joined = " ".join(template)
                for placeholder in (
                    "{python}",
                    "{target_python}",
                    "{command}",
                    "{adapter_timeout}",
                    "{output}",
                ):
                    self.assertIn(placeholder, joined)
                self.assertIn(adapter_provider, template)
                timeout = lane.provider_config.get("campaign_adapter_timeout_seconds")
                self.assertIsInstance(timeout, int)
                self.assertGreater(timeout, campaign_adapters.INNER_TIMEOUT_MARGIN_SECONDS)

    def test_lanes_without_adapters_stay_manual(self) -> None:
        self.assertNotIn("campaign_adapter_argv", REFERENCE_PROVIDERS["aider"].provider_config)


class StructuredResultCapabilityTests(unittest.TestCase):
    """The offline structured-result probe exercises real provider extraction paths."""

    def test_all_maintained_lanes_pass_structured_result_capability(self) -> None:
        for prov in (
            "codex",
            "claude_audit",
            "claude",
            "antigravity_cli",
            "antigravity",
            "muse_cli",
            "muse",
            "devin_cli",
        ):
            with self.subTest(provider=prov):
                self.assertTrue(campaign_adapters.check_structured_result_capability(prov))

    def test_claude_audit_routes_through_extract_claude_result(self) -> None:
        """claude_audit must explicitly route through _extract_claude_result."""
        with mock.patch(
            "code_mower.campaign_adapters._extract_claude_result",
            wraps=campaign_adapters._extract_claude_result,
        ) as mock_extract:
            self.assertTrue(campaign_adapters.check_structured_result_capability("claude_audit"))
            mock_extract.assert_called_once()

    def test_claude_audit_fails_if_generic_extractor_used_on_envelope(self) -> None:
        """Regression: claude_audit envelope fails schema validation under generic extractor.

        If claude_audit falls through to the generic parse_response_json extractor,
        the Claude envelope object (with is_error, result) is returned directly
        instead of unwrapping the inner adoption result, causing validation to fail.
        """
        from code_mower import gemini_cli_audit_pr as code_mower_gemini_cli

        sample_payload = _adoption_result("claude_audit")
        claude_envelope = json.dumps({"is_error": False, "result": json.dumps(sample_payload)})

        # Generic extractor cannot unwrap the envelope
        generic_extracted = code_mower_gemini_cli.parse_response_json(claude_envelope)
        self.assertIsNotNone(generic_extracted)
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(generic_extracted)

        # But _extract_claude_result correctly extracts and validates it
        extracted = campaign_adapters._extract_claude_result(claude_envelope)
        self.assertEqual(extracted, sample_payload)
        release_qualify.validate_adoption_result_payload(extracted)

    def test_claude_audit_capability_probe_fails_on_error_envelope(self) -> None:
        """If _extract_claude_result returns None (e.g. is_error=True), check_structured_result_capability fails."""
        with mock.patch(
            "code_mower.campaign_adapters._extract_claude_result",
            return_value=None,
        ):
            self.assertFalse(campaign_adapters.check_structured_result_capability("claude_audit"))


if __name__ == "__main__":
    unittest.main()
