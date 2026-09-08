"""Atomic pull-request outcome contract for CodeMower.com uploads."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from .errors import CloudBundleError


PR_OUTCOME_EVENT_TYPE = "pr_outcome"
PR_OUTCOME_SCHEMA = "code_mower.prOutcome.v1"
PR_OUTCOME_OBSERVATION_STATE_SCHEMA = "code_mower.prOutcomeObservations.v1"
DEFAULT_OBSERVATION_STATE_PATH = Path(".code-mower") / "pr-outcome-observations.json"
# ``reverted`` is reserved for producers that hold rollback evidence.
# GitHub's PR-list ``state`` field can only prove open, merged, or closed.
PR_OUTCOME_VALUES = ("open", "merged", "closed_unmerged", "reverted")
PR_OUTCOME_RANK = {
    "open": 0,
    "closed_unmerged": 1,
    "merged": 2,
    "reverted": 3,
}
PR_COST_COVERAGE_VALUES = ("complete", "partial", "unknown")
PR_OUTCOME_COUNT_METRICS = (
    "pr_count",
    "fix_round_count",
    "reviewer_catch_count",
    "blocking_bug_count",
    "cost_reported_run_count",
    "cost_expected_run_count",
    "cost_covered_pr_count",
)
PR_OUTCOME_COST_METRICS = ("reported_cost_usd",)
# Metadata-only lane label used when a run attempt is known to exist but its
# evidence could not be read or attributed.  It is a fixed constant so it can
# never carry paths, file contents, or secrets.
UNATTRIBUTED_EVIDENCE_LANE = "unreadable-evidence"
# Fallback for lane/provider identifiers that do not match the bounded
# categorical format.  It is a fixed constant so invalid paths, commands,
# whitespace, and secret-like text can never be placed in uploadable fields.
UNKNOWN_EVIDENCE_SOURCE = "unknown-source"
_LANE_IDENTIFIER_RE = re.compile(r"^[a-z0-9]+(?:[_.-][a-z0-9]+)*$")
PR_OUTCOME_DIMENSIONS = (
    "pr_outcome_schema",
    "pr_number",
    "opened_at",
    "merged_at",
    "closed_at",
    "reverted_at",
    "outcome",
    "cost_coverage",
    "missing_cost_sources",
    "pr_outcome_observation_version",
    "evidence_incomplete",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudBundleError(f"pr_outcome {field} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, field: str) -> dt.datetime:
    text = _required_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudBundleError(f"pr_outcome {field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CloudBundleError(f"pr_outcome {field} must include a UTC offset")
    return parsed


def _outcome_rank(value: object) -> int | None:
    return PR_OUTCOME_RANK.get(str(value or "").strip())


def _count(metrics: Mapping[str, Any], field: str, *, required: bool = False) -> int | None:
    value = metrics.get(field)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CloudBundleError(f"pr_outcome metric {field!r} must be a non-negative integer")
    return value


def _run_event_identity(event: Mapping[str, Any]) -> str:
    """Return the source identity used for dedup and evidence versioning.

    Reviewer-spend rows converted to ``reviewer_run`` events keep their source
    ``run_id`` in ``dimensions.spend_run_id``; the envelope ``event_id`` may be
    a generated fallback, so the spend identity wins whenever it is present.
    """

    dimensions = _as_mapping(event.get("dimensions"))
    if "spend_run_id" in dimensions:
        return str(dimensions.get("spend_run_id") or "").strip()
    return str(event.get("event_id") or "").strip()


def _canonical_lane_identifier(value: object) -> str:
    """Return a bounded lane/provider identifier or the fixed fallback.

    Only lowercase alphanumerics with internal ``._-`` separators and a
    reasonable length are allowed, matching existing lane names.  Paths,
    command strings, whitespace-heavy strings, and secret-like text are
    replaced with ``UNKNOWN_EVIDENCE_SOURCE`` so they never reach an upload.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    text = text.lower()
    if len(text) <= 64 and _LANE_IDENTIFIER_RE.match(text):
        return text
    return UNKNOWN_EVIDENCE_SOURCE


def _run_event_canonical(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable, metadata-only representation for evidence versioning."""

    dimensions = _as_mapping(event.get("dimensions"))
    metrics = _as_mapping(event.get("metrics"))
    item: dict[str, Any] = {
        "event_id": _run_event_identity(event),
        "event_type": str(event.get("event_type") or "").strip(),
        "provider": _canonical_lane_identifier(event.get("provider")),
        "lens": _canonical_lane_identifier(event.get("lens")),
        "status": str(event.get("status") or "").strip(),
        "repo_slug": str(event.get("repo_slug") or "").strip(),
        "pr_number": str(dimensions.get("pr_number") or "").strip(),
        "builder_provider": _canonical_lane_identifier(
            dimensions.get("builder_provider")
        ),
        "lane": _canonical_lane_identifier(dimensions.get("lane")),
        "head_sha": str(dimensions.get("head_sha") or "").strip(),
    }
    if "cost_usd" in metrics:
        cost = metrics["cost_usd"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, int | float)
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise CloudBundleError(
                "pr_outcome run event cost_usd must be finite and non-negative"
            )
        item["cost_usd"] = cost
    return item


def _run_events_digest(run_events: list[Mapping[str, Any]]) -> str:
    """Return a deterministic SHA-256 digest of the observed run evidence.

    The digest is metadata-only: it uses the same visible fields that are safe
    to include in pr_outcome identity, never raw diffs, prompts, or transcripts.
    """

    canonical = [_run_event_canonical(event) for event in run_events]
    try:
        canonical.sort(
            key=lambda item: json.dumps(item, sort_keys=True, allow_nan=False)
        )
        encoded = json.dumps(
            canonical, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise CloudBundleError(
            "pr_outcome run evidence could not be serialized deterministically"
        ) from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _observation_fingerprint(
    *,
    outcome: str,
    opened_at: str,
    merged_at: str,
    closed_at: str,
    reverted_at: str,
    evidence_digest: str,
    evidence_incomplete: bool = False,
    missing_sources: list[str] | None = None,
) -> str:
    """Return a deterministic fingerprint of the full observation content."""

    payload = {
        "outcome": outcome,
        "opened_at": opened_at,
        "merged_at": merged_at,
        "closed_at": closed_at,
        "reverted_at": reverted_at,
        "evidence_digest": evidence_digest,
        "evidence_incomplete": bool(evidence_incomplete),
        "incomplete_evidence_state": sorted(set(missing_sources or [])),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_freshness(created_at: str) -> str:
    """Return the canonical source-freshness timestamp for a pr_outcome.

    The source freshness is the GitHub-side evidence timestamp (``updatedAt``
    for the PR list, supplied as ``created_at``).  It is kept separate from
    the synthetic ``created_at`` emitted on the event so that monotonic
    ordering can be enforced by source advancement while corrected spend
    evidence with an unchanged source timestamp still receives a later
    observation timestamp.
    """

    text = str(created_at or "").strip()
    if not text:
        return _utc_now()
    try:
        parsed = _timestamp(text, "source_freshness")
    except CloudBundleError:
        return _utc_now()
    return parsed.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(text: str, field: str) -> dt.datetime:
    """Parse a UTC-normalized timestamp for ordering comparisons."""

    parsed = _timestamp(text, field)
    return parsed.astimezone(dt.UTC).replace(microsecond=0)


def max_source_freshness(first: str, second: str) -> str:
    """Return the later of two source-freshness timestamps.

    The persisted observation watermark must never regress, so state writes
    keep the maximum of the stored and newly emitted freshness.  Values that
    fail to parse lose to parseable ones; if neither parses, ``second`` (the
    newly emitted value) wins.
    """

    best_text = ""
    best_at: dt.datetime | None = None
    for text in (str(first or "").strip(), str(second or "").strip()):
        if not text:
            continue
        try:
            parsed = _parse_utc(text, "source_freshness")
        except CloudBundleError:
            continue
        if best_at is None or parsed > best_at:
            best_at = parsed
            best_text = text
    if best_at is None:
        return str(second or first or "").strip()
    return best_text


def _observed_at(
    source_freshness: str,
    run_events: list[Mapping[str, Any]],
    *,
    fingerprint: str,
    outcome: str,
    prior_observation: Mapping[str, Any] | None = None,
) -> str:
    """Return a deterministic observation timestamp for this pr_outcome.

    The synthetic observation timestamp is the latest of the source
    freshness, the latest run event ``created_at``, and one second after the
    prior observation's synthetic timestamp.  This gives late-arriving local
    spend evidence a later ``created_at`` even when the GitHub ``updatedAt``
    did not change, while keeping the source-freshness ordering independent.

    ``prior_observation`` carries the locally recorded ``fingerprint``,
    ``created_at``, ``source_freshness``, and ``outcome`` of the last emitted
    observation for this PR.  An unchanged fingerprint reproduces the prior
    ``created_at`` so retries stay byte-for-byte idempotent; a changed
    fingerprint with an unchanged source timestamp still receives a later
    observation timestamp.  Source freshness is compared before the
    idempotent early return so a stale retry can never be accepted and its
    older ``source_freshness`` can never be persisted over the recorded
    watermark.  A stale snapshot whose source evidence is older than the
    prior source, or whose outcome regressed without a newer source, is
    rejected before an observation timestamp can be assigned.
    """

    fresh = _parse_utc(source_freshness, "source_freshness")

    latest = fresh
    for event in run_events:
        ts_text = str(event.get("created_at") or "").strip()
        if not ts_text:
            continue
        try:
            parsed = _parse_utc(ts_text, "run event created_at")
        except CloudBundleError:
            continue
        if parsed > latest:
            latest = parsed

    prior = _as_mapping(prior_observation) if prior_observation else {}
    prior_fingerprint = str(prior.get("fingerprint") or "").strip()
    prior_text = str(prior.get("created_at") or "").strip()
    prior_fresh_text = str(prior.get("source_freshness") or "").strip()
    prior_outcome = str(prior.get("outcome") or "").strip()
    if prior_fingerprint and prior_text and prior_fresh_text:
        try:
            prior_at = _parse_utc(prior_text, "prior observation created_at")
            prior_fresh = _parse_utc(
                prior_fresh_text, "prior observation source_freshness"
            )
        except CloudBundleError:
            return latest.isoformat().replace("+00:00", "Z")
        if fresh < prior_fresh:
            raise CloudBundleError(
                "stale pr_outcome source evidence cannot supersede a newer observation"
            )
        if prior_fingerprint == fingerprint:
            return prior_at.isoformat().replace("+00:00", "Z")
        if fresh == prior_fresh:
            current_rank = _outcome_rank(outcome)
            prior_rank = _outcome_rank(prior_outcome)
            if (
                current_rank is not None
                and prior_rank is not None
                and current_rank < prior_rank
            ):
                raise CloudBundleError(
                    "stale pr_outcome snapshot cannot supersede a newer observation"
                )
        if prior_at + dt.timedelta(seconds=1) > latest:
            latest = prior_at + dt.timedelta(seconds=1)

    return latest.isoformat().replace("+00:00", "Z")


def _lane_for_run_event(event: Mapping[str, Any]) -> str:
    """Return a safe lane/provider identifier for an observed spend attempt."""

    event_type = str(event.get("event_type") or "").strip()
    dimensions = _as_mapping(event.get("dimensions"))
    if event_type == "builder_run":
        return _canonical_lane_identifier(
            dimensions.get("builder_provider")
            or event.get("provider")
            or ""
        )
    if event_type == "reviewer_run":
        return _canonical_lane_identifier(
            dimensions.get("lane")
            or dimensions.get("lane_id")
            or dimensions.get("audit_comment_lane_id")
            or event.get("lens")
            or event.get("provider")
            or ""
        )
    return ""


def _dedupe_sorted_run_events(
    run_events: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return run-attempt events in canonical order for deterministic dedup.

    Only ``builder_run`` and ``reviewer_run`` events are returned, sorted by
    the same canonical serialization used for the evidence digest.  When
    several rows share a source identity with conflicting content, the row
    that sorts first wins regardless of input order, so aggregation and
    fingerprinting resolve duplicate identities identically and input order
    cannot alter totals without altering observation identity.
    """

    typed = [
        event
        for event in run_events
        if str(event.get("event_type") or "").strip()
        in {"builder_run", "reviewer_run"}
    ]
    try:
        typed.sort(
            key=lambda event: json.dumps(
                _run_event_canonical(event), sort_keys=True, allow_nan=False
            )
        )
    except (TypeError, ValueError) as exc:
        raise CloudBundleError(
            "pr_outcome run evidence could not be serialized deterministically"
        ) from exc
    return typed


def _aggregate_run_costs(
    run_events: list[Mapping[str, Any]],
) -> tuple[int, int, float, list[str]]:
    """Deduplicate attempts by event_id and classify cost reporting.

    Returns (expected_attempts, reported_attempts, total_cost_usd,
    missing_lane_sources).  Only ``builder_run`` and ``reviewer_run`` events
    are considered; other event types are ignored.  Missing cost is counted as
    an expected attempt with no reported cost, preserving unknown as unknown.

    Attempts with missing or duplicate source identities (``event_id``, or
    ``dimensions.spend_run_id`` for converted reviewer-spend rows) are never
    silently dropped; they are counted as expected attempts with unknown cost
    and, when the lane can be determined, recorded in ``missing_lane_sources``.
    This keeps them from inflating ``complete`` coverage.  Duplicate
    identities are resolved in canonical order, so the surviving cost is
    independent of input ordering.
    """

    seen: set[str] = set()
    expected = 0
    reported = 0
    total_cost = 0.0
    missing_lanes: list[str] = []

    for event in _dedupe_sorted_run_events(run_events):
        expected += 1
        identity = _run_event_identity(event)
        if not identity or identity in seen:
            lane = _lane_for_run_event(event)
            if lane:
                missing_lanes.append(lane)
            continue
        seen.add(identity)

        metrics = _as_mapping(event.get("metrics"))
        cost = metrics.get("cost_usd")
        if cost is not None:
            if (
                isinstance(cost, bool)
                or not isinstance(cost, int | float)
                or not math.isfinite(cost)
                or cost < 0
            ):
                raise CloudBundleError(
                    "pr_outcome run event cost_usd must be finite and non-negative"
                )
            reported += 1
            total_cost += float(cost)
        else:
            lane = _lane_for_run_event(event)
            if lane:
                missing_lanes.append(lane)

    # Preserve order while removing duplicate lane labels from the diagnostic.
    seen_lanes: set[str] = set()
    unique_missing: list[str] = []
    for lane in missing_lanes:
        if lane not in seen_lanes:
            seen_lanes.add(lane)
            unique_missing.append(lane)

    return expected, reported, total_cost, unique_missing


def build_pr_outcome_event(
    *,
    repo_slug: str,
    pr_number: str,
    outcome: str,
    opened_at: str,
    merged_at: str = "",
    closed_at: str = "",
    reverted_at: str = "",
    run_events: list[Mapping[str, Any]],
    team_id: str = "",
    install_id: str = "",
    source: str = "code-mower cloud pr-outcomes",
    created_at: str = "",
    tool: Mapping[str, Any] | None = None,
    prior_observation: Mapping[str, Any] | None = None,
    evidence_incomplete: bool = False,
) -> dict[str, Any]:
    """Build one metadata-only ``pr_outcome`` event from observed attempts.

    ``run_events`` should be the builder/reviewer run events (or spend rows
    converted to ``reviewer_run`` events) that belong to this PR.  Cost is
    preserved as reported; attempts without reported cost stay missing so
    coverage remains ``unknown`` rather than zero.

    The ``reverted`` outcome is reserved for producers that can demonstrate a
    rollback; it must not be inferred from a GitHub PR-list state alone.

    ``prior_observation`` optionally carries the locally recorded
    ``fingerprint``/``created_at`` of the last emitted observation for this PR
    (see ``pr_outcome_observation_record``).  Supplying it keeps unchanged
    retries idempotent and makes corrected evidence chronologically newer even
    when no source timestamp advanced.

    ``evidence_incomplete`` marks that part of the attempt inventory could
    not be loaded and could not be attributed to a specific PR.  It records
    one additional expected attempt with unknown cost so ``complete``
    coverage can never be emitted while evidence is known to be missing.
    """

    from code_mower import __version__
    from code_mower.providers.provenance import build_code_mower_tool_provenance

    expected, reported, total_cost, missing_sources = _aggregate_run_costs(
        run_events
    )
    if evidence_incomplete:
        expected += 1
        if UNATTRIBUTED_EVIDENCE_LANE not in missing_sources:
            missing_sources.append(UNATTRIBUTED_EVIDENCE_LANE)

    evidence_digest = _run_events_digest(run_events)

    if reported > 0 and reported == expected:
        cost_coverage = "complete"
    elif reported > 0:
        cost_coverage = "partial"
    else:
        cost_coverage = "unknown"

    metrics: dict[str, Any] = {
        "pr_count": 1,
        "cost_reported_run_count": reported,
        "cost_expected_run_count": expected,
        "cost_covered_pr_count": 1 if cost_coverage == "complete" else 0,
    }
    if cost_coverage in ("complete", "partial"):
        metrics["reported_cost_usd"] = round(total_cost, 6)

    dimensions: dict[str, Any] = {
        "pr_outcome_schema": PR_OUTCOME_SCHEMA,
        "pr_number": pr_number,
        "opened_at": opened_at,
        "outcome": outcome,
        "cost_coverage": cost_coverage,
        "pr_outcome_observation_version": evidence_digest,
    }
    if merged_at:
        dimensions["merged_at"] = merged_at
    if closed_at:
        dimensions["closed_at"] = closed_at
    if reverted_at:
        dimensions["reverted_at"] = reverted_at
    if missing_sources:
        dimensions["missing_cost_sources"] = missing_sources
    if evidence_incomplete:
        dimensions["evidence_incomplete"] = True

    fingerprint = _observation_fingerprint(
        outcome=outcome,
        opened_at=opened_at,
        merged_at=merged_at,
        closed_at=closed_at,
        reverted_at=reverted_at,
        evidence_digest=evidence_digest,
        evidence_incomplete=evidence_incomplete,
        missing_sources=missing_sources,
    )
    source_freshness = _source_freshness(created_at)
    created_at_value = _observed_at(
        source_freshness,
        run_events,
        fingerprint=fingerprint,
        outcome=outcome,
        prior_observation=prior_observation,
    )
    event_id_seed = (
        f"code-mower-pr-outcome:{repo_slug}:{pr_number}:{created_at_value}:"
        f"{fingerprint}"
    )
    event: dict[str, Any] = {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, event_id_seed)),
        "event_type": PR_OUTCOME_EVENT_TYPE,
        "created_at": created_at_value,
        "repo_slug": repo_slug,
        "team_id": team_id,
        "install_id": install_id,
        "source": source,
        "provider": "code-mower",
        "lens": "outcome",
        "status": "observed",
        "tool": tool
        if tool is not None
        else build_code_mower_tool_provenance(
            source=source,
            version=__version__,
            role="reporter",
        ),
        "metrics": metrics,
        "dimensions": dimensions,
    }
    event["source_freshness"] = source_freshness
    return event


def pr_outcome_observation_key(repo_slug: str, pr_number: str) -> str:
    """Return the local observation-state key for one repository PR."""

    return f"{repo_slug}#{pr_number}"


def pr_outcome_observation_record(
    event: Mapping[str, Any],
    source_freshness: str = "",
) -> dict[str, str]:
    """Return the local metadata-only state entry for an emitted event."""

    dimensions = _as_mapping(event.get("dimensions"))
    missing_sources = dimensions.get("missing_cost_sources")
    if not isinstance(missing_sources, list):
        missing_sources = []
    fingerprint = _observation_fingerprint(
        outcome=str(dimensions.get("outcome") or ""),
        opened_at=str(dimensions.get("opened_at") or ""),
        merged_at=str(dimensions.get("merged_at") or ""),
        closed_at=str(dimensions.get("closed_at") or ""),
        reverted_at=str(dimensions.get("reverted_at") or ""),
        evidence_digest=str(
            dimensions.get("pr_outcome_observation_version") or ""
        ),
        evidence_incomplete=bool(dimensions.get("evidence_incomplete", False)),
        missing_sources=missing_sources,
    )
    fresh = source_freshness or str(
        event.get("source_freshness") or event.get("created_at") or ""
    )
    return {
        "fingerprint": fingerprint,
        "created_at": str(event.get("created_at") or ""),
        "outcome": str(dimensions.get("outcome") or ""),
        "source_freshness": fresh,
    }


def _malformed_observation_state_error() -> CloudBundleError:
    """Return the bounded, path-free error for corrupt observation state."""

    return CloudBundleError(
        "pr_outcome observation state is malformed; aborting export/upload "
        "rather than discarding recorded ordering state. Remove or repair "
        "the repository observation state file and retry."
    )


def load_pr_outcome_observations(path: Path) -> dict[str, dict[str, str]]:
    """Load local pr_outcome observation state; fail closed on bad data.

    Only a genuinely missing state file is valid initial state.  An
    unreadable or malformed file raises a bounded, path-free
    ``CloudBundleError`` so export/upload aborts instead of silently
    replacing recorded ordering state with an empty mapping -- which would
    let a later correction tie on ``created_at``.
    """

    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise _malformed_observation_state_error() from exc
    except OSError as exc:
        # ``strerror`` carries the OS reason only -- never a path -- so the
        # diagnostic stays metadata-only.
        reason = getattr(exc, "strerror", None) or "read failed"
        raise CloudBundleError(
            "unable to read pr_outcome observation state "
            f"({reason}); aborting export/upload so a later correction "
            "cannot tie on created_at. Restore read access to the "
            "repository state directory and retry."
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _malformed_observation_state_error() from exc
    if not isinstance(payload, Mapping):
        raise _malformed_observation_state_error()
    observations = payload.get("observations")
    if not isinstance(observations, Mapping):
        raise _malformed_observation_state_error()
    result: dict[str, dict[str, str]] = {}
    for key, item in observations.items():
        if not isinstance(item, Mapping):
            raise _malformed_observation_state_error()
        fingerprint = str(item.get("fingerprint") or "").strip()
        created_at = str(item.get("created_at") or "").strip()
        if not fingerprint or not created_at:
            raise _malformed_observation_state_error()
        try:
            _timestamp(created_at, "prior observation created_at")
        except CloudBundleError as exc:
            raise _malformed_observation_state_error() from exc
        source_freshness = str(item.get("source_freshness") or created_at).strip()
        try:
            _timestamp(source_freshness, "prior observation source_freshness")
        except CloudBundleError as exc:
            raise _malformed_observation_state_error() from exc
        outcome = str(item.get("outcome") or "").strip()
        if outcome and outcome not in PR_OUTCOME_VALUES:
            raise _malformed_observation_state_error()
        result[str(key)] = {
            "fingerprint": fingerprint,
            "created_at": created_at,
            "source_freshness": source_freshness,
            "outcome": outcome,
        }
    return result


def save_pr_outcome_observations(
    path: Path,
    observations: Mapping[str, Mapping[str, str]],
) -> None:
    """Atomically persist local pr_outcome observation state."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": PR_OUTCOME_OBSERVATION_STATE_SCHEMA,
        "observations": {
            str(key): {
                "fingerprint": str(item.get("fingerprint") or ""),
                "created_at": str(item.get("created_at") or ""),
                "source_freshness": str(
                    item.get("source_freshness") or item.get("created_at") or ""
                ),
                "outcome": str(item.get("outcome") or ""),
            }
            for key, item in sorted(observations.items())
        },
    }
    tmp_path = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(destination)


def validate_pr_outcome_payload(event: Mapping[str, Any]) -> None:
    """Validate one metadata-only PR outcome and its cost coverage."""

    _required_text(event.get("repo_slug"), "repo_slug")
    dimensions = event.get("dimensions")
    metrics = event.get("metrics")
    if not isinstance(dimensions, Mapping):
        raise CloudBundleError("pr_outcome dimensions must be an object")
    if not isinstance(metrics, Mapping):
        raise CloudBundleError("pr_outcome metrics must be an object")
    if dimensions.get("pr_outcome_schema") != PR_OUTCOME_SCHEMA:
        raise CloudBundleError(
            "pr_outcome dimensions.pr_outcome_schema must be "
            f"{PR_OUTCOME_SCHEMA!r}"
        )
    unknown_dimensions = [str(key) for key in dimensions if key not in PR_OUTCOME_DIMENSIONS]
    if unknown_dimensions:
        raise CloudBundleError(f"unsupported pr_outcome dimension {unknown_dimensions[0]!r}")

    if "evidence_incomplete" in dimensions and not isinstance(
        dimensions["evidence_incomplete"], bool
    ):
        raise CloudBundleError(
            "pr_outcome dimension 'evidence_incomplete' must be a boolean"
        )
    observation_version = dimensions.get("pr_outcome_observation_version")
    if (
        not isinstance(observation_version, str)
        or not observation_version.strip()
    ):
        raise CloudBundleError(
            "pr_outcome dimension 'pr_outcome_observation_version' must be a non-empty string"
        )

    pr_number = _required_text(dimensions.get("pr_number"), "dimension 'pr_number'")
    if not pr_number.isdigit() or int(pr_number) < 1:
        raise CloudBundleError("pr_outcome dimension 'pr_number' must be a positive integer string")

    opened_at = _timestamp(dimensions.get("opened_at"), "dimension 'opened_at'")
    timestamps = {
        key: _timestamp(dimensions[key], f"dimension {key!r}")
        for key in ("merged_at", "closed_at", "reverted_at")
        if dimensions.get(key) not in (None, "")
    }
    for key, value in timestamps.items():
        if value < opened_at:
            raise CloudBundleError(f"pr_outcome dimension {key!r} cannot precede opened_at")

    outcome = _required_text(dimensions.get("outcome"), "dimension 'outcome'")
    if outcome not in PR_OUTCOME_VALUES:
        raise CloudBundleError(f"unsupported pr_outcome outcome {outcome!r}")
    if outcome in {"merged", "reverted"} and "merged_at" not in timestamps:
        raise CloudBundleError(f"pr_outcome outcome {outcome!r} requires merged_at")
    if outcome == "closed_unmerged" and "closed_at" not in timestamps:
        raise CloudBundleError("pr_outcome outcome 'closed_unmerged' requires closed_at")
    if outcome == "reverted" and "reverted_at" not in timestamps:
        raise CloudBundleError("pr_outcome outcome 'reverted' requires reverted_at")
    if outcome == "reverted" and timestamps["reverted_at"] < timestamps["merged_at"]:
        raise CloudBundleError("pr_outcome dimension 'reverted_at' cannot precede merged_at")

    allowed_metrics = set(PR_OUTCOME_COUNT_METRICS + PR_OUTCOME_COST_METRICS)
    unknown_metrics = [str(key) for key in metrics if key not in allowed_metrics]
    if unknown_metrics:
        raise CloudBundleError(f"unsupported pr_outcome metric {unknown_metrics[0]!r}")
    if _count(metrics, "pr_count", required=True) != 1:
        raise CloudBundleError("pr_outcome metric 'pr_count' must equal 1")
    covered_prs = _count(metrics, "cost_covered_pr_count", required=True)
    for field in PR_OUTCOME_COUNT_METRICS:
        _count(metrics, field, required=field in {"pr_count", "cost_covered_pr_count"})
    catches = _count(metrics, "reviewer_catch_count")
    blockers = _count(metrics, "blocking_bug_count")
    if catches is not None and blockers is not None and blockers > catches:
        raise CloudBundleError("pr_outcome blocking_bug_count cannot exceed reviewer_catch_count")

    if "missing_cost_sources" in dimensions:
        missing = dimensions["missing_cost_sources"]
        if not isinstance(missing, list) or not all(
            isinstance(item, str) and item.strip() for item in missing
        ):
            raise CloudBundleError(
                "pr_outcome dimension 'missing_cost_sources' must be a list of non-empty strings"
            )

    coverage = _required_text(dimensions.get("cost_coverage"), "dimension 'cost_coverage'")
    if coverage not in PR_COST_COVERAGE_VALUES:
        raise CloudBundleError(f"unsupported pr_outcome cost_coverage {coverage!r}")
    reported_runs = _count(metrics, "cost_reported_run_count")
    expected_runs = _count(metrics, "cost_expected_run_count")
    if (reported_runs is None) != (expected_runs is None):
        raise CloudBundleError("pr_outcome cost run counts must be provided together")

    cost = metrics.get("reported_cost_usd")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, int | float)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise CloudBundleError("pr_outcome metric 'reported_cost_usd' must be finite and non-negative")
    if coverage == "complete":
        if cost is None or reported_runs is None or reported_runs != expected_runs or covered_prs != 1:
            raise CloudBundleError(
                "complete pr_outcome cost coverage requires reported cost, equal run counts, "
                "and cost_covered_pr_count=1"
            )
    elif coverage == "partial":
        if (
            cost is None
            or reported_runs is None
            or reported_runs < 1
            or reported_runs >= expected_runs
            or covered_prs != 0
        ):
            raise CloudBundleError(
                "partial pr_outcome cost coverage requires reported cost, 0 < reported < expected, "
                "and cost_covered_pr_count=0"
            )
    elif cost is not None or covered_prs != 0 or (reported_runs is not None and reported_runs != 0):
        raise CloudBundleError(
            "unknown pr_outcome cost coverage must omit reported cost and have zero covered/reported counts"
        )
