#!/usr/bin/env python3
"""Diagnose and bounce Claude CLI auth/env issues."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, "", "tools"}:
    try:
        from tools.claude_cli_environment import (
            CLAUDE_AUTH_OVERRIDE_ENV,
            clean_claude_cli_env,
            render_claude_env_unset_snippet,
        )
        from tools.doctor_checks.provider_probe import evaluate_json_probe
    except ImportError:  # pragma: no cover - direct script execution fallback
        from claude_cli_environment import (  # type: ignore
            CLAUDE_AUTH_OVERRIDE_ENV,
            clean_claude_cli_env,
            render_claude_env_unset_snippet,
        )
        from doctor_checks.provider_probe import evaluate_json_probe  # type: ignore
else:  # pragma: no cover - exercised after package extraction.
    from .claude_cli_environment import (
        CLAUDE_AUTH_OVERRIDE_ENV,
        clean_claude_cli_env,
        render_claude_env_unset_snippet,
    )
    from .doctor_checks.provider_probe import evaluate_json_probe


DEFAULT_PROBE_ARGS = (
    "--print",
    "--output-format",
    "json",
    "--no-session-persistence",
    "--setting-sources",
    "local",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--disable-slash-commands",
    "--tools",
    "",
    "--model",
    "sonnet",
    "--max-budget-usd",
    "0.25",
    "Reply with exactly: ok",
)

PROBE_CONFIG = {
    "doctor_probe_error_fields": ("is_error", "api_error_status"),
    "doctor_probe_auth_status_fields": ("api_error_status",),
    "doctor_probe_expect_json_field": "result",
    "doctor_probe_expect_json_value": "ok",
}


def _run_probe(
    *,
    command_path: str,
    args: Sequence[str],
    timeout_seconds: int,
    env: Mapping[str, str],
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="code-mower-claude-bounce-") as tmp:
        try:
            return (
                subprocess.run(
                    [command_path, *args],
                    cwd=tmp,
                    env=dict(env),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                ),
                None,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)


def _summarize_probe(
    *,
    label: str,
    completed: subprocess.CompletedProcess[str] | None,
    exception: str | None,
    removed_env: tuple[str, ...],
) -> dict[str, Any]:
    if completed is None:
        return {
            "label": label,
            "status": "warn",
            "message": "probe could not run",
            "returncode": None,
            "removed_env": list(removed_env),
            "exception_type": "probe_exception",
        }
    output = (completed.stdout or completed.stderr or "").strip()
    status, message, detail = evaluate_json_probe(
        PROBE_CONFIG,
        output,
        returncode=completed.returncode,
    )
    return {
        "label": label,
        "status": status,
        "message": message,
        "returncode": completed.returncode,
        "removed_env": list(removed_env),
        **detail,
    }


def bounce_claude_cli(
    *,
    command: str = "claude",
    probe_args: tuple[str, ...] = DEFAULT_PROBE_ARGS,
    timeout_seconds: int = 30,
    base_env: Mapping[str, str] | None = None,
    extra_unset: tuple[str, ...] = (),
    skip_inherited_probe: bool = False,
) -> dict[str, Any]:
    env = dict(os.environ if base_env is None else base_env)
    command_path = shutil.which(command, path=env.get("PATH")) or ""
    if not command_path:
        return {
            "status": "fail",
            "command": command,
            "command_path": None,
            "probes": [],
            "recommendation": "install_or_fix_claude_path",
            "message": f"{command} was not found on PATH",
        }

    probes: list[dict[str, Any]] = []
    inherited_summary: dict[str, Any] | None = None
    if not skip_inherited_probe:
        completed, exception = _run_probe(
            command_path=command_path,
            args=probe_args,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        inherited_summary = _summarize_probe(
            label="inherited_env",
            completed=completed,
            exception=exception,
            removed_env=(),
        )
        probes.append(inherited_summary)

    clean_env, removed = clean_claude_cli_env(
        env,
        scrub_auth_overrides=True,
        extra_unset=extra_unset,
    )
    completed, exception = _run_probe(
        command_path=command_path,
        args=probe_args,
        timeout_seconds=timeout_seconds,
        env=clean_env,
    )
    clean_summary = _summarize_probe(
        label="clean_env",
        completed=completed,
        exception=exception,
        removed_env=removed,
    )
    probes.append(clean_summary)

    inherited_ok = bool(inherited_summary and inherited_summary.get("status") == "pass")
    clean_ok = clean_summary.get("status") == "pass"
    clean_auth_error = bool(clean_summary.get("auth_error_detected"))
    inherited_auth_error = bool(
        inherited_summary and inherited_summary.get("auth_error_detected")
    )

    if inherited_ok:
        status = "pass"
        recommendation = "no_action_needed"
        message = "Claude CLI smoke probe passed with the inherited environment"
    elif clean_ok:
        status = "pass"
        recommendation = "use_clean_claude_env_or_restart_parent_app"
        message = (
            "Claude CLI passed after removing Claude/Anthropic auth override env vars; "
            "source the generated unset snippet or restart the parent app with a clean environment"
        )
    elif clean_auth_error:
        status = "warn"
        recommendation = "reauthenticate_claude"
        message = "Claude CLI still reports auth failure after env cleanup"
    elif inherited_auth_error:
        status = "warn"
        recommendation = "clean_env_did_not_resolve_auth"
        message = "Inherited env had auth failure, and the clean probe did not pass"
    else:
        status = "warn"
        recommendation = "inspect_cli_output_manually"
        message = "Claude CLI probe did not pass; inspect local CLI install/auth manually"

    return {
        "status": status,
        "command": command,
        "command_path": command_path,
        "probe_args": list(probe_args),
        "scrubbed_env_names": list(CLAUDE_AUTH_OVERRIDE_ENV),
        "extra_unset": list(extra_unset),
        "probes": probes,
        "recommendation": recommendation,
        "message": message,
    }


def render_bounce_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower Claude CLI bounce",
        f"Status: {report.get('status', 'unknown')}",
        f"Command: {report.get('command')} ({report.get('command_path') or 'not found'})",
        f"Recommendation: {report.get('recommendation')}",
        f"Message: {report.get('message')}",
        "",
    ]
    for probe in report.get("probes", []):
        if not isinstance(probe, Mapping):
            continue
        removed = ", ".join(probe.get("removed_env", [])) or "none"
        lines.append(
            f"- {probe.get('label')}: {probe.get('status')} "
            f"returncode={probe.get('returncode')} removed_env={removed}"
        )
        message = probe.get("message")
        if message:
            lines.append(f"  {message}")
        auth_status = probe.get("auth_status_code")
        if auth_status:
            lines.append(f"  auth_status_code={auth_status}")
    lines.extend(
        [
            "",
            "If clean_env passes but inherited_env fails, source the generated unset",
            "snippet in your shell or fully restart the app that launched the shell.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default=os.environ.get("CLAUDE_CLI_PATH", "claude"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--skip-inherited-probe",
        action="store_true",
        help="only run the clean-env Claude probe",
    )
    parser.add_argument(
        "--unset-env",
        action="append",
        default=[],
        help="additional env var to remove for the clean probe; repeatable",
    )
    parser.add_argument(
        "--write-env",
        type=Path,
        default=None,
        help="write a shell snippet that unsets Claude/Anthropic auth override vars",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = bounce_claude_cli(
        command=args.command,
        timeout_seconds=args.timeout,
        extra_unset=tuple(args.unset_env),
        skip_inherited_probe=args.skip_inherited_probe,
    )
    if args.write_env is not None:
        args.write_env.parent.mkdir(parents=True, exist_ok=True)
        args.write_env.write_text(render_claude_env_unset_snippet(), encoding="utf-8")
        report["written_env_file"] = str(args.write_env)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_bounce_text(report), end="")
        if args.write_env is not None:
            print(f"Wrote env cleanup snippet: {args.write_env}")
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
