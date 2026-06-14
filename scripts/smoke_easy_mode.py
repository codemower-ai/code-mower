#!/usr/bin/env python3
"""Smoke test the standalone Code Mower easy-mode package flow."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CODE_MOWER_BIN = "code-mower"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if stdout_path is not None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(completed.stdout, encoding="utf-8")
    result = {
        "command": command,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(result, indent=2, sort_keys=True))
    return result


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_executable(path_text: str) -> Path:
    resolved = shutil.which(path_text)
    if resolved:
        return Path(resolved).resolve()
    return Path(path_text).expanduser().resolve()


def _default_code_mower_bin() -> str:
    resolved = shutil.which(DEFAULT_CODE_MOWER_BIN)
    if resolved:
        return resolved

    sibling_script = Path(sys.executable).parent / DEFAULT_CODE_MOWER_BIN
    if sibling_script.is_file():
        return str(sibling_script)

    resolved_sibling_script = Path(sys.executable).resolve().parent / DEFAULT_CODE_MOWER_BIN
    if resolved_sibling_script.is_file():
        return str(resolved_sibling_script)

    return DEFAULT_CODE_MOWER_BIN


def run_smoke(*, code_mower_bin: Path, work_dir: Path) -> dict[str, Any]:
    if not code_mower_bin.is_file():
        raise RuntimeError(f"code-mower executable not found: {code_mower_bin}")

    toy_repo = work_dir / "toy-repo"
    outputs = work_dir / "outputs"
    if toy_repo.exists():
        shutil.rmtree(toy_repo)
    toy_repo.mkdir(parents=True)
    outputs.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = f"{code_mower_bin.parent}{os.pathsep}{env.get('PATH', '')}"

    git = shutil.which("git")
    if git:
        _run([git, "init", "-q"], cwd=toy_repo, env=env)
        _run([git, "config", "user.name", "Code Mower Smoke"], cwd=toy_repo, env=env)
        _run([git, "config", "user.email", "smoke@example.com"], cwd=toy_repo, env=env)
        _run([git, "config", "commit.gpgSign", "false"], cwd=toy_repo, env=env)
    (toy_repo / "README.md").write_text(
        "# Code Mower smoke toy repo\n", encoding="utf-8"
    )
    if git:
        _run([git, "add", "README.md"], cwd=toy_repo, env=env)
        _run(
            [git, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "Initial smoke repo"],
            cwd=toy_repo,
            env=env,
        )

    steps: list[dict[str, Any]] = []
    cm = str(code_mower_bin)
    steps.append(
        _run([cm, "providers", "list"], cwd=toy_repo, env=env, stdout_path=outputs / "providers.txt")
    )
    steps.append(
        _run(
            [cm, "init", "--easy", "--apply", "--output-dir", ".code-mower.generated", "--json"],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / "init-apply.json",
        )
    )
    steps.append(
        _run(
            ["bash", ".code-mower.generated/smoke-tests.sh"],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / "generated-smoke-tests.txt",
        )
    )
    steps.append(
        _run([cm, "doctor", "--easy", "--json"], cwd=toy_repo, env=env, stdout_path=outputs / "doctor.json")
    )
    steps.append(
        _run(
            [cm, "next-steps", "--profile", "recommended", "--json"],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / "next-steps.json",
        )
    )
    steps.append(
        _run(
            [
                cm,
                "migration",
                "wrapper-rehearsal",
                "--repo-path",
                str(toy_repo),
                "--local-command",
                cm,
                "--package-command",
                cm,
                "--json",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / "wrapper-rehearsal.json",
        )
    )

    code_mower_dir = toy_repo / ".code-mower"
    code_mower_dir.mkdir(exist_ok=True)
    steps.append(
        _run(
            [
                cm,
                "calibration",
                "plan",
                ".code-mower.generated/calibration-corpus.json",
                "--replicates",
                "2",
                "--json",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=code_mower_dir / "calibration-plan.json",
        )
    )
    steps.append(
        _run(
            [cm, "calibration", "evidence", ".code-mower.generated/calibration-corpus.json", "--json"],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / "calibration-evidence.json",
        )
    )
    steps.append(
        _run(
            [
                cm,
                "reviewer-metrics",
                "calibration-evidence.json",
                "--spend",
                ".code-mower.generated/reviewer-spend.json",
                "--json",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / "reviewer-metrics.json",
        )
    )
    steps.append(
        _run(
            [cm, "calibration", "policy", "reviewer-metrics.json", "--json"],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / "lane-policy.json",
        )
    )
    steps.append(
        _run(
            [
                cm,
                "calibration",
                "value-report",
                ".code-mower.generated/calibration-corpus.json",
                "--spend",
                ".code-mower.generated/reviewer-spend.json",
                "--output",
                "reviewer-value-report.md",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / "value-report.txt",
        )
    )
    steps.append(
        _run(
            [
                cm,
                "cloud",
                "export",
                "--report",
                "reviewer-metrics=reviewer-metrics.json",
                "--report",
                "lane-policy=lane-policy.json",
                "--report",
                "value-report=reviewer-value-report.md",
                "--output-dir",
                ".code-mower/cloud-benchmark-bundle",
                "--json",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / "cloud-export.json",
        )
    )
    steps.append(
        _run(
            [
                cm,
                "cloud",
                "upload",
                ".code-mower/cloud-benchmark-bundle",
                "--dry-run",
                "--json",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / "cloud-upload-dry-run.json",
        )
    )
    steps.append(
        _run(
            [
                cm,
                "cloud",
                "dogfood",
                "--repo-path",
                str(toy_repo),
                "--repo-slug",
                "example/toy-repo",
                "--source",
                "easy-mode-smoke",
                "--endpoint",
                "http://localhost:3000/api/ingest",
                "--json",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / "cloud-dogfood-dry-run.json",
        )
    )

    summary = {
        "mode": "code-mower-easy-mode-smoke",
        "status": "pass",
        "code_mower_bin": str(code_mower_bin),
        "work_dir": str(work_dir),
        "toy_repo": str(toy_repo),
        "outputs_dir": str(outputs),
        "steps": steps,
    }
    _write_json(outputs / "smoke-summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--code-mower-bin",
        default=_default_code_mower_bin(),
        help="Path to the installed code-mower executable.",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Directory for the generated toy repo and outputs. Defaults to a temp dir.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    work_dir = Path(args.work_dir) if args.work_dir else Path(
        tempfile.mkdtemp(prefix="code-mower-easy-smoke-")
    )
    try:
        summary = run_smoke(code_mower_bin=_resolve_executable(args.code_mower_bin), work_dir=work_dir)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Code Mower easy-mode smoke passed: {work_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
