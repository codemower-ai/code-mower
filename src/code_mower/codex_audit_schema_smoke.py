#!/usr/bin/env python3
"""Smoke-test Codex structured-output support for audit verdicts.

This is intentionally smaller than a full PR audit: it only checks that
the installed Codex CLI still accepts the committed
`codex_audit_verdict.schema.json` with generic `codex exec
--output-schema` and returns a wrapper-validated verdict artifact.
Run after Codex CLI upgrades, after schema edits, or when structured
audits start returning UNKNOWN.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Optional, Sequence

if __package__ in {None, "", "tools"}:
    try:
        from tools.codex_audit_pr import (
            AuditConfig,
            CODEX_AUDIT_SCHEMA_ID,
            CODEX_AUDIT_VERDICT_SCHEMA_PATH,
            DEFAULT_CODEX_CLI_PATH,
            _build_subprocess_env,
            _codex_exec_command,
            _make_secure_temp_dir,
            _trusted_repo_root,
            load_structured_codex_verdict,
            preflight_codex_cli,
        )
    except ImportError:  # pragma: no cover - direct script execution fallback
        from codex_audit_pr import (  # type: ignore
            AuditConfig,
            CODEX_AUDIT_SCHEMA_ID,
            CODEX_AUDIT_VERDICT_SCHEMA_PATH,
            DEFAULT_CODEX_CLI_PATH,
            _build_subprocess_env,
            _codex_exec_command,
            _make_secure_temp_dir,
            _trusted_repo_root,
            load_structured_codex_verdict,
            preflight_codex_cli,
        )
else:  # pragma: no cover - exercised after package extraction.
    from .codex_audit_pr import (
        AuditConfig,
        CODEX_AUDIT_SCHEMA_ID,
        CODEX_AUDIT_VERDICT_SCHEMA_PATH,
        DEFAULT_CODEX_CLI_PATH,
        _build_subprocess_env,
        _codex_exec_command,
        _make_secure_temp_dir,
        _trusted_repo_root,
        load_structured_codex_verdict,
        preflight_codex_cli,
    )


def _smoke_prompt() -> str:
    return (
        "Return exactly one Codex audit verdict JSON object for the "
        f"{CODEX_AUDIT_SCHEMA_ID} schema. Use verdict \"blocked\". "
        "Use summary \"Structured audit schema smoke passed.\" Include "
        "exactly one finding with severity \"P2\", title "
        "\"Schema smoke finding\", file "
        "\"tools/codex_audit_schema_smoke.py\", line 1, and detail "
        "\"This synthetic P2 verifies the schema and wrapper parser.\""
    )


def run_schema_smoke(codex_cli_path: str, timeout: int) -> int:
    config = AuditConfig(
        github_token="",
        repo_paths={},
        codex_cli_path=codex_cli_path,
        timeout=timeout,
    )
    version = preflight_codex_cli(config)
    tmp_dir = _make_secure_temp_dir("codex-audit-schema-smoke-")
    verdict_path = tmp_dir / "verdict.json"
    command = _codex_exec_command(
        config,
        "--sandbox",
        "read-only",
        "--output-schema",
        str(CODEX_AUDIT_VERDICT_SCHEMA_PATH),
        "--output-last-message",
        str(verdict_path),
        "-",
        skip_git_repo_check=True,
    )
    try:
        result = subprocess.run(
            command,
            cwd=str(_trusted_repo_root()),
            input=_smoke_prompt(),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_build_subprocess_env(None),
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stdout, end="")
            print(result.stderr, file=sys.stderr, end="")
            return result.returncode or 1

        parsed = load_structured_codex_verdict(verdict_path)
        if parsed.verdict != "BLOCKED" or parsed.p2_count != 1:
            print(
                "error: schema smoke produced an unexpected verdict: "
                + json.dumps(parsed.to_dict(), sort_keys=True),
                file=sys.stderr,
            )
            return 1

        print(f"codex: {version}")
        print(f"schema: {CODEX_AUDIT_VERDICT_SCHEMA_PATH}")
        print(json.dumps(parsed.to_dict(), sort_keys=True))
        return 0
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-cli-path",
        default=os.environ.get("CODEX_CLI_PATH", DEFAULT_CODEX_CLI_PATH),
        help="Path to the Codex CLI binary.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds for the smoke call.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        return run_schema_smoke(args.codex_cli_path, args.timeout)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(
            f"error: codex schema smoke timed out after {args.timeout}s",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
