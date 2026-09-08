#!/usr/bin/env python3
"""Provider-neutral work-tracker contract for Code Mower.

Normalizes GitHub- or Jira-shaped work items into one contract so callers
stop depending on GitHub-specific issue dictionaries. No network calls and no
mutation/apply logic: those land in later, dependent issues (#799, #800).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


TRACKER_WORK_ITEM_SCHEMA = "code_mower.trackerWorkItem.v1"

TRACKER_KINDS = ("github", "jira_cloud")

LIFECYCLE_CATEGORIES = ("new", "in_progress", "blocked", "done")

ALLOWED_MUTATION_OPERATIONS = ("assign", "transition", "comment", "link")

ALLOWED_GITHUB_IDENTITY_KEYS = frozenset({"repo", "number"})
REQUIRED_GITHUB_IDENTITY_KEYS = ALLOWED_GITHUB_IDENTITY_KEYS
ALLOWED_JIRA_IDENTITY_KEYS = frozenset({"cloud_id", "project_id", "issue_id", "issue_key"})
REQUIRED_JIRA_IDENTITY_KEYS = frozenset({"cloud_id", "project_id", "issue_id"})

# The contract may only carry short, bounded provider labels, never prose.
ALLOWED_PROVIDER_METADATA_KEYS = frozenset({"status_name", "issue_type"})
MAX_PROVIDER_METADATA_VALUE_LENGTH = 64
MAX_IDENTITY_VALUE_LENGTH = 256
MAX_LABEL_LENGTH = 128

ALLOWED_WORK_ITEM_FIELDS = frozenset(
    {
        "schema",
        "source_kind",
        "identity",
        "url",
        "lifecycle_category",
        "labels",
        "assigned",
        "created_at",
        "updated_at",
        "provider_metadata",
    }
)

# Field names that must never appear in a normalized work item, even under an
# additive/renamed key, because they imply issue-body-shaped prose or raw
# process output rather than bounded identity/lifecycle metadata.
DENYLISTED_WORK_ITEM_FIELDS = frozenset(
    {
        "body",
        "description",
        "comment",
        "comments",
        "attachment",
        "attachments",
        "source",
        "diff",
        "transcript",
        "raw_output",
        "stdout",
        "stderr",
        "auth_output",
        "local_path",
        "secret",
        "secrets",
        "token",
        "prompt",
    }
)

# Safe targets a `tracker.jira_cloud.field_mappings` config entry may map a
# Jira field id onto. This is a config-schema concern (see config.py) but the
# allow-list lives here so the contract stays the single source of truth for
# "what is a safe normalized field."
SAFE_FIELD_MAPPING_TARGETS = frozenset({"lifecycle_category", "labels", "assigned"})


@dataclass(frozen=True)
class TrackerCapabilities:
    """Read support and mutation plan/apply support, modeled separately: a
    tracker can be read-eligible with no mutation authority, and a tracker
    configured to plan mutations can still never apply one from this
    contract alone."""

    kind: str
    can_read: bool
    can_plan_mutations: bool
    can_apply_mutations: bool
    allowed_mutation_operations: tuple[str, ...] = field(default_factory=tuple)


def tracker_capabilities(
    kind: str,
    jira_cloud_config: Mapping[str, Any] | None = None,
    *,
    apply_requested: bool = False,
) -> TrackerCapabilities:
    """Return declared capabilities for a tracker kind; no network call.

    ``apply_requested`` is the runtime half of the write guard (the
    ``--apply`` flag on the guarded mutation surface). It defaults to False,
    so configuration alone never reports apply authority.
    """
    if kind == "github":
        return TrackerCapabilities(
            kind="github",
            can_read=True,
            can_plan_mutations=False,
            can_apply_mutations=False,
        )
    if kind == "jira_cloud":
        mutations: Mapping[str, Any] = {}
        if isinstance(jira_cloud_config, Mapping):
            raw_mutations = jira_cloud_config.get("mutations")
            if isinstance(raw_mutations, Mapping):
                mutations = raw_mutations
        allowed_ops = tuple(
            op
            for op in (mutations.get("allowed_operations") or [])
            if op in ALLOWED_MUTATION_OPERATIONS
        )
        can_plan = mutations.get("writes_enabled") is True and bool(allowed_ops)
        return TrackerCapabilities(
            kind="jira_cloud",
            can_read=True,
            can_plan_mutations=can_plan,
            # Both guards, never one: configured write enablement AND an
            # explicit runtime apply flag.
            can_apply_mutations=can_plan and bool(apply_requested),
            allowed_mutation_operations=allowed_ops,
        )
    raise ValueError(f"unsupported tracker kind: {kind!r}")


def _validate_identity_keys(
    identity: Mapping[str, Any],
    allowed: frozenset[str],
    required: frozenset[str],
    path: str,
    errors: list[str],
) -> None:
    unknown = set(identity) - allowed
    for key in sorted(unknown):
        errors.append(f"{path}.{key}: unknown identity field")
    for key in sorted(set(identity) & allowed):
        value = identity.get(key)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_IDENTITY_VALUE_LENGTH
            or "\n" in value
            or "\r" in value
        ):
            errors.append(
                f"{path}.{key}: must be a non-empty string of at most "
                f"{MAX_IDENTITY_VALUE_LENGTH} characters"
            )
    missing = {
        key
        for key in required
        if key not in identity
    }
    if missing:
        errors.append(f"{path}: missing required identity fields: {sorted(missing)}")


def validate_tracker_work_item(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Closed-schema validation returning bounded remediation strings; an
    empty tuple means the payload is valid."""
    errors: list[str] = []
    unknown = set(payload) - ALLOWED_WORK_ITEM_FIELDS
    denylisted = unknown & DENYLISTED_WORK_ITEM_FIELDS
    for field_name in sorted(denylisted):
        errors.append(f"{field_name}: not permitted in the normalized tracker contract")
    for field_name in sorted(unknown - denylisted):
        errors.append(f"{field_name}: unknown tracker work item field")

    if payload.get("schema") != TRACKER_WORK_ITEM_SCHEMA:
        errors.append(f"schema: must be {TRACKER_WORK_ITEM_SCHEMA!r}")

    kind = payload.get("source_kind")
    if kind not in TRACKER_KINDS:
        errors.append(f"source_kind: must be one of {sorted(TRACKER_KINDS)}")

    identity = payload.get("identity")
    if not isinstance(identity, Mapping) or not identity:
        errors.append("identity: must be a non-empty mapping")
    elif kind == "github":
        _validate_identity_keys(
            identity,
            ALLOWED_GITHUB_IDENTITY_KEYS,
            REQUIRED_GITHUB_IDENTITY_KEYS,
            "identity",
            errors,
        )
        number = identity.get("number")
        valid_number = (
            isinstance(number, str)
            and number.isascii()
            and number.isdigit()
            and len(number) <= MAX_IDENTITY_VALUE_LENGTH
        )
        if valid_number:
            valid_number = int(number) >= 1
        if isinstance(number, str) and not valid_number:
            errors.append("identity.number: must be a positive integer string")
    elif kind == "jira_cloud":
        _validate_identity_keys(
            identity,
            ALLOWED_JIRA_IDENTITY_KEYS,
            REQUIRED_JIRA_IDENTITY_KEYS,
            "identity",
            errors,
        )

    url = payload.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        errors.append("url: must be a bounded HTTPS URL")
    else:
        try:
            parsed_url = urlparse(url)
            valid_url = (
                parsed_url.scheme == "https"
                and bool(parsed_url.netloc)
                and parsed_url.username is None
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            errors.append("url: must be a bounded HTTPS URL")

    for field_name in ("created_at", "updated_at"):
        value = payload.get(field_name)
        valid_timestamp = False
        if isinstance(value, str) and value and len(value) <= 64:
            try:
                parsed_timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                valid_timestamp = parsed_timestamp.tzinfo is not None
            except ValueError:
                pass
        if not valid_timestamp:
            errors.append(f"{field_name}: must be a bounded ISO 8601 timestamp with timezone")

    if payload.get("lifecycle_category") not in LIFECYCLE_CATEGORIES:
        errors.append(f"lifecycle_category: must be one of {sorted(LIFECYCLE_CATEGORIES)}")

    labels = payload.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str)
        and bool(label.strip())
        and len(label) <= MAX_LABEL_LENGTH
        and "\n" not in label
        and "\r" not in label
        for label in labels
    ):
        errors.append("labels: must be a list of strings")

    if not isinstance(payload.get("assigned"), bool):
        errors.append("assigned: must be true or false")

    provider_metadata = payload.get("provider_metadata")
    if not isinstance(provider_metadata, Mapping):
        errors.append("provider_metadata: must be a mapping")
    else:
        for key, value in provider_metadata.items():
            if key not in ALLOWED_PROVIDER_METADATA_KEYS:
                errors.append(f"provider_metadata.{key}: unknown provider metadata field")
            elif (
                not isinstance(value, str)
                or len(value) > MAX_PROVIDER_METADATA_VALUE_LENGTH
                or "\n" in value
                or "\r" in value
            ):
                errors.append(
                    f"provider_metadata.{key}: must be a short single-line string "
                    f"of at most {MAX_PROVIDER_METADATA_VALUE_LENGTH} characters"
                )

    return tuple(errors)


def _label_names(value: Any) -> list[str]:
    names: list[str] = []
    raw = value if isinstance(value, Sequence) and not isinstance(value, str) else []
    for item in raw:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return sorted(set(names))


def normalize_github_work_item(issue: Mapping[str, Any], *, repo: str) -> dict[str, Any]:
    """Pure mapping from a `gh issue list --json ...`-shaped item to the
    contract; no network call."""
    number = issue.get("number")
    try:
        number_text = str(int(number))
    except (TypeError, ValueError):
        number_text = ""
    return {
        "schema": TRACKER_WORK_ITEM_SCHEMA,
        "source_kind": "github",
        "identity": {"repo": repo, "number": number_text},
        "url": str(issue.get("url") or ""),
        "lifecycle_category": "done" if str(issue.get("state") or "").upper() == "CLOSED" else "new",
        "labels": _label_names(issue.get("labels")),
        "assigned": bool(issue.get("assignees")),
        "created_at": str(issue.get("createdAt") or issue.get("created_at") or ""),
        "updated_at": str(issue.get("updatedAt") or issue.get("updated_at") or ""),
        "provider_metadata": {},
    }
