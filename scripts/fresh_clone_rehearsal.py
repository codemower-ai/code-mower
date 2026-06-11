#!/usr/bin/env python3
"""Rehearse Code Mower from a fresh clone and clean virtual environment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class RehearsalError(RuntimeError):
    def __init__(self, message: str, steps: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.steps = steps


def _venv_python(venv_dir: Path) -> Path:
    unix_python = venv_dir / "bin" / "python"
    if unix_python.exists():
        return unix_python
    return venv_dir / "Scripts" / "python.exe"


def _default_repo_url() -> str:
    package_root = Path(__file__).resolve().parents[1]
    return str(package_root)


def _run(command: list[str], *, cwd: Path, steps: list[dict[str, Any]]) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    step = {
        "command": command,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    steps.append(step)
    if completed.returncode != 0:
        raise RehearsalError(f"command failed: {' '.join(command)}", steps)


def run_rehearsal(args: argparse.Namespace) -> dict[str, Any]:
    work_dir = (
        Path(args.work_dir).expanduser().resolve()
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix="code-mower-fresh-clone-"))
    )
    clone_dir = work_dir / "clone"
    venv_dir = work_dir / "venv"
    smoke_dir = work_dir / "smoke"
    steps: list[dict[str, Any]] = []

    if clone_dir.exists() or venv_dir.exists() or smoke_dir.exists():
        raise RuntimeError(f"work directory is not clean: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)

    repo_url = args.repo_url or _default_repo_url()
    _run(["git", "clone", repo_url, str(clone_dir)], cwd=work_dir, steps=steps)
    if args.ref:
        _run(["git", "checkout", args.ref], cwd=clone_dir, steps=steps)

    python_bin = Path(args.python).expanduser().resolve() if args.python else Path(sys.executable)
    _run([str(python_bin), "-m", "venv", str(venv_dir)], cwd=work_dir, steps=steps)
    venv_python = _venv_python(venv_dir)
    _run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], cwd=clone_dir, steps=steps)
    _run([str(venv_python), "-m", "pip", "install", "-e", "."], cwd=clone_dir, steps=steps)
    _run([str(venv_python), "-m", "pip", "check"], cwd=clone_dir, steps=steps)
    _run(
        [
            str(venv_python),
            "scripts/smoke_easy_mode.py",
            "--work-dir",
            str(smoke_dir),
            "--json",
        ],
        cwd=clone_dir,
        steps=steps,
    )

    return {
        "mode": "code-mower-fresh-clone-rehearsal",
        "status": "pass",
        "repo_url": repo_url,
        "ref": args.ref,
        "work_dir": str(work_dir),
        "clone_dir": str(clone_dir),
        "steps": steps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", help="Repository URL or local path to clone.")
    parser.add_argument("--ref", help="Branch, tag, or commit to check out after cloning.")
    parser.add_argument("--work-dir", help="Clean work directory for clone, venv, and smoke output.")
    parser.add_argument("--python", help="Python executable used to create the rehearsal venv.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    try:
        payload = run_rehearsal(args)
    except Exception as exc:  # pragma: no cover - exercised by CLI failure paths.
        payload = {
            "mode": "code-mower-fresh-clone-rehearsal",
            "status": "fail",
            "error": str(exc),
            "steps": getattr(exc, "steps", []),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"fresh clone rehearsal failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"fresh clone rehearsal passed: {payload['clone_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
