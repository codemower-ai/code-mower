#!/usr/bin/env python3
"""Render a non-mutating Code Mower init plan for a setup profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
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
else:  # pragma: no cover - exercised after package extraction.
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
}
GATE_HEALTH_WORKFLOW_PATH = ".github/workflows/code-mower-gate-health.yml"
GATE_HEALTH_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-gate-health.yml.j2"
AGENT_PR_LABELER_WORKFLOW_PATH = ".github/workflows/code-mower-agent-pr-labeler.yml"
AGENT_PR_LABELER_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-agent-pr-labeler.yml.j2"
FIX_ROUND_DISPATCH_WORKFLOW_PATH = ".github/workflows/code-mower-fix-round-dispatch.yml"
FIX_ROUND_DISPATCH_WORKFLOW_TEMPLATE = "templates/workflows/code-mower-fix-round-dispatch.yml.j2"

DEFAULT_APPLY_OUTPUT_DIR = ".code-mower.generated"
APPLY_MANIFEST_FILENAME = "code-mower-init-plan.json"
APPLY_SUMMARY_FILES = (
    "labels.txt",
    "required-secrets.txt",
    "required-variables.txt",
    "smoke-tests.sh",
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
        # Copy the packaged helper module so product-repo gate workflows can
        # import tools.audit_labeler_lib without a separate package install.
        "tools/audit_labeler_lib.py",
        "audit_labeler_lib.py",
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
    "ready_label": "tier:R",
    "phase_labels": "phase:0,phase:1,phase:2,phase:3,phase:4,phase:5",
    "reviewer_spend_path": ".code-mower/reviewer-spend.json",
    "reviewer_value_report_path": ".code-mower/reviewer-value-report.md",
    "dispatch_token_env": "DISPATCH_TOKEN",
    "dispatch_token_expires_var": "DISPATCH_TOKEN_EXPIRES_AT",
}

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


def _normalize_repo_slug(value: str) -> str:
    slug = value.strip().strip("/")
    if not OWNER_REPO_RE.fullmatch(slug):
        raise ConfigError("--add-repo expects an OWNER/REPO slug")
    return slug


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


def _csv_value(value: Any, default: str) -> str:
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _owner_surface_config(config: Mapping[str, Any]) -> dict[str, str]:
    raw = config.get("owner_surface")
    surface = raw if isinstance(raw, Mapping) else {}
    rendered: dict[str, str] = {}
    for key, default in OWNER_SURFACE_DEFAULTS.items():
        if key == "phase_labels":
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
) -> tuple[dict[str, str], ...]:
    entries: list[dict[str, str]] = []
    for lane_id, lane in selected_lanes.items():
        if lane.get("driver") != "local_cli":
            continue
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


def _gate_lane_entry(lane_id: str, lane: Mapping[str, Any]) -> dict[str, str]:
    labels = _labels_for(lane)
    trailer_lane = _trailer_lane_name(lane_id, lane)
    github_actions_workflows = LOCAL_AUDIT_WORKFLOW_PATH if lane.get("driver") == "local_cli" else ""
    return {
        "id": lane_id,
        "author_lane": _author_lane_name(lane_id, lane),
        "display_name": _display_name(trailer_lane),
        "done": str(labels["done"]),
        "blocked": str(labels["blocked"]),
        "builder_label": f"builder:{trailer_lane}",
        "bot_authors": _bot_author_csv(_default_trailer_bot_authors(trailer_lane), lane),
        "authors_env": _authors_env_for_lane(lane_id, lane),
        "github_actions_workflows": github_actions_workflows,
    }


def _identity_section(raw_identity: Mapping[str, Any], section: str) -> dict[str, str]:
    raw_section = raw_identity.get(section)
    if not isinstance(raw_section, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in raw_section.items()
        if str(key) and str(value)
    }


def _author_exclusion_payload(
    config: Mapping[str, Any],
    selected_lanes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    raw_identity = config.get("builder_identity")
    identity = raw_identity if isinstance(raw_identity, Mapping) else {}
    payload = {
        "enabled": bool(config.get("merge_authority_excludes_author", True)),
        "labels": _identity_section(identity, "labels"),
        "authors": _identity_section(identity, "authors"),
        "trailers": _identity_section(identity, "trailers"),
    }
    for lane_id, lane in selected_lanes.items():
        if lane.get("type") == "audit":
            author_lane = _author_lane_name(lane_id, lane)
            payload["labels"].setdefault(f"builder:{author_lane}", author_lane)
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
                "needs": str(labels["needs"]),
                "done": str(labels["done"]),
                "blocked": str(labels["blocked"]),
            }
        )
    return tuple(entries)


def _builder_labels_by_lane(author_exclusion: Mapping[str, Any]) -> dict[str, str]:
    labels = _identity_section(author_exclusion, "labels")
    out: dict[str, str] = {}
    for label, lane in sorted(labels.items()):
        if label.startswith("builder:"):
            out.setdefault(lane, label)
    return out


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
        if len(lane_labels) > 1:
            warnings.append(
                f"builder_identity: lane {lane!r} maps multiple builder labels; "
                f"generated automation uses {lane_labels[0]!r}"
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
    prefixes = _identity_section(identity, "branch_prefixes")
    labels_by_lane = _builder_labels_by_lane(author_exclusion)
    rules_by_lane: dict[str, dict[str, Any]] = {}
    for prefix, lane in sorted(prefixes.items()):
        label = labels_by_lane.get(lane) or f"builder:{lane}"
        rule = rules_by_lane.setdefault(
            lane,
            {"builder_lane": lane, "builder_label": label, "branch_prefixes": []},
        )
        rule["branch_prefixes"].append(prefix)
    return tuple(rules_by_lane[lane] for lane in sorted(rules_by_lane))


def _fix_round_rules(
    config: Mapping[str, Any],
    author_exclusion: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    identity = config.get("builder_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    mentions = _identity_section(identity, "fix_round_mentions")
    labels_by_lane = _builder_labels_by_lane(author_exclusion)
    rules: list[dict[str, str]] = []
    for lane, mention in sorted(mentions.items()):
        label = labels_by_lane.get(lane) or f"builder:{lane}"
        rules.append(
            {
                "builder_lane": lane,
                "builder_label": label,
                "mention": mention,
            }
        )
    return tuple(rules)


def _gate_workflow_entry(
    config: Mapping[str, Any],
    selected_lanes: Mapping[str, Mapping[str, Any]],
    *,
    author_exclusion_json: str,
    owner_label: str = DEFAULT_OWNER_LABEL,
    owner_sitting_label: str = "owner-sitting",
    owner_login: str = "",
    gate_override_label: str = "gate:override",
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
        "__AUTHOR_EXCLUSION_JSON__": str(
            entry.get("author_exclusion_json") or '{"enabled":false}'
        ),
        "__AUTHORS_ENV__": str(entry.get("authors_env") or ""),
        "__BLOCKED_LABEL__": str(entry.get("blocked_label") or ""),
        "__BOT_AUTHORS__": str(entry.get("bot_authors") or ""),
        "__DISPLAY_NAME__": str(entry.get("display_name") or ""),
        "__DONE_LABEL__": str(entry.get("done_label") or ""),
        "__AGENT_PR_RULES_JSON__": str(entry.get("agent_pr_rules_json") or "[]"),
        "__AGENT_PR_AUDIT_LANES_JSON__": str(
            entry.get("agent_pr_audit_lanes_json") or "[]"
        ),
        "__DISPATCH_TOKEN_ENV__": str(entry.get("dispatch_token_env") or "DISPATCH_TOKEN"),
        "__FIX_ROUND_RULES_JSON__": str(entry.get("fix_round_rules_json") or "[]"),
        "__FIX_ROUND_AUDIT_LANES_JSON__": str(
            entry.get("fix_round_audit_lanes_json") or "[]"
        ),
        "__GATE_HEALTH_CRON__": str(entry.get("gate_health_cron") or ""),
        "__GATE_HEALTH_LANES_JSON__": str(entry.get("gate_health_lanes_json") or "[]"),
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
        "__GATE_LANES_JSON__": str(entry.get("gate_lanes_json") or "[]"),
        "__GATE_OVERRIDE_LABEL_JSON__": json.dumps(str(gate_override_label)),
        "__GITHUB_ACTIONS_WORKFLOWS__": str(entry.get("github_actions_workflows") or ""),
        "__LABEL_TOKEN_ENV__": str(entry.get("label_token_env") or ""),
        "__LANE_ID__": str(entry.get("lane_id") or ""),
        "__LOCAL_AUDIT_LABEL_CONTAINS__": str(entry.get("local_audit_label_contains") or ""),
        "__LOCAL_AUDIT_LABEL_MATCH__": str(entry.get("local_audit_label_match") or ""),
        "__LOCAL_AUDIT_LANES_JSON__": str(entry.get("local_audit_lanes_json") or "[]"),
        "__LOCAL_AUDIT_RUNNER_LABEL__": str(entry.get("local_audit_runner_label") or ""),
        "__LOCAL_AUDIT_TOKEN_ENV_ASSIGNMENTS__": str(
            entry.get("local_audit_token_env_assignments") or ""
        ),
        "__NEEDS_LABEL__": str(entry.get("needs_label") or ""),
        "__NEEDS_OWNER_LABEL__": str(entry.get("needs_owner_label") or ""),
        "__OWNER_DECISION_LABEL__": str(entry.get("owner_decision_label") or ""),
        "__OWNER_SITTING_LABEL__": str(entry.get("owner_sitting_label") or ""),
        "__OWNER_LOGIN__": str(entry.get("owner_login") or ""),
        "__GATE_OVERRIDE_LABEL__": str(entry.get("gate_override_label") or ""),
        "__PHASE_LABELS__": str(entry.get("phase_labels") or ""),
        "__READY_LABEL__": str(entry.get("ready_label") or ""),
        "__REVIEWER_SPEND_PATH__": str(entry.get("reviewer_spend_path") or ""),
        "__REVIEWER_VALUE_REPORT_PATH__": str(
            entry.get("reviewer_value_report_path") or ""
        ),
        "__STATUS_ISSUE__": str(entry.get("status_issue") or ""),
        "__OWNER_LABEL__": json.dumps(
            str(entry.get("owner_label") or DEFAULT_OWNER_LABEL)
        ),
        "__OWNER_SITTING_LABEL_JSON__": json.dumps(
            str(entry.get("owner_sitting_label") or "owner-sitting")
        ),
        "__OWNER_LOGIN_JSON__": json.dumps(str(entry.get("owner_login") or "")),
        "__TRAILER_LANE__": str(entry.get("trailer_lane") or ""),
        "__TRAILER_PREFIX__": str(entry.get("trailer_prefix") or ""),
        "__WEEKLY_STATUS_CRON__": str(entry.get("weekly_status_cron") or ""),
        "__WORKFLOW_NAME__": str(entry.get("workflow_name") or "Code Mower labeler"),
    }
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _workflow_template_needs_render(source: str) -> bool:
    return source in {
        "agent-pr-labeler-workflow-template",
        "shared-cleanup-template",
        "code-mower-gate-health-workflow-template",
        "code-mower-gate-workflow-template",
        "fix-round-dispatch-workflow-template",
        "hosted-bridge-workflow-template",
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
    copy_from = str(entry.get("copy_from", path))
    candidates.append(source_root / copy_from)
    if package_copy_from and not entry.get("package_copy_first"):
        candidates.append(Path(__file__).resolve().parent / str(package_copy_from))
    return tuple(candidates)


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

    for entry, path, destination in generated_destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = next(
            (candidate for candidate in _copy_source_candidates(source_root, entry, path) if candidate.is_file()),
            None,
        )
        if source is not None:
            if entry.get("source") == "shared-stale-label-template":
                destination.write_text(
                    _render_stale_workflow_template(
                        source.read_text(encoding="utf-8"),
                        lane=str(entry.get("stale_lane") or "devin"),
                    ),
                    encoding="utf-8",
                )
            elif _workflow_template_needs_render(str(entry.get("source"))):
                destination.write_text(
                    _render_workflow_template(source.read_text(encoding="utf-8"), entry),
                    encoding="utf-8",
                )
            else:
                shutil.copyfile(source, destination)
        else:
            destination.write_text(_placeholder_text(path, str(entry["source"])), encoding="utf-8")
            placeholder_files.append(str(destination))
        if entry.get("mode") == "0755":
            destination.chmod(0o755)
        written_files.append(str(destination))

    return {
        "mode": "apply",
        "output_dir": str(output_dir),
        "written_files": written_files,
        "placeholder_files": placeholder_files,
    }


def render_init_plan(
    config: Mapping[str, Any],
    profile_id: str = "recommended",
    config_path: str = "code-mower.example.yml",
    *,
    package_mode: bool | None = None,
    package_command: str | None = None,
    add_repositories: tuple[str, ...] = (),
) -> RenderedPlan:
    issues = validate_config(config)
    if issues:
        raise ConfigError(f"invalid Code Mower config:\n{_format_issues(issues)}")

    profile = _profile(config, profile_id)
    lanes: Mapping[str, Mapping[str, Any]] = config["lanes"]
    selected_lanes = {lane_id: lanes[lane_id] for lane_id in profile.lanes}

    labels: list[str] = []
    workflows: list[dict[str, str]] = []
    generated_files: list[dict[str, str]] = []
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
    author_exclusion_json = _author_exclusion_json(config, selected_lanes)
    author_exclusion = json.loads(author_exclusion_json)
    audit_rearm_entries = _audit_rearm_entries(selected_lanes)
    agent_pr_rules = _agent_pr_label_rules(config, author_exclusion)
    fix_round_rules = _fix_round_rules(config, author_exclusion)
    warnings.extend(
        _builder_identity_rule_warnings(
            author_exclusion,
            [*agent_pr_rules, *fix_round_rules],
        )
    )

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

    local_audit_entries = _local_audit_entries(selected_lanes)
    human_token_required = human_automation_token_required(
        config,
        tuple(selected_lanes.items()),
    )
    if human_token_required:
        required_secrets.add(owner_surface["dispatch_token_env"])
        required_variables.add(owner_surface["dispatch_token_expires_var"])
    if local_audit_entries and LOCAL_AUDIT_WORKFLOW_PATH not in generated_paths:
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
            )
        )

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
        "python3 -m py_compile "
        '"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tools/status_report.py"'
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

    data = {
        "mode": "dry-run",
        "profile": {
            "id": profile.profile_id,
            "description": profile.description,
            "lanes": list(profile.lanes),
        },
        "labels": sorted(set(labels)),
        "workflows": workflows,
        "generated_files": generated_files,
        "required_secrets": sorted(required_secrets),
        "required_variables": sorted(required_variables),
        "human_automation_token": {
            "required": human_token_required,
            "secret": owner_surface["dispatch_token_env"],
            "expires_var": owner_surface["dispatch_token_expires_var"],
            "scopes": list(HUMAN_AUTOMATION_TOKEN_SCOPES),
        },
        "repositories": _repository_entries(config),
        "additional_repositories": list(add_repositories),
        "merge_authority_lanes": merge_authority_lanes,
        "informational_lanes": informational_lanes,
        "merge_authority_excludes_author": json.loads(author_exclusion_json)["enabled"],
        "smoke_tests": smoke_tests,
        "warnings": warnings,
    }

    lines = [
        "Code Mower init dry-run",
        f"Profile: {profile.profile_id}",
        f"Description: {profile.description}",
        "",
        "Selected lanes:",
    ]
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

    token = data["human_automation_token"]
    lines.extend(["", "Human automation token:"])
    if token["required"]:
        lines.append(f"- secret: {token['secret']}")
        lines.append(f"- expiry variable: {token['expires_var']} (YYYY-MM-DD)")
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
    parser.add_argument("--json", action="store_true", help="emit dry-run plan as JSON")
    args = parser.parse_args(argv)

    if args.easy:
        args.profile = "recommended"
        if not args.dry_run and not args.apply:
            args.dry_run = True
    if args.apply and args.dry_run:
        print("error: choose either --dry-run or --apply", file=sys.stderr)
        return 1
    if not args.dry_run and not args.apply:
        print("error: choose --dry-run or --apply", file=sys.stderr)
        return 1

    try:
        config_source = _resolve_config_path(args.config)
        rendered_config_path = (
            str(config_source) if config_source != Path(args.config) else args.config
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
        )
        apply_result = (
            apply_init_plan(plan, Path(args.output_dir))
            if args.apply
            else None
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(apply_result or plan.data, indent=2, sort_keys=True))
    elif apply_result:
        print(f"Code Mower init apply wrote {len(apply_result['written_files'])} files")
        print(f"Output: {apply_result['output_dir']}")
        if apply_result["placeholder_files"]:
            print("Placeholders:")
            for path in apply_result["placeholder_files"]:
                print(f"- {path}")
    else:
        print(plan.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
