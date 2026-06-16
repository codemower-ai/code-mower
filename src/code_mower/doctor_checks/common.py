"""Shared doctor helper functions and dependency shims."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import STATUS_FAIL, STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parents[1]))
    if module_dir.parent.name == "code_mower":  # pragma: no cover - direct package path.
        from code_mower import config as code_mower_config
        from code_mower import local_llm_profiles
        from code_mower import package as code_mower_package
        from code_mower import secrets as code_mower_secrets
    else:  # pragma: no cover - extracted direct CLI path.
        from tools import (
            code_mower_config,
            code_mower_package,
            code_mower_secrets,
            local_llm_profiles,
        )
elif __package__.startswith("tools"):
    from .. import (
        code_mower_config,
        code_mower_package,
        code_mower_secrets,
        local_llm_profiles,
    )
else:  # pragma: no cover - exercised after package extraction.
    from .. import config as code_mower_config
    from .. import local_llm_profiles
    from .. import package as code_mower_package
    from .. import secrets as code_mower_secrets


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

__all__ = [
    "ACTIONS_BILLING_BLOCK_PATTERNS",
    "ACTIONS_COST_SAMPLE_DEFAULT",
    "ACTIONS_COST_SAMPLE_MAX",
    "ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS",
    "ACTIONS_METADATA_WORKFLOW_MARKERS",
    "DoctorCheck",
    "MAX_ACTIONS_FAILED_JOBS_TO_INSPECT",
    "MAX_ACTIONS_FAILED_RUNS_TO_INSPECT",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_SKIP",
    "STATUS_WARN",
    "SUPPORTED_TOKEN_FILE_ENV_NAMES",
    "TRUTHY_ENV_VALUES",
    "as_mapping",
    "as_sequence",
    "code_mower_config",
    "code_mower_package",
    "code_mower_secrets",
    "join_names",
    "load_inputs",
    "local_cli_remediation",
    "local_llm_profiles",
    "token_remediation",
]


def as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise code_mower_config.ConfigError(f"{name} must be a mapping")


def as_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def join_names(names: Sequence[str]) -> str:
    return ", ".join(str(name) for name in names)


def token_remediation(
    missing: Sequence[str],
    missing_any: Sequence[Sequence[str]],
) -> str:
    parts: list[str] = []
    if missing:
        parts.append(
            "set "
            + join_names(missing)
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
            parts.append("set one of " + join_names(names))
    if not parts:
        return ""
    return "; ".join(parts) + "."


def local_cli_remediation(commands: Sequence[str], command_env: str = "") -> str:
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


def load_inputs(
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
