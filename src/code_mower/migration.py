#!/usr/bin/env python3
"""Rehearse migration from product-local Code Mower tools to the package."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import __version__ as CODE_MOWER_VERSION
    from code_mower.migration_mirror import (
        PRODUCT_SUPPORT_PATTERNS,
        RUNNER_ALIASES,
        _default_local_command,
        _line_requires_workflow_file,
        _relative_existing_files,
        _workflow_file_references,
        _workflow_local_fallback_references,
        render_mirror_removal_plan,
        render_mirror_removal_text,
        render_runner_aliases,
        render_runner_aliases_text,
    )
    from code_mower.migration_rehearsal import (
        FIRST_USER_ARTIFACTS,
        MIRRORED_IMPLEMENTATION_PATTERNS,
        PRIVACY_EXCLUDED_CONTENT,
        CommandResult,
        RehearsalError,
        RunOutput,
        _default_product_rehearsal_local_command,
        _first_user_artifacts,
        _first_user_readiness_scorecard,
        _glob_relative_files,
        _json_payload,
        _load_release_readiness,
        _package_spec_uses_package_index,
        _pip_install_command,
        _pip_upgrade_command,
        _resolve_python_executable,
        _resolve_install_package_spec,
        _run,
        _run_rehearsal_step,
        _run_rehearsal_step_to_file,
        render_package_install_rehearsal_text,
        run_package_install_rehearsal,
    )
    from code_mower.versioning import release_tag_for_version
else:
    try:
        from . import __version__ as CODE_MOWER_VERSION
        from .migration_mirror import (
            PRODUCT_SUPPORT_PATTERNS,
            RUNNER_ALIASES,
            _default_local_command,
            _line_requires_workflow_file,
            _relative_existing_files,
            _workflow_file_references,
            _workflow_local_fallback_references,
            render_mirror_removal_plan,
            render_mirror_removal_text,
            render_runner_aliases,
            render_runner_aliases_text,
        )
        from .migration_rehearsal import (
            FIRST_USER_ARTIFACTS,
            MIRRORED_IMPLEMENTATION_PATTERNS,
            PRIVACY_EXCLUDED_CONTENT,
            CommandResult,
            RehearsalError,
            RunOutput,
            _default_product_rehearsal_local_command,
            _first_user_artifacts,
            _first_user_readiness_scorecard,
            _glob_relative_files,
            _json_payload,
            _load_release_readiness,
            _package_spec_uses_package_index,
            _pip_install_command,
            _pip_upgrade_command,
            _resolve_python_executable,
            _resolve_install_package_spec,
            _run,
            _run_rehearsal_step,
            _run_rehearsal_step_to_file,
            render_package_install_rehearsal_text,
            run_package_install_rehearsal,
        )
        from .versioning import release_tag_for_version
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from code_mower import __version__ as CODE_MOWER_VERSION
        from code_mower.migration_mirror import (
            PRODUCT_SUPPORT_PATTERNS,
            RUNNER_ALIASES,
            _default_local_command,
            _line_requires_workflow_file,
            _relative_existing_files,
            _workflow_file_references,
            _workflow_local_fallback_references,
            render_mirror_removal_plan,
            render_mirror_removal_text,
            render_runner_aliases,
            render_runner_aliases_text,
        )
        from code_mower.migration_rehearsal import (
            FIRST_USER_ARTIFACTS,
            MIRRORED_IMPLEMENTATION_PATTERNS,
            PRIVACY_EXCLUDED_CONTENT,
            CommandResult,
            RehearsalError,
            RunOutput,
            _default_product_rehearsal_local_command,
            _first_user_artifacts,
            _first_user_readiness_scorecard,
            _glob_relative_files,
            _json_payload,
            _load_release_readiness,
            _package_spec_uses_package_index,
            _pip_install_command,
            _pip_upgrade_command,
            _resolve_python_executable,
            _resolve_install_package_spec,
            _run,
            _run_rehearsal_step,
            _run_rehearsal_step_to_file,
            render_package_install_rehearsal_text,
            run_package_install_rehearsal,
        )
        from code_mower.versioning import release_tag_for_version

__all__ = [
    "FIRST_USER_ARTIFACTS",
    "MIRRORED_IMPLEMENTATION_PATTERNS",
    "PRIVACY_EXCLUDED_CONTENT",
    "PRODUCT_SUPPORT_PATTERNS",
    "RUNNER_ALIASES",
    "CommandResult",
    "RehearsalError",
    "RunOutput",
    "_default_local_command",
    "_default_product_rehearsal_local_command",
    "_first_user_artifacts",
    "_first_user_readiness_scorecard",
    "_glob_relative_files",
    "_json_payload",
    "_line_requires_workflow_file",
    "_load_release_readiness",
    "_package_spec_uses_package_index",
    "_pip_install_command",
    "_pip_upgrade_command",
    "_relative_existing_files",
    "_resolve_python_executable",
    "_resolve_install_package_spec",
    "_run",
    "_run_rehearsal_step",
    "_run_rehearsal_step_to_file",
    "_workflow_file_references",
    "_workflow_local_fallback_references",
    "render_mirror_removal_plan",
    "render_mirror_removal_text",
    "render_package_install_rehearsal_text",
    "render_runner_aliases",
    "render_runner_aliases_text",
    "render_setup_drift_report",
    "render_setup_drift_text",
    "run_package_install_rehearsal",
]


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
SETUP_DRIFT_SCHEMA = "code_mower.setupDrift.v1"
SETUP_DRIFT_CLASSIFICATIONS = ("same", "differs", "new", "repo-only", "missing-from-output")
SETUP_DRIFT_SETUP_FILENAMES = {
    "calibration-corpus.json",
    "code-mower.yml",
    "context-packs.json",
    "reviewer-spend.json",
    "reviewer-value-report.example.md",
}
SETUP_DRIFT_WORKFLOW_KEYWORDS = (
    "antigravity",
    "claude",
    "code-mower",
    "codex",
    "cursor",
    "devin",
    "dispatch-lanes",
    "gemini",
    "gitar",
    "grok",
    "local-cli-audit",
    "needs-owner",
    "weekly-status",
)
SETUP_DRIFT_TOOL_FILENAMES = {
    "audit_labeler_lib.py",
    "code_mower",
    "code_mower_standalone_pin.env",
    "code_mower_standalone_shadow.sh",
    "decisions.py",
    "safe_gh_comment.py",
    "status_report.py",
}
STANDALONE_PIN_RELATIVE_PATH = "tools/code_mower_standalone_pin.env"
STANDALONE_PIN_REF_KEY = "CODE_MOWER_STANDALONE_REF"
STANDALONE_PIN_PLACEHOLDER_FRAGMENT = "pin-a-reviewed-code-mower"
STANDALONE_PIN_SAFE_REF_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._/@+-"
)


def _parse_standalone_pin_ref(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == STANDALONE_PIN_REF_KEY:
            try:
                parts = shlex.split(value, comments=True, posix=True)
            except ValueError:
                return ""
            return parts[0] if parts else ""
    return ""


def _is_standalone_pin_placeholder(ref: str) -> bool:
    normalized = ref.strip()
    return (
        not normalized
        or normalized in {"TODO", "TBD"}
        or normalized.startswith("<")
        or STANDALONE_PIN_PLACEHOLDER_FRAGMENT in normalized
    )


def _safe_standalone_pin_ref(ref: str) -> str:
    value = ref.strip()
    if 0 < len(value) <= 160 and all(char in STANDALONE_PIN_SAFE_REF_CHARS for char in value):
        return value
    return "<redacted-ref>"


def _standalone_pin_drift_summary(
    repo_path: Path,
    *,
    package_version: str = CODE_MOWER_VERSION,
) -> dict[str, Any]:
    expected_ref = release_tag_for_version(package_version)
    summary: dict[str, Any] = {
        "path": STANDALONE_PIN_RELATIVE_PATH,
        "package_version": package_version,
        "expected_ref": expected_ref,
    }
    pin_path = repo_path / STANDALONE_PIN_RELATIVE_PATH
    if not pin_path.is_file():
        return {
            **summary,
            "status": "skip",
            "reason": "pin_file_absent",
            "current_ref_status": "absent",
            "next_action": "no standalone pin file found",
        }
    try:
        current_ref = _parse_standalone_pin_ref(pin_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {
            **summary,
            "status": "warn",
            "reason": "pin_file_unreadable",
            "current_ref_status": "unreadable",
            "read_error": exc.__class__.__name__,
            "next_action": "fix or regenerate the standalone pin file",
        }
    if _is_standalone_pin_placeholder(current_ref):
        return {
            **summary,
            "status": "warn",
            "reason": "pin_ref_placeholder",
            "current_ref_status": "placeholder",
            "next_action": "set CODE_MOWER_STANDALONE_REF to a reviewed release tag or commit in a PR",
        }
    status = "pass" if current_ref == expected_ref else "warn"
    return {
        **summary,
        "status": status,
        "reason": "matches_running_package" if status == "pass" else "pin_ref_differs",
        "current_ref_status": "present",
        "current_ref": _safe_standalone_pin_ref(current_ref),
        "next_action": (
            "standalone pin matches the running Code Mower package"
            if status == "pass"
            else "review whether tools/code_mower_standalone_pin.env should move to the current release tag"
        ),
    }


def _setup_drift_next_action(
    *,
    changed_count: int,
    standalone_pin: Mapping[str, Any],
) -> str:
    file_action = "review differs, new, repo-only, and missing-from-output entries before copying generated setup"
    pin_warn = standalone_pin.get("status") == "warn"
    if changed_count and pin_warn:
        return f"{file_action}; also {standalone_pin['next_action']}"
    if changed_count:
        return file_action
    if pin_warn:
        return str(standalone_pin["next_action"])
    return "generated setup matches tracked Code Mower files"


def _resolve_command(command_text: str) -> tuple[str, ...]:
    stripped = command_text.strip()
    if not stripped:
        raise ValueError("command must not be empty")
    path_command = Path(stripped).expanduser()
    if path_command.is_file():
        return (str(path_command.resolve()),)
    resolved = shutil.which(stripped)
    if resolved:
        return (resolved,)
    parts = tuple(shlex.split(stripped))
    if not parts:
        raise ValueError("command must not be empty")
    return parts


def _default_package_command() -> tuple[str, ...]:
    resolved = shutil.which("code-mower")
    return (resolved or "code-mower",)


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


def _load_init_module() -> Any:
    if __package__ in {None, ""}:  # pragma: no cover - script entrypoint fallback.
        import code_mower.init as code_mower_init
    else:
        from . import init as code_mower_init

    return code_mower_init


def _setup_drift_config_path(repo_path: Path, config_arg: str | None, code_mower_init: Any) -> Path:
    raw = config_arg or ("code-mower.yml" if (repo_path / "code-mower.yml").is_file() else "code-mower.example.yml")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and (repo_path / candidate).is_file():
        return (repo_path / candidate).resolve()
    return code_mower_init._resolve_config_path(raw)


def _git_tracked_files(repo_path: Path) -> tuple[set[str], bool]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files", "-z"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return set(), False
    return {Path(item).as_posix() for item in completed.stdout.split("\0") if item}, True


def _is_setup_candidate_path(path: str) -> bool:
    normalized = Path(path).as_posix()
    name = Path(normalized).name.lower()
    if normalized in SETUP_DRIFT_SETUP_FILENAMES:
        return True
    if normalized.startswith("docs/lanes/"):
        return True
    if normalized.startswith("tools/lane_configs/"):
        return True
    if normalized.startswith(".github/workflows/"):
        return any(keyword in name for keyword in SETUP_DRIFT_WORKFLOW_KEYWORDS)
    if normalized.startswith("tools/"):
        return (
            name in SETUP_DRIFT_TOOL_FILENAMES
            or name.startswith("code_mower")
            or (name.startswith("run_") and name.endswith("_audit_pr.sh"))
        )
    return False


def _setup_drift_file(
    path: str,
    classification: str,
    *,
    generated_bytes: int | None = None,
    repo_bytes: int | None = None,
    tracked: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": path,
        "classification": classification,
        "tracked": tracked,
    }
    if generated_bytes is not None:
        item["generated_bytes"] = generated_bytes
    if repo_bytes is not None:
        item["repo_bytes"] = repo_bytes
    return item


def _classify_setup_drift(
    *,
    repo_path: Path,
    generated_files: Mapping[str, str | None],
    tracked_files: set[str],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    generated_paths = set(generated_files)
    for path in sorted(generated_paths):
        generated = generated_files[path]
        repo_file = repo_path / path
        repo_is_tracked = path in tracked_files
        repo_exists = repo_file.is_file()
        if generated is None:
            files.append(
                _setup_drift_file(
                    path,
                    "missing-from-output",
                    tracked=repo_is_tracked,
                )
            )
            continue
        generated_bytes = generated.encode("utf-8")
        if repo_is_tracked and repo_exists:
            repo_bytes = repo_file.read_bytes()
            classification = "same" if repo_bytes == generated_bytes else "differs"
            files.append(
                _setup_drift_file(
                    path,
                    classification,
                    generated_bytes=len(generated_bytes),
                    repo_bytes=len(repo_bytes),
                    tracked=True,
                )
            )
        else:
            files.append(
                _setup_drift_file(
                    path,
                    "new",
                    generated_bytes=len(generated_bytes),
                    tracked=repo_is_tracked,
                )
            )

    for path in sorted(tracked_files - generated_paths):
        if _is_setup_candidate_path(path):
            repo_file = repo_path / path
            files.append(
                _setup_drift_file(
                    path,
                    "repo-only",
                    repo_bytes=repo_file.stat().st_size if repo_file.is_file() else None,
                    tracked=True,
                )
            )
    order = {name: index for index, name in enumerate(SETUP_DRIFT_CLASSIFICATIONS)}
    return sorted(files, key=lambda item: (order.get(str(item["classification"]), 99), str(item["path"])))


def _generated_setup_files_from_plan(plan: Any, *, source_root: Path, code_mower_init: Any) -> dict[str, str | None]:
    generated: dict[str, str | None] = {}
    for entry in plan.data["generated_files"]:
        path = str(entry["path"])
        try:
            materialized = code_mower_init._materialize_generated_file(
                entry,
                path,
                Path(path),
                source_root=source_root,
            )
        except (OSError, KeyError, ValueError):
            generated[path] = None
            continue
        generated[path] = materialized.text
    return generated


def render_setup_drift_report(
    *,
    repo_path: Path,
    config: str | None = None,
    profile: str = "recommended",
    builders: str = "",
    add_repositories: Sequence[str] = (),
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo path is not a directory: {repo_path}")
    code_mower_init = _load_init_module()
    config_path = _setup_drift_config_path(repo_path, config, code_mower_init)
    builder_lanes = code_mower_init._parse_builder_lanes(builders) if builders else ()
    loaded_config, added_repos = code_mower_init.config_with_added_repositories(
        code_mower_init.load_config(config_path),
        tuple(add_repositories),
    )
    plan = code_mower_init.render_init_plan(
        loaded_config,
        profile_id=profile,
        config_path=str(config_path),
        add_repositories=added_repos,
        builders=builder_lanes,
    )
    generated = _generated_setup_files_from_plan(
        plan,
        source_root=code_mower_init._repo_root().resolve(),
        code_mower_init=code_mower_init,
    )
    tracked, tracked_available = _git_tracked_files(repo_path)
    files = _classify_setup_drift(
        repo_path=repo_path,
        generated_files=generated,
        tracked_files=tracked,
    )
    counts = {name: 0 for name in SETUP_DRIFT_CLASSIFICATIONS}
    for item in files:
        counts[str(item["classification"])] = counts.get(str(item["classification"]), 0) + 1
    changed_count = sum(counts[name] for name in SETUP_DRIFT_CLASSIFICATIONS if name != "same")
    standalone_pin = _standalone_pin_drift_summary(repo_path)
    standalone_pin_warn = standalone_pin["status"] == "warn"
    return {
        "schema": SETUP_DRIFT_SCHEMA,
        "mode": "setup-drift",
        "status": "pass" if changed_count == 0 and not standalone_pin_warn else "warn",
        "repo_path": str(repo_path),
        "config": str(config_path),
        "profile": profile,
        "builders": list(builder_lanes),
        "additional_repositories": list(added_repos),
        "tracked_source": "git" if tracked_available else "unavailable",
        "counts": counts,
        "file_count": len(files),
        "changed_count": changed_count,
        "standalone_pin": standalone_pin,
        "files": files,
        "next_action": _setup_drift_next_action(
            changed_count=changed_count,
            standalone_pin=standalone_pin,
        ),
    }


def render_setup_drift_text(payload: dict[str, Any], *, limit: int = 50) -> str:
    counts = payload.get("counts") or {}
    lines = [
        "Code Mower setup drift",
        f"Status: {payload['status']}",
        f"Repo: {payload['repo_path']}",
        f"Profile: {payload['profile']}",
        "Counts: "
        + ", ".join(f"{name}={counts.get(name, 0)}" for name in SETUP_DRIFT_CLASSIFICATIONS),
        f"Next: {payload['next_action']}",
        "",
    ]
    standalone_pin = payload.get("standalone_pin") or {}
    if standalone_pin and standalone_pin.get("status") != "skip":
        current = (
            f" current={standalone_pin['current_ref']}"
            if standalone_pin.get("current_ref")
            else ""
        )
        lines.extend(
            [
                f"Standalone pin: {str(standalone_pin['status']).upper()} "
                f"{standalone_pin['reason']} expected={standalone_pin['expected_ref']}{current}",
                "",
            ]
        )
    changed = [item for item in payload.get("files") or [] if item.get("classification") != "same"]
    if not changed:
        lines.append("- PASS generated setup matches tracked Code Mower files")
    else:
        for item in changed[:limit]:
            details = []
            if "repo_bytes" in item:
                details.append(f"repo={item['repo_bytes']}b")
            if "generated_bytes" in item:
                details.append(f"generated={item['generated_bytes']}b")
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"- {str(item['classification']).upper()} {item['path']}{suffix}")
        remaining = len(changed) - limit
        if remaining > 0:
            lines.append(f"- ... {remaining} more changed path(s); rerun with --json for the full list")
    return "\n".join(lines) + "\n"


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
        raise ValueError("could not infer local product Code Mower command; pass --local-command")

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
    mirror = subparsers.add_parser("mirror-removal-plan")
    mirror.add_argument("--repo-path", type=Path, default=Path.cwd())
    mirror.add_argument("--shadow-cycles", type=int, default=0)
    mirror.add_argument("--required-shadow-cycles", type=int, default=1)
    mirror.add_argument("--standalone-default-cycles", type=int, default=0)
    mirror.add_argument("--required-standalone-default-cycles", type=int, default=1)
    mirror.add_argument("--json", action="store_true")
    setup_drift = subparsers.add_parser("setup-drift")
    setup_drift.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Code Mower config to render; defaults to repo code-mower.yml or packaged starter config",
    )
    setup_drift.add_argument("--repo-path", type=Path, default=Path.cwd())
    setup_drift.add_argument("--profile", default="recommended")
    setup_drift.add_argument(
        "--builders",
        metavar="LANES",
        default="",
        help="comma-separated builder lanes to include, e.g. codex,claude,cursor",
    )
    setup_drift.add_argument(
        "--add-repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="append a sibling repository target while rendering the setup plan",
    )
    setup_drift.add_argument("--limit", type=int, default=50, help="changed paths to show in text output")
    setup_drift.add_argument("--json", action="store_true")
    aliases = subparsers.add_parser("runner-aliases")
    aliases.add_argument(
        "--legacy",
        default=None,
        help="optional legacy script path or basename to filter, e.g. run_codex_audit_pr.sh",
    )
    aliases.add_argument("--json", action="store_true")
    release = subparsers.add_parser("release-readiness")
    release.add_argument("--repo-path", type=Path, default=Path.cwd())
    release.add_argument("--json", action="store_true")
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
        help="Python 3.12+ executable used to create the clean rehearsal venv",
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
    package_install.add_argument(
        "--allow-package-index",
        action="store_true",
        help=(
            "allow bare package-index specs such as code-mower==0.9.2b1; "
            "normal unit/CI rehearsals should use a local path instead"
        ),
    )
    package_install.add_argument(
        "--upgrade-pip",
        action="store_true",
        help="upgrade pip inside the rehearsal venv before installing the package",
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
                local_command=_resolve_command(args.local_command) if args.local_command else None,
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

    if args.command == "setup-drift":
        try:
            payload = render_setup_drift_report(
                repo_path=args.repo_path,
                config=args.config,
                profile=args.profile,
                builders=args.builders,
                add_repositories=tuple(args.add_repo),
            )
        except (OSError, ValueError) as exc:
            payload = {
                "schema": SETUP_DRIFT_SCHEMA,
                "mode": "setup-drift",
                "status": "fail",
                "error": str(exc),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"setup drift failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_setup_drift_text(payload, limit=args.limit), end="")
        return 0

    if args.command == "runner-aliases":
        payload = render_runner_aliases(legacy=args.legacy)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_runner_aliases_text(payload), end="")
        return 0

    if args.command == "release-readiness":
        release_readiness = _load_release_readiness()
        payload = release_readiness.render_release_readiness(repo_path=args.repo_path)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(release_readiness.render_release_readiness_text(payload), end="")
        return 0 if payload["status"] == "pass" else 1

    if args.command == "package-install-rehearsal":
        try:
            payload = run_package_install_rehearsal(
                package_spec=args.package_spec,
                repo_path=args.repo_path,
                local_command=_resolve_command(args.local_command) if args.local_command else None,
                python=args.python,
                work_dir=args.work_dir,
                timeout=args.timeout,
                shadow_cycles=args.shadow_cycles,
                standalone_default_cycles=args.standalone_default_cycles,
                pip_index_url=args.pip_index_url,
                pip_extra_index_urls=args.pip_extra_index_url,
                allow_package_index=args.allow_package_index,
                upgrade_pip=args.upgrade_pip,
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
