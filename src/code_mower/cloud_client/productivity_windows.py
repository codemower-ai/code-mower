"""Deterministic normalized productivity windows for CodeMower.com uploads.

Issue #738: the cloud contract accepts ``productivity_summary`` events, but
normal dogfood/repo-sync workflows had no consistent baseline producer
suitable for before/after Code Mower comparisons. This module is that
producer: it converts one metadata-only window observation
(``code_mower.productivityWindow.v1``) into one normalized
``productivity_summary`` event (``code_mower.benchmarkEvent.v1`` with
``dimensions.productivity_schema=code_mower.productivityMetrics.v1``).

Design notes:

- Repository or release scope only. The converter emits
  ``aggregation_subject`` ``repo`` or ``release`` windows; per-PR, per-issue,
  and per-provider scorecard slices remain the job of other producers.
- Separated timings. Elapsed (``cycle_time_seconds``), observed active agent
  time (``active_time_seconds``), queue/wait (``queue_wait_seconds``),
  review (``time_to_first_review_seconds``), time-to-green
  (``time_to_green_seconds``), merge (``time_to_merge_seconds``), and
  owner-wait (``owner_wait_seconds``) are distinct metrics. ``wait_time_seconds``
  is accepted as an explicit aggregate only when the operator supplies it;
  it is never synthesized from other timings.
- Missing stays unavailable. Timings, counts, and defect/revert linkage that
  are not explicitly observed are omitted from the event, never zero-filled.
  Explicit ``active_time_coverage``/``defect_coverage`` dimensions record what
  was observed so incomplete data cannot be presented as complete.
- No causal claims. Every windowed event carries
  ``dimensions.causal_claim=none`` and a ``comparison_basis`` dimension so a
  pre-Code-Mower or operator-selected comparison window reads as correlation
  context, never as proof that Code Mower caused a change.
- Deterministic and idempotent. The event id is a UUIDv5 over the canonical
  window content (never random, never wall-clock), and ``created_at`` is the
  window end. Repeating a sync over the same observation file re-emits the
  same event id and the same bytes.
- Metadata-only. Input and output pass the shared metadata privacy scan, use
  closed dimension/metric vocabularies for windowed events, and must not
  contain source, diffs, prompts, transcripts, issue bodies, raw output,
  local paths, auth output, or secrets.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from code_mower import __version__
from code_mower.providers import build_code_mower_tool_provenance

from .bundle import validate_metadata_payload
from .errors import CloudBundleError


PRODUCTIVITY_WINDOW_INPUT_SCHEMA = "code_mower.productivityWindow.v1"
PRODUCTIVITY_WINDOW_DIMENSION = "code_mower.productivityWindow.v1"
PRODUCTIVITY_WINDOW_EVENT_TYPE = "productivity_summary"

WINDOW_SUBJECTS = ("repo", "release")
COMPARISON_BASIS_VALUES = (
    "code_mower_window",
    "pre_code_mower",
    "operator_selected",
    "unknown",
)
TIMING_PROVENANCE_VALUES = (
    "github_lifecycle",
    "github_lifecycle_and_local_timing",
    "operator_supplied",
    "unknown",
)
COVERAGE_VALUES = ("observed", "unavailable")
CAUSAL_CLAIM_NONE = "none"

#: Timings accepted on a window observation. Each maps 1:1 onto the
#: contracted ``productivity_summary`` time metric of the same name.
#: ``cycle_time_seconds`` is always the ``window_end`` minus ``window_start``
#: span; ``timings.elapsed_seconds`` is not accepted (rejected as an
#: unsupported timing) so operator input can never silently diverge from the
#: emitted elapsed metric.
WINDOW_TIMING_FIELDS = (
    "active_seconds",
    "queue_wait_seconds",
    "wait_time_seconds",
    "time_to_first_review_seconds",
    "time_to_green_seconds",
    "time_to_merge_seconds",
    "owner_wait_seconds",
)

TIMING_TO_METRIC = {
    "active_seconds": "active_time_seconds",
    "queue_wait_seconds": "queue_wait_seconds",
    "wait_time_seconds": "wait_time_seconds",
    "time_to_first_review_seconds": "time_to_first_review_seconds",
    "time_to_green_seconds": "time_to_green_seconds",
    "time_to_merge_seconds": "time_to_merge_seconds",
    "owner_wait_seconds": "owner_wait_seconds",
}

#: Counts accepted on a window observation. Each maps onto the contracted
#: ``productivity_summary`` count metric of the same name and is included
#: only when explicitly observed in the input.
WINDOW_COUNT_FIELDS = (
    "merged_pr_count",
    "abandoned_pr_count",
    "reverted_pr_count",
    "fix_round_count",
    "owner_intervention_count",
    "post_merge_defect_count",
)

WINDOW_OPTIONAL_TEXT_FIELDS = (
    "aggregation_key",
    "release",
    "pilot_posture",
    "event_source",
)

#: Closed dimension vocabulary for normalized window events: the required
#: productivity dimensions, the small metadata-only optional dimensions the
#: contract already permits on repo/release windows, and the explicit
#: window/coverage/provenance markers this producer adds. Undeclared fields
#: are rejected rather than becoming accidental prose channels.
WINDOW_ALLOWED_DIMENSIONS = frozenset(
    {
        "productivity_schema",
        "repo_slug",
        "window_start",
        "window_end",
        "window_granularity",
        "aggregation_subject",
        "aggregation_key",
        "release",
        "pilot_posture",
        "event_source",
        "productivity_window_schema",
        "comparison_basis",
        "timing_provenance",
        "active_time_coverage",
        "defect_coverage",
        "causal_claim",
    }
)

_PATH_LIKE_PATTERN = re.compile(
    r"/(home|Users|tmp|var|etc|root|code-mower)/|^[A-Za-z]:\\|~/|\.\.[/\\]"
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudBundleError(f"productivity_window {field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise CloudBundleError(f"productivity_window {field} must be a string")
    return value.strip()


def _single_line(value: str, field: str) -> str:
    if "\n" in value or "\r" in value:
        raise CloudBundleError(
            f"productivity_window {field} must be single-line metadata, not prose or output"
        )
    if _PATH_LIKE_PATTERN.search(value):
        raise CloudBundleError(
            f"productivity_window {field} must not contain local paths"
        )
    return value


def _timestamp(value: object, field: str) -> dt.datetime:
    text = _required_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudBundleError(
            f"productivity_window {field} must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CloudBundleError(f"productivity_window {field} must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def _finite_seconds(value: object, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CloudBundleError(
            f"productivity_window timing {field!r} must be numeric when observed"
        )
    if not math.isfinite(value) or value < 0:
        raise CloudBundleError(
            f"productivity_window timing {field!r} must be finite and non-negative"
        )
    return value


def _observed_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CloudBundleError(
            f"productivity_window count {field!r} must be a non-negative integer when observed"
        )
    return value


def productivity_window_event_id(window: Mapping[str, Any]) -> str:
    """Return the deterministic event id for a normalized window.

    The id is a UUIDv5 over the canonical window content only (identity,
    timings, counts, coverage, and provenance). Envelope context such as
    source, team, install, and tool version is intentionally excluded so the
    same observation re-emitted through another route or sync keeps one id.
    """

    canonical = json.dumps(window, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"productivity-window:{canonical}"))


def _window_identity(window: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "repo_slug": window["repo_slug"],
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "window_granularity": window["window_granularity"],
        "aggregation_subject": window["aggregation_subject"],
        "aggregation_key": window.get("aggregation_key", ""),
        "release": window.get("release", ""),
        "pilot_posture": window.get("pilot_posture", ""),
        "event_source": window.get("event_source", ""),
        "comparison_basis": window.get("comparison_basis", "unknown"),
        "timing_provenance": window.get("timing_provenance", "unknown"),
        "metrics": window.get("metrics", {}),
        "active_time_coverage": window.get("active_time_coverage", "unavailable"),
        "defect_coverage": window.get("defect_coverage", "unavailable"),
    }


def normalize_window_observation(
    value: Mapping[str, Any],
    *,
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Normalize one window observation into canonical window content.

    Missing timings, counts, and defect/revert linkage stay omitted, never
    zero-filled. Explicit coverage dimensions record what was observed.
    """

    if not isinstance(value, Mapping):
        raise CloudBundleError("productivity_window observation must be a JSON object")
    validate_metadata_payload(value)
    if value.get("schema") != PRODUCTIVITY_WINDOW_INPUT_SCHEMA:
        raise CloudBundleError(
            f"unsupported productivity_window schema {value.get('schema')!r}; "
            f"expected {PRODUCTIVITY_WINDOW_INPUT_SCHEMA!r}"
        )

    unknown_top_level = [
        str(key)
        for key in value
        if key
        not in {
            "schema",
            "repo_slug",
            "window_start",
            "window_end",
            "window_granularity",
            "aggregation_subject",
            "aggregation_key",
            "release",
            "comparison_basis",
            "timing_provenance",
            "pilot_posture",
            "event_source",
            "timings",
            "counts",
        }
    ]
    if unknown_top_level:
        raise CloudBundleError(
            f"unsupported productivity_window field {unknown_top_level[0]!r}"
        )

    resolved_repo = _optional_text(value.get("repo_slug"), "'repo_slug'") or repo_slug.strip()
    if not resolved_repo:
        raise CloudBundleError(
            "productivity_window 'repo_slug' must be a non-empty string "
            "(observation or sync context)"
        )
    _single_line(resolved_repo, "'repo_slug'")
    if "/" not in resolved_repo:
        raise CloudBundleError(
            "productivity_window 'repo_slug' must look like OWNER/REPO"
        )

    start = _timestamp(value.get("window_start"), "'window_start'")
    end = _timestamp(value.get("window_end"), "'window_end'")
    if end <= start:
        raise CloudBundleError("productivity_window 'window_end' must be after 'window_start'")

    granularity = _required_text(value.get("window_granularity"), "'window_granularity'")
    if granularity not in ("cycle", "day", "week", "release", "custom"):
        raise CloudBundleError(
            f"unsupported productivity_window granularity {granularity!r}; "
            "allowed: cycle, day, week, release, custom"
        )
    subject = _required_text(value.get("aggregation_subject"), "'aggregation_subject'")
    if subject not in WINDOW_SUBJECTS:
        raise CloudBundleError(
            f"unsupported productivity_window aggregation_subject {subject!r}; "
            "this producer emits repository or release windows only"
        )

    optionals: dict[str, str] = {}
    for field in WINDOW_OPTIONAL_TEXT_FIELDS:
        text = _single_line(_optional_text(value.get(field), repr(field)), repr(field))
        if text:
            optionals[field] = text
    if subject == "release" and not optionals.get("release"):
        raise CloudBundleError(
            "productivity_window release windows require the 'release' field"
        )

    comparison_basis = (
        _optional_text(value.get("comparison_basis"), "'comparison_basis'") or "unknown"
    )
    if comparison_basis not in COMPARISON_BASIS_VALUES:
        raise CloudBundleError(
            f"unsupported productivity_window comparison_basis {comparison_basis!r}; "
            f"allowed: {', '.join(COMPARISON_BASIS_VALUES)}"
        )
    timing_provenance = (
        _optional_text(value.get("timing_provenance"), "'timing_provenance'") or "unknown"
    )
    if timing_provenance not in TIMING_PROVENANCE_VALUES:
        raise CloudBundleError(
            f"unsupported productivity_window timing_provenance {timing_provenance!r}; "
            f"allowed: {', '.join(TIMING_PROVENANCE_VALUES)}"
        )

    timings = value.get("timings", {})
    if timings in (None, ""):
        timings = {}
    if not isinstance(timings, Mapping):
        raise CloudBundleError("productivity_window 'timings' must be an object")
    unknown_timings = [str(key) for key in timings if key not in WINDOW_TIMING_FIELDS]
    if unknown_timings:
        raise CloudBundleError(
            f"unsupported productivity_window timing {unknown_timings[0]!r}"
        )
    metrics: dict[str, float | int] = {
        "cycle_time_seconds": (end - start).total_seconds()
    }
    active_observed = False
    for field in WINDOW_TIMING_FIELDS:
        if timings.get(field) in (None, ""):
            continue
        number = _finite_seconds(timings.get(field), field)
        metrics[TIMING_TO_METRIC[field]] = number
        if field == "active_seconds":
            active_observed = True
    for metric, number in metrics.items():
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise CloudBundleError(  # pragma: no cover - guarded above
                f"productivity_window metric {metric!r} must be numeric"
            )
        if not math.isfinite(number) or number < 0:
            raise CloudBundleError(  # pragma: no cover - guarded above
                f"productivity_window metric {metric!r} must be finite and non-negative"
            )

    counts = value.get("counts", {})
    if counts in (None, ""):
        counts = {}
    if not isinstance(counts, Mapping):
        raise CloudBundleError("productivity_window 'counts' must be an object")
    unknown_counts = [str(key) for key in counts if key not in WINDOW_COUNT_FIELDS]
    if unknown_counts:
        raise CloudBundleError(
            f"unsupported productivity_window count {unknown_counts[0]!r}"
        )
    for field in WINDOW_COUNT_FIELDS:
        if counts.get(field) in (None, ""):
            continue
        metrics[field] = _observed_count(counts.get(field), field)

    defect_observed = any(
        field in metrics for field in ("post_merge_defect_count", "reverted_pr_count")
    )

    window: dict[str, Any] = {
        "repo_slug": resolved_repo,
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end": end.isoformat().replace("+00:00", "Z"),
        "window_granularity": granularity,
        "aggregation_subject": subject,
        **optionals,
        "comparison_basis": comparison_basis,
        "timing_provenance": timing_provenance,
        "metrics": metrics,
        "active_time_coverage": "observed" if active_observed else "unavailable",
        "defect_coverage": "observed" if defect_observed else "unavailable",
        "team_id": team_id.strip(),
        "install_id": install_id.strip(),
        "source": source.strip(),
    }
    return window


def productivity_window_to_event(
    value: Mapping[str, Any],
    *,
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Convert one window observation into a normalized event.

    The converter is deterministic: the same observation always yields the
    same event id, timestamps, dimensions, and metrics, so repeated syncs
    stay idempotent. Late-arriving local timing only changes the event when
    the observation content itself changes.
    """

    from .events import EVENT_SCHEMA, validate_cloud_event

    window = normalize_window_observation(
        value,
        repo_slug=repo_slug,
        team_id=team_id,
        install_id=install_id,
        source=source,
    )
    metrics = dict(window["metrics"])
    dimensions: dict[str, Any] = {
        "productivity_schema": "code_mower.productivityMetrics.v1",
        "productivity_window_schema": PRODUCTIVITY_WINDOW_DIMENSION,
        "repo_slug": window["repo_slug"],
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "window_granularity": window["window_granularity"],
        "aggregation_subject": window["aggregation_subject"],
        "comparison_basis": window["comparison_basis"],
        "timing_provenance": window["timing_provenance"],
        "active_time_coverage": window["active_time_coverage"],
        "defect_coverage": window["defect_coverage"],
        "causal_claim": CAUSAL_CLAIM_NONE,
    }
    for field in WINDOW_OPTIONAL_TEXT_FIELDS:
        if window.get(field):
            dimensions[field] = window[field]
    event_source = source.strip() or "productivity-window"
    event = {
        "schema": EVENT_SCHEMA,
        "event_id": productivity_window_event_id(_window_identity(window)),
        "event_type": PRODUCTIVITY_WINDOW_EVENT_TYPE,
        "created_at": window["window_end"],
        "repo_slug": window["repo_slug"],
        "team_id": window["team_id"],
        "install_id": window["install_id"],
        "source": window["source"] or event_source,
        "provider": "code-mower",
        "lens": "productivity",
        "status": "observed",
        "tool": build_code_mower_tool_provenance(
            source=window["source"] or event_source,
            version=__version__,
            role="reporter",
        ),
        "metrics": metrics,
        "dimensions": dimensions,
    }
    validate_metadata_payload(event)
    return validate_cloud_event(event)


def productivity_window_event_from_dict(
    value: Mapping[str, Any],
    event_type: str,
    *,
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
    source: str = "",
) -> dict[str, Any] | None:
    """Convert a window observation dict when loading ``productivity_summary`` events."""

    if event_type != PRODUCTIVITY_WINDOW_EVENT_TYPE:
        return None
    if not isinstance(value, Mapping):
        return None
    if value.get("schema") != PRODUCTIVITY_WINDOW_INPUT_SCHEMA:
        return None
    return productivity_window_to_event(
        value,
        repo_slug=repo_slug,
        team_id=team_id,
        install_id=install_id,
        source=source,
    )


def _parsed_window_candidates(text: str, path: Path) -> list[Any]:
    """Parse raw window-file candidates via the shared event-file parser.

    Thin wrapper over :func:`events.parse_event_file_candidates` so JSON/JSONL
    handling cannot drift; productivity-window-specific candidate conversion
    and safe diagnostics stay in :func:`load_productivity_window_events`.
    """

    from .events import parse_event_file_candidates

    return parse_event_file_candidates(text, path)


def load_productivity_window_events(
    path: Path,
    event_type: str,
    *,
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
    source: str = "",
) -> list[dict[str, Any]]:
    """Load window observations (and raw events) from a JSON/JSONL file.

    Window observations convert deterministically with sync-supplied repo
    context; recognized local artifacts (builder-run authoring runs and
    adoption results, the same chain cloud dogfood converts) convert via
    the shared artifact loader; entries that are already normalized events
    pass through the standard event normalizer unchanged.
    """

    from .events import artifact_event_from_dict, normalize_event, safe_event_type

    safe_event_type(event_type)
    resolved = path.expanduser()
    if not resolved.is_file():
        raise CloudBundleError(f"event file does not exist or is not a file: {resolved}")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CloudBundleError(f"event file is not UTF-8 text: {resolved}") from exc
    except OSError as exc:
        raise CloudBundleError(f"unable to read event file {resolved}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for item in _parsed_window_candidates(text, resolved):
        if not isinstance(item, Mapping):
            raise CloudBundleError(f"event file contains a non-object event: {resolved}")
        converted = productivity_window_event_from_dict(
            item,
            event_type,
            repo_slug=repo_slug,
            team_id=team_id,
            install_id=install_id,
            source=source,
        ) or artifact_event_from_dict(item, event_type)
        if converted is not None:
            events.append(converted)
        else:
            events.append(normalize_event(dict(item), event_type))
    return events


def is_normalized_productivity_window_event(event: Mapping[str, Any]) -> bool:
    """Return whether an event is a normalized window carrying the marker.

    Only events with ``dimensions.productivity_window_schema`` equal to the
    producer stamp count as ``productivity_baseline`` coverage; legacy or
    third-party ``productivity_summary`` events without the stamp do not,
    even though they share the event type.
    """

    dimensions = event.get("dimensions")
    return (
        isinstance(dimensions, Mapping)
        and dimensions.get("productivity_window_schema") == PRODUCTIVITY_WINDOW_DIMENSION
    )


def count_normalized_productivity_window_events(events: list[Mapping[str, Any]]) -> int:
    """Count normalized windows in an event list (marker carriers only)."""

    return sum(1 for event in events if is_normalized_productivity_window_event(event))


def validate_productivity_window_event(event: Mapping[str, Any]) -> None:
    """Validate the stricter normalized-window contract.

    Runs after the standard ``productivity_summary`` validation: windowed
    events (those carrying ``productivity_window_schema``) must use a closed
    dimension vocabulary, repo/release subjects, an explicit no-causality
    marker, and coverage dimensions that agree with the emitted metrics.
    Events without the window stamp (including all uploads before this
    producer) keep the permissive historical validation unchanged.
    """

    dimensions = event.get("dimensions")
    metrics = event.get("metrics")
    if not isinstance(dimensions, Mapping) or not isinstance(metrics, Mapping):
        raise CloudBundleError("productivity_window event dimensions/metrics must be objects")
    if dimensions.get("productivity_window_schema") != PRODUCTIVITY_WINDOW_DIMENSION:
        return
    unknown_dimensions = [
        str(key) for key in dimensions if key not in WINDOW_ALLOWED_DIMENSIONS
    ]
    if unknown_dimensions:
        raise CloudBundleError(
            f"unsupported productivity_window dimension {unknown_dimensions[0]!r}"
        )
    subject = dimensions.get("aggregation_subject")
    if subject not in WINDOW_SUBJECTS:
        raise CloudBundleError(
            f"unsupported productivity_window aggregation_subject {subject!r}"
        )
    if subject == "release" and not str(dimensions.get("release") or "").strip():
        raise CloudBundleError("productivity_window release windows require 'release'")
    for scoped in ("pr_number", "issue_number", "branch"):
        if str(dimensions.get(scoped) or "").strip():
            raise CloudBundleError(
                f"productivity_window {subject} windows must not carry {scoped!r}"
            )
    if dimensions.get("comparison_basis") not in COMPARISON_BASIS_VALUES:
        raise CloudBundleError(
            f"unsupported productivity_window comparison_basis "
            f"{dimensions.get('comparison_basis')!r}"
        )
    if dimensions.get("timing_provenance") not in TIMING_PROVENANCE_VALUES:
        raise CloudBundleError(
            f"unsupported productivity_window timing_provenance "
            f"{dimensions.get('timing_provenance')!r}"
        )
    if dimensions.get("causal_claim") != CAUSAL_CLAIM_NONE:
        raise CloudBundleError(
            "productivity_window events must carry causal_claim 'none'; "
            "before/after comparisons are correlation context, not causal proof"
        )
    active_coverage = dimensions.get("active_time_coverage")
    if active_coverage not in COVERAGE_VALUES:
        raise CloudBundleError(
            f"unsupported productivity_window active_time_coverage {active_coverage!r}"
        )
    defect_coverage = dimensions.get("defect_coverage")
    if defect_coverage not in COVERAGE_VALUES:
        raise CloudBundleError(
            f"unsupported productivity_window defect_coverage {defect_coverage!r}"
        )
    if ("active_time_seconds" in metrics) != (active_coverage == "observed"):
        raise CloudBundleError(
            "productivity_window active_time_coverage must be 'observed' "
            "exactly when active_time_seconds is emitted"
        )
    defect_emitted = any(
        key in metrics for key in ("post_merge_defect_count", "reverted_pr_count")
    )
    if defect_emitted != (defect_coverage == "observed"):
        raise CloudBundleError(
            "productivity_window defect_coverage must be 'observed' "
            "exactly when defect or revert linkage is emitted"
        )
    if "cycle_time_seconds" not in metrics:
        raise CloudBundleError(
            "productivity_window events must include elapsed cycle_time_seconds"
        )
    window_start = _timestamp(dimensions.get("window_start"), "'window_start'")
    window_end = _timestamp(dimensions.get("window_end"), "'window_end'")
    if window_end <= window_start:
        raise CloudBundleError(
            "productivity_window 'window_end' must be after 'window_start'"
        )
    cycle = metrics.get("cycle_time_seconds")
    if isinstance(cycle, bool) or not isinstance(cycle, int | float):
        raise CloudBundleError(
            "productivity_window metric 'cycle_time_seconds' must be numeric"
        )
    if not math.isfinite(cycle) or cycle < 0:
        raise CloudBundleError(
            "productivity_window metric 'cycle_time_seconds' must be finite "
            "and non-negative"
        )
    expected_span = (window_end - window_start).total_seconds()
    if cycle != expected_span:
        raise CloudBundleError(
            f"productivity_window cycle_time_seconds {cycle!r} must match "
            f"window span {expected_span} seconds "
            "('window_end' minus 'window_start')"
        )
