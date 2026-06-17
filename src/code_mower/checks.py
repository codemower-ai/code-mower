#!/usr/bin/env python3
"""Detect and run a repository's native check surface."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CheckSpec:
    id: str
    category: str
    language: str
    tool: str
    command: tuple[str, ...]
    source: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "language": self.language,
            "tool": self.tool,
            "command": list(self.command),
            "source": self.source,
            "reason": self.reason,
        }


NODE_SCRIPT_CANDIDATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("node.lint", "lint", ("lint",)),
    ("node.typecheck", "typecheck", ("typecheck", "type-check", "check-types", "tsc")),
    ("node.test", "test", ("test", "test:unit")),
    ("node.build", "build", ("build",)),
)

MAKE_TARGETS: tuple[tuple[str, str], ...] = (
    ("make.lint", "lint"),
    ("make.test", "test"),
    ("make.build", "build"),
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _node_package_manager(repo_path: Path) -> tuple[str, tuple[str, ...], str]:
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm", ("pnpm", "run"), "pnpm-lock.yaml"
    if (repo_path / "yarn.lock").exists():
        return "yarn", ("yarn",), "yarn.lock"
    if (repo_path / "bun.lockb").exists() or (repo_path / "bun.lock").exists():
        return "bun", ("bun", "run"), "bun lockfile"
    return "npm", ("npm", "run"), "package.json"


def _detect_node_checks(repo_path: Path) -> list[CheckSpec]:
    package_json = repo_path / "package.json"
    if not package_json.exists():
        return []
    data = _read_json(package_json)
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []

    manager, prefix, lock_source = _node_package_manager(repo_path)
    checks: list[CheckSpec] = []
    for check_id, category, candidates in NODE_SCRIPT_CANDIDATES:
        script = next((candidate for candidate in candidates if candidate in scripts), None)
        if script is None:
            continue
        checks.append(
            CheckSpec(
                id=check_id,
                category=category,
                language="javascript",
                tool=manager,
                command=(*prefix, script),
                source=f"package.json:scripts.{script}",
                reason=f"{lock_source} selects {manager}; package.json declares {script!r}.",
            )
        )
    return checks


def _python_command(repo_path: Path) -> tuple[str, str]:
    candidates = (
        repo_path / ".venv" / "bin" / "python",
        repo_path / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate), ".venv"
    return sys.executable, "current interpreter"


def _has_ruff_config(repo_path: Path) -> bool:
    if (repo_path / "ruff.toml").exists() or (repo_path / ".ruff.toml").exists():
        return True
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return "[tool.ruff]" in text or "[tool.ruff." in text


def _detect_python_checks(repo_path: Path) -> list[CheckSpec]:
    has_python_project = any(
        (repo_path / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")
    )
    tests_dir = repo_path / "tests"
    has_python_tests = tests_dir.is_dir() and any(tests_dir.rglob("*.py"))
    if not has_python_project and not has_python_tests:
        return []

    python, python_source = _python_command(repo_path)
    checks: list[CheckSpec] = []
    if _has_ruff_config(repo_path):
        checks.append(
            CheckSpec(
                id="python.ruff",
                category="lint",
                language="python",
                tool="ruff",
                command=(python, "-m", "ruff", "check", "."),
                source="ruff configuration",
                reason=f"Ruff config is present; using {python_source} Python.",
            )
        )
    if has_python_tests:
        checks.append(
            CheckSpec(
                id="python.pytest",
                category="test",
                language="python",
                tool="pytest",
                command=(python, "-m", "pytest", "tests", "-q"),
                source="tests/",
                reason=f"tests/ exists; using {python_source} Python.",
            )
        )
    return checks


def _make_targets(repo_path: Path) -> set[str]:
    makefile = repo_path / "Makefile"
    if not makefile.exists():
        return set()
    targets: set[str] = set()
    try:
        for line in makefile.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("."):
                continue
            name, separator, _rest = stripped.partition(":")
            if separator and name and all(char not in name for char in " \t=$"):
                targets.add(name)
    except OSError:
        return set()
    return targets


def _detect_make_checks(repo_path: Path) -> list[CheckSpec]:
    targets = _make_targets(repo_path)
    return [
        CheckSpec(
            id=check_id,
            category=target,
            language="generic",
            tool="make",
            command=("make", target),
            source=f"Makefile:{target}",
            reason=f"Makefile declares {target!r}.",
        )
        for check_id, target in MAKE_TARGETS
        if target in targets
    ]


def detect_checks(repo_path: Path) -> list[CheckSpec]:
    repo_path = repo_path.resolve()
    checks = [
        *_detect_node_checks(repo_path),
        *_detect_python_checks(repo_path),
        *_detect_make_checks(repo_path),
    ]
    seen: set[str] = set()
    unique: list[CheckSpec] = []
    for check in checks:
        if check.id in seen:
            continue
        seen.add(check.id)
        unique.append(check)
    return unique


def _command_text(command: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _preview(text: str | bytes | None, *, enabled: bool, limit: int = 4000) -> str | None:
    if not enabled:
        return None
    text = _output_text(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def run_checks(
    checks: list[CheckSpec],
    *,
    repo_path: Path,
    timeout_seconds: int,
    include_output: bool = False,
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    exit_code = 0
    for check in checks:
        started = time.monotonic()
        result: dict[str, Any] = {
            **check.as_dict(),
            "command_text": _command_text(check.command),
        }
        if dry_run:
            result.update(
                {
                    "status": "planned",
                    "returncode": None,
                    "duration_seconds": 0.0,
                }
            )
            results.append(result)
            continue

        try:
            completed = subprocess.run(
                check.command,
                cwd=repo_path,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            duration = time.monotonic() - started
            result.update(
                {
                    "status": "failed",
                    "returncode": 127,
                    "duration_seconds": round(duration, 3),
                    "error": str(exc),
                }
            )
            exit_code = 1
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            result.update(
                {
                    "status": "timeout",
                    "returncode": None,
                    "duration_seconds": round(duration, 3),
                    "stdout_preview": _preview(exc.stdout or "", enabled=include_output),
                    "stderr_preview": _preview(exc.stderr or "", enabled=include_output),
                }
            )
            exit_code = 1
        else:
            duration = time.monotonic() - started
            status = "passed" if completed.returncode == 0 else "failed"
            result.update(
                {
                    "status": status,
                    "returncode": completed.returncode,
                    "duration_seconds": round(duration, 3),
                    "stdout_preview": _preview(completed.stdout, enabled=include_output),
                    "stderr_preview": _preview(completed.stderr, enabled=include_output),
                }
            )
            if completed.returncode != 0:
                exit_code = 1
        results.append(result)
    return results, exit_code


def _filter_checks(checks: list[CheckSpec], only: list[str]) -> list[CheckSpec]:
    if not only:
        return checks
    wanted = {item for group in only for item in group.split(",") if item}
    return [
        check
        for check in checks
        if check.id in wanted or check.category in wanted or check.language in wanted
    ]


def _render_detect_text(checks: list[CheckSpec]) -> str:
    if not checks:
        return "No native checks detected.\n"
    lines = ["Detected native checks:"]
    for check in checks:
        lines.append(f"- {check.id}: {_command_text(check.command)}")
        lines.append(f"  source: {check.source}")
    return "\n".join(lines) + "\n"


def _render_run_text(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No native checks detected.\n"
    lines = ["Native check results:"]
    for result in results:
        lines.append(
            f"- {result['id']}: {result['status']} "
            f"({result['duration_seconds']}s) {result['command_text']}"
        )
    return "\n".join(lines) + "\n"


def _json_payload(mode: str, repo_path: Path, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": mode,
        "repo_path": str(repo_path.resolve()),
        "check_count": len(checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower checks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect")
    detect.add_argument("--repo-path", type=Path, default=Path.cwd())
    detect.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run")
    run.add_argument("--repo-path", type=Path, default=Path.cwd())
    run.add_argument("--only", action="append", default=[])
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--include-output", action="store_true")
    run.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    repo_path = args.repo_path.resolve()
    checks = detect_checks(repo_path)

    if args.command == "detect":
        check_dicts = [check.as_dict() for check in checks]
        if args.json:
            print(json.dumps(_json_payload("code-mower-checks-detect", repo_path, check_dicts), indent=2))
        else:
            print(_render_detect_text(checks), end="")
        return 0

    if args.command == "run":
        selected = _filter_checks(checks, args.only)
        results, exit_code = run_checks(
            selected,
            repo_path=repo_path,
            timeout_seconds=args.timeout,
            include_output=args.include_output,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(_json_payload("code-mower-checks-run", repo_path, results), indent=2))
        else:
            print(_render_run_text(results), end="")
        return exit_code

    raise AssertionError(f"unhandled checks command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
