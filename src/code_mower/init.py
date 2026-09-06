#!/usr/bin/env python3
"""Render a non-mutating Code Mower init plan for a setup profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools import audit_limits as code_mower_audit_limits
    from tools import decisions as code_mower_decisions
    from tools import code_mower_secrets
    from tools.code_mower_config import (
        ConfigError,
        RenderedPlan,
        _format_issues,
        _labels_for,
        load_config,
        required_secret_entries_for_lane,
        validate_config,
    )
    from tools.doctor_checks.github_human_token import human_automation_token_required
    from tools.workflow_actionlint import (
        GeneratedWorkflow,
        WorkflowLintError,
        WorkflowLintUnavailable,
        custom_self_hosted_runner_labels,
        is_github_workflow_path,
        run_actionlint_on_workflows,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import audit_limits as code_mower_audit_limits
    from . import decisions as code_mower_decisions
    from . import secrets as code_mower_secrets
    from .config import (
        ConfigError,
        RenderedPlan,
        _format_issues,
        _labels_for,
        load_config,
        required_secret_entries_for_lane,
        validate_config,
    )
    from .doctor_checks.github_human_token import human_automation_token_required
    from .workflow_actionlint import (
        GeneratedWorkflow,
        WorkflowLintError,
        WorkflowLintUnavailable,
        custom_self_hosted_runner_labels,
        is_github_workflow_path,
        run_actionlint_on_workflows,
    )


WORKFLOW_TARGETS_BY_DRIVER = {
    "api_model": (
        ".github/workflows/{lane_stem}.yml",
        ".github/workflows/{lane_stem}-labeler.yml",
    ),
    "hosted_bridge": (
        ".github/workflows/{lane_stem}-bridge.yml",
        ".github/workflows/{lane_stem}-labeler.yml",
    ),
    "local_cli": (".github/workflows/{lane_stem}-labeler.yml",),
    "manual": (),
    "saas_event": (".github/workflows/{lane_stem}-labeler.yml",),
}

LOCAL_AUDIT_WORKFLOW_PATH = ".github/workflows/local-cli-audit.yml"
LOCAL_AUDIT_WORKFLOW_SOURCE = "self-hosted-local-audit-workflow-template"
LOCAL_AUDIT_WORKFLOW_TEMPLATE = "templates/workflows/self-hosted-local-audit.yml.j2"
LOCAL_AUDIT_RUNNER_LABEL = "code-mower-audit"
LOCAL_AUDIT_WRAPPERS = {
    "claude": "tools/run_claude_audit_pr.sh",
    "codex": "tools/run_codex_audit_pr.sh",
    "devin-cli": "tools/run_devin_cli_audit_pr.sh",
}
GATE_HEALTH_WORKFLOW_PATH = ".github/workflows/code-mower-gate-health.yml"
GATE_HEALTH_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-gate-health.yml.j2"
AGENT_PR_LABELER_WORKFLOW_PATH = ".github/workflows/code-mower-agent-pr-labeler.yml"
AGENT_PR_LABELER_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-agent-pr-labeler.yml.j2"
FIX_ROUND_DISPATCH_WORKFLOW_PATH = ".github/workflows/code-mower-fix-round-dispatch.yml"
FIX_ROUND_DISPATCH_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-fix-round-dispatch.yml.j2"
BUILDER_DISPATCH_WORKFLOW_PATH = ".github/workflows/dispatch-lanes.yml"
BUILDER_DISPATCH_WORKFLOW_TEMPLATE = "templates/workflows/dispatch-lanes.yml.j2"
LANE_MAC_RUNNER_WORKFLOW_PATH = ".github/workflows/lane-mac-runner.yml"
LANE_MAC_RUNNER_WORKFLOW_TEMPLATE = "templates/workflows/lane-mac-runner.yml.j2"
LANE_MAC_RUNNER_SCRIPT_PATH = "tools/lanes/run_mac_lane.sh"
LANE_MAC_RUNNER_SCRIPT_TEMPLATE = "templates/lanes/run_mac_lane.sh"
LANE_STANDING_README_PATH = "docs/lanes/README.md"
LANE_STANDING_README_TEMPLATE = "templates/lanes/README.md"
BUILDER_LANE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
BUILDER_ALIAS_TO_LANE = {
    "cursor": "cursor",
    "devin-cli": "devin",
    "devin-cloud": "devin",
    "grok": "cursor",
    "grok-bot": "cursor",
}
BUILDER_LEGACY_BUILDER_LABELS = {
    "cursor": ("builder:grok-bot",),
}
BUILDER_LEGACY_DISPATCH_LABELS = {
    "cursor": ("dispatched:grok-bot",),
}
BUILDER_DEFAULT_MENTIONS = {
    "claude": "Claude lane -",
    "codex": "@codex",
    "cursor": "@cursor",
}
BUILDER_DEFAULT_DOC_SLUGS = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor",
    "devin": "devin",
}
MAC_RUNNER_BUILDER_LANES = {"claude", "codex", "devin"}

DEFAULT_APPLY_OUTPUT_DIR = ".code-mower.generated"
APPLY_MANIFEST_FILENAME = "code-mower-init-plan.json"
APPLY_SUMMARY_FILES = (
    "labels.txt",
    "required-secrets.txt",
    "required-variables.txt",
    "smoke-tests.sh",
)
ADOPTION_CONFIG_PATH = "code-mower.yml"
ADOPTION_CONFIG_FIELDS = (
    "repositories[0].slug",
    "repositories[0].default_branch",
    "owner_surface.owner_login",
    "owner_surface.status_issue",
    "decisions.authorities",
)
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REFERENCE_PYTHON = ".code-mower-venv/bin/python"
GEMINI_AUTH_FILE_ENV = "GEMINI_API_KEY_FILE"
GEMINI_AUTH_ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
STARTER_DATA_FILES = (
    (
        "calibration-corpus.json",
        "tools/calibration_corpus.example.json",
        "templates/calibration-corpus.example.json",
        "starter-calibration-corpus",
    ),
    (
        "context-packs.json",
        "tools/context_packs.example.json",
        "templates/context-packs.example.json",
        "starter-context-packs",
    ),
    (
        "reviewer-spend.json",
        "tools/reviewer_spend.example.json",
        "templates/reviewer-spend.example.json",
        "starter-reviewer-spend",
    ),
    (
        "reviewer-value-report.example.md",
        "tools/reviewer_value_report.example.md",
        "templates/reviewer-value-report.example.md",
        "starter-reviewer-value-report",
    ),
)

PRODUCT_SUPPORT_FILES = (
    (
        "tools/code_mower",
        "templates/product-support/code_mower",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/code_mower_standalone_shadow.sh",
        "templates/product-support/code_mower_standalone_shadow.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/code_mower_standalone_pin.env",
        "templates/product-support/code_mower_standalone_pin.env",
        "product-support-config",
        "0644",
    ),
    (
        "tools/run_codex_audit_pr.sh",
        "templates/product-support/run_codex_audit_pr.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/run_claude_audit_pr.sh",
        "templates/product-support/run_claude_audit_pr.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/run_devin_cli_audit_pr.sh",
        "templates/product-support/run_devin_cli_audit_pr.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        # Copy the packaged helper module so product-repo gate workflows can
        # import tools.audit_labeler_lib without a separate package install.
        "tools/audit_labeler_lib.py",
        "audit_labeler_lib.py",
        "product-support-helper",
        "0644",
    ),
    (
        "tools/decisions.py",
        "decisions.py",
        "product-support-helper",
        "0644",
    ),
    (
        "tools/safe_gh_comment.py",
        "templates/product-support/safe_gh_comment.py",
        "product-support-helper",
        "0755",
    ),
    (
        "tools/status_report.py",
        "templates/product-support/status_report.py",
        "product-support-status-report",
        "0755",
    ),
)

OWNER_SURFACE_DEFAULTS = {
    "owner_login": "TODO_OWNER_LOGIN",
    "needs_owner_label": "needs-owner",
    "owner_decision_label": "owner-decision",
    "owner_sitting_label": "owner-sitting",
    "gate_override_label": "gate:override",
    "status_issue": "TODO_STATUS_ISSUE",
    "weekly_cron": "0 14 * * 1",
    "gate_health_cron": "*/15 * * * *",
    "gate_health_max_wait_minutes": "30",
    "gate_health_liveness_minutes": "45",
    "local_audit_runner_label": LOCAL_AUDIT_RUNNER_LABEL,
    "local_audit_runner_enabled_var": "CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED",
    "lane_runner_labels": "self-hosted,macOS,code-mower-lane",
    "lane_runner_enabled_var": "LANE_MAC_RUNNER_ENABLED",
    "lane_runner_cron": "*/15 * * * *",
    "lane_runner_max_minutes": "90",
    "lane_runner_trusted_authors": "",
    "builder_dispatch_cron": "*/30 * * * *",
    "builder_wip_cap": "5",
    "ready_label": "tier:R",
    "phase_labels": "phase:0,phase:1,phase:2,phase:3,phase:4,phase:5",
    "reviewer_spend_path": ".code-mower/reviewer-spend.json",
    "reviewer_value_report_path": ".code-mower/reviewer-value-report.md",
    "dispatch_token_env": "DISPATCH_TOKEN",
    "dispatch_token_expires_var": "DISPATCH_TOKEN_EXPIRES_AT",
}
LANE_MAC_RUNNER_TIMEOUT_GRACE_MINUTES = 15

HUMAN_AUTOMATION_TOKEN_SCOPES = (
    "Contents: read",
    "Issues: read/write",
    "Pull requests: read/write",
)

OWNER_SURFACE_WORKFLOW_FILES = (
    (
        ".github/workflows/needs-owner-notify.yml",
        "templates/workflows/needs-owner-notify.yml.j2",
        "owner-notify-workflow-template",
    ),
    (
        ".github/workflows/weekly-status.yml",
        "templates/workflows/weekly-status.yml.j2",
        "weekly-status-workflow-template",
    ),
)

GATE_WORKFLOW_PATH = ".github/workflows/code-mower-gate.yml"
GATE_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-gate.yml.j2"
DEFAULT_OWNER_LABEL = "needs-owner"


@dataclass(frozen=True)
class InitProfile:
    profile_id: str
    description: str
    lanes: tuple[str, ...]


@dataclass(frozen=True)
class MaterializedGeneratedFile:
    entry: Mapping[str, Any]
    path: str
    destination: Path
    text: str
    placeholder: bool


def _profile(config: Mapping[str, Any], profile_id: str) -> InitProfile:
    profiles = config.get("profiles")
    if not isinstance(profiles, Mapping) or profile_id not in profiles:
        available = ", ".join(sorted(profiles)) if isinstance(profiles, Mapping) else "none"
        raise ConfigError(f"unknown profile {profile_id!r}; available profiles: {available}")
    profile = profiles[profile_id]
    if not isinstance(profile, Mapping):
        raise ConfigError(f"profile {profile_id!r} must be a mapping")
    lanes = profile.get("lanes", [])
    if not isinstance(lanes, list):
        raise ConfigError(f"profile {profile_id!r} lanes must be a list")
    return InitProfile(
        profile_id=profile_id,
        description=str(profile.get("description", "")),
        lanes=tuple(str(lane) for lane in lanes),
    )


def _workflow_targets(lane_id: str, lane: Mapping[str, Any]) -> tuple[str, ...]:
    templates = WORKFLOW_TARGETS_BY_DRIVER.get(str(lane.get("driver")), ())
    normalized = lane_id.replace("_", "-")
    lane_type = str(lane.get("type"))
    suffix = "-review" if lane_type == "review" else "-audit"
    lane_stem = normalized if normalized.endswith(suffix) else f"{normalized}{suffix}"
    return tuple(template.format(lane_stem=lane_stem) for template in templates)


def _trailer_lane_name(lane_id: str, lane: Mapping[str, Any]) -> str:
    return str(lane.get("trailer_lane") or lane.get("lane_config") or lane_id)


def _author_lane_name(lane_id: str, lane: Mapping[str, Any]) -> str:
    return str(lane.get("author_lane") or _trailer_lane_name(lane_id, lane))


def _lane_module_name(lane_id: str) -> str:
    return lane_id.replace("-", "_")


def _display_name(raw_name: str) -> str:
    return raw_name.replace("_", " ").replace("-", " ").title()


def _workflow_name_for_target(target: str) -> str:
    stem = Path(target).stem.replace("-", " ")
    return stem[:1].upper() + stem[1:]


def _normalize_repo_slug(value: str, *, option: str = "--add-repo") -> str:
    slug = value.strip().strip("/")
    if not OWNER_REPO_RE.fullmatch(slug):
        raise ConfigError(f"{option} expects an OWNER/REPO slug")
    return slug


def _github_repo_slug_from_remote(remote_url: str) -> str:
    remote = remote_url.strip()
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("https://github.com/"):
        remote = remote.removeprefix("https://github.com/")
    elif remote.startswith("http://github.com/"):
        remote = remote.removeprefix("http://github.com/")
    elif remote.startswith("ssh://git@github.com/"):
        remote = remote.removeprefix("ssh://git@github.com/")
    else:
        return ""
    slug = remote.removesuffix(".git").strip("/")
    return slug if OWNER_REPO_RE.fullmatch(slug) else ""


def _detect_github_repo_slug(repo_path: Path, *, git_bin: str = "git") -> str:
    try:
        completed = subprocess.run(
            [git_bin, "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return _github_repo_slug_from_remote(completed.stdout)


def _target_repo_slug_for_labels(
    checkout_dir: Path,
    *,
    explicit_repo: str | None = None,
) -> str:
    if explicit_repo:
        return _normalize_repo_slug(explicit_repo, option="--repo")
    return _detect_github_repo_slug(checkout_dir)


def config_with_added_repositories(
    config: Mapping[str, Any],
    add_repos: list[str] | tuple[str, ...],
) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    """Return a config copy with additional repository targets appended."""

    if not add_repos:
        return config, ()
    repositories = list(config.get("repositories") or [])
    existing_slugs = {
        str(repo.get("slug") or "")
        for repo in repositories
        if isinstance(repo, Mapping)
    }
    added: list[str] = []
    for raw_slug in add_repos:
        slug = _normalize_repo_slug(raw_slug)
        if slug in existing_slugs:
            continue
        repositories.append({"slug": slug, "default_branch": "main"})
        existing_slugs.add(slug)
        added.append(slug)
    return {**dict(config), "repositories": repositories}, tuple(added)


def _repository_entries(config: Mapping[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for repo in config.get("repositories") or []:
        if not isinstance(repo, Mapping):
            continue
        slug = str(repo.get("slug") or "")
        if not slug:
            continue
        entry = {
            "slug": slug,
            "default_branch": str(repo.get("default_branch") or "main"),
        }
        local_path_env = str(repo.get("local_path_env") or "")
        if local_path_env:
            entry["local_path_env"] = local_path_env
        entries.append(entry)
    return entries


def _audit_token_env(lane: Mapping[str, Any]) -> str:
    token_env = lane.get("token_env", [])
    if not isinstance(token_env, list):
        token_env = []
    names = [str(name) for name in token_env]
    for name in names:
        if name != "GITHUB_TOKEN" and name.endswith("_LABEL_TOKEN"):
            return name
    for name in names:
        if name != "GITHUB_TOKEN":
            return name
    return "CODE_MOWER_AUDIT_LABEL_TOKEN"


def _token_env_names(lane: Mapping[str, Any]) -> tuple[str, ...]:
    token_env = lane.get("token_env", [])
    if isinstance(token_env, str):
        return (token_env,)
    if isinstance(token_env, list):
        return tuple(str(name) for name in token_env)
    return ()


def _bot_author_csv(authors: str, lane: Mapping[str, Any]) -> str:
    values = [author.strip() for author in authors.split(",") if author.strip()]
    return ",".join(dict.fromkeys(values))


def _authors_env_for_trailer_lane(trailer_lane: str) -> str:
    lane_config = _load_trailer_lane_config(trailer_lane)
    if lane_config is not None:
        return lane_config.authors_env_var
    normalized = trailer_lane.replace("-", "_").upper()
    explicit = {
        "CLAUDE": "CLAUDE_AUDIT_BOT_AUTHORS",
        "CODEX": "CODEX_BOT_AUTHORS",
        "DEVIN": "DEVIN_BOT_AUTHORS",
    }
    return explicit.get(normalized, f"{normalized}_BOT_AUTHORS")


def _authors_env_for_lane(lane_id: str, lane: Mapping[str, Any]) -> str:
    if lane.get("driver") == "saas_event":
        adapter = str(lane.get("adapter") or lane.get("provider") or lane_id)
        return f"{adapter.replace('-', '_').upper()}_BOT_AUTHORS"
    return _authors_env_for_trailer_lane(_trailer_lane_name(lane_id, lane))


def _load_trailer_lane_config(trailer_lane: str) -> Any | None:
    try:
        if __package__ in {None, "", "tools"}:
            from tools.lane_configs import load_lane_config
        else:  # pragma: no cover - package import path covered by CLI tests.
            from .lane_configs import load_lane_config

        return load_lane_config(trailer_lane)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None


def _default_trailer_bot_authors(trailer_lane: str) -> str:
    lane_config = _load_trailer_lane_config(trailer_lane)
    if lane_config is not None:
        return ",".join(lane_config.default_authors)
    stem = trailer_lane.replace("_", "-")
    return f"{stem}-audit-bot,{stem}-audit-bot[bot]"


def _decision_coverage_for_trailer_lane(trailer_lane: str) -> bool:
    lane_config = _load_trailer_lane_config(trailer_lane)
    return bool(getattr(lane_config, "decision_coverage", False))


def _configured_bot_authors(lane: Mapping[str, Any]) -> str:
    provider_config = lane.get("provider_config")
    if not isinstance(provider_config, Mapping):
        return ""
    authors = provider_config.get("bot_authors", [])
    if not isinstance(authors, list):
        return ""
    return ",".join(str(author).strip() for author in authors if str(author).strip())


def _default_bot_authors_for_lane(lane_id: str, lane: Mapping[str, Any]) -> str:
    if lane.get("driver") == "saas_event":
        return _configured_bot_authors(lane)
    return _default_trailer_bot_authors(_trailer_lane_name(lane_id, lane))


def _trailer_prefix_for_lane(trailer_lane: str) -> str:
    return f"{trailer_lane.replace('-', '_').upper()}_AUDIT_STATE"


def _workflow_entry_for_target(
    lane_id: str,
    lane: Mapping[str, Any],
    target: str,
    *,
    author_exclusion_json: str = '{"enabled":false}',
    decision_authorities: str = "",
) -> dict[str, str]:
    driver = str(lane.get("driver"))
    labels = _labels_for(lane)
    trailer_lane = _trailer_lane_name(lane_id, lane)
    common = {
        "path": target,
        "lane_id": lane_id,
        "workflow_name": _workflow_name_for_target(target),
        "display_name": _display_name(trailer_lane),
        "needs_label": str(labels["needs"]),
        "done_label": str(labels["done"]),
        "blocked_label": str(labels["blocked"]),
        "label_token_env": _audit_token_env(lane),
        "author_exclusion_json": author_exclusion_json,
        "decision_authorities": decision_authorities,
    }
    if target.endswith("-bridge.yml"):
        return {
            **common,
            "source": "hosted-bridge-workflow-template",
            "copy_from": "templates/workflows/hosted-bridge.yml.j2",
            "package_copy_from": "templates/workflows/hosted-bridge.yml.j2",
        }
    if driver == "saas_event":
        adapter = str(lane.get("adapter") or lane.get("provider") or lane_id)
        return {
            **common,
            "source": "saas-reviewer-labeler-workflow-template",
            "copy_from": "templates/workflows/saas-reviewer-labeler.yml.j2",
            "package_copy_from": "templates/workflows/saas-reviewer-labeler.yml.j2",
            "adapter": adapter,
            "authors_env": f"{adapter.replace('-', '_').upper()}_BOT_AUTHORS",
            "bot_authors": _bot_author_csv(_configured_bot_authors(lane), lane),
        }
    return {
        **common,
        "source": "trailer-comment-labeler-workflow-template",
        "copy_from": "templates/workflows/trailer-comment-labeler.yml.j2",
        "package_copy_from": "templates/workflows/trailer-comment-labeler.yml.j2",
        "trailer_lane": trailer_lane,
        "trailer_prefix": _trailer_prefix_for_lane(trailer_lane),
        "authors_env": _authors_env_for_trailer_lane(trailer_lane),
        "bot_authors": _bot_author_csv(_default_trailer_bot_authors(trailer_lane), lane),
        "github_actions_workflows": LOCAL_AUDIT_WORKFLOW_PATH if driver == "local_cli" else "",
    }


def _trusted_author_variables_for_generated_files(
    generated_files: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    variables: list[str] = []
    for entry in generated_files:
        variable = str(entry.get("authors_env") or "").strip()
        if variable:
            variables.append(variable)
    return tuple(sorted(dict.fromkeys(variables)))


def _csv_value(value: Any, default: str) -> str:
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _csv_items(value: Any, default: str = "") -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return tuple(item.strip() for item in default.split(",") if item.strip())


def _lane_runner_label_items(value: Any) -> tuple[str, ...]:
    default = OWNER_SURFACE_DEFAULTS["lane_runner_labels"]
    return _csv_items(value, default) or _csv_items(default)


def _yaml_scalar(value: Any) -> str:
    return json.dumps(str(value))


def _shell_literal(value: Any) -> str:
    return shlex.quote(str(value))


def _python_syntax_check_command(path_expression: str) -> str:
    code = (
        "from pathlib import Path; "
        "import sys; "
        "path = Path(sys.argv[1]); "
        "compile(path.read_text(encoding='utf-8'), str(path), 'exec')"
    )
    return f"python3 -c {shlex.quote(code)} {path_expression}"


def _yaml_inline_list(items: Sequence[str]) -> str:
    return ", ".join(_yaml_scalar(item) for item in items)


def _owner_surface_config(config: Mapping[str, Any]) -> dict[str, str]:
    raw = config.get("owner_surface")
    surface = raw if isinstance(raw, Mapping) else {}
    rendered: dict[str, str] = {}
    for key, default in OWNER_SURFACE_DEFAULTS.items():
        if key == "lane_runner_labels":
            rendered[key] = ",".join(_lane_runner_label_items(surface.get(key)))
            continue
        if key in {"phase_labels", "lane_runner_trusted_authors"}:
            rendered[key] = _csv_value(surface.get(key), default)
            continue
        value = surface.get(key, default)
        if key == "gate_override_label" and key in surface and value is not None:
            rendered[key] = str(value).strip()
            continue
        rendered[key] = str(value).strip() if value is not None else default
        if not rendered[key]:
            rendered[key] = default
    return rendered


def _owner_surface_workflow_entry(
    path: str,
    copy_from: str,
    source_name: str,
    owner_surface: Mapping[str, str],
) -> dict[str, str]:
    return {
        "path": path,
        "source": source_name,
        "copy_from": copy_from,
        "package_copy_from": copy_from,
        "owner_login": owner_surface["owner_login"],
        "needs_owner_label": owner_surface["needs_owner_label"],
        "owner_decision_label": owner_surface["owner_decision_label"],
        "owner_sitting_label": owner_surface["owner_sitting_label"],
        "gate_override_label": owner_surface["gate_override_label"],
        "status_issue": owner_surface["status_issue"],
        "weekly_status_cron": owner_surface["weekly_cron"],
        "ready_label": owner_surface["ready_label"],
        "phase_labels": owner_surface["phase_labels"],
        "reviewer_spend_path": owner_surface["reviewer_spend_path"],
        "reviewer_value_report_path": owner_surface["reviewer_value_report_path"],
    }


def _local_audit_entries(
    selected_lanes: Mapping[str, Mapping[str, Any]],
    audit_settings: code_mower_audit_limits.AuditLimitSettings | None = None,
) -> tuple[dict[str, str], ...]:
    settings = audit_settings or code_mower_audit_limits.AuditLimitSettings()
    entries: list[dict[str, str]] = []
    for lane_id, lane in selected_lanes.items():
        if lane.get("driver") != "local_cli":
            continue
        provider_config = lane.get("provider_config", {})
        if provider_config.get("local_audit_eligible") is False:
            raise ConfigError(
                f"local audit lane {lane_id!r} is not available yet: it declares "
                "local_audit_eligible=false because the executable wrapper is not "
                "registered until #746. Choose an available local audit provider "
                "(for example claude or codex), or remove it from the profile."
            )
        trailer_lane = _trailer_lane_name(lane_id, lane)
        wrapper = LOCAL_AUDIT_WRAPPERS.get(trailer_lane.replace("_", "-"))
        if wrapper is None:
            continue
        labels = _labels_for(lane)
        entries.append(
            {
                "lane": trailer_lane,
                "display_name": _display_name(trailer_lane),
                "needs_label": str(labels["needs"]),
                "token_env": _audit_token_env(lane),
                "script": wrapper,
                "audit_budget_usd": settings.budget_usd,
                "audit_max_diff_bytes": str(settings.max_diff_bytes),
                "audit_max_diff_hard_limit_bytes": str(
                    settings.max_diff_hard_limit_bytes
                ),
            }
        )
    return tuple(entries)


def _actions_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _local_audit_label_expression(entries: tuple[dict[str, str], ...], source: str) -> str:
    label_path = "github.event.label.name" if source == "event" else "github.event.pull_request.labels.*.name"
    if source == "event":
        return " || ".join(
            f"{label_path} == {_actions_string(entry['needs_label'])}" for entry in entries
        )
    return " || ".join(
        "contains({label_path}, {label})".format(
            label_path=label_path,
            label=_actions_string(entry["needs_label"]),
        )
        for entry in entries
    )


def _local_audit_workflow_entry(
    entries: tuple[dict[str, str], ...],
    *,
    local_audit_runner_label: str = LOCAL_AUDIT_RUNNER_LABEL,
    local_audit_runner_enabled_var: str = "CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED",
) -> dict[str, str]:
    token_envs = sorted({entry["token_env"] for entry in entries if entry["token_env"]})
    token_assignments = "\n".join(
        f"          {token_env}: ${{{{ secrets.{token_env} }}}}" for token_env in token_envs
    )
    return {
        "path": LOCAL_AUDIT_WORKFLOW_PATH,
        "source": LOCAL_AUDIT_WORKFLOW_SOURCE,
        "copy_from": LOCAL_AUDIT_WORKFLOW_TEMPLATE,
        "package_copy_from": LOCAL_AUDIT_WORKFLOW_TEMPLATE,
        "local_audit_lanes_json": json.dumps(list(entries), sort_keys=True),
        "local_audit_label_match": _local_audit_label_expression(entries, "event"),
        "local_audit_label_contains": _local_audit_label_expression(entries, "pull_request"),
        "local_audit_runner_label": local_audit_runner_label,
        "local_audit_runner_enabled_var": local_audit_runner_enabled_var,
        "local_audit_token_env_assignments": token_assignments,
    }


def _var_env_assignments(names: Sequence[str], *, indent: str = "          ") -> str:
    unique = sorted({str(name).strip() for name in names if str(name).strip()})
    return "\n".join(
        f"{indent}{name}: ${{{{ vars.{name} || '' }}}}"
        for name in unique
    )


def _builder_authors_for_lane(
    builder_lane: str,
    author_exclusion: Mapping[str, Any],
) -> str:
    author_map = _identity_section(author_exclusion, "authors")
    normalized_builder = builder_lane.replace("_", "-").lower()
    return ",".join(
        author
        for author, lane in sorted(author_map.items())
        if str(lane).replace("_", "-").lower() == normalized_builder
    )


def _gate_health_lane_entry(
    lane_id: str,
    lane: Mapping[str, Any],
    author_exclusion: Mapping[str, Any],
) -> dict[str, str]:
    labels = _labels_for(lane)
    trailer_lane = _trailer_lane_name(lane_id, lane)
    author_lane = _author_lane_name(lane_id, lane)
    github_actions_workflows = (
        LOCAL_AUDIT_WORKFLOW_PATH if lane.get("driver") == "local_cli" else ""
    )
    return {
        "id": lane_id,
        "author_lane": author_lane,
        "display_name": _display_name(trailer_lane),
        "needs": str(labels["needs"]),
        "done": str(labels["done"]),
        "blocked": str(labels["blocked"]),
        "builder_label": f"builder:{author_lane}",
        "builder_authors": _builder_authors_for_lane(author_lane, author_exclusion),
        "bot_authors": _bot_author_csv(_default_bot_authors_for_lane(lane_id, lane), lane),
        "authors_env": _authors_env_for_lane(lane_id, lane),
        "github_actions_workflows": github_actions_workflows,
    }


def _gate_health_workflow_entry(
    selected_lanes: Mapping[str, Mapping[str, Any]],
    owner_surface: Mapping[str, str],
    *,
    author_exclusion_json: str,
    include_local_audit_runner: bool,
) -> dict[str, str]:
    author_exclusion = json.loads(author_exclusion_json)
    audit_lanes = [
        _gate_health_lane_entry(lane_id, lane, author_exclusion)
        for lane_id, lane in selected_lanes.items()
        if lane.get("type") == "audit"
    ]
    return {
        "path": GATE_HEALTH_WORKFLOW_PATH,
        "source": "code-mower-gate-health-workflow-template",
        "copy_from": GATE_HEALTH_WORKFLOW_TEMPLATE,
        "package_copy_from": GATE_HEALTH_WORKFLOW_TEMPLATE,
        "gate_health_lanes_json": json.dumps(
            audit_lanes,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "gate_health_author_env_assignments": _var_env_assignments(
            [lane.get("authors_env", "") for lane in audit_lanes]
        ),
        "gate_health_cron": owner_surface["gate_health_cron"],
        "gate_health_max_wait_minutes": owner_surface["gate_health_max_wait_minutes"],
        "gate_health_liveness_minutes": owner_surface["gate_health_liveness_minutes"],
        "local_audit_runner_label": owner_surface["local_audit_runner_label"] if include_local_audit_runner else "",
        "needs_owner_label": owner_surface["needs_owner_label"],
        "owner_sitting_label": owner_surface["owner_sitting_label"],
        "owner_login": owner_surface["owner_login"],
        "status_issue": owner_surface["status_issue"],
    }


def _agent_pr_labeler_workflow_entry(
    rules: Sequence[Mapping[str, Any]],
    audit_lanes: Sequence[Mapping[str, str]],
    owner_surface: Mapping[str, str],
) -> dict[str, str]:
    return {
        "path": AGENT_PR_LABELER_WORKFLOW_PATH,
        "source": "agent-pr-labeler-workflow-template",
        "copy_from": AGENT_PR_LABELER_WORKFLOW_TEMPLATE,
        "package_copy_from": AGENT_PR_LABELER_WORKFLOW_TEMPLATE,
        "agent_pr_rules_json": json.dumps(list(rules), separators=(",", ":"), sort_keys=True),
        "agent_pr_audit_lanes_json": json.dumps(
            list(audit_lanes),
            separators=(",", ":"),
            sort_keys=True,
        ),
        "dispatch_token_env": owner_surface["dispatch_token_env"],
    }


def _fix_round_dispatch_workflow_entry(
    rules: Sequence[Mapping[str, str]],
    audit_lanes: Sequence[Mapping[str, str]],
    owner_surface: Mapping[str, str],
) -> dict[str, str]:
    return {
        "path": FIX_ROUND_DISPATCH_WORKFLOW_PATH,
        "source": "fix-round-dispatch-workflow-template",
        "copy_from": FIX_ROUND_DISPATCH_WORKFLOW_TEMPLATE,
        "package_copy_from": FIX_ROUND_DISPATCH_WORKFLOW_TEMPLATE,
        "fix_round_rules_json": json.dumps(list(rules), separators=(",", ":"), sort_keys=True),
        "fix_round_audit_lanes_json": json.dumps(
            list(audit_lanes),
            separators=(",", ":"),
            sort_keys=True,
        ),
        "dispatch_token_env": owner_surface["dispatch_token_env"],
        "needs_owner_label": owner_surface["needs_owner_label"],
    }


def _build_loop_owner_labels(owner_surface: Mapping[str, str]) -> list[str]:
    labels = (
        owner_surface["needs_owner_label"],
        owner_surface["owner_decision_label"],
        owner_surface["owner_sitting_label"],
    )
    return [label for label in labels if label]


def _canonical_builder_lane(raw_name: str) -> str:
    normalized = raw_name.strip().replace("_", "-").lower()
    if not normalized:
        raise ConfigError("--builders requires at least one lane name")
    lane = BUILDER_ALIAS_TO_LANE.get(normalized, normalized)
    if not BUILDER_LANE_NAME_RE.fullmatch(lane):
        raise ConfigError(
            f"builder lane {raw_name!r} must match [A-Za-z0-9][A-Za-z0-9_-]*"
        )
    return lane


def _parse_builder_lanes(raw_builders: str | None) -> tuple[str, ...]:
    if raw_builders is None:
        return ()
    raw_items = [
        item.strip()
        for chunk in raw_builders.split(",")
        for item in chunk.split()
        if item.strip()
    ]
    if not raw_items:
        raise ConfigError("--builders requires at least one lane name")
    lanes: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        lane = _canonical_builder_lane(item)
        if lane in seen:
            continue
        seen.add(lane)
        lanes.append(lane)
    return tuple(lanes)


def _builder_doc_slug(builder_lane: str) -> str:
    return BUILDER_DEFAULT_DOC_SLUGS.get(builder_lane, builder_lane)


def _builder_mention(builder_lane: str, fix_round_rules: Sequence[Mapping[str, Any]]) -> str:
    for rule in fix_round_rules:
        if str(rule.get("builder_lane") or "") == builder_lane:
            mention = str(rule.get("mention") or "").strip()
            if mention:
                return mention
    return BUILDER_DEFAULT_MENTIONS.get(builder_lane, f"{_display_name(builder_lane)} lane -")


def _builder_audit_need_labels(
    builder_lane: str,
    audit_lanes: Sequence[Mapping[str, str]],
) -> tuple[str, ...]:
    labels: list[str] = []
    for lane in audit_lanes:
        if str(lane.get("author_lane") or lane.get("id") or "") == builder_lane:
            continue
        needs = str(lane.get("needs") or "")
        if needs:
            labels.append(needs)
    if not labels:
        labels.extend(str(lane.get("needs") or "") for lane in audit_lanes if lane.get("needs"))
    return tuple(dict.fromkeys(labels))


def _builder_dispatch_lane_entries(
    builder_lanes: Sequence[str],
    audit_lanes: Sequence[Mapping[str, str]],
    author_exclusion: Mapping[str, Any],
    fix_round_rules: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    labels_by_lane = _builder_labels_by_lane(author_exclusion)
    entries: list[dict[str, Any]] = []
    for builder_lane in builder_lanes:
        doc_slug = _builder_doc_slug(builder_lane)
        audit_labels = _builder_audit_need_labels(builder_lane, audit_lanes)
        doc_template_slug = (
            doc_slug if builder_lane in BUILDER_DEFAULT_DOC_SLUGS else "generic"
        )
        builder_label = labels_by_lane.get(builder_lane) or f"builder:{builder_lane}"
        dispatch_label = f"dispatched:{builder_lane}"
        entries.append(
            {
                "lane": builder_lane,
                "display_name": _display_name(builder_lane),
                "builder_label": builder_label,
                "builder_labels": list(_builder_label_aliases(author_exclusion, builder_lane)),
                "dispatch_label": dispatch_label,
                "dispatch_labels": list(_builder_dispatch_label_aliases(builder_lane)),
                "mention": _builder_mention(builder_lane, fix_round_rules),
                "doc": f"lanes/{doc_slug}.md",
                "doc_target": f"docs/lanes/{doc_slug}.md",
                "doc_template": f"templates/lanes/{doc_template_slug}.md",
                "audit_labels": ",".join(audit_labels),
                "audit_labels_display": ", ".join(audit_labels) if audit_labels else "none",
                "mac_runner": "true" if builder_lane in MAC_RUNNER_BUILDER_LANES else "false",
            }
        )
    return tuple(entries)


def _build_loop_trusted_authors(
    owner_surface: Mapping[str, str],
    decision_authorities: Sequence[str],
) -> tuple[str, ...]:
    trusted_authors = [
        *decision_authorities,
        *_csv_items(owner_surface["lane_runner_trusted_authors"], ""),
    ]
    return tuple(dict.fromkeys(trusted_authors))


def _dispatch_lanes_workflow_entry(
    builder_entries: Sequence[Mapping[str, Any]],
    owner_surface: Mapping[str, str],
    *,
    decision_authorities: Sequence[str] = (),
) -> dict[str, str]:
    owner_labels = _build_loop_owner_labels(owner_surface)
    return {
        "path": BUILDER_DISPATCH_WORKFLOW_PATH,
        "source": "builder-dispatch-workflow-template",
        "copy_from": BUILDER_DISPATCH_WORKFLOW_TEMPLATE,
        "package_copy_from": BUILDER_DISPATCH_WORKFLOW_TEMPLATE,
        "builder_dispatch_lanes_json": json.dumps(
            list(builder_entries),
            separators=(",", ":"),
            sort_keys=True,
        ),
        "builder_dispatch_cron": owner_surface["builder_dispatch_cron"],
        "build_loop_ready_label": owner_surface["ready_label"],
        "build_loop_owner_labels_json": json.dumps(
            [label for label in owner_labels if label],
            separators=(",", ":"),
        ),
        "build_loop_max_wip": owner_surface["builder_wip_cap"],
        "dispatch_token_env": owner_surface["dispatch_token_env"],
        "build_loop_trusted_authors_json": json.dumps(
            _build_loop_trusted_authors(owner_surface, decision_authorities),
            separators=(",", ":"),
        ),
    }


def _lane_mac_runner_workflow_entry(
    builder_entries: Sequence[Mapping[str, str]],
    owner_surface: Mapping[str, str],
) -> dict[str, str]:
    mac_lanes = [
        str(entry["lane"])
        for entry in builder_entries
        if str(entry.get("mac_runner") or "") == "true"
    ]
    labels = _lane_runner_label_items(owner_surface["lane_runner_labels"])
    max_minutes = code_mower_audit_limits.parse_positive_int(
        owner_surface["lane_runner_max_minutes"],
        field_name="owner_surface.lane_runner_max_minutes",
    )
    return {
        "path": LANE_MAC_RUNNER_WORKFLOW_PATH,
        "source": "lane-mac-runner-workflow-template",
        "copy_from": LANE_MAC_RUNNER_WORKFLOW_TEMPLATE,
        "package_copy_from": LANE_MAC_RUNNER_WORKFLOW_TEMPLATE,
        "lane_mac_runner_lanes_yaml": _yaml_inline_list(mac_lanes),
        "lane_mac_runner_lanes_display": ", ".join(mac_lanes),
        "lane_mac_runner_labels": _yaml_inline_list(labels),
        "lane_mac_runner_enabled_var": owner_surface["lane_runner_enabled_var"],
        "lane_mac_runner_cron": owner_surface["lane_runner_cron"],
        "lane_mac_runner_max_minutes": owner_surface["lane_runner_max_minutes"],
        "lane_mac_runner_timeout_minutes": str(
            max_minutes + LANE_MAC_RUNNER_TIMEOUT_GRACE_MINUTES
        ),
    }


def _lane_mac_runner_script_entry(
    builder_entries: Sequence[Mapping[str, str]],
    audit_lanes: Sequence[Mapping[str, str]],
    owner_surface: Mapping[str, str],
    *,
    config: Mapping[str, Any],
    decision_authorities: Sequence[str],
) -> dict[str, str]:
    mac_lanes = [
        str(entry["lane"])
        for entry in builder_entries
        if str(entry.get("mac_runner") or "") == "true"
    ]
    blocked_labels = [
        str(entry.get("blocked") or "")
        for entry in audit_lanes
        if str(entry.get("blocked") or "")
    ]
    audit_labels = {
        str(entry["author_lane"]): {
            "blocked": str(entry.get("blocked") or ""),
            "done": str(entry.get("done") or ""),
            "needs": str(entry.get("needs") or ""),
        }
        for entry in audit_lanes
    }
    builder_labels = {
        str(entry["lane"]): str(entry["builder_label"])
        for entry in builder_entries
        if str(entry.get("mac_runner") or "") == "true"
    }
    identity = config.get("builder_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    configured_prefixes = _identity_section(identity, "branch_prefixes")
    branch_prefixes: dict[str, list[str]] = {
        lane: [f"{lane}/"] for lane in mac_lanes
    }
    for prefix, lane in sorted(configured_prefixes.items()):
        if lane in branch_prefixes and prefix not in branch_prefixes[lane]:
            branch_prefixes[lane].append(prefix)
    return {
        "path": LANE_MAC_RUNNER_SCRIPT_PATH,
        "source": "lane-mac-runner-script-template",
        "copy_from": LANE_MAC_RUNNER_SCRIPT_TEMPLATE,
        "package_copy_from": LANE_MAC_RUNNER_SCRIPT_TEMPLATE,
        "package_copy_first": True,
        "mode": "0755",
        "lane_mac_runner_allowed_case": "|".join(mac_lanes) or "codex|claude",
        "lane_mac_runner_builder_labels_json": json.dumps(
            builder_labels,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "lane_mac_runner_branch_prefixes_json": json.dumps(
            branch_prefixes,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "lane_mac_runner_blocked_labels_jq": " or ".join(
            f'.name=={json.dumps(label)}' for label in blocked_labels
        )
        or "false",
        "lane_mac_runner_audit_labels_json": json.dumps(
            audit_labels,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "lane_mac_runner_owner_labels_json": json.dumps(
            _build_loop_owner_labels(owner_surface),
            separators=(",", ":"),
        ),
        "lane_mac_runner_trusted_authors": ",".join(
            _build_loop_trusted_authors(owner_surface, decision_authorities)
        ),
        "build_loop_ready_label": owner_surface["ready_label"],
        "needs_owner_label": owner_surface["needs_owner_label"],
    }


def _lane_standing_file_entry(
    entry: Mapping[str, str],
    owner_surface: Mapping[str, str],
) -> dict[str, str]:
    return {
        "path": str(entry["doc_target"]),
        "source": "lane-standing-instructions-template",
        "copy_from": str(entry["doc_template"]),
        "package_copy_from": str(entry["doc_template"]),
        "lane_name": str(entry["lane"]),
        "lane_display_name": str(entry["display_name"]),
        "builder_label": str(entry["builder_label"]),
        "required_audit_labels": str(entry["audit_labels_display"]),
        "needs_owner_label": owner_surface["needs_owner_label"],
    }


def _lane_standing_readme_entry(
    builder_entries: Sequence[Mapping[str, str]],
    owner_surface: Mapping[str, str],
) -> dict[str, str]:
    rows = "\n".join(
        "| {display} | `{doc}` | `{builder_label}` | `{dispatch_label}` | {trigger} |".format(
            display=entry["display_name"],
            doc=entry["doc"],
            builder_label=entry["builder_label"],
            dispatch_label=entry["dispatch_label"],
            trigger=(
                "Mac runner"
                if str(entry.get("mac_runner") or "") == "true"
                else f"{entry['mention']} dispatch comment"
            ),
        )
        for entry in builder_entries
    )
    return {
        "path": LANE_STANDING_README_PATH,
        "source": "lane-standing-readme-template",
        "copy_from": LANE_STANDING_README_TEMPLATE,
        "package_copy_from": LANE_STANDING_README_TEMPLATE,
        "builder_lane_rows": rows,
        "build_loop_ready_label": owner_surface["ready_label"],
        "needs_owner_label": owner_surface["needs_owner_label"],
        "owner_blocking_labels": ", ".join(
            f"`{label}`" for label in _build_loop_owner_labels(owner_surface)
        ),
        "build_loop_max_wip": owner_surface["builder_wip_cap"],
        "dispatch_token_env": owner_surface["dispatch_token_env"],
        "dispatch_token_expires_var": owner_surface["dispatch_token_expires_var"],
        "lane_mac_runner_enabled_var": owner_surface["lane_runner_enabled_var"],
    }


def _gate_lane_entry(lane_id: str, lane: Mapping[str, Any]) -> dict[str, Any]:
    labels = _labels_for(lane)
    trailer_lane = _trailer_lane_name(lane_id, lane)
    github_actions_workflows = LOCAL_AUDIT_WORKFLOW_PATH if lane.get("driver") == "local_cli" else ""
    return {
        "id": lane_id,
        "author_lane": _author_lane_name(lane_id, lane),
        "display_name": _display_name(trailer_lane),
        "done": str(labels["done"]),
        "blocked": str(labels["blocked"]),
        "decision_coverage": _decision_coverage_for_trailer_lane(trailer_lane),
        "builder_label": f"builder:{trailer_lane}",
        "bot_authors": _bot_author_csv(_default_trailer_bot_authors(trailer_lane), lane),
        "authors_env": _authors_env_for_lane(lane_id, lane),
        "github_actions_workflows": github_actions_workflows,
    }


def _identity_section(
    raw_identity: Mapping[str, Any],
    section: str,
    *,
    canonicalize_lanes: bool = False,
) -> dict[str, str]:
    raw_section = raw_identity.get(section)
    if not isinstance(raw_section, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw_section.items():
        key_text = str(key)
        lane = str(value)
        if not key_text or not lane:
            continue
        if canonicalize_lanes:
            lane = _canonical_builder_lane(lane)
        out[key_text] = lane
    return out


def _author_exclusion_payload(
    config: Mapping[str, Any],
    selected_lanes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    raw_identity = config.get("builder_identity")
    identity = raw_identity if isinstance(raw_identity, Mapping) else {}
    payload = {
        "enabled": bool(config.get("merge_authority_excludes_author", True)),
        "labels": _identity_section(identity, "labels", canonicalize_lanes=True),
        "authors": _identity_section(identity, "authors", canonicalize_lanes=True),
        "trailers": _identity_section(identity, "trailers", canonicalize_lanes=True),
    }
    for label, lane in tuple(payload["labels"].items()):
        if label in BUILDER_LEGACY_BUILDER_LABELS.get(lane, ()):
            payload["labels"].setdefault(f"builder:{lane}", lane)
    for lane_id, lane in selected_lanes.items():
        if lane.get("type") == "audit":
            raw_author_lane = _author_lane_name(lane_id, lane)
            author_lane = _canonical_builder_lane(raw_author_lane)
            payload["labels"].setdefault(f"builder:{author_lane}", author_lane)
            if raw_author_lane != author_lane:
                payload["labels"].setdefault(f"builder:{raw_author_lane}", author_lane)
    return payload


def _author_exclusion_json(
    config: Mapping[str, Any],
    selected_lanes: Mapping[str, Mapping[str, Any]],
) -> str:
    return json.dumps(
        _author_exclusion_payload(config, selected_lanes),
        separators=(",", ":"),
        sort_keys=True,
    )


def _audit_rearm_entries(
    selected_lanes: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    entries: list[dict[str, str]] = []
    for lane_id, lane in selected_lanes.items():
        if lane.get("type") != "audit" or not lane.get("merge_authority"):
            continue
        labels = _labels_for(lane)
        entries.append(
            {
                "id": lane_id,
                "author_lane": _author_lane_name(lane_id, lane),
                "needs": str(labels["needs"]),
                "done": str(labels["done"]),
                "blocked": str(labels["blocked"]),
            }
        )
    return tuple(entries)


def _builder_labels_by_lane(author_exclusion: Mapping[str, Any]) -> dict[str, str]:
    labels = _identity_section(author_exclusion, "labels")
    out: dict[str, str] = {}
    labels_by_lane: dict[str, list[str]] = {}
    for label, lane in sorted(labels.items()):
        if label.startswith("builder:"):
            labels_by_lane.setdefault(lane, []).append(label)
    for lane, lane_labels in sorted(labels_by_lane.items()):
        default_label = f"builder:{lane}"
        legacy_labels = set(BUILDER_LEGACY_BUILDER_LABELS.get(lane, ()))
        if legacy_labels and default_label in lane_labels:
            out[lane] = default_label
            continue
        custom_labels = [label for label in lane_labels if label not in legacy_labels]
        out[lane] = custom_labels[0] if custom_labels else default_label
    return out


def _builder_label_aliases(
    author_exclusion: Mapping[str, Any],
    builder_lane: str,
) -> tuple[str, ...]:
    labels = _identity_section(author_exclusion, "labels")
    primary_label = _builder_labels_by_lane(author_exclusion).get(
        builder_lane,
        f"builder:{builder_lane}",
    )
    aliases = [
        primary_label,
        *(
            label
            for label, lane in sorted(labels.items())
            if lane == builder_lane and label.startswith("builder:")
        ),
        *BUILDER_LEGACY_BUILDER_LABELS.get(builder_lane, ()),
    ]
    return tuple(dict.fromkeys(label for label in aliases if label))


def _builder_dispatch_label_aliases(builder_lane: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                f"dispatched:{builder_lane}",
                *BUILDER_LEGACY_DISPATCH_LABELS.get(builder_lane, ()),
            )
        )
    )


def _builder_identity_rule_warnings(
    author_exclusion: Mapping[str, Any],
    rules: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    labels = _identity_section(author_exclusion, "labels")
    labels_by_lane: dict[str, list[str]] = {}
    for label, lane in sorted(labels.items()):
        if label.startswith("builder:"):
            labels_by_lane.setdefault(lane, []).append(label)

    warnings: list[str] = []
    for lane, lane_labels in sorted(labels_by_lane.items()):
        primary_label = _builder_labels_by_lane(author_exclusion).get(lane)
        allowed_aliases = {
            label
            for label in (
                primary_label,
                *BUILDER_LEGACY_BUILDER_LABELS.get(lane, ()),
            )
            if label
        }
        unexpected_labels = [label for label in lane_labels if label not in allowed_aliases]
        if unexpected_labels:
            warnings.append(
                f"builder_identity: lane {lane!r} maps multiple builder labels; "
                f"generated automation uses {primary_label!r}"
            )
    for lane in sorted({str(rule.get("builder_lane") or "") for rule in rules}):
        if lane and lane not in labels_by_lane:
            warnings.append(
                f"builder_identity: lane {lane!r} has no matching builder label; "
                f"generated automation will use 'builder:{lane}'"
            )
    return tuple(warnings)


def _agent_pr_label_rules(
    config: Mapping[str, Any],
    author_exclusion: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    identity = config.get("builder_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    prefixes = _identity_section(identity, "branch_prefixes", canonicalize_lanes=True)
    labels_by_lane = _builder_labels_by_lane(author_exclusion)
    rules_by_lane: dict[str, dict[str, Any]] = {}
    for prefix, lane in sorted(prefixes.items()):
        label = labels_by_lane.get(lane) or f"builder:{lane}"
        rule = rules_by_lane.setdefault(
            lane,
            {
                "builder_lane": lane,
                "builder_label": label,
                "builder_labels": list(_builder_label_aliases(author_exclusion, lane)),
                "branch_prefixes": [],
            },
        )
        rule["branch_prefixes"].append(prefix)
    return tuple(rules_by_lane[lane] for lane in sorted(rules_by_lane))


def _fix_round_rules(
    config: Mapping[str, Any],
    author_exclusion: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    identity = config.get("builder_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    mentions = _identity_section(identity, "fix_round_mentions")
    labels_by_lane = _builder_labels_by_lane(author_exclusion)
    rules_by_lane: dict[str, dict[str, Any]] = {}
    for raw_lane, mention in sorted(mentions.items()):
        lane = _canonical_builder_lane(raw_lane)
        label = labels_by_lane.get(lane) or f"builder:{lane}"
        rules_by_lane[lane] = (
            {
                "builder_lane": lane,
                "builder_label": label,
                "builder_labels": list(_builder_label_aliases(author_exclusion, lane)),
                "mention": mention,
            }
        )
    return tuple(rules_by_lane[lane] for lane in sorted(rules_by_lane))


def _gate_workflow_entry(
    config: Mapping[str, Any],
    selected_lanes: Mapping[str, Mapping[str, Any]],
    *,
    author_exclusion_json: str,
    owner_label: str = DEFAULT_OWNER_LABEL,
    owner_sitting_label: str = "owner-sitting",
    owner_login: str = "",
    gate_override_label: str = "gate:override",
    decision_authorities: str = "",
) -> dict[str, str]:
    gate_lanes = [
        _gate_lane_entry(lane_id, lane)
        for lane_id, lane in selected_lanes.items()
        if lane.get("merge_authority")
    ]
    return {
        "path": GATE_WORKFLOW_PATH,
        "source": "code-mower-gate-workflow-template",
        "copy_from": GATE_WORKFLOW_TEMPLATE,
        "package_copy_from": GATE_WORKFLOW_TEMPLATE,
        "gate_lanes_json": json.dumps(gate_lanes, separators=(",", ":"), sort_keys=True),
        "gate_author_env_assignments": _var_env_assignments(
            [lane.get("authors_env", "") for lane in gate_lanes]
        ),
        "owner_label": owner_label,
        "owner_sitting_label": owner_sitting_label,
        "owner_login": owner_login,
        "gate_override_label": gate_override_label,
        "decision_authorities": decision_authorities,
        "author_exclusion_json": author_exclusion_json,
    }


def _running_as_package() -> bool:
    return bool(__package__ and __package__ != "tools")


def _default_package_command() -> str:
    command = sys.argv[0] or "code-mower"
    name = Path(command).name
    if name.endswith(".py"):
        return f"{shlex.quote(sys.executable)} -m code_mower.cli"
    python_suffix = name.removeprefix("python")
    is_python_launcher = name == "python" or (
        name.startswith("python")
        and bool(python_suffix)
        and python_suffix.replace(".", "").isdigit()
    )
    if is_python_launcher or name in {
        "",
        "-c",
        "__main__.py",
        "pytest",
        "py.test",
    }:
        command = "code-mower"
    return shlex.quote(command)


def _resolve_config_path(config_arg: str) -> Path:
    path = Path(config_arg)
    if path.is_file() or config_arg != "code-mower.example.yml":
        return path

    script_path = Path(__file__).resolve()
    candidates = []
    if _running_as_package():
        candidates.append(script_path.parent / "templates" / "code-mower.example.yml")
    else:
        candidates.append(script_path.parents[1] / "code-mower.example.yml")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return path


def _init_config_error_message(exc: Exception, *, config_arg: str) -> str:
    requested = str(config_arg)
    lines = [
        f"error: {exc}",
        f"Init loaded config {requested!r} from cwd {Path.cwd()}.",
    ]
    if requested == "code-mower.example.yml":
        lines.append(
            "For a fresh repo, run `code-mower init --easy` from the repository "
            "checkout to use the packaged starter config."
        )
    else:
        lines.append(
            "For an existing repo, run from the checkout that contains "
            "code-mower.yml or pass its path explicitly."
        )
    return "\n".join(lines)


def _lane_smoke_tests(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    package_mode: bool,
) -> tuple[str, ...]:
    driver = lane.get("driver")
    if package_mode and driver in {"local_cli", "hosted_bridge", "api_model", "manual", "saas_event"}:
        return ()
    if driver in {"local_cli", "hosted_bridge", "api_model"}:
        trailer_lane = json.dumps(_trailer_lane_name(lane_id, lane))
        code = f"from tools.lane_configs import load_lane_config; load_lane_config({trailer_lane})"
        return (
            f"{REFERENCE_PYTHON} -c {shlex.quote(code)}",
        )
    if driver == "saas_event":
        adapter = json.dumps(str(lane.get("adapter")))
        code = f"from tools.adapters import load_adapter; load_adapter({adapter})"
        return (
            f"{REFERENCE_PYTHON} -c {shlex.quote(code)}",
        )
    if driver == "manual":
        return ("bash -n tools/post_review.sh",)
    return ()


def _lane_warnings(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    package_mode: bool,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if lane.get("spend_policy") == "paid":
        warnings.append(f"{lane_id}: paid lane; init dry-run will not trigger a review")
    if lane.get("enabled_by_default") is False:
        warnings.append(f"{lane_id}: opt-in lane selected by profile")
    if lane.get("trigger_policy") == "manual":
        warnings.append(f"{lane_id}: manual trigger policy; installer must not auto-dispatch")
    if "GITHUB_TOKEN" in _token_env_names(lane) and "github-actions[bot]" not in {
        author.strip()
        for author in _default_bot_authors_for_lane(lane_id, lane).split(",")
        if author.strip()
    }:
        warnings.append(
            f"{lane_id}: GITHUB_TOKEN posting comments as github-actions[bot] requires "
            "an explicit *_BOT_AUTHORS repository variable"
        )
    if package_mode:
        driver = lane.get("driver")
        if driver in {"local_cli", "hosted_bridge", "api_model"}:
            warnings.append(
                f"{lane_id}: lane-config smoke deferred until package-relative lane configs are extracted"
            )
        elif driver == "saas_event":
            warnings.append(
                f"{lane_id}: adapter smoke deferred until package-relative adapters are extracted"
            )
        elif driver == "manual":
            warnings.append(
                f"{lane_id}: manual review script smoke deferred until repo-local review scripts are installed"
            )
    return tuple(warnings)


def _repo_root() -> Path:
    if __package__ and __package__ != "tools":
        return Path.cwd()
    return Path(__file__).resolve().parents[1]


def default_auth_config_dir(home_dir: Path | None = None) -> Path:
    return (home_dir or Path.home()) / ".config" / "code-mower"


def default_gemini_auth_path(home_dir: Path | None = None) -> Path:
    return default_auth_config_dir(home_dir) / "gemini.env"


def _shell_export_line(name: str, value: str) -> str:
    return f"export {name}={shlex.quote(value)}"


def _parse_gemini_secret_source(text: str) -> str:
    return code_mower_secrets.parse_secret_file_text(
        text,
        supported_env_names=set(GEMINI_AUTH_ENV_NAMES),
    ).value


def write_gemini_auth_file(
    secret_value: str,
    path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    value = _parse_gemini_secret_source(secret_value)
    if not value:
        raise ConfigError("Gemini key source was empty or not a supported Gemini key assignment")
    destination = (path or default_gemini_auth_path()).expanduser()
    if destination.is_symlink():
        raise ConfigError(f"{destination} is a symlink; refusing to write secrets through it")
    if destination.exists() and not destination.is_file():
        raise ConfigError(f"{destination} is not a regular file; refusing to write secrets")
    if destination.exists() and not force:
        raise ConfigError(f"{destination} already exists; pass --force to overwrite it")
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path is None or not parent_existed:
        destination.parent.chmod(0o700)
    if destination.exists():
        destination.chmod(0o600)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise ConfigError(f"{destination} already exists; pass --force to overwrite it") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value + "\n")
    finally:
        if fd != -1:
            os.close(fd)
    destination.chmod(0o600)
    return {
        "mode": "auth",
        "provider": "gemini",
        "path": str(destination),
        "file_env": GEMINI_AUTH_FILE_ENV,
        "shell_export": _shell_export_line(GEMINI_AUTH_FILE_ENV, str(destination)),
    }


def _render_gemini_auth_instructions(path: Path | None = None) -> str:
    destination = (path or default_gemini_auth_path()).expanduser()
    return "\n".join(
        [
            "Code Mower Gemini auth setup",
            f"Credential file: {destination}",
            "",
            "Write the key without putting it in shell history:",
            f"  printf '%s\\n' \"$GEMINI_API_KEY\" | code-mower init auth gemini --from-stdin --path {shlex.quote(str(destination))}",
            "",
            "Then make the file discoverable:",
            f"  {_shell_export_line(GEMINI_AUTH_FILE_ENV, str(destination))}",
            "",
        ]
    )


def _auth_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="code-mower init auth")
    subparsers = parser.add_subparsers(dest="provider", required=True)
    gemini = subparsers.add_parser("gemini")
    gemini.add_argument(
        "--from-stdin",
        action="store_true",
        help="read the Gemini key or GEMINI_API_KEY assignment from stdin",
    )
    gemini.add_argument(
        "--from-env",
        nargs="?",
        const="GEMINI_API_KEY",
        help="read the key from an environment variable, defaulting to GEMINI_API_KEY",
    )
    gemini.add_argument(
        "--path",
        type=Path,
        default=None,
        help="credential file path, defaulting to ~/.config/code-mower/gemini.env",
    )
    gemini.add_argument("--force", action="store_true", help="overwrite an existing file")
    gemini.add_argument("--print-shell", action="store_true", help="print shell export setup")
    gemini.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.provider != "gemini":  # pragma: no cover - argparse guards this.
        raise AssertionError(f"unhandled auth provider: {args.provider}")
    if args.from_stdin and args.from_env:
        print("error: choose either --from-stdin or --from-env", file=sys.stderr)
        return 1
    if not args.from_stdin and not args.from_env:
        text = _render_gemini_auth_instructions(args.path)
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": "auth",
                        "provider": "gemini",
                        "path": str((args.path or default_gemini_auth_path()).expanduser()),
                        "file_env": GEMINI_AUTH_FILE_ENV,
                        "created": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(text, end="")
        return 0

    if args.from_stdin:
        source_text = sys.stdin.read()
    else:
        env_name = str(args.from_env)
        if env_name not in GEMINI_AUTH_ENV_NAMES:
            print(
                "error: --from-env must be GEMINI_API_KEY or GOOGLE_API_KEY",
                file=sys.stderr,
            )
            return 1
        source_text = os.environ.get(env_name, "")
        if not source_text:
            print(f"error: {env_name} is not set", file=sys.stderr)
            return 1

    try:
        result = write_gemini_auth_file(source_text, args.path, force=args.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({**result, "created": True}, indent=2, sort_keys=True))
    else:
        print(f"Code Mower Gemini auth file written: {result['path']}")
        if args.print_shell:
            print(result["shell_export"])
        else:
            print(f"Set {GEMINI_AUTH_FILE_ENV} for future shells:")
            print(f"  {result['shell_export']}")
    return 0


def _safe_output_path(output_dir: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ConfigError(f"unsafe generated path: {relative_path}")
    destination = output_dir.joinpath(path)
    try:
        destination.resolve().relative_to(output_dir.resolve())
    except ValueError as exc:
        raise ConfigError(f"generated path escapes output directory: {relative_path}") from exc
    return destination


def _placeholder_text(path: str, source: str) -> str:
    return (
        f"# Code Mower generated placeholder for {path}\n"
        f"# Source template: {source}\n"
        "# This reference apply mode writes placeholders only when the source\n"
        "# file is not present in the checkout. The standalone package will\n"
        "# render this file from bundled templates.\n"
    )


def _render_stale_workflow_template(text: str, *, lane: str) -> str:
    """Render the shared stale-label workflow without requiring Jinja at runtime."""

    display_name = _display_name(lane)
    return (
        text.replace("{% raw %}", "")
        .replace("{% endraw %}", "")
        .replace(
            "name: Code Mower Clear Stale Audits",
            f"name: Code Mower {display_name} Clear Stale Audits",
        )
        .replace('default: "devin"', f'default: "{lane}"')
        .replace("github.event.inputs.lane || 'devin'", f"github.event.inputs.lane || '{lane}'")
        .replace("code-mower-clear-stale-devin-", f"code-mower-clear-stale-{lane}-")
    )


def _render_workflow_template(text: str, entry: Mapping[str, Any]) -> str:
    """Render lightweight workflow placeholders without requiring Jinja."""

    rendered = text.replace("{% raw %}", "").replace("{% endraw %}", "")
    gate_override_label = entry.get("gate_override_label", "gate:override")
    if gate_override_label is None:
        gate_override_label = "gate:override"
    replacements = {
        "__ADAPTER__": str(entry.get("adapter") or ""),
        "__AUTHOR_EXCLUSION_JSON__": _yaml_scalar(
            entry.get("author_exclusion_json") or '{"enabled":false}'
        ),
        "__AUTHORS_ENV__": str(entry.get("authors_env") or ""),
        "__BLOCKED_LABEL__": str(entry.get("blocked_label") or ""),
        "__BOT_AUTHORS__": str(entry.get("bot_authors") or ""),
        "__BUILDER_DISPATCH_CRON__": _yaml_scalar(
            entry.get("builder_dispatch_cron") or ""
        ),
        "__BUILDER_DISPATCH_LANES_JSON__": _yaml_scalar(
            entry.get("builder_dispatch_lanes_json") or "[]"
        ),
        "__BUILDER_LANE_ROWS__": str(entry.get("builder_lane_rows") or ""),
        "__BUILDER_LABEL__": str(entry.get("builder_label") or ""),
        "__BUILD_LOOP_MAX_WIP__": str(entry.get("build_loop_max_wip") or "5"),
        "__BUILD_LOOP_OWNER_LABELS_JSON__": _yaml_scalar(
            entry.get("build_loop_owner_labels_json") or "[]"
        ),
        "__BUILD_LOOP_READY_LABEL_YAML__": _yaml_scalar(
            entry.get("build_loop_ready_label") or ""
        ),
        "__BUILD_LOOP_READY_LABEL__": str(entry.get("build_loop_ready_label") or ""),
        "__BUILD_LOOP_TRUSTED_AUTHORS_JSON__": _yaml_scalar(
            entry.get("build_loop_trusted_authors_json") or "[]"
        ),
        "__DISPLAY_NAME__": str(entry.get("display_name") or ""),
        "__DONE_LABEL__": str(entry.get("done_label") or ""),
        "__AGENT_PR_RULES_JSON__": _yaml_scalar(
            entry.get("agent_pr_rules_json") or "[]"
        ),
        "__AGENT_PR_AUDIT_LANES_JSON__": _yaml_scalar(
            entry.get("agent_pr_audit_lanes_json") or "[]"
        ),
        "__DISPATCH_TOKEN_ENV__": str(entry.get("dispatch_token_env") or "DISPATCH_TOKEN"),
        "__DISPATCH_TOKEN_EXPIRES_VAR__": str(
            entry.get("dispatch_token_expires_var") or "DISPATCH_TOKEN_EXPIRES_AT"
        ),
        "__FIX_ROUND_RULES_JSON__": _yaml_scalar(
            entry.get("fix_round_rules_json") or "[]"
        ),
        "__FIX_ROUND_AUDIT_LANES_JSON__": _yaml_scalar(
            entry.get("fix_round_audit_lanes_json") or "[]"
        ),
        "__GATE_HEALTH_CRON__": _yaml_scalar(entry.get("gate_health_cron") or ""),
        "__GATE_HEALTH_LANES_JSON__": _yaml_scalar(
            entry.get("gate_health_lanes_json") or "[]"
        ),
        "__GATE_HEALTH_AUTHOR_ENV_ASSIGNMENTS__": str(
            entry.get("gate_health_author_env_assignments") or ""
        ),
        "__GATE_HEALTH_MAX_WAIT_MINUTES__": str(
            entry.get("gate_health_max_wait_minutes") or "30"
        ),
        "__GATE_HEALTH_LIVENESS_MINUTES__": str(
            entry.get("gate_health_liveness_minutes") or "45"
        ),
        "__GATE_AUTHOR_ENV_ASSIGNMENTS__": str(
            entry.get("gate_author_env_assignments") or ""
        ),
        "__GATE_LANES_JSON__": _yaml_scalar(entry.get("gate_lanes_json") or "[]"),
        "__GATE_OVERRIDE_LABEL_JSON__": _yaml_scalar(str(gate_override_label)),
        "__DECISION_AUTHORITIES__": _yaml_scalar(
            str(entry.get("decision_authorities") or "")
        ),
        "__GITHUB_ACTIONS_WORKFLOWS__": str(entry.get("github_actions_workflows") or ""),
        "__LABEL_TOKEN_ENV__": str(entry.get("label_token_env") or ""),
        "__LANE_ID__": str(entry.get("lane_id") or ""),
        "__LOCAL_AUDIT_LABEL_CONTAINS__": str(entry.get("local_audit_label_contains") or ""),
        "__LOCAL_AUDIT_LABEL_MATCH__": str(entry.get("local_audit_label_match") or ""),
        "__LOCAL_AUDIT_LANES_JSON__": str(entry.get("local_audit_lanes_json") or "[]"),
        "__LOCAL_AUDIT_RUNNER_LABEL__": str(entry.get("local_audit_runner_label") or ""),
        "__LOCAL_AUDIT_RUNNER_LABEL_YAML__": _yaml_scalar(
            entry.get("local_audit_runner_label") or ""
        ),
        "__LOCAL_AUDIT_RUNNER_ENABLED_VAR__": str(
            entry.get("local_audit_runner_enabled_var")
            or "CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED"
        ),
        "__LOCAL_AUDIT_TOKEN_ENV_ASSIGNMENTS__": str(
            entry.get("local_audit_token_env_assignments") or ""
        ),
        "__LANE_DISPLAY_NAME__": str(entry.get("lane_display_name") or ""),
        "__NEEDS_LABEL__": str(entry.get("needs_label") or ""),
        "__NEEDS_LABEL_YAML__": _yaml_scalar(entry.get("needs_label") or ""),
        "__NEEDS_OWNER_LABEL__": str(entry.get("needs_owner_label") or ""),
        "__NEEDS_OWNER_LABEL_YAML__": _yaml_scalar(
            entry.get("needs_owner_label") or ""
        ),
        "__LANE_MAC_RUNNER_ALLOWED_CASE__": str(
            entry.get("lane_mac_runner_allowed_case") or ""
        ),
        "__LANE_MAC_RUNNER_BUILDER_LABELS_JSON__": str(
            _shell_literal(entry.get("lane_mac_runner_builder_labels_json") or "{}")
        ),
        "__LANE_MAC_RUNNER_BRANCH_PREFIXES_JSON__": str(
            _shell_literal(entry.get("lane_mac_runner_branch_prefixes_json") or "{}")
        ),
        "__LANE_MAC_RUNNER_AUDIT_LABELS_JSON__": str(
            _shell_literal(entry.get("lane_mac_runner_audit_labels_json") or "{}")
        ),
        "__LANE_MAC_RUNNER_BLOCKED_LABELS_JQ__": str(
            _shell_literal(entry.get("lane_mac_runner_blocked_labels_jq") or "false")
        ),
        "__LANE_MAC_RUNNER_CRON__": _yaml_scalar(
            entry.get("lane_mac_runner_cron") or ""
        ),
        "__LANE_MAC_RUNNER_ENABLED_VAR__": str(
            entry.get("lane_mac_runner_enabled_var") or ""
        ),
        "__LANE_MAC_RUNNER_LABELS__": str(entry.get("lane_mac_runner_labels") or ""),
        "__LANE_MAC_RUNNER_LANES_DISPLAY__": str(
            entry.get("lane_mac_runner_lanes_display") or ""
        ),
        "__LANE_MAC_RUNNER_LANES_YAML__": str(
            entry.get("lane_mac_runner_lanes_yaml") or ""
        ),
        "__LANE_MAC_RUNNER_MAX_MINUTES__": str(
            entry.get("lane_mac_runner_max_minutes") or "90"
        ),
        "__LANE_MAC_RUNNER_TIMEOUT_MINUTES__": str(
            entry.get("lane_mac_runner_timeout_minutes") or "105"
        ),
        "__LANE_MAC_RUNNER_OWNER_LABELS_JSON__": str(
            _shell_literal(entry.get("lane_mac_runner_owner_labels_json") or "[]")
        ),
        "__LANE_MAC_RUNNER_TRUSTED_AUTHORS__": str(
            _shell_literal(entry.get("lane_mac_runner_trusted_authors") or "")
        ),
        "__BUILD_LOOP_READY_LABEL_SH__": str(
            _shell_literal(entry.get("build_loop_ready_label") or "")
        ),
        "__NEEDS_OWNER_LABEL_SH__": str(
            _shell_literal(entry.get("needs_owner_label") or "")
        ),
        "__LANE_NAME__": str(entry.get("lane_name") or ""),
        "__OWNER_DECISION_LABEL__": str(entry.get("owner_decision_label") or ""),
        "__OWNER_DECISION_LABEL_YAML__": _yaml_scalar(
            entry.get("owner_decision_label") or ""
        ),
        "__OWNER_SITTING_LABEL__": str(entry.get("owner_sitting_label") or ""),
        "__OWNER_SITTING_LABEL_YAML__": _yaml_scalar(
            entry.get("owner_sitting_label") or ""
        ),
        "__OWNER_LOGIN__": str(entry.get("owner_login") or ""),
        "__OWNER_LOGIN_YAML__": _yaml_scalar(entry.get("owner_login") or ""),
        "__OWNER_BLOCKING_LABELS__": str(
            entry.get("owner_blocking_labels")
            or "`needs-owner`, `owner-decision`, `owner-sitting`"
        ),
        "__GATE_OVERRIDE_LABEL__": str(entry.get("gate_override_label") or ""),
        "__GATE_OVERRIDE_LABEL_YAML__": _yaml_scalar(
            entry.get("gate_override_label") or ""
        ),
        "__PHASE_LABELS__": _yaml_scalar(entry.get("phase_labels") or ""),
        "__READY_LABEL__": _yaml_scalar(entry.get("ready_label") or ""),
        "__REQUIRED_AUDIT_LABELS__": str(entry.get("required_audit_labels") or ""),
        "__REVIEWER_SPEND_PATH__": _yaml_scalar(entry.get("reviewer_spend_path") or ""),
        "__REVIEWER_VALUE_REPORT_PATH__": _yaml_scalar(
            entry.get("reviewer_value_report_path") or ""
        ),
        "__STATUS_ISSUE__": _yaml_scalar(entry.get("status_issue") or ""),
        "__OWNER_LABEL__": _yaml_scalar(
            str(entry.get("owner_label") or DEFAULT_OWNER_LABEL)
        ),
        "__OWNER_SITTING_LABEL_JSON__": _yaml_scalar(
            str(entry.get("owner_sitting_label") or "owner-sitting")
        ),
        "__OWNER_LOGIN_JSON__": _yaml_scalar(str(entry.get("owner_login") or "")),
        "__TRAILER_LANE__": str(entry.get("trailer_lane") or ""),
        "__TRAILER_PREFIX__": str(entry.get("trailer_prefix") or ""),
        "__WEEKLY_STATUS_CRON__": _yaml_scalar(entry.get("weekly_status_cron") or ""),
        "__WORKFLOW_NAME__": str(entry.get("workflow_name") or "Code Mower labeler"),
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _workflow_template_needs_render(source: str) -> bool:
    return source in {
        "agent-pr-labeler-workflow-template",
        "builder-dispatch-workflow-template",
        "shared-cleanup-template",
        "code-mower-gate-health-workflow-template",
        "code-mower-gate-workflow-template",
        "fix-round-dispatch-workflow-template",
        "hosted-bridge-workflow-template",
        "lane-mac-runner-script-template",
        "lane-mac-runner-workflow-template",
        "lane-standing-instructions-template",
        "lane-standing-readme-template",
        "owner-notify-workflow-template",
        "self-hosted-local-audit-workflow-template",
        "saas-reviewer-labeler-workflow-template",
        "trailer-comment-labeler-workflow-template",
        "weekly-status-workflow-template",
    }


def _copy_source_candidates(source_root: Path, entry: Mapping[str, Any], path: str) -> tuple[Path, ...]:
    candidates: list[Path] = []
    package_copy_from = entry.get("package_copy_from")
    if entry.get("package_copy_first") and package_copy_from:
        candidates.append(Path(__file__).resolve().parent / str(package_copy_from))
    copy_from_path = entry.get("copy_from_path")
    if copy_from_path:
        candidates.append(Path(str(copy_from_path)).expanduser())
    if not copy_from_path or "copy_from" in entry:
        copy_from = str(entry.get("copy_from", path))
        candidates.append(source_root / copy_from)
    if package_copy_from and not entry.get("package_copy_first"):
        candidates.append(Path(__file__).resolve().parent / str(package_copy_from))
    return tuple(candidates)


def _materialize_generated_file(
    entry: Mapping[str, Any],
    path: str,
    destination: Path,
    *,
    source_root: Path,
) -> MaterializedGeneratedFile:
    source = next(
        (candidate for candidate in _copy_source_candidates(source_root, entry, path) if candidate.is_file()),
        None,
    )
    if source is None:
        return MaterializedGeneratedFile(
            entry=entry,
            path=path,
            destination=destination,
            text=_placeholder_text(path, str(entry["source"])),
            placeholder=True,
        )
    if entry.get("source") == "shared-stale-label-template":
        text = _render_stale_workflow_template(
            source.read_text(encoding="utf-8"),
            lane=str(entry.get("stale_lane") or "devin"),
        )
    elif _workflow_template_needs_render(str(entry.get("source"))):
        text = _render_workflow_template(source.read_text(encoding="utf-8"), entry)
    else:
        text = source.read_text(encoding="utf-8")
    return MaterializedGeneratedFile(
        entry=entry,
        path=path,
        destination=destination,
        text=text,
        placeholder=False,
    )


def _generated_workflows_for_actionlint(
    files: Sequence[MaterializedGeneratedFile],
) -> tuple[GeneratedWorkflow, ...]:
    return tuple(
        GeneratedWorkflow(path=item.path, text=item.text)
        for item in files
        if is_github_workflow_path(item.path)
    )


def _skipped_actionlint_result(
    workflows: Sequence[GeneratedWorkflow],
    *,
    actionlint_bin: str,
    reason: str,
) -> dict[str, object]:
    workflow_items = tuple(workflows)
    return {
        "status": "skipped",
        "actionlint_bin": actionlint_bin,
        "reason": reason,
        "workflow_count": len(workflow_items),
        "workflows": [workflow.path for workflow in workflow_items],
        "custom_runner_labels": list(custom_self_hosted_runner_labels(workflow_items)),
    }


def _previous_apply_paths(output_dir: Path) -> list[Path]:
    manifest_path = output_dir / APPLY_MANIFEST_FILENAME
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    paths = [manifest_path, *(output_dir / filename for filename in APPLY_SUMMARY_FILES)]
    generated_files = manifest.get("generated_files", [])
    if isinstance(generated_files, list):
        for entry in generated_files:
            if not isinstance(entry, dict):
                continue
            try:
                paths.append(_safe_output_path(output_dir, str(entry.get("path", ""))))
            except ConfigError:
                continue
    return paths


def _prune_previous_apply(output_dir: Path) -> None:
    previous_paths = _previous_apply_paths(output_dir)
    for path in sorted(previous_paths, key=lambda item: len(item.parts), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()

    generated_parents: set[Path] = set()
    for path in previous_paths:
        parent = path.parent
        while parent != output_dir and output_dir in parent.parents:
            generated_parents.add(parent)
            parent = parent.parent

    for directory in sorted(generated_parents, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_init_plan(
    plan: RenderedPlan,
    output_dir: Path,
    *,
    source_root: Path | None = None,
    actionlint_bin: str | None = None,
) -> dict[str, Any]:
    source_root = (source_root or _repo_root()).resolve()
    resolved_output_dir = output_dir.resolve()
    if resolved_output_dir == source_root:
        raise ConfigError(
            "refusing to write generated output into the source root; "
            "choose a dedicated --output-dir such as .code-mower.generated"
        )

    generated_destinations: list[tuple[dict[str, Any], str, Path]] = []
    for entry in plan.data["generated_files"]:
        path = str(entry["path"])
        generated_destinations.append((entry, path, _safe_output_path(output_dir, path)))

    materialized = [
        _materialize_generated_file(
            entry,
            path,
            destination,
            source_root=source_root,
        )
        for entry, path, destination in generated_destinations
    ]
    actionlint_result: Mapping[str, object] | None = None
    if actionlint_bin:
        workflows_for_lint = _generated_workflows_for_actionlint(materialized)
        try:
            lint_result = run_actionlint_on_workflows(
                workflows_for_lint,
                actionlint_bin=actionlint_bin,
            )
        except WorkflowLintUnavailable as exc:
            actionlint_result = _skipped_actionlint_result(
                workflows_for_lint,
                actionlint_bin=actionlint_bin,
                reason=str(exc),
            )
        except WorkflowLintError as exc:
            raise ConfigError(str(exc)) from exc
        else:
            actionlint_result = {"status": "passed", **lint_result.as_dict()}

    output_dir.mkdir(parents=True, exist_ok=True)
    _prune_previous_apply(output_dir)
    written_files: list[str] = []
    placeholder_files: list[str] = []

    manifest_path = output_dir / APPLY_MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(plan.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written_files.append(str(manifest_path))

    summary_files = {
        "labels.txt": "\n".join(plan.data["labels"]) + "\n",
        "required-secrets.txt": "\n".join(plan.data["required_secrets"]) + "\n",
        "required-variables.txt": "\n".join(plan.data["required_variables"]) + "\n",
        "smoke-tests.sh": "#!/usr/bin/env bash\nset -euo pipefail\n\n"
        + "\n".join(plan.data["smoke_tests"])
        + "\n",
    }
    for filename, text in summary_files.items():
        destination = output_dir / filename
        destination.write_text(text, encoding="utf-8")
        if filename.endswith(".sh"):
            destination.chmod(0o755)
        written_files.append(str(destination))

    for item in materialized:
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        item.destination.write_text(item.text, encoding="utf-8")
        if item.placeholder:
            placeholder_files.append(str(item.destination))
        if item.entry.get("mode") == "0755":
            item.destination.chmod(0o755)
        written_files.append(str(item.destination))

    result: dict[str, Any] = {
        "mode": "apply",
        "output_dir": str(output_dir),
        "written_files": written_files,
        "placeholder_files": placeholder_files,
    }
    if actionlint_result is not None:
        result["actionlint"] = actionlint_result
    return result


def ensure_github_labels(
    labels: Sequence[str],
    *,
    repo: str | None = None,
    gh_bin: str = "gh",
    color: str = "ededed",
) -> dict[str, Any]:
    unique_labels = sorted({label for label in labels if label})
    if not unique_labels:
        return {
            "status": "skipped",
            "reason": "no labels requested",
            "repo": repo or "",
            "requested": [],
            "created": [],
        }
    if shutil.which(gh_bin) is None and not Path(gh_bin).expanduser().is_file():
        return {
            "status": "skipped",
            "reason": f"{gh_bin} executable not found",
            "repo": repo or "",
            "requested": unique_labels,
            "created": [],
        }

    repo_args = ["--repo", repo] if repo else []
    try:
        existing_result = subprocess.run(
            [
                gh_bin,
                "label",
                "list",
                *repo_args,
                "--limit",
                "1000",
                "--json",
                "name",
                "-q",
                ".[].name",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return {
            "status": "failed",
            "reason": (exc.stderr or exc.stdout or str(exc)).strip(),
            "repo": repo or "",
            "requested": unique_labels,
            "created": [],
        }

    existing = {line.strip() for line in existing_result.stdout.splitlines() if line.strip()}
    created: list[str] = []
    failed: list[dict[str, str]] = []
    for label in unique_labels:
        if label in existing:
            continue
        try:
            subprocess.run(
                [
                    gh_bin,
                    "label",
                    "create",
                    label,
                    *repo_args,
                    "--color",
                    color,
                    "--description",
                    "Code Mower generated label",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            failed.append(
                {
                    "label": label,
                    "reason": (exc.stderr or exc.stdout or str(exc)).strip(),
                }
            )
            continue
        created.append(label)

    return {
        "status": "failed" if failed else "passed",
        "repo": repo or "",
        "requested": unique_labels,
        "created": created,
        "existing": sorted(existing.intersection(unique_labels)),
        "failed": failed,
    }


PACKAGED_STARTER_CONFIG_NAME = "code-mower.example.yml"


def config_source_kind(config_path: str) -> str:
    """Basename heuristic for direct plan renders.

    `main` instead classifies from resolution context (packaged fallback
    versus a file the user supplied) and passes the explicit result via
    `render_init_plan(source_kind=...)`, so an explicitly supplied local
    file named like the starter is never misclassified.
    """

    if Path(config_path).name == PACKAGED_STARTER_CONFIG_NAME:
        return "packaged_starter"
    return "explicit_repository_config"


def root_adoption_config_present(repo_root: str | Path | None = None) -> bool:
    """Check whether a root code-mower.yml exists without changing config selection."""

    root = Path(repo_root) if repo_root is not None else Path.cwd()
    try:
        return (root / ADOPTION_CONFIG_PATH).is_file()
    except OSError:
        return False


def setup_drift_next_step(*, profile_id: str) -> str:
    """Exact next step when a root config exists but the packaged starter was selected."""

    quoted_profile = shlex.quote(profile_id)
    return (
        "root code-mower.yml exists but the packaged starter config was selected; "
        f"rerun with `code-mower init code-mower.yml --profile {quoted_profile} --dry-run` "
        "to use the explicit repository config, or compare with "
        "`code-mower migration setup-drift --repo-path .` "
        "(see docs/upgrade-existing-repo.md)"
    )


def render_init_plan(
    config: Mapping[str, Any],
    profile_id: str = "recommended",
    config_path: str = "code-mower.example.yml",
    *,
    package_mode: bool | None = None,
    package_command: str | None = None,
    add_repositories: tuple[str, ...] = (),
    builders: tuple[str, ...] = (),
    repo_root: str | Path | None = None,
    source_kind: str | None = None,
) -> RenderedPlan:
    issues = validate_config(config)
    if issues:
        raise ConfigError(f"invalid Code Mower config:\n{_format_issues(issues)}")

    profile = _profile(config, profile_id)
    lanes: Mapping[str, Mapping[str, Any]] = config["lanes"]
    selected_lanes = {lane_id: lanes[lane_id] for lane_id in profile.lanes}

    labels: list[str] = []
    workflows: list[dict[str, str]] = []
    generated_files: list[dict[str, Any]] = []
    workflow_targets: set[str] = set()
    generated_paths: set[str] = set()
    required_secrets: set[str] = set()
    required_variables: set[str] = set()
    quoted_config_path = shlex.quote(config_path)
    quoted_profile_id = shlex.quote(profile.profile_id)
    if package_mode is None:
        package_mode = _running_as_package()
    smoke_command_prefix = (
        shlex.quote(package_command)
        if package_mode and package_command
        else _default_package_command()
        if package_mode
        else f"{REFERENCE_PYTHON} tools/code_mower"
    )
    add_repo_args = " ".join(
        f"--add-repo {shlex.quote(slug)}" for slug in add_repositories
    )
    add_repo_suffix = f" {add_repo_args}" if add_repo_args else ""
    smoke_tests: list[str] = [
        (
            f"{smoke_command_prefix} config validate {quoted_config_path}"
            if package_mode
            else f"{smoke_command_prefix}_config.py {quoted_config_path} --validate-only"
        ),
        (
            f"{smoke_command_prefix} init {quoted_config_path} --profile "
            f"{quoted_profile_id}{add_repo_suffix} --dry-run --json"
            if package_mode
            else f"{smoke_command_prefix}_init.py {quoted_config_path} --profile "
            f"{quoted_profile_id}{add_repo_suffix} --dry-run --json"
        ),
    ]
    warnings: list[str] = []
    merge_authority_lanes: list[str] = []
    informational_lanes: list[str] = []
    owner_surface = _owner_surface_config(config)
    decision_authority_list = code_mower_decisions.decision_authorities_from_config(config)
    decision_authorities = ",".join(decision_authority_list)
    author_exclusion_json = _author_exclusion_json(config, selected_lanes)
    author_exclusion = json.loads(author_exclusion_json)
    audit_rearm_entries = _audit_rearm_entries(selected_lanes)
    agent_pr_rules = _agent_pr_label_rules(config, author_exclusion)
    fix_round_rules = _fix_round_rules(config, author_exclusion)
    builder_entries = _builder_dispatch_lane_entries(
        builders,
        audit_rearm_entries,
        author_exclusion,
        fix_round_rules,
    )
    warnings.extend(
        _builder_identity_rule_warnings(
            author_exclusion,
            [*agent_pr_rules, *fix_round_rules],
        )
    )

    if ADOPTION_CONFIG_PATH not in generated_paths:
        generated_paths.add(ADOPTION_CONFIG_PATH)
        adoption_config_entry: dict[str, Any] = {
            "path": ADOPTION_CONFIG_PATH,
            "source": "editable-adoption-config",
            "copy_from_path": str(Path(config_path).expanduser().resolve()),
        }
        if Path(config_path).name == "code-mower.example.yml":
            adoption_config_entry["package_copy_from"] = "templates/code-mower.example.yml"
        generated_files.append(adoption_config_entry)

    for lane_id, lane in selected_lanes.items():
        lane_labels = _labels_for(lane)
        labels.extend(str(lane_labels[key]) for key in ("needs", "done", "blocked"))
        if lane.get("merge_authority"):
            merge_authority_lanes.append(lane_id)
        if lane.get("informational"):
            informational_lanes.append(lane_id)
        required_secrets.update(required_secret_entries_for_lane(lane))
        for target in _workflow_targets(lane_id, lane):
            if target in workflow_targets:
                warnings.append(f"{lane_id}: workflow target {target} collides with another lane")
                continue
            workflow_targets.add(target)
            workflows.append(
                {
                    "lane": lane_id,
                    "driver": str(lane["driver"]),
                    "target": target,
                }
            )
            if target not in generated_paths:
                generated_paths.add(target)
                generated_files.append(
                    _workflow_entry_for_target(
                        lane_id,
                        lane,
                        target,
                        author_exclusion_json=author_exclusion_json,
                        decision_authorities=decision_authorities,
                    )
                )
        if lane.get("driver") in {"local_cli", "hosted_bridge", "api_model"}:
            trailer_lane = _trailer_lane_name(lane_id, lane)
            trailer_module = _lane_module_name(trailer_lane)
            path = f"tools/lane_configs/{trailer_module}.py"
            if path in generated_paths:
                warnings.append(f"{lane_id}: generated file {path} collides with another lane")
            else:
                generated_paths.add(path)
                generated_files.append(
                    {
                        "path": path,
                        "source": "lane-config-template",
                    }
                )
        smoke_tests.extend(_lane_smoke_tests(lane_id, lane, package_mode=package_mode))
        warnings.extend(_lane_warnings(lane_id, lane, package_mode=package_mode))

    for label in (
        owner_surface["needs_owner_label"],
        owner_surface["owner_decision_label"],
        owner_surface["owner_sitting_label"],
        owner_surface["gate_override_label"],
    ):
        if label:
            labels.append(label)
    for label in _builder_labels_by_lane(author_exclusion).values():
        labels.append(label)
    for rule in [*agent_pr_rules, *fix_round_rules]:
        label = str(rule.get("builder_label") or "")
        if label:
            labels.append(label)
    if builder_entries:
        labels.append(owner_surface["ready_label"])
        for entry in builder_entries:
            labels.append(str(entry["builder_label"]))
            labels.append(str(entry["dispatch_label"]))

    audit_settings = code_mower_audit_limits.audit_limits_from_config(config)
    local_audit_entries = _local_audit_entries(selected_lanes, audit_settings)
    human_token_required = human_automation_token_required(
        config,
        tuple(selected_lanes.items()),
    )
    if human_token_required:
        required_secrets.add(owner_surface["dispatch_token_env"])
        required_variables.add(owner_surface["dispatch_token_expires_var"])
    if builder_entries:
        required_secrets.add(owner_surface["dispatch_token_env"])
        required_variables.add(owner_surface["dispatch_token_expires_var"])
        if any(str(entry.get("mac_runner") or "") == "true" for entry in builder_entries):
            required_variables.add(owner_surface["lane_runner_enabled_var"])
    if local_audit_entries and LOCAL_AUDIT_WORKFLOW_PATH not in generated_paths:
        required_variables.add(owner_surface["local_audit_runner_enabled_var"])
        workflow_targets.add(LOCAL_AUDIT_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "local-cli-audits",
                "driver": "local_cli",
                "target": LOCAL_AUDIT_WORKFLOW_PATH,
            }
        )
        generated_paths.add(LOCAL_AUDIT_WORKFLOW_PATH)
        generated_files.append(
            _local_audit_workflow_entry(
                local_audit_entries,
                local_audit_runner_label=owner_surface["local_audit_runner_label"],
                local_audit_runner_enabled_var=owner_surface[
                    "local_audit_runner_enabled_var"
                ],
            )
        )

    if builder_entries and BUILDER_DISPATCH_WORKFLOW_PATH not in generated_paths:
        workflow_targets.add(BUILDER_DISPATCH_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "builder-dispatch",
                "driver": "builder_dispatch",
                "target": BUILDER_DISPATCH_WORKFLOW_PATH,
            }
        )
        generated_paths.add(BUILDER_DISPATCH_WORKFLOW_PATH)
        generated_files.append(
            _dispatch_lanes_workflow_entry(
                builder_entries,
                owner_surface,
                decision_authorities=decision_authority_list,
            )
        )

    mac_runner_entries = tuple(
        entry
        for entry in builder_entries
        if str(entry.get("mac_runner") or "") == "true"
    )
    if mac_runner_entries and LANE_MAC_RUNNER_WORKFLOW_PATH not in generated_paths:
        workflow_targets.add(LANE_MAC_RUNNER_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "lane-mac-runner",
                "driver": "lane_mac_runner",
                "target": LANE_MAC_RUNNER_WORKFLOW_PATH,
            }
        )
        generated_paths.add(LANE_MAC_RUNNER_WORKFLOW_PATH)
        generated_files.append(_lane_mac_runner_workflow_entry(builder_entries, owner_surface))

    if mac_runner_entries and LANE_MAC_RUNNER_SCRIPT_PATH not in generated_paths:
        generated_paths.add(LANE_MAC_RUNNER_SCRIPT_PATH)
        generated_files.append(
            _lane_mac_runner_script_entry(
                builder_entries,
                audit_rearm_entries,
                owner_surface,
                config=config,
                decision_authorities=decision_authority_list,
            )
        )

    if builder_entries and LANE_STANDING_README_PATH not in generated_paths:
        generated_paths.add(LANE_STANDING_README_PATH)
        generated_files.append(_lane_standing_readme_entry(builder_entries, owner_surface))
    for entry in builder_entries:
        target = str(entry["doc_target"])
        if target in generated_paths:
            warnings.append(f"lane standing instruction target {target} collides")
            continue
        generated_paths.add(target)
        generated_files.append(_lane_standing_file_entry(entry, owner_surface))

    if merge_authority_lanes and GATE_WORKFLOW_PATH not in generated_paths:
        workflow_targets.add(GATE_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "code-mower-gate",
                "driver": "gate",
                "target": GATE_WORKFLOW_PATH,
            }
        )
        generated_paths.add(GATE_WORKFLOW_PATH)
        generated_files.append(
            _gate_workflow_entry(
                config,
                selected_lanes,
                author_exclusion_json=author_exclusion_json,
                owner_label=owner_surface["needs_owner_label"],
                owner_sitting_label=owner_surface["owner_sitting_label"],
                owner_login=owner_surface["owner_login"],
                gate_override_label=owner_surface["gate_override_label"],
                decision_authorities=decision_authorities,
            )
        )

    has_audit_lanes = any(lane.get("type") == "audit" for lane in selected_lanes.values())
    if has_audit_lanes and GATE_HEALTH_WORKFLOW_PATH not in generated_paths:
        if local_audit_entries:
            required_secrets.add("CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN")
        workflow_targets.add(GATE_HEALTH_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "code-mower-gate-health",
                "driver": "gate_health",
                "target": GATE_HEALTH_WORKFLOW_PATH,
            }
        )
        generated_paths.add(GATE_HEALTH_WORKFLOW_PATH)
        generated_files.append(
            _gate_health_workflow_entry(
                selected_lanes,
                owner_surface,
                author_exclusion_json=author_exclusion_json,
                include_local_audit_runner=bool(local_audit_entries),
            )
        )

    if (
        agent_pr_rules
        and audit_rearm_entries
        and AGENT_PR_LABELER_WORKFLOW_PATH not in generated_paths
    ):
        required_secrets.add(owner_surface["dispatch_token_env"])
        workflow_targets.add(AGENT_PR_LABELER_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "code-mower-agent-pr-labeler",
                "driver": "agent_pr_labeler",
                "target": AGENT_PR_LABELER_WORKFLOW_PATH,
            }
        )
        generated_paths.add(AGENT_PR_LABELER_WORKFLOW_PATH)
        generated_files.append(
            _agent_pr_labeler_workflow_entry(
                agent_pr_rules,
                audit_rearm_entries,
                owner_surface,
            )
        )

    if (
        fix_round_rules
        and audit_rearm_entries
        and FIX_ROUND_DISPATCH_WORKFLOW_PATH not in generated_paths
    ):
        required_secrets.add(owner_surface["dispatch_token_env"])
        workflow_targets.add(FIX_ROUND_DISPATCH_WORKFLOW_PATH)
        workflows.append(
            {
                "lane": "code-mower-fix-round-dispatch",
                "driver": "fix_round_dispatch",
                "target": FIX_ROUND_DISPATCH_WORKFLOW_PATH,
            }
        )
        generated_paths.add(FIX_ROUND_DISPATCH_WORKFLOW_PATH)
        generated_files.append(
            _fix_round_dispatch_workflow_entry(
                fix_round_rules,
                audit_rearm_entries,
                owner_surface,
            )
        )

    cleanup_path = ".github/workflows/audit-label-cleanup.yml"
    if cleanup_path not in generated_paths:
        required_secrets.add("AUDIT_LABEL_CLEANUP_TOKEN")
        generated_paths.add(cleanup_path)
        generated_files.append(
            {
                "path": cleanup_path,
                "source": "shared-cleanup-template",
                "copy_from": "templates/workflows/audit-label-cleanup.yml.j2",
                "package_copy_from": "templates/workflows/audit-label-cleanup.yml.j2",
            }
        )
    for lane_id, lane in selected_lanes.items():
        hygiene = lane.get("review_hygiene")
        if not isinstance(hygiene, Mapping) or not hygiene:
            continue
        stale_path = str(hygiene["workflow"])
        if stale_path not in generated_paths:
            generated_paths.add(stale_path)
            generated_files.append(
                {
                    "path": stale_path,
                    "source": "shared-stale-label-template",
                    "copy_from": "templates/workflows/review-clear-stale.yml.j2",
                    "package_copy_from": "templates/workflows/review-clear-stale.yml.j2",
                    "stale_lane": _trailer_lane_name(lane_id, lane),
                }
            )
    for target, copy_from, source_name in OWNER_SURFACE_WORKFLOW_FILES:
        if target in generated_paths:
            warnings.append(f"owner surface workflow target {target} collides")
            continue
        generated_paths.add(target)
        generated_files.append(
            _owner_surface_workflow_entry(target, copy_from, source_name, owner_surface)
        )
    for target, copy_from, package_copy_from, source_name in STARTER_DATA_FILES:
        if target in generated_paths:
            warnings.append(f"starter data target {target} collides with another generated file")
            continue
        generated_paths.add(target)
        generated_files.append(
            {
                "path": target,
                "source": source_name,
                "copy_from": copy_from,
                "package_copy_from": package_copy_from,
            }
        )
    for target, package_copy_from, source_name, mode in PRODUCT_SUPPORT_FILES:
        if target in generated_paths:
            warnings.append(f"product support target {target} collides with another generated file")
            continue
        generated_paths.add(target)
        generated_files.append(
            {
                "path": target,
                "source": source_name,
                "package_copy_from": package_copy_from,
                "package_copy_first": True,
                "mode": mode,
            }
        )
    smoke_tests.append(
        _python_syntax_check_command(
            '"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tools/status_report.py"'
        )
    )
    trusted_author_variables = _trusted_author_variables_for_generated_files(
        generated_files
    )

    if not merge_authority_lanes:
        warnings.append(
            f"{profile.profile_id}: profile has no merge-authority lanes; keep informational only"
        )
    if owner_surface["owner_login"] == OWNER_SURFACE_DEFAULTS["owner_login"]:
        warnings.append(
            "owner_surface.owner_login is unset; notify workflow will mention repository owner without assignment"
        )
    if owner_surface["status_issue"] == OWNER_SURFACE_DEFAULTS["status_issue"]:
        warnings.append(
            "owner_surface.status_issue is unset; weekly status workflow will skip until configured"
        )

    resolved_source_kind = source_kind or config_source_kind(config_path)
    # A packaged fallback resolves to a long absolute install path; display
    # the stable starter name instead so output stays portable.
    display_config_path = (
        PACKAGED_STARTER_CONFIG_NAME if resolved_source_kind == "packaged_starter" else config_path
    )
    has_root_config = root_adoption_config_present(repo_root)
    drift_hint = (
        setup_drift_next_step(profile_id=profile.profile_id)
        if resolved_source_kind == "packaged_starter" and has_root_config
        else ""
    )
    data = {
        "mode": "dry-run",
        "profile": {
            "id": profile.profile_id,
            "description": profile.description,
            "lanes": list(profile.lanes),
        },
        "config_source": {
            "kind": resolved_source_kind,
            "requested_path": display_config_path,
            "root_config_present": has_root_config,
        },
        "setup_drift_hint": drift_hint,
        "labels": sorted(set(labels)),
        "workflows": workflows,
        "generated_files": generated_files,
        "required_secrets": sorted(required_secrets),
        "required_variables": sorted(required_variables),
        "human_automation_token": {
            "required": human_token_required or bool(builder_entries),
            "secret": owner_surface["dispatch_token_env"],
            "expires_var": owner_surface["dispatch_token_expires_var"],
            "scopes": list(HUMAN_AUTOMATION_TOKEN_SCOPES),
        },
        "builder_loop": {
            "enabled": bool(builder_entries),
            "builders": list(builders),
            "lanes": list(builder_entries),
            "ready_label": owner_surface["ready_label"],
            "owner_labels": json.loads(
                _dispatch_lanes_workflow_entry(
                    builder_entries,
                    owner_surface,
                    decision_authorities=decision_authority_list,
                )[
                    "build_loop_owner_labels_json"
                ]
            )
            if builder_entries
            else [],
            "wip_cap": owner_surface["builder_wip_cap"],
            "runner_labels": list(
                _lane_runner_label_items(owner_surface["lane_runner_labels"])
            ),
            "runner_enabled_var": owner_surface["lane_runner_enabled_var"],
        },
        "adoption_config": {
            "path": ADOPTION_CONFIG_PATH,
            "fields_to_edit": list(ADOPTION_CONFIG_FIELDS),
            "trusted_author_variables": list(trusted_author_variables),
            "pilot_doctor_command": "code-mower doctor --adoption --repo OWNER/REPO --json",
        },
        "repositories": _repository_entries(config),
        "additional_repositories": list(add_repositories),
        "audit": {
            "budget_usd": audit_settings.budget_usd,
            "budget_description": audit_settings.budget_description,
            "max_diff_bytes": audit_settings.max_diff_bytes,
            "max_diff_hard_limit_bytes": audit_settings.max_diff_hard_limit_bytes,
        },
        "merge_authority_lanes": merge_authority_lanes,
        "informational_lanes": informational_lanes,
        "merge_authority_excludes_author": json.loads(author_exclusion_json)["enabled"],
        "smoke_tests": smoke_tests,
        "warnings": warnings,
    }

    if resolved_source_kind == "packaged_starter":
        source_line = f"Config source: packaged starter ({display_config_path})"
    else:
        source_line = f"Config source: explicit repository config ({display_config_path})"
    lines = [
        "Code Mower init dry-run",
        f"Profile: {profile.profile_id}",
        f"Description: {profile.description}",
        source_line,
        "",
        "Selected lanes:",
    ]
    if drift_hint:
        lines.extend(["", f"Setup drift: {drift_hint}"])
    for lane_id, lane in selected_lanes.items():
        if lane.get("merge_authority"):
            role = "merge-authority"
        elif lane.get("informational"):
            role = "informational"
        else:
            role = "standard"
        lines.append(f"- {lane_id}: {lane['driver']} / {lane['provider']} ({role})")

    lines.extend(["", "Labels to ensure:"])
    lines.extend(f"- {label}" for label in data["labels"])

    lines.extend(["", "Workflow files to render:"])
    if workflows:
        lines.extend(f"- {workflow['target']} ({workflow['lane']})" for workflow in workflows)
    else:
        lines.append("- none")

    lines.extend(["", "Generated file manifest:"])
    lines.extend(
        f"- {entry['path']} [{entry['source']}]" for entry in data["generated_files"]
    )

    lines.extend(["", "Editable adoption config:"])
    lines.append(
        f"- review {ADOPTION_CONFIG_PATH}, edit it for this repository, then commit it at the repo root"
    )
    lines.append("- fields to edit: " + ", ".join(ADOPTION_CONFIG_FIELDS))
    if trusted_author_variables:
        lines.append(
            "- trusted audit author variables: "
            + ", ".join(trusted_author_variables)
        )
    else:
        lines.append("- trusted audit author variables: none")
    lines.append("- verify: code-mower doctor --adoption --repo OWNER/REPO --json")

    lines.extend(["", "Configured repositories:"])
    if data["repositories"]:
        lines.extend(
            f"- {repo['slug']} (default branch: {repo['default_branch']})"
            for repo in data["repositories"]
        )
    else:
        lines.append("- none")
    if add_repositories:
        lines.extend(["", "Additional repository targets from --add-repo:"])
        lines.extend(f"- {slug}" for slug in add_repositories)

    lines.extend(["", "Audit limits:"])
    lines.append(f"- budget: {data['audit']['budget_description']}")
    lines.append(f"- max diff bytes: {data['audit']['max_diff_bytes']}")
    lines.append(
        f"- max diff hard limit bytes: {data['audit']['max_diff_hard_limit_bytes']}"
    )

    required_setup: list[str] = []
    token = data["human_automation_token"]
    if token["required"]:
        required_setup.append(
            f"create human automation token secret {token['secret']} and "
            f"expiry variable {token['expires_var']} (YYYY-MM-DD or never)"
        )
    if merge_authority_lanes:
        required_setup.extend(
            [
                "enable repository auto-merge "
                "(`gh api -X PATCH repos/OWNER/REPO -f allow_auto_merge=true`)",
                "require `code-mower/gate` from Any source "
                "(API checks[].app_id: null), not GitHub Actions "
                "(app_id: 15368)",
            ]
        )
    if required_setup:
        lines.extend(["", "Required setup next steps:"])
        lines.extend(f"- {step}" for step in required_setup)

    if builder_entries:
        lines.extend(["", "Builder loop:"])
        lines.append(f"- ready label: {owner_surface['ready_label']}")
        lines.append(f"- owner labels skipped: {', '.join(data['builder_loop']['owner_labels'])}")
        lines.append(f"- per-lane WIP cap: {owner_surface['builder_wip_cap']}")
        lines.append(f"- dispatch token secret: {owner_surface['dispatch_token_env']}")
        lines.append(f"- token expiry variable: {owner_surface['dispatch_token_expires_var']}")
        if mac_runner_entries:
            lines.append(
                f"- Mac runner variable: {owner_surface['lane_runner_enabled_var']}=true"
            )
            lines.append(
                "- Mac runner labels: "
                + ", ".join(data["builder_loop"]["runner_labels"])
            )
        for entry in builder_entries:
            lines.append(
                "- {lane}: {label} -> {dispatch_label}, docs/{doc}, audits: {audits}".format(
                    lane=entry["lane"],
                    label=entry["builder_label"],
                    dispatch_label=entry["dispatch_label"],
                    doc=entry["doc"],
                    audits=entry["audit_labels_display"],
                )
            )

    if merge_authority_lanes:
        lines.extend(
            [
                "",
                "Branch protection:",
                "- require `code-mower/gate` from Any source "
                "(API checks[].app_id: null), not GitHub Actions "
                "(app_id: 15368)",
                "- inspect: gh api repos/OWNER/REPO/branches/main/protection/required_status_checks",
            ]
        )

    lines.extend(["", "Required secrets/PAT fallbacks:"])
    if data["required_secrets"]:
        lines.extend(f"- {secret}" for secret in data["required_secrets"])
    else:
        lines.append("- none beyond GITHUB_TOKEN")

    lines.extend(["", "Required repository variables:"])
    if data["required_variables"]:
        lines.extend(f"- {variable}" for variable in data["required_variables"])
    else:
        lines.append("- none")

    lines.extend(["", "Human automation token:"])
    if token["required"]:
        lines.append(f"- secret: {token['secret']}")
        lines.append(f"- expiry variable: {token['expires_var']} (YYYY-MM-DD or never)")
        lines.append("- scopes: " + "; ".join(token["scopes"]))
        lines.append(
            f"- setup: gh secret set {token['secret']} && "
            f"gh variable set {token['expires_var']} --body YYYY-MM-DD"
        )
    else:
        lines.append("- not required for this profile")

    lines.extend(["", "Smoke tests after render:"])
    lines.extend(f"- {test}" for test in smoke_tests)

    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)

    return RenderedPlan(text="\n".join(lines) + "\n", data=data)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["auth"]:
        return _auth_main(argv[1:])

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="code-mower.example.yml")
    parser.add_argument("--profile", default="recommended")
    parser.add_argument(
        "--builders",
        metavar="LANES",
        help=(
            "render the build-loop dispatcher for comma-separated builder lanes "
            "(for example: codex,claude,cursor); defaults to --dry-run when no mode is set"
        ),
    )
    parser.add_argument(
        "--easy",
        action="store_true",
        help=(
            "safe first-run alias for --profile recommended --dry-run; combine "
            "with --apply to write generated output instead"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="render the init plan")
    parser.add_argument("--apply", action="store_true", help="write generated files to --output-dir")
    parser.add_argument(
        "--add-repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help=(
            "append a sibling repository target to the rendered plan without "
            "editing code-mower.yml; repeat for multiple repos"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_APPLY_OUTPUT_DIR,
        help="safe output directory for --apply mode",
    )
    parser.add_argument(
        "--actionlint-bin",
        default="actionlint",
        help="actionlint executable used to validate generated workflows during --apply",
    )
    parser.add_argument(
        "--skip-actionlint",
        action="store_true",
        help="write generated workflows without actionlint validation",
    )
    parser.add_argument(
        "--skip-github-labels",
        action="store_true",
        help=(
            "with --apply label setup, do not create missing GitHub labels in "
            "the target repo via gh; --dry-run lists labels without mutating GitHub"
        ),
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help=(
            "explicit GitHub repository for --apply label creation; "
            "defaults to the current checkout's origin remote"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit dry-run plan as JSON")
    args = parser.parse_args(argv)

    if args.easy:
        args.profile = "recommended"
        if not args.dry_run and not args.apply:
            args.dry_run = True
    if args.builders and not args.dry_run and not args.apply:
        args.dry_run = True
    if args.apply and args.dry_run:
        print("error: choose either --dry-run or --apply", file=sys.stderr)
        return 1
    if not args.dry_run and not args.apply:
        print("error: choose --dry-run or --apply", file=sys.stderr)
        return 1

    try:
        label_repo_override = (
            _normalize_repo_slug(args.repo, option="--repo") if args.repo else None
        )
        builder_lanes = _parse_builder_lanes(args.builders)
        config_source = _resolve_config_path(args.config)
        # An explicitly supplied local file keeps its identity even when its
        # basename matches the starter; only a resolved packaged fallback
        # counts as the packaged starter.
        packaged_fallback = config_source != Path(args.config)
        rendered_config_path = (
            str(config_source) if packaged_fallback else args.config
        )
        config, added_repos = config_with_added_repositories(
            load_config(config_source),
            tuple(args.add_repo),
        )
        plan = render_init_plan(
            config,
            profile_id=args.profile,
            config_path=rendered_config_path,
            add_repositories=added_repos,
            builders=builder_lanes,
            repo_root=Path.cwd(),
            source_kind="packaged_starter" if packaged_fallback else "explicit_repository_config",
        )
        label_repo = ""
        should_ensure_github_labels = bool(
            args.apply
            and not args.skip_github_labels
            and (builder_lanes or args.add_repo or label_repo_override)
        )
        if should_ensure_github_labels:
            label_repo = _target_repo_slug_for_labels(
                Path.cwd(),
                explicit_repo=label_repo_override,
            )
            if not label_repo:
                raise ConfigError(
                    "target GitHub repository could not be determined from "
                    "the current checkout; rerun from a GitHub checkout, "
                    "pass --repo OWNER/REPO, or pass --skip-github-labels"
                )
        apply_result = (
            apply_init_plan(
                plan,
                Path(args.output_dir),
                actionlint_bin=None if args.skip_actionlint else args.actionlint_bin,
            )
            if args.apply
            else None
        )
        if apply_result is not None and should_ensure_github_labels:
            github_labels = ensure_github_labels(
                plan.data["labels"],
                repo=label_repo,
            )
            apply_result = {
                **apply_result,
                "github_labels": github_labels,
            }
    except ConfigError as exc:
        print(_init_config_error_message(exc, config_arg=args.config), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(apply_result or plan.data, indent=2, sort_keys=True))
    elif apply_result:
        print(f"Code Mower init apply wrote {len(apply_result['written_files'])} files")
        print(f"Output: {apply_result['output_dir']}")
        actionlint_result = apply_result.get("actionlint")
        if isinstance(actionlint_result, Mapping) and actionlint_result.get("status") == "skipped":
            print(f"Warning: skipped actionlint: {actionlint_result.get('reason')}")
        if apply_result["placeholder_files"]:
            print("Placeholders:")
            for path in apply_result["placeholder_files"]:
                print(f"- {path}")
        if args.builders:
            print("Builder loop next steps:")
            token = plan.data["human_automation_token"]
            print(f"- set secret {token['secret']}")
            print(f"- set variable {token['expires_var']} (YYYY-MM-DD or never)")
            builder_loop = plan.data["builder_loop"]
            print(f"- set variable CODE_MOWER_MAX_WIP or use default {builder_loop['wip_cap']}")
            if builder_loop["runner_enabled_var"] in plan.data["required_variables"]:
                print(f"- set variable {builder_loop['runner_enabled_var']}=true after the Mac runner is ready")
        label_result = apply_result.get("github_labels")
        if isinstance(label_result, Mapping):
            label_repo = label_result.get("repo")
            label_scope = f" for {label_repo}" if label_repo else ""
            if label_result.get("status") == "passed":
                print(
                    f"GitHub labels{label_scope}: "
                    f"{len(label_result.get('created', []))} created, "
                    f"{len(label_result.get('existing', []))} already present"
                )
            else:
                print(
                    f"Warning: GitHub labels{label_scope} not fully ensured: "
                    f"{label_result.get('reason') or label_result.get('failed')}"
                )
    else:
        print(plan.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
