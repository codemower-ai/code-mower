#!/usr/bin/env python3
"""Release campaign orchestrator for multi-provider release qualification."""

from __future__ import annotations

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
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import config as code_mower_config
    from code_mower import lane_status
    from code_mower.file_locks import FileLockError, exclusive_file_lock
    from code_mower.provider_registry import REFERENCE_PROVIDERS, ProviderLane
    from code_mower.release_qualify import (
        _detect_host_class,
        _detect_runtime_class,
        _extract_package_identity,
        _parse_exact_package_spec,
        _validate_qualification_context,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        validate_adoption_result_payload,
    )
else:
    from . import config as code_mower_config
    from . import lane_status
    from .file_locks import FileLockError, exclusive_file_lock
    from .provider_registry import REFERENCE_PROVIDERS, ProviderLane
    from .release_qualify import (
        _detect_host_class,
        _detect_runtime_class,
        _extract_package_identity,
        _parse_exact_package_spec,
        _validate_qualification_context,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        validate_adoption_result_payload,
    )

CAMPAIGN_SCHEMA = "code_mower.releaseCampaign.v1"
DISPATCH_SCHEMA = "code_mower.releaseCampaignDispatch.v1"
RESULT_MARKER_SCHEMA = "code_mower.releaseCampaignResult.v1"
BOARD_RELEASE_CAMPAIGNS_SCHEMA = "code_mower.boardReleaseCampaigns.v1"
DEFAULT_CAMPAIGNS_RELATIVE_DIR = Path(".code-mower") / "campaigns"
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 900

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
    "cursor_bugbot",
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
    "cursor": "cursor_bugbot",
    "cursor_bugbot": "cursor_bugbot",
    "cursor_grok_bot": "cursor_bugbot",
    "cursor_cloud_agent": "cursor_bugbot",
    "grok_bot": "cursor_bugbot",
    "grok": "grok_build",
    "grok_build": "grok_build",
    "devin": "devin",
}

VALID_PROVIDER_STATES = {
    "queued",
    "running",
    "blocked",
    "unavailable",
    "complete",
}

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
    dispatch_mode: str = "dry_run"
    dispatched_at: str | None = None
    completed_at: str | None = None
    # Set before invoking a paid/hosted dispatch or local adapter (even if it
    # fails or the outcome is uncertain). Once set, resume never repeats the
    # attempt automatically -- only an explicit --retry-provider does.
    attempted_at: str | None = None
    next_action: str = ""
    next_detail: str = ""
    error: str = ""
    dispatch_ref: dict[str, Any] = field(default_factory=dict)
    adoption_result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in VALID_PROVIDER_STATES:
            self.state = "unavailable"

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
    repo_slug: str
    status: str
    dry_run: bool
    elapsed_seconds: float
    created_at: str
    updated_at: str
    next_action: str
    next_detail: str
    providers: list[dict[str, Any]]

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
) -> str:
    seed = (
        f"{campaign_id}:{provider}:{release_tag}:{qualification_context}:{starting_version}"
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


@contextmanager
def locked_campaigns_dir(campaigns_dir: Path) -> Iterator[IO[str]]:
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
    with exclusive_file_lock(lock_path) as lock_file:
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
    if not package_identity:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            candidate = json.load(fh)
        validate_adoption_result_payload(
            candidate, expected_package_identity=package_identity
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if (
        candidate.get("release_tag") != release_tag
        or candidate.get("provider") != provider
        or candidate.get("qualification_context") != qualification_context
        or candidate.get("starting_version") != starting_version
    ):
        return None
    return candidate


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
) -> dict[str, Any] | None:
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
    """
    if not package_identity:
        return None
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
        ):
            continue
        adoption_result = wrapper.get("adoption_result")
        if not isinstance(adoption_result, dict):
            continue
        try:
            validate_adoption_result_payload(
                adoption_result, expected_package_identity=package_identity
            )
        except ValueError:
            continue
        if (
            adoption_result.get("provider") != provider
            or adoption_result.get("release_tag") != release_tag
            or adoption_result.get("qualification_context") != qualification_context
            or str(adoption_result.get("starting_version") or "") != starting_version
        ):
            continue
        return adoption_result
    return None


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
    starting_version: str = "",
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> tuple[bool, dict[str, Any], str]:
    """Post the dispatch comment that tells a remote provider exactly what to qualify.

    An upgrade dispatch advertises its exact ``starting_version`` in both the
    machine-readable marker and the human-facing instructions: the accepted
    result must carry that same starting version, so a remote runner that is
    never told it would be guessing. A dispatch that cannot state the starting
    version of an upgrade campaign is refused rather than posted. Cold-install
    (and ``unknown``) campaigns have no starting version and omit the field.
    """
    if not repo_slug or not issue_number:
        return False, {}, _safe_error("missing_issue_number")
    if qualification_context == "upgrade" and not starting_version:
        return False, {}, _safe_error("campaign_identity_incomplete")

    dispatch_marker = {
        "schema": DISPATCH_SCHEMA,
        "campaign_id": campaign_id,
        "release_tag": release_tag,
        "package_spec": package_spec,
        "provider": provider,
        "qualification_context": qualification_context,
        "idempotency_key": idempotency_key,
    }
    if starting_version:
        dispatch_marker["starting_version"] = starting_version
    marker_str = json.dumps(dispatch_marker, sort_keys=True)
    starting_version_line = (
        f"- **Starting Version:** `{starting_version}`\n" if starting_version else ""
    )
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
        f"- **Context:** `{qualification_context}`\n"
        f"{starting_version_line}"
        f"- **Idempotency Key:** `{idempotency_key}`\n\n"
        f"Reply with a comment containing a `CODE_MOWER_ADOPTION_RESULT` "
        f"marker wrapping schema `{RESULT_MARKER_SCHEMA}` with matching "
        f"campaign_id, provider, release_tag, and idempotency_key, plus an "
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


def _poll_github_comments(
    repo_slug: str,
    issue_number: int | str,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
) -> tuple[list[dict[str, Any]], str]:
    if not repo_slug or not issue_number:
        return [], _safe_error("github_poll_unavailable")
    data, error = gh_json_runner(
        ["issue", "view", str(issue_number), "--repo", repo_slug, "--json", "comments"]
    )
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


def _build_adapter_argv(
    lane: ProviderLane,
    resolved_command: str,
    *,
    release_tag: str,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
    output_path: Path,
    repo_path: Path,
    argv_template: Any,
) -> list[str]:
    validated_template = _validate_adapter_argv_template(argv_template)
    substitutions = {
        "command": resolved_command,
        "release_tag": release_tag,
        "package_spec": package_spec,
        "qualification_context": qualification_context,
        "starting_version": starting_version,
        "output": str(output_path),
        "repo_path": str(repo_path),
    }
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

    Only `campaign_adapter_argv` and `campaign_adapter_timeout_seconds` are
    read from the matching lane's `provider_config`; every other key in the
    repo config is ignored here so this does not widen the general config
    contract. A missing repo config, a missing `lanes` key, or a lane with no
    matching entry is not an override (empty mapping, no error). An existing
    repo config that fails to load, or is structurally malformed (a present
    non-mapping `lanes`, lane, or provider_config entry), returns
    `adapter_configuration_invalid` instead of silently treating the config
    as absent -- the specific override values are validated by the caller.

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
        for key in ("campaign_adapter_argv", "campaign_adapter_timeout_seconds")
        if key in provider_cfg
    }
    return overrides, "", ""


def _resolve_campaign_adapter_config(
    lane: ProviderLane,
    repo_path: Path,
) -> tuple[Any, Any, str, str]:
    """Resolve the effective campaign_adapter_argv/timeout for a lane.

    Overlays only the two allowed override keys from repo_path/code-mower.yml
    onto the immutable reference lane's provider_config; the reference lane
    itself is never mutated. Returns
    (argv_template, timeout_value, error_code, error_detail).
    """
    overrides, error, detail = _load_campaign_adapter_overrides(lane, repo_path)
    if error:
        return None, None, error, detail
    argv_template = overrides.get("campaign_adapter_argv", lane.provider_config.get("campaign_adapter_argv"))
    timeout_value = overrides.get(
        "campaign_adapter_timeout_seconds",
        lane.provider_config.get("campaign_adapter_timeout_seconds"),
    )
    return argv_template, timeout_value, "", ""


def _invoke_local_adapter(
    lane: ProviderLane,
    provider: str,
    *,
    release_tag: str,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
    output_path: Path,
    repo_path: Path,
    which_fn: Callable[[str], str | None],
    adapter_runner: AdapterRunner,
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

    resolved = _find_command(lane, which_fn=which_fn)
    if not resolved:
        cmd = lane.provider_config.get("command") or provider
        return None, _safe_error("command_not_found"), f"command not found: {cmd}"
    resolved_path = which_fn(resolved) or resolved

    try:
        argv = _build_adapter_argv(
            lane,
            resolved_path,
            release_tag=release_tag,
            package_spec=package_spec,
            qualification_context=qualification_context,
            starting_version=starting_version,
            output_path=output_path,
            repo_path=repo_path,
            argv_template=argv_template,
        )
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
    providers: Sequence[str] = (),
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
        seen_providers.add(canonical_name)
        resolved_providers.append((canonical_name, lane))

    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    environment = _detect_environment()

    campaign_providers: list[dict[str, Any]] = []
    for canonical_name, lane in resolved_providers:
        idemp_key = _compute_idempotency_key(
            campaign_id, canonical_name, release_tag, qualification_context, starting_version
        )
        cp = CampaignProvider(
            provider=canonical_name,
            lane_id=lane.lane_id,
            driver=lane.driver,
            state="queued",
            environment=environment,
            elapsed_seconds=0.0,
            idempotency_key=idemp_key,
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
        repo_slug=repo_slug,
        status=overall_status,
        dry_run=True,
        elapsed_seconds=0.0,
        created_at=now_utc,
        updated_at=now_utc,
        next_action=next_action,
        next_detail=next_detail,
        providers=campaign_providers,
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
) -> dict[str, Any]:
    """Execute dispatch, polling, or status progression on a campaign.

    `retry_provider` is the only way a provider whose applied dispatch/adapter
    was already attempted (`attempted_at` set) gets invoked again -- ordinary
    resume never repeats a paid/hosted dispatch or local adapter run.
    """
    current_env = os.environ if env is None else env
    repo_path = repo_path or Path.cwd()
    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path)
    results_dir = campaigns_dir / "results"
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Applied is monotonic (see `campaign_has_been_applied`): this run may turn
    # a dry-run campaign into an applied one, but a poll that omits `--apply`
    # never turns an applied campaign back into a preview.
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
    repo_slug = str(campaign.get("repo_slug") or "")

    retry_canonical = ""
    if retry_provider:
        try:
            retry_canonical, _ = resolve_provider_lane(retry_provider)
        except ValueError:
            retry_canonical = ""

    for provider_data in campaign.get("providers", []):
        provider = str(provider_data.get("provider") or "")
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
                provider_data["next_detail"] = f"outcome: {outcome}"
            provider_data["completed_at"] = now_utc
            continue

        # 2. If already complete, preserve state
        if current_state == "complete":
            provider_data["next_action"] = "none"
            continue

        # 3. If running, poll for a bound result marker
        if current_state == "running":
            dispatch_ref = provider_data.get("dispatch_ref", {})
            ref_issue = dispatch_ref.get("issue_number") or issue_number
            found_result = None
            if ref_issue and repo_slug:
                comments, error = _poll_github_comments(
                    repo_slug,
                    ref_issue,
                    gh_json_runner=gh_json_runner,
                )
                if error:
                    provider_data["error"] = error
                else:
                    provider_data["error"] = ""
                    trusted_authors = _resolve_trusted_bot_authors(lane, env=current_env)
                    for comment in comments:
                        if not _is_trusted_github_author(_comment_author_login(comment), trusted_authors):
                            # Identity binding (campaign/provider/tag/idempotency key)
                            # alone is not enough -- those fields are visible in the
                            # public dispatch comment, so only a configured trusted
                            # author's reply is ever considered.
                            continue
                        body = str(comment.get("body") or "")
                        found_result = _extract_bound_adoption_result(
                            body,
                            campaign_id=campaign_id,
                            provider=provider,
                            release_tag=release_tag,
                            idempotency_key=str(provider_data.get("idempotency_key") or ""),
                            qualification_context=context,
                            starting_version=starting_version,
                            package_identity=package_identity,
                        )
                        if found_result:
                            provider_data["adoption_result"] = found_result
                            provider_data["elapsed_seconds"] = float(
                                found_result.get("elapsed_seconds") or 0.0
                            )
                            provider_data["completed_at"] = now_utc
                            outcome = found_result.get("outcome")
                            if outcome in {"pass", "pass_with_warnings"}:
                                provider_data["state"] = "complete"
                                provider_data["next_action"] = "none"
                            else:
                                provider_data["state"] = "blocked"
                                provider_data["next_action"] = (
                                    f"inspect {provider} qualification failures"
                                )
                            break
            if found_result is not None or not is_explicit_retry:
                # Ordinary resume is poll-only and never redispatches. An
                # explicit retry that already found a valid trusted result
                # is complete/blocked from polling above -- it must not be
                # redispatched either.
                continue
            # Explicit retry of a still-running provider with no valid
            # trusted result yet: fall through to the capability checks and
            # applied-dispatch section below for exactly one redispatch.

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
            provider_data["attempted_at"] = now_utc
            save_campaign(campaign, campaigns_dir)
            start_p = time.time()
            result, error_code, detail = _invoke_local_adapter(
                lane,
                provider,
                release_tag=release_tag,
                package_spec=package_spec,
                qualification_context=context,
                starting_version=starting_version,
                output_path=result_path,
                repo_path=repo_path,
                which_fn=which_fn,
                adapter_runner=adapter_runner,
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
                    provider_data["next_detail"] = f"outcome: {outcome}"

        elif lane.driver in {"saas_event", "hosted_bridge"}:
            if not has_creds:
                provider_data["state"] = "unavailable"
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
                provider_data["state"] = "unavailable"
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
                provider_data["state"] = "unavailable"
                provider_data["error"] = _safe_error("campaign_identity_incomplete")
                provider_data["next_action"] = (
                    f"recreate the campaign with --starting-version before dispatching {provider}"
                )
                provider_data["next_detail"] = "upgrade campaign is missing starting_version"
            else:
                provider_data["attempted_at"] = now_utc
                save_campaign(campaign, campaigns_dir)
                ok, ref, err = _dispatch_github_comment(
                    repo_slug,
                    issue_number,
                    campaign_id,
                    release_tag,
                    package_spec,
                    provider,
                    context,
                    provider_data["idempotency_key"],
                    starting_version=starting_version,
                    command_runner=command_runner,
                )
                if ok:
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
                else:
                    provider_data["state"] = "unavailable"
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

    overall_status, next_action, next_detail = _aggregate_campaign_status(
        campaign.get("providers", []),
        dry_run=campaign.get("dry_run", True),
    )
    campaign["status"] = overall_status
    campaign["next_action"] = next_action
    campaign["next_detail"] = next_detail
    campaign["updated_at"] = now_utc

    total_elapsed = sum(
        float(p.get("elapsed_seconds") or 0.0)
        for p in campaign.get("providers", [])
    )
    campaign["elapsed_seconds"] = round(total_elapsed, 2)

    save_campaign(campaign, campaigns_dir)
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
            f"adoption result tag {adoption_res.get('release_tag')} does not match campaign {release_tag}"
        )

    canonical_provider, _ = resolve_provider_lane(provider)
    if adoption_res.get("provider") != canonical_provider:
        raise ValueError(
            f"adoption result provider {adoption_res.get('provider')!r} does not match {canonical_provider!r}"
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
        lines.append(f"- {p.get('provider')}: {state} ({env}, elapsed {elapsed:.1f}s) -> {action}{detail}")
    return "\n".join(lines)


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


def release_campaigns_board_payload(
    repo_path: Path | str = ".",
    *,
    campaigns_dir: Path | None = None,
) -> dict[str, Any]:
    dir_path = campaigns_dir or default_campaigns_dir(repo_path)
    if not dir_path.is_dir():
        return {
            "schema": BOARD_RELEASE_CAMPAIGNS_SCHEMA,
            "available": True,
            "campaigns": [],
            "card_count": 0,
            "next_action": "no active campaigns",
            "next_detail": "run code-mower release campaign --release-tag <tag> to start one",
        }

    campaigns = list_campaigns(dir_path)
    projected_campaigns: list[dict[str, Any]] = []
    total_cards = 0

    # Every field below is projected defensively: persisted campaign files are
    # read-only here, may predate the current schema, and one malformed value
    # must degrade that single field (or skip that single provider) rather
    # than raise out of the Board payload.
    for c in campaigns:
        cards: list[dict[str, Any]] = []
        raw_providers = c.get("providers")
        for p in raw_providers if isinstance(raw_providers, list) else []:
            if not isinstance(p, Mapping):
                continue
            provider = _board_text(p, "provider", "")
            cards.append(
                {
                    "release": _board_text(c, "release_tag", ""),
                    "provider": provider,
                    "lane_id": _board_text(p, "lane_id", provider),
                    "environment": _board_text(p, "environment", "local"),
                    "state": _board_text(p, "state", "queued"),
                    "elapsed_seconds": _board_elapsed_seconds(p.get("elapsed_seconds")),
                    "next_action": _board_text(p, "next_action", ""),
                    "next_detail": _board_text(p, "next_detail", ""),
                }
            )
        total_cards += len(cards)
        projected_campaigns.append(
            {
                "campaign_id": _board_text(c, "campaign_id", ""),
                "release_tag": _board_text(c, "release_tag", ""),
                "package_spec": _board_text(c, "package_spec", ""),
                "qualification_context": _board_text(c, "qualification_context", ""),
                "status": _board_text(c, "status", "queued"),
                "dry_run": _board_dry_run(c.get("dry_run", True)),
                "elapsed_seconds": _board_elapsed_seconds(c.get("elapsed_seconds")),
                "next_action": _board_text(c, "next_action", ""),
                "next_detail": _board_text(c, "next_detail", ""),
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


CAMPAIGN_ACTIONS = ("create", "status", "resume", "dispatch")


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
    providers: Sequence[str],
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
    return ""


def campaign_command(
    *,
    action: str | None = None,
    release_tag: str = "",
    package_spec: str = "",
    providers: Sequence[str] = (),
    qualification_context: str = "",
    starting_version: str = "",
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
    emit_json: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    which_fn: Callable[[str], str | None] = shutil.which,
    adapter_runner: AdapterRunner = run_local_adapter_command,
    env: Mapping[str, str] | None = None,
) -> int:
    """Create, inspect, or advance a release qualification campaign.

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

    A status invocation -- ``status=True`` or ``action="status"`` -- takes no
    lock at all, because it is always read-only: combining it with a mutating
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

    An explicit ``campaign_id`` is validated first, before either route, so a
    malformed identifier produces a bounded error and never creates a campaign
    directory, a lock file, or any other on-disk state.
    """
    repo_path = repo_path or Path.cwd()
    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path)

    if campaign_id:
        try:
            validate_campaign_id(campaign_id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # `status` is read-only, unconditionally: a mutating intent spelled
    # alongside it is rejected here rather than executed under a read-only
    # spelling or silently dropped. Nothing has been locked, read, or written
    # yet, so a rejected request leaves no trace at all.
    is_status_request = status or action == "status"
    if is_status_request:
        conflict = _status_mutation_conflict(
            action=action,
            record_result=record_result,
            retry_provider=retry_provider,
            apply=apply,
            resume=resume,
        )
        if conflict:
            print(f"error: {conflict}", file=sys.stderr)
            return 1

    # What remains is exactly the read-only route; every other spelling is
    # potentially mutating and takes the campaign directory lock.
    is_read_only_status = is_status_request

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
                    file=sys.stderr,
                )
                return 1
            except OSError:
                # Bounded and path-free, like every other campaign error surface:
                # the errno text and the local directory path are never echoed.
                print(
                    "error: could not acquire the release campaign directory lock; "
                    "check that the campaigns directory exists and is writable",
                    file=sys.stderr,
                )
                return 1
        return _campaign_command_impl(
            repo_path=repo_path,
            campaigns_dir=campaigns_dir,
            action=action,
            release_tag=release_tag,
            package_spec=package_spec,
            providers=providers,
            qualification_context=qualification_context,
            starting_version=starting_version,
            repo_slug=repo_slug,
            issue=issue,
            apply=apply,
            resume=resume,
            status=status,
            campaign_id=campaign_id,
            record_result=record_result,
            record_provider=record_provider,
            retry_provider=retry_provider,
            emit_json=emit_json,
            command_runner=command_runner,
            gh_json_runner=gh_json_runner,
            which_fn=which_fn,
            adapter_runner=adapter_runner,
            env=env,
        )


def _campaign_command_impl(
    *,
    repo_path: Path,
    campaigns_dir: Path,
    action: str | None = None,
    release_tag: str = "",
    package_spec: str = "",
    providers: Sequence[str] = (),
    qualification_context: str = "",
    starting_version: str = "",
    repo_slug: str = "",
    issue: str | int = "",
    apply: bool = False,
    resume: bool = False,
    status: bool = False,
    campaign_id: str = "",
    record_result: Path | None = None,
    record_provider: str = "",
    retry_provider: str = "",
    emit_json: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    which_fn: Callable[[str], str | None] = shutil.which,
    adapter_runner: AdapterRunner = run_local_adapter_command,
    env: Mapping[str, str] | None = None,
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
            providers=providers,
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
            providers=providers,
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
