#!/usr/bin/env python3
"""Rehearse migration from product-local Code Mower tools to the package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
MIRRORED_IMPLEMENTATION_PATTERNS = (
    "tools/code_mower_*.py",
    "tools/*_audit_pr.py",
    "tools/*_labeler.py",
    "tools/lane_prompts/*.md",
    "tools/calibration_corpus*.json",
    "tools/reviewer_spend*.json",
    "tools/context_packs*.json",
    "tools/CODE_MOWER*.md",
)
PRODUCT_SUPPORT_PATTERNS = (
    "tools/run_code_mower_tests.sh",
    "tools/run_code_mower_standalone_rehearsal.sh",
    "tools/run_codex_audit_pr.sh",
    "tools/run_claude_audit_pr.sh",
    "tools/devin_audit_bridge.py",
    "tools/audit_handoff_log.py",
    "tools/request_review.py",
    "tools/safe_gh_comment.py",
    "tools/codex_audit_env.sh",
)
CALIBRATION_EVIDENCE_ADDITIVE_KEYS = frozenset(
    {
        "audit_input_insufficient_count",
        "audit_input_insufficient_runs",
        "result_category",
    }
)
FIRST_USER_ARTIFACTS = (
    ("calibration_plan", ".code-mower/calibration-plan.json"),
    ("draft_calibration_corpus", ".code-mower/draft-calibration-corpus.json"),
    ("draft_reviewer_value_report", ".code-mower/draft-reviewer-value-report.md"),
    ("calibration_evidence", "calibration-evidence.json"),
    ("reviewer_metrics", "reviewer-metrics.json"),
    ("lane_policy", "lane-policy.json"),
    ("reviewer_value_report", "reviewer-value-report.md"),
    ("cloud_export", "cloud-export.json"),
    ("cloud_upload_dry_run", "cloud-upload-dry-run.json"),
    ("cloud_dogfood_dry_run", "cloud-dogfood-dry-run.json"),
)
PRIVACY_EXCLUDED_CONTENT = frozenset(
    {
        "source_code",
        "raw_diffs",
        "raw_model_transcripts",
        "raw_stdout_stderr",
        "auth_probe_output",
        "secrets",
    }
)

RUNNER_ALIASES = (
    {
        "legacy": "tools/gemini_cli_audit_pr.py",
        "standalone": "code-mower gemini-cli",
        "status": "supported",
        "notes": "Gemini CLI compatibility runner.",
    },
    {
        "legacy": "tools/antigravity_cli_audit_pr.py",
        "standalone": "code-mower antigravity-cli",
        "status": "supported",
        "notes": "Preferred Google CLI lane after Antigravity migration.",
    },
    {
        "legacy": "tools/hermes_cli_audit_pr.py",
        "standalone": "code-mower hermes-cli",
        "status": "supported",
        "notes": "Hermes Agent calibration runner; requires explicit ambient-home opt-in.",
    },
    {
        "legacy": "tools/coderabbit_cli_audit_pr.py",
        "standalone": "code-mower coderabbit-cli",
        "status": "supported",
        "notes": "Manual informational CodeRabbit CLI evidence capture.",
    },
    {
        "legacy": "tools/local_llm_audit_pr.py",
        "standalone": "code-mower local-llm audit",
        "status": "supported",
        "notes": "OpenAI-compatible local model audit runner.",
    },
    {
        "legacy": "tools/trailer_comment_labeler.py",
        "standalone": "code-mower trailer-comment-labeler",
        "status": "supported",
        "notes": "Use for structured audit trailer/comment label state.",
    },
    {
        "legacy": "tools/saas_reviewer_labeler.py",
        "standalone": "code-mower saas-reviewer-labeler",
        "status": "supported",
        "notes": "Use for SaaS reviewer event label state.",
    },
    {
        "legacy": "tools/run_codex_audit_pr.sh",
        "standalone": "",
        "status": "product-wrapper",
        "notes": (
            "No generic standalone Codex authoring runner exists yet. Keep the "
            "product wrapper for model invocation/repost artifacts, and use "
            "`code-mower trailer-comment-labeler --lane codex` for merge-bar "
            "label state."
        ),
    },
    {
        "legacy": "tools/run_claude_audit_pr.sh",
        "standalone": "",
        "status": "product-wrapper",
        "notes": (
            "No generic standalone Claude authoring runner exists yet. Keep the "
            "product wrapper for model invocation/repost artifacts, and use "
            "`code-mower trailer-comment-labeler --lane claude` for merge-bar "
            "label state."
        ),
    },
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


class RehearsalError(RuntimeError):
    def __init__(self, message: str, steps: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.steps = steps


def _resolve_command(command_text: str) -> tuple[str, ...]:
    parts = tuple(part for part in command_text.split(" ") if part)
    if not parts:
        raise ValueError("command must not be empty")
    return parts


def _default_local_command(repo_path: Path) -> tuple[str, ...] | None:
    command_candidate = repo_path / "tools" / "code_mower"
    if command_candidate.is_file():
        return (str(command_candidate),)
    candidate = repo_path / "tools" / "code_mower_cli.py"
    if candidate.is_file():
        return (sys.executable, str(candidate))
    return None


def _default_product_rehearsal_local_command(repo_path: Path) -> tuple[str, ...]:
    """Prefer local fallback before mirror removal, wrapper default after it."""

    wrapper = repo_path / "tools" / "code_mower"
    if not wrapper.is_file():
        return ("env", "CODE_MOWER_USE_LOCAL=1", "tools/code_mower")
    mirrored_candidates = _glob_relative_files(repo_path, MIRRORED_IMPLEMENTATION_PATTERNS)
    if mirrored_candidates:
        return ("env", "CODE_MOWER_USE_LOCAL=1", "tools/code_mower")
    return ("tools/code_mower",)


def _default_package_command() -> tuple[str, ...]:
    resolved = shutil.which("code-mower")
    return (resolved or "code-mower",)


def _venv_python(venv_dir: Path) -> Path:
    unix_python = venv_dir / "bin" / "python"
    if unix_python.exists():
        return unix_python
    return venv_dir / "Scripts" / "python.exe"


def _venv_code_mower(venv_dir: Path) -> Path:
    if os.name != "nt":
        return venv_dir / "bin" / "code-mower"
    return venv_dir / "Scripts" / "code-mower.exe"


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


def _run_rehearsal_step(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    steps: list[dict[str, Any]],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    step = {
        "command": list(command),
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout_preview": completed.stdout[-4000:],
        "stderr_preview": completed.stderr[-4000:],
    }
    steps.append(step)
    if completed.returncode != 0:
        raise RehearsalError(
            f"command failed: {' '.join(str(part) for part in command)}",
            steps,
        )
    return completed


def _run_rehearsal_step_to_file(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    steps: list[dict[str, Any]],
    timeout: int,
    stdout_path: Path,
) -> subprocess.CompletedProcess[str]:
    completed = _run_rehearsal_step(
        command,
        cwd=cwd,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    return completed


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _first_user_artifacts(toy_repo: Path) -> dict[str, str]:
    return {
        key: str(toy_repo / relative_path)
        for key, relative_path in FIRST_USER_ARTIFACTS
    }


def _read_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _readiness_check(
    *,
    check_id: str,
    title: str,
    status: str,
    evidence: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": check_id,
        "title": title,
        "status": status,
        "evidence": evidence,
    }
    if detail:
        check["detail"] = detail
    return check


def _artifact_exists_check(
    *,
    check_id: str,
    title: str,
    path: Path,
    min_bytes: int = 1,
) -> dict[str, Any]:
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    return _readiness_check(
        check_id=check_id,
        title=title,
        status="pass" if exists and size >= min_bytes else "fail",
        evidence=str(path),
        detail={"exists": exists, "bytes": size},
    )


def _step_succeeded(steps: Sequence[dict[str, Any]], *needles: str) -> bool:
    for step in steps:
        if step.get("returncode") != 0:
            continue
        command = " ".join(str(part) for part in step.get("command", ()))
        if all(needle in command for needle in needles):
            return True
    return False


def _cloud_dry_run_check(
    *,
    check_id: str,
    title: str,
    path: Path,
    upload_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = upload_payload if upload_payload is not None else _read_json_file(path)
    upload = payload.get("upload") if isinstance(payload, dict) else None
    if not isinstance(upload, dict):
        upload = payload if isinstance(payload, dict) else {}
    excluded = set(upload.get("excluded_content") or ())
    missing_exclusions = sorted(PRIVACY_EXCLUDED_CONTENT - excluded)
    detail = {
        "mode": upload.get("mode"),
        "privacy_mode": upload.get("privacy_mode"),
        "requires_yes": upload.get("requires_yes"),
        "would_upload": upload.get("would_upload"),
        "missing_exclusions": missing_exclusions,
    }
    passes = (
        path.is_file()
        and upload.get("mode") == "cloud-upload-dry-run"
        and upload.get("privacy_mode") == "metadata_and_reports"
        and upload.get("requires_yes") is True
        and upload.get("would_upload") is False
        and not missing_exclusions
    )
    return _readiness_check(
        check_id=check_id,
        title=title,
        status="pass" if passes else "fail",
        evidence=str(path),
        detail=detail,
    )


def _first_user_readiness_scorecard(
    *,
    toy_repo: Path,
    outputs: Path,
    version: str,
    steps: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    artifacts = _first_user_artifacts(toy_repo)
    generated_dir = toy_repo / ".code-mower.generated"
    cloud_upload_path = Path(artifacts["cloud_upload_dry_run"])
    dogfood_path = Path(artifacts["cloud_dogfood_dry_run"])
    cloud_export_payload = _read_json_file(Path(artifacts["cloud_export"]))
    dogfood_payload = _read_json_file(dogfood_path)
    dogfood_upload = (
        dogfood_payload.get("upload") if isinstance(dogfood_payload, dict) else None
    )

    checks = [
        _readiness_check(
            check_id="package-installed",
            title="Package installs and exposes the CLI",
            status="pass" if version.startswith("code-mower ") else "fail",
            evidence=version,
        ),
        _readiness_check(
            check_id="easy-init-generated",
            title="Easy-mode setup writes reviewable generated files",
            status=(
                "pass"
                if (
                    (generated_dir / "code-mower-init-plan.json").is_file()
                    and (generated_dir / "smoke-tests.sh").is_file()
                    and (generated_dir / "tools" / "code_mower").is_file()
                )
                else "fail"
            ),
            evidence=str(generated_dir),
        ),
        _readiness_check(
            check_id="doctor-ran",
            title="First-run doctor completes",
            status="pass" if _step_succeeded(steps, "doctor", "--easy") else "fail",
            evidence="code-mower doctor --easy --json",
        ),
        _artifact_exists_check(
            check_id="draft-calibration-corpus",
            title="Auto-discovery creates a reviewable draft corpus",
            path=Path(artifacts["draft_calibration_corpus"]),
        ),
        _artifact_exists_check(
            check_id="draft-value-report",
            title="Draft corpus can produce a reviewer value report",
            path=Path(artifacts["draft_reviewer_value_report"]),
        ),
        _artifact_exists_check(
            check_id="starter-value-report",
            title="Starter corpus can produce the first value report",
            path=Path(artifacts["reviewer_value_report"]),
        ),
        _readiness_check(
            check_id="cloud-export-metadata-bundle",
            title="Cloud export creates a metadata/report bundle",
            status=(
                "pass"
                if (
                    isinstance(cloud_export_payload, dict)
                    and cloud_export_payload.get("mode") == "cloud-export"
                    and len(cloud_export_payload.get("included_reports") or ()) >= 3
                    and cloud_export_payload.get("upload_ready") is False
                )
                else "fail"
            ),
            evidence=artifacts["cloud_export"],
        ),
        _cloud_dry_run_check(
            check_id="cloud-upload-dry-run-privacy",
            title="Cloud upload stays dry-run and excludes private content",
            path=cloud_upload_path,
        ),
        _readiness_check(
            check_id="cloud-dogfood-dry-run",
            title="CodeMower.com dogfood path stays dry-run by default",
            status=(
                "pass"
                if (
                    isinstance(dogfood_payload, dict)
                    and dogfood_payload.get("status") == "dry_run"
                )
                else "fail"
            ),
            evidence=str(dogfood_path),
        ),
        _cloud_dry_run_check(
            check_id="cloud-dogfood-upload-privacy",
            title="Dogfood upload preview excludes private content",
            path=dogfood_path,
            upload_payload=dogfood_upload,
        ),
    ]
    passed = sum(1 for check in checks if check["status"] == "pass")
    failed = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    scorecard = {
        "mode": "code-mower-first-user-readiness",
        "status": "pass" if failed == 0 else "fail",
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "total": len(checks),
        "checks": checks,
        "artifact": str(outputs / "first-user-readiness.json"),
    }
    return scorecard


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


def _resolve_install_package_spec(package_spec: str, *, base_dir: Path | None = None) -> str:
    candidate_text = package_spec.strip()
    if not candidate_text:
        return package_spec
    if candidate_text.startswith(("git+", "http://", "https://")):
        return package_spec
    looks_path_like = (
        candidate_text.startswith((".", "/", "~"))
        or os.sep in candidate_text
        or (os.altsep is not None and os.altsep in candidate_text)
    )
    if not looks_path_like:
        return package_spec

    base = (base_dir or Path.cwd()).expanduser().resolve()
    candidate = Path(candidate_text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        return package_spec
    return str(candidate)


def _pip_install_command(
    venv_python: Path,
    package_spec: str,
    *,
    pip_index_url: str = "",
    pip_extra_index_urls: Sequence[str] | None = None,
) -> list[str]:
    command = [str(venv_python), "-m", "pip", "install"]
    if pip_index_url:
        command.extend(["--index-url", pip_index_url])
    for extra_index_url in pip_extra_index_urls or ():
        if extra_index_url:
            command.extend(["--extra-index-url", extra_index_url])
    command.append(package_spec)
    return command


def _write_public_rehearsal_toy_repo(
    toy_repo: Path,
    *,
    steps: list[dict[str, Any]],
    env: dict[str, str],
    timeout: int,
) -> None:
    toy_repo.mkdir(parents=True)
    git = shutil.which("git")
    if not git:
        (toy_repo / "README.md").write_text(
            "# Code Mower package-install rehearsal\n",
            encoding="utf-8",
        )
        return
    _run_rehearsal_step([git, "init", "-q"], cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    _run_rehearsal_step([git, "config", "user.name", "Code Mower Rehearsal"], cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    _run_rehearsal_step([git, "config", "user.email", "rehearsal@example.com"], cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    _run_rehearsal_step([git, "config", "commit.gpgSign", "false"], cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    (toy_repo / "README.md").write_text(
        "# Code Mower package-install rehearsal\n",
        encoding="utf-8",
    )
    _run_rehearsal_step([git, "add", "README.md"], cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    _run_rehearsal_step(
        [git, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "Initial rehearsal repo"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )


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

    python_bin = python.expanduser().resolve() if python else Path(sys.executable)
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
    if repo_path is not None:
        repo_path = repo_path.expanduser().resolve()
        if not repo_path.is_dir():
            raise ValueError(f"repo path is not a directory: {repo_path}")
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
                f"{payload.get('product_wrapper_rehearsal', {}).get('status', 'not-run')}",
                "Product mirror-removal status: "
                f"{payload.get('product_mirror_removal_plan', {}).get('status', 'not-run')}",
            ]
        )
    lines.append("")
    lines.append(f"Full JSON: {Path(payload['work_dir']) / 'outputs' / 'package-install-rehearsal.json'}")
    return "\n".join(lines) + "\n"


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


def _relative_existing_files(repo_path: Path, candidates: Sequence[str]) -> list[str]:
    return [
        candidate
        for candidate in candidates
        if (repo_path / candidate).exists()
    ]


def _glob_relative_files(repo_path: Path, patterns: Sequence[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        for path in repo_path.glob(pattern):
            if path.is_file():
                found.add(path.relative_to(repo_path).as_posix())
    return sorted(found)


def _workflow_file_references(
    repo_path: Path,
    relative_files: Sequence[str],
) -> list[dict[str, Any]]:
    workflow_root = repo_path / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
    tracked = set(relative_files)
    references: list[dict[str, Any]] = []
    for workflow in sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in workflow_root.glob(pattern)
        if path.is_file()
    ):
        try:
            lines = workflow.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = workflow.read_text(encoding="utf-8", errors="replace").splitlines()
        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for relative_file in tracked:
                if relative_file in line and _line_requires_workflow_file(
                    line,
                    relative_file,
                ):
                    references.append(
                        {
                            "workflow": workflow.relative_to(repo_path).as_posix(),
                            "line": line_number,
                            "file": relative_file,
                            "text": stripped[:240],
                        }
                    )
    return references


def _workflow_local_fallback_references(repo_path: Path) -> list[dict[str, Any]]:
    workflow_root = repo_path / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
    references: list[dict[str, Any]] = []
    for workflow in sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in workflow_root.glob(pattern)
        if path.is_file()
    ):
        try:
            lines = workflow.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = workflow.read_text(encoding="utf-8", errors="replace").splitlines()
        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if (
                "CODE_MOWER_USE_LOCAL=1" in line
                and "tools/code_mower" in line
            ):
                references.append(
                    {
                        "workflow": workflow.relative_to(repo_path).as_posix(),
                        "line": line_number,
                        "text": stripped[:240],
                    }
                )
    return references


def _line_requires_workflow_file(line: str, relative_file: str) -> bool:
    escaped = re.escape(relative_file)
    return bool(
        re.search(rf"\bpython3?\b[^\n]*{escaped}", line)
        or re.search(rf"\[\s*!\s*-f\s+{escaped}\s*\]", line)
        or re.search(rf"\btest\s+!\s+-f\s+{escaped}\b", line)
        or re.search(rf"\btest\s+-f\s+{escaped}\b", line)
    )


def render_mirror_removal_plan(
    *,
    repo_path: Path,
    shadow_cycles: int,
    required_shadow_cycles: int,
    standalone_default_cycles: int,
    required_standalone_default_cycles: int,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo path is not a directory: {repo_path}")

    support_files = _relative_existing_files(
        repo_path,
        (
            "tools/code_mower",
            "tools/code_mower_standalone_pin.env",
            "tools/code_mower_standalone_shadow.sh",
        ),
    )
    product_support_files = _relative_existing_files(
        repo_path,
        PRODUCT_SUPPORT_PATTERNS,
    )
    local_command = _default_local_command(repo_path)
    mirrored_candidates = _glob_relative_files(
        repo_path,
        MIRRORED_IMPLEMENTATION_PATTERNS,
    )
    workflow_mirror_references = _workflow_file_references(
        repo_path,
        mirrored_candidates,
    )
    workflow_local_fallback_references = _workflow_local_fallback_references(
        repo_path,
    )
    mirrors_absent = (
        not mirrored_candidates
        and not workflow_mirror_references
        and not workflow_local_fallback_references
    )
    ready_for_shadow = {
        "standalone_pin_file_present": "tools/code_mower_standalone_pin.env"
        in support_files,
        "standalone_shadow_wrapper_present": "tools/code_mower_standalone_shadow.sh"
        in support_files,
        "product_local_command_present": local_command is not None,
        "mirrored_files_detected": bool(mirrored_candidates),
    }
    support_ready = (
        ready_for_shadow["standalone_pin_file_present"]
        and ready_for_shadow["standalone_shadow_wrapper_present"]
        and ready_for_shadow["product_local_command_present"]
    )
    shadow_ready = support_ready and shadow_cycles >= required_shadow_cycles
    cycle_ready_for_removal = (
        shadow_ready
        and standalone_default_cycles >= required_standalone_default_cycles
    )
    removal_ready = (
        cycle_ready_for_removal
        and not workflow_mirror_references
        and not workflow_local_fallback_references
    )
    blockers = []
    if not mirrors_absent and not ready_for_shadow["standalone_pin_file_present"]:
        blockers.append("add tools/code_mower_standalone_pin.env")
    if not mirrors_absent and not ready_for_shadow["standalone_shadow_wrapper_present"]:
        blockers.append("add tools/code_mower_standalone_shadow.sh")
    if not mirrors_absent and not ready_for_shadow["product_local_command_present"]:
        blockers.append("identify the product-local Code Mower command")
    if not mirrors_absent and shadow_cycles < required_shadow_cycles:
        blockers.append(
            f"complete {required_shadow_cycles - shadow_cycles} more clean shadow cycle(s)"
        )
    if shadow_ready and standalone_default_cycles < required_standalone_default_cycles:
        blockers.append(
            "flip to pinned standalone by default and complete "
            f"{required_standalone_default_cycles - standalone_default_cycles} "
            "clean standalone-default cycle(s)"
        )
    if workflow_mirror_references:
        blockers.append(
            "migrate workflow references from removable mirrored files to "
            "standalone wrapper commands before deleting mirrors"
        )
    if workflow_local_fallback_references:
        blockers.append(
            "remove CODE_MOWER_USE_LOCAL=1 workflow fallback calls before "
            "deleting mirrors; private repos need a public/package install path "
            "or authenticated standalone checkout for Actions"
        )
    if mirrors_absent and support_ready:
        status = "mirrors_removed"
    elif mirrors_absent:
        status = "no_mirrors_detected"
    elif removal_ready:
        status = "ready_to_remove_mirrors"
    elif cycle_ready_for_removal and workflow_local_fallback_references:
        status = "local_fallback_dependency_blocks_mirror_removal"
    elif cycle_ready_for_removal:
        status = "workflow_entrypoint_migration_required"
    elif shadow_ready:
        status = "ready_to_flip_default"
    else:
        status = "shadow_required"
    return {
        "mode": "code-mower-mirror-removal-plan",
        "repo_path": str(repo_path),
        "status": status,
        "shadow_cycles": shadow_cycles,
        "required_shadow_cycles": required_shadow_cycles,
        "standalone_default_cycles": standalone_default_cycles,
        "required_standalone_default_cycles": required_standalone_default_cycles,
        "support_files": support_files,
        "support_file_count": len(support_files),
        "product_support_files": product_support_files,
        "product_support_file_count": len(product_support_files),
        "local_command": list(local_command or ()),
        "mirrored_file_count": len(mirrored_candidates),
        "mirrored_files": mirrored_candidates,
        "workflow_mirrored_file_reference_count": len(workflow_mirror_references),
        "workflow_mirrored_file_references": workflow_mirror_references,
        "workflow_local_fallback_reference_count": len(
            workflow_local_fallback_references
        ),
        "workflow_local_fallback_references": workflow_local_fallback_references,
        "readiness": ready_for_shadow,
        "mirrors_absent": mirrors_absent,
        "blockers": blockers,
        "steps": [
            "Run code-mower migration wrapper-rehearsal against the pinned standalone release and require mismatch_count: 0.",
            "Run at least the required number of clean product release cycles with the pinned standalone wrapper available.",
            "Move workflow calls from mirrored Python files to tools/code_mower standalone wrapper subcommands.",
            "Flip product workflows or wrapper defaults to the pinned standalone command while keeping the product-local mirrors in place.",
            "For private standalone repos, configure authenticated Actions checkout or wait for a public/package install path before removing local mirrors.",
            "Run the normal product merge gates and post-merge deploy checks.",
            "Remove mirrored implementation files in a dedicated PR after the standalone default cycle stays clean.",
        ],
        "notes": [
            "This plan is intentionally conservative: mirrored files are inventory, not deletion approval.",
            "Keep support wrappers such as tools/code_mower and the standalone pin/shadow files during mirror removal.",
            "Product-specific support files may remain after mirrored implementation files are removed.",
            "CODE_MOWER_USE_LOCAL=1 workflow calls are allowed for private-repo safety, but they intentionally depend on repo-local mirror files.",
            "Keep product feature work independent from mirror-removal PRs.",
        ],
    }


def render_mirror_removal_text(payload: dict[str, Any]) -> str:
    lines = [
        "Code Mower mirror-removal plan",
        f"Status: {payload['status']}",
        f"Repo: {payload['repo_path']}",
        f"Shadow cycles: {payload['shadow_cycles']}/{payload['required_shadow_cycles']}",
        "Standalone-default cycles: "
        f"{payload['standalone_default_cycles']}/"
        f"{payload['required_standalone_default_cycles']}",
        f"Mirrored files detected: {payload['mirrored_file_count']}",
        "Workflow mirrored-file references: "
        f"{payload.get('workflow_mirrored_file_reference_count', 0)}",
        "Workflow local-fallback references: "
        f"{payload.get('workflow_local_fallback_reference_count', 0)}",
        "",
        "Next steps:",
    ]
    for step in payload["steps"]:
        lines.append(f"- {step}")
    if payload.get("mirrors_absent"):
        lines.append("")
        lines.append(
            "Mirror inventory is empty: no removable mirrored implementation files "
            "or workflow references were detected."
        )
    if payload["blockers"]:
        lines.append("")
        lines.append("Blockers:")
        for blocker in payload["blockers"]:
            lines.append(f"- {blocker}")
    return "\n".join(lines) + "\n"


def render_runner_aliases(*, legacy: str | None = None) -> dict[str, Any]:
    aliases = [dict(row) for row in RUNNER_ALIASES]
    if legacy:
        needle = legacy.strip()
        aliases = [
            row
            for row in aliases
            if row["legacy"] == needle or Path(row["legacy"]).name == needle
        ]
    return {
        "mode": "code-mower-runner-aliases",
        "status": "pass",
        "aliases": aliases,
    }


def render_runner_aliases_text(payload: dict[str, Any]) -> str:
    lines = ["Code Mower runner aliases", ""]
    aliases = payload.get("aliases", [])
    if not aliases:
        lines.append("No aliases matched.")
        return "\n".join(lines) + "\n"
    for row in aliases:
        standalone = row.get("standalone") or "(no generic standalone alias)"
        lines.append(f"- {row['legacy']} -> {standalone}")
        lines.append(f"  status: {row['status']}")
        lines.append(f"  notes: {row['notes']}")
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
    mirror = subparsers.add_parser("mirror-removal-plan")
    mirror.add_argument("--repo-path", type=Path, default=Path.cwd())
    mirror.add_argument("--shadow-cycles", type=int, default=0)
    mirror.add_argument("--required-shadow-cycles", type=int, default=1)
    mirror.add_argument("--standalone-default-cycles", type=int, default=0)
    mirror.add_argument("--required-standalone-default-cycles", type=int, default=1)
    mirror.add_argument("--json", action="store_true")
    aliases = subparsers.add_parser("runner-aliases")
    aliases.add_argument(
        "--legacy",
        default=None,
        help="optional legacy script path or basename to filter, e.g. run_codex_audit_pr.sh",
    )
    aliases.add_argument("--json", action="store_true")
    package_install = subparsers.add_parser("package-install-rehearsal")
    package_install.add_argument(
        "--package-spec",
        default="code-mower",
        help=(
            "package spec to pip install into a clean venv; use a local path, "
            "git URL, or package index name"
        ),
    )
    package_install.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="optional product repo to compare against the installed package",
    )
    package_install.add_argument(
        "--local-command",
        default="",
        help=(
            "product-local command prefix for --repo-path, e.g. "
            "'env CODE_MOWER_USE_LOCAL=1 tools/code_mower'"
        ),
    )
    package_install.add_argument(
        "--python",
        type=Path,
        default=None,
        help="Python 3.11+ executable used to create the clean rehearsal venv",
    )
    package_install.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="empty or absent directory for venv, toy repo, and JSON outputs",
    )
    package_install.add_argument(
        "--pip-index-url",
        default="",
        help="optional pip --index-url for package-install rehearsal",
    )
    package_install.add_argument(
        "--pip-extra-index-url",
        action="append",
        default=[],
        help="optional pip --extra-index-url; may be provided multiple times",
    )
    package_install.add_argument("--timeout", type=int, default=180)
    package_install.add_argument("--shadow-cycles", type=int, default=1)
    package_install.add_argument("--standalone-default-cycles", type=int, default=1)
    package_install.add_argument("--json", action="store_true")
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

    if args.command == "mirror-removal-plan":
        try:
            payload = render_mirror_removal_plan(
                repo_path=args.repo_path,
                shadow_cycles=args.shadow_cycles,
                required_shadow_cycles=args.required_shadow_cycles,
                standalone_default_cycles=args.standalone_default_cycles,
                required_standalone_default_cycles=args.required_standalone_default_cycles,
            )
        except ValueError as exc:
            payload = {
                "mode": "code-mower-mirror-removal-plan",
                "status": "fail",
                "error": str(exc),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"mirror-removal plan failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_mirror_removal_text(payload), end="")
        return 0

    if args.command == "runner-aliases":
        payload = render_runner_aliases(legacy=args.legacy)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_runner_aliases_text(payload), end="")
        return 0

    if args.command == "package-install-rehearsal":
        try:
            payload = run_package_install_rehearsal(
                package_spec=args.package_spec,
                repo_path=args.repo_path,
                local_command=_resolve_command(args.local_command)
                if args.local_command
                else None,
                python=args.python,
                work_dir=args.work_dir,
                timeout=args.timeout,
                shadow_cycles=args.shadow_cycles,
                standalone_default_cycles=args.standalone_default_cycles,
                pip_index_url=args.pip_index_url,
                pip_extra_index_urls=args.pip_extra_index_url,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError, RehearsalError) as exc:
            payload = {
                "mode": "code-mower-package-install-rehearsal",
                "status": "fail",
                "error": str(exc),
                "steps": getattr(exc, "steps", []),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"package-install rehearsal failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_package_install_rehearsal_text(payload), end="")
        return 0

    raise AssertionError(f"unhandled migration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
