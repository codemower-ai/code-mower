#!/usr/bin/env python3
"""Run provider-neutral Code Mower setup and runtime checks."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import config as code_mower_config
        from code_mower import local_llm_profiles
        from code_mower import package as code_mower_package
        from code_mower import secrets as code_mower_secrets
        from code_mower.doctor_checks import (
            STATUS_FAIL,
            STATUS_PASS,
            STATUS_SKIP,
            STATUS_WARN,
            DoctorCheck,
            DoctorReport,
        )
    else:
        from tools import (
            code_mower_config,
            code_mower_package,
            code_mower_secrets,
            local_llm_profiles,
        )
        from tools.doctor_checks import (
            STATUS_FAIL,
            STATUS_PASS,
            STATUS_SKIP,
            STATUS_WARN,
            DoctorCheck,
            DoctorReport,
        )
elif __package__ == "tools":
    from tools import (
        code_mower_config,
        code_mower_package,
        code_mower_secrets,
        local_llm_profiles,
    )
    from tools.doctor_checks import (
        STATUS_FAIL,
        STATUS_PASS,
        STATUS_SKIP,
        STATUS_WARN,
        DoctorCheck,
        DoctorReport,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import config as code_mower_config
    from . import local_llm_profiles
    from . import package as code_mower_package
    from . import secrets as code_mower_secrets
    from .doctor_checks import (
        STATUS_FAIL,
        STATUS_PASS,
        STATUS_SKIP,
        STATUS_WARN,
        DoctorCheck,
        DoctorReport,
    )

SUPPORTED_TOKEN_FILE_ENV_NAMES = frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"})
TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
ACTIONS_BILLING_BLOCK_PATTERNS = (
    "recent account payments have failed",
    "spending limit needs to be increased",
)
ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "action_required", "timed_out", "startup_failure"}
)
MAX_ACTIONS_FAILED_RUNS_TO_INSPECT = 5
MAX_ACTIONS_FAILED_JOBS_TO_INSPECT = 20
ACTIONS_COST_SAMPLE_DEFAULT = 100
ACTIONS_COST_SAMPLE_MAX = 100
ACTIONS_METADATA_WORKFLOW_MARKERS = (
    "audit-labeler",
    "labeler",
    "clear-stale",
    "audit-label-cleanup",
    "devin-audit-bridge",
)
DEFAULT_CLOUD_TOKEN_ENV = "CODE_MOWER_CLOUD_TOKEN"
DEFAULT_CLOUD_TOKEN_DIR = Path("~/.config/code-mower/tokens")


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise code_mower_config.ConfigError(f"{name} must be a mapping")


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def resolve_doctor_config_path_for_script(
    config_arg: str,
    *,
    easy: bool = False,
    script_path: Path,
) -> Path:
    path = Path(config_arg)
    if path.is_file() or config_arg != "code-mower.yml" or not easy:
        return path

    script_path = script_path.resolve()
    candidates = [
        script_path.parent / "templates" / "code-mower.example.yml",
        script_path.parents[1] / "code-mower.example.yml",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return path


def resolve_doctor_config_path(config_arg: str, *, easy: bool = False) -> Path:
    return resolve_doctor_config_path_for_script(
        config_arg,
        easy=easy,
        script_path=Path(__file__),
    )


def _join_names(names: Sequence[str]) -> str:
    return ", ".join(str(name) for name in names)


def _token_remediation(
    missing: Sequence[str],
    missing_any: Sequence[Sequence[str]],
) -> str:
    parts: list[str] = []
    if missing:
        parts.append(
            "set "
            + _join_names(missing)
            + " in your shell or GitHub secret store before enabling this lane"
        )
    for group in missing_any:
        names = [str(name) for name in group]
        if not names:
            continue
        if set(names) & {"GEMINI_API_KEY", "GOOGLE_API_KEY"}:
            parts.append(
                "set GEMINI_API_KEY or GOOGLE_API_KEY, or run "
                "`code-mower init auth gemini` and export GEMINI_API_KEY_FILE"
            )
        else:
            parts.append("set one of " + _join_names(names))
    if not parts:
        return ""
    return "; ".join(parts) + "."


def _local_cli_remediation(commands: Sequence[str], command_env: str = "") -> str:
    command_text = " or ".join(str(command) for command in commands if command)
    if not command_text:
        return (
            "Configure provider_config.command or a non-empty provider id for "
            "this lane, then rerun doctor."
        )
    if command_env:
        return (
            f"Install {command_text}, or set {command_env} to the absolute path "
            "of the provider CLI, then rerun doctor."
        )
    return f"Install {command_text} and ensure it is on PATH, then rerun doctor."


def _load_inputs(
    config_path: Path,
    provider_templates_path: Path,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, list[DoctorCheck]]:
    checks: list[DoctorCheck] = []
    config: Mapping[str, Any] | None = None
    templates: Mapping[str, Any] | None = None

    try:
        config = code_mower_config.load_config(config_path)
        issues = code_mower_config.validate_config(config)
    except (OSError, code_mower_config.ConfigError) as exc:
        checks.append(
            DoctorCheck(
                name="config.load",
                status=STATUS_FAIL,
                message=f"cannot load config: {exc}",
                remediation=(
                    "Run `code-mower init --easy` to render a starter config, "
                    "or pass an existing config path to doctor."
                ),
            )
        )
    else:
        if issues:
            checks.append(
                DoctorCheck(
                    name="config.validate",
                    status=STATUS_FAIL,
                    message=code_mower_config._format_issues(issues),
                    remediation=(
                        "Fix the listed config issues or regenerate the starter "
                        "setup with `code-mower init --easy`."
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="config.validate",
                    status=STATUS_PASS,
                    message="config validates",
                )
            )

    try:
        templates = code_mower_package.load_provider_templates(provider_templates_path)
    except (OSError, code_mower_config.ConfigError) as exc:
        checks.append(
            DoctorCheck(
                name="provider_templates.load",
                status=STATUS_FAIL,
                message=f"cannot load provider templates: {exc}",
                remediation=(
                    "Run from the repository root, install the packaged templates, "
                    "or pass --provider-templates with a readable catalog path."
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="provider_templates.load",
                status=STATUS_PASS,
                message="provider templates load",
            )
        )

    return config, templates, checks


def _selected_lanes(
    config: Mapping[str, Any],
    profile: str | None,
) -> tuple[str, ...]:
    lanes = _as_mapping(config.get("lanes"), "lanes")
    if not profile:
        return tuple(str(lane_id) for lane_id in sorted(lanes))
    profiles = _as_mapping(config.get("profiles", {}), "profiles")
    profile_config = profiles.get(profile)
    if not isinstance(profile_config, Mapping):
        known = ", ".join(sorted(str(item) for item in profiles))
        raise code_mower_config.ConfigError(
            f"unknown profile {profile!r}; known profiles: {known}"
        )
    return tuple(str(lane_id) for lane_id in _as_sequence(profile_config.get("lanes", [])))


def _provider_template_coverage(
    lanes: Sequence[str],
    templates: Mapping[str, Any],
) -> DoctorCheck:
    provider_templates = _as_mapping(
        templates.get("provider_templates"),
        "provider_templates",
    )
    missing = sorted(set(lanes) - set(str(item) for item in provider_templates))
    if missing:
        return DoctorCheck(
            name="provider_templates.coverage",
            status=STATUS_FAIL,
            message=f"provider templates missing selected lanes: {', '.join(missing)}",
            detail={"missing_lanes": missing},
            remediation=(
                "Add these lane ids to the provider catalog or remove them from "
                "the selected profile before running audits."
            ),
        )
    return DoctorCheck(
        name="provider_templates.coverage",
        status=STATUS_PASS,
        message="provider templates cover selected lanes",
    )


def _check_python_runtime() -> DoctorCheck:
    version = ".".join(str(part) for part in sys.version_info[:3])
    status = STATUS_PASS if sys.version_info >= (3, 11) else STATUS_FAIL
    return DoctorCheck(
        name="runtime.python",
        status=status,
        message=(
            f"Python {version} satisfies Code Mower's >=3.11 requirement"
            if status == STATUS_PASS
            else f"Python {version} is too old; Code Mower requires >=3.11"
        ),
        detail={
            "executable": sys.executable,
            "version": version,
            "required": ">=3.11",
        },
        remediation=(
            None
            if status == STATUS_PASS
            else "Run Code Mower with Python >=3.11, then rerun doctor."
        ),
    )


def _auth_probe_output_detail(output: str) -> dict[str, Any]:
    """Return non-content diagnostics for auth probes.

    Auth CLIs often print account names, hostnames, scopes, or token hints even
    on success. Keep enough shape for debugging without persisting that text in
    machine-readable doctor reports.
    """

    text = output.strip()
    return {
        "output_redacted": bool(text),
        "output_line_count": len(text.splitlines()) if text else 0,
    }


def _check_github_auth_surface(
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> DoctorCheck:
    token_env = [name for name in ("GITHUB_TOKEN", "GH_TOKEN") if os.environ.get(name)]
    gh_path = shutil.which("gh")
    if token_env:
        if probe_runtime and gh_path:
            try:
                completed = subprocess.run(
                    [gh_path, "auth", "status"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=http_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_WARN,
                    message=f"GitHub token env is set, but auth probe failed: {exc}",
                    detail={"token_env": token_env, "gh_path": gh_path},
                    remediation=(
                        "Run `gh auth status` locally, refresh the token if needed, "
                        "or export a valid GITHUB_TOKEN/GH_TOKEN."
                    ),
                )
            output = (completed.stdout or completed.stderr or "").strip()
            if completed.returncode == 0:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_PASS,
                    message="GitHub token env and CLI auth probe are valid",
                    detail={
                        "token_env": token_env,
                        "gh_path": gh_path,
                        "returncode": completed.returncode,
                        **_auth_probe_output_detail(output),
                    },
                )
            return DoctorCheck(
                name="runtime.github_auth",
                status=STATUS_WARN,
                message=f"GitHub token env is set, but auth probe exited {completed.returncode}",
                detail={
                    "token_env": token_env,
                    "gh_path": gh_path,
                    "returncode": completed.returncode,
                    **_auth_probe_output_detail(output),
                },
                remediation=(
                    "Run `gh auth status`, refresh CLI auth with `gh auth login`, "
                    "or export a valid GITHUB_TOKEN/GH_TOKEN."
                ),
            )
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_PASS,
            message="GitHub token env is set",
            detail={"token_env": token_env, "gh_path": gh_path or ""},
        )
    if gh_path:
        if probe_runtime:
            try:
                completed = subprocess.run(
                    [gh_path, "auth", "status"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=http_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_WARN,
                    message=f"GitHub CLI auth probe failed: {exc}",
                    detail={"token_env": [], "gh_path": gh_path},
                    remediation=(
                        "Run `gh auth login` or export GITHUB_TOKEN/GH_TOKEN, "
                        "then rerun doctor."
                    ),
                )
            output = (completed.stdout or completed.stderr or "").strip()
            if completed.returncode == 0:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_PASS,
                    message="GitHub CLI auth probe succeeded",
                    detail={
                        "token_env": [],
                        "gh_path": gh_path,
                        "returncode": completed.returncode,
                        **_auth_probe_output_detail(output),
                    },
                )
            return DoctorCheck(
                name="runtime.github_auth",
                status=STATUS_WARN,
                message=f"GitHub CLI auth probe exited {completed.returncode}",
                detail={
                    "token_env": [],
                    "gh_path": gh_path,
                    "returncode": completed.returncode,
                    **_auth_probe_output_detail(output),
                },
                remediation=(
                    "Run `gh auth login` or export GITHUB_TOKEN/GH_TOKEN, "
                    "then rerun doctor."
                ),
            )
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_WARN,
            message="GitHub CLI is available, but token env is not set and auth was not probed",
            detail={"token_env": [], "gh_path": gh_path},
            remediation=(
                "Run with --probe-runtime to verify gh auth, or export "
                "GITHUB_TOKEN/GH_TOKEN before enabling GitHub-backed lanes."
            ),
        )
    return DoctorCheck(
        name="runtime.github_auth",
        status=STATUS_WARN,
        message="neither GITHUB_TOKEN/GH_TOKEN nor gh CLI was found",
        detail={"token_env": [], "gh_path": ""},
        remediation=(
            "Install the GitHub CLI and run `gh auth login`, or export "
            "GITHUB_TOKEN/GH_TOKEN."
        ),
    )


def _check_ripgrep() -> DoctorCheck:
    path = shutil.which("rg")
    if path:
        return DoctorCheck(
            name="runtime.ripgrep",
            status=STATUS_PASS,
            message="rg found",
            detail={"command": "rg", "path": path},
        )
    return DoctorCheck(
        name="runtime.ripgrep",
        status=STATUS_WARN,
        message="rg was not found; reviewer CLIs may fall back to slower grep tools",
        detail={"command": "rg"},
        remediation=(
            "Install ripgrep, for example `brew install ripgrep` on macOS or "
            "`apt-get install ripgrep` on Ubuntu, and ensure rg is on PATH."
        ),
    )


def _check_pytest() -> DoctorCheck:
    spec = importlib.util.find_spec("pytest")
    if spec is not None:
        return DoctorCheck(
            name="runtime.pytest",
            status=STATUS_PASS,
            message="pytest import is available for product-side test wrappers",
            detail={"module": "pytest"},
        )
    return DoctorCheck(
        name="runtime.pytest",
        status=STATUS_WARN,
        message=(
            "pytest is not installed in this Python environment; standalone "
            "easy-mode does not require it, but product-side Code Mower test "
            "wrappers often do"
        ),
        detail={"module": "pytest"},
        remediation=(
            "Install pytest in the product repository virtualenv before running "
            "product-side wrapper tests, for example `python -m pip install pytest`."
        ),
    )


def _token_file_mentions_cloud_token(path: Path, token_env: str) -> bool | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if stripped.startswith(f"{token_env}="):
            return True
    return False


def _check_cloud_token_surface(
    *,
    token_env: str = DEFAULT_CLOUD_TOKEN_ENV,
    token_dir: Path | None = None,
) -> DoctorCheck:
    """Check optional Code Mower Cloud token setup without exposing secrets."""

    if os.environ.get(token_env):
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_PASS,
            message="Code Mower Cloud token env is set",
            detail={"token_env": token_env, "source": "env"},
        )

    directory = (token_dir or DEFAULT_CLOUD_TOKEN_DIR).expanduser()
    redacted_dir = "~/.config/code-mower/tokens"
    if not directory.exists():
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_SKIP,
            message="optional Code Mower Cloud token is not configured",
            detail={"token_env": token_env, "token_dir": redacted_dir},
            remediation=(
                "Cloud upload is optional. To enable it, create or receive a "
                "developer/team token, then run `code-mower cloud setup "
                "--token-stdin` and source the generated env file."
            ),
        )

    token_files: list[str] = []
    unreadable_files: list[str] = []
    insecure_files: list[str] = []
    for path in sorted(directory.glob("*.env")):
        has_token = _token_file_mentions_cloud_token(path, token_env)
        if has_token is None:
            unreadable_files.append(path.name)
            continue
        if not has_token:
            continue
        token_files.append(path.name)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            unreadable_files.append(path.name)
            continue
        if mode & 0o077:
            insecure_files.append(path.name)

    if unreadable_files:
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_WARN,
            message="could not inspect one or more Code Mower Cloud token files",
            detail={
                "token_env": token_env,
                "token_dir": redacted_dir,
                "unreadable_file_count": len(unreadable_files),
                "unreadable_files": unreadable_files,
            },
            remediation=(
                "Verify token files under ~/.config/code-mower/tokens are UTF-8 "
                "env files owned by the current user."
            ),
        )

    if not token_files:
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_SKIP,
            message="optional Code Mower Cloud token file was not found",
            detail={"token_env": token_env, "token_dir": redacted_dir},
            remediation=(
                "Cloud upload is optional. To enable it, run `code-mower cloud "
                "setup --token-stdin` and source the generated env file."
            ),
        )

    detail = {
        "token_env": token_env,
        "token_dir": redacted_dir,
        "token_file_count": len(token_files),
        "token_files": token_files,
    }
    if insecure_files:
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_WARN,
            message="Code Mower Cloud token file permissions are too broad",
            detail={**detail, "insecure_files": insecure_files},
            remediation="Run `chmod 600 ~/.config/code-mower/tokens/*.env`.",
        )

    return DoctorCheck(
        name="cloud.token",
        status=STATUS_PASS,
        message="Code Mower Cloud token file is configured",
        detail=detail,
    )


def _global_runtime_checks(
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    return [
        _check_python_runtime(),
        _check_pytest(),
        _check_github_auth_surface(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        ),
        _check_ripgrep(),
    ]


def _configured_repositories(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    repos: list[Mapping[str, Any]] = []
    for repo in _as_sequence(config.get("repositories", [])):
        if isinstance(repo, Mapping) and repo.get("slug"):
            repos.append(repo)
    return tuple(repos)


def _github_api_payload(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[Any | None, dict[str, Any]]:
    try:
        completed = subprocess.run(
            [gh_path, "api", endpoint],
            capture_output=True,
            text=True,
            check=False,
            timeout=http_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"error_type": exc.__class__.__name__}
    output = (completed.stdout or completed.stderr or "").strip()
    detail: dict[str, Any] = {
        "endpoint": endpoint,
        "returncode": completed.returncode,
        **_auth_probe_output_detail(output),
    }
    if completed.returncode != 0:
        return None, detail
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        detail["parse_error"] = "json"
        return None, detail
    return payload, detail


def _github_api_json(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    payload, detail = _github_api_payload(
        gh_path,
        endpoint,
        http_timeout=http_timeout,
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, Mapping):
        detail["parse_error"] = "not_object"
        return None, detail
    return payload, detail


def _github_api_list(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[list[Any] | None, dict[str, Any]]:
    payload, detail = _github_api_payload(
        gh_path,
        endpoint,
        http_timeout=http_timeout,
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, list):
        detail["parse_error"] = "not_list"
        return None, detail
    return payload, detail


def _selected_saas_or_hosted_lanes(
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[str]:
    selected: list[str] = []
    for lane_id, lane in lanes:
        if str(lane.get("driver", "")) in {"saas_event", "hosted_bridge"}:
            selected.append(lane_id)
    return selected


def _annotation_mentions_actions_billing_block(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in ACTIONS_BILLING_BLOCK_PATTERNS)


def _check_run_id_from_actions_job(job: Mapping[str, Any]) -> Any | None:
    check_run_url = str(job.get("check_run_url") or "")
    match = re.search(r"/check-runs/([0-9]+)$", check_run_url)
    if match:
        return match.group(1)
    return None


def _check_recent_actions_billing_blocks(
    *,
    gh_path: str,
    slug: str,
    http_timeout: int,
) -> DoctorCheck:
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page=10",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"could not inspect recent GitHub Actions runs for {slug}",
            detail={"repo": slug, **runs_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata, then rerun "
                "`code-mower doctor --github`."
            ),
        )

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"recent Actions response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    inspected_runs = 0
    inspected_jobs = 0
    incomplete_inspections: list[dict[str, Any]] = []
    for run in raw_runs:
        if (
            not isinstance(run, Mapping)
            or run.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
        ):
            continue
        if inspected_runs >= MAX_ACTIONS_FAILED_RUNS_TO_INSPECT:
            incomplete_inspections.append(
                {
                    "stage": "runs",
                    "reason": "inspection_limit_reached",
                    "limit": MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
                }
            )
            break
        run_id = run.get("id")
        if run_id is None:
            continue
        inspected_runs += 1
        jobs_payload, _jobs_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=20",
            http_timeout=http_timeout,
        )
        if jobs_payload is None:
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                }
            )
            continue
        raw_jobs = jobs_payload.get("jobs")
        if not isinstance(raw_jobs, list):
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                    "reason": "missing_jobs",
                }
            )
            continue
        if not raw_jobs:
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                    "reason": "no_jobs",
                }
            )
            continue
        for job in raw_jobs:
            if (
                not isinstance(job, Mapping)
                or job.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
            ):
                continue
            if inspected_jobs >= MAX_ACTIONS_FAILED_JOBS_TO_INSPECT:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "stage": "annotations",
                        "reason": "inspection_limit_reached",
                        "limit": MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
                    }
                )
                break
            job_id = job.get("id")
            if job_id is None:
                continue
            check_run_id = _check_run_id_from_actions_job(job)
            if check_run_id is None:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "job": str(job.get("name") or ""),
                        "stage": "annotations",
                        "reason": "missing_check_run_id",
                    }
                )
                continue
            inspected_jobs += 1
            annotations, _annotations_detail = _github_api_list(
                gh_path,
                f"repos/{slug}/check-runs/{check_run_id}/annotations?per_page=20",
                http_timeout=http_timeout,
            )
            if annotations is None:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "job_id": job_id,
                        "job": str(job.get("name") or ""),
                        "stage": "annotations",
                    }
                )
                continue
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    continue
                message = str(annotation.get("message") or "")
                if _annotation_mentions_actions_billing_block(message):
                    return DoctorCheck(
                        name="github.actions.recent_failures",
                        status=STATUS_WARN,
                        message=f"{slug} has recent Actions jobs blocked by billing or spending limits",
                        detail={
                            "repo": slug,
                            "billing_block_count": 1,
                            "billing_blocks": [
                                {
                                    "run_id": run_id,
                                    "workflow": str(run.get("name") or ""),
                                    "head_sha": str(run.get("head_sha") or ""),
                                    "job_id": job_id,
                                    "check_run_id": str(check_run_id),
                                    "job": str(job.get("name") or ""),
                                }
                            ],
                            "inspected_failed_runs": inspected_runs,
                            "inspected_failed_jobs": inspected_jobs,
                        },
                        remediation=(
                            "Fix GitHub billing or Actions spending limits, then rerun failed "
                            "workflows before relying on branch protection or deploy checks."
                        ),
                    )

    if incomplete_inspections:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"{slug} has recent failed Actions runs that doctor could not fully inspect",
            detail={
                "repo": slug,
                "incomplete_inspection_count": len(incomplete_inspections),
                "incomplete_inspections": incomplete_inspections[:5],
                "inspected_failed_runs": inspected_runs,
                "inspected_failed_jobs": inspected_jobs,
            },
            remediation=(
                "Verify gh auth can read workflow jobs and annotations, or inspect "
                "recent failed Actions runs manually before treating doctor as a "
                "green setup signal."
            ),
        )

    return DoctorCheck(
        name="github.actions.recent_failures",
        status=STATUS_PASS,
        message=f"{slug} has no recent Actions billing-block annotations",
        detail={
            "repo": slug,
            "inspected_failed_runs": inspected_runs,
            "inspected_failed_jobs": inspected_jobs,
        },
    )


def _parse_github_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _approx_run_seconds(run: Mapping[str, Any]) -> float:
    started = _parse_github_timestamp(
        run.get("run_started_at") or run.get("created_at")
    )
    updated = _parse_github_timestamp(run.get("updated_at"))
    if started is None or updated is None or updated < started:
        return 0.0
    return (updated - started).total_seconds()


def _is_metadata_workflow(name: str, path: str) -> bool:
    haystack = f"{name}\n{path}".lower()
    return any(marker in haystack for marker in ACTIONS_METADATA_WORKFLOW_MARKERS)


def _check_actions_cost_sample(
    *,
    gh_path: str,
    slug: str,
    private: bool,
    http_timeout: int,
    sample_limit: int,
) -> DoctorCheck:
    bounded_limit = max(1, min(sample_limit, ACTIONS_COST_SAMPLE_MAX))
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page={bounded_limit}",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"could not sample recent GitHub Actions usage for {slug}",
            detail={"repo": slug, **runs_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata. Private repos "
                "should periodically inspect Actions usage metrics after enabling "
                "hosted or informational lanes."
            ),
        )

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"Actions usage response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    workflow_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    workflow_seconds: defaultdict[str, float] = defaultdict(float)
    metadata_counts: Counter[str] = Counter()
    metadata_seconds: defaultdict[str, float] = defaultdict(float)
    schedule_runs = 0
    total_seconds = 0.0

    for run in raw_runs:
        if not isinstance(run, Mapping):
            continue
        workflow_name = str(run.get("name") or run.get("display_title") or "unknown")
        workflow_path = str(run.get("path") or "")
        event = str(run.get("event") or "unknown")
        seconds = _approx_run_seconds(run)
        workflow_counts[workflow_name] += 1
        event_counts[event] += 1
        workflow_seconds[workflow_name] += seconds
        total_seconds += seconds
        if event == "schedule":
            schedule_runs += 1
        if _is_metadata_workflow(workflow_name, workflow_path):
            metadata_counts[workflow_name] += 1
            metadata_seconds[workflow_name] += seconds

    sample_size = sum(workflow_counts.values())
    metadata_run_count = sum(metadata_counts.values())
    metadata_share = (metadata_run_count / sample_size) if sample_size else 0.0
    top_workflows = [
        {
            "workflow": workflow,
            "runs": count,
            "approx_minutes": round(workflow_seconds[workflow] / 60, 2),
        }
        for workflow, count in workflow_counts.most_common(10)
    ]
    top_metadata_workflows = [
        {
            "workflow": workflow,
            "runs": count,
            "approx_minutes": round(metadata_seconds[workflow] / 60, 2),
        }
        for workflow, count in metadata_counts.most_common(10)
    ]
    detail = {
        "repo": slug,
        "private": private,
        "sample_limit": bounded_limit,
        "sampled_runs": sample_size,
        "approx_total_minutes": round(total_seconds / 60, 2),
        "metadata_workflow_runs": metadata_run_count,
        "metadata_workflow_share": round(metadata_share, 3),
        "schedule_runs": schedule_runs,
        "top_workflows": top_workflows,
        "top_metadata_workflows": top_metadata_workflows,
        "events": [
            {"event": event, "runs": count}
            for event, count in event_counts.most_common(10)
        ],
    }
    if not sample_size:
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_PASS,
            message=f"{slug} has no recent Actions runs in the sampled window",
            detail=detail,
        )

    noisy_metadata = private and metadata_run_count >= max(5, int(sample_size * 0.2))
    scheduled_private = private and schedule_runs > 0
    if noisy_metadata or scheduled_private:
        reasons: list[str] = []
        if noisy_metadata:
            reasons.append(
                f"{metadata_run_count}/{sample_size} sampled runs look like metadata or reviewer labeler workflows"
            )
        if scheduled_private:
            reasons.append(f"{schedule_runs} sampled runs were scheduled")
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"{slug} private-repo Actions sample suggests avoidable spend: {'; '.join(reasons)}",
            detail=detail,
            remediation=(
                "Prefer label, trusted-comment, or workflow_dispatch triggers for "
                "optional hosted reviewers; add job-level if guards before checkout; "
                "keep informational lanes out of branch protection."
            ),
        )

    return DoctorCheck(
        name="github.actions.cost_sample",
        status=STATUS_PASS,
        message=f"{slug} recent Actions sample has no obvious private-repo cost traps",
        detail=detail,
    )


def _check_github_setup(
    *,
    config: Mapping[str, Any],
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
    http_timeout: int,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    gh_path = shutil.which("gh")
    if not gh_path:
        return [
            DoctorCheck(
                name="github.cli",
                status=STATUS_WARN,
                message="GitHub setup checks require the gh CLI",
                detail={"gh_path": ""},
                remediation=(
                    "Install GitHub CLI, run `gh auth login`, then rerun "
                    "`code-mower doctor --github`."
                ),
            )
        ]

    checks.append(
        DoctorCheck(
            name="github.cli",
            status=STATUS_PASS,
            message="gh found for GitHub setup checks",
            detail={"gh_path": gh_path},
        )
    )

    repos = _configured_repositories(config)
    if not repos:
        checks.append(
            DoctorCheck(
                name="github.repositories",
                status=STATUS_WARN,
                message="config declares no repositories for GitHub setup checks",
                remediation=(
                    "Add repositories[].slug entries to code-mower.yml before "
                    "running GitHub-backed audits."
                ),
            )
        )
        return checks

    selected_saas_or_hosted = _selected_saas_or_hosted_lanes(lanes)
    private_repos: list[str] = []
    unknown_visibility_repos: list[str] = []
    for repo in repos:
        slug = str(repo.get("slug") or "")
        configured_default_branch = str(repo.get("default_branch") or "main")
        repo_payload, repo_detail = _github_api_json(
            gh_path,
            f"repos/{slug}",
            http_timeout=http_timeout,
        )
        if repo_payload is None:
            unknown_visibility_repos.append(slug)
            checks.append(
                DoctorCheck(
                    name="github.repo.metadata",
                    status=STATUS_WARN,
                    message=f"could not read GitHub repository metadata for {slug}",
                    detail={"repo": slug, **repo_detail},
                    remediation=(
                        "Verify gh auth can read this repo. Private repos need "
                        "a token or GitHub App installation with repository access."
                    ),
                )
            )
            continue

        is_private = bool(repo_payload.get("private"))
        default_branch = str(
            repo_payload.get("default_branch") or configured_default_branch or "main"
        )
        if is_private:
            private_repos.append(slug)
        checks.append(
            DoctorCheck(
                name="github.repo.metadata",
                status=STATUS_PASS,
                message=(
                    f"{slug} is reachable "
                    f"({'private' if is_private else 'public'} repository)"
                ),
                detail={
                    "repo": slug,
                    "private": is_private,
                    "visibility": str(repo_payload.get("visibility") or ""),
                    "default_branch": str(repo_payload.get("default_branch") or ""),
                    "archived": bool(repo_payload.get("archived")),
                    "fork": bool(repo_payload.get("fork")),
                },
            )
        )

        permissions = repo_payload.get("permissions")
        if isinstance(permissions, Mapping):
            write_like = any(
                bool(permissions.get(name))
                for name in ("admin", "maintain", "push", "triage")
            )
            checks.append(
                DoctorCheck(
                    name="github.repo.permissions",
                    status=STATUS_PASS if write_like else STATUS_WARN,
                    message=(
                        f"{slug} token has repository write-adjacent permission"
                        if write_like
                        else f"{slug} token appears read-only for repository metadata"
                    ),
                    detail={
                        "repo": slug,
                        "admin": bool(permissions.get("admin")),
                        "maintain": bool(permissions.get("maintain")),
                        "push": bool(permissions.get("push")),
                        "triage": bool(permissions.get("triage")),
                        "pull": bool(permissions.get("pull")),
                    },
                    remediation=(
                        None
                        if write_like
                        else (
                            "Configure a fine-grained PAT or GitHub App token with "
                            "Issues read/write and Pull requests read before expecting "
                            "Code Mower to apply labels or comments."
                        )
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="github.repo.permissions",
                    status=STATUS_WARN,
                    message=f"{slug} metadata did not include token permissions",
                    detail={"repo": slug},
                    remediation=(
                        "If label writes fail, configure the lane token secrets "
                        "documented by the provider matrix."
                    ),
                )
            )

        actions_payload, actions_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/permissions",
            http_timeout=http_timeout,
        )
        if actions_payload is None:
            checks.append(
                DoctorCheck(
                    name="github.actions.permissions",
                    status=STATUS_WARN,
                    message=f"could not inspect GitHub Actions permissions for {slug}",
                    detail={"repo": slug, **actions_detail},
                    remediation=(
                        "A repo admin should verify Actions are enabled and workflow "
                        "token permissions can write issues/labels or that PAT "
                        "fallback secrets are configured."
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="github.actions.permissions",
                    status=(
                        STATUS_PASS
                        if bool(actions_payload.get("enabled"))
                        else STATUS_WARN
                    ),
                    message=(
                        f"{slug} GitHub Actions are enabled and inspectable"
                        if bool(actions_payload.get("enabled"))
                        else f"{slug} GitHub Actions appear disabled"
                    ),
                    detail={
                        "repo": slug,
                        "enabled": bool(actions_payload.get("enabled")),
                        "allowed_actions": str(actions_payload.get("allowed_actions") or ""),
                    },
                    remediation=(
                        None
                        if bool(actions_payload.get("enabled"))
                        else (
                            "Enable GitHub Actions for this repository before "
                            "expecting Code Mower labelers or audit workflows to run."
                        )
                    ),
                )
            )

        checks.append(
            _check_recent_actions_billing_blocks(
                gh_path=gh_path,
                slug=slug,
                http_timeout=http_timeout,
            )
        )
        checks.append(
            _check_actions_cost_sample(
                gh_path=gh_path,
                slug=slug,
                private=is_private,
                http_timeout=http_timeout,
                sample_limit=actions_cost_sample,
            )
        )

        encoded_branch = urllib.parse.quote(default_branch, safe="")
        protection_payload, protection_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/branches/{encoded_branch}/protection",
            http_timeout=http_timeout,
        )
        if protection_payload is None:
            checks.append(
                DoctorCheck(
                    name="github.branch_protection",
                    status=STATUS_WARN,
                    message=f"could not confirm branch protection for {slug}@{default_branch}",
                    detail={
                        "repo": slug,
                        "default_branch": default_branch,
                        **protection_detail,
                    },
                    remediation=(
                        "Before enabling autonomous merge, protect the default branch "
                        "and make required checks explicit."
                    ),
                )
            )
        else:
            required_checks = protection_payload.get("required_status_checks")
            contexts: list[str] = []
            if isinstance(required_checks, Mapping):
                raw_contexts = required_checks.get("contexts")
                if isinstance(raw_contexts, list):
                    contexts = [str(item) for item in raw_contexts]
            checks.append(
                DoctorCheck(
                    name="github.branch_protection",
                    status=STATUS_PASS,
                    message=f"{slug}@{default_branch} branch protection is inspectable",
                    detail={
                        "repo": slug,
                        "default_branch": default_branch,
                        "required_status_check_count": len(contexts),
                    },
                )
            )

    if private_repos and selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_WARN,
                message=(
                    "private repos selected with SaaS/hosted lanes: "
                    + ", ".join(selected_saas_or_hosted)
                ),
                detail={
                    "private_repo_count": len(private_repos),
                    "lanes": selected_saas_or_hosted,
                },
                remediation=(
                    "Install each provider's GitHub App for the selected private "
                    "repositories, confirm plan support, and decide whether sending "
                    "diffs/source to that provider is acceptable."
                ),
            )
        )
    elif unknown_visibility_repos and selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_WARN,
                message=(
                    "could not determine repository visibility for SaaS/hosted lanes: "
                    + ", ".join(selected_saas_or_hosted)
                ),
                detail={
                    "unknown_repo_count": len(unknown_visibility_repos),
                    "lanes": selected_saas_or_hosted,
                },
                remediation=(
                    "Verify gh auth can read repository metadata before deciding "
                    "whether hosted provider apps need private-repo access."
                ),
            )
        )
    elif selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_PASS,
                message="selected SaaS/hosted lanes have no private repos in config",
                detail={"lanes": selected_saas_or_hosted},
            )
        )
    return checks


def _merge_mapping_defaults(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def _effective_lane(
    lane_id: str,
    lane: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
) -> Mapping[str, Any]:
    template = provider_templates.get(lane_id, {})
    if not isinstance(template, Mapping):
        template = {}
    merged = _merge_mapping_defaults(template, lane)
    for key in ("labels", "provider_config", "review_hygiene"):
        template_value = template.get(key, {})
        lane_value = lane.get(key, {})
        if isinstance(template_value, Mapping) and isinstance(lane_value, Mapping):
            merged[key] = _merge_mapping_defaults(template_value, lane_value)
    return merged


def _check_token_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
    token_env = list(_as_sequence(lane.get("token_env", [])))
    token_env_any = [
        [str(item) for item in _as_sequence(group)]
        for group in _as_sequence(lane.get("token_env_any", []))
    ]
    review_hygiene = lane.get("review_hygiene", {})
    if not token_env and isinstance(review_hygiene, Mapping) and review_hygiene.get("token_env"):
        token_env = [review_hygiene["token_env"]]
    checks: list[DoctorCheck] = []
    if not token_env and not token_env_any:
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_SKIP,
                lane=lane_id,
                message="lane declares no token env vars",
            )
        )
        return checks

    def token_file_value(name: str, path_text: str) -> str:
        if name not in SUPPORTED_TOKEN_FILE_ENV_NAMES:
            return ""
        try:
            result = code_mower_secrets.read_secret_file(
                Path(path_text),
                supported_env_names=SUPPORTED_TOKEN_FILE_ENV_NAMES,
            )
        except OSError:
            return ""
        return result.value

    def token_is_present(name: str) -> bool:
        if os.environ.get(name):
            return True
        path_text = os.environ.get(f"{name}_FILE", "").strip()
        if not path_text:
            return False
        return bool(token_file_value(name, path_text))

    token_file_env = [
        f"{name}_FILE"
        for name in [str(item) for item in token_env]
        if name in SUPPORTED_TOKEN_FILE_ENV_NAMES and os.environ.get(f"{name}_FILE")
    ]
    token_file_env.extend(
        f"{name}_FILE"
        for group in token_env_any
        for name in group
        if name in SUPPORTED_TOKEN_FILE_ENV_NAMES and os.environ.get(f"{name}_FILE")
    )
    missing = [str(name) for name in token_env if not token_is_present(str(name))]
    missing_any = [
        group
        for group in token_env_any
        if group and not any(token_is_present(name) for name in group)
    ]
    if missing or missing_any:
        messages = []
        if missing:
            messages.append(f"missing token env vars: {', '.join(missing)}")
        for group in missing_any:
            messages.append(f"set one of: {', '.join(group)}")
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_WARN,
                lane=lane_id,
                message="; ".join(messages),
                detail={
                    "missing": missing,
                    "missing_any": missing_any,
                    "token_file_env": sorted(set(token_file_env)),
                },
                remediation=_token_remediation(missing, missing_any),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_PASS,
                lane=lane_id,
                message="token env vars are set",
                detail={
                    "token_env": [str(name) for name in token_env],
                    "token_env_any": token_env_any,
                    "token_file_env": sorted(set(token_file_env)),
                },
            )
        )
    return checks


def _check_required_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return []
    required = [
        str(name)
        for name in _as_sequence(provider_config.get("required_env", []))
        if str(name).strip()
    ]
    required_truthy = [
        str(name)
        for name in _as_sequence(provider_config.get("required_env_truthy", []))
        if str(name).strip()
    ]
    if not required and not required_truthy:
        return []
    missing = [name for name in required if not os.environ.get(name)]
    missing_truthy = [
        name
        for name in required_truthy
        if os.environ.get(name, "").strip().lower() not in TRUTHY_ENV_VALUES
    ]
    if missing or missing_truthy:
        return [
            DoctorCheck(
                name="env.required",
                status=STATUS_WARN,
                lane=lane_id,
                message=(
                    "missing required env vars: "
                    + ", ".join([*missing, *missing_truthy])
                ),
                detail={
                    "missing": missing,
                    "missing_truthy": missing_truthy,
                    "required_env": required,
                    "required_env_truthy": required_truthy,
                },
                remediation=(
                    "Set the required env vars only when you accept the lane's "
                    "documented runtime trust model."
                ),
            )
        ]
    return [
        DoctorCheck(
            name="env.required",
            status=STATUS_PASS,
            lane=lane_id,
            message="required env vars are set",
            detail={
                "required_env": required,
                "required_env_truthy": required_truthy,
            },
        )
    ]


def _candidate_local_cli_commands(lane: Mapping[str, Any]) -> list[str]:
    provider_config = lane.get("provider_config", {})
    commands: list[str] = []
    if isinstance(provider_config, Mapping):
        command_env = str(provider_config.get("command_env", ""))
        if command_env and os.environ.get(command_env):
            commands.append(str(os.environ[command_env]))
        if provider_config.get("command"):
            commands.append(str(provider_config["command"]))
        for command in _as_sequence(provider_config.get("alternate_commands", [])):
            if command:
                commands.append(str(command))
    if not commands:
        provider = str(lane.get("provider", ""))
        commands.append(provider.replace("_", "-"))
    deduped: list[str] = []
    for command in commands:
        if command and command not in deduped:
            deduped.append(command)
    return deduped


def _local_cli_command(lane: Mapping[str, Any]) -> str:
    candidates = _candidate_local_cli_commands(lane)
    if candidates:
        return candidates[0]
    provider = str(lane.get("provider", "")).replace("_", "-")
    return provider or "unknown"


def _check_local_cli(lane_id: str, lane: Mapping[str, Any]) -> DoctorCheck:
    provider_config = lane.get("provider_config", {})
    commands = _candidate_local_cli_commands(lane)
    detail: dict[str, Any] = {"commands": commands}
    if isinstance(provider_config, Mapping) and provider_config.get("command"):
        detail["command"] = str(provider_config["command"])
    if isinstance(provider_config, Mapping) and provider_config.get("command_env"):
        detail["command_env"] = str(provider_config["command_env"])
    if isinstance(provider_config, Mapping) and provider_config.get("protocol"):
        detail["protocol"] = str(provider_config["protocol"])
    for command in commands:
        resolved = shutil.which(command)
        if resolved:
            detail.update({"command": command, "path": resolved})
            return DoctorCheck(
                name="runtime.local_cli",
                status=STATUS_PASS,
                lane=lane_id,
                message=f"{command} found",
                detail=detail,
            )
    return DoctorCheck(
        name="runtime.local_cli",
        status=STATUS_WARN,
        lane=lane_id,
        message=f"none of the candidate commands were found: {', '.join(commands)}",
        detail=detail,
        remediation=_local_cli_remediation(
            commands,
            str(detail.get("command_env", "")),
        ),
    )


def _resolved_local_cli_command(lane: Mapping[str, Any]) -> tuple[str, str] | None:
    for command in _candidate_local_cli_commands(lane):
        resolved = shutil.which(command)
        if resolved:
            return command, resolved
    return None


def _local_cli_probe_args(lane: Mapping[str, Any], command: str) -> tuple[str, ...]:
    provider_config = lane.get("provider_config", {})
    if isinstance(provider_config, Mapping):
        raw_probe = provider_config.get("doctor_probe_args")
        if isinstance(raw_probe, (list, tuple)) and raw_probe:
            return tuple(str(part) for part in raw_probe)
    provider = str(lane.get("provider") or "")
    if provider == "gemini":
        return ("--version",)
    if provider == "antigravity":
        return ("--version",)
    if provider == "coderabbit":
        return ("--version",)
    if provider == "claude":
        return ("--version",)
    if provider == "codex":
        return ("--version",)
    return ("--help",)


def _local_cli_probe_timeout(lane: Mapping[str, Any], default_timeout: int) -> int:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return default_timeout
    raw_timeout = provider_config.get("doctor_probe_timeout_seconds")
    if raw_timeout is None:
        return default_timeout
    try:
        return max(1, int(raw_timeout))
    except (TypeError, ValueError):
        return default_timeout


def _parse_probe_json(output: str) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    text = output.strip()
    if not text:
        return None, {"json_parsed": False, "json_error": "empty_output"}

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            continue
        if isinstance(payload, Mapping):
            return payload, {
                "json_parsed": True,
                "json_extracted": candidate != text,
            }
        return None, {"json_parsed": False, "json_error": "not_object"}
    return None, {"json_parsed": False, "json_error": last_error}


def _json_field(payload: Mapping[str, Any], dotted_field: str) -> Any:
    value: Any = payload
    for part in dotted_field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _probe_error_value_is_clean(value: Any) -> bool:
    return value is None or value is False or value == "" or value == 0


def _local_cli_probe_env(lane: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    child_env = os.environ.copy()
    forwarded: list[str] = []

    token_names: list[str] = []
    for item in _as_sequence(lane.get("token_env", [])):
        token_names.append(str(item))
    for group in _as_sequence(lane.get("token_env_any", [])):
        for item in _as_sequence(group):
            token_names.append(str(item))

    for name in token_names:
        if name not in SUPPORTED_TOKEN_FILE_ENV_NAMES or child_env.get(name):
            continue
        path_text = child_env.get(f"{name}_FILE", "").strip()
        if not path_text:
            continue
        try:
            result = code_mower_secrets.read_secret_file(
                Path(path_text),
                supported_env_names=SUPPORTED_TOKEN_FILE_ENV_NAMES,
            )
        except OSError:
            continue
        if not result.ok:
            continue
        child_env[name] = result.value
        forwarded.append(f"{name}_FILE")

    return child_env, {"token_file_env_forwarded": sorted(set(forwarded))}


def _evaluate_json_probe(
    provider_config: Mapping[str, Any],
    output: str,
    *,
    returncode: int,
) -> tuple[str, str, dict[str, Any]]:
    payload, parse_detail = _parse_probe_json(output)
    detail: dict[str, Any] = dict(parse_detail)
    if payload is None:
        if returncode != 0:
            return (
                STATUS_WARN,
                f"probe exited {returncode} without parseable JSON",
                detail,
            )
        return (
            STATUS_WARN,
            "probe output was not parseable JSON",
            detail,
        )

    error_fields = [
        str(field)
        for field in _as_sequence(provider_config.get("doctor_probe_error_fields", []))
        if str(field).strip()
    ]
    dirty_errors = []
    for field in error_fields:
        value = _json_field(payload, field)
        if not _probe_error_value_is_clean(value):
            dirty_errors.append(field)
    detail["error_fields"] = error_fields
    detail["dirty_error_fields"] = dirty_errors

    field = str(provider_config.get("doctor_probe_expect_json_field", "")).strip()
    expected = provider_config.get("doctor_probe_expect_json_value")
    response_matches = True
    if field:
        value = _json_field(payload, field)
        response_matches = (
            expected is None
            or str(value).strip() == str(expected).strip()
        )
        detail.update(
            {
                "response_field": field,
                "response_present": value is not None,
                "response_length": len(str(value)) if value is not None else 0,
                "response_matches_expected": response_matches,
            }
        )

    if returncode != 0:
        return STATUS_WARN, f"probe exited {returncode}", detail
    if dirty_errors:
        return (
            STATUS_WARN,
            "probe reported provider/API error fields",
            detail,
        )
    if not response_matches:
        return (
            STATUS_WARN,
            "probe response did not match expected sentinel",
            detail,
        )
    return STATUS_PASS, "auth smoke probe succeeded", detail


def _check_local_cli_probe(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> DoctorCheck:
    if not probe_runtime:
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_SKIP,
            lane=lane_id,
            message="local CLI probing skipped; pass --probe-runtime to run a harmless version/help command",
        )
    resolved_pair = _resolved_local_cli_command(lane)
    if resolved_pair is None:
        command = _local_cli_command(lane)
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane=lane_id,
            message=f"{command} was not found, so runtime probe could not run",
            detail={"command": command},
            remediation=_local_cli_remediation([command]),
        )
    command, resolved = resolved_pair
    probe_args = _local_cli_probe_args(lane, command)
    timeout_seconds = _local_cli_probe_timeout(lane, http_timeout)
    child_env, env_detail = _local_cli_probe_env(lane)
    try:
        completed = subprocess.run(
            [resolved, *probe_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane=lane_id,
            message=f"{command} probe failed: {exc}",
            detail={
                "command": command,
                "path": resolved,
                "args": list(probe_args),
                "timeout_seconds": timeout_seconds,
                **env_detail,
            },
            remediation=(
                f"Run `{command} {' '.join(probe_args)}` manually, fix CLI "
                "installation or auth, then rerun doctor --probe-runtime."
            ),
        )
    output = (completed.stdout or completed.stderr or "").strip()
    provider_config = lane.get("provider_config", {})
    json_detail: dict[str, Any] = {}
    json_message = ""
    if isinstance(provider_config, Mapping) and provider_config.get("doctor_probe_expect_json"):
        status, json_message, json_detail = _evaluate_json_probe(
            provider_config,
            output,
            returncode=completed.returncode,
        )
    else:
        status = STATUS_PASS if completed.returncode == 0 else STATUS_WARN
    return DoctorCheck(
        name="runtime.local_cli.probe",
        status=status,
        lane=lane_id,
        message=(
            f"{command} {json_message}"
            if json_message
            else (
                f"{command} probe succeeded"
                if status == STATUS_PASS
                else f"{command} probe exited {completed.returncode}"
            )
        ),
        detail={
            "command": command,
            "path": resolved,
            "args": list(probe_args),
            "timeout_seconds": timeout_seconds,
            "returncode": completed.returncode,
            **env_detail,
            **json_detail,
            **_auth_probe_output_detail(output),
        },
        remediation=(
            None
            if status == STATUS_PASS
            else (
                f"Run `{command} {' '.join(probe_args)}` manually, fix CLI "
                "installation or auth, then rerun doctor --probe-runtime."
            )
        ),
    )


def _profile_from_config(profile_id: str, raw: Mapping[str, Any]) -> local_llm_profiles.LocalLlmProfile:
    canonical = local_llm_profiles.LOCAL_LLM_PROFILES.get(profile_id)
    def profile_int(field: str, default: int) -> int:
        value = raw.get(field, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"API model profile {profile_id!r} field {field!r} must be an integer"
            ) from exc

    return local_llm_profiles.LocalLlmProfile(
        profile_id=profile_id,
        description=str(
            raw.get(
                "description",
                canonical.description if canonical else f"{profile_id} local LLM profile",
            )
        ),
        api_base=str(raw.get("api_base", canonical.api_base if canonical else "")),
        model=str(raw.get("model", canonical.model if canonical else "")),
        endpoint=str(raw.get("endpoint", canonical.endpoint if canonical else "")),
        api_key=str(raw.get("api_key", canonical.api_key if canonical else "EMPTY")),
        context_window=profile_int(
            "context_window", canonical.context_window if canonical else 128_000
        ),
        max_files=profile_int("max_files", canonical.max_files if canonical else 25),
        max_file_bytes=profile_int(
            "max_file_bytes", canonical.max_file_bytes if canonical else 60_000
        ),
        http_timeout=profile_int("http_timeout", canonical.http_timeout if canonical else 900),
        informational=bool(
            raw.get("informational", canonical.informational if canonical else True)
        ),
    )


def _local_llm_lane_profiles(lane: Mapping[str, Any]) -> tuple[local_llm_profiles.LocalLlmProfile, ...]:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return ()
    raw_profiles = provider_config.get("profiles", {})
    profile_env = str(provider_config.get("profile_env", ""))
    selected_profile_id = os.environ.get(profile_env) if profile_env else None
    if selected_profile_id:
        if isinstance(raw_profiles, Mapping):
            raw_profile = raw_profiles.get(selected_profile_id)
            if isinstance(raw_profile, Mapping):
                return (_profile_from_config(selected_profile_id, raw_profile),)
        try:
            return (local_llm_profiles.get_profile(selected_profile_id),)
        except KeyError:
            return ()
    if isinstance(raw_profiles, Mapping):
        return tuple(
            _profile_from_config(str(profile_id), profile)
            for profile_id, profile in sorted(raw_profiles.items())
            if isinstance(profile, Mapping)
        )
    if isinstance(raw_profiles, (list, tuple)):
        profiles: list[local_llm_profiles.LocalLlmProfile] = []
        for profile_id in raw_profiles:
            try:
                profiles.append(local_llm_profiles.get_profile(str(profile_id)))
            except KeyError:
                continue
        return tuple(profiles)
    return ()


def fetch_openai_compatible_models(
    api_base: str,
    api_key: str,
    timeout: int,
) -> list[str]:
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", []) if isinstance(payload, Mapping) else []
    return [
        str(entry.get("id"))
        for entry in data
        if isinstance(entry, Mapping) and entry.get("id")
    ]


def _api_model_key(lane: Mapping[str, Any], profile: local_llm_profiles.LocalLlmProfile) -> str:
    provider_config = lane.get("provider_config", {})
    api_key_env = ""
    if isinstance(provider_config, Mapping):
        api_key_env = str(provider_config.get("api_key_env", ""))
    token_env = lane.get("token_env", [])
    if not api_key_env and isinstance(token_env, list):
        for item in token_env:
            name = str(item)
            if name.endswith("_API_KEY"):
                api_key_env = name
                break
    if api_key_env:
        value = os.environ.get(api_key_env)
        if value:
            return value
    return profile.api_key


def _provider_env_override(
    provider_config: Mapping[str, Any],
    env_key: str,
    fallback: str,
) -> str:
    env_name = str(provider_config.get(env_key, ""))
    if not env_name:
        return fallback
    return os.environ.get(env_name) or fallback


def _runtime_profile(
    lane: Mapping[str, Any],
    profile: local_llm_profiles.LocalLlmProfile,
) -> local_llm_profiles.LocalLlmProfile:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return profile
    return local_llm_profiles.LocalLlmProfile(
        profile_id=profile.profile_id,
        description=profile.description,
        api_base=_provider_env_override(provider_config, "api_base_env", profile.api_base),
        model=_provider_env_override(provider_config, "model_env", profile.model),
        endpoint=profile.endpoint,
        api_key=profile.api_key,
        context_window=profile.context_window,
        max_files=profile.max_files,
        max_file_bytes=profile.max_file_bytes,
        http_timeout=profile.http_timeout,
        informational=profile.informational,
    )


def _check_api_model(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    try:
        profiles = _local_llm_lane_profiles(lane)
    except (TypeError, ValueError) as exc:
        checks.append(
            DoctorCheck(
                name="runtime.api_model.profiles",
                status=STATUS_FAIL,
                lane=lane_id,
                message=str(exc),
                remediation=(
                    "Fix provider_config.profiles in code-mower.yml or use a "
                    "known packaged local LLM profile id."
                ),
            )
        )
        return checks
    if not profiles:
        checks.append(
            DoctorCheck(
                name="runtime.api_model.profiles",
                status=STATUS_WARN,
                lane=lane_id,
                message="no API model profiles configured",
                remediation=(
                    "Add provider_config.profiles to this lane or select a "
                    "packaged local LLM profile."
                ),
            )
        )
        return checks

    checks.append(
        DoctorCheck(
            name="runtime.api_model.profiles",
            status=STATUS_PASS,
            lane=lane_id,
            message=f"{len(profiles)} API model profile(s) configured",
            detail={"profiles": [profile.profile_id for profile in profiles]},
        )
    )
    if not probe_runtime:
        checks.append(
            DoctorCheck(
                name="runtime.api_model.probe",
                status=STATUS_SKIP,
                lane=lane_id,
                message="runtime probing skipped; pass --probe-runtime to query model endpoints",
            )
        )
        return checks

    for profile in profiles:
        runtime_profile = _runtime_profile(lane, profile)
        try:
            models = fetch_openai_compatible_models(
                runtime_profile.api_base,
                _api_model_key(lane, profile),
                http_timeout,
            )
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            checks.append(
                DoctorCheck(
                    name="runtime.api_model.probe",
                    status=STATUS_WARN,
                    lane=lane_id,
                    message=f"{runtime_profile.profile_id} probe failed: {exc}",
                    detail={
                        "profile": runtime_profile.profile_id,
                        "api_base": runtime_profile.api_base,
                    },
                    remediation=(
                        "Start the local OpenAI-compatible endpoint, fix api_base "
                        "or api key overrides, then rerun doctor --probe-runtime."
                    ),
                )
            )
            continue
        expected = runtime_profile.model
        status = STATUS_PASS if expected in models else STATUS_WARN
        message = (
            f"{runtime_profile.profile_id} reports expected model {expected}"
            if status == STATUS_PASS
            else f"{runtime_profile.profile_id} did not report expected model {expected}"
        )
        checks.append(
            DoctorCheck(
                name="runtime.api_model.probe",
                status=status,
                lane=lane_id,
                message=message,
                detail={
                    "profile": runtime_profile.profile_id,
                    "api_base": runtime_profile.api_base,
                    "expected_model": expected,
                    "models": models,
                },
                remediation=(
                    None
                    if status == STATUS_PASS
                    else (
                        "Update the configured local LLM model or start an endpoint "
                        "that serves the expected model."
                    )
                ),
            )
        )
    return checks


def _check_lane_runtime(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    checks = _check_token_env(lane_id, lane)
    checks.extend(_check_required_env(lane_id, lane))
    driver = str(lane.get("driver", ""))
    if driver == "local_cli":
        checks.append(_check_local_cli(lane_id, lane))
        checks.append(
            _check_local_cli_probe(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    elif driver == "api_model":
        checks.extend(
            _check_api_model(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    elif driver in {"manual", "hosted_bridge", "saas_event"}:
        checks.append(
            DoctorCheck(
                name="runtime.probe",
                status=STATUS_SKIP,
                lane=lane_id,
                message=f"{driver} lanes do not have a local runtime probe yet",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="runtime.probe",
                status=STATUS_WARN,
                lane=lane_id,
                message=f"unknown driver {driver!r}; no runtime probe available",
                remediation=(
                    "Use a supported driver: local_cli, api_model, hosted_bridge, "
                    "saas_event, or manual."
                ),
            )
        )
    return checks


def run_doctor(
    *,
    config_path: Path,
    provider_templates_path: Path,
    profile: str | None,
    probe_runtime: bool = False,
    github: bool = False,
    cloud: bool = False,
    http_timeout: int = 5,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
) -> DoctorReport:
    config, templates, checks = _load_inputs(config_path, provider_templates_path)
    if config is None or templates is None:
        return DoctorReport(
            config_path=str(config_path),
            provider_templates_path=str(provider_templates_path),
            profile=profile,
            checks=tuple(checks),
        )

    try:
        lanes = _selected_lanes(config, profile)
    except code_mower_config.ConfigError as exc:
        checks.append(
            DoctorCheck(
                name="profile.select",
                status=STATUS_FAIL,
                message=str(exc),
                remediation=(
                    "Choose an existing profile from code-mower.yml or run "
                    "`code-mower init --easy` to inspect the recommended profile."
                ),
            )
        )
        return DoctorReport(
            config_path=str(config_path),
            provider_templates_path=str(provider_templates_path),
            profile=profile,
            checks=tuple(checks),
        )

    checks.append(
        DoctorCheck(
            name="profile.select",
            status=STATUS_PASS,
            message=(
                f"selected profile {profile}: {', '.join(lanes)}"
                if profile
                else f"selected all lanes: {', '.join(lanes)}"
            ),
            detail={"lanes": list(lanes)},
        )
    )
    checks.append(_provider_template_coverage(lanes, templates))
    checks.extend(
        _global_runtime_checks(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        )
    )

    lane_configs = _as_mapping(config.get("lanes"), "lanes")
    provider_templates = _as_mapping(
        templates.get("provider_templates"),
        "provider_templates",
    )
    effective_lanes: list[tuple[str, Mapping[str, Any]]] = []
    for lane_id in lanes:
        lane = lane_configs.get(lane_id)
        if not isinstance(lane, Mapping):
            checks.append(
                DoctorCheck(
                    name="lane.load",
                    status=STATUS_FAIL,
                    lane=lane_id,
                    message="selected lane is missing from config",
                    remediation=(
                        "Add the lane to code-mower.yml or remove it from the "
                        "selected profile."
                    ),
                )
            )
            continue
        lane = _effective_lane(lane_id, lane, provider_templates)
        effective_lanes.append((lane_id, lane))
        checks.extend(
            _check_lane_runtime(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )

    if github:
        checks.extend(
            _check_github_setup(
                config=config,
                lanes=effective_lanes,
                http_timeout=http_timeout,
                actions_cost_sample=actions_cost_sample,
            )
        )

    if cloud:
        checks.append(_check_cloud_token_surface())

    return DoctorReport(
        config_path=str(config_path),
        provider_templates_path=str(provider_templates_path),
        profile=profile,
        checks=tuple(checks),
    )


def render_doctor_text(report: DoctorReport) -> str:
    lines = [
        "Code Mower doctor",
        f"Status: {report.status}",
        f"Config: {report.config_path}",
        f"Provider templates: {report.provider_templates_path}",
    ]
    if report.profile:
        lines.append(f"Profile: {report.profile}")
    lines.extend(
        [
            f"Checks: {len(report.checks)} ({report.failures} failed, {report.warnings} warnings)",
            "",
        ]
    )
    for check in report.checks:
        lane = f" [{check.lane}]" if check.lane else ""
        lines.append(f"- {check.status.upper()} {check.name}{lane}: {check.message}")
        if check.remediation:
            lines.append(f"  remediation: {check.remediation}")
    return "\n".join(lines) + "\n"


def resolve_doctor_provider_templates_path(path_text: str) -> Path:
    path = Path(path_text)
    if path_text == code_mower_package.DEFAULT_PROVIDER_TEMPLATES and not path.is_absolute():
        project_catalog = Path.cwd() / code_mower_package.DEFAULT_PROVIDER_TEMPLATES
        if project_catalog.exists():
            return project_catalog
    return code_mower_package.resolve_provider_templates_path(path_text)


def _apply_first_run_defaults(args: argparse.Namespace) -> None:
    if not (getattr(args, "v05", False) or getattr(args, "preflight", False)):
        return
    args.easy = True
    if args.profile is None:
        args.profile = "recommended"
    args.probe_runtime = True
    args.github = True
    args.cloud = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="code-mower.yml")
    parser.add_argument(
        "--provider-templates",
        default=code_mower_package.DEFAULT_PROVIDER_TEMPLATES,
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--easy",
        action="store_true",
        help=(
            "first-run alias for --profile recommended; if code-mower.yml is "
            "absent, use the packaged example config"
        ),
    )
    parser.add_argument(
        "--v05",
        action="store_true",
        help=(
            "v0.5 early-adopter preset: --easy --profile recommended "
            "--probe-runtime --github --cloud"
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "friendly alias for the v0.5 first-run preset: --easy "
            "--profile recommended --probe-runtime --github --cloud"
        ),
    )
    parser.add_argument("--probe-runtime", action="store_true")
    parser.add_argument(
        "--github",
        action="store_true",
        help="inspect GitHub repo visibility, branch protection, and provider setup hints",
    )
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="check optional Code Mower Cloud token setup without reading or printing token values",
    )
    parser.add_argument("--http-timeout", type=int, default=5)
    parser.add_argument(
        "--actions-cost-sample",
        type=int,
        default=ACTIONS_COST_SAMPLE_DEFAULT,
        help=(
            "number of recent Actions runs to sample for private-repo cost "
            f"diagnostics, capped at {ACTIONS_COST_SAMPLE_MAX}"
        ),
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    _apply_first_run_defaults(args)
    if args.easy and args.profile is None:
        args.profile = "recommended"

    try:
        provider_templates_path = resolve_doctor_provider_templates_path(args.provider_templates)
        report = run_doctor(
            config_path=resolve_doctor_config_path(args.config, easy=args.easy),
            provider_templates_path=provider_templates_path,
            profile=args.profile,
            probe_runtime=args.probe_runtime,
            github=args.github,
            cloud=args.cloud,
            http_timeout=args.http_timeout,
            actions_cost_sample=args.actions_cost_sample,
        )
    except code_mower_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(render_doctor_text(report), end="")
    if report.failures:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
