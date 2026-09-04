#!/usr/bin/env python3
"""Tests for the maintained release-campaign provider adapters.

Every provider invocation runs through an injected ``provider_runner`` mock:
these tests never execute a real provider CLI, spend, or touch the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import campaign_adapters, release_campaigns
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
        "executor": f"{provider}_cli",
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


def _fake_bin(tmp: Path, name: str = "provider-bin") -> str:
    bin_path = tmp / name
    bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return str(bin_path)


def _ok(
    argv: Any, *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr=stderr)


class ArgvBuilderTests(unittest.TestCase):
    """Each maintained transport builds its verified noninteractive surface."""

    def test_codex_argv_uses_exec_with_stdin_and_schema(self) -> None:
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
        no_model = campaign_adapters.build_codex_argv(
            codex_bin="/bin/codex",
            schema_path="/tmp/schema.json",
            last_message_path="/tmp/last.md",
            workdir="/tmp/work",
        )
        self.assertNotIn("--model", no_model)

    def test_claude_argv_uses_print_with_json_envelope(self) -> None:
        argv = campaign_adapters.build_claude_argv(
            claude_bin="/bin/claude",
            model="sonnet",
            max_budget_usd="5.00",
            schema_json="{}",
        )
        self.assertEqual(argv[0], "/bin/claude")
        for flag in ("--print", "--output-format", "json", "--model", "sonnet",
                     "--max-budget-usd", "5.00", "--json-schema"):
            self.assertIn(flag, argv)
        self.assertIn("--no-session-persistence", argv)

    def test_antigravity_argv_uses_prompt_file_and_timeout(self) -> None:
        argv = campaign_adapters.build_antigravity_argv(
            agy_bin="/bin/agy",
            workspace_dir="/tmp/work/ws",
            prompt_file_name="campaign.prompt-input.txt",
            timeout_seconds=870,
            model="gemini-3",
        )
        self.assertEqual(argv[0], "/bin/agy")
        self.assertIn("--print", argv)
        self.assertIn("--print-timeout", argv)
        self.assertIn("870s", argv)
        self.assertIn("--model", argv)
        # The prompt travels in a file, not on the command line.
        self.assertFalse(any("prompt-input" in token and len(token) > 200 for token in argv))

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
        for flag in ("exec", "--json", "--prompt-file", "--workspace",
                     "--provider", "meta", "--approval-mode", "never",
                     "--max-model-steps", "12",
                     "--model", "muse", "--reasoning-effort", "high"):
            self.assertIn(flag, argv)


class AdapterTransportTests(unittest.TestCase):
    """End-to-end adapter runs with a mocked provider subprocess."""

    def _run(
        self,
        provider: str,
        runner: Any,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 870,
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
                provider_runner=runner,
            )
        return code, output, tmp

    def test_codex_reads_last_message_file_and_ignores_stdout(self) -> None:
        seen: dict[str, Any] = {}

        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            seen["argv"] = list(argv)
            seen["prompt"] = prompt_input
            seen["timeout"] = timeout
            # Secret-looking stdout must never reach the output file: the
            # candidate comes only from the provider-written message file.
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(json.dumps(_adoption_result("codex")), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, stdout="sk-antigravity SECRET", stderr="")

        code, output, _tmp = self._run("codex", runner)
        self.assertEqual(code, 0)
        self.assertIsNotNone(seen["prompt"])
        self.assertIn("exec", seen["argv"])
        self.assertTrue(output.is_file())
        stored = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(stored, _adoption_result("codex"))
        self.assertNotIn("SECRET", output.read_text(encoding="utf-8"))

    def test_claude_reads_structured_output_envelope(self) -> None:
        seen: dict[str, Any] = {}

        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            seen["argv"] = list(argv)
            seen["prompt"] = prompt_input
            seen["timeout"] = timeout
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

    def test_antigravity_uses_prompt_file_not_stdin(self) -> None:
        seen: dict[str, Any] = {}

        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            seen["argv"] = list(argv)
            seen["prompt"] = prompt_input
            seen["timeout"] = timeout
            prompt_files = list(Path(workdir).glob("*.txt"))
            self.assertEqual(len(prompt_files), 1)
            self.assertIn("release-qualification", prompt_files[0].read_text(encoding="utf-8").lower())
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

    def test_muse_reads_jsonl_events_with_prompt_file(self) -> None:
        seen: dict[str, Any] = {}

        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            seen["argv"] = list(argv)
            seen["stdin"] = prompt_input
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

    def test_inner_timeout_reaches_provider_runner(self) -> None:
        seen: dict[str, Any] = {}

        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
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
                provider_runner=runner,
            )
        return code, output

    def test_provider_timeout_fails_closed(self) -> None:
        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            raise subprocess.TimeoutExpired(list(argv), timeout)

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_nonzero_exit_writes_no_result(self) -> None:
        code, output = self._run_raw("claude", lambda *a: _ok(a[0], returncode=1))
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_garbage_result_is_rejected(self) -> None:
        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            return subprocess.CompletedProcess(list(argv), 0, stdout="not json at all {{{", stderr="")

        code, output = self._run_raw("antigravity", runner,
                                     env={campaign_adapters.ANTIGRAVITY_AMBIENT_HOME_ENV: "1"})
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_mismatched_provider_is_rejected(self) -> None:
        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
            last = Path(argv[argv.index("--output-last-message") + 1])
            last.write_text(json.dumps(_adoption_result("claude")), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

        code, output = self._run_raw("codex", runner)
        self.assertNotEqual(code, 0)
        self.assertFalse(output.is_file())

    def test_mismatched_tag_is_rejected(self) -> None:
        def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
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

            def runner(argv: Any, prompt_input: Any, timeout: int, workdir: Path) -> Any:
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


class MaintainedRegistryTests(unittest.TestCase):
    """The registry ships maintained adapters for exactly the four lanes."""

    def test_four_lanes_carry_maintained_adapter_argv(self) -> None:
        expected = {
            "codex": "codex",
            "claude_audit": "claude",
            "antigravity_cli": "antigravity",
            "muse_cli": "muse",
        }
        for lane_id, adapter_provider in expected.items():
            with self.subTest(lane=lane_id):
                lane = REFERENCE_PROVIDERS[lane_id]
                template = lane.provider_config.get("campaign_adapter_argv")
                self.assertIsNotNone(template)
                assert template is not None
                joined = " ".join(template)
                for placeholder in ("{python}", "{command}", "{adapter_timeout}", "{output}"):
                    self.assertIn(placeholder, joined)
                self.assertIn(adapter_provider, template)
                timeout = lane.provider_config.get("campaign_adapter_timeout_seconds")
                self.assertIsInstance(timeout, int)
                self.assertGreater(timeout, campaign_adapters.INNER_TIMEOUT_MARGIN_SECONDS)

    def test_lanes_without_adapters_stay_manual(self) -> None:
        self.assertNotIn("campaign_adapter_argv", REFERENCE_PROVIDERS["aider"].provider_config)


if __name__ == "__main__":
    unittest.main()
