#!/usr/bin/env python3
"""Preflight the local environment for structured Codex audits.

This catches the common macOS failure mode where a freshly-created
framework-Python venv is executable but cannot validate GitHub TLS
certificates. Run after sourcing ``tools/codex_audit_env.sh`` and before
starting a long PR audit.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence


DEFAULT_CODEX_CLI_PATH = "/Applications/Codex.app/Contents/Resources/codex"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
ENV_HELP = "Run: source tools/codex_audit_env.sh"

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def resolve_required_setting(
    *,
    cli_value: Optional[str],
    env: Mapping[str, str],
    env_name: str,
    label: str,
) -> CheckResult:
    value = cli_value or env.get(env_name)
    if not value:
        return CheckResult(label, False, f"{env_name} is not set. {ENV_HELP}")
    path = Path(value)
    if not path.exists():
        return CheckResult(label, False, f"{env_name} does not exist: {value}")
    if not os.access(path, os.X_OK):
        return CheckResult(label, False, f"{env_name} is not executable: {value}")
    return CheckResult(label, True, str(path))


def _run_command(
    command: Sequence[str],
    *,
    timeout: int,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess:
    argv = list(command)
    try:
        return runner(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr or stdout or f"timed out after {timeout}s"
        return subprocess.CompletedProcess(argv, 1, stdout=stdout, stderr=detail)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(exc))


def check_python_github_tls(
    *,
    python_path: str,
    github_url: str,
    timeout: int,
    runner: Runner = subprocess.run,
) -> CheckResult:
    probe = (
        "import urllib.error\n"
        "import urllib.request\n"
        "try:\n"
        f"    with urllib.request.urlopen({github_url!r}, timeout={timeout}) as response:\n"
        "        response.read(1)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    exc.close()\n"
        "print('github TLS ok')\n"
    )
    result = _run_command(
        [python_path, "-c", probe],
        timeout=timeout,
        runner=runner,
    )
    if result.returncode == 0:
        return CheckResult("github TLS", True, result.stdout.strip() or github_url)
    detail = (result.stderr or result.stdout or "").strip()
    return CheckResult(
        "github TLS",
        False,
        detail
        or f"{python_path} could not reach {github_url}; check certificates/network.",
    )


def check_codex_cli(
    *,
    codex_cli_path: str,
    timeout: int,
    runner: Runner = subprocess.run,
) -> CheckResult:
    result = _run_command(
        [codex_cli_path, "--version"],
        timeout=timeout,
        runner=runner,
    )
    if result.returncode == 0:
        return CheckResult("codex CLI", True, result.stdout.strip() or codex_cli_path)
    detail = (result.stderr or result.stdout or "").strip()
    return CheckResult("codex CLI", False, detail or f"{codex_cli_path} failed")


def run_preflight(
    *,
    python_path: str,
    codex_cli_path: str,
    github_url: str = DEFAULT_GITHUB_API_URL,
    timeout: int = 10,
    runner: Runner = subprocess.run,
) -> list[CheckResult]:
    return [
        check_python_github_tls(
            python_path=python_path,
            github_url=github_url,
            timeout=timeout,
            runner=runner,
        ),
        check_codex_cli(
            codex_cli_path=codex_cli_path,
            timeout=timeout,
            runner=runner,
        ),
    ]


def _print_results(results: Sequence[CheckResult], *, quiet: bool) -> None:
    for result in results:
        if quiet and result.ok:
            continue
        marker = "ok" if result.ok else "error"
        stream = sys.stdout if result.ok else sys.stderr
        print(f"{marker}: {result.name}: {result.detail}", file=stream)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        dest="python_path",
        help="Controlled Python to preflight. Defaults to CODEX_AUDIT_PYTHON.",
    )
    parser.add_argument(
        "--codex-cli-path",
        default=os.environ.get("CODEX_CLI_PATH"),
        help="Codex CLI path. Defaults to CODEX_CLI_PATH.",
    )
    parser.add_argument(
        "--github-url",
        default=DEFAULT_GITHUB_API_URL,
        help="HTTPS URL used to verify the selected Python's TLS store.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Timeout in seconds for each probe.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only failures.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    env = os.environ
    python_check = resolve_required_setting(
        cli_value=args.python_path,
        env=env,
        env_name="CODEX_AUDIT_PYTHON",
        label="controlled Python",
    )
    codex_check = resolve_required_setting(
        cli_value=args.codex_cli_path,
        env=env,
        env_name="CODEX_CLI_PATH",
        label="Codex CLI",
    )
    setup_results = [python_check, codex_check]
    if not all(result.ok for result in setup_results):
        _print_results(setup_results, quiet=args.quiet)
        return 1

    probe_results = run_preflight(
        python_path=python_check.detail,
        codex_cli_path=codex_check.detail,
        github_url=args.github_url,
        timeout=args.timeout,
    )
    _print_results(setup_results + probe_results, quiet=args.quiet)
    return 0 if all(result.ok for result in probe_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
