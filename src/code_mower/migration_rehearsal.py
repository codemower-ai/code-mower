#!/usr/bin/env python3
"""Package-install rehearsal helpers for Code Mower migration gates."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower.migration_install import (
        MIRRORED_IMPLEMENTATION_PATTERNS,
        CommandResult,
        RehearsalError,
        RunOutput,
        _default_product_rehearsal_local_command,
        _glob_relative_files,
        _json_payload,
        _load_release_readiness,
        _pip_install_command,
        _resolve_install_package_spec,
        _run,
        _run_rehearsal_step,
        _run_rehearsal_step_to_file,
        _venv_code_mower,
        _venv_python,
        _write_json,
        _write_public_rehearsal_toy_repo,
    )
    from code_mower.migration_readiness import (
        FIRST_USER_ARTIFACTS as FIRST_USER_ARTIFACTS,
        PRIVACY_EXCLUDED_CONTENT as PRIVACY_EXCLUDED_CONTENT,
        first_user_artifacts as _first_user_artifacts,
        first_user_readiness_scorecard as _first_user_readiness_scorecard,
    )
else:
    from .migration_install import (
        MIRRORED_IMPLEMENTATION_PATTERNS,
        CommandResult,
        RehearsalError,
        RunOutput,
        _default_product_rehearsal_local_command,
        _glob_relative_files,
        _json_payload,
        _load_release_readiness,
        _pip_install_command,
        _resolve_install_package_spec,
        _run,
        _run_rehearsal_step,
        _run_rehearsal_step_to_file,
        _venv_code_mower,
        _venv_python,
        _write_json,
        _write_public_rehearsal_toy_repo,
    )
    from .migration_readiness import (
        FIRST_USER_ARTIFACTS as FIRST_USER_ARTIFACTS,
        PRIVACY_EXCLUDED_CONTENT as PRIVACY_EXCLUDED_CONTENT,
        first_user_artifacts as _first_user_artifacts,
        first_user_readiness_scorecard as _first_user_readiness_scorecard,
    )

__all__ = [
    "FIRST_USER_ARTIFACTS",
    "MIRRORED_IMPLEMENTATION_PATTERNS",
    "PRIVACY_EXCLUDED_CONTENT",
    "CommandResult",
    "RehearsalError",
    "RunOutput",
    "_default_product_rehearsal_local_command",
    "_first_user_artifacts",
    "_first_user_readiness_scorecard",
    "_glob_relative_files",
    "_json_payload",
    "_load_release_readiness",
    "_pip_install_command",
    "_resolve_python_executable",
    "_resolve_install_package_spec",
    "_run",
    "_run_rehearsal_step",
    "_run_rehearsal_step_to_file",
    "_write_json",
    "_write_public_rehearsal_toy_repo",
    "_write_rehearsal_auto_discovery_fixture",
    "render_package_install_rehearsal_text",
    "run_package_install_rehearsal",
]


def _repo_has_product_wrapper(repo_path: Path) -> bool:
    return (repo_path / "tools" / "code_mower").is_file()


def _run_external_repo_readiness(
    *,
    code_mower_bin: Path,
    repo_path: Path,
    env: dict[str, str],
    steps: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    """Validate installed CLI behavior against a repo without local wrappers."""

    detect_completed = _run_rehearsal_step(
        [
            str(code_mower_bin),
            "checks",
            "detect",
            "--repo-path",
            str(repo_path),
            "--json",
        ],
        cwd=repo_path,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    detect_payload = _json_payload(detect_completed.stdout)
    dry_run_completed = _run_rehearsal_step(
        [
            str(code_mower_bin),
            "checks",
            "run",
            "--repo-path",
            str(repo_path),
            "--dry-run",
            "--json",
        ],
        cwd=repo_path,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    dry_run_payload = _json_payload(dry_run_completed.stdout)
    doctor_completed = _run_rehearsal_step(
        [
            str(code_mower_bin),
            "doctor",
            "--easy",
            "--json",
        ],
        cwd=repo_path,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    doctor_payload = _json_payload(doctor_completed.stdout)

    check_count = 0
    if isinstance(detect_payload, dict):
        check_count = int(detect_payload.get("check_count") or 0)

    return {
        "mode": "code-mower-external-repo-readiness",
        # _run_rehearsal_step raises RehearsalError on non-zero exit, so reaching
        # this point means all three installed-CLI checks completed successfully.
        "status": "pass",
        "repo_path": str(repo_path),
        "wrapper_present": False,
        "check_count": check_count,
        "checks_detect": detect_payload,
        "checks_dry_run": dry_run_payload,
        "doctor": doctor_payload,
        "note": (
            "No repo-local tools/code_mower wrapper was found, so the rehearsal "
            "validated the installed Code Mower CLI against the repo instead of "
            "running product-wrapper parity."
        ),
    }


def _write_rehearsal_auto_discovery_fixture(path: Path) -> None:
    """Write a tiny GitHub PR-list fixture for offline auto-discovery rehearsal."""

    fixed_head = "b" * 40
    blocked_head = "a" * 40
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        path,
        [
            {
                "number": 1,
                "title": "Clean rehearsal docs update",
                "headRefOid": "c" * 40,
                "baseRefName": "main",
                "changedFiles": 1,
                "comments": [],
                "reviews": [],
            },
            {
                "number": 2,
                "title": "Fix rehearsal blocker",
                "headRefOid": fixed_head,
                "baseRefName": "main",
                "changedFiles": 4,
                "comments": [
                    {
                        "body": (
                            f"Head SHA: `{blocked_head}`\n"
                            "Findings: P0=0, P1=0, P2=1, P3=0\n"
                            "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->\n"
                        )
                    },
                    {
                        "body": (
                            f"Head SHA: `{fixed_head}`\n"
                            "Findings: P0=0, P1=0, P2=0, P3=0\n"
                            "<!-- CODEX_AUDIT_STATE: codex-audit-done -->\n"
                        )
                    },
                ],
                "reviews": [],
            },
        ],
    )


def _resolve_python_executable(python: Path | None) -> Path:
    if python is None:
        return Path(sys.executable)

    raw = python.expanduser()
    if not raw.is_absolute() and len(raw.parts) == 1:
        resolved_command = shutil.which(str(raw))
        if resolved_command:
            return Path(resolved_command).resolve()
        raise ValueError(
            f"Python executable not found on PATH: {raw}. "
            "Use an absolute path such as `--python \"$(command -v python3.12)\"`."
        )

    resolved = raw.resolve()
    if not resolved.exists():
        raise ValueError(f"Python executable does not exist: {resolved}")
    return resolved


def run_package_install_rehearsal(
    *,
    package_spec: str,
    repo_path: Path | None = None,
    local_command: Sequence[str] | None = None,
    python: Path | None = None,
    work_dir: Path | None = None,
    timeout: int = 180,
    shadow_cycles: int = 1,
    standalone_default_cycles: int = 1,
    pip_index_url: str = "",
    pip_extra_index_urls: Sequence[str] | None = None,
) -> dict[str, Any]:
    requested_package_spec = package_spec
    package_spec = _resolve_install_package_spec(package_spec)
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="code-mower-package-install-"))
    else:
        work_dir = work_dir.expanduser().resolve()
    venv_dir = work_dir / "venv"
    toy_repo = work_dir / "toy-repo"
    outputs = work_dir / "outputs"
    if venv_dir.exists() or toy_repo.exists():
        raise ValueError(f"work directory is not clean: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)

    python_bin = _resolve_python_executable(python)
    steps: list[dict[str, Any]] = []

    _run_rehearsal_step(
        [str(python_bin), "-m", "venv", str(venv_dir)],
        cwd=work_dir,
        env=None,
        steps=steps,
        timeout=timeout,
    )
    venv_python = _venv_python(venv_dir)
    code_mower_bin = _venv_code_mower(venv_dir)
    _run_rehearsal_step(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=work_dir,
        env=None,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        _pip_install_command(
            venv_python,
            package_spec,
            pip_index_url=pip_index_url,
            pip_extra_index_urls=pip_extra_index_urls,
        ),
        cwd=work_dir,
        env=None,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [str(venv_python), "-m", "pip", "check"],
        cwd=work_dir,
        env=None,
        steps=steps,
        timeout=timeout,
    )
    version = _run_rehearsal_step(
        [str(code_mower_bin), "--version"],
        cwd=work_dir,
        env=None,
        steps=steps,
        timeout=timeout,
    ).stdout.strip()

    env = os.environ.copy()
    env["PATH"] = f"{code_mower_bin.parent}{os.pathsep}{env.get('PATH', '')}"
    _write_public_rehearsal_toy_repo(
        toy_repo,
        steps=steps,
        env=env,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [str(code_mower_bin), "providers", "list"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [
            str(code_mower_bin),
            "init",
            "--easy",
            "--apply",
            "--output-dir",
            ".code-mower.generated",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        ["bash", ".code-mower.generated/smoke-tests.sh"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [str(code_mower_bin), "doctor", "--easy", "--json"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [str(code_mower_bin), "next-steps", "--profile", "recommended", "--json"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [
            str(code_mower_bin),
            "migration",
            "wrapper-rehearsal",
            "--repo-path",
            str(toy_repo),
            "--local-command",
            str(code_mower_bin),
            "--package-command",
            str(code_mower_bin),
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "calibration",
            "plan",
            ".code-mower.generated/calibration-corpus.json",
            "--replicates",
            "2",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / ".code-mower" / "calibration-plan.json",
    )
    auto_discovery_input = toy_repo / ".code-mower" / "auto-discovery-prs.json"
    _write_rehearsal_auto_discovery_fixture(auto_discovery_input)
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "calibration",
            "auto-discover",
            "--repo",
            "example/toy-repo",
            "--input",
            str(auto_discovery_input),
            "--output",
            ".code-mower/draft-calibration-corpus.json",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=outputs / "auto-discover.json",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "calibration",
            "value-report",
            ".code-mower/draft-calibration-corpus.json",
            "--output",
            ".code-mower/draft-reviewer-value-report.md",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=outputs / "draft-value-report.txt",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "calibration",
            "evidence",
            ".code-mower.generated/calibration-corpus.json",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / "calibration-evidence.json",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "reviewer-metrics",
            "calibration-evidence.json",
            "--spend",
            ".code-mower.generated/reviewer-spend.json",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / "reviewer-metrics.json",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "calibration",
            "policy",
            "reviewer-metrics.json",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / "lane-policy.json",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
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
        steps=steps,
        timeout=timeout,
        stdout_path=outputs / "value-report.txt",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
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
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / "cloud-export.json",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "cloud",
            "upload",
            ".code-mower/cloud-benchmark-bundle",
            "--dry-run",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / "cloud-upload-dry-run.json",
    )
    _run_rehearsal_step_to_file(
        [
            str(code_mower_bin),
            "cloud",
            "dogfood",
            "--repo-path",
            str(toy_repo),
            "--repo-slug",
            "example/toy-repo",
            "--source",
            "package-install-rehearsal",
            "--output-dir",
            ".code-mower/cloud-dogfood-bundle",
            "--endpoint",
            "http://localhost:3000/api/ingest",
            "--json",
        ],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
        stdout_path=toy_repo / "cloud-dogfood-dry-run.json",
    )

    product_wrapper_payload: dict[str, Any] | None = None
    product_mirror_payload: dict[str, Any] | None = None
    external_repo_payload: dict[str, Any] | None = None
    if repo_path is not None:
        repo_path = repo_path.expanduser().resolve()
        if not repo_path.is_dir():
            raise ValueError(f"repo path is not a directory: {repo_path}")
        if local_command or _repo_has_product_wrapper(repo_path):
            product_local_command = (
                tuple(local_command)
                if local_command
                else _default_product_rehearsal_local_command(repo_path)
            )
            product_local_command_text = " ".join(product_local_command)
            wrapper_completed = _run_rehearsal_step(
                [
                    str(code_mower_bin),
                    "migration",
                    "wrapper-rehearsal",
                    "--repo-path",
                    str(repo_path),
                    "--local-command",
                    product_local_command_text,
                    "--package-command",
                    str(code_mower_bin),
                    "--json",
                ],
                cwd=repo_path,
                env=env,
                steps=steps,
                timeout=timeout,
            )
            product_wrapper_payload = _json_payload(wrapper_completed.stdout)
            if (
                not isinstance(product_wrapper_payload, dict)
                or product_wrapper_payload.get("status") != "pass"
            ):
                raise RehearsalError(
                    "product wrapper rehearsal did not pass",
                    steps,
                )
            mirror_completed = _run_rehearsal_step(
                [
                    str(code_mower_bin),
                    "migration",
                    "mirror-removal-plan",
                    "--repo-path",
                    str(repo_path),
                    "--shadow-cycles",
                    str(shadow_cycles),
                    "--standalone-default-cycles",
                    str(standalone_default_cycles),
                    "--json",
                ],
                cwd=repo_path,
                env=env,
                steps=steps,
                timeout=timeout,
            )
            product_mirror_payload = _json_payload(mirror_completed.stdout)
        else:
            external_repo_payload = _run_external_repo_readiness(
                code_mower_bin=code_mower_bin,
                repo_path=repo_path,
                env=env,
                steps=steps,
                timeout=timeout,
            )

    readiness = _first_user_readiness_scorecard(
        toy_repo=toy_repo,
        outputs=outputs,
        version=version,
        steps=steps,
    )
    _write_json(outputs / "first-user-readiness.json", readiness)
    if readiness.get("status") != "pass":
        raise RehearsalError("first-user readiness scorecard did not pass", steps)

    payload = {
        "mode": "code-mower-package-install-rehearsal",
        "status": "pass",
        "package_spec": package_spec,
        "requested_package_spec": requested_package_spec,
        "pip_index_url": pip_index_url,
        "pip_extra_index_urls": list(pip_extra_index_urls or ()),
        "python": str(python_bin),
        "work_dir": str(work_dir),
        "venv_dir": str(venv_dir),
        "code_mower_bin": str(code_mower_bin),
        "version": version,
        "toy_repo": str(toy_repo),
        "first_user_artifacts": _first_user_artifacts(toy_repo),
        "first_user_readiness": readiness,
        "first_user_readiness_path": str(outputs / "first-user-readiness.json"),
        "repo_path": str(repo_path) if repo_path is not None else "",
        "step_count": len(steps),
        "steps": steps,
        "product_wrapper_rehearsal": product_wrapper_payload,
        "product_mirror_removal_plan": product_mirror_payload,
        "external_repo_readiness": external_repo_payload,
    }
    _write_json(outputs / "package-install-rehearsal.json", payload)
    return payload


def render_package_install_rehearsal_text(payload: dict[str, Any]) -> str:
    lines = [
        "Code Mower package-install rehearsal",
        f"Status: {payload['status']}",
        f"Package: {payload['package_spec']}",
        f"Version: {payload.get('version', '')}",
        f"Work dir: {payload['work_dir']}",
        f"Toy repo: {payload['toy_repo']}",
        f"Steps: {payload['step_count']}",
    ]
    readiness = payload.get("first_user_readiness") or {}
    if readiness:
        lines.extend(
            [
                (
                    "First-user readiness: "
                    f"{readiness.get('status', 'unknown')} "
                    f"({readiness.get('passed', 0)}/{readiness.get('total', 0)} passed)"
                ),
                f"Readiness scorecard: {payload.get('first_user_readiness_path', '')}",
            ]
        )
    artifacts = payload.get("first_user_artifacts") or {}
    if artifacts:
        lines.extend(
            [
                f"Value report: {artifacts.get('reviewer_value_report', '')}",
                f"Draft corpus: {artifacts.get('draft_calibration_corpus', '')}",
                f"Draft value report: {artifacts.get('draft_reviewer_value_report', '')}",
                f"Cloud upload dry run: {artifacts.get('cloud_upload_dry_run', '')}",
                f"Cloud dogfood dry run: {artifacts.get('cloud_dogfood_dry_run', '')}",
            ]
        )
    if payload.get("repo_path"):
        lines.extend(
            [
                f"Product repo: {payload['repo_path']}",
                "Product wrapper rehearsal: "
                f"{(payload.get('product_wrapper_rehearsal') or {}).get('status', 'not-run')}",
                "Product mirror-removal status: "
                f"{(payload.get('product_mirror_removal_plan') or {}).get('status', 'not-run')}",
            ]
        )
        external = payload.get("external_repo_readiness") or {}
        if external:
            lines.extend(
                [
                    "External repo readiness: "
                    f"{external.get('status', 'not-run')} "
                    f"({external.get('check_count', 0)} native checks detected)",
                ]
            )
    lines.append("")
    lines.append(
        f"Full JSON: {Path(payload['work_dir']) / 'outputs' / 'package-install-rehearsal.json'}"
    )
    return "\n".join(lines) + "\n"
