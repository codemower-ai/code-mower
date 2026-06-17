#!/usr/bin/env python3
"""Package-install rehearsal helpers for Code Mower migration gates."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower.migration_readiness import (
        FIRST_USER_ARTIFACTS as FIRST_USER_ARTIFACTS,
        PRIVACY_EXCLUDED_CONTENT as PRIVACY_EXCLUDED_CONTENT,
        first_user_artifacts as _first_user_artifacts,
        first_user_readiness_scorecard as _first_user_readiness_scorecard,
    )
else:
    from .migration_readiness import (
        FIRST_USER_ARTIFACTS as FIRST_USER_ARTIFACTS,
        PRIVACY_EXCLUDED_CONTENT as PRIVACY_EXCLUDED_CONTENT,
        first_user_artifacts as _first_user_artifacts,
        first_user_readiness_scorecard as _first_user_readiness_scorecard,
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


def _default_product_rehearsal_local_command(repo_path: Path) -> tuple[str, ...]:
    """Prefer local fallback before mirror removal, wrapper default after it."""

    wrapper = repo_path / "tools" / "code_mower"
    if not wrapper.is_file():
        return ("env", "CODE_MOWER_USE_LOCAL=1", "tools/code_mower")
    mirrored_candidates = _glob_relative_files(repo_path, MIRRORED_IMPLEMENTATION_PATTERNS)
    if mirrored_candidates:
        return ("env", "CODE_MOWER_USE_LOCAL=1", "tools/code_mower")
    return ("tools/code_mower",)


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


def _load_release_readiness() -> Any:
    """Load the release-readiness helper without breaking legacy tools imports."""

    if __package__ in {None, ""}:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    module_names: list[str] = []
    if __package__:
        module_names.append(f"{__package__}.release_readiness")
    module_names.extend(["code_mower.release_readiness", "release_readiness"])
    last_error: ImportError | None = None
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
    raise ImportError("unable to import release_readiness helper") from last_error


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
    _run_rehearsal_step(
        [git, "config", "user.name", "Code Mower Rehearsal"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [git, "config", "user.email", "rehearsal@example.com"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    _run_rehearsal_step(
        [git, "config", "commit.gpgSign", "false"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    (toy_repo / "README.md").write_text(
        "# Code Mower package-install rehearsal\n",
        encoding="utf-8",
    )
    _run_rehearsal_step(
        [git, "add", "README.md"], cwd=toy_repo, env=env, steps=steps, timeout=timeout
    )
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
    lines.append(
        f"Full JSON: {Path(payload['work_dir']) / 'outputs' / 'package-install-rehearsal.json'}"
    )
    return "\n".join(lines) + "\n"


def _json_payload(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _glob_relative_files(repo_path: Path, patterns: Sequence[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        for path in repo_path.glob(pattern):
            if path.is_file():
                found.add(path.relative_to(repo_path).as_posix())
    return sorted(found)
