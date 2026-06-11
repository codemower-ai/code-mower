#!/usr/bin/env python3
"""Rehearse migration from product-local Code Mower tools to the package."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


DEFAULT_COMMANDS = (
    ("providers", "list"),
    (
        "prompts",
        "validate",
        "--lenses",
        "base-audit,calibration-policy,package-runtime",
        "--json",
    ),
)
CALIBRATION_CANDIDATES = (
    ".code-mower.generated/calibration-corpus.json",
    "tools/calibration_corpus.json",
    "tools/calibration_corpus.example.json",
    "templates/calibration-corpus.json",
)
CALIBRATION_EVIDENCE_ADDITIVE_KEYS = frozenset(
    {
        "audit_input_insufficient_count",
        "audit_input_insufficient_runs",
        "result_category",
    }
)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout_sha256: str
    stderr_preview: str
    stdout_preview: str

    @classmethod
    def from_completed(
        cls,
        command: Sequence[str],
        completed: subprocess.CompletedProcess[str],
    ) -> "CommandResult":
        return cls(
            command=tuple(command),
            returncode=int(completed.returncode),
            stdout_sha256=hashlib.sha256(
                completed.stdout.encode("utf-8", errors="replace")
            ).hexdigest(),
            stdout_preview=completed.stdout[:800],
            stderr_preview=completed.stderr[:1200],
        )


@dataclass(frozen=True)
class RunOutput:
    public: CommandResult
    stdout: str


def _resolve_command(command_text: str) -> tuple[str, ...]:
    parts = tuple(part for part in command_text.split(" ") if part)
    if not parts:
        raise ValueError("command must not be empty")
    return parts


def _default_local_command(repo_path: Path) -> tuple[str, ...] | None:
    candidate = repo_path / "tools" / "code_mower_cli.py"
    if candidate.is_file():
        return (sys.executable, str(candidate))
    return None


def _default_package_command() -> tuple[str, ...]:
    resolved = shutil.which("code-mower")
    return (resolved or "code-mower",)


def _run(command: Sequence[str], *, cwd: Path, timeout: int) -> RunOutput:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return RunOutput(
        public=CommandResult.from_completed(command, completed),
        stdout=completed.stdout,
    )


def _json_payload(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _prune_additive_calibration_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _prune_additive_calibration_keys(item)
            for key, item in value.items()
            if key not in CALIBRATION_EVIDENCE_ADDITIVE_KEYS
        }
    if isinstance(value, list):
        return [_prune_additive_calibration_keys(item) for item in value]
    return value


def _compatibility_for(
    suffix: Sequence[str],
    local: RunOutput,
    package: RunOutput,
) -> tuple[bool, str]:
    if local.public.returncode != package.public.returncode:
        return False, "returncode_mismatch"
    if local.public.stdout_sha256 == package.public.stdout_sha256:
        return True, "exact_stdout_match"
    if tuple(suffix) == ("providers", "list"):
        local_providers = {line.strip() for line in local.stdout.splitlines() if line.strip()}
        package_providers = {line.strip() for line in package.stdout.splitlines() if line.strip()}
        if local_providers and local_providers <= package_providers:
            return True, "package_provider_superset"
    if suffix[:2] == ("prompts", "validate"):
        local_payload = _json_payload(local.stdout)
        package_payload = _json_payload(package.stdout)
        if isinstance(local_payload, dict) and isinstance(package_payload, dict):
            local_payload.pop("prompt_dir", None)
            package_payload.pop("prompt_dir", None)
            if local_payload == package_payload:
                return True, "prompt_dir_only_difference"
    if suffix[:2] == ("calibration", "evidence"):
        local_payload = _json_payload(local.stdout)
        package_payload = _json_payload(package.stdout)
        if (
            isinstance(local_payload, dict)
            and isinstance(package_payload, dict)
            and _prune_additive_calibration_keys(local_payload)
            == _prune_additive_calibration_keys(package_payload)
        ):
            return True, "calibration_evidence_additive_schema_only"
    return False, "stdout_mismatch"


def _safe_commands(repo_path: Path) -> list[tuple[str, ...]]:
    commands = list(DEFAULT_COMMANDS)
    for candidate in CALIBRATION_CANDIDATES:
        if (repo_path / candidate).is_file():
            commands.append(("calibration", "evidence", candidate, "--json"))
            break
    return commands


def run_wrapper_rehearsal(
    *,
    repo_path: Path,
    local_command: Sequence[str] | None = None,
    package_command: Sequence[str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo path is not a directory: {repo_path}")
    local_command = tuple(local_command) if local_command else _default_local_command(repo_path)
    package_command = tuple(package_command or _default_package_command())
    if not local_command:
        raise ValueError(
            "could not infer local product Code Mower command; pass --local-command"
        )

    comparisons: list[dict[str, Any]] = []
    for suffix in _safe_commands(repo_path):
        local = _run((*local_command, *suffix), cwd=repo_path, timeout=timeout)
        package = _run((*package_command, *suffix), cwd=repo_path, timeout=timeout)
        match, reason = _compatibility_for(suffix, local, package)
        comparisons.append(
            {
                "suffix": list(suffix),
                "match": match,
                "reason": reason,
                "local": asdict(local.public),
                "package": asdict(package.public),
            }
        )

    mismatches = [item for item in comparisons if not item["match"]]
    return {
        "mode": "code-mower-product-wrapper-rehearsal",
        "status": "pass" if not mismatches else "warn",
        "repo_path": str(repo_path),
        "local_command": list(local_command),
        "package_command": list(package_command),
        "comparison_count": len(comparisons),
        "mismatch_count": len(mismatches),
        "comparisons": comparisons,
        "notes": [
            "Only read-only commands are compared.",
            "A pass means this repo is a candidate for CODE_MOWER_USE_STANDALONE shadow mode, not that local tools can be deleted yet.",
        ],
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        "Code Mower product wrapper rehearsal",
        f"Status: {payload['status']}",
        f"Repo: {payload['repo_path']}",
        f"Comparisons: {payload['comparison_count']} ({payload['mismatch_count']} mismatches)",
        "",
    ]
    for item in payload["comparisons"]:
        status = "PASS" if item["match"] else "WARN"
        lines.append(f"- {status} {' '.join(item['suffix'])}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    wrapper = subparsers.add_parser("wrapper-rehearsal")
    wrapper.add_argument("--repo-path", type=Path, default=Path.cwd())
    wrapper.add_argument(
        "--local-command",
        default="",
        help="product-local command prefix, e.g. 'python tools/code_mower_cli.py'",
    )
    wrapper.add_argument(
        "--package-command",
        default="",
        help="standalone command prefix, e.g. 'code-mower'",
    )
    wrapper.add_argument("--timeout", type=int, default=60)
    wrapper.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "wrapper-rehearsal":
        try:
            payload = run_wrapper_rehearsal(
                repo_path=args.repo_path,
                local_command=_resolve_command(args.local_command)
                if args.local_command
                else None,
                package_command=_resolve_command(args.package_command)
                if args.package_command
                else None,
                timeout=args.timeout,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            payload = {
                "mode": "code-mower-product-wrapper-rehearsal",
                "status": "fail",
                "error": str(exc),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"wrapper rehearsal failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_text(payload), end="")
        return 0 if payload["status"] == "pass" else 1

    raise AssertionError(f"unhandled migration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
