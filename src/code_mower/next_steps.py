#!/usr/bin/env python3
"""Render the next Code Mower actions after first-run setup."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools import code_mower_package
    from tools.code_mower_config import ConfigError
else:  # pragma: no cover - exercised after package extraction.
    from . import package as code_mower_package
    from .config import ConfigError


DEFAULT_PROVIDER_TEMPLATES = code_mower_package.DEFAULT_PROVIDER_TEMPLATES
DEFAULT_PROFILE = "recommended"
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PR_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
REQUEST_LABEL_RE = re.compile(r"^needs-[A-Za-z0-9][A-Za-z0-9_-]*-(audit|review)$")


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ConfigError(f"{name} must be a mapping")


def _profile_lanes(templates: Mapping[str, Any], profile_id: str) -> tuple[str, ...]:
    profiles = _as_mapping(templates.get("profiles"), "profiles")
    profile = profiles.get(profile_id)
    if not isinstance(profile, Mapping):
        available = ", ".join(sorted(str(key) for key in profiles)) or "none"
        raise ConfigError(f"unknown profile {profile_id!r}; available profiles: {available}")
    lanes = profile.get("lanes", [])
    if not isinstance(lanes, list):
        raise ConfigError(f"profile {profile_id!r} lanes must be a list")
    return tuple(str(lane) for lane in lanes)


def _lane_catalog(templates: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    catalog = _as_mapping(templates.get("provider_templates"), "provider_templates")
    result: dict[str, Mapping[str, Any]] = {}
    for lane_id, lane in catalog.items():
        if isinstance(lane, Mapping):
            result[str(lane_id)] = lane
    return result


def _lane_classes(
    lanes: tuple[str, ...],
    catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    merge_gating: list[str] = []
    informational: list[str] = []
    paid_or_manual: list[str] = []
    local_or_cli: list[str] = []
    for lane_id in lanes:
        lane = catalog.get(lane_id, {})
        if lane.get("merge_authority"):
            merge_gating.append(lane_id)
        if lane.get("informational"):
            informational.append(lane_id)
        if lane.get("spend_policy") == "paid" or lane.get("trigger_policy") == "manual":
            paid_or_manual.append(lane_id)
        if lane.get("driver") in {"local_cli", "api_model"}:
            local_or_cli.append(lane_id)
    return {
        "merge_gating": tuple(merge_gating),
        "informational": tuple(informational),
        "paid_or_manual": tuple(paid_or_manual),
        "local_or_cli": tuple(local_or_cli),
    }


def _request_label(lane_id: str, lane: Mapping[str, Any]) -> str:
    lane_type = str(lane.get("type") or "audit")
    stem = lane_id
    if stem.endswith("_audit"):
        stem = stem[: -len("_audit")]
    if stem.endswith("_review"):
        stem = stem[: -len("_review")]
    suffix = "review" if lane_type == "review" else "audit"
    label = f"needs-{stem.replace('_', '-')}-{suffix}"
    if not REQUEST_LABEL_RE.fullmatch(label):
        raise ConfigError(f"generated request label is not shell-safe: {label!r}")
    return label


def _validate_repo_and_pr(repo: str, pr: str) -> None:
    if not GITHUB_REPO_RE.fullmatch(repo):
        raise ConfigError(
            "repo must be a GitHub OWNER/REPO slug containing only letters, "
            "numbers, '.', '_', or '-'"
        )
    if not PR_NUMBER_RE.fullmatch(pr):
        raise ConfigError("pr must be a positive numeric pull request number")


def build_next_steps(
    templates: Mapping[str, Any],
    *,
    profile: str = DEFAULT_PROFILE,
    repo: str = "owner/repo",
    pr: str = "123",
) -> Mapping[str, Any]:
    _validate_repo_and_pr(repo, pr)
    lanes = _profile_lanes(templates, profile)
    catalog = _lane_catalog(templates)
    classes = _lane_classes(lanes, catalog)
    audit_lanes = classes["merge_gating"] or lanes[:1]
    first_audit_lane = audit_lanes[0] if audit_lanes else "codex"
    first_audit_label = _request_label(first_audit_lane, catalog.get(first_audit_lane, {}))
    quoted_profile = shlex.quote(profile)
    if profile == DEFAULT_PROFILE:
        init_plan_command = "code-mower init --easy"
        init_apply_command = "code-mower init --easy --apply --output-dir .code-mower.generated"
        doctor_command = "code-mower doctor --easy --probe-runtime --github --json"
    else:
        init_plan_command = f"code-mower init --profile {quoted_profile} --dry-run"
        init_apply_command = (
            "code-mower init "
            f"--profile {quoted_profile} --apply --output-dir .code-mower.generated"
        )
        doctor_command = (
            f"code-mower doctor --easy --profile {quoted_profile} "
            "--probe-runtime --json"
        )

    steps: list[dict[str, Any]] = [
        {
            "id": "render-easy-config",
            "title": "Render the easy-mode starter plan",
            "command": init_plan_command,
            "why": (
                "Shows labels, secrets, workflows, and smoke tests without mutating "
                "the repository."
            ),
        },
        {
            "id": "write-reviewable-config",
            "title": "Write generated setup files to a reviewable directory",
            "command": init_apply_command,
            "why": "Creates files the user can inspect before installing workflows.",
        },
        {
            "id": "doctor-easy",
            "title": "Verify first-run readiness",
            "command": doctor_command,
            "why": (
                "Checks GitHub auth, Python, provider catalog coverage, CLI "
                "availability, token env, Actions cost traps, and harmless runtime probes."
            ),
        },
        {
            "id": "wrapper-rehearsal",
            "title": "Compare standalone package behavior with any repo-local Code Mower tools",
            "command": "code-mower migration wrapper-rehearsal --repo-path . --json",
            "why": (
                "Gives existing product repos a shadow-mode migration check before "
                "they replace mirrored tool files with a pinned standalone package."
            ),
        },
        {
            "id": "first-audit",
            "title": "Run the first head-bound audit on a real PR",
            "command": f"gh pr edit {pr} --repo {repo} --add-label {first_audit_label}",
            "why": "Gets a structured reviewer result against the user's actual codebase.",
            "lanes": list(audit_lanes),
            "label": first_audit_label,
        },
        {
            "id": "calibration-run",
            "title": "Run a starter calibration corpus",
            "command": (
                "code-mower calibration run .code-mower.generated/calibration-corpus.json "
                "--lanes gemini-cli --results-dir .code-mower/calibration-results --json"
            ),
            "why": "Persists raw reviewer results for quality, latency, and cost analysis.",
        },
        {
            "id": "value-report",
            "title": "Generate the first reviewer value report",
            "command": (
                "code-mower calibration value-report .code-mower.generated/calibration-corpus.json "
                "--runs .code-mower/calibration-results/calibration-run-results.json "
                "--spend .code-mower.generated/reviewer-spend.json "
                "--output reviewer-value-report.md"
            ),
            "why": "Converts reviewer outcomes into useful-rate, precision, latency, and spend evidence.",
        },
        {
            "id": "calibration-evidence",
            "title": "Write adjudicated calibration evidence for metrics",
            "command": (
                "code-mower calibration evidence .code-mower.generated/calibration-corpus.json "
                "--json > calibration-evidence.json"
            ),
            "why": "Creates the reviewer evidence input consumed by metrics and lane policy.",
        },
        {
            "id": "lane-policy",
            "title": "Decide informational, selective, and merge-gating lanes from evidence",
            "command": (
                "code-mower reviewer-metrics calibration-evidence.json "
                "--spend .code-mower.generated/reviewer-spend.json "
                "--json > reviewer-metrics.json && "
                "code-mower calibration policy reviewer-metrics.json --json > lane-policy.json"
            ),
            "why": "Keeps new reviewers informational until measured on the real codebase.",
            "informational_lanes": list(classes["informational"]),
        },
        {
            "id": "context-packs",
            "title": "Add selective context packs for repeated review blind spots",
            "command": "code-mower context-packs .code-mower.generated/context-packs.json --json",
            "why": "Lets lanes request important surrounding files without bloating every audit.",
        },
        {
            "id": "cloud-export",
            "title": "Export a local benchmark bundle when ready to share aggregate data",
            "command": (
                "code-mower cloud export "
                "--report reviewer-metrics=reviewer-metrics.json "
                "--report lane-policy=lane-policy.json "
                "--report value-report=reviewer-value-report.md "
                "--output-dir .code-mower/cloud-benchmark-bundle --json"
            ),
            "why": "Produces a local, source-free bundle for future opt-in benchmarking/reporting.",
        },
    ]

    return {
        "schema": "code_mower.nextSteps.v1",
        "profile": profile,
        "lanes": list(lanes),
        "lane_classes": {key: list(value) for key, value in classes.items()},
        "steps": steps,
    }


def render_next_steps_text(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower next steps ({payload['profile']})",
        "",
        "Lanes: " + ", ".join(payload.get("lanes", [])),
        "",
    ]
    for index, step in enumerate(payload.get("steps", []), start=1):
        lines.append(f"{index}. {step['title']}")
        lines.append(f"   command: {step['command']}")
        lines.append(f"   why: {step['why']}")
    return "\n".join(lines) + "\n"


def _resolve_provider_templates(path_text: str | None) -> Path:
    if path_text is None:
        return code_mower_package.resolve_provider_templates_path(DEFAULT_PROVIDER_TEMPLATES)
    return Path(path_text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--repo", default="owner/repo")
    parser.add_argument("--pr", default="123")
    parser.add_argument("--provider-templates", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        templates = code_mower_package.load_provider_templates(
            _resolve_provider_templates(args.provider_templates)
        )
        payload = build_next_steps(
            templates,
            profile=args.profile,
            repo=args.repo,
            pr=args.pr,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_next_steps_text(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
