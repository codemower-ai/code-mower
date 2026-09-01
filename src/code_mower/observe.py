#!/usr/bin/env python3
"""Optional local observability helpers for Code Mower operators."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AGENTTRAIL_VERSION = "0.2.0"
DEFAULT_BOARD_URL = "http://localhost:5330"


@dataclass(frozen=True)
class AgentTrailConfig:
    repo: Path
    version: str = AGENTTRAIL_VERSION
    npx_command: str = "npx"
    node_command: str = "node"
    port: int | None = None
    dry_run: bool = False
    allow_repo_changes: bool = False
    claude_hooks: bool = False
    settle_seconds: float = 5.0


def _package(version: str) -> str:
    return f"agenttrail@{version}"


def build_agenttrail_command(config: AgentTrailConfig) -> list[str]:
    command = [config.npx_command, "-y", _package(config.version), str(config.repo), "--no-open"]
    if config.port is not None:
        command.extend(["--port", str(config.port)])
    return command


def build_agenttrail_hooks_command(config: AgentTrailConfig) -> list[str]:
    command = [
        config.npx_command,
        "-y",
        _package(config.version),
        "init",
        "--hooks-only",
        str(config.repo),
    ]
    if config.port is not None:
        command.extend(["--port", str(config.port)])
    return command


def _git_status(repo: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def _node_major(version_text: str) -> int | None:
    try:
        return int(version_text.strip().lstrip("v").split(".", 1)[0])
    except ValueError:
        return None


def _check_runtime(config: AgentTrailConfig) -> str | None:
    if shutil.which(config.node_command) is None:
        return f"{config.node_command!r} was not found on PATH; AgentTrail requires Node 20+."
    if shutil.which(config.npx_command) is None:
        return f"{config.npx_command!r} was not found on PATH; install npm/npx first."
    try:
        completed = subprocess.run(
            [config.node_command, "--version"],
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return f"could not verify {config.node_command!r}: {exc}"
    major = _node_major(completed.stdout)
    if major is None or major < 20:
        return f"AgentTrail requires Node 20+; observed {completed.stdout.strip() or 'unknown'}."
    return None


def _start_background(command: list[str], repo: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _terminate_process_group(process: subprocess.Popen[bytes], *, timeout: float = 5.0) -> bool:
    if process.poll() is not None:
        return True

    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = None

    try:
        if pgid is None:
            process.terminate()
        else:
            os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass

    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        try:
            if pgid is None:
                process.kill()
            else:
                os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False


def _watch_launch_status(
    process: subprocess.Popen[bytes],
    repo: Path,
    before_status: str | None,
    settle_seconds: float,
) -> tuple[int | None, str | None, bool, int]:
    poll_interval_seconds = 0.25
    attempts = max(1, math.ceil(max(0.0, settle_seconds) / poll_interval_seconds))
    after_status = before_status

    for attempt in range(attempts):
        if process.poll() is not None:
            return process.returncode or 1, after_status, False, attempt + 1
        if before_status is not None:
            observed_status = _git_status(repo)
            if observed_status is not None:
                after_status = observed_status
                if observed_status != before_status:
                    return None, after_status, True, attempt + 1
        if attempt < attempts - 1:
            time.sleep(poll_interval_seconds)

    if process.poll() is not None:
        return process.returncode or 1, after_status, False, attempts
    return None, after_status, False, attempts


def _base_payload(config: AgentTrailConfig, status: str) -> dict[str, Any]:
    return {
        "mode": "observe-agenttrail",
        "status": status,
        "repo": str(config.repo),
        "agenttrail_version": config.version,
    }


def _board_url(config: AgentTrailConfig) -> str:
    return DEFAULT_BOARD_URL if config.port is None else f"http://localhost:{config.port}"


def run_agenttrail(config: AgentTrailConfig) -> tuple[int, dict[str, Any]]:
    if not config.repo.is_dir():
        return 2, {
            "mode": "observe-agenttrail",
            "status": "error",
            "error": f"repository path does not exist or is not a directory: {config.repo}",
        }

    launch_command = build_agenttrail_command(config)
    hooks_command = build_agenttrail_hooks_command(config)
    before_status = _git_status(config.repo)
    if config.dry_run:
        payload = _base_payload(config, "dry-run")
        payload.update(
            command=launch_command,
            claude_hooks_command=hooks_command if config.claude_hooks else None,
            calls_init=False,
            board_url=_board_url(config),
            repo_status_checked=before_status is not None,
        )
        return 0, payload

    runtime_error = _check_runtime(config)
    if runtime_error:
        payload = _base_payload(config, "error")
        payload["error"] = runtime_error
        return 127, payload

    try:
        if config.claude_hooks:
            subprocess.run(
                hooks_command,
                cwd=config.repo,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        process = _start_background(launch_command, config.repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        payload = _base_payload(config, "error")
        payload["error"] = f"could not start AgentTrail: {exc}"
        return 1, payload

    exit_code, after_status, repo_changed, settle_attempts = _watch_launch_status(
        process,
        config.repo,
        before_status,
        config.settle_seconds,
    )
    if exit_code is not None:
        payload = _base_payload(config, "error")
        payload["error"] = "AgentTrail exited immediately after launch."
        return exit_code, payload

    payload = _base_payload(config, "started")
    payload.update(
        command=launch_command,
        pid=process.pid,
        board_url=_board_url(config),
        repo_status_checked=before_status is not None and after_status is not None,
        repo_changed=repo_changed,
        settle_seconds=config.settle_seconds,
        settle_attempts=settle_attempts,
    )
    if repo_changed and not config.allow_repo_changes:
        terminated = _terminate_process_group(process)
        payload["status"] = "blocked"
        payload["terminated"] = terminated
        payload["error"] = (
            "AgentTrail launch changed repository status. Inspect `git status`, then rerun "
            "with --allow-repo-changes only if the change was intentional."
        )
        return 2, payload
    return 0, payload


def _print_result(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload.get("status") == "dry-run":
        print("AgentTrail observe dry run")
        print("command: " + " ".join(str(part) for part in payload["command"]))
        print("does not call: agenttrail init")
    elif payload.get("status") == "started":
        print(f"AgentTrail started for {payload['repo']}")
        print(f"board: {payload['board_url']}")
        print("repo status changed: " + ("yes" if payload.get("repo_changed") else "no"))
    else:
        print(f"AgentTrail observe {payload.get('status')}: {payload.get('error')}", file=sys.stderr)


def _agenttrail_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="code-mower observe agenttrail")
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--agenttrail-version",
        default=os.environ.get("CODE_MOWER_AGENTTRAIL_VERSION", AGENTTRAIL_VERSION),
    )
    parser.add_argument("--npx-command", default=os.environ.get("CODE_MOWER_NPX_COMMAND", "npx"))
    parser.add_argument("--node-command", default=os.environ.get("CODE_MOWER_NODE_COMMAND", "node"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-repo-changes", action="store_true")
    parser.add_argument("--claude-hooks", action="store_true")
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    config = AgentTrailConfig(
        repo=Path(args.repo).expanduser().resolve(),
        version=args.agenttrail_version,
        npx_command=args.npx_command,
        node_command=args.node_command,
        port=args.port,
        dry_run=args.dry_run,
        allow_repo_changes=args.allow_repo_changes,
        claude_hooks=args.claude_hooks,
        settle_seconds=max(0.0, args.settle_seconds),
    )
    exit_code, payload = run_agenttrail(config)
    _print_result(payload, json_output=args.json)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower observe")
    parser.add_argument("command", choices=["agenttrail"])
    args, rest = parser.parse_known_args(list(argv or []))
    if args.command == "agenttrail":
        return _agenttrail_main(rest)
    raise AssertionError(f"unhandled observe command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
