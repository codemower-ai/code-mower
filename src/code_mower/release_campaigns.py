#!/usr/bin/env python3
"""Release campaign orchestrator for multi-provider release qualification."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import config as code_mower_config
    from code_mower import lane_status
    from code_mower.campaign_discovery import (
        discover_campaign_directories,
        list_discovered_campaigns,
        publish_campaigns_directory,
        resolve_command_campaigns_dir,
        resolve_repo_identity,
    )
    from code_mower.file_locks import FileLockError, exclusive_file_lock
    from code_mower.provider_registry import REFERENCE_PROVIDERS, ProviderLane
    from code_mower.release_qualify import (
        ADOPTION_RESULT_FIELDS,
        DEFAULT_PACKAGE_SOURCE,
        PRODUCTION_PYPI_INDEX_URL,
        TESTPYPI_INDEX_URL,
        _detect_host_class,
        _detect_runtime_class,
        _extract_package_identity,
        _parse_exact_package_spec,
        _validate_package_source,
        _validate_qualification_context,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        validate_adoption_result_payload,
    )
else:
    from . import config as code_mower_config
    from . import lane_status
    from .campaign_discovery import (
        discover_campaign_directories,
        list_discovered_campaigns,
        publish_campaigns_directory,
        resolve_command_campaigns_dir,
        resolve_repo_identity,
    )
    from .file_locks import FileLockError, exclusive_file_lock
    from .provider_registry import REFERENCE_PROVIDERS, ProviderLane
    from .release_qualify import (
        ADOPTION_RESULT_FIELDS,
        DEFAULT_PACKAGE_SOURCE,
        PRODUCTION_PYPI_INDEX_URL,
        TESTPYPI_INDEX_URL,
        _detect_host_class,
        _detect_runtime_class,
        _extract_package_identity,
        _parse_exact_package_spec,
        _validate_package_source,
        _validate_qualification_context,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        validate_adoption_result_payload,
    )

CAMPAIGN_SCHEMA = "code_mower.releaseCampaign.v1"
DISPATCH_SCHEMA = "code_mower.releaseCampaignDispatch.v1"
RESULT_MARKER_SCHEMA = "code_mower.releaseCampaignResult.v1"
TRIGGER_MARKER_SCHEMA = "code_mower.releaseCampaignTrigger.v1"
BOARD_RELEASE_CAMPAIGNS_SCHEMA = "code_mower.boardReleaseCampaigns.v1"
CAMPAIGN_WATCH_SCHEMA = "code_mower.releaseCampaignWatch.v1"
DEFAULT_CAMPAIGNS_RELATIVE_DIR = Path(".code-mower") / "campaigns"
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 900
# Margin between the outer campaign adapter timeout and the inner provider
# budget the maintained adapters enforce on their own subprocess. Keep in sync
# with campaign_adapters.INNER_TIMEOUT_MARGIN_SECONDS.
ADAPTER_INNER_TIMEOUT_MARGIN_SECONDS = 30
DEFAULT_WATCH_INTERVAL_SECONDS = 10.0
DEFAULT_WATCH_TIMEOUT_SECONDS = 600.0
DEFAULT_HOSTED_RESPONSE_TIMEOUT_SECONDS = 3600

# Campaign identifiers are storage keys: each one maps to exactly one file named
# `<campaign_id>.json`. The mapping is one-to-one and lossless -- no character is
# ever substituted -- so two different campaign ids can never name one file. The
# alphabet is deliberately narrow:
#   * lowercase ASCII only, because a campaign directory may live on a
#     case-insensitive volume -- APFS on macOS is case-insensitive by default --
#     where `Campaign-A` and `campaign-a` are the same file while the id lookup
#     would still treat them as two campaigns. Rejecting uppercase keeps ids
#     case-stable everywhere instead of colliding on some filesystems.
#   * letters, digits, `.`, `_`, `-` only, and a leading letter or digit. That
#     excludes `/`, `\`, and NUL (path traversal and separator injection), `.`
#     and `..` (directory self/parent references), and every dotfile spelling --
#     including the `.tmp.` write-staging prefix and the campaign directory lock
#     file, which therefore can never be addressed as a campaign.
#   * a bounded length, so an id can never exceed a filesystem's name limit and
#     be silently truncated into another campaign's filename.
CAMPAIGN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_CAMPAIGN_ID_LENGTH = 64
CAMPAIGNS_LOCK_FILENAME = ".campaigns.lock"
CAMPAIGN_TEMP_PREFIX = ".tmp."

DEFAULT_CAMPAIGN_PROVIDERS = (
    "claude",
    "codex",
    "antigravity",
    "muse",
    "cursor_cloud_agent",
    "devin",
)

PROVIDER_ALIAS_MAP: dict[str, str] = {
    "claude": "claude_audit",
    "claude_code": "claude_audit",
    "claude_audit": "claude_audit",
    "claude_review": "claude_review",
    "codex": "codex",
    "antigravity": "antigravity_cli",
    "agy": "antigravity_cli",
    "antigravity_cli": "antigravity_cli",
    "muse": "muse_cli",
    "muse_cli": "muse_cli",
    "cursor": "cursor_cloud_agent",
    "cursor_cloud_agent": "cursor_cloud_agent",
    "cursor_bugbot": "cursor_bugbot",
    "cursor_grok_bot": "cursor_bugbot",
    "grok_bot": "cursor_bugbot",
    "grok": "grok_build",
    "grok_build": "grok_build",
    "devin": "devin",
    "devin_cloud": "devin",
    "devin_cli": "devin_cli",
}

VALID_PROVIDER_STATES = {
    "queued",
    "running",
    "blocked",
    "unavailable",
    "complete",
}

VALID_PROVIDER_POSTURES = frozenset({"required", "informational"})
DEFAULT_PROVIDER_POSTURE = "required"


def _provider_posture(provider_data: Mapping[str, Any]) -> str:
    """Return closed provider posture ('required' or 'informational').

    Preserves compatibility: campaigns or provider entries without posture data
    treat the provider as required, exactly as current releases do.
    """
    posture = provider_data.get("posture")
    if isinstance(posture, str) and posture in VALID_PROVIDER_POSTURES:
        return posture
    return DEFAULT_PROVIDER_POSTURE


def _stored_provider_posture(provider_data: Mapping[str, Any]) -> str | None:
    """Return stored provider posture if explicitly present and valid, else None.

    Legacy campaign provider entries predating posture storage return None so
    their upload events preserve the exact pre-v1.0.9 event shape and
    deterministic event id.
    """
    posture = provider_data.get("posture")
    if isinstance(posture, str) and posture in VALID_PROVIDER_POSTURES:
        return posture
    return None

# Bounded, safe error codes. Persisted campaign state may only ever carry one
# of these values in the `error` field -- never a raw exception message, gh
# stdout/stderr, or adapter output. `_safe_error` enforces this at the source.
SAFE_ERROR_CODES = frozenset(
    {
        "",
        "command_not_found",
        "missing_credentials",
        "missing_issue_number",
        "no_campaign_adapter_configured",
        "adapter_configuration_invalid",
        "adapter_timeout",
        "adapter_exited_nonzero",
        "adapter_produced_no_result",
        "adapter_result_invalid",
        "adapter_result_mismatch",
        "campaign_identity_incomplete",
        "github_dispatch_failed",
        "github_poll_unavailable",
        "hosted_response_timeout",
        "hosted_result_rejected",
        "hosted_transport_unverified",
        "python_runtime_unavailable",
        "unknown_provider",
    }
)

# Errors that mean the adapter ran but produced something wrong -- these are
# real signal and require inspection, not just missing prerequisites.
_ADAPTER_ERROR_STATE = {
    "no_campaign_adapter_configured": "unavailable",
    "adapter_configuration_invalid": "unavailable",
    "command_not_found": "unavailable",
    "adapter_timeout": "unavailable",
    "python_runtime_unavailable": "unavailable",
    "adapter_exited_nonzero": "blocked",
    "adapter_produced_no_result": "blocked",
    "adapter_result_invalid": "blocked",
    "adapter_result_mismatch": "blocked",
}

# A result marker is emitted, and documented, as a *single-line* HTML comment
# wrapping exactly one JSON object. The pattern therefore anchors the complete
# marker line and captures greedily through the last closing brace on it, so the
# capture always ends at the JSON object's own final brace. The previous lazy,
# DOTALL pattern ended the capture at the first `}` that happened to be followed
# by `-->`, which a `}-->` sequence inside a permitted JSON *string* value could
# supply: a correctly bound, trusted result was then truncated into unparseable
# JSON and silently discarded. DOTALL is deliberately absent and both ends are
# anchored, so a marker can neither run past its own line nor pick up trailing
# text; a malformed marker still fails closed at the `json.loads` below.
RESULT_MARKER_RE = re.compile(
    r"^[^\S\n]*<!--[^\S\n]*CODE_MOWER_ADOPTION_RESULT:[^\S\n]*"
    r"(\{.*\})"
    r"[^\S\n]*-->[^\S\n]*$",
    re.MULTILINE,
)

# Argv-only adapter invocation: (argv, timeout_seconds) -> CompletedProcess.
# Never invoked with shell=True; stdout/stderr are read for diagnosis only
# and are never persisted into campaign state.
AdapterRunner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


def _safe_error(code: str) -> str:
    if code not in SAFE_ERROR_CODES:
        raise ValueError(f"unregistered campaign error code: {code!r}")
    return code


def run_local_adapter_command(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Default argv-only adapter runner. No shell, no shared environment mutation."""
    return subprocess.run(list(argv), check=False, text=True, capture_output=True, timeout=timeout)


@dataclass
class CampaignProvider:
    """Metadata-only state for one campaign provider participant."""

    provider: str
    lane_id: str
    driver: str
    state: str
    environment: str
    elapsed_seconds: float
    idempotency_key: str
    posture: str = "required"
    dispatch_mode: str = "dry_run"
    dispatched_at: str | None = None
    completed_at: str | None = None
    response_deadline_at: str | None = None
    transport_verified: bool | None = None
    # Set before invoking a paid/hosted dispatch or local adapter (even if it
    # fails or the outcome is uncertain). Once set, resume never repeats the
    # attempt automatically -- only an explicit --retry-provider does. A hosted
    # dispatch stamps it together with `state="running"` and the issue identity
    # in `dispatch_ref`, so an attempt interrupted mid-post is left pollable
    # rather than stalled (see `dispatch_or_advance_campaign`).
    attempted_at: str | None = None
    next_action: str = ""
    next_detail: str = ""
    error: str = ""
    dispatch_ref: dict[str, Any] = field(default_factory=dict)
    adoption_result: dict[str, Any] | None = None
    # Bounded metadata-only summaries of superseded attempts (see
    # MAX_ATTEMPT_HISTORY_ENTRIES). Stored campaigns written before this field
    # existed lack the key; readers must use .get with a default, never direct
    # indexing.
    attempt_history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state not in VALID_PROVIDER_STATES:
            self.state = "unavailable"
        if self.posture not in VALID_PROVIDER_POSTURES:
            self.posture = "required"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReleaseCampaign:
    """Versioned metadata-only release qualification campaign."""

    schema: str
    campaign_id: str
    release_tag: str
    package_identity: str
    package_spec: str
    normalized_version: str
    qualification_context: str
    starting_version: str
    package_source: str
    repo_slug: str
    status: str
    dry_run: bool
    elapsed_seconds: float
    created_at: str
    updated_at: str
    next_action: str
    next_detail: str
    providers: list[dict[str, Any]]
    provider_posture_configured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# How many campaign ids an ambiguous-release-tag error may name before it
# degrades to a count. Keeps the message bounded no matter how many campaign
# files share a tag.
AMBIGUOUS_RELEASE_TAG_ID_LIMIT = 4


def default_campaigns_dir(repo_path: Path | str = ".") -> Path:
    return Path(repo_path) / DEFAULT_CAMPAIGNS_RELATIVE_DIR


def is_valid_campaign_id(campaign_id: Any) -> bool:
    """Report whether ``campaign_id`` is inside the documented storage-safe alphabet."""
    return (
        isinstance(campaign_id, str)
        and 0 < len(campaign_id) <= MAX_CAMPAIGN_ID_LENGTH
        and CAMPAIGN_ID_PATTERN.fullmatch(campaign_id) is not None
    )


def validate_campaign_id(campaign_id: Any) -> str:
    """Return ``campaign_id`` unchanged, or raise ``ValueError`` with a bounded message.

    Rejecting out-of-alphabet ids here -- before any lookup, save, or mutation --
    is what makes the id-to-filename mapping one-to-one. The previous behavior
    substituted unsupported characters, so ``campaign/a`` and ``campaign_a``
    both addressed ``campaign_a.json``: naming one could silently load, advance,
    and overwrite the other's campaign. Nothing is sanitized now; an id is
    either usable verbatim as a filename stem or refused.
    """
    if not isinstance(campaign_id, str) or not campaign_id:
        raise ValueError("campaign_id must be a non-empty string")
    if len(campaign_id) > MAX_CAMPAIGN_ID_LENGTH:
        raise ValueError(
            f"campaign_id must be at most {MAX_CAMPAIGN_ID_LENGTH} characters"
        )
    if CAMPAIGN_ID_PATTERN.fullmatch(campaign_id) is None:
        raise ValueError(
            "campaign_id must use only lowercase ASCII letters, digits, '.', '_', "
            "and '-', and must start with a letter or digit"
        )
    return campaign_id


def campaign_filename(campaign_id: str) -> str:
    """Map a validated campaign id to its one and only storage filename."""
    return f"{validate_campaign_id(campaign_id)}.json"


def resolve_provider_lane(name: str) -> tuple[str, ProviderLane]:
    """Resolve a provider alias to its canonical key and declarative lane configuration.

    Fails closed: a name that is not present in ``PROVIDER_ALIAS_MAP`` or the
    ``REFERENCE_PROVIDERS`` registry raises ``ValueError`` rather than
    fabricating a manual fallback lane for an unrecognized provider.
    """
    normalized = name.strip().lower()
    target_lane_id = PROVIDER_ALIAS_MAP.get(normalized, normalized)
    lane = REFERENCE_PROVIDERS.get(target_lane_id)
    if lane is None:
        known = ", ".join(sorted(set(PROVIDER_ALIAS_MAP) | set(REFERENCE_PROVIDERS)))
        raise ValueError(f"unknown release campaign provider {name!r}; known providers: {known}")
    return lane.provider, lane


def _compute_idempotency_key(
    campaign_id: str,
    provider: str,
    release_tag: str,
    qualification_context: str,
    starting_version: str = "",
    package_source: str = DEFAULT_PACKAGE_SOURCE,
) -> str:
    seed = (
        f"{campaign_id}:{provider}:{release_tag}:{qualification_context}:"
        f"{starting_version}:{package_source}"
    ).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:16]


def _detect_environment() -> str:
    host_class = _detect_host_class()
    runtime_class = _detect_runtime_class()
    return f"{host_class}/{runtime_class}"


def _find_command(
    lane: ProviderLane,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> str | None:
    config = lane.provider_config
    cmd = config.get("command") or lane.provider
    if which_fn(cmd):
        return cmd
    for alt in config.get("alternate_commands", ()):
        if which_fn(alt):
            return alt
    return None


def _check_credentials(
    lane: ProviderLane,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    current_env = os.environ if env is None else env
    if lane.token_env:
        found = any(current_env.get(token) for token in lane.token_env)
        if not found:
            return False, lane.token_env[0]
    required_any = lane.provider_config.get("required_env_any", ())
    if required_any and not any(current_env.get(var) for var in required_any):
        return False, required_any[0]
    return True, ""


def _check_hosted_transport(
    lane: ProviderLane,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Read an explicit acknowledgement when GitHub cannot verify an App transport."""
    variable = str(lane.provider_config.get("campaign_transport_ready_env") or "")
    if not variable:
        return True, ""
    current_env = os.environ if env is None else env
    ready = str(current_env.get(variable) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return ready, variable


def _configured_hosted_response_timeout(lane: ProviderLane) -> int | None:
    value = lane.provider_config.get(
        "campaign_response_timeout_seconds",
        DEFAULT_HOSTED_RESPONSE_TIMEOUT_SECONDS,
    )
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _hosted_response_timeout(lane: ProviderLane) -> int:
    return _configured_hosted_response_timeout(lane) or DEFAULT_HOSTED_RESPONSE_TIMEOUT_SECONDS


# Closed hosted dispatch profile: the five independent readiness checks a
# hosted (`hosted_bridge`/`saas_event`) release qualification must pass
# before a paid dispatch. Each check is judged on its own signal so one
# verified dimension can never mask another:
#   * auth: a dispatch token is present (comment permission).
#   * installation: the provider App is installed for the repo and has been
#     seen answering campaign issue comments, acknowledged explicitly via the
#     lane's `campaign_transport_ready_env`. Token presence alone proves
#     nothing about the App.
#   * trigger: the lane declares the provider's real builder trigger text, so
#     the dispatch comment tells the remote runner exactly what to watch for.
#   * trusted_responder: at least one GitHub login is trusted to post the
#     adoption-result marker, so a reply can be authenticated.
#   * result_return: the bounded response wait is configured, so provider
#     silence degrades to `hosted_response_timeout` evidence instead of an
#     unbounded hang.
HOSTED_DISPATCH_PROFILE_CHECKS = (
    "auth",
    "installation",
    "trigger",
    "trusted_responder",
    "result_return",
)


def hosted_dispatch_profile(
    lane: ProviderLane,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate the closed hosted dispatch profile for one lane.

    Returns one ``{"ready": bool, "detail": str, "remediation": str}`` entry
    per check in :data:`HOSTED_DISPATCH_PROFILE_CHECKS`. Details and
    remediations are bounded metadata only: provider/lane names and
    environment variable names, never secret values, paths, or output.
    """
    current_env = os.environ if env is None else env
    profile: dict[str, dict[str, Any]] = {}

    has_creds, missing_cred = _check_credentials(lane, env=current_env)
    profile["auth"] = {
        "ready": has_creds,
        "detail": "dispatch token present" if has_creds else "dispatch token missing",
        "remediation": (
            ""
            if has_creds
            else f"set {missing_cred} in the environment for {lane.provider} campaign dispatch"
        ),
    }

    transport_ready, transport_var = _check_hosted_transport(lane, env=current_env)
    if not transport_var:
        profile["installation"] = {
            "ready": True,
            "detail": "no separate installation acknowledgement configured",
            "remediation": "",
        }
    else:
        profile["installation"] = {
            "ready": transport_ready,
            "detail": (
                "provider App installation verified"
                if transport_ready
                else "provider App installation not verified"
            ),
            "remediation": (
                ""
                if transport_ready
                else (
                    f"verify the {lane.provider} GitHub App answers campaign "
                    f"issue comments, then set {transport_var}=1"
                )
            ),
        }

    trigger_comments = tuple(lane.provider_config.get("trigger_comments") or ())
    trigger_ready = bool(trigger_comments)
    profile["trigger"] = {
        "ready": trigger_ready,
        "detail": (
            "builder trigger configured"
            if trigger_ready
            else "no builder trigger configured"
        ),
        "remediation": (
            ""
            if trigger_ready
            else (
                f"configure the {lane.provider} builder trigger before dispatching "
                f"release qualification"
            )
        ),
    }

    trusted_authors = _resolve_trusted_bot_authors(lane, env=current_env)
    responder_ready = bool(trusted_authors)
    profile["trusted_responder"] = {
        "ready": responder_ready,
        "detail": (
            "trusted responder allowlist configured"
            if responder_ready
            else "no trusted responder configured"
        ),
        "remediation": (
            ""
            if responder_ready
            else (
                f"configure bot_authors for {lane.provider} so adoption-result "
                f"replies can be authenticated"
            )
        ),
    }

    timeout_ready = _configured_hosted_response_timeout(lane) is not None
    profile["result_return"] = {
        "ready": timeout_ready,
        "detail": (
            "bounded result-return wait configured"
            if timeout_ready
            else "result-return wait is not a positive integer"
        ),
        "remediation": (
            ""
            if timeout_ready
            else (
                f"configure a positive campaign_response_timeout_seconds for "
                f"{lane.provider} so silence becomes timeout evidence"
            )
        ),
    }
    return profile


def hosted_dispatch_blockers(profile: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Name the dispatch-profile checks that are not ready, in closed order."""
    return [name for name in HOSTED_DISPATCH_PROFILE_CHECKS if not profile.get(name, {}).get("ready")]


def _response_deadline(started_at: str, timeout_seconds: int) -> str:
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        started = datetime.now(UTC).replace(microsecond=0)
    return (started + timedelta(seconds=timeout_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response_deadline_expired(deadline: Any, now_utc: str) -> bool:
    if not isinstance(deadline, str) or not deadline:
        return False
    try:
        return datetime.strptime(now_utc, "%Y-%m-%dT%H:%M:%SZ") >= datetime.strptime(
            deadline, "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        return False


def _failure_reason_remediation(failure_reason: str) -> str:
    """Return safe, actionable remediation for a package_install failure reason."""
    remediation_map = {
        "network": "retry after verifying network connectivity and DNS resolution",
        "package_index": "retry after index propagation or verify package is published",
        "runtime": "verify Python version compatibility and system dependencies",
        "sandbox_permission": "check disk space, filesystem permissions, and sandbox config",
        "unknown": "rerun qualification in provider environment and inspect local diagnostics",
    }
    return remediation_map.get(failure_reason, "rerun in provider environment")


def _extract_failure_detail(result: Mapping[str, Any] | None) -> str:
    """Extract failure_reason and remediation from a blocked adoption result."""
    if not isinstance(result, Mapping):
        return ""
    outcome = result.get("outcome")
    if outcome not in {"fail", "incomplete"}:
        return f"outcome: {outcome}"

    # Look for package_install step with failure_reason
    steps = result.get("steps", [])
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("id") == "package_install":
                failure_reason = step.get("failure_reason")
                if failure_reason:
                    remediation = _failure_reason_remediation(failure_reason)
                    return f"outcome: {outcome}, package_install: {failure_reason} ({remediation})"

    return f"outcome: {outcome}"


def _provider_next_action(
    provider: str,
    lane: ProviderLane,
    state: str,
    *,
    command_available: bool,
    has_credentials: bool,
    has_issue: bool,
    dry_run: bool,
    adapter_configured: bool = True,
    error: str = "",
    error_code: str = "",
) -> tuple[str, str]:
    if state == "complete":
        return "none", ""
    if state == "blocked":
        return f"inspect {provider} qualification failures", error
    if state == "running":
        if lane.driver in {"saas_event", "hosted_bridge"}:
            return f"poll {provider} remote progress marker", ""
        return f"poll {provider} local process", ""
    if state == "unavailable":
        if lane.driver == "local_cli" and not adapter_configured:
            return (
                f"record manual result for {provider}",
                "no campaign adapter configured",
            )
        if lane.driver == "local_cli" and (
            error_code == "python_runtime_unavailable"
            or error == "python_runtime_unavailable"
        ):
            return (
                "install Python 3.12+ on PATH or set CODE_MOWER_PYTHON",
                error if error != "python_runtime_unavailable" else "supported Python 3.12+ runtime is unavailable",
            )
        if lane.driver == "local_cli" and not command_available:
            cmd = lane.provider_config.get("command") or provider
            return f"install {cmd} CLI on PATH or record manual result", error or f"command not found: {cmd}"
        if lane.driver in {"hosted_bridge", "saas_event"} and not has_credentials:
            token = error or (lane.token_env[0] if lane.token_env else "credentials")
            return f"set {token} or record manual result", error
        if lane.driver in {"saas_event", "hosted_bridge"} and not has_issue:
            return f"provide GitHub issue number via --issue for {provider} dispatch", error
        return f"configure {provider} prerequisites or record manual result", error
    if state == "queued":
        if dry_run:
            if lane.driver == "local_cli":
                return f"run with --apply to execute {provider} qualification", ""
            if lane.driver == "saas_event":
                return f"run with --apply to dispatch {provider} via GitHub comment", ""
            if lane.driver == "hosted_bridge":
                return f"run with --apply to dispatch paid remote {provider} qualification", ""
            return f"run with --apply to execute {provider}", ""
        return f"dispatch {provider}", ""
    return f"inspect {provider}", error


def _aggregate_campaign_status(
    providers: Sequence[dict[str, Any]],
    *,
    dry_run: bool,
) -> tuple[str, str, str]:
    if not providers:
        return "queued", "add providers to campaign", ""

    req = [p for p in providers if _provider_posture(p) == "required"]
    info = [p for p in providers if _provider_posture(p) == "informational"]

    if not info:
        blocked = [p["provider"] for p in providers if p.get("state") == "blocked"]
        running = [p["provider"] for p in providers if p.get("state") == "running"]
        queued = [p["provider"] for p in providers if p.get("state") == "queued"]
        unavailable = [p["provider"] for p in providers if p.get("state") == "unavailable"]
        complete = [p["provider"] for p in providers if p.get("state") == "complete"]

        if blocked:
            return (
                "blocked",
                f"inspect qualification failures for {', '.join(blocked)}",
                f"{len(blocked)} provider(s) failed qualification checks",
            )
        if running:
            return (
                "running",
                f"poll running providers: {', '.join(running)}",
                f"{len(running)} provider(s) currently running",
            )
        if len(complete) == len(providers):
            return (
                "complete",
                "campaign complete; all providers passed",
                f"all {len(complete)} provider(s) qualified successfully",
            )
        # "queued" is a claim that applying would dispatch something, so it is only
        # honest while at least one provider is actually dispatchable. A dry run is
        # not an exception: previewing every provider as unavailable and still
        # advising "run with --apply" points at a command that cannot dispatch
        # anything. Report the prerequisite work instead -- which covers a missing
        # issue number, repo slug, credentials and adapter configuration alike,
        # because each of those already lands its provider in "unavailable".
        if queued:
            if dry_run:
                return (
                    "queued",
                    "run with --apply to dispatch providers",
                    f"dry-run preview with {len(queued)} queued and {len(unavailable)} unavailable provider(s)",
                )
            return (
                "queued",
                f"dispatch queued providers: {', '.join(queued)}",
                f"{len(queued)} provider(s) waiting for dispatch",
            )
        if unavailable and len(unavailable) == len(providers) - len(complete):
            return (
                "unavailable",
                f"configure prerequisites for unavailable providers: {', '.join(unavailable)}",
                f"{len(unavailable)} provider(s) unavailable",
            )
        return "queued", "inspect campaign providers", ""

    req_blocked = [p["provider"] for p in req if p.get("state") == "blocked"]
    req_running = [p["provider"] for p in req if p.get("state") == "running"]
    req_queued = [p["provider"] for p in req if p.get("state") == "queued"]
    req_unavailable = [p["provider"] for p in req if p.get("state") == "unavailable"]
    req_complete = [p["provider"] for p in req if p.get("state") == "complete"]

    info_blocked = [p["provider"] for p in info if p.get("state") == "blocked"]
    info_running = [p["provider"] for p in info if p.get("state") == "running"]
    info_queued = [p["provider"] for p in info if p.get("state") == "queued"]
    info_unavailable = [p["provider"] for p in info if p.get("state") == "unavailable"]
    info_complete = [p["provider"] for p in info if p.get("state") == "complete"]

    if req_blocked:
        return (
            "blocked",
            f"inspect qualification failures for required provider(s): {', '.join(req_blocked)}",
            f"blocked required evidence: {len(req_blocked)} required provider(s) failed qualification checks ({', '.join(req_blocked)})",
        )
    if not dry_run and req_unavailable:
        return (
            "blocked",
            f"configure prerequisites for unavailable required provider(s): {', '.join(req_unavailable)}",
            f"blocked required evidence: {len(req_unavailable)} required provider(s) unavailable ({', '.join(req_unavailable)})",
        )
    if dry_run and len(req_unavailable) == len(req):
        return (
            "unavailable",
            f"configure prerequisites for unavailable required provider(s): {', '.join(req_unavailable)}",
            f"blocked required evidence: all {len(req_unavailable)} required provider(s) unavailable ({', '.join(req_unavailable)})",
        )
    if req_running:
        running_names = req_running + info_running
        return (
            "running",
            f"poll running providers: {', '.join(running_names)}",
            f"waiting required evidence: {len(req_running)} required provider(s) currently running ({', '.join(req_running)})",
        )
    if req_queued:
        if dry_run:
            return (
                "queued",
                "run with --apply to dispatch providers",
                f"waiting required evidence: dry-run preview with {len(req_queued)} queued and {len(req_unavailable)} unavailable required provider(s)",
            )
        queued_names = req_queued + info_queued
        return (
            "queued",
            f"dispatch queued providers: {', '.join(queued_names)}",
            f"waiting required evidence: {len(req_queued)} required provider(s) waiting for dispatch",
        )
    if len(req_complete) == len(req):
        if info_running:
            return (
                "running",
                f"poll running informational providers: {', '.join(info_running)}",
                f"required providers passed; {len(info_running)} informational provider(s) currently running",
            )
        if info_queued:
            if dry_run:
                return (
                    "queued",
                    "run with --apply to dispatch providers",
                    f"required providers passed; dry-run preview with {len(info_queued)} queued and {len(info_unavailable)} unavailable informational provider(s)",
                )
            return (
                "queued",
                f"dispatch queued informational providers: {', '.join(info_queued)}",
                f"required providers passed; {len(info_queued)} informational provider(s) waiting for dispatch",
            )
        info_findings = info_blocked + info_unavailable
        if info_findings:
            return (
                "complete",
                "campaign complete with informational findings; required providers passed",
                f"success with informational findings: all {len(req_complete)} required provider(s) passed; "
                f"{len(info_findings)} informational provider(s) reported findings ({', '.join(info_findings)})",
            )
        return (
            "complete",
            "campaign complete; all providers passed",
            f"all {len(req_complete) + len(info_complete)} provider(s) qualified successfully",
        )
    if req_unavailable:
        if dry_run:
            return (
                "unavailable",
                f"configure prerequisites for unavailable required provider(s): {', '.join(req_unavailable)}",
                f"blocked required evidence: {len(req_unavailable)} required provider(s) unavailable ({', '.join(req_unavailable)})",
            )
        return (
            "blocked",
            f"configure prerequisites for unavailable required provider(s): {', '.join(req_unavailable)}",
            f"blocked required evidence: {len(req_unavailable)} required provider(s) unavailable ({', '.join(req_unavailable)})",
        )
    return "queued", "inspect campaign providers", ""


@contextmanager
def locked_campaigns_dir(
    campaigns_dir: Path,
    *,
    timeout_seconds: float = 900.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[IO[str]]:
    """Hold an exclusive advisory lock over one campaign directory.

    Every *mutating* campaign command serializes on this lock across its whole
    read-decide-invoke-persist sequence, so two concurrent invocations can never
    both observe a provider as un-attempted and both run its local adapter or
    post its paid/hosted dispatch. Read-only status requests and the Board
    projection do not take it: they only read campaign files, which are
    published by atomic rename, so they stay answerable while a long applied run
    holds the lock and against a read-only campaign directory.

    The lock is an exclusive OS lock on a dedicated file, taken through
    :func:`code_mower.file_locks.exclusive_file_lock` and shared with
    ``board_store._locked_store``. Whichever backend that picks -- POSIX
    ``flock`` or a Windows byte-range lock -- the OS releases it when the
    holding file descriptor closes, including on an uncaught exception or an
    abrupt process exit, so there is no stale-lock protocol, no owner/pid
    bookkeeping, and no lease to renew. A crashed holder blocks nobody.

    The lock file's name starts with a dot, so it is neither matched by the
    ``*.json`` campaign scan nor addressable as a campaign id.
    """
    campaigns_dir.mkdir(parents=True, exist_ok=True)
    lock_path = campaigns_dir / CAMPAIGNS_LOCK_FILENAME
    with exclusive_file_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        monotonic=monotonic,
    ) as lock_file:
        yield lock_file


def save_campaign(
    campaign: ReleaseCampaign | dict[str, Any],
    campaigns_dir: Path,
) -> Path:
    campaigns_dir.mkdir(parents=True, exist_ok=True)
    payload = campaign.to_dict() if isinstance(campaign, ReleaseCampaign) else campaign
    filename = campaign_filename(payload["campaign_id"])
    target_path = campaigns_dir / filename
    # Stage into a name unique to this write. A single shared `.tmp.<name>`
    # staging path is itself a collision point: two writers -- a locked campaign
    # command and an unrelated direct `save_campaign` call, or two direct calls
    # -- would interleave their partial writes into one file and then both
    # rename it, so the survivor could be a torn blend of two payloads. Readers
    # stay safe either way because the publish step is a single atomic
    # `os.replace`, so a campaign file is never observed half-written.
    temp_target = campaigns_dir / f"{CAMPAIGN_TEMP_PREFIX}{uuid.uuid4().hex}.{filename}"
    try:
        with temp_target.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(temp_target, target_path)
    except BaseException:
        temp_target.unlink(missing_ok=True)
        raise
    return target_path


def load_campaign_by_id(
    campaign_id: str,
    campaigns_dir: Path,
) -> dict[str, Any] | None:
    """Resolve the campaign stored under exactly ``campaign_id``.

    A campaign id is a storage key that maps one-to-one onto
    ``<campaign_id>.json``, so this reads that one canonical filename and
    nothing else -- no directory scan, no fallback.

    Two exactness rules make the answer unambiguous:

    * **Only the canonical file is consulted.** An earlier dual-purpose lookup
      accepted an id *or* a release tag and, when the named file was missing,
      scanned the directory matching either the stored ``campaign_id`` or the
      stored ``release_tag``. So ``--campaign-id v1.0.0``, with no
      ``v1.0.0.json`` on disk, would silently resolve to whatever campaign
      carried ``release_tag: v1.0.0`` -- an id the caller named explicitly
      answered with a campaign filed under a different id. An explicit id that
      names no stored campaign must report exactly that.
    * **The stored field must agree.** The file is authoritative about its own
      identity, so ``campaign_id`` inside it must equal the request. A file
      whose stem and stored id disagree (hand-edited, restored from elsewhere,
      or copied) is not this campaign, and returning it would let a caller
      advance, dispatch, or report one campaign while naming another.

    An invalid id can address no file at all, so it resolves to nothing.
    """
    if not campaigns_dir.is_dir() or not is_valid_campaign_id(campaign_id):
        return None
    path = campaigns_dir / campaign_filename(campaign_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != CAMPAIGN_SCHEMA:
        return None
    if data.get("campaign_id") != campaign_id:
        return None
    return data


def load_campaign_by_release_tag(
    release_tag: str,
    campaigns_dir: Path,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the single campaign whose stored ``release_tag`` is exactly ``release_tag``.

    Returns ``(campaign, error)``. ``error`` is empty both when one campaign
    matches and when none does; it is non-empty only when the tag is ambiguous.

    This is deliberately *not* :func:`load_campaign_by_id`. A tag that happens
    to also be a well-formed campaign id (``v1.0.0`` is one) would resolve
    through that function to ``v1.0.0.json`` -- whatever campaign a custom
    ``--campaign-id`` had stored there, even when it is for an entirely
    different release. A tag-only request must never be answered with another
    release's state, so this lookup ignores filenames entirely and matches only
    on the stored ``release_tag`` field.

    Nothing here selects between several matches. Campaign ids map one-to-one
    onto files, but a custom ``--campaign-id`` lets two campaigns carry the same
    release tag, and picking one of them would depend on directory order --
    silently advancing, dispatching, or reporting an arbitrary campaign. The
    ambiguity is reported instead, bounded (a fixed number of ids at most), and
    the caller is told to name the campaign with ``--campaign-id``.
    """
    if not release_tag or not campaigns_dir.is_dir():
        return None, ""

    matches: list[dict[str, Any]] = []
    for entry in sorted(campaigns_dir.glob("*.json")):
        if entry.name.startswith(CAMPAIGN_TEMP_PREFIX):
            continue
        try:
            with entry.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("schema") != CAMPAIGN_SCHEMA:
            continue
        if data.get("release_tag") != release_tag:
            continue
        matches.append(data)

    if not matches:
        return None, ""
    if len(matches) == 1:
        return matches[0], ""

    # Only well-formed ids are named back, and only a few of them: a stored file
    # is untrusted input, so neither its count nor its contents may widen this
    # message beyond a bounded, path-free line.
    named = sorted(
        str(c.get("campaign_id"))
        for c in matches
        if is_valid_campaign_id(c.get("campaign_id"))
    )
    listed = ", ".join(named[:AMBIGUOUS_RELEASE_TAG_ID_LIMIT])
    if not listed:
        detail = ""
    elif len(named) > AMBIGUOUS_RELEASE_TAG_ID_LIMIT:
        detail = f" ({listed}, ...)"
    else:
        detail = f" ({listed})"
    return None, (
        f"release tag {release_tag!r} matches {len(matches)} campaigns{detail}; "
        "name the one you mean with --campaign-id"
    )


def list_campaigns(campaigns_dir: Path) -> list[dict[str, Any]]:
    if not campaigns_dir.is_dir():
        return []
    campaigns: list[dict[str, Any]] = []
    for entry in campaigns_dir.glob("*.json"):
        if entry.name.startswith(CAMPAIGN_TEMP_PREFIX):
            continue
        try:
            with entry.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and data.get("schema") == CAMPAIGN_SCHEMA:
                    campaigns.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    campaigns.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
    return campaigns


def campaign_package_identity(package_spec: str) -> str:
    """The normalized package name every result for this campaign must report.

    Derived from the campaign's own exact ``package_spec`` rather than assumed
    to be Code Mower: the campaign command deliberately accepts exact package
    specs, so binding a result to a hard-coded package would both accept a
    result for the wrong distribution and refuse every legitimate campaign for
    another one.

    Returns ``""`` when the stored spec is not an exact package-index spec --
    which `initialize_campaign` never produces, so it means a hand-edited or
    corrupted campaign file. Callers treat that as "nothing can be bound" and
    fail closed rather than falling back to an unbound comparison.
    """
    try:
        return _extract_package_identity(package_spec)
    except ValueError:
        return ""


def _load_bound_result_file(
    path: Path,
    *,
    provider: str,
    release_tag: str,
    qualification_context: str,
    starting_version: str,
    package_identity: str,
) -> dict[str, Any] | None:
    """Load and strictly validate a local adoptionResult file bound to this campaign.

    Identity binding covers provider, release_tag, and package_identity as well
    as qualification_context and starting_version -- a cold-install result must
    not be accepted for an upgrade campaign (or vice versa), an upgrade result
    must match this campaign's exact starting_version, not just any upgrade, and
    a result for another distribution must never qualify this campaign's
    package.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            candidate = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not is_bound_adoption_result(
        candidate,
        provider=provider,
        release_tag=release_tag,
        qualification_context=qualification_context,
        starting_version=starting_version,
        package_identity=package_identity,
    ):
        return None
    return candidate


def is_bound_adoption_result(
    candidate: Any,
    *,
    provider: str,
    release_tag: str,
    qualification_context: str,
    starting_version: str,
    package_identity: str,
) -> bool:
    """Report whether an adoption result passes schema *and* campaign identity binding.

    The one place the binding rules are written down, so a result loaded from a
    drop-in file, and a result already stored in campaign state and revalidated
    later (before it is converted into a cloud event), are held to exactly the
    same contract. An empty ``package_identity`` means the campaign's own spec
    binds nothing, so nothing can be bound to it: that fails closed.
    """
    if not package_identity or not isinstance(candidate, dict):
        return False
    try:
        validate_adoption_result_payload(
            candidate, expected_package_identity=package_identity
        )
    except (TypeError, ValueError):
        return False
    return not (
        candidate.get("release_tag") != release_tag
        or candidate.get("provider") != provider
        or candidate.get("qualification_context") != qualification_context
        or candidate.get("starting_version") != starting_version
    )


def _adoption_result_rejection_detail(exc: ValueError) -> str:
    """Return a bounded, field-level reason for a closed adoption result rejection.

    This only consumes the structured ValueError from
    validate_adoption_result_payload, so it never echoes raw comment text,
    marker bodies, or unbounded provider values.
    """
    message = str(exc)
    # Match the longest field names first to avoid substring false positives
    # (e.g., matching "out" before "outcome").
    known_fields = sorted(ADOPTION_RESULT_FIELDS, key=len, reverse=True)
    for field_name in known_fields:
        if field_name in message:
            return f"adoption result field '{field_name}' rejected"
    if "unsupported field" in message:
        return "adoption result has unsupported field"
    if "missing required field" in message:
        return "adoption result missing required field"
    if "schema" in message:
        return "adoption result schema rejected"
    if "steps" in message:
        return "adoption result steps rejected"
    return "adoption result rejected"


def _adoption_result_binding_detail(
    adoption_result: Mapping[str, Any],
    *,
    provider: str,
    release_tag: str,
    qualification_context: str,
    starting_version: str,
) -> str:
    """Return a bounded, field-level reason for a campaign-binding mismatch.

    The wrapper already matched this campaign, so a mismatch here means the
    embedded adoption_result claims a different provider, release,
    qualification context, or starting version than the dispatch it is wrapped
    in. Only the bounded field name is surfaced; the actual and expected values
    are never logged or persisted.
    """
    checks = (
        ("provider", adoption_result.get("provider"), provider),
        ("release_tag", adoption_result.get("release_tag"), release_tag),
        (
            "qualification_context",
            adoption_result.get("qualification_context"),
            qualification_context,
        ),
        (
            "starting_version",
            str(adoption_result.get("starting_version") or ""),
            starting_version,
        ),
    )
    for field_name, actual, expected in checks:
        if actual != expected:
            return f"adoption result field '{field_name}' rejected"
    return "adoption result binding rejected"


def _extract_bound_adoption_result_ex(
    text: str,
    *,
    campaign_id: str,
    provider: str,
    release_tag: str,
    idempotency_key: str,
    qualification_context: str,
    starting_version: str,
    package_identity: str,
    package_source: str = DEFAULT_PACKAGE_SOURCE,
) -> tuple[dict[str, Any] | None, str]:
    """Extract an adoptionResult from a GitHub comment, requiring explicit identity binding.

    A bare adoptionResult JSON blob is never accepted: the comment must wrap
    it in a RESULT_MARKER_SCHEMA envelope whose campaign_id, provider,
    release_tag, and idempotency_key match this exact dispatch -- otherwise a
    stale or unrelated comment could be replayed to fabricate completion. The
    idempotency_key alone is not sufficient binding for qualification_context
    and starting_version: it is generated once at campaign creation and never
    reproduced independently here, so the embedded adoption_result's own
    qualification_context and starting_version are checked directly against
    the campaign's expected values -- a cold-install result can never
    complete an upgrade campaign, and an upgrade result must match this
    campaign's exact starting_version, even if a wrapper key were copied or
    generated incorrectly. The embedded result's ``package_identity`` is bound
    the same way, against the identity derived from the campaign's own exact
    package spec, so a result for another distribution can never complete it.
    The wrapper's own ``package_source`` (missing treated as ``pypi``, the
    legacy default) is likewise checked directly against the campaign's
    expected source: the closed adoptionResult schema carries no source field
    of its own to cross-check, so this is the one place that binding is
    enforced.

    Returns a ``(result, rejection_detail)`` pair. ``result`` is the validated
    adoption result, or ``None`` if no valid marker was found. ``rejection_detail``
    is a bounded, field-level reason when a trusted, correctly bound marker was
    present but its adoption result failed closed validation, or an empty string
    when no such marker was present.
    """
    if not package_identity:
        return None, ""
    last_rejection_detail = ""
    for match in RESULT_MARKER_RE.finditer(text):
        try:
            wrapper = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(wrapper, dict) or wrapper.get("schema") != RESULT_MARKER_SCHEMA:
            continue
        if (
            wrapper.get("campaign_id") != campaign_id
            or wrapper.get("provider") != provider
            or wrapper.get("release_tag") != release_tag
            or wrapper.get("idempotency_key") != idempotency_key
            or str(wrapper.get("package_source") or DEFAULT_PACKAGE_SOURCE) != package_source
        ):
            continue
        adoption_result = wrapper.get("adoption_result")
        if not isinstance(adoption_result, dict):
            last_rejection_detail = "adoption result missing"
            continue
        if (
            adoption_result.get("provider") != provider
            or adoption_result.get("release_tag") != release_tag
            or adoption_result.get("qualification_context") != qualification_context
            or str(adoption_result.get("starting_version") or "") != starting_version
        ):
            last_rejection_detail = _adoption_result_binding_detail(
                adoption_result,
                provider=provider,
                release_tag=release_tag,
                qualification_context=qualification_context,
                starting_version=starting_version,
            )
            continue
        try:
            validate_adoption_result_payload(
                adoption_result, expected_package_identity=package_identity
            )
        except ValueError as exc:
            last_rejection_detail = _adoption_result_rejection_detail(exc)
            continue
        return adoption_result, ""
    return None, last_rejection_detail


def _extract_bound_adoption_result(
    text: str,
    *,
    campaign_id: str,
    provider: str,
    release_tag: str,
    idempotency_key: str,
    qualification_context: str,
    starting_version: str,
    package_identity: str,
    package_source: str = DEFAULT_PACKAGE_SOURCE,
) -> dict[str, Any] | None:
    """Return the validated adoption result, or None.

    Backward-compatible wrapper for callers that do not need the rejection
    reason.
    """
    result, _ = _extract_bound_adoption_result_ex(
        text,
        campaign_id=campaign_id,
        provider=provider,
        release_tag=release_tag,
        idempotency_key=idempotency_key,
        qualification_context=qualification_context,
        starting_version=starting_version,
        package_identity=package_identity,
        package_source=package_source,
    )
    return result


def _dispatch_github_comment(
    repo_slug: str,
    issue_number: int | str,
    campaign_id: str,
    release_tag: str,
    package_spec: str,
    provider: str,
    qualification_context: str,
    idempotency_key: str,
    *,
    posture: str = "required",
    starting_version: str = "",
    package_source: str = DEFAULT_PACKAGE_SOURCE,
    trigger_comments: tuple[str, ...] = (),
    reconciliation_key: str = "",
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> tuple[bool, dict[str, Any], str]:
    """Post the dispatch comment that tells a remote provider exactly what to qualify.

    An upgrade dispatch advertises its exact ``starting_version`` in both the
    machine-readable marker and the human-facing instructions: the accepted
    result must carry that same starting version, so a remote runner that is
    never told it would be guessing. A dispatch that cannot state the starting
    version of an upgrade campaign is refused rather than posted. Cold-install
    (and ``unknown``) campaigns have no starting version and omit the field.

    When ``trigger_comments`` is supplied, the dispatch body includes them so
    a remote provider knows exactly which comment to watch for.
    """
    if not repo_slug or not issue_number:
        return False, {}, _safe_error("missing_issue_number")
    if qualification_context == "upgrade" and not starting_version:
        return False, {}, _safe_error("campaign_identity_incomplete")

    _validate_package_source(package_source)
    dispatch_marker = {
        "schema": DISPATCH_SCHEMA,
        "campaign_id": campaign_id,
        "release_tag": release_tag,
        "package_spec": package_spec,
        "provider": provider,
        "posture": posture,
        "qualification_context": qualification_context,
        "package_source": package_source,
        "idempotency_key": idempotency_key,
    }
    if starting_version:
        dispatch_marker["starting_version"] = starting_version
    if reconciliation_key:
        dispatch_marker["reconciliation_key"] = reconciliation_key
    marker_str = json.dumps(dispatch_marker, sort_keys=True)
    starting_version_line = (
        f"- **Starting Version:** `{starting_version}`\n" if starting_version else ""
    )
    # The source line names only the closed identifier plus the fixed,
    # canonical index URLs that identifier resolves to -- never an arbitrary
    # or user-supplied URL -- so a remote runner installs from the right
    # index without guessing.
    package_source_line = (
        f"- **Package Source:** `{package_source}` (candidate index: `{TESTPYPI_INDEX_URL}`, "
        f"dependency index: `{PRODUCTION_PYPI_INDEX_URL}`). Download the candidate with "
        f"`--no-deps` from TestPyPI, verify its exact package identity and version, then install "
        f"the verified local artifact with dependencies from production PyPI. Never combine "
        f"the indexes with `--extra-index-url`.\n"
        if package_source == "testpypi"
        else f"- **Package Source:** `{package_source}`\n"
    )
    trigger_comments_line = ""
    if trigger_comments:
        formatted_triggers = ", ".join(f"`{tc}`" for tc in trigger_comments)
        trigger_comments_line = f"- **Trigger comments:** {formatted_triggers}\n"
    starting_version_requirement = (
        f" The embedded `adoption_result` must report `qualification_context` "
        f"`{qualification_context}` and `starting_version` `{starting_version}`; "
        f"a result from any other starting version is rejected."
        if starting_version
        else f" The embedded `adoption_result` must report `qualification_context` "
        f"`{qualification_context}` with an empty `starting_version`."
    )
    body = (
        f"### Code Mower Release Qualification Dispatch\n\n"
        f"- **Release Tag:** `{release_tag}`\n"
        f"- **Package Spec:** `{package_spec}`\n"
        f"- **Provider:** `{provider}`\n"
        f"- **Posture:** `{posture}`\n"
        f"- **Context:** `{qualification_context}`\n"
        f"{starting_version_line}"
        f"{package_source_line}"
        f"{trigger_comments_line}"
        f"- **Idempotency Key:** `{idempotency_key}`\n\n"
        f"Reply with a comment containing a `CODE_MOWER_ADOPTION_RESULT` "
        f"marker wrapping schema `{RESULT_MARKER_SCHEMA}` with matching "
        f"campaign_id, provider, release_tag, package_source, and idempotency_key, plus an "
        f"embedded `adoption_result`. The marker must be a single-line HTML "
        f"comment on a line of its own.{starting_version_requirement} "
        f"See docs/release-qualification.md.\n\n"
        f"<!-- CODE_MOWER_RELEASE_CAMPAIGN: {marker_str} -->\n"
    )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as fh:
        body_path = Path(fh.name)
        fh.write(body)

    try:
        completed = command_runner(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo_slug,
                "--body-file",
                str(body_path),
            ]
        )
    except (OSError, ValueError):
        return False, {}, _safe_error("github_dispatch_failed")
    finally:
        try:
            body_path.unlink()
        except OSError:
            pass

    returncode = getattr(completed, "returncode", 1)
    if returncode != 0:
        return False, {}, _safe_error("github_dispatch_failed")

    return True, {"issue_number": str(issue_number), "comment_posted": True}, ""


def _post_trigger_comment(
    repo_slug: str,
    issue_number: int | str,
    trigger_command: str,
    *,
    campaign_id: str,
    provider: str,
    reconciliation_key: str,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> tuple[bool, dict[str, Any], str]:
    """Post the trigger command as a separate comment to actually start the provider.

    For manually triggered hosted providers (Devin, Cursor Cloud Agent), the
    dispatch comment documents what to qualify, but the provider starts only
    when it sees its configured trigger comment. Post that trigger as a plain
    comment body so the provider will actually begin the qualification run.
    """
    if (
        not repo_slug
        or not issue_number
        or not trigger_command
        or not campaign_id
        or not provider
        or not reconciliation_key
    ):
        return False, {}, "missing trigger prerequisites"

    marker = json.dumps(
        {
            "schema": TRIGGER_MARKER_SCHEMA,
            "campaign_id": campaign_id,
            "provider": provider,
            "reconciliation_key": reconciliation_key,
        },
        sort_keys=True,
    )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as fh:
        trigger_path = Path(fh.name)
        fh.write(f"{trigger_command}\n\n<!-- CODE_MOWER_RELEASE_TRIGGER: {marker} -->\n")

    try:
        completed = command_runner(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo_slug,
                "--body-file",
                str(trigger_path),
            ]
        )
    except (OSError, ValueError):
        return False, {}, "trigger comment post failed"
    finally:
        try:
            trigger_path.unlink()
        except OSError:
            pass

    returncode = getattr(completed, "returncode", 1)
    if returncode != 0:
        return False, {}, "trigger comment post failed"

    return True, {"trigger_posted": True}, ""


def _has_matching_release_marker(
    comments: Sequence[Mapping[str, Any]],
    marker_name: str,
    expected: Mapping[str, str],
) -> bool:
    """Match a side effect using its pre-persisted, unguessable reconciliation key."""
    if not expected.get("reconciliation_key"):
        return False
    pattern = re.compile(
        rf"<!--\s*{re.escape(marker_name)}:\s*(\{{.*?\}})\s*-->",
        re.DOTALL,
    )
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        match = pattern.search(body)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(1))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        if all(
            str(payload.get(key) or ("required" if key == "posture" else "")) == value
            for key, value in expected.items()
        ):
            return True
    return False


def _poll_github_comments(
    repo_slug: str,
    issue_number: int | str,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
) -> tuple[list[dict[str, Any]], str]:
    if not repo_slug or not issue_number:
        return [], _safe_error("github_poll_unavailable")
    try:
        result = gh_json_runner(
            ["issue", "view", str(issue_number), "--repo", repo_slug, "--json", "comments"]
        )
    except (
        OSError,
        ValueError,
        lane_status.LaneStatusUnavailable,
        subprocess.TimeoutExpired,
    ):
        return [], _safe_error("github_poll_unavailable")
    if isinstance(result, tuple) and len(result) == 2:
        data, error = result
    else:
        data, error = result, ""
    if error:
        return [], _safe_error("github_poll_unavailable")
    if not isinstance(data, dict):
        return [], _safe_error("github_poll_unavailable")
    comments = data.get("comments")
    if not isinstance(comments, list):
        return [], _safe_error("github_poll_unavailable")
    return [c for c in comments if isinstance(c, dict)], ""


def _normalize_github_login(login: str) -> str:
    return login.strip().lower()


def _comment_author_login(comment: Mapping[str, Any]) -> str:
    """Extract the commenter's login from the `gh issue view --json comments` shape."""
    author = comment.get("author")
    if isinstance(author, Mapping):
        login = author.get("login")
        if isinstance(login, str):
            return login
    return ""


def _resolve_trusted_bot_authors(
    lane: ProviderLane,
    *,
    env: Mapping[str, str],
) -> tuple[str, ...]:
    """Resolve the closed set of GitHub logins trusted to post adoption-result markers.

    Only the lane's declarative `provider_config.bot_authors` and an optional
    `provider_config.bot_authors_env` environment override are honored. The
    idempotency key alone is not sufficient identity binding -- it is visible
    in the public dispatch comment, so anyone could reply with a matching
    marker. A lane with no trusted authors configured trusts nobody.
    """
    authors: list[str] = [str(a) for a in lane.provider_config.get("bot_authors") or ()]
    bot_authors_env = lane.provider_config.get("bot_authors_env")
    if bot_authors_env:
        raw = env.get(str(bot_authors_env), "")
        authors.extend(part.strip() for part in raw.split(",") if part.strip())
    return tuple(_normalize_github_login(a) for a in authors if a)


def _is_trusted_github_author(author_login: str, trusted_authors: Sequence[str]) -> bool:
    if not author_login or not trusted_authors:
        return False
    return _normalize_github_login(author_login) in trusted_authors


def _validate_adapter_argv_template(template: Any) -> tuple[str, ...]:
    """Validate campaign_adapter_argv is a list of non-empty scalar tokens.

    Applies to both the registry-declared template and any repo_path/
    code-mower.yml override -- neither is trusted to already be well-formed,
    since the override comes from adopter-controlled YAML.
    """
    if not isinstance(template, (list, tuple)):
        raise ValueError("campaign_adapter_argv must be a list")
    tokens: list[str] = []
    for token in template:
        if isinstance(token, bool) or not isinstance(token, (str, int, float)):
            raise ValueError("campaign_adapter_argv tokens must be non-empty scalar values")
        text = str(token)
        if not text:
            raise ValueError("campaign_adapter_argv tokens must be non-empty scalar values")
        tokens.append(text)
    if not tokens:
        raise ValueError("campaign_adapter_argv must not be empty")
    return tuple(tokens)


def _validate_adapter_timeout(value: Any) -> int:
    """Validate campaign_adapter_timeout_seconds.

    Accepts a real int/float (the registry form) or a base-10 integer string
    (the repo_path/code-mower.yml override form, since its minimal YAML-subset
    parser leaves bare numbers as strings). Anything else is invalid.

    The documented contract is a *positive integer*, so a numeric value must be
    integral as written. Rounding one that is not -- `int(1.9)` is `1` -- would
    silently enforce a shorter adapter timeout than the adopter configured, and
    `int(0.5)` would turn a value this function is supposed to reject into a
    hard-failing zero-second timeout. Non-finite floats are rejected for the
    same reason: `int(float("nan"))` raises and `int(float("inf"))` raises, so
    they would otherwise escape as an unbounded traceback rather than the
    bounded error every other malformed value gets. Bools are excluded before
    the numeric branch because `True` is an `int` and would parse as one second.
    """
    if isinstance(value, bool):
        raise ValueError("campaign_adapter_timeout_seconds must be a positive integer")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
            raise ValueError("campaign_adapter_timeout_seconds must be a positive integer")
        parsed = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError as exc:
            raise ValueError("campaign_adapter_timeout_seconds must be a positive integer") from exc
    else:
        raise ValueError("campaign_adapter_timeout_seconds must be a positive integer")
    if parsed <= 0:
        raise ValueError("campaign_adapter_timeout_seconds must be a positive integer")
    return parsed


def _validate_positive_duration(value: Any, name: str) -> float:
    """Validate that value is a positive finite float number of seconds."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive number of seconds")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive number of seconds") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return number


def _build_adapter_argv(
    lane: ProviderLane,
    resolved_command: str,
    *,
    release_tag: str,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
    package_source: str = DEFAULT_PACKAGE_SOURCE,
    output_path: Path,
    repo_path: Path,
    argv_template: Any,
    adapter_timeout: int | None = None,
    python_bin: str = "",
    target_runtime: str = "",
) -> list[str]:
    validated_template = _validate_adapter_argv_template(argv_template)
    substitutions = {
        "command": resolved_command,
        "release_tag": release_tag,
        "package_spec": package_spec,
        "qualification_context": qualification_context,
        "starting_version": starting_version,
        "package_source": package_source,
        "output": str(output_path),
        "repo_path": str(repo_path),
        # The running interpreter running Code Mower
        "python": sys.executable,
        # The resolved supported Python executable for target runtime qualification
        "target_python": python_bin or sys.executable,
        "target_runtime": target_runtime or _detect_runtime_class(),
    }
    if adapter_timeout is not None:
        substitutions["adapter_timeout"] = str(adapter_timeout)
    try:
        return [token.format(**substitutions) for token in validated_template]
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        # Every way a malformed placeholder can fail against these plain string
        # substitutions is one configuration error: an unknown field name
        # (KeyError), a positional or out-of-range index (IndexError),
        # attribute access such as `{repo_path.parent}` (AttributeError), and
        # non-integer subscripting such as `{output[dir]}` (TypeError). They all
        # become the same bounded `adapter_configuration_invalid` for the
        # caller. Letting the last two escape instead raised out of an applied
        # run *after* `attempted_at` was stamped, leaving a provider that looked
        # queued but that an ordinary resume would skip.
        raise ValueError(f"invalid campaign_adapter_argv template for lane {lane.lane_id!r}") from exc


def _campaign_adapter_override_lane_keys(lane: ProviderLane) -> tuple[str, ...]:
    """Repo config keys that may declare an override for this lane: its canonical
    lane_id, plus any alias that resolves to the same lane.

    Sorted, not a set: these keys are matched against adopter config and named
    back in error messages, and set iteration order is an implementation detail
    that can differ between runs. Every consumer must behave identically no
    matter which spelling an adopter happened to write first.
    """
    keys = {lane.lane_id}
    keys.update(alias for alias, target in PROVIDER_ALIAS_MAP.items() if target == lane.lane_id)
    return tuple(sorted(keys))


def _load_campaign_adapter_overrides(
    lane: ProviderLane,
    repo_path: Path,
) -> tuple[Mapping[str, Any], str, str]:
    """Load narrowly-scoped campaign adapter overrides from repo_path/code-mower.yml.

    Only `campaign_adapter_argv`, `campaign_adapter_timeout_seconds`, and
    `campaign_adapter_enabled` are read from the matching lane's
    `provider_config`; every other key in the repo config is ignored here so
    this does not widen the general config contract. A missing repo config, a
    missing `lanes` key, or a lane with no matching entry is not an override
    (empty mapping, no error). An existing repo config that fails to load, or
    is structurally malformed (a present non-mapping `lanes`, lane, or
    provider_config entry), returns `adapter_configuration_invalid` instead of
    silently treating the config as absent -- the specific override values are
    validated by the caller.

    A lane can be spelled several ways (`muse` and `muse_cli`; `claude` and
    `claude_code`), and all of those spellings name one lane with one adapter
    command. A config that declares more than one of them is therefore
    *ambiguous*, not merely redundant: the entries may carry two different
    `campaign_adapter_argv` values, and picking one of them would mean running
    whichever alias happened to be looked up first. That is refused with the
    same bounded `adapter_configuration_invalid` code as any other malformed
    override, and a detail naming the conflicting spellings. Only keys drawn
    from the built-in alias table are ever named, so the message stays bounded
    and cannot echo adopter config text.

    Returns (overrides, error_code, error_detail).
    """
    config_path = repo_path / "code-mower.yml"
    if not config_path.is_file():
        return {}, "", ""

    try:
        loaded = code_mower_config.load_config(config_path)
    except (OSError, code_mower_config.ConfigError):
        return {}, _safe_error("adapter_configuration_invalid"), ""

    lanes_cfg = loaded.get("lanes")
    if lanes_cfg is None:
        return {}, "", ""
    if not isinstance(lanes_cfg, Mapping):
        return {}, _safe_error("adapter_configuration_invalid"), ""

    configured_keys = [
        key
        for key in _campaign_adapter_override_lane_keys(lane)
        if lanes_cfg.get(key) is not None
    ]
    if len(configured_keys) > 1:
        return (
            {},
            _safe_error("adapter_configuration_invalid"),
            (
                f"code-mower.yml configures the same provider lane under "
                f"{len(configured_keys)} names ({', '.join(configured_keys)}); "
                "keep exactly one"
            ),
        )
    if not configured_keys:
        return {}, "", ""

    lane_cfg = lanes_cfg.get(configured_keys[0])
    if not isinstance(lane_cfg, Mapping):
        return {}, _safe_error("adapter_configuration_invalid"), ""

    provider_cfg = lane_cfg.get("provider_config")
    if provider_cfg is None:
        return {}, "", ""
    if not isinstance(provider_cfg, Mapping):
        return {}, _safe_error("adapter_configuration_invalid"), ""

    overrides = {
        key: provider_cfg[key]
        for key in (
            "campaign_adapter_argv",
            "campaign_adapter_timeout_seconds",
            "campaign_adapter_enabled",
        )
        if key in provider_cfg
    }
    return overrides, "", ""


def _resolve_campaign_adapter_config(
    lane: ProviderLane,
    repo_path: Path,
) -> tuple[Any, Any, str, str]:
    """Resolve the effective campaign_adapter_argv/timeout for a lane.

    Overlays only the allowed override keys from repo_path/code-mower.yml
    onto the immutable reference lane's provider_config; the reference lane
    itself is never mutated. Returns
    (argv_template, timeout_value, error_code, error_detail).
    """
    overrides, error, detail = _load_campaign_adapter_overrides(lane, repo_path)
    if error:
        return None, None, error, detail
    enabled = overrides.get(
        "campaign_adapter_enabled",
        lane.provider_config.get("campaign_adapter_enabled", True),
    )
    if not isinstance(enabled, bool):
        return None, None, _safe_error("adapter_configuration_invalid"), ""
    if enabled is False:
        # Explicitly disabled: behave as if no adapter were configured, so
        # the provider degrades to unavailable/manual rather than running.
        return None, None, "", ""
    argv_template = overrides.get("campaign_adapter_argv", lane.provider_config.get("campaign_adapter_argv"))
    timeout_value = overrides.get(
        "campaign_adapter_timeout_seconds",
        lane.provider_config.get("campaign_adapter_timeout_seconds"),
    )
    return argv_template, timeout_value, "", ""


_VERSIONED_PYTHON_RE = re.compile(r"^python3\.(\d+)$")


def _versioned_python_candidates(path_value: str) -> tuple[str, ...]:
    """Return discovered Python 3.12+ command names in newest-first order."""
    minors: set[int] = set()
    for raw_dir in path_value.split(os.pathsep):
        if not raw_dir:
            continue
        try:
            entries = Path(raw_dir).expanduser().iterdir()
            for entry in entries:
                match = _VERSIONED_PYTHON_RE.fullmatch(entry.name)
                if match and int(match.group(1)) >= 12:
                    minors.add(int(match.group(1)))
        except OSError:
            continue
    return tuple(f"python3.{minor}" for minor in sorted(minors, reverse=True))


def resolve_supported_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, str] | None:
    """Resolve one supported Python 3.12+ runtime deterministically.

    Returns (executable, target_runtime) where target_runtime is
    f"python_{major}.{minor}", or None when no supported runtime exists.
    """
    env = os.environ if environ is None else environ
    explicit = env.get("CODE_MOWER_PYTHON", "").strip()
    if explicit:
        candidates = (explicit,)
    else:
        discovered = _versioned_python_candidates(str(env.get("PATH", "")))
        candidates = (
            sys.executable,
            *discovered,
            "python3.12",
            "python3.13",
            "python3.14",
            "python3",
        )
    which = shutil.which if which_fn is None else which_fn
    run = subprocess.run if runner is None else runner
    for cand in candidates:
        if not cand:
            continue
        cand_path = Path(cand).expanduser()
        if (
            os.sep in cand
            or (os.altsep and os.altsep in cand)
            or cand == sys.executable
            or (cand == explicit and cand_path.exists())
        ):
            if cand_path.exists():
                resolved = str(cand_path.resolve()) if not cand_path.is_absolute() else cand
            else:
                resolved = None
        else:
            resolved = which(cand)
            if resolved and not Path(resolved).is_absolute():
                resolved = str(Path(resolved).resolve())
        if not resolved:
            continue
        try:
            completed = run(
                [resolved, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        out = (completed.stdout or "").strip()
        parts = out.split(".")
        try:
            major, minor = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            continue
        if (major, minor) >= (3, 12):
            return resolved, f"python_{major}.{minor}"
    return None


def _invoke_local_adapter(
    lane: ProviderLane,
    provider: str,
    *,
    release_tag: str,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
    package_source: str = DEFAULT_PACKAGE_SOURCE,
    output_path: Path,
    repo_path: Path,
    which_fn: Callable[[str], str | None],
    adapter_runner: AdapterRunner,
    python_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Invoke a provider's explicit, registry-configured campaign adapter.

    Never falls back to running Code Mower's own qualification under the
    provider's name: a provider can complete only when its own adapter
    command actually ran (argv only, no shell) and produced a valid,
    identity-matching result file. Returns (result, error_code, detail).
    """
    package_identity = campaign_package_identity(package_spec)
    if not package_identity:
        # The campaign's stored spec yields no package identity to bind the
        # adapter's result to, so no result could be accepted. Refuse before
        # invoking anything rather than run an adapter whose output is
        # guaranteed to be rejected.
        return (
            None,
            _safe_error("campaign_identity_incomplete"),
            f"{provider} campaign package spec is not an exact package-index spec",
        )
    argv_template, timeout_value, config_error, config_detail = _resolve_campaign_adapter_config(
        lane, repo_path
    )
    if config_error:
        return (
            None,
            config_error,
            config_detail or f"{provider} campaign adapter configuration is invalid",
        )
    if not argv_template:
        return None, _safe_error("no_campaign_adapter_configured"), "no campaign adapter configured"

    runtime = resolve_supported_runtime(environ=environ, which_fn=which_fn, runner=python_runner)
    if runtime is None:
        return (
            None,
            _safe_error("python_runtime_unavailable"),
            "supported Python 3.12+ runtime is unavailable",
        )
    python_bin, target_runtime = runtime

    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        return (
            None,
            _safe_error("adapter_result_invalid"),
            f"{provider} adapter prior result could not be cleared",
        )

    resolved = _find_command(lane, which_fn=which_fn)
    if not resolved:
        cmd = lane.provider_config.get("command") or provider
        return None, _safe_error("command_not_found"), f"command not found: {cmd}"
    resolved_path = which_fn(resolved) or resolved

    try:
        timeout = (
            _validate_adapter_timeout(timeout_value)
            if timeout_value is not None
            else DEFAULT_ADAPTER_TIMEOUT_SECONDS
        )
    except ValueError:
        return (
            None,
            _safe_error("adapter_configuration_invalid"),
            f"{provider} campaign adapter configuration is invalid",
        )
    try:
        argv = _build_adapter_argv(
            lane,
            resolved_path,
            release_tag=release_tag,
            package_spec=package_spec,
            qualification_context=qualification_context,
            starting_version=starting_version,
            package_source=package_source,
            output_path=output_path,
            repo_path=repo_path,
            argv_template=argv_template,
            adapter_timeout=max(1, timeout - ADAPTER_INNER_TIMEOUT_MARGIN_SECONDS),
            python_bin=python_bin,
            target_runtime=target_runtime,
        )
    except ValueError:
        return (
            None,
            _safe_error("adapter_configuration_invalid"),
            f"{provider} campaign adapter configuration is invalid",
        )

    try:
        completed = adapter_runner(argv, timeout)
    except subprocess.TimeoutExpired:
        return None, _safe_error("adapter_timeout"), f"{provider} adapter exceeded {timeout}s"
    except OSError:
        return None, _safe_error("command_not_found"), f"command not found: {argv[0]}"

    if completed.returncode != 0:
        return None, _safe_error("adapter_exited_nonzero"), f"{provider} adapter exited {completed.returncode}"

    if not output_path.is_file():
        return None, _safe_error("adapter_produced_no_result"), f"{provider} adapter did not write a result file"

    try:
        with output_path.open("r", encoding="utf-8") as fh:
            result = json.load(fh)
        validate_adoption_result_payload(
            result, expected_package_identity=package_identity
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None, _safe_error("adapter_result_invalid"), f"{provider} adapter result failed schema validation"

    if (
        result.get("provider") != provider
        or result.get("executor") != provider
        or result.get("release_tag") != release_tag
        or result.get("qualification_context") != qualification_context
        or result.get("starting_version") != starting_version
    ):
        return None, _safe_error("adapter_result_mismatch"), f"{provider} adapter result identity mismatch"

    return result, "", ""


def initialize_campaign(
    *,
    release_tag: str,
    package_spec: str = "",
    qualification_context: str = "cold_install",
    starting_version: str = "",
    package_source: str = DEFAULT_PACKAGE_SOURCE,
    providers: Sequence[str] = (),
    required_providers: Sequence[str] | None = None,
    repo_slug: str = "",
    campaign_id: str = "",
) -> ReleaseCampaign:
    valid, normalized_version, error = _validate_tag_format(release_tag)
    if not valid:
        raise ValueError(error)

    if not package_spec:
        package_spec = f"code-mower=={normalized_version}"

    # A single parse of the spec supplies both the identity this campaign binds
    # its results to and the version it pins. Re-reading the version with a
    # separate, narrower name grammar used to refuse exact specs whose
    # distribution name contains a dot -- `zope.interface==5.0.0`, or the
    # documented `code.mower==1.0.0` -- as a version mismatch they did not have.
    package_identity, spec_version = _parse_exact_package_spec(package_spec)
    if spec_version != normalized_version:
        raise ValueError(f"Version mismatch: tag {normalized_version} vs spec {package_spec}")

    _validate_qualification_context(qualification_context)
    _validate_starting_version(starting_version)
    if qualification_context == "upgrade" and not starting_version:
        raise ValueError("starting_version is required for upgrade qualification")
    if qualification_context != "upgrade" and starting_version:
        raise ValueError("starting_version is only valid for upgrade qualification")
    if (
        qualification_context == "upgrade"
        and _version_key(starting_version) >= _version_key(normalized_version)
    ):
        raise ValueError("starting_version must be lower than the target version")
    _validate_package_source(package_source)

    if not campaign_id:
        campaign_id = f"campaign-{release_tag}"
    # The generated default and any explicit id are held to the same storage
    # contract, so the id a campaign is created under is always exactly the stem
    # of the file it lives in.
    validate_campaign_id(campaign_id)

    provider_keys = list(providers) if providers else list(DEFAULT_CAMPAIGN_PROVIDERS)

    # Canonicalize before any participant is constructed. Two spellings of one
    # provider -- an exact repeat, or two aliases of the same lane -- would
    # otherwise become two participants sharing a single idempotency key and a
    # single result file path, so one provider's evidence would be counted
    # twice toward the campaign. Fail closed instead of deduplicating silently:
    # the caller asked for something the campaign cannot represent.
    resolved_providers: list[tuple[str, ProviderLane]] = []
    seen_providers: set[str] = set()
    for p_name in provider_keys:
        canonical_name, lane = resolve_provider_lane(p_name)
        if canonical_name in seen_providers:
            raise ValueError(
                f"duplicate release campaign provider {canonical_name!r}: it was named "
                "more than once, directly or through an alias; list each provider "
                "exactly once"
            )
        # Lanes that explicitly opt out of release campaigns fail closed here,
        # before any participant is constructed. Only an explicit
        # `campaign_eligible` false is honored; providers that omit the key keep
        # their current behavior.
        if lane.provider_config.get("campaign_eligible") is False:
            raise ValueError(
                f"release campaign provider {canonical_name!r} is not campaign "
                f"eligible: {lane.lane_id} declares campaign_eligible=false "
                "because it has no maintained campaign adapter yet; remove it "
                "or choose an eligible provider (for example devin, codex, or "
                "cursor_cloud_agent)"
            )
        # New campaigns may include providers with work_order_execution capability.
        # Lanes marked role:reviewer or capability:code_review must fail before dispatch.
        role = lane.provider_config.get("role", "")
        capability = lane.provider_config.get("capability", "")
        if role == "reviewer" or capability == "code_review":
            raise ValueError(
                f"release campaign provider {canonical_name!r} is a review-only lane "
                f"(role: {role!r}, capability: {capability!r}) and cannot execute "
                f"package qualification campaigns. Release campaigns require providers "
                f"with work_order_execution capability; choose cursor_cloud_agent or "
                f"another builder provider instead"
            )
        seen_providers.add(canonical_name)
        resolved_providers.append((canonical_name, lane))

    if required_providers is not None:
        if not required_providers:
            raise ValueError(
                "--required-providers cannot be empty; specify a non-empty subset of selected providers"
            )
        resolved_required: set[str] = set()
        seen_required: set[str] = set()
        for r_name in required_providers:
            r_canonical, _ = resolve_provider_lane(r_name)
            if r_canonical in seen_required:
                raise ValueError(
                    f"duplicate required provider {r_canonical!r}: it was named more than once; "
                    "list each required provider exactly once"
                )
            seen_required.add(r_canonical)
            resolved_required.add(r_canonical)

        for r_canonical in resolved_required:
            if r_canonical not in seen_providers:
                raise ValueError(
                    f"required provider {r_canonical!r} is not in the selected providers for this campaign: "
                    f"{', '.join(sorted(seen_providers))}"
                )
    else:
        resolved_required = set(seen_providers)

    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    environment = _detect_environment()

    campaign_providers: list[dict[str, Any]] = []
    for canonical_name, lane in resolved_providers:
        idemp_key = _compute_idempotency_key(
            campaign_id,
            canonical_name,
            release_tag,
            qualification_context,
            starting_version,
            package_source,
        )
        posture = "required" if canonical_name in resolved_required else "informational"
        cp = CampaignProvider(
            provider=canonical_name,
            lane_id=lane.lane_id,
            driver=lane.driver,
            state="queued",
            environment=environment,
            elapsed_seconds=0.0,
            idempotency_key=idemp_key,
            posture=posture,
            dispatch_mode="dry_run",
            next_action=f"run with --apply to execute {canonical_name}",
            next_detail="",
        )
        campaign_providers.append(cp.to_dict())

    overall_status, next_action, next_detail = _aggregate_campaign_status(
        campaign_providers,
        dry_run=True,
    )

    return ReleaseCampaign(
        schema=CAMPAIGN_SCHEMA,
        campaign_id=campaign_id,
        release_tag=release_tag,
        package_identity=package_identity,
        package_spec=package_spec,
        normalized_version=normalized_version,
        qualification_context=qualification_context,
        starting_version=starting_version,
        package_source=package_source,
        repo_slug=repo_slug,
        status=overall_status,
        dry_run=True,
        elapsed_seconds=0.0,
        created_at=now_utc,
        updated_at=now_utc,
        next_action=next_action,
        next_detail=next_detail,
        providers=campaign_providers,
        provider_posture_configured=required_providers is not None,
    )


def _record_dry_run_dispatch_mode(provider_data: dict[str, Any]) -> None:
    """Record a dry-run evaluation without erasing an applied dispatch mode.

    Per-provider ``dispatch_mode`` is monotonic for the same reason the
    campaign-level flag is: a provider that was dispatched under ``--apply``
    was dispatched, and a later poll that omits the flag is not evidence to the
    contrary. Only a provider that has never been dispatched under ``--apply``
    is (re)labelled a dry-run preview.
    """
    if provider_data.get("dispatch_mode") != "applied":
        provider_data["dispatch_mode"] = "dry_run"


# Bounded per-provider attempt history. An explicit --retry-provider starts a new
# attempt that supersedes the stored one, and newly arrived evidence may do the
# same on resume; the superseded attempt leaves one metadata-only summary here
# so prior applied attempts stay auditable. Entries carry timestamps, state,
# outcome, error code, and elapsed time only -- never the adoption result, raw
# output, paths, or secrets -- and only the most recent entries are kept.
MAX_ATTEMPT_HISTORY_ENTRIES = 5

# Provider states whose stored adoption result is terminal qualification
# evidence. `complete` holds passing evidence; `blocked` holds failing
# evidence. Both convert to metadata-only adoption_run events on upload, while
# providers that emitted no valid result stay skipped.
TERMINAL_EVIDENCE_STATES = frozenset({"complete", "blocked"})

# Closed outcome vocabulary for attempt-history summaries. Mirrors
# release_qualify.VALID_OUTCOMES without importing the runner; anything else
# degrades to "" rather than leaking stored content.
ATTEMPT_HISTORY_OUTCOMES = frozenset({"pass", "pass_with_warnings", "fail", "incomplete"})


# Upper bound for an attempt-history timestamp string. The tool writes
# `%Y-%m-%dT%H:%M:%SZ` (20 characters); retained hand-edited values are kept
# only when short enough to be timestamps and parse as documented UTC
# timestamps, so arbitrary-size strings -- and short secrets or other
# arbitrary strings -- stored in these fields can never be preserved
# across a retry.
ATTEMPT_HISTORY_TIMESTAMP_MAX_LENGTH = 64


def _sanitize_history_timestamp(value: Any) -> str | None:
    """Return ``value`` when it is a valid documented UTC timestamp, else None.

    Length alone cannot distinguish a timestamp from a short secret or
    arbitrary string, so retained values must also parse as timestamps.
    Semantic validation reuses :func:`_parse_board_timestamp`, and the
    documented ``%Y-%m-%dT%H:%M:%SZ`` syntax is enforced strictly; anything
    else degrades to None while valid campaign timestamps are returned
    unchanged.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if len(value) > ATTEMPT_HISTORY_TIMESTAMP_MAX_LENGTH:
        return None
    if _parse_board_timestamp(value) is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return value


def _prior_attempt_summary(provider_data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Summarize the stored attempt in metadata-only form, or None when no attempt exists."""
    stored_result = provider_data.get("adoption_result")
    outcome = stored_result.get("outcome") if isinstance(stored_result, Mapping) else ""
    if not isinstance(outcome, str) or outcome not in ATTEMPT_HISTORY_OUTCOMES:
        outcome = ""
    error = provider_data.get("error")
    if not isinstance(error, str) or error not in SAFE_ERROR_CODES:
        error = ""
    elapsed = provider_data.get("elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        elapsed = 0.0
    attempted_at = provider_data.get("attempted_at")
    completed_at = provider_data.get("completed_at")
    if not attempted_at and not completed_at and not isinstance(stored_result, Mapping):
        return None
    state = provider_data.get("state")
    if not isinstance(state, str) or state not in VALID_PROVIDER_STATES:
        state = ""
    dispatched_at = provider_data.get("dispatched_at")
    return {
        "attempted_at": _sanitize_history_timestamp(attempted_at),
        "dispatched_at": _sanitize_history_timestamp(dispatched_at),
        "completed_at": _sanitize_history_timestamp(completed_at),
        "state": state,
        "outcome": outcome,
        "error": error,
        "elapsed_seconds": round(float(elapsed), 2),
    }


# The only fields an attempt-history entry may carry. Everything else --
# result bodies, output, paths, secrets, nested mappings -- is dropped when
# retained entries are rebuilt.
ATTEMPT_HISTORY_FIELDS = frozenset(
    {
        "attempted_at",
        "dispatched_at",
        "completed_at",
        "state",
        "outcome",
        "error",
        "elapsed_seconds",
    }
)


def _sanitize_attempt_history_entry(entry: Any) -> dict[str, Any] | None:
    """Rebuild one retained history entry from the closed allowed scalar fields.

    Stored campaign files are untrusted input: a retained entry may have been
    hand-edited to carry result bodies, output, paths, secrets, nested
    values, or arbitrarily large strings. Only the bounded scalar history
    fields survive, each validated and coerced exactly as
    :func:`_prior_attempt_summary` validates fresh summaries; unknown and
    nested fields are discarded. Returns None for malformed entries (not a
    mapping, or a mapping with none of the allowed fields), which the caller
    drops instead of preserving.
    """
    if not isinstance(entry, Mapping):
        return None
    if not any(field_name in entry for field_name in ATTEMPT_HISTORY_FIELDS):
        return None

    state = entry.get("state")
    if not isinstance(state, str) or state not in VALID_PROVIDER_STATES:
        state = ""
    outcome = entry.get("outcome")
    if not isinstance(outcome, str) or outcome not in ATTEMPT_HISTORY_OUTCOMES:
        outcome = ""
    error = entry.get("error")
    if not isinstance(error, str) or error not in SAFE_ERROR_CODES:
        error = ""
    elapsed = entry.get("elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        elapsed = 0.0
    return {
        "attempted_at": _sanitize_history_timestamp(entry.get("attempted_at")),
        "dispatched_at": _sanitize_history_timestamp(entry.get("dispatched_at")),
        "completed_at": _sanitize_history_timestamp(entry.get("completed_at")),
        "state": state,
        "outcome": outcome,
        "error": error,
        "elapsed_seconds": round(float(elapsed), 2),
    }


def _record_attempt_history(provider_data: dict[str, Any]) -> None:
    """Append the superseded attempt's bounded summary, keeping history bounded.

    Retained history is always sanitized and capped, even when there is no
    new current-attempt summary to append: a queued provider with no
    timestamps/result can still carry hand-edited malicious or unbounded
    ``attempt_history``, and an explicit retry must not preserve it verbatim.
    """
    history = provider_data.get("attempt_history")
    if not isinstance(history, list):
        history = []
    # Retained entries are rebuilt from the closed allowed scalar fields, not
    # copied verbatim: a malformed or hand-edited stored history can carry
    # result bodies, output, paths, secrets, or arbitrarily large values, and
    # saving them again would preserve that content despite the metadata-only
    # contract. Malformed entries are discarded; survivors are capped with the
    # new summary (when one exists).
    sanitized = [_sanitize_attempt_history_entry(item) for item in history]
    history = [item for item in sanitized if item is not None]
    summary = _prior_attempt_summary(provider_data)
    if summary is not None:
        history.append(summary)
    provider_data["attempt_history"] = history[-MAX_ATTEMPT_HISTORY_ENTRIES:]


def _save_campaign_progress(
    campaign: dict[str, Any],
    campaigns_dir: Path,
    *,
    now_utc: str,
) -> None:
    """Persist the campaign with a campaign-level header that matches its providers.

    Every save inside an applied run -- the mid-run checkpoints as much as the
    final one -- publishes a file the Board and ``--status`` may read next,
    possibly because this process never reached its final save. Writing provider
    state without recomputing ``status``/``next_action``/``next_detail`` from it
    would publish a header describing the campaign as it was *before* the
    attempt, so an interrupted run would leave the Board advising work that no
    longer applies -- "run with --apply to dispatch providers" for a provider
    that is already dispatched and pollable.
    """
    status, next_action, next_detail = _aggregate_campaign_status(
        campaign.get("providers", []),
        dry_run=campaign.get("dry_run", True),
    )
    campaign["status"] = status
    campaign["next_action"] = next_action
    campaign["next_detail"] = next_detail
    campaign["updated_at"] = now_utc
    campaign["elapsed_seconds"] = round(
        sum(float(p.get("elapsed_seconds") or 0.0) for p in campaign.get("providers", [])),
        2,
    )
    save_campaign(campaign, campaigns_dir)


def dispatch_or_advance_campaign(
    campaign: dict[str, Any],
    *,
    apply: bool = False,
    issue_number: str | int = "",
    repo_path: Path | None = None,
    campaigns_dir: Path | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    adapter_runner: AdapterRunner = run_local_adapter_command,
    env: Mapping[str, str] | None = None,
    retry_provider: str = "",
    repo_slug_override: str = "",
    poll_only: bool = False,
) -> dict[str, Any]:
    """Execute dispatch, polling, or status progression on a campaign.

    `retry_provider` is the only way a provider whose applied dispatch/adapter
    was already attempted (`attempted_at` set) gets invoked again -- ordinary
    resume never repeats a paid/hosted dispatch or local adapter run.

    A hosted/SaaS dispatch is therefore checkpointed as a *pollable* state
    before its external post rather than after: `attempted_at`, `running`, the
    issue identity in `dispatch_ref`, and `dispatch_mode="applied"` are all
    persisted first, so an interruption anywhere around the post leaves a
    campaign an ordinary resume can poll to a conclusion instead of one stuck
    between "already attempted" and "never dispatched".
    """
    current_env = os.environ if env is None else env
    campaign_before_poll = copy.deepcopy(campaign) if poll_only else None
    repo_path = repo_path or Path.cwd()
    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path)
    results_dir = campaigns_dir / "results"
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Applied is monotonic (see `campaign_has_been_applied`): this run may turn
    # a dry-run campaign into an applied one, but a poll that omits `--apply`
    # never turns an applied campaign back into a preview.
    if not poll_only:
        campaign["dry_run"] = not (apply or campaign_has_been_applied(campaign))
    campaign_id = str(campaign.get("campaign_id") or "")
    release_tag = str(campaign.get("release_tag") or "")
    package_spec = str(campaign.get("package_spec") or "")
    # Every result this run accepts -- a local drop-in file, an adapter's own
    # output, or a trusted GitHub comment -- is bound to the package named by
    # this campaign's exact spec, not to a hard-coded distribution.
    package_identity = campaign_package_identity(package_spec)
    context = str(campaign.get("qualification_context") or "cold_install")
    starting_version = str(campaign.get("starting_version") or "")
    package_source = str(campaign.get("package_source") or DEFAULT_PACKAGE_SOURCE)
    # A watch may need a repository solely to poll an older campaign that did
    # not persist one. Use that value for this invocation without assigning it
    # to the campaign, so observed provider transitions can be saved while the
    # campaign's repository identity remains unchanged.
    repo_slug = str(campaign.get("repo_slug") or repo_slug_override or "")

    retry_canonical = ""
    if retry_provider:
        try:
            retry_canonical, _ = resolve_provider_lane(retry_provider)
        except ValueError:
            retry_canonical = ""

    for provider_data in campaign.get("providers", []):
        provider = str(provider_data.get("provider") or "")
        # Retry freeze: an explicit --retry-provider run advances only the
        # retried provider. This early continue is the single authoritative
        # freeze: every other participant is byte-stable for this run -- no
        # polling, error clearing, deadline expiry, state, evidence,
        # next-action, or timestamp mutations. Byte stability is exact: the
        # frozen provider's dict, including next_action/next_detail, remains
        # unchanged during another provider's explicit retry. Aggregate
        # campaign fields still recompute on save.
        if retry_canonical and provider != retry_canonical:
            continue
        try:
            _, lane = resolve_provider_lane(provider)
        except ValueError:
            provider_data["state"] = "unavailable"
            provider_data["error"] = _safe_error("unknown_provider")
            provider_data["next_action"] = "remove unrecognized provider from campaign"
            provider_data["next_detail"] = ""
            continue
        current_state = str(provider_data.get("state") or "queued")
        is_explicit_retry = bool(retry_canonical) and provider == retry_canonical

        # 1. Check local result drop-in first (manual override or adapter output).
        # An explicit retry of this provider must not accept a pre-existing
        # result file -- that would be stale evidence from the attempt being
        # retried (or older), not evidence of the new attempt.
        local_result_file = results_dir / f"{campaign_id}_{provider}.json"
        bound_result = (
            _load_bound_result_file(
                local_result_file,
                provider=provider,
                release_tag=release_tag,
                qualification_context=context,
                starting_version=starting_version,
                package_identity=package_identity,
            )
            if local_result_file.is_file() and not is_explicit_retry
            else None
        )
        if bound_result is not None:
            prior_result = provider_data.get("adoption_result")
            if prior_result != bound_result:
                # Genuinely new evidence supersedes the stored attempt: leave
                # a bounded summary only when a prior stored result actually
                # existed (the first completion of a running attempt
                # supersedes nothing), then stamp this completion.
                # Re-reading identical evidence (a resume re-poll, or another
                # provider's retry) is not a new attempt and must not move
                # chronology.
                if isinstance(prior_result, Mapping) and prior_result:
                    _record_attempt_history(provider_data)
                provider_data["completed_at"] = now_utc
            provider_data["adoption_result"] = bound_result
            provider_data["elapsed_seconds"] = float(bound_result.get("elapsed_seconds") or 0.0)
            provider_data["error"] = ""
            outcome = bound_result.get("outcome")
            if outcome in {"pass", "pass_with_warnings"}:
                provider_data["state"] = "complete"
                provider_data["next_action"] = "none"
                provider_data["next_detail"] = ""
            else:
                provider_data["state"] = "blocked"
                provider_data["next_action"] = f"inspect {provider} qualification failures"
                provider_data["next_detail"] = _extract_failure_detail(bound_result)
            continue

        # 2. If already complete, preserve state
        if current_state == "complete":
            if not poll_only:
                provider_data["next_action"] = "none"
            continue

        # 3. If running, check trigger status and retry if needed, then poll
        if current_state == "running":
            raw_dispatch_ref = provider_data.get("dispatch_ref", {})
            dispatch_ref = dict(raw_dispatch_ref) if isinstance(raw_dispatch_ref, Mapping) else {}
            ref_issue = dispatch_ref.get("issue_number") or issue_number
            comments: list[dict[str, Any]] = []
            poll_error = ""
            if ref_issue and repo_slug:
                comments, poll_error = _poll_github_comments(
                    repo_slug,
                    ref_issue,
                    gh_json_runner=gh_json_runner,
                )

            # A completed result is authoritative and must be consumed before
            # considering any retry side effect. Otherwise a missing trigger
            # marker could restart a provider that has already finished.
            found_result = None
            if poll_error:
                provider_data["error"] = poll_error
            else:
                provider_data["error"] = ""
                trusted_authors = _resolve_trusted_bot_authors(lane, env=current_env)
                for comment in comments:
                    if not _is_trusted_github_author(
                        _comment_author_login(comment), trusted_authors
                    ):
                        continue
                    found_result, rejection_detail = _extract_bound_adoption_result_ex(
                        str(comment.get("body") or ""),
                        campaign_id=campaign_id,
                        provider=provider,
                        release_tag=release_tag,
                        idempotency_key=str(provider_data.get("idempotency_key") or ""),
                        qualification_context=context,
                        starting_version=starting_version,
                        package_identity=package_identity,
                        package_source=package_source,
                    )
                    if found_result:
                        prior_found = provider_data.get("adoption_result")
                        if prior_found != found_result:
                            # Genuinely new evidence supersedes the stored
                            # attempt; record history only when a prior
                            # stored result actually existed (the first
                            # completion of a running attempt supersedes
                            # nothing). Identical evidence must not move
                            # chronology (see the drop-in path above).
                            if isinstance(prior_found, Mapping) and prior_found:
                                _record_attempt_history(provider_data)
                            provider_data["completed_at"] = now_utc
                        provider_data["adoption_result"] = found_result
                        provider_data["elapsed_seconds"] = float(
                            found_result.get("elapsed_seconds") or 0.0
                        )
                        provider_data["error"] = ""
                        outcome = found_result.get("outcome")
                        if outcome in {"pass", "pass_with_warnings"}:
                            provider_data["state"] = "complete"
                            provider_data["next_action"] = "none"
                            provider_data["next_detail"] = ""
                        else:
                            provider_data["state"] = "blocked"
                            provider_data["next_action"] = (
                                f"inspect {provider} qualification failures"
                            )
                            provider_data["next_detail"] = _extract_failure_detail(found_result)
                        break
                    if rejection_detail:
                        provider_data["error"] = _safe_error("hosted_result_rejected")
                        action, detail = _provider_next_action(
                            provider,
                            lane,
                            "blocked",
                            command_available=True,
                            has_credentials=True,
                            has_issue=True,
                            dry_run=False,
                            error=rejection_detail,
                        )
                        provider_data["next_action"] = action
                        provider_data["next_detail"] = detail
            if found_result is not None:
                continue

            # For manually triggered providers, retry trigger if not yet posted.
            # Skip this automatic retry if an explicit --retry-provider is active,
            # since the explicit redispatch path will post its own trigger.
            # Also gate on apply: trigger retry is a write operation, not a poll.
            trigger_comments = tuple(lane.provider_config.get("trigger_comments") or ())
            if not poll_only:
                provider_data.setdefault("trigger_posted", not bool(trigger_comments))
            trigger_posted = provider_data.get("trigger_posted", not bool(trigger_comments))

            if (
                trigger_comments
                and not trigger_posted
                and not is_explicit_retry
            ):
                dispatch_key = str(provider_data.get("dispatch_reconciliation_key") or "")
                trigger_key = str(provider_data.get("trigger_reconciliation_key") or "")
                if poll_only:
                    if poll_error or not dispatch_key or not trigger_key:
                        continue
                    dispatch_expected = {
                        "schema": DISPATCH_SCHEMA,
                        "campaign_id": campaign_id,
                        "provider": provider,
                        "idempotency_key": str(provider_data.get("idempotency_key") or ""),
                        "reconciliation_key": dispatch_key,
                        "posture": _provider_posture(provider_data),
                    }
                    dispatch_posted = bool(dispatch_ref.get("comment_posted"))
                    if not dispatch_posted and _has_matching_release_marker(
                        comments,
                        "CODE_MOWER_RELEASE_CAMPAIGN",
                        dispatch_expected,
                    ):
                        dispatch_ref["comment_posted"] = True
                        provider_data["dispatch_ref"] = dispatch_ref
                        dispatch_posted = True
                    trigger_expected = {
                        "schema": TRIGGER_MARKER_SCHEMA,
                        "campaign_id": campaign_id,
                        "provider": provider,
                        "reconciliation_key": trigger_key,
                    }
                    if dispatch_posted and _has_matching_release_marker(
                        comments,
                        "CODE_MOWER_RELEASE_TRIGGER",
                        trigger_expected,
                    ):
                        provider_data["trigger_posted"] = True
                        provider_data["response_deadline_at"] = _response_deadline(
                            now_utc,
                            _hosted_response_timeout(lane),
                        )
                        provider_data["next_action"], provider_data["next_detail"] = (
                            _provider_next_action(
                                provider,
                                lane,
                                "running",
                                command_available=True,
                                has_credentials=True,
                                has_issue=True,
                                dry_run=False,
                            )
                        )
                    continue
                if not dispatch_key or not trigger_key:
                    dispatch_key = dispatch_key or uuid.uuid4().hex
                    trigger_key = trigger_key or uuid.uuid4().hex
                    provider_data["dispatch_reconciliation_key"] = dispatch_key
                    provider_data["trigger_reconciliation_key"] = trigger_key
                    # Persist both nonces before any external side effect. The
                    # trigger nonce is deliberately absent from the dispatch
                    # comment, so it cannot be copied to forge trigger success.
                    _save_campaign_progress(campaign, campaigns_dir, now_utc=now_utc)

                dispatch_posted = bool(dispatch_ref.get("comment_posted"))
                dispatch_expected = {
                    "schema": DISPATCH_SCHEMA,
                    "campaign_id": campaign_id,
                    "provider": provider,
                    "idempotency_key": str(provider_data.get("idempotency_key") or ""),
                    "reconciliation_key": dispatch_key,
                    "posture": _provider_posture(provider_data),
                }
                if not dispatch_posted and not poll_error:
                    dispatch_posted = _has_matching_release_marker(
                        comments,
                        "CODE_MOWER_RELEASE_CAMPAIGN",
                        dispatch_expected,
                    )
                    if dispatch_posted:
                        dispatch_ref["comment_posted"] = True
                        provider_data["dispatch_ref"] = dispatch_ref

                trigger_expected = {
                    "schema": TRIGGER_MARKER_SCHEMA,
                    "campaign_id": campaign_id,
                    "provider": provider,
                    "reconciliation_key": trigger_key,
                }
                if poll_error:
                    provider_data["next_action"] = f"retry {provider} trigger reconciliation"
                    provider_data["next_detail"] = "GitHub comments are temporarily unavailable"
                elif not dispatch_posted:
                    provider_data["next_action"] = (
                        f"run with --apply --retry-provider {provider} to retry the dispatch"
                    )
                    provider_data["next_detail"] = (
                        "trigger withheld because the campaign dispatch is not confirmed"
                    )
                elif _has_matching_release_marker(
                    comments,
                    "CODE_MOWER_RELEASE_TRIGGER",
                    trigger_expected,
                ):
                    # The trigger nonce is never published in the earlier
                    # dispatch marker. Possession therefore authenticates this
                    # exact side effect without assuming which human-owned
                    # token posted the trigger comment.
                    provider_data["trigger_posted"] = True
                    provider_data["response_deadline_at"] = _response_deadline(
                        now_utc,
                        _hosted_response_timeout(lane),
                    )
                    provider_data["next_action"], provider_data["next_detail"] = (
                        _provider_next_action(
                            provider,
                            lane,
                            "running",
                            command_available=True,
                            has_credentials=True,
                            has_issue=True,
                            dry_run=False,
                        )
                    )
                elif not apply:
                    provider_data["next_action"] = (
                        f"run with --resume --apply to retry the {provider} trigger"
                    )
                    provider_data["next_detail"] = (
                        "trigger reconciliation is read-only without --apply"
                    )
                elif ref_issue and repo_slug:
                    trigger_ok, _trigger_ref, _trigger_err = _post_trigger_comment(
                        repo_slug,
                        ref_issue,
                        trigger_comments[0],
                        campaign_id=campaign_id,
                        provider=provider,
                        reconciliation_key=trigger_key,
                        command_runner=command_runner,
                    )
                    provider_data["trigger_posted"] = trigger_ok
                    if trigger_ok:
                        provider_data["response_deadline_at"] = _response_deadline(
                            now_utc,
                            _hosted_response_timeout(lane),
                        )
                        provider_data["next_action"], provider_data["next_detail"] = (
                            _provider_next_action(
                                provider,
                                lane,
                                "running",
                                command_available=True,
                                has_credentials=True,
                                has_issue=True,
                                dry_run=False,
                            )
                        )
                    else:
                        provider_data["next_action"] = f"retry {provider} trigger comment post"
                        provider_data["next_detail"] = "trigger comment post failed on retry"
                _save_campaign_progress(campaign, campaigns_dir, now_utc=now_utc)
                # After trigger reconciliation, continue to result polling below.

            trigger_posted = provider_data.get(
                "trigger_posted", not bool(trigger_comments)
            )
            if (
                lane.driver in {"saas_event", "hosted_bridge"}
                and trigger_posted
            ):
                deadline = provider_data.get("response_deadline_at")
                deadline_was_missing = not isinstance(deadline, str) or not deadline
                if deadline_was_missing:
                    # A running campaign created before response deadlines
                    # shipped gets a full window from its first upgraded poll.
                    # Backdating from attempted_at could falsely time out paid
                    # work immediately and encourage a duplicate retry.
                    deadline = _response_deadline(
                        now_utc,
                        _hosted_response_timeout(lane),
                    )
                    provider_data["response_deadline_at"] = deadline
                if (
                    not poll_error
                    and not is_explicit_retry
                    and not deadline_was_missing
                    and _response_deadline_expired(deadline, now_utc)
                ):
                    if provider_data.get("error") == _safe_error("hosted_result_rejected"):
                        # A trusted, correctly bound marker was present but its
                        # adoption result failed closed validation. The provider
                        # has responded; the rejection is the terminal state.
                        provider_data["state"] = "blocked"
                        provider_data["response_deadline_at"] = deadline
                    else:
                        provider_data["state"] = "unavailable"
                        provider_data["response_deadline_at"] = deadline
                        provider_data["error"] = _safe_error("hosted_response_timeout")
                        provider_data["next_action"] = (
                            f"record manual result for {provider} or run with --apply "
                            f"--retry-provider {provider}"
                        )
                        provider_data["next_detail"] = (
                            "hosted provider returned no trusted result before its response deadline"
                        )
                    continue

            if not is_explicit_retry:
                # Ordinary resume is poll/reconciliation-only and never
                # redispatches. Explicit retry may continue below only after
                # the trusted result scan above found no terminal evidence.
                continue
            # Explicit retry of a still-running provider with no valid
            # trusted result yet: fall through to the capability checks and
            # applied-dispatch section below for exactly one redispatch.

        if poll_only:
            continue

        # 3.5 Retry preview safety: --retry-provider without --apply must be
        # read-only. This covers a still-running provider with no new result
        # from the safe poll above, and a previously blocked/unavailable
        # attempted provider -- neither may be rewritten by the dry-run
        # evaluation below, and neither may be dispatched.
        if is_explicit_retry and not apply and bool(provider_data.get("attempted_at")):
            provider_data["next_action"] = (
                f"run with --apply --retry-provider {provider} to retry {provider}"
            )
            continue

        # 3.6 Ordinary (non-retry) dry-run resume of an already-attempted
        # terminal-failure provider must preserve its state, evidence, error,
        # and attempted_at -- merely observing it in a dry-run must not turn a
        # recorded failure back into queued/unavailable capability guesswork.
        # Only an explicit --retry-provider (handled above and below) may
        # move it forward.
        if (
            not apply
            and not is_explicit_retry
            and bool(provider_data.get("attempted_at"))
            and current_state in {"blocked", "unavailable"}
        ):
            _record_dry_run_dispatch_mode(provider_data)
            provider_data["next_action"] = (
                f"run with --apply --retry-provider {provider} to retry {provider}"
            )
            continue

        # 4. Check capabilities and readiness
        cmd_found = _find_command(lane, which_fn=which_fn)
        has_creds, missing_cred = _check_credentials(lane, env=current_env)
        transport_ready, _ = _check_hosted_transport(lane, env=current_env)
        # Closed hosted dispatch profile: auth, installation, trigger,
        # trusted responder, and result return are judged independently, so
        # one verified dimension can never mask another.
        dispatch_profile = (
            hosted_dispatch_profile(lane, env=current_env)
            if lane.driver in {"hosted_bridge", "saas_event"}
            else {}
        )
        # Every failed check of the closed dispatch profile blocks the
        # preview: auth, installation, trigger, trusted responder, and result
        # return are judged independently, so one verified dimension can
        # never mask another. Auth and issue prerequisites keep their more
        # specific errors in the branches above; anything still failing here
        # surfaces with its exact bounded remediation.
        dispatch_blockers = (
            hosted_dispatch_blockers(dispatch_profile)
            if lane.driver in {"hosted_bridge", "saas_event"}
            else []
        )
        if lane.driver in {"hosted_bridge", "saas_event"}:
            provider_data["transport_verified"] = transport_ready
        has_issue = bool(issue_number)
        effective_argv_template, _, adapter_config_error, _ = _resolve_campaign_adapter_config(
            lane, repo_path
        )
        adapter_configured = bool(effective_argv_template) and not adapter_config_error

        # 5. Dry-run evaluations
        if not apply:
            _record_dry_run_dispatch_mode(provider_data)
            if lane.driver == "local_cli" and not adapter_configured:
                provider_data["state"] = "unavailable"
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=bool(cmd_found),
                    has_credentials=True,
                    has_issue=True,
                    dry_run=True,
                    adapter_configured=False,
                )
            elif lane.driver == "local_cli" and not cmd_found:
                provider_data["state"] = "unavailable"
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=False,
                    has_credentials=True,
                    has_issue=True,
                    dry_run=True,
                    error=f"command not found: {lane.provider_config.get('command') or provider}",
                )
            elif lane.driver in {"hosted_bridge", "saas_event"} and not has_creds:
                provider_data["state"] = "unavailable"
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=False,
                    has_issue=has_issue,
                    dry_run=True,
                    error=missing_cred,
                )
            elif lane.driver in {"hosted_bridge", "saas_event"} and (
                not issue_number or not repo_slug
            ):
                # Same prerequisite the applied path enforces below: a hosted
                # dispatch is a comment on a specific GitHub issue, so without
                # an issue number (and the repo slug that addresses it) there
                # is nothing --apply could dispatch. The preview must say so
                # rather than report the provider queued and ready, which sent
                # the operator to an --apply run that only ever came back
                # unavailable. Evaluating this needs no network call: both
                # values are already in hand.
                provider_data["state"] = "unavailable"
                provider_data["error"] = _safe_error("missing_issue_number")
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=True,
                    has_issue=False,
                    dry_run=True,
                    error="missing issue number",
                )
            elif lane.driver in {"hosted_bridge", "saas_event"} and dispatch_blockers:
                # A paid dispatch must never preview as queued while any
                # closed dispatch-profile check fails: the preview judges
                # exactly what --apply would need, so it reports unavailable
                # with the exact bounded remediation before any dispatch. An
                # explicit --apply may still dispatch (the operator's
                # choice), under the bounded response deadline.
                provider_data["state"] = "unavailable"
                provider_data["error"] = _safe_error("hosted_transport_unverified")
                blocker_remediations = [
                    str(dispatch_profile.get(name, {}).get("remediation") or "")
                    for name in dispatch_blockers
                ]
                action = "; ".join(item for item in blocker_remediations if item)
                if not action:
                    action = (
                        f"resolve {', '.join(dispatch_blockers)} for {provider} "
                        f"before dispatching release qualification"
                    )
                detail = (
                    f"{provider} hosted dispatch blocked: "
                    f"{', '.join(dispatch_blockers)}"
                )
                if action:
                    detail = f"{detail}; {action}"
            else:
                provider_data["state"] = "queued"
                # A prerequisite recorded by an earlier preview (a missing
                # issue number, say) is stale once this evaluation finds the
                # provider dispatchable again; a queued provider must not keep
                # advertising an error it no longer has. Previously-attempted
                # failures never reach here -- they are preserved above.
                provider_data["error"] = ""
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "queued",
                    command_available=bool(cmd_found),
                    has_credentials=has_creds,
                    has_issue=has_issue,
                    dry_run=True,
                )
            provider_data["next_action"] = action
            provider_data["next_detail"] = detail
            continue

        # 6. Applied dispatch (requires explicit apply flag)
        provider_data["dispatch_mode"] = "applied"

        attempt_gated = lane.driver in {"local_cli", "saas_event", "hosted_bridge"}
        already_attempted = bool(provider_data.get("attempted_at"))
        if attempt_gated and already_attempted and not is_explicit_retry:
            # A prior applied attempt (success, failure, or uncertain outcome)
            # was already made and persisted -- resume must not silently repeat
            # a paid/hosted dispatch or local adapter invocation. Only the
            # explicit --retry-provider flag may do that.
            provider_data["next_action"] = f"use --retry-provider {provider} to retry {provider}"
            continue

        if lane.driver == "local_cli":
            results_dir.mkdir(parents=True, exist_ok=True)
            result_path = results_dir / f"{campaign_id}_{provider}.json"
            # Remove any pre-existing result file (e.g. from an attempt being
            # retried) before invoking, so a new attempt that fails to write
            # its own output can never be satisfied by stale evidence left
            # behind on disk.
            try:
                result_path.unlink()
            except FileNotFoundError:
                pass
            # A new attempt supersedes the stored one: keep its bounded
            # metadata-only summary before restamping. First attempts record
            # nothing (there is no prior attempt to summarize). The complete
            # superseded current-attempt surface -- stored result, both
            # timestamps, and elapsed time -- is reset before the new
            # attempt, so a result-less failed retry (or an interruption
            # before the adapter finishes) cannot retain superseded
            # evidence or chronology. A successful completion restamps its
            # new values normally below.
            _record_attempt_history(provider_data)
            provider_data["adoption_result"] = None
            provider_data["completed_at"] = None
            provider_data["dispatched_at"] = None
            provider_data["elapsed_seconds"] = 0.0
            provider_data["attempted_at"] = now_utc
            provider_data["state"] = "running"
            provider_data["error"] = ""
            (
                provider_data["next_action"],
                provider_data["next_detail"],
            ) = _provider_next_action(
                provider,
                lane,
                "running",
                command_available=True,
                has_credentials=True,
                has_issue=True,
                dry_run=False,
            )
            _save_campaign_progress(campaign, campaigns_dir, now_utc=now_utc)
            start_p = time.time()
            result, error_code, detail = _invoke_local_adapter(
                lane,
                provider,
                release_tag=release_tag,
                package_spec=package_spec,
                qualification_context=context,
                starting_version=starting_version,
                package_source=package_source,
                output_path=result_path,
                repo_path=repo_path,
                which_fn=which_fn,
                adapter_runner=adapter_runner,
                environ=current_env,
            )
            if result is None:
                provider_data["state"] = _ADAPTER_ERROR_STATE.get(error_code, "unavailable")
                provider_data["error"] = error_code
                if error_code == "adapter_configuration_invalid":
                    provider_data["next_action"] = (
                        f"fix campaign_adapter_argv/campaign_adapter_timeout_seconds "
                        f"configuration for {provider} or record manual result"
                    )
                    provider_data["next_detail"] = detail
                else:
                    provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                        provider,
                        lane,
                        provider_data["state"],
                        command_available=error_code != "command_not_found",
                        has_credentials=True,
                        has_issue=True,
                        dry_run=False,
                        adapter_configured=adapter_configured,
                        error=detail,
                        error_code=error_code,
                    )
            else:
                provider_data["adoption_result"] = result
                provider_data["dispatched_at"] = now_utc
                provider_data["completed_at"] = now_utc
                provider_data["elapsed_seconds"] = round(time.time() - start_p, 2)
                provider_data["error"] = ""
                outcome = result.get("outcome")
                if outcome in {"pass", "pass_with_warnings"}:
                    provider_data["state"] = "complete"
                    provider_data["next_action"] = "none"
                    provider_data["next_detail"] = ""
                else:
                    provider_data["state"] = "blocked"
                    provider_data["next_action"] = f"inspect {provider} qualification failures"
                    provider_data["next_detail"] = _extract_failure_detail(result)

        elif lane.driver in {"saas_event", "hosted_bridge"}:
            # Only an explicit retry can reach this branch with the provider
            # already `running` against a known issue -- i.e. a dispatch whose
            # outcome is still uncertain. A prerequisite refusal below performs
            # no external call and learns nothing about that dispatch, so it
            # must not take the provider's pollability away: demoting it to
            # `unavailable` would stop every later resume from reading the
            # comment the interrupted attempt may already have posted. Such a
            # retry keeps polling and reports the prerequisite to supply.
            unmet_prerequisite_state = (
                "running"
                if current_state == "running"
                and (provider_data.get("dispatch_ref") or {}).get("issue_number")
                else "unavailable"
            )
            if not has_creds:
                provider_data["state"] = unmet_prerequisite_state
                provider_data["error"] = _safe_error("missing_credentials")
                provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=False,
                    has_issue=has_issue,
                    dry_run=False,
                    error=missing_cred,
                )
            elif not issue_number or not repo_slug:
                provider_data["state"] = unmet_prerequisite_state
                provider_data["error"] = _safe_error("missing_issue_number")
                provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=True,
                    has_issue=False,
                    dry_run=False,
                    error="missing issue number",
                )
            elif context == "upgrade" and not starting_version:
                # Fail closed before any attempt is recorded: an upgrade
                # dispatch that cannot advertise its exact starting_version
                # could be answered by a result from any starting version.
                provider_data["state"] = unmet_prerequisite_state
                provider_data["error"] = _safe_error("campaign_identity_incomplete")
                provider_data["next_action"] = (
                    f"recreate the campaign with --starting-version before dispatching {provider}"
                )
                provider_data["next_detail"] = "upgrade campaign is missing starting_version"
            else:
                # Checkpoint a *pollable* uncertain state before the external
                # post, not merely an attempt claim. The post is a side effect
                # this process cannot undo and cannot re-observe: once `gh` has
                # been invoked, a process exit before the final save leaves
                # whatever this checkpoint wrote as the whole record of it.
                #
                # Recording only `attempted_at` while the provider stayed
                # `queued` was that record, and it was unpollable: an ordinary
                # resume skips a queued provider's poll and declines to
                # redispatch an attempted one, so the campaign stalled until
                # someone ran an explicit --retry-provider, which then posted a
                # second comment for a dispatch that may well have succeeded.
                #
                # So the checkpoint states everything a later resume needs to
                # find out what really happened: the attempt, `running` (the
                # one state whose resume path polls), the issue the comment was
                # addressed to, and the applied dispatch mode. It deliberately
                # does *not* state that the post succeeded -- `dispatched_at`
                # stays unset and `comment_posted` is False until `gh` returns
                # zero -- because polling an issue that never received a comment
                # is safe and self-correcting, while inferring a successful post
                # is neither. If nothing was ever posted, resume keeps polling
                # and finds nothing, and the existing explicit-retry policy
                # remains the only way to dispatch again.
                #
                # A redispatch supersedes the stored attempt, so its bounded
                # metadata-only summary is kept first (a first dispatch
                # records nothing) and the complete superseded
                # current-attempt surface -- stored result, both timestamps,
                # and elapsed time -- is reset before the new attempt, so a
                # result-less failed retry (or an interruption after this
                # checkpoint) cannot upload superseded evidence or retain
                # superseded chronology. A subsequent successful dispatch
                # stamps its new dispatched_at normally below.
                _record_attempt_history(provider_data)
                provider_data["adoption_result"] = None
                provider_data["completed_at"] = None
                provider_data["dispatched_at"] = None
                provider_data["elapsed_seconds"] = 0.0
                provider_data["attempted_at"] = now_utc
                provider_data["state"] = "running"
                provider_data["error"] = ""
                provider_data["dispatch_ref"] = {
                    "issue_number": str(issue_number),
                    "comment_posted": False,
                }
                # A crash after the dispatch post but before the trigger post
                # must remain retriable, so record the trigger as not-yet-posted
                # for manually triggered providers before the pre-post save.
                trigger_comments = tuple(lane.provider_config.get("trigger_comments") or ())
                provider_data["trigger_posted"] = not bool(trigger_comments)
                provider_data["response_deadline_at"] = (
                    None
                    if trigger_comments
                    else _response_deadline(
                        now_utc,
                        _hosted_response_timeout(lane),
                    )
                )
                if trigger_comments:
                    provider_data["dispatch_reconciliation_key"] = uuid.uuid4().hex
                    provider_data["trigger_reconciliation_key"] = uuid.uuid4().hex
                (
                    provider_data["next_action"],
                    provider_data["next_detail"],
                ) = _provider_next_action(
                    provider,
                    lane,
                    "running",
                    command_available=True,
                    has_credentials=True,
                    has_issue=True,
                    dry_run=False,
                )
                _save_campaign_progress(campaign, campaigns_dir, now_utc=now_utc)
                trigger_comments = tuple(lane.provider_config.get("trigger_comments") or ())
                ok, ref, err = _dispatch_github_comment(
                    repo_slug,
                    issue_number,
                    campaign_id,
                    release_tag,
                    package_spec,
                    provider,
                    context,
                    provider_data["idempotency_key"],
                    posture=_provider_posture(provider_data),
                    starting_version=starting_version,
                    package_source=package_source,
                    trigger_comments=trigger_comments,
                    reconciliation_key=str(
                        provider_data.get("dispatch_reconciliation_key") or ""
                    ),
                    command_runner=command_runner,
                )
                if ok:
                    # The post is confirmed: replace the uncertain checkpoint
                    # reference with the returned dispatch metadata and stamp
                    # the dispatch time the checkpoint withheld.
                    provider_data["state"] = "running"
                    provider_data["error"] = ""
                    provider_data["dispatched_at"] = now_utc
                    provider_data["dispatch_ref"] = ref
                    provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                        provider,
                        lane,
                        "running",
                        command_available=True,
                        has_credentials=True,
                        has_issue=True,
                        dry_run=False,
                    )
                    # Make the confirmed dispatch durable before posting a
                    # trigger that may start a paid provider run.
                    _save_campaign_progress(campaign, campaigns_dir, now_utc=now_utc)
                    # For manually triggered providers, post the trigger command
                    # as a separate comment so the provider will actually start.
                    if trigger_comments:
                        trigger_ok, _trigger_ref, _trigger_err = _post_trigger_comment(
                            repo_slug,
                            issue_number,
                            trigger_comments[0],
                            campaign_id=campaign_id,
                            provider=provider,
                            reconciliation_key=str(
                                provider_data["trigger_reconciliation_key"]
                            ),
                            command_runner=command_runner,
                        )
                        # Persist trigger status so resume can retry on failure
                        provider_data["trigger_posted"] = trigger_ok
                        if trigger_ok:
                            provider_data["response_deadline_at"] = _response_deadline(
                                now_utc,
                                _hosted_response_timeout(lane),
                            )
                        if not trigger_ok:
                            provider_data["next_action"] = (
                                f"retry {provider} trigger comment post"
                            )
                            provider_data["next_detail"] = (
                                f"{provider_data['next_detail']}; trigger comment may not have posted"
                            )
                    else:
                        # Non-trigger providers are immediately pollable
                        provider_data["trigger_posted"] = True
                else:
                    # The dispatch failed *in process*, so this run knows the
                    # outcome and records it: the checkpoint's provisional
                    # `running` is replaced by the unavailable/error result,
                    # and the reference is left reporting that no comment was
                    # posted.
                    provider_data["state"] = "unavailable"
                    provider_data["response_deadline_at"] = None
                    provider_data["error"] = err
                    if err == "campaign_identity_incomplete":
                        provider_data["next_action"] = (
                            f"recreate the campaign with --starting-version before "
                            f"dispatching {provider}"
                        )
                        provider_data["next_detail"] = (
                            "upgrade campaign is missing starting_version"
                        )
                    else:
                        provider_data["next_action"] = (
                            f"retry {provider} dispatch when GitHub is available"
                        )
                        provider_data["next_detail"] = ""
        else:
            provider_data["state"] = "unavailable"
            provider_data["next_action"] = f"record manual adoption result for {provider}"
            provider_data["next_detail"] = "manual adapter fallback"

    if not poll_only or campaign != campaign_before_poll:
        _save_campaign_progress(campaign, campaigns_dir, now_utc=now_utc)
    return campaign


def record_manual_result(
    campaign: dict[str, Any],
    provider: str,
    result_path_or_dict: Path | dict[str, Any],
    *,
    campaigns_dir: Path | None = None,
    repo_path: Path | None = None,
) -> dict[str, Any]:
    if isinstance(result_path_or_dict, Path):
        try:
            with result_path_or_dict.open("r", encoding="utf-8") as fh:
                adoption_res = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "adoption result file is missing, unreadable, or not valid JSON"
            ) from exc
    else:
        adoption_res = result_path_or_dict

    package_identity = campaign_package_identity(str(campaign.get("package_spec") or ""))
    if not package_identity:
        raise ValueError(
            "campaign package spec is not an exact package-index spec, so no "
            "adoption result can be bound to it"
        )
    validate_adoption_result_payload(
        adoption_res, expected_package_identity=package_identity
    )

    release_tag = campaign.get("release_tag")
    if adoption_res.get("release_tag") != release_tag:
        raise ValueError(
            "adoption result release_tag does not match the campaign release tag"
        )

    canonical_provider, _ = resolve_provider_lane(provider)
    if adoption_res.get("provider") != canonical_provider:
        raise ValueError(
            "adoption result provider does not match the recorded provider"
        )

    qualification_context = campaign.get("qualification_context")
    if adoption_res.get("qualification_context") != qualification_context:
        raise ValueError(
            "adoption result qualification_context does not match campaign qualification_context"
        )
    campaign_starting_version = str(campaign.get("starting_version") or "")
    if str(adoption_res.get("starting_version") or "") != campaign_starting_version:
        raise ValueError(
            "adoption result starting_version does not match campaign starting_version"
        )

    matched = False
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for p in campaign.get("providers", []):
        if p.get("provider") == canonical_provider:
            # A re-recorded result supersedes the stored attempt: keep its
            # bounded metadata-only summary first (a first record keeps none).
            # Byte-identical evidence is idempotent: no history entry and no
            # completion restamp.
            if p.get("adoption_result") == adoption_res:
                matched = True
                break
            _record_attempt_history(p)
            p["adoption_result"] = adoption_res
            p["elapsed_seconds"] = float(adoption_res.get("elapsed_seconds") or 0.0)
            p["error"] = ""
            outcome = adoption_res.get("outcome")
            if outcome in {"pass", "pass_with_warnings"}:
                p["state"] = "complete"
                p["next_action"] = "none"
                p["next_detail"] = ""
            else:
                p["state"] = "blocked"
                p["next_action"] = f"inspect {canonical_provider} qualification failures"
                p["next_detail"] = f"outcome: {outcome}"
            p["completed_at"] = now_utc
            matched = True
            break

    if not matched:
        raise ValueError(f"Provider {provider} not found in campaign")

    overall_status, next_action, next_detail = _aggregate_campaign_status(
        campaign.get("providers", []),
        dry_run=campaign.get("dry_run", True),
    )
    campaign["status"] = overall_status
    campaign["next_action"] = next_action
    campaign["next_detail"] = next_detail
    campaign["updated_at"] = now_utc

    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path or Path.cwd())
    save_campaign(campaign, campaigns_dir)
    return campaign


def _load_cloud_client() -> Any:
    """Import the cloud client lazily, on the upload path only.

    Campaign creation, dispatch, polling, and status must keep working with no
    cloud surface loaded at all, so the import happens where it is used rather
    than at module import time. It also keeps the dependency one-directional:
    ``code_mower.board`` imports this module lazily for its campaign
    projection, and the cloud client imports ``board``.
    """
    if __package__ in {None, ""}:  # pragma: no cover - script-mode fallback
        from code_mower import cloud_client
    else:
        from . import cloud_client
    return cloud_client


CAMPAIGN_UPLOAD_SCHEMA = "code_mower.releaseCampaignUpload.v1"
CAMPAIGN_UPLOAD_SOURCE = "code-mower release campaign upload"

# The only package identity the cloud `adoption_run` contract accepts. A
# campaign for another distribution is refused with one bounded error instead of
# being reported as six malformed provider results.
CAMPAIGN_UPLOAD_PACKAGE_IDENTITY = "code-mower"

# Why a *completed* provider's stored adoption result could not be converted
# into the closed adoption_run event contract. Closed and bounded like
# SAFE_ERROR_CODES: a rejection reports one of these codes and nothing else --
# never a validator message, a file path, or any part of the stored result.
CAMPAIGN_UPLOAD_REJECT_CODES = frozenset(
    {
        "provider_list_invalid",
        "provider_entry_invalid",
        "adoption_result_missing",
        "adoption_result_invalid",
        "adoption_result_unconvertible",
        "adoption_result_state_mismatch",
    }
)

# Outcomes the campaign state machine stores under each terminal provider
# state (see the `complete`/`blocked` assignments in
# dispatch_or_advance_campaign and record_manual_result): `complete` carries
# only passing evidence, `blocked` only failing/incomplete evidence. Upload
# enforces this pairing so a corrupted or hand-edited campaign whose terminal
# state contradicts its bound result outcome is rejected, never published.
COMPLETE_STATE_OUTCOMES = frozenset({"pass", "pass_with_warnings"})
BLOCKED_STATE_OUTCOMES = frozenset({"fail", "incomplete"})

# A campaign's provider list holds at most one entry per known provider, so this
# bound is far above anything the tool itself writes. It exists because a stored
# campaign file is untrusted input: the upload converter iterates `providers`
# only after confirming it is an actual bounded list, so a hand-edited file
# carrying `null`, a number, a string, or a million entries is refused with one
# bounded rejection instead of raising TypeError or being walked element by
# element.
MAX_CAMPAIGN_UPLOAD_PROVIDERS = 64

_PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _safe_provider_name(value: Any) -> str:
    """Project a stored provider name, or ``unknown`` for anything else.

    Campaign files are untrusted input on this path -- the upload summary is
    printed and uploaded-adjacent -- so a hand-edited provider field can only
    ever surface as a safe identifier.
    """
    return value if isinstance(value, str) and _PROVIDER_NAME_PATTERN.match(value) else "unknown"


def _skipped_provider_row(entry: Mapping[str, Any], provider: str, state: str) -> dict[str, str]:
    error = entry.get("error")
    return {
        "provider": provider,
        "posture": _provider_posture(entry),
        "state": state,
        "reason": error if isinstance(error, str) and error in SAFE_ERROR_CODES else "",
    }


def build_campaign_upload_events(
    campaign: Mapping[str, Any],
    *,
    team_id: str = "",
    install_id: str = "",
    source: str = CAMPAIGN_UPLOAD_SOURCE,
) -> dict[str, Any]:
    """Convert every terminal provider's revalidated result into adoption_run events.

    Terminal state is the campaign's own record, but it is never taken on
    trust: each stored result is revalidated against the closed
    adoption-result schema and rebound to this campaign's provider, release
    tag, package identity, qualification context, and starting version -- the
    same contract :func:`is_bound_adoption_result` applies to a drop-in result
    file -- before :func:`cloud_client.adoption_result_to_event` converts it.
    Conversion itself enforces the metadata-only adoption_run contract, so
    missing model, token, and cost data stays unavailable rather than
    zero-filled, and no report text, output, path, or prose can reach an
    event.

    Both terminal states carry evidence: `complete` holds passing results and
    `blocked` holds failing ones (`fail`, and `incomplete` where the schema
    allows it), so every schema-valid executed terminal result becomes one
    metadata-only event. A provider that is not terminal is *skipped* and
    counted separately: a queued, running, or unavailable provider has no
    evidence to publish, which is not an error -- and neither does a
    `blocked` provider whose adapter never produced a result file. A terminal
    provider whose stored result is present but missing, unbindable, or
    malformed is *rejected*, with one bounded code from
    :data:`CAMPAIGN_UPLOAD_REJECT_CODES` -- as is a terminal provider whose
    state contradicts its bound result outcome (`complete` with a
    failing/incomplete outcome, or `blocked` with a passing one), which can
    only come from corrupted or hand-edited storage. The caller refuses the
    whole upload in that case: silently skipping it would publish a partial
    event set while reporting success, and repairing it would fabricate
    evidence.

    The stored ``providers`` value is itself untrusted: it is converted only
    when it is an actual list or tuple of at most
    :data:`MAX_CAMPAIGN_UPLOAD_PROVIDERS` entries. A hand-edited campaign whose
    ``providers`` is missing, ``null``, a scalar, a string, a mapping, or
    oversized is refused whole with a single ``provider_list_invalid``
    rejection -- the same bounded ``invalid_results`` outcome as an unusable
    result, never a ``TypeError`` and never an element-by-element walk of an
    unbounded value.

    Each entry's stored ``state`` is untrusted in the same way. It is compared
    against :data:`VALID_PROVIDER_STATES` only after it is known to be a string,
    because an unhashable hand-edited value such as a list or a mapping makes
    that membership test raise ``TypeError`` rather than answer ``False``. A
    state of any unrecognized type reads as ``unavailable`` -- exactly as a
    stray string state already does -- so the provider is skipped, has no
    evidence published, and its raw stored value never reaches the summary.

    The event list is ordered by provider name, so the same campaign always
    produces the same event set in the same order -- what a preview shows is
    exactly what ``--yes`` uploads.
    """
    cloud = _load_cloud_client()
    release_tag = str(campaign.get("release_tag") or "")
    qualification_context = str(campaign.get("qualification_context") or "")
    starting_version = str(campaign.get("starting_version") or "")
    repo_slug = str(campaign.get("repo_slug") or "")
    package_identity = campaign_package_identity(str(campaign.get("package_spec") or ""))

    converted: list[tuple[str, dict[str, Any]]] = []
    skipped: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    complete_count = 0

    entries = campaign.get("providers")
    if not isinstance(entries, (list, tuple)) or len(entries) > MAX_CAMPAIGN_UPLOAD_PROVIDERS:
        return {
            "events": [],
            "accepted_providers": [],
            "skipped_providers": [],
            "rejected_providers": [
                {"provider": "unknown", "state": "unknown", "reason": "provider_list_invalid"}
            ],
            "provider_count": 0,
            "complete_count": 0,
            "package_identity": package_identity,
            "repo_slug": repo_slug,
        }

    posture_configured = bool(
        campaign.get("provider_posture_configured")
        if isinstance(campaign, Mapping)
        else getattr(campaign, "provider_posture_configured", False)
    )

    for entry in entries:
        if not isinstance(entry, Mapping):
            complete_count += 1
            rejected.append(
                {"provider": "unknown", "state": "unknown", "reason": "provider_entry_invalid"}
            )
            continue
        provider = _safe_provider_name(entry.get("provider"))
        # Membership is tested only once the stored state is known to be a
        # string: an unhashable hand-edited value (a list, a mapping) makes
        # `value in VALID_PROVIDER_STATES` raise TypeError instead of answering
        # False. Anything unrecognized reads as `unavailable`, as a stray string
        # state already does.
        state = entry.get("state")
        if not isinstance(state, str) or state not in VALID_PROVIDER_STATES:
            state = "unavailable"
        if state not in TERMINAL_EVIDENCE_STATES:
            skipped.append(_skipped_provider_row(entry, provider, str(state)))
            continue
        result = entry.get("adoption_result")
        if not isinstance(result, Mapping) or not result:
            if state == "complete":
                complete_count += 1
                rejected.append(
                    {
                        "provider": provider,
                        "posture": _provider_posture(entry),
                        "state": state,
                        "reason": "adoption_result_missing",
                    }
                )
            else:
                # Blocked without stored evidence: the adapter failed before
                # producing a result. Nothing to publish, and not an error.
                skipped.append(_skipped_provider_row(entry, provider, str(state)))
            continue
        if state == "complete":
            complete_count += 1
        if not is_bound_adoption_result(
            dict(result),
            provider=provider,
            release_tag=release_tag,
            qualification_context=qualification_context,
            starting_version=starting_version,
            package_identity=package_identity,
        ):
            rejected.append(
                {
                    "provider": provider,
                    "posture": _provider_posture(entry),
                    "state": state,
                    "reason": "adoption_result_invalid",
                }
            )
            continue
        # Terminal state is never normalized into evidence: a `complete`
        # provider may carry only a passing outcome, and `blocked` terminal
        # evidence must carry a failing/incomplete outcome, exactly as the
        # state machine stores them. A contradictory pair can only come from
        # corrupted or hand-edited storage, so it is rejected with a bounded
        # metadata-only reason rather than published as successful evidence.
        outcome = result.get("outcome")
        allowed_outcomes = (
            COMPLETE_STATE_OUTCOMES if state == "complete" else BLOCKED_STATE_OUTCOMES
        )
        if outcome not in allowed_outcomes:
            rejected.append(
                {
                    "provider": provider,
                    "posture": _provider_posture(entry),
                    "state": state,
                    "reason": "adoption_result_state_mismatch",
                }
            )
            continue
        provider_posture = (
            _stored_provider_posture(entry) if posture_configured else None
        )
        try:
            event = cloud.adoption_result_to_event(
                dict(result),
                repo_slug=repo_slug,
                team_id=team_id,
                install_id=install_id,
                source=source,
                provider_posture=provider_posture,
            )
        except cloud.CloudBundleError:
            # The converter's message describes the offending field, but it is
            # derived from stored campaign content; only the bounded code
            # crosses into the summary.
            rejected.append(
                {
                    "provider": provider,
                    "posture": _provider_posture(entry),
                    "state": state,
                    "reason": "adoption_result_unconvertible",
                }
            )
            continue
        converted.append((provider, event))

    converted.sort(key=lambda item: item[0])
    return {
        "events": [event for _, event in converted],
        "accepted_providers": [provider for provider, _ in converted],
        "skipped_providers": sorted(skipped, key=lambda row: row["provider"]),
        "rejected_providers": sorted(rejected, key=lambda row: row["provider"]),
        "provider_count": len(entries),
        "complete_count": complete_count,
        "package_identity": package_identity,
        "repo_slug": repo_slug,
        "provider_postures": {
            _safe_provider_name(entry.get("provider")): _provider_posture(entry)
            for entry in entries
            if isinstance(entry, Mapping)
        },
    }


def _campaign_upload_next_action(
    *,
    status: str,
    event_count: int,
    rejected: Sequence[Mapping[str, str]],
    skipped: Sequence[Mapping[str, str]],
    token_status: str,
) -> tuple[str, str]:
    """The one bounded, safe next action for an upload outcome.

    Every branch names a command the caller can actually run next, and nothing
    else: no stored result content, no endpoint response text, no paths.
    """
    if status == "invalid_results":
        names = ", ".join(sorted({str(row.get("provider") or "unknown") for row in rejected}))
        return (
            f"inspect stored qualification results for: {names}",
            "re-record them with --record-result, or retry those providers",
        )
    if status == "no_events":
        return (
            "complete at least one provider before uploading",
            f"{len(skipped)} provider(s) have no terminal qualification evidence",
        )
    if status == "uploaded":
        return ("none", f"{event_count} adoption_run event(s) uploaded")
    if token_status == "ambiguous":
        return (
            "select one Code Mower Cloud token profile with --install-id or --token-file",
            "several stored token profiles matched, so none was selected",
        )
    if token_status != "ok":
        return (
            "run `code-mower cloud setup --token-stdin` before uploading with --yes",
            "no Code Mower Cloud token was resolved for this upload",
        )
    return (
        "re-run `code-mower release campaign upload --yes` to upload",
        f"{event_count} adoption_run event(s) previewed; nothing left this machine",
    )


def render_campaign_upload_text(result: Mapping[str, Any]) -> str:
    counts = result.get("counts", {})
    lines = [
        f"Release Campaign Upload: {result.get('release_tag')} ({result.get('campaign_id')})",
        f"Status: {result.get('status')}",
        f"Endpoint: {result.get('endpoint')}",
        f"Events: {counts.get('events', 0)} adoption_run event(s) "
        f"from {counts.get('accepted', 0)} terminal provider(s)",
        f"Skipped: {counts.get('skipped', 0)} provider(s) without terminal evidence",
        f"Rejected: {counts.get('rejected', 0)} terminal provider(s) with unusable results",
        "Model/token/cost: unavailable (metadata-only, never zero-filled)",
        f"Next: {result.get('next_action')}",
    ]
    if result.get("next_detail"):
        lines.append(f"Detail: {result.get('next_detail')}")
    if result.get("status") == "dry_run":
        lines.append("Network: skipped (pass --yes to upload)")
    return "\n".join(lines)


def campaign_upload(
    campaign: Mapping[str, Any],
    *,
    endpoint: str = "",
    token_env: str = "",
    token_file: Path | None = None,
    token_dir: Path | None = None,
    install_id: str = "",
    team_id: str = "",
    yes: bool = False,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Preview or perform the cloud upload of one campaign's terminal evidence.

    Preview is the default and is the same computation as the applied upload:
    both build the identical deterministic event set and the identical payload,
    and only the final network post is conditional on ``yes``. The token
    resolver, endpoint resolution, bundle/metadata validation, idempotent event
    ids, and HTTP posting are all the shared cloud client's -- none of it is
    reimplemented here.

    Returns a metadata-only summary. Raises ``CloudBundleError`` (the cloud
    client's) for a bounded local failure such as an unresolvable token; the
    caller renders it. A campaign whose completed results cannot all be
    converted returns ``status: invalid_results`` and uploads nothing.
    """
    cloud = _load_cloud_client()
    resolved_token_env = token_env or cloud.DEFAULT_TOKEN_ENV
    requested_endpoint = endpoint or os.environ.get(
        "CODE_MOWER_CLOUD_ENDPOINT", cloud.DEFAULT_UPLOAD_ENDPOINT
    )
    token_resolution = cloud.resolve_cloud_token(
        token_env=resolved_token_env,
        token_file=token_file,
        token_dir=token_dir,
        install_id=install_id,
    )
    resolved_endpoint = cloud.resolve_cloud_endpoint(requested_endpoint, token_resolution)
    resolved_team_id, resolved_install_id = cloud.resolve_cloud_identity(
        team_id=team_id,
        install_id=install_id,
        resolution=token_resolution,
    )

    package_identity = campaign_package_identity(str(campaign.get("package_spec") or ""))
    if package_identity != CAMPAIGN_UPLOAD_PACKAGE_IDENTITY:
        raise cloud.CloudBundleError(
            "the cloud adoption_run contract accepts only "
            f"{CAMPAIGN_UPLOAD_PACKAGE_IDENTITY!r} release qualification; this "
            "campaign qualifies a different package, so its results are not uploadable"
        )

    plan = build_campaign_upload_events(
        campaign,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
    )
    events = plan["events"]
    summary: dict[str, Any] = {
        "schema": CAMPAIGN_UPLOAD_SCHEMA,
        "mode": "release-campaign-upload",
        "campaign_id": str(campaign.get("campaign_id") or ""),
        "release_tag": str(campaign.get("release_tag") or ""),
        "qualification_context": str(campaign.get("qualification_context") or ""),
        "package_identity": package_identity,
        "endpoint": resolved_endpoint,
        "token_status": token_resolution.status,
        "requires_yes": not yes,
        "upload_mode": "metadata_only",
        "counts": {
            "providers": plan["provider_count"],
            "complete": plan["complete_count"],
            "skipped": len(plan["skipped_providers"]),
            "accepted": len(plan["accepted_providers"]),
            "rejected": len(plan["rejected_providers"]),
            "events": len(events),
        },
        "accepted_providers": plan["accepted_providers"],
        "skipped_providers": plan["skipped_providers"],
        "rejected_providers": plan["rejected_providers"],
        # The event set is derived deterministically from stored results, and
        # each id is the converter's content-addressed uuid5, so a preview and
        # every later upload of the same evidence carry the same ids: repeating
        # an upload is idempotent rather than duplicative.
        "event_ids": [str(event.get("event_id") or "") for event in events],
        "provider_postures": plan.get("provider_postures", {}),
        "unavailable_measurements": ["cost", "model", "token"],
    }

    if plan["rejected_providers"]:
        status = "invalid_results"
    elif not events:
        status = "no_events"
    else:
        status = "uploaded" if yes else "dry_run"
    summary["status"] = status
    summary["would_upload"] = status == "uploaded"
    summary["next_action"], summary["next_detail"] = _campaign_upload_next_action(
        status=status,
        event_count=len(events),
        rejected=plan["rejected_providers"],
        skipped=plan["skipped_providers"],
        token_status=token_resolution.status,
    )
    if status in {"invalid_results", "no_events"}:
        return summary

    payload = cloud.build_event_upload_payload(
        events=events,
        repo_slug=plan["repo_slug"],
        team_id=resolved_team_id,
        install_id=resolved_install_id,
    )
    if not yes:
        summary["upload"] = cloud.build_dogfood_dry_run_preview(
            endpoint=resolved_endpoint,
            payload=payload,
        )
        return summary
    token = cloud.require_upload_token(
        endpoint=resolved_endpoint,
        resolution=token_resolution,
        local_endpoint=cloud.is_local_http_endpoint(resolved_endpoint),
    )
    try:
        summary["upload"] = cloud.post_upload_payload(
            payload=payload,
            endpoint=resolved_endpoint,
            token=token,
            timeout=timeout,
        )
    except cloud.CloudBundleError as exc:
        # A failed post's message can carry the endpoint's own response body --
        # arbitrary remote text. The campaign surface stays bounded, so the
        # failure is reported without it. Nothing was recorded either way, and
        # event ids are content-addressed, so re-running the upload republishes
        # the same events rather than duplicating them.
        raise cloud.CloudBundleError(
            "release campaign upload did not complete: the cloud endpoint "
            "rejected the upload or could not be reached; re-run "
            "`code-mower release campaign upload --yes` (event ids are "
            "idempotent) or check `code-mower cloud doctor`"
        ) from exc
    return summary


def render_campaign_text(campaign: Mapping[str, Any]) -> str:
    lines = [
        f"Release Campaign: {campaign.get('release_tag')} ({campaign.get('qualification_context')})",
        f"Status: {campaign.get('status')} ({'dry-run' if campaign.get('dry_run') else 'applied'})",
        f"Next: {campaign.get('next_action')}",
    ]
    if campaign.get("next_detail"):
        lines.append(f"Detail: {campaign.get('next_detail')}")
    lines.append("Providers:")
    for p in campaign.get("providers", []):
        state = p.get("state")
        env = p.get("environment")
        elapsed = p.get("elapsed_seconds") or 0.0
        action = p.get("next_action") or "none"
        detail = f" ({p.get('next_detail')})" if p.get("next_detail") else ""
        posture = _provider_posture(p)
        lines.append(f"- {p.get('provider')}: {state} ({posture}, {env}, elapsed {elapsed:.1f}s) -> {action}{detail}")
    return "\n".join(lines)


def _watch_retry_guidance(stop_reason: str, campaign: Mapping[str, Any]) -> str:
    rtag = str(campaign.get("release_tag") or "")
    cid = str(campaign.get("campaign_id") or "")
    id_flag = f"--campaign-id {cid}" if cid else f"--release-tag {rtag}"
    if stop_reason == "complete":
        return ""
    if stop_reason == "blocked":
        blocked_providers = [
            str(p.get("provider") or "")
            for p in campaign.get("providers", [])
            if isinstance(p, Mapping) and p.get("state") == "blocked"
        ]
        if blocked_providers:
            first_p = blocked_providers[0]
            return (
                f"inspect failures and retry with 'code-mower release campaign "
                f"{id_flag} --retry-provider {first_p} --apply'"
            )
        return (
            f"inspect failures and retry with 'code-mower release campaign "
            f"{id_flag} --retry-provider <provider> --apply'"
        )
    if stop_reason == "owner_action":
        return (
            "resolve provider prerequisites (credentials, issue, adapter) or record a "
            "manual result with --record-result"
        )
    if stop_reason in {"timeout", "interrupt"}:
        return f"re-run 'code-mower release campaign watch {id_flag}' to resume watching"
    if stop_reason == "remote_unavailable":
        return (
            f"verify GitHub connectivity and re-run 'code-mower release campaign watch {id_flag}'"
        )
    if stop_reason == "invalid_campaign":
        return "verify campaign identifier or create a new campaign with --release-tag <tag>"
    return ""


def _campaign_discrete_state(
    campaign: Mapping[str, Any],
) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    status = str(campaign.get("status") or "")
    providers = []
    for p in campaign.get("providers", []):
        if isinstance(p, Mapping):
            providers.append(
                (
                    str(p.get("provider") or ""),
                    str(p.get("state") or ""),
                    str(p.get("error") or ""),
                )
            )
    return (status, tuple(providers))


def _describe_transitions(
    old_c: Mapping[str, Any],
    new_c: Mapping[str, Any],
    elapsed: float,
) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    old_providers = {
        str(p.get("provider") or ""): p
        for p in old_c.get("providers", [])
        if isinstance(p, Mapping)
    }
    for new_p in new_c.get("providers", []):
        if not isinstance(new_p, Mapping):
            continue
        pname = str(new_p.get("provider") or "")
        old_p = old_providers.get(pname, {})
        old_pstate = str(old_p.get("state") or "")
        new_pstate = str(new_p.get("state") or "")
        old_error = str(old_p.get("error") or "")
        new_error = str(new_p.get("error") or "")
        if old_pstate != new_pstate or old_error != new_error:
            transitions.append(
                {
                    "provider": pname,
                    "from_state": old_pstate,
                    "to_state": new_pstate,
                    "from_error": old_error,
                    "to_error": new_error,
                    "elapsed_seconds": round(elapsed, 2),
                }
            )
    old_status = str(old_c.get("status") or "")
    new_status = str(new_c.get("status") or "")
    if old_status != new_status:
        transitions.append(
            {
                "campaign_status": True,
                "from_status": old_status,
                "to_status": new_status,
                "elapsed_seconds": round(elapsed, 2),
            }
        )
    return transitions


def _build_watch_summary(
    *,
    campaign_id: str,
    release_tag: str,
    package_identity: str,
    qualification_context: str,
    status: str,
    stop_reason: str,
    polls: int,
    elapsed_seconds: float,
    interval_seconds: float,
    timeout_seconds: float,
    next_action: str = "",
    next_detail: str = "",
    retry_guidance: str = "",
    transitions: Sequence[Mapping[str, Any]] = (),
    providers: Sequence[Mapping[str, Any]] = (),
    error: str = "",
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schema": CAMPAIGN_WATCH_SCHEMA,
        "mode": "release-campaign-watch",
        "campaign_id": campaign_id,
        "release_tag": release_tag,
        "package_identity": package_identity,
        "qualification_context": qualification_context,
        "status": status,
        "stop_reason": stop_reason,
        "polls": polls,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "interval_seconds": interval_seconds,
        "timeout_seconds": timeout_seconds,
        "next_action": next_action,
        "next_detail": next_detail,
        "retry_guidance": retry_guidance,
        "transitions": list(transitions),
        "providers": [
            {
                "provider": str(p.get("provider") or ""),
                "posture": _provider_posture(p),
                "state": str(p.get("state") or ""),
                "elapsed_seconds": round(float(p.get("elapsed_seconds") or 0.0), 2),
                "next_action": str(p.get("next_action") or ""),
                "next_detail": str(p.get("next_detail") or ""),
                "error": str(p.get("error") or ""),
            }
            for p in providers
            if isinstance(p, Mapping)
        ],
    }
    if error:
        summary["error"] = error
    return summary


def _watch_campaign_validation_error(campaign: Any) -> str:
    """Return a bounded reason when stored campaign data is unsafe to watch."""
    if not isinstance(campaign, Mapping) or campaign.get("schema") != CAMPAIGN_SCHEMA:
        return "invalid campaign payload"
    try:
        campaign_id = validate_campaign_id(campaign.get("campaign_id"))
        valid_tag, normalized_version, _ = _validate_tag_format(campaign.get("release_tag"))
        if not valid_tag:
            raise ValueError
        _package_identity, package_version = _parse_exact_package_spec(
            campaign.get("package_spec")
        )
        if package_version != normalized_version:
            raise ValueError
        context = campaign.get("qualification_context")
        starting_version = campaign.get("starting_version")
        _validate_qualification_context(context)
        _validate_starting_version(starting_version)
        if context == "upgrade" and not starting_version:
            raise ValueError
        if context != "upgrade" and starting_version:
            raise ValueError
        # Missing is a legacy campaign predating this field; it reads as the
        # documented default rather than failing validation.
        _validate_package_source(str(campaign.get("package_source") or DEFAULT_PACKAGE_SOURCE))
        if not campaign_id:
            raise ValueError
    except (TypeError, ValueError):
        return "invalid campaign identity"

    providers = campaign.get("providers")
    if not isinstance(providers, list):
        return "invalid campaign provider collection"
    for provider_data in providers:
        if not isinstance(provider_data, dict):
            return "invalid campaign provider collection"
        provider = provider_data.get("provider")
        if not isinstance(provider, str) or not provider:
            return "invalid campaign provider collection"
        try:
            resolve_provider_lane(provider)
        except ValueError:
            return "invalid campaign provider collection"
        raw_elapsed = provider_data.get("elapsed_seconds", 0.0)
        if raw_elapsed is None:
            raw_elapsed = 0.0
        if isinstance(raw_elapsed, bool) or not isinstance(raw_elapsed, int | float):
            return "invalid campaign provider metrics"
        elapsed = float(raw_elapsed)
        if not math.isfinite(elapsed) or elapsed < 0:
            return "invalid campaign provider metrics"
    return ""


def _watch_repo_slug(
    campaign: Mapping[str, Any], requested_repo_slug: str
) -> tuple[str, str]:
    """Resolve a poll-only repository override, or reject an identity conflict."""
    stored_repo_slug = str(campaign.get("repo_slug") or "")
    if requested_repo_slug and stored_repo_slug and requested_repo_slug != stored_repo_slug:
        return "", "requested repo slug does not match stored campaign"
    return stored_repo_slug or requested_repo_slug, ""


def campaign_watch(
    campaign: Mapping[str, Any] | None = None,
    *,
    campaign_id: str = "",
    release_tag: str = "",
    campaigns_dir: Path | None = None,
    repo_path: Path | None = None,
    repo_slug: str = "",
    issue: str | int = "",
    interval: float | None = None,
    timeout: float | None = None,
    emit_json: bool = False,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    which_fn: Callable[[str], str | None] = shutil.which,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    adapter_runner: AdapterRunner = run_local_adapter_command,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Poll a stored release campaign at a positive interval and bounded timeout.

    Stops distinctly for complete, genuine owner action or blocked state, timeout,
    interrupt, remote unavailable, and invalid campaign. Text mode prints the initial
    state, real state transitions, and final result only; JSON mode emits one stable
    metadata-only final summary.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    repo_path = repo_path or Path.cwd()
    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path)

    val_interval = DEFAULT_WATCH_INTERVAL_SECONDS if interval is None else interval
    val_timeout = DEFAULT_WATCH_TIMEOUT_SECONDS if timeout is None else timeout

    try:
        validated_interval = _validate_positive_duration(val_interval, "interval")
        validated_timeout = _validate_positive_duration(val_timeout, "timeout")
    except ValueError as exc:
        summary = _build_watch_summary(
            campaign_id=campaign_id or (str(campaign.get("campaign_id") or "") if campaign else ""),
            release_tag=release_tag or (str(campaign.get("release_tag") or "") if campaign else ""),
            package_identity="",
            qualification_context="",
            status="invalid",
            stop_reason="invalid_campaign",
            polls=0,
            elapsed_seconds=0.0,
            interval_seconds=0.0,
            timeout_seconds=0.0,
            next_action="pass positive interval and timeout",
            next_detail=str(exc),
            retry_guidance="specify positive numbers for --interval and --timeout",
            error=str(exc),
        )
        if not emit_json:
            print(f"error: {exc}", file=err)
        return summary

    target_campaign: Any = dict(campaign) if isinstance(campaign, Mapping) else campaign
    if target_campaign is None:
        if campaign_id:
            try:
                validate_campaign_id(campaign_id)
            except ValueError as exc:
                summary = _build_watch_summary(
                    campaign_id=campaign_id,
                    release_tag=release_tag,
                    package_identity="",
                    qualification_context="",
                    status="invalid",
                    stop_reason="invalid_campaign",
                    polls=0,
                    elapsed_seconds=0.0,
                    interval_seconds=validated_interval,
                    timeout_seconds=validated_timeout,
                    next_action="pass a valid campaign id",
                    next_detail=str(exc),
                    retry_guidance="use lowercase letters, digits, '.', '_', and '-'",
                    error=str(exc),
                )
                if not emit_json:
                    print(f"error: {exc}", file=err)
                return summary

        loaded, identifier, id_error = _load_requested_campaign(
            campaign_id=campaign_id,
            release_tag=release_tag,
            campaigns_dir=campaigns_dir,
        )
        if id_error:
            summary = _build_watch_summary(
                campaign_id=campaign_id or identifier,
                release_tag=release_tag,
                package_identity="",
                qualification_context="",
                status="invalid",
                stop_reason="invalid_campaign",
                polls=0,
                elapsed_seconds=0.0,
                interval_seconds=validated_interval,
                timeout_seconds=validated_timeout,
                next_action="resolve ambiguous release tag with --campaign-id",
                next_detail=id_error,
                retry_guidance="pass --campaign-id to uniquely select a campaign",
                error=id_error,
            )
            if not emit_json:
                print(f"error: {id_error}", file=err)
            return summary
        if loaded is None and identifier:
            msg = f"no campaign found for {identifier!r}"
            summary = _build_watch_summary(
                campaign_id=campaign_id or identifier,
                release_tag=release_tag,
                package_identity="",
                qualification_context="",
                status="invalid",
                stop_reason="invalid_campaign",
                polls=0,
                elapsed_seconds=0.0,
                interval_seconds=validated_interval,
                timeout_seconds=validated_timeout,
                next_action="create a campaign first",
                next_detail=msg,
                retry_guidance="verify campaign identifier or create a new campaign with --release-tag <tag>",
                error=msg,
            )
            if not emit_json:
                print(f"error: {msg}", file=err)
            return summary
        if loaded is None:
            all_c = list_campaigns(campaigns_dir)
            if not all_c:
                msg = "no campaigns found"
                summary = _build_watch_summary(
                    campaign_id=campaign_id,
                    release_tag=release_tag,
                    package_identity="",
                    qualification_context="",
                    status="invalid",
                    stop_reason="invalid_campaign",
                    polls=0,
                    elapsed_seconds=0.0,
                    interval_seconds=validated_interval,
                    timeout_seconds=validated_timeout,
                    next_action="create a campaign first",
                    next_detail=msg,
                    retry_guidance="create a campaign with code-mower release campaign --release-tag <tag>",
                    error=msg,
                )
                if not emit_json:
                    print(f"error: {msg}", file=err)
                return summary
            loaded = all_c[0]
        target_campaign = loaded

    validation_error = _watch_campaign_validation_error(target_campaign)
    if validation_error:
        msg = validation_error
        summary = _build_watch_summary(
            campaign_id=campaign_id,
            release_tag=release_tag,
            package_identity="",
            qualification_context="",
            status="invalid",
            stop_reason="invalid_campaign",
            polls=0,
            elapsed_seconds=0.0,
            interval_seconds=validated_interval,
            timeout_seconds=validated_timeout,
            next_action="check campaign file schema",
            next_detail=msg,
            retry_guidance="verify campaign storage file",
            error=msg,
        )
        if not emit_json:
            print(f"error: {msg}", file=err)
        return summary

    watch_repo_slug, repo_slug_error = _watch_repo_slug(
        target_campaign, repo_slug
    )
    if repo_slug_error:
        summary = _build_watch_summary(
            campaign_id=str(target_campaign.get("campaign_id") or campaign_id),
            release_tag=str(target_campaign.get("release_tag") or release_tag),
            package_identity="",
            qualification_context=str(
                target_campaign.get("qualification_context") or ""
            ),
            status="invalid",
            stop_reason="invalid_campaign",
            polls=0,
            elapsed_seconds=0.0,
            interval_seconds=validated_interval,
            timeout_seconds=validated_timeout,
            next_action="use the repository recorded by the campaign",
            next_detail=repo_slug_error,
            retry_guidance="omit --repo-slug or pass the stored repository",
            error=repo_slug_error,
        )
        if not emit_json:
            print(f"error: {repo_slug_error}", file=err)
        return summary

    cid = str(target_campaign.get("campaign_id") or "")
    rtag = str(target_campaign.get("release_tag") or "")
    pkg_spec = str(target_campaign.get("package_spec") or "")
    pkg_id = campaign_package_identity(pkg_spec)
    qcontext = str(target_campaign.get("qualification_context") or "cold_install")

    target_path = campaigns_dir / campaign_filename(cid)
    target_absent = not target_path.is_file()

    # Initial check / poll at t=0
    start_time = time_fn()
    deadline = start_time + validated_timeout

    def watch_gh_json(args: Sequence[str]) -> Any:
        """Bound production GitHub calls to this watch's remaining wall time."""
        if gh_json_runner is lane_status.run_gh_json:
            remaining = deadline - time_fn()
            if remaining <= 0:
                return {"comments": []}
            return lane_status.run_gh_json(args, timeout=remaining)
        return gh_json_runner(args)

    polls = 0
    all_transitions: list[dict[str, Any]] = []
    current_campaign: dict[str, Any] = dict(target_campaign)
    stop_reason: str | None = None
    watch_error = ""
    initial_transitions: list[dict[str, Any]] = []

    try:
        with locked_campaigns_dir(
            campaigns_dir,
            timeout_seconds=max(deadline - time_fn(), 0.0),
            sleep=sleep_fn,
            monotonic=time_fn,
        ):
            if target_absent and not target_path.is_file():
                save_campaign(target_campaign, campaigns_dir)
            reloaded = load_campaign_by_id(cid, campaigns_dir)
            if reloaded is None:
                msg = f"campaign {cid!r} could not be loaded from storage"
                summary = _build_watch_summary(
                    campaign_id=cid,
                    release_tag=rtag,
                    package_identity=pkg_id,
                    qualification_context=qcontext,
                    status="invalid",
                    stop_reason="invalid_campaign",
                    polls=0,
                    elapsed_seconds=time_fn() - start_time,
                    interval_seconds=validated_interval,
                    timeout_seconds=validated_timeout,
                    error=msg,
                )
                if not emit_json:
                    print(f"error: {msg}", file=err)
                return summary
            watch_repo_slug, _ = _watch_repo_slug(reloaded, repo_slug)
            initial_snapshot = copy.deepcopy(reloaded)
            current_campaign = dispatch_or_advance_campaign(
                reloaded,
                apply=False,
                issue_number=issue,
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                which_fn=which_fn,
                command_runner=command_runner,
                gh_json_runner=watch_gh_json,
                adapter_runner=adapter_runner,
                env=env,
                repo_slug_override=watch_repo_slug,
                poll_only=True,
            )
            initial_transitions = _describe_transitions(
                initial_snapshot,
                current_campaign,
                time_fn() - start_time,
            )
            all_transitions.extend(initial_transitions)

        if not emit_json:
            print(render_campaign_text(current_campaign), file=out)
            for transition in initial_transitions:
                if transition.get("campaign_status"):
                    print(
                        "Transition: campaign status "
                        f"{transition['from_status']} -> {transition['to_status']}",
                        file=out,
                    )
                else:
                    transition_error = (
                        f" (error: {transition['to_error']})"
                        if transition.get("to_error")
                        else ""
                    )
                    print(
                        f"Transition: {transition['provider']} "
                        f"{transition['from_state']} -> {transition['to_state']}"
                        f"{transition_error}",
                        file=out,
                    )

        # Check terminal/owner-action conditions on initial state
        outage_providers = [
            p
            for p in current_campaign.get("providers", [])
            if isinstance(p, Mapping)
            and p.get("state") == "running"
            and p.get("error") == "github_poll_unavailable"
        ]
        if outage_providers:
            stop_reason = "remote_unavailable"
        elif current_campaign.get("status") == "complete":
            stop_reason = "complete"
        elif current_campaign.get("status") == "blocked":
            stop_reason = "blocked"
        else:
            running_providers = [
                p
                for p in current_campaign.get("providers", [])
                if isinstance(p, Mapping) and p.get("state") == "running"
            ]
            if not running_providers:
                stop_reason = "owner_action"
            elif time_fn() - start_time >= validated_timeout:
                stop_reason = "timeout"

        if stop_reason is None:
            # Watch loop for running campaign
            current_discrete_state = _campaign_discrete_state(current_campaign)
            while True:
                now = time_fn()
                elapsed = now - start_time
                if elapsed >= validated_timeout:
                    stop_reason = "timeout"
                    break

                remaining = validated_timeout - elapsed
                sleep_time = min(validated_interval, remaining)
                if sleep_time <= 0:
                    stop_reason = "timeout"
                    break

                sleep_fn(sleep_time)

                polls += 1
                with locked_campaigns_dir(
                    campaigns_dir,
                    timeout_seconds=max(deadline - time_fn(), 0.0),
                    sleep=sleep_fn,
                    monotonic=time_fn,
                ):
                    reloaded = load_campaign_by_id(cid, campaigns_dir)
                    if reloaded is None:
                        stop_reason = "invalid_campaign"
                        break
                    watch_repo_slug, repo_slug_error = _watch_repo_slug(
                        reloaded, repo_slug
                    )
                    if repo_slug_error:
                        current_campaign["next_action"] = (
                            "use the repository recorded by the campaign"
                        )
                        current_campaign["next_detail"] = repo_slug_error
                        stop_reason = "invalid_campaign"
                        break
                    updated = dispatch_or_advance_campaign(
                        reloaded,
                        apply=False,
                        issue_number=issue,
                        repo_path=repo_path,
                        campaigns_dir=campaigns_dir,
                        which_fn=which_fn,
                        command_runner=command_runner,
                        gh_json_runner=watch_gh_json,
                        adapter_runner=adapter_runner,
                        env=env,
                        repo_slug_override=watch_repo_slug,
                        poll_only=True,
                    )

                now_after_poll = time_fn()
                elapsed_after_poll = now_after_poll - start_time
                new_discrete_state = _campaign_discrete_state(updated)
                if new_discrete_state != current_discrete_state:
                    step_transitions = _describe_transitions(
                        current_campaign, updated, elapsed_after_poll
                    )
                    all_transitions.extend(step_transitions)
                    if not emit_json:
                        for t in step_transitions:
                            if t.get("campaign_status"):
                                print(
                                    f"Transition: campaign status {t['from_status']} -> {t['to_status']}",
                                    file=out,
                                )
                            else:
                                err_s = f" (error: {t['to_error']})" if t.get("to_error") else ""
                                print(
                                    f"Transition: {t['provider']} {t['from_state']} -> {t['to_state']}{err_s}",
                                    file=out,
                                )
                    current_campaign = updated
                    current_discrete_state = new_discrete_state
                else:
                    # No-change suppression
                    current_campaign = updated

                # Check outage / remote unavailable
                outage_providers = [
                    p
                    for p in updated.get("providers", [])
                    if isinstance(p, Mapping)
                    and p.get("state") == "running"
                    and p.get("error") == "github_poll_unavailable"
                ]
                if outage_providers:
                    stop_reason = "remote_unavailable"
                    break

                # Check complete
                if updated.get("status") == "complete":
                    stop_reason = "complete"
                    break

                # Check blocked
                if updated.get("status") == "blocked":
                    stop_reason = "blocked"
                    break

                # Check owner action (all running finished but campaign not complete)
                running_left = [
                    p
                    for p in updated.get("providers", [])
                    if isinstance(p, Mapping) and p.get("state") == "running"
                ]
                if not running_left:
                    stop_reason = "owner_action"
                    break

                if time_fn() - start_time >= validated_timeout:
                    stop_reason = "timeout"
                    break

    except KeyboardInterrupt:
        stop_reason = "interrupt"
    except FileLockError:
        stop_reason = "timeout"
    except OSError:
        stop_reason = "invalid_campaign"
        watch_error = "campaign storage is unavailable"
        current_campaign["next_action"] = "check campaign storage access"
        current_campaign["next_detail"] = watch_error

    elapsed_final = time_fn() - start_time
    stop_reason = stop_reason or "timeout"
    retry_guidance = _watch_retry_guidance(stop_reason, current_campaign)
    if not emit_json:
        detail = current_campaign.get("next_detail") or current_campaign.get("next_action") or ""
        detail_str = f" ({detail})" if detail else ""
        print(f"Final result: {stop_reason}{detail_str}", file=out)
        if retry_guidance:
            print(f"Retry guidance: {retry_guidance}", file=out)

    return _build_watch_summary(
        campaign_id=cid,
        release_tag=rtag,
        package_identity=pkg_id,
        qualification_context=qcontext,
        status=str(current_campaign.get("status") or ""),
        stop_reason=stop_reason,
        polls=polls,
        elapsed_seconds=elapsed_final,
        interval_seconds=validated_interval,
        timeout_seconds=validated_timeout,
        next_action=str(current_campaign.get("next_action") or ""),
        next_detail=str(current_campaign.get("next_detail") or ""),
        retry_guidance=retry_guidance,
        transitions=all_transitions,
        providers=current_campaign.get("providers", []),
        error=watch_error,
    )


def _board_text(source: Mapping[str, Any], key: str, default: str) -> str:
    """Project a persisted string field for the Board, dropping malformed values.

    A value that is not already a string -- a null, a number, or a nested
    object left by an older or hand-edited campaign file -- falls back to the
    field's default instead of being rendered with ``str()``, so the Board
    stays metadata-only and never splices a raw persisted structure into
    /api/status.
    """
    value = source.get(key, default)
    return value if isinstance(value, str) else default


def _board_elapsed_seconds(value: Any) -> float:
    """Project a persisted elapsed_seconds field as a finite, nonnegative float.

    A missing, null, nonnumeric, NaN, or infinite value degrades to 0.0. One
    malformed or older campaign file must never raise out of this projection
    and take Board /api/status -- and every healthy campaign with it -- down.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0.0:
        return 0.0
    return number


def _board_dry_run(value: Any) -> bool:
    """Project a persisted dry_run flag, reading anything malformed as dry run."""
    return value if isinstance(value, bool) else True


def campaign_has_been_applied(campaign: Mapping[str, Any]) -> bool:
    """Whether this campaign has ever run under ``--apply``.

    Applied is a *monotonic* transition: a dry-run campaign becomes applied the
    first time it is dispatched with ``--apply``, and nothing moves it back.
    A later ``resume`` or status poll that simply omits ``--apply`` is not a
    statement that the dispatches, paid runs, and attempts already made never
    happened, so it must not rewrite the campaign's identity back to a dry-run
    preview -- which would relabel real evidence as a preview in stored state,
    in `render_campaign_text`, and on the Board, and would make the aggregate
    status advise "run with --apply to dispatch providers" for providers that
    have already been dispatched.

    Two independent records answer this, and either is enough: the campaign's
    own ``dry_run`` flag (read strictly -- only an explicit ``False`` counts as
    applied, so a malformed value degrades to dry run exactly as
    :func:`_board_dry_run` does), and any provider whose ``dispatch_mode`` was
    stamped ``applied``. The second covers a campaign whose top-level flag was
    lost or corrupted while its per-provider attempt records survived.
    """
    if campaign.get("dry_run") is False:
        return True
    providers = campaign.get("providers")
    if not isinstance(providers, list):
        return False
    return any(
        isinstance(p, Mapping) and p.get("dispatch_mode") == "applied"
        for p in providers
    )


def _is_local_cli_provider(p: Mapping[str, Any]) -> bool:
    """Whether a campaign provider participant is configured for local CLI execution."""
    driver = p.get("driver")
    if isinstance(driver, str) and driver:
        return driver == "local_cli"
    provider = p.get("provider")
    if isinstance(provider, str) and provider:
        try:
            _, lane = resolve_provider_lane(provider)
            return lane.driver == "local_cli"
        except ValueError:
            return False
    return False


def _parse_board_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a timezone-aware UTC datetime.

    Degrades safely to None on any malformed, out-of-range, or non-string input.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def _resolve_board_now(now: datetime | str | float | None) -> datetime:
    """Resolve current time for Board projection to a timezone-aware UTC datetime."""
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)
    if isinstance(now, (int, float)):
        if not math.isfinite(now):
            return datetime.now(UTC)
        try:
            return datetime.fromtimestamp(now, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return datetime.now(UTC)
    if isinstance(now, str):
        parsed = _parse_board_timestamp(now)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def _effective_adapter_timeout(
    provider: str,
    *,
    repo_path: Path | str = ".",
) -> int:
    """Resolve the effective campaign adapter timeout for a provider in seconds.

    Checks the repository override in code-mower.yml when valid. Malformed or
    missing configs degrade safely to the lane's default timeout or
    DEFAULT_ADAPTER_TIMEOUT_SECONDS.
    """
    try:
        _, lane = resolve_provider_lane(provider)
    except ValueError:
        return DEFAULT_ADAPTER_TIMEOUT_SECONDS

    default_timeout = DEFAULT_ADAPTER_TIMEOUT_SECONDS
    try:
        lane_cfg_timeout = lane.provider_config.get("campaign_adapter_timeout_seconds")
        if lane_cfg_timeout is not None:
            default_timeout = _validate_adapter_timeout(lane_cfg_timeout)
    except ValueError:
        default_timeout = DEFAULT_ADAPTER_TIMEOUT_SECONDS

    try:
        path = Path(repo_path)
        overrides, error_code, _ = _load_campaign_adapter_overrides(lane, path)
        if not error_code and "campaign_adapter_timeout_seconds" in overrides:
            override_val = overrides["campaign_adapter_timeout_seconds"]
            return _validate_adapter_timeout(override_val)
    except (ValueError, OSError, TypeError):
        pass

    return default_timeout


def _is_checkpointed_local_cli_candidate(
    p: Mapping[str, Any],
) -> bool:
    """Whether provider is a candidate for checkpointed in-flight / stale projection."""
    persisted_state = _board_text(p, "state", "queued")
    if persisted_state not in {"queued", "running"}:
        return False

    attempted_at = p.get("attempted_at")
    if not isinstance(attempted_at, str) or not attempted_at.strip():
        return False

    if not _is_local_cli_provider(p):
        return False

    if p.get("completed_at") or p.get("adoption_result"):
        return False

    return True


def _board_provider_projection(
    p: Mapping[str, Any],
    *,
    campaign_id: str = "",
    campaigns_dir: Path | None = None,
    repo_path: Path | str = ".",
    now: datetime | str | float | None = None,
) -> tuple[str, str, str]:
    """Project a provider participant's state, next_action, and next_detail for Board.

    A local_cli provider checkpointed with attempted_at while still persisted
    queued and without completion/result evidence renders as running only while
    plausibly live within its effective campaign adapter timeout (including
    valid repository overrides in code-mower.yml).

    Once that bounded window is exceeded, or when timestamps are malformed, it
    degrades safely to non-running queued state with concise retry guidance
    without mutating persisted campaign state on disk.
    """
    persisted_state = _board_text(p, "state", "queued")
    next_action = _board_text(p, "next_action", "")
    next_detail = _board_text(p, "next_detail", "")

    if persisted_state not in {"queued", "running"}:
        return persisted_state, next_action, next_detail

    if not _is_checkpointed_local_cli_candidate(p):
        return persisted_state, next_action, next_detail

    provider = _board_text(p, "provider", "")
    attempted_dt = _parse_board_timestamp(p.get("attempted_at"))
    timeout_seconds = _effective_adapter_timeout(provider, repo_path=repo_path)
    now_dt = _resolve_board_now(now)

    if (
        attempted_dt is not None
        and -5.0 <= (now_dt - attempted_dt).total_seconds() <= timeout_seconds
    ):
        return (
            "running",
            f"wait for {provider} local adapter",
            f"local adapter attempt is within its {timeout_seconds}s timeout window",
        )

    retry_action = (
        next_action
        if "--retry-provider" in next_action
        else f"use --retry-provider {provider} to retry {provider}"
    )
    if attempted_dt is None:
        retry_detail = "local adapter attempt timestamp is malformed"
    elif (now_dt - attempted_dt).total_seconds() < -5.0:
        retry_detail = "local adapter attempt timestamp is in the future"
    else:
        retry_detail = f"local adapter attempt exceeded {timeout_seconds}s timeout"

    return "queued", retry_action, retry_detail


def _board_provider_state(
    p: Mapping[str, Any],
    *,
    campaign_id: str = "",
    campaigns_dir: Path | None = None,
    repo_path: Path | str = ".",
    now: datetime | str | float | None = None,
) -> str:
    """Project a provider participant's state for the Board."""
    state, _, _ = _board_provider_projection(
        p,
        campaign_id=campaign_id,
        campaigns_dir=campaigns_dir,
        repo_path=repo_path,
        now=now,
    )
    return state


def release_campaigns_board_payload(
    repo_path: Path | str = ".",
    *,
    campaigns_dir: Path | None = None,
    repo_slug: str = "",
    now: datetime | str | float | None = None,
) -> dict[str, Any]:
    empty = {
        "schema": BOARD_RELEASE_CAMPAIGNS_SCHEMA,
        "available": True,
        "campaigns": [],
        "card_count": 0,
        "next_action": "no active campaigns",
        "next_detail": "run code-mower release campaign --release-tag <tag> to start one",
    }
    if campaigns_dir is not None:
        dir_path = Path(campaigns_dir)
        campaigns = list_campaigns(dir_path)
        collisions: tuple[str, ...] = ()
    else:
        local_dir = default_campaigns_dir(repo_path)
        identity = resolve_repo_identity(repo_path, repo_slug)
        lookup_dirs = discover_campaign_directories(identity, local_dir=local_dir)
        campaigns, collisions = list_discovered_campaigns(lookup_dirs)
        dir_path = lookup_dirs[0] if lookup_dirs else local_dir
    if collisions:
        named = ", ".join(collisions[:AMBIGUOUS_RELEASE_TAG_ID_LIMIT])
        empty["next_action"] = "resolve ambiguous campaigns with --campaigns-dir"
        empty["next_detail"] = (
            f"campaign id {collisions[0]!r} is stored in more than one campaign "
            "directory; pass --campaigns-dir to select one"
            if len(collisions) == 1
            else (
                f"campaign ids ({named}) are stored in more than one campaign "
                "directory; pass --campaigns-dir to select one"
            )
        )
        return empty
    if not campaigns:
        return empty

    projected_campaigns: list[dict[str, Any]] = []
    total_cards = 0

    # Every field below is projected defensively: persisted campaign files are
    # read-only here, may predate the current schema, and one malformed value
    # must degrade that single field (or skip that single provider) rather
    # than raise out of the Board payload.
    for c in campaigns:
        campaign_id = _board_text(c, "campaign_id", "")
        cards: list[dict[str, Any]] = []
        raw_providers = c.get("providers")
        for p in raw_providers if isinstance(raw_providers, list) else []:
            if not isinstance(p, Mapping):
                continue
            provider = _board_text(p, "provider", "")
            state, next_action, next_detail = _board_provider_projection(
                p,
                campaign_id=campaign_id,
                campaigns_dir=dir_path,
                repo_path=repo_path,
                now=now,
            )
            cards.append(
                {
                    "release": _board_text(c, "release_tag", ""),
                    "provider": provider,
                    "posture": _provider_posture(p),
                    "lane_id": _board_text(p, "lane_id", provider),
                    "environment": _board_text(p, "environment", "local"),
                    "state": state,
                    "elapsed_seconds": _board_elapsed_seconds(p.get("elapsed_seconds")),
                    "response_deadline_at": _board_text(
                        p, "response_deadline_at", ""
                    ),
                    "transport_verified": (
                        p.get("transport_verified")
                        if isinstance(p.get("transport_verified"), bool)
                        else None
                    ),
                    "next_action": next_action,
                    "next_detail": next_detail,
                }
            )
        dry_run = _board_dry_run(c.get("dry_run", True))
        status, next_action, next_detail = _aggregate_campaign_status(
            cards,
            dry_run=dry_run,
        )
        retry_cards = [
            card
            for card in cards
            if card["state"] == "queued"
            and "--retry-provider" in card["next_action"]
        ]
        if status == "queued" and retry_cards:
            if len(retry_cards) == 1:
                next_action = retry_cards[0]["next_action"]
                next_detail = retry_cards[0]["next_detail"]
            else:
                providers = ", ".join(card["provider"] for card in retry_cards)
                next_action = f"retry stale local adapters: {providers}"
                next_detail = f"{len(retry_cards)} local adapter attempt(s) exceeded their timeout"
        total_cards += len(cards)
        projected_campaigns.append(
            {
                "campaign_id": _board_text(c, "campaign_id", ""),
                "release_tag": _board_text(c, "release_tag", ""),
                "package_spec": _board_text(c, "package_spec", ""),
                "qualification_context": _board_text(c, "qualification_context", ""),
                "status": status,
                "dry_run": dry_run,
                "elapsed_seconds": _board_elapsed_seconds(c.get("elapsed_seconds")),
                "next_action": next_action,
                "next_detail": next_detail,
                "cards": cards,
            }
        )

    overall_action = (
        projected_campaigns[0]["next_action"]
        if projected_campaigns
        else "no active campaigns"
    )
    overall_detail = (
        projected_campaigns[0]["next_detail"]
        if projected_campaigns
        else ""
    )

    return {
        "schema": BOARD_RELEASE_CAMPAIGNS_SCHEMA,
        "available": True,
        "campaigns": projected_campaigns,
        "card_count": total_cards,
        "next_action": overall_action,
        "next_detail": overall_detail,
    }


def _validate_retry_provider(
    retry_provider: str,
    campaign: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve and validate --retry-provider against campaign membership.

    Returns (canonical_provider, error_message); error_message is empty on
    success and canonical_provider is empty when retry_provider was not given.
    """
    if not retry_provider:
        return "", ""
    try:
        canonical, _ = resolve_provider_lane(retry_provider)
    except ValueError as exc:
        return "", str(exc)
    if not any(p.get("provider") == canonical for p in campaign.get("providers", [])):
        return "", f"--retry-provider {retry_provider!r} is not part of this campaign"
    return canonical, ""


CAMPAIGN_ACTIONS = ("create", "status", "resume", "dispatch", "upload", "watch")

# The boolean flags that are older spellings of an action, and the action each
# one asks for. Both remain supported: `--status` is the original spelling of
# `status`, and `--resume` of `resume`.
_LEGACY_ACTION_FLAGS: tuple[tuple[str, str], ...] = (
    ("--status", "status"),
    ("--resume", "resume"),
)

# The authoritative action/legacy-flag matrix. Each row lists the legacy flags
# an explicit action may be spelled with; every pairing absent from a row states
# two different intents in one invocation and is refused.
#
#              | --status | --resume
#   create     |    no    |    no     -- create starts a campaign, the flags
#   status     |   yes    |    no        advance or read an existing one
#   resume     |    no    |   yes
#   dispatch   |    no    |   yes     -- `dispatch` and `--resume` are two
#                                        spellings of "advance the existing
#                                        campaign" and route identically
#   upload     |    no    |    no     -- upload publishes evidence the campaign
#                                        already has; it never advances one
#   watch      |    no    |    no     -- watch monitors an existing campaign
#
# An omitted action (``None``) is not a row: it states no action of its own, so
# either flag simply supplies one, and the two together are caught as a status
# request carrying the mutating `--resume` intent.
_COMPATIBLE_LEGACY_FLAGS: Mapping[str, frozenset[str]] = {
    "create": frozenset(),
    "status": frozenset({"--status"}),
    "resume": frozenset({"--resume"}),
    "dispatch": frozenset({"--resume"}),
    "upload": frozenset(),
    "watch": frozenset(),
}


def _action_flag_conflict(*, action: str | None, status: bool, resume: bool) -> str:
    """Report a bounded conflict between an explicit action and a legacy flag.

    Consults :data:`_COMPATIBLE_LEGACY_FLAGS`, which is the single place the
    action/flag matrix is written down. Before it existed, each combination was
    resolved by whichever branch of the command body happened to test its
    boolean first, and `create --resume` -- two contradictory actions in one
    invocation -- reached the body with both ``is_create`` and ``is_resume``
    true and was answered by the resume branch, so an explicit `create` request
    failed with "no existing campaign to resume" (and, when a campaign did
    exist, with "already exists"). Neither answers the request that was made.
    The combination is refused here instead, before any lookup, directory
    creation, lock, write, dispatch, poll, or adapter call.
    """
    allowed = _COMPATIBLE_LEGACY_FLAGS.get(action or "")
    if allowed is None:
        # No action, or one this table does not describe: nothing to contradict.
        return ""
    supplied = {"--status": status, "--resume": resume}
    offending = [
        (flag, flag_action)
        for flag, flag_action in _LEGACY_ACTION_FLAGS
        if supplied[flag] and flag not in allowed
    ]
    if not offending:
        return ""
    named = ", ".join(f"{flag} (the {flag_action!r} action)" for flag, flag_action in offending)
    return (
        f"the {action!r} action cannot be combined with {named}; "
        "one invocation states one campaign action, so re-run with exactly one of them"
    )


def _status_mutation_conflict(
    *,
    action: str | None,
    record_result: Path | None,
    retry_provider: str,
    apply: bool,
    resume: bool,
) -> str:
    """Report a bounded conflict between a status request and a mutating intent.

    ``status`` is a read-only spelling: it reads campaign files and prints them,
    takes no lock, and writes nothing. A mutating flag carried alongside it
    therefore has no honest reading. Executing it would mean mutating under a
    read-only spelling -- and lock-free, since the read-only route deliberately
    skips the campaign directory lock, so a `--status --retry-provider` run
    would dispatch outside the serialization contract. Ignoring it (what
    ``--status --retry-provider`` used to do: take the lock, then fall into the
    status branch and print) silently drops work the caller asked for and
    reports success. Neither is acceptable, so the combination is refused here,
    before any lock, mutation, poll, or dispatch.
    """
    intents: list[str] = []
    if retry_provider:
        intents.append("--retry-provider")
    if record_result is not None:
        intents.append("--record-result")
    if apply:
        intents.append("--apply")
    if resume:
        intents.append("--resume")
    if action is not None and action != "status":
        # `action` is a fixed choice on the command line, but this is a library
        # entry point too: an unrecognized value is described, never echoed.
        intents.append(
            f"the {action!r} action" if action in CAMPAIGN_ACTIONS else "a non-status action"
        )
    if not intents:
        return ""
    return (
        f"status is read-only and cannot be combined with {', '.join(intents)}; "
        "re-run the mutating request without --status/the status action"
    )


def _upload_intent_conflict(
    *,
    action: str | None,
    record_result: Path | None,
    retry_provider: str,
    apply: bool,
    yes: bool,
) -> str:
    """Report a bounded conflict between an upload request and another intent.

    ``upload`` publishes evidence an existing campaign already holds. It never
    dispatches, retries, records, or advances anything, so a flag that asks for
    one of those states a second intent with no honest reading -- executing it
    would perform paid or mutating work under a read-only spelling, and ignoring
    it would drop work the caller asked for while reporting success.

    ``--yes`` is the mirror image: it authorizes the upload's network post and
    means nothing to any other action, so carrying it elsewhere is refused
    rather than silently ignored (a caller who spelled ``resume --yes`` expecting
    an upload would otherwise be told the campaign advanced and never learn that
    nothing was published).
    """
    if action == "upload":
        intents: list[str] = []
        if apply:
            intents.append("--apply")
        if record_result is not None:
            intents.append("--record-result")
        if retry_provider:
            intents.append("--retry-provider")
        if not intents:
            return ""
        return (
            "upload publishes an existing campaign's terminal evidence and cannot be "
            f"combined with {', '.join(intents)}; run that campaign action first, then "
            "upload"
        )
    if yes:
        return (
            "--yes authorizes the upload network post and applies only to the 'upload' "
            "action; re-run as `campaign upload --yes`"
        )
    return ""


def _watch_intent_conflict(
    *,
    action: str | None,
    record_result: Path | None,
    retry_provider: str,
    apply: bool,
    yes: bool,
    interval: float | None = None,
    timeout: float | None = None,
) -> str:
    if action == "watch":
        intents: list[str] = []
        if apply:
            intents.append("--apply")
        if record_result is not None:
            intents.append("--record-result")
        if retry_provider:
            intents.append("--retry-provider")
        if yes:
            intents.append("--yes")
        if not intents:
            return ""
        return (
            "watch polls campaign progress without executing or mutating providers and cannot be "
            f"combined with {', '.join(intents)}; run that campaign action first, then watch"
        )
    if interval is not None and action != "watch":
        return "--interval applies only to the 'watch' action; re-run as `campaign watch`"
    if timeout is not None and action not in {"watch", "upload"}:
        return (
            "--timeout applies only to the 'watch' and 'upload' actions; re-run as "
            "`campaign watch` or `campaign upload`"
        )
    return ""


def _required_providers_intent_conflict(
    *,
    action: str | None,
    status: bool,
    required_providers: Any = None,
) -> str:
    """Report an option-scope conflict when --required-providers is passed to read-only actions."""
    if required_providers is not None and (status or action in {"status", "watch", "upload"}):
        return (
            "--required-providers applies only to campaign creation, resume, and dispatch; "
            "re-run as `campaign create`, `campaign resume`, or `campaign dispatch`"
        )
    return ""


def _command_intent_conflict(
    *,
    action: str | None,
    record_result: Path | None,
    retry_provider: str,
    apply: bool,
    resume: bool,
    status: bool,
    yes: bool = False,
    interval: float | None = None,
    timeout: float | None = None,
    required_providers: Any = None,
) -> str:
    """Report the one bounded reason this invocation states conflicting intents.

    The single validation gate for command intent: every contradictory
    action/flag combination is decided here, from the tables above, before the
    command touches the campaign directory at all. A request refused here has
    made no lookup, created no directory or lock file, written no state, run no
    adapter, and posted or polled nothing.

    Status is checked first so that a status request carrying *any* mutating
    intent -- including a conflicting action -- is reported as the read-only
    violation it is; then the upload/``--yes`` pairing, which is read-only over
    campaign state in the same way; what remains is the action/legacy-flag
    matrix.
    """
    if status or action == "status":
        conflict = _status_mutation_conflict(
            action=action,
            record_result=record_result,
            retry_provider=retry_provider,
            apply=apply,
            resume=resume,
        )
        if conflict:
            return conflict
    conflict = _upload_intent_conflict(
        action=action,
        record_result=record_result,
        retry_provider=retry_provider,
        apply=apply,
        yes=yes,
    )
    if conflict:
        return conflict
    conflict = _watch_intent_conflict(
        action=action,
        record_result=record_result,
        retry_provider=retry_provider,
        apply=apply,
        yes=yes,
        interval=interval,
        timeout=timeout,
    )
    if conflict:
        return conflict
    conflict = _required_providers_intent_conflict(
        action=action,
        status=status,
        required_providers=required_providers,
    )
    if conflict:
        return conflict
    return _action_flag_conflict(action=action, status=status, resume=resume)


def _load_requested_campaign(
    *,
    campaign_id: str,
    release_tag: str,
    campaigns_dir: Path,
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve the campaign an explicit identifier refers to.

    Returns ``(campaign, identifier, error)``. ``identifier`` is empty only for
    an unqualified request (neither ``--campaign-id`` nor ``--release-tag``) --
    that is the one case allowed to fall back to the newest campaign. When
    ``--campaign-id`` resolves to a campaign for a different release tag than
    the one the caller also named, the request is rejected rather than answered
    with the unrelated campaign's data.

    The two identifiers are resolved by two different *exact* lookups, on
    purpose, and neither can answer with the other's match. An id addresses
    exactly one file, so ``--campaign-id`` uses :func:`load_campaign_by_id`,
    which reads only ``<campaign-id>.json`` and requires its stored
    ``campaign_id`` to match -- an explicitly named id is never answered with a
    campaign that merely carries that text as its *release tag* (and, when a tag
    was named too, both fields must still agree). A tag is not a storage key, so
    a tag-only request uses :func:`load_campaign_by_release_tag`, which matches
    the stored ``release_tag`` field and nothing else: routing it through the
    id lookup would let a tag that is *also* a well-formed campaign id
    (``v1.0.0``) resolve to a custom-id campaign belonging to another release. A
    tag shared by several campaigns is reported as ambiguous rather than
    resolved arbitrarily.
    """
    identifier = campaign_id or release_tag
    if not identifier:
        return None, "", ""
    if campaign_id:
        found = load_campaign_by_id(campaign_id, campaigns_dir)
        if found is None:
            return None, identifier, ""
        if release_tag and str(found.get("release_tag") or "") != release_tag:
            return (
                None,
                identifier,
                f"campaign {campaign_id!r} does not match --release-tag {release_tag!r}",
            )
        return found, identifier, ""
    found, ambiguity = load_campaign_by_release_tag(release_tag, campaigns_dir)
    if ambiguity:
        return None, identifier, ambiguity
    return found, identifier, ""


def _existing_campaign_conflict(
    campaign: Mapping[str, Any],
    *,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
    package_source: str = "",
    providers: Sequence[str],
    required_providers: Sequence[str] | None = None,
    repo_slug: str = "",
) -> str:
    """Report a bounded conflict between an existing campaign and creation arguments.

    An existing campaign is never replaced by a fresh one, so creation-time
    arguments that describe a *different* campaign cannot be honored. They are
    rejected explicitly instead of being silently ignored while the stored
    campaign advances under different terms. Only supplied values are compared:
    an unsupplied ``--qualification-context`` arrives here as an empty string
    (the "unspecified" sentinel) and asserts nothing about the stored campaign,
    while *every* explicitly supplied context -- including ``cold_install`` --
    is compared against the stored one, so an explicit cold-install request can
    never silently advance a stored upgrade campaign. A stored campaign that
    carries no context at all is compared as ``cold_install``, which is the
    context its own dispatch and evidence checks already use.

    ``repo_slug`` is the one field an existing campaign may still be *completed*
    with: a campaign created without a repository has nowhere to dispatch, and
    supplying the slug later fills the empty stored value (see
    ``campaign_command``). Overwriting a slug the campaign already carries is a
    different matter -- it would repoint an in-flight campaign's dispatch and
    polling at another repository, so a mismatch against a non-empty stored slug
    is rejected here instead.
    """
    stored_slug = str(campaign.get("repo_slug") or "")
    if repo_slug and stored_slug and repo_slug != stored_slug:
        return (
            f"--repo-slug {repo_slug!r} does not match existing campaign repo slug "
            f"{stored_slug!r}; an existing campaign's repository is fixed once set"
        )
    stored_context = str(campaign.get("qualification_context") or "cold_install")
    if qualification_context and qualification_context != stored_context:
        return (
            f"--qualification-context {qualification_context!r} does not match existing "
            f"campaign context {stored_context!r}"
        )
    stored_source = str(campaign.get("package_source") or DEFAULT_PACKAGE_SOURCE)
    if package_source and package_source != stored_source:
        return (
            f"--package-source {package_source!r} does not match existing campaign "
            f"source {stored_source!r}; an existing campaign's package source is "
            "fixed once set"
        )
    if starting_version and starting_version != str(campaign.get("starting_version") or ""):
        return (
            f"--starting-version {starting_version!r} does not match existing campaign "
            f"starting version {str(campaign.get('starting_version') or '')!r}"
        )
    if package_spec and package_spec != str(campaign.get("package_spec") or ""):
        return (
            f"--package-spec {package_spec!r} does not match existing campaign spec "
            f"{str(campaign.get('package_spec') or '')!r}"
        )
    if providers:
        requested: set[str] = set()
        for name in providers:
            try:
                canonical, _ = resolve_provider_lane(name)
            except ValueError as exc:
                return str(exc)
            requested.add(canonical)
        stored = {str(p.get("provider") or "") for p in campaign.get("providers", [])}
        if requested != stored:
            return (
                "--providers does not match the existing campaign's providers "
                f"({', '.join(sorted(stored))}); an existing campaign's provider set "
                "is fixed at creation"
            )
    if required_providers is not None:
        if not required_providers:
            return "--required-providers cannot be empty; specify a non-empty subset of selected providers"
        stored_providers = campaign.get("providers", [])
        stored_names = {
            str(p.get("provider") or "")
            for p in stored_providers
            if isinstance(p, Mapping)
        }
        req_requested: set[str] = set()
        seen_required: set[str] = set()
        for name in required_providers:
            try:
                canonical, _ = resolve_provider_lane(name)
            except ValueError as exc:
                return str(exc)
            if canonical in seen_required:
                return (
                    f"duplicate required provider {canonical!r}: it was named more than once; "
                    "list each required provider exactly once"
                )
            seen_required.add(canonical)
            if canonical not in stored_names:
                return (
                    f"required provider {canonical!r} is not in the existing campaign's providers "
                    f"({', '.join(sorted(stored_names))})"
                )
            req_requested.add(canonical)
        stored_required = {
            str(p.get("provider") or "")
            for p in stored_providers
            if isinstance(p, Mapping) and _provider_posture(p) == "required"
        }
        if req_requested != stored_required:
            return (
                f"--required-providers does not match existing campaign provider posture "
                f"({', '.join(sorted(stored_required))}); an existing campaign's provider posture "
                "is fixed once set"
            )
    return ""


def campaign_command(
    *,
    action: str | None = None,
    release_tag: str = "",
    package_spec: str = "",
    providers: Sequence[str] = (),
    required_providers: Sequence[str] | str | None = None,
    qualification_context: str = "",
    starting_version: str = "",
    package_source: str = "",
    repo_path: Path | None = None,
    repo_slug: str = "",
    issue: str | int = "",
    apply: bool = False,
    resume: bool = False,
    status: bool = False,
    campaign_id: str = "",
    campaigns_dir: Path | None = None,
    record_result: Path | None = None,
    record_provider: str = "",
    retry_provider: str = "",
    yes: bool = False,
    endpoint: str = "",
    token_env: str = "",
    token_file: Path | None = None,
    token_dir: Path | None = None,
    install_id: str = "",
    team_id: str = "",
    timeout: float | None = None,
    interval: float | None = None,
    emit_json: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    which_fn: Callable[[str], str | None] = shutil.which,
    adapter_runner: AdapterRunner = run_local_adapter_command,
    env: Mapping[str, str] | None = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Create, inspect, advance, or publish a release qualification campaign.

    Every *potentially mutating* invocation runs under an exclusive advisory
    lock on the campaign directory (see :func:`locked_campaigns_dir`), held
    across the *whole* sequence: loading stored state, deciding what to do,
    claiming an attempt by stamping ``attempted_at``, invoking a local adapter
    or posting a hosted dispatch, and persisting the result. Without that
    bracket, two commands started at the same time could both load a campaign
    before either had persisted its attempt claim, and both would run the
    provider's adapter or post its paid dispatch. Because the lock is taken
    *before* the first read, the second command necessarily reloads after the
    first has finished and observes the ``attempted_at`` it wrote, so it
    declines the repeat exactly as an ordinary sequential resume would. That
    covers create, implicit create/advance, resume, dispatch, ``--record-result``,
    ``--retry-provider``, and the repository-slug fill.

    A status invocation -- ``status=True`` or ``action="status"`` --, an
    ``upload`` invocation, and a ``watch`` invocation take no lock at top level.
    ``upload`` reads one campaign snapshot, converts the
    evidence it already holds into cloud events, and posts them; it writes no
    campaign file, so locking it would only make a long network post block
    dispatch and polling for the whole campaign directory. ``watch`` acquires
    the campaign directory lock inside each individual poll iteration rather
    than holding it across the entire watch timeout, preserving directory lock
    availability for other readers and commands. Combining status with
    a mutating
    intent (``record_result``, ``retry_provider``, ``apply``, ``resume``, or a
    conflicting non-status action) is refused with a bounded error before any
    lock, mutation, poll, or dispatch, so such a request neither mutates under a
    read-only spelling nor has its mutation silently dropped. Status only reads
    campaign files, and those are published with a single atomic
    rename, so it can never observe a half-written or blended campaign. Locking
    it would make ``--status`` block behind a long applied run holding the lock
    and would demand a writable campaign directory to answer a question that
    writes nothing; the same is true of the Board projection built on
    ``list_campaigns``, which is likewise lock-free.

    Command intent itself is validated before either route as well, from one
    authoritative table (:data:`_COMPATIBLE_LEGACY_FLAGS`): an explicit action
    combined with a legacy flag naming a different action -- ``create`` with
    ``resume=True`` most importantly -- is a contradiction with no honest
    reading, and is refused with a bounded error rather than silently resolved
    to whichever of the two the command body happens to test first.

    Option scope is enforced before any locks or lookups: ``--interval`` is
    valid only for the ``watch`` action, ``--timeout`` is valid only for
    ``watch`` and ``upload`` (which use it for watch duration and request timeout
    respectively), and ``--required-providers`` is valid only for campaign
    creation, resume, and dispatch (where it configures or verifies stored
    provider posture). Supplying an option to an action where it would be
    silently ignored is rejected with a bounded error before touching campaign
    state.

    An explicit ``campaign_id`` is validated first, before either route, so a
    malformed identifier produces a bounded error and never creates a campaign
    directory, a lock file, or any other on-disk state.
    """
    repo_path = repo_path or Path.cwd()
    explicit_campaigns_dir = campaigns_dir is not None
    write_dir = campaigns_dir if explicit_campaigns_dir else default_campaigns_dir(repo_path)
    campaigns_dir = write_dir
    err = stderr if stderr is not None else sys.stderr

    # Command intent is validated as a whole, once, before anything else: a
    # request that states two conflicting intents is refused here rather than
    # resolved by whichever branch of the body tests its boolean first. `status`
    # is read-only unconditionally, so a mutating intent spelled alongside it is
    # never executed under a read-only spelling nor silently dropped; and an
    # explicit action is never combined with a legacy flag naming a different
    # one. Nothing has been locked, read, or written yet, so a rejected request
    # leaves no trace at all.
    is_status_request = status or action == "status"
    conflict = _command_intent_conflict(
        action=action,
        record_result=record_result,
        retry_provider=retry_provider,
        apply=apply,
        resume=resume,
        status=status,
        yes=yes,
        interval=interval,
        timeout=timeout,
        required_providers=required_providers,
    )
    if conflict:
        print(f"error: {conflict}", file=err)
        return 1

    parsed_required_providers: tuple[str, ...] | None = None
    if isinstance(required_providers, str):
        req_list = [p.strip() for p in required_providers.split(",") if p.strip()]
        if not req_list:
            print(
                "error: --required-providers cannot be empty; specify a non-empty subset of selected providers",
                file=err,
            )
            return 1
        parsed_required_providers = tuple(req_list)
    elif required_providers is not None:
        parsed_required_providers = tuple(required_providers)
        if not parsed_required_providers:
            print(
                "error: --required-providers cannot be empty; specify a non-empty subset of selected providers",
                file=err,
            )
            return 1

    if campaign_id and action != "watch":
        try:
            validate_campaign_id(campaign_id)
        except ValueError as exc:
            print(f"error: {exc}", file=err)
            return 1

    # What remains is exactly the read-only route; every other spelling is
    # potentially mutating and takes the campaign directory lock.
    is_read_only_status = is_status_request or action in {"upload", "watch"}

    identity: str | None = None
    if not explicit_campaigns_dir:
        identity = resolve_repo_identity(repo_path, repo_slug)
        publish_campaigns_directory(write_dir, identity)
        if is_read_only_status:
            campaigns_dir, discovery_error = resolve_command_campaigns_dir(
                write_dir=write_dir,
                repo_identity=identity,
                campaign_id=campaign_id,
                release_tag=release_tag,
                select_newest=(is_status_request or action == "watch")
                and not campaign_id
                and not release_tag,
            )
            if discovery_error:
                print(f"error: {discovery_error}", file=err)
                return 1

    with ExitStack() as stack:
        if not is_read_only_status:
            try:
                stack.enter_context(locked_campaigns_dir(campaigns_dir))
            except FileLockError:
                # The Windows backend cannot block in the kernel, so it gives up
                # after a bounded wait and raises this instead of an OSError.
                # Contention is not a broken directory, so it gets its own
                # message -- equally bounded and path-free -- rather than the
                # writability advice below.
                print(
                    "error: could not acquire the release campaign directory lock; "
                    "another campaign command is holding it, so retry once that "
                    "command finishes",
                    file=err,
                )
                return 1
            except OSError:
                # Bounded and path-free, like every other campaign error surface:
                # the errno text and the local directory path are never echoed.
                print(
                    "error: could not acquire the release campaign directory lock; "
                    "check that the campaigns directory exists and is writable",
                    file=err,
                )
                return 1
        result = _campaign_command_impl(
            repo_path=repo_path,
            campaigns_dir=campaigns_dir,
            action=action,
            release_tag=release_tag,
            package_spec=package_spec,
            providers=providers,
            required_providers=parsed_required_providers,
            qualification_context=qualification_context,
            starting_version=starting_version,
            package_source=package_source,
            repo_slug=repo_slug,
            issue=issue,
            apply=apply,
            resume=resume,
            status=status,
            campaign_id=campaign_id,
            record_result=record_result,
            record_provider=record_provider,
            retry_provider=retry_provider,
            yes=yes,
            endpoint=endpoint,
            token_env=token_env,
            token_file=token_file,
            token_dir=token_dir,
            install_id=install_id,
            team_id=team_id,
            timeout=timeout,
            interval=interval,
            emit_json=emit_json,
            command_runner=command_runner,
            gh_json_runner=gh_json_runner,
            which_fn=which_fn,
            adapter_runner=adapter_runner,
            env=env,
            time_fn=time_fn,
            sleep_fn=sleep_fn,
            stdout=stdout,
            stderr=stderr,
        )
        if not explicit_campaigns_dir:
            assert identity is not None
            publish_campaigns_directory(write_dir, identity)
            if campaigns_dir != write_dir:
                publish_campaigns_directory(campaigns_dir, identity)
        return result


def _campaign_command_impl(
    *,
    repo_path: Path,
    campaigns_dir: Path,
    action: str | None = None,
    release_tag: str = "",
    package_spec: str = "",
    providers: Sequence[str] = (),
    required_providers: Sequence[str] | None = None,
    qualification_context: str = "",
    starting_version: str = "",
    package_source: str = "",
    repo_slug: str = "",
    issue: str | int = "",
    apply: bool = False,
    resume: bool = False,
    status: bool = False,
    campaign_id: str = "",
    record_result: Path | None = None,
    record_provider: str = "",
    retry_provider: str = "",
    yes: bool = False,
    endpoint: str = "",
    token_env: str = "",
    token_file: Path | None = None,
    token_dir: Path | None = None,
    install_id: str = "",
    team_id: str = "",
    timeout: float | None = None,
    interval: float | None = None,
    emit_json: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    which_fn: Callable[[str], str | None] = shutil.which,
    adapter_runner: AdapterRunner = run_local_adapter_command,
    env: Mapping[str, str] | None = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Body of :func:`campaign_command`.

    Split out only so its caller can decide whether to bracket the whole
    read-decide-invoke-persist sequence in the campaign directory lock. Every
    mutating route holds that lock for the duration of this call; a read-only
    status request runs here with no lock held. This function itself makes no
    locking decision and must not be called directly by mutating callers -- see
    :func:`campaign_command` for the concurrency contract.

    The early ``record_result``/``is_status`` branches below are ordered to
    match that split: the record path (which mutates) is reached only on the
    locked route, and the status path returns before the first write of any
    other branch, so the lock-free route reads and prints and nothing more. The
    two can no longer be requested together -- ``campaign_command`` rejects a
    status request carrying any mutating intent before either route begins.

    The ``is_*`` booleans below therefore describe a request that has already
    been checked for self-contradiction: at most one of ``is_create``,
    ``is_status``, and ``is_resume``/``is_dispatch`` can be true, so the order
    in which the branches test them no longer decides which of two conflicting
    intents wins.

    ``qualification_context`` carries an "unspecified" sentinel: the empty
    string means the caller did not ask for a context at all. That distinction
    matters because ``cold_install`` is both the creation default *and* a
    context a caller can explicitly request. Collapsing the two would make an
    explicit ``--qualification-context cold_install`` against a stored upgrade
    campaign indistinguishable from an omitted flag, so it would be silently
    ignored while the upgrade campaign advanced -- exactly what the
    identity-conflict invariant exists to prevent. Omitted, the context defaults
    to ``cold_install`` when creating and asserts nothing when advancing;
    supplied, it is checked against the stored context before any mutation,
    polling, or dispatch.
    """
    is_status = status or action == "status"
    is_resume = resume or action == "resume"
    is_dispatch = action == "dispatch"
    is_create = action == "create"
    is_upload = action == "upload"
    is_watch = action == "watch"

    if is_watch:
        summary = campaign_watch(
            campaign_id=campaign_id,
            release_tag=release_tag,
            campaigns_dir=campaigns_dir,
            repo_path=repo_path,
            repo_slug=repo_slug,
            issue=issue,
            interval=interval,
            timeout=timeout,
            emit_json=emit_json,
            stdout=stdout,
            stderr=stderr,
            time_fn=time_fn,
            sleep_fn=sleep_fn,
            which_fn=which_fn,
            command_runner=command_runner,
            gh_json_runner=gh_json_runner,
            adapter_runner=adapter_runner,
            env=env,
        )
        if emit_json:
            out = stdout if stdout is not None else sys.stdout
            print(json.dumps(summary, indent=2, sort_keys=True), file=out)
        stop_reason = summary.get("stop_reason")
        if stop_reason == "complete":
            return 0
        if stop_reason == "interrupt":
            return 130
        return 1

    existing, identifier, identifier_error = _load_requested_campaign(
        campaign_id=campaign_id,
        release_tag=release_tag,
        campaigns_dir=campaigns_dir,
    )
    if identifier_error:
        print(f"error: {identifier_error}", file=sys.stderr)
        return 1

    if record_result:
        if not existing:
            print("error: cannot record result without existing campaign", file=sys.stderr)
            return 1
        if not record_provider:
            print("error: --record-provider required when recording result", file=sys.stderr)
            return 1
        try:
            updated = record_manual_result(
                existing,
                record_provider,
                record_result,
                campaigns_dir=campaigns_dir,
                repo_path=repo_path,
            )
            if emit_json:
                print(json.dumps(updated, indent=2, sort_keys=True))
            else:
                print(render_campaign_text(updated))
            return 0 if updated.get("status") != "blocked" else 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if is_status:
        if identifier:
            # An explicitly named campaign is answered with that campaign or
            # nothing: falling back to the newest unrelated campaign would
            # report another release's state under the requested identifier.
            if existing is None:
                print(f"error: no campaign found for {identifier!r}", file=sys.stderr)
                return 1
        else:
            all_c = list_campaigns(campaigns_dir)
            if not all_c:
                print("error: no campaigns found", file=sys.stderr)
                return 1
            existing = all_c[0]
        if emit_json:
            print(json.dumps(existing, indent=2, sort_keys=True))
        else:
            print(render_campaign_text(existing))
        return 0

    if is_upload:
        # Upload publishes what an existing campaign already recorded; like
        # `resume`/`dispatch` it never falls through to creating one, and like
        # `status` it writes no campaign state.
        if existing is None:
            target = f" for {identifier!r}" if identifier else ""
            print(
                f"error: no existing campaign{target} to upload; create and complete "
                f"one first with --release-tag <tag>",
                file=sys.stderr,
            )
            return 1
        cloud = _load_cloud_client()
        try:
            result = campaign_upload(
                existing,
                endpoint=endpoint,
                token_env=token_env,
                token_file=token_file,
                token_dir=token_dir,
                install_id=install_id,
                team_id=team_id,
                yes=yes,
                timeout=timeout if timeout is not None else 20.0,
            )
        except cloud.CloudBundleError as exc:
            # Every message reaching here is locally generated and bounded: the
            # endpoint's own response text is converted to a fixed line inside
            # `campaign_upload` before it can be printed.
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if emit_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(render_campaign_upload_text(result))
        return 1 if result["status"] == "invalid_results" else 0

    if existing:
        # An existing campaign is never replaced by a fresh queued one: the
        # recorded provider states, evidence, and attempt markers are the only
        # thing standing between a repeated invocation and a rerun local
        # adapter or a reposted paid dispatch. Every non-status invocation that
        # names an existing campaign is routed through resume/advance
        # semantics, which honor `attempted_at` idempotency.
        if is_create:
            print(
                f"error: campaign {str(existing.get('campaign_id') or '')!r} already exists "
                f"for release tag {str(existing.get('release_tag') or '')!r}; use "
                f"`status`, `resume`, or `dispatch` instead of `create`",
                file=sys.stderr,
            )
            return 1
        conflict = _existing_campaign_conflict(
            existing,
            package_spec=package_spec,
            qualification_context=qualification_context,
            starting_version=starting_version,
            package_source=package_source,
            providers=providers,
            required_providers=required_providers,
            repo_slug=repo_slug,
        )
        if conflict:
            print(f"error: {conflict}", file=sys.stderr)
            return 1
        if not (is_resume or is_dispatch):
            print(
                f"note: campaign {str(existing.get('campaign_id') or '')!r} already exists; "
                f"advancing it (resume semantics) instead of creating a new one",
                file=sys.stderr,
            )
        retry_canonical, retry_error = _validate_retry_provider(retry_provider, existing)
        if retry_error:
            print(f"error: {retry_error}", file=sys.stderr)
            return 1
        if repo_slug and not str(existing.get("repo_slug") or ""):
            # A campaign created without a repository slug has nowhere to post a
            # hosted dispatch, and its provider set is fixed at creation -- so
            # the slug is supplied here instead. Every rejection above has
            # already been made, so this is the first mutation: fill the empty
            # value and persist it *before* advancing, so the dispatch that uses
            # it and every later poll that answers it read the same stored
            # repository. A non-empty stored slug is never rewritten (a mismatch
            # is rejected as a conflict), so this cannot change an existing
            # campaign's identity.
            existing["repo_slug"] = repo_slug
            save_campaign(existing, campaigns_dir)
        updated = dispatch_or_advance_campaign(
            existing,
            apply=apply,
            issue_number=issue,
            repo_path=repo_path,
            campaigns_dir=campaigns_dir,
            which_fn=which_fn,
            command_runner=command_runner,
            gh_json_runner=gh_json_runner,
            adapter_runner=adapter_runner,
            env=env,
            retry_provider=retry_canonical,
        )
        if emit_json:
            print(json.dumps(updated, indent=2, sort_keys=True))
        else:
            print(render_campaign_text(updated))
        return 0 if updated.get("status") != "blocked" else 1

    if is_resume or is_dispatch:
        # `resume` and `dispatch` act on an existing campaign only; neither
        # silently falls through to creating (and immediately dispatching) a
        # brand-new campaign.
        target = f" for {identifier!r}" if identifier else ""
        print(
            f"error: no existing campaign{target} to "
            f"{'dispatch' if is_dispatch else 'resume'}; create one first with "
            f"--release-tag <tag>",
            file=sys.stderr,
        )
        return 1

    if not release_tag:
        print("error: --release-tag is required to create a campaign", file=sys.stderr)
        return 1

    try:
        campaign_obj = initialize_campaign(
            release_tag=release_tag,
            package_spec=package_spec,
            # An omitted context creates a cold-install campaign, the documented
            # default; only the comparison against an existing campaign needs to
            # tell an omitted flag from an explicit `cold_install`.
            qualification_context=qualification_context or "cold_install",
            starting_version=starting_version,
            # An omitted source creates a pypi campaign, the documented default;
            # same "unspecified vs. explicit default" distinction as context.
            package_source=package_source or DEFAULT_PACKAGE_SOURCE,
            providers=providers,
            required_providers=required_providers,
            repo_slug=repo_slug,
            campaign_id=campaign_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    campaign_dict = campaign_obj.to_dict()

    # Creation reaches here only when no *stored campaign* answers the request,
    # which is not quite the same as "no file occupies this id": a campaign id
    # is a storage key, and a tag-only request resolves by release tag, so the
    # id this campaign would be created under can still be taken -- by a
    # campaign for another release stored under a custom id, or by a file that
    # failed to load as a campaign. Saving would overwrite it. Refuse instead;
    # an existing campaign is never replaced, and neither is anything else in
    # the campaign directory.
    created_id = str(campaign_dict.get("campaign_id") or "")
    if (campaigns_dir / campaign_filename(created_id)).exists():
        print(
            f"error: campaign id {created_id!r} is already in use by a stored campaign "
            f"file; pass --campaign-id with an unused id to create a campaign for "
            f"release tag {release_tag!r}",
            file=sys.stderr,
        )
        return 1

    retry_canonical, retry_error = _validate_retry_provider(retry_provider, campaign_dict)
    if retry_error:
        print(f"error: {retry_error}", file=sys.stderr)
        return 1
    updated = dispatch_or_advance_campaign(
        campaign_dict,
        apply=apply,
        issue_number=issue,
        repo_path=repo_path,
        campaigns_dir=campaigns_dir,
        which_fn=which_fn,
        command_runner=command_runner,
        gh_json_runner=gh_json_runner,
        adapter_runner=adapter_runner,
        env=env,
        retry_provider=retry_canonical,
    )

    if emit_json:
        print(json.dumps(updated, indent=2, sort_keys=True))
    else:
        print(render_campaign_text(updated))
    return 0 if updated.get("status") != "blocked" else 1
