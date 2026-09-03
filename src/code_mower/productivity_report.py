#!/usr/bin/env python3
"""Local productivity and effectiveness reporting for Code Mower."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import board_store
from . import lane_status
from . import reviewer_spend
from .cloud_client.errors import CloudBundleError
from .cloud_client.events import validate_cloud_event
from .cloud_client.productivity import (
    PRODUCTIVITY_COUNT_METRICS,
    PRODUCTIVITY_EVENT_TYPE,
    PRODUCTIVITY_TOKEN_METRICS,
)


PRODUCTIVITY_REPORT_SCHEMA = "code_mower.productivityReport.v1"
PRODUCTIVITY_BOARD_SCHEMA = "code_mower.boardProductivity.v1"
ACTIVE_AGENT_STATES = {"active", "in_progress", "queued", "running", "working"}
PRODUCTIVITY_DIMENSION_KEYS = (
    "window_start",
    "window_end",
    "window_granularity",
    "aggregation_subject",
    "aggregation_key",
    "release",
    "provider",
    "role",
    "issue_number",
    "pr_number",
    "verdict",
    "pilot_posture",
    "event_source",
)
PRODUCTIVITY_ADDITIVE_METRICS = {
    *PRODUCTIVITY_COUNT_METRICS,
    *PRODUCTIVITY_TOKEN_METRICS,
    "cost_usd",
}
PRODUCTIVITY_PROVIDER_SUBJECTS = {"builder", "lane", "provider", "reviewer"}
PRODUCTIVITY_HEADLINE_SUBJECT_PRIORITY = ("repo", "release", "issue", "pr")
PRODUCTIVITY_SCORECARDS_SCHEMA = "code_mower.providerScorecards.v1"
PRODUCTIVITY_SCORECARD_SCHEMA = "code_mower.providerScorecard.v1"
PROMOTION_POLICY_PATH = "docs/lane-promotion-policy.md"
INFRA_VERDICTS = {
    "CANCELLED",
    "ERROR",
    "INFRA_ERROR",
    "PROVIDER_UNAVAILABLE",
    "TIMEOUT",
    "UNKNOWN",
}
SCORECARD_COUNT_METRICS = (
    "builder_run_count",
    "reviewer_run_count",
    "audit_pass_count",
    "audit_blocked_count",
    "reviewer_catch_count",
    "blocking_bug_count",
    "blocked_finding_count",
    "false_blocker_count",
    "missed_blocker_count",
    "fix_round_count",
    "checks_failed_count",
)
PUBLIC_SPEND_LANE_KEYS = (
    "lane",
    "provider",
    "role",
    "reviewer_run_count",
    "audit_pass_count",
    "audit_blocked_count",
    "infra_failure_count",
    "wall_seconds",
    "cost_usd",
    "total_tokens",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        text = value.strip().replace("$", "")
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _int(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _duration_seconds(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds())


def _known_number(value: object) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    if isinstance(number, float) and not number.is_integer():
        return f"{number:.3f}".rstrip("0").rstrip(".")
    return str(int(number))


def _known_seconds(value: object) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    if number >= 3600:
        return f"{number / 3600:.1f}h"
    if number >= 60:
        return f"{number / 60:.1f}m"
    return f"{number:.1f}s"


def _known_money(value: object) -> str:
    number = _number(value)
    return "unknown" if number is None else f"${float(number):.3f}"


def _event_candidates(parsed: object) -> Iterable[Mapping[str, Any]]:
    if isinstance(parsed, Mapping):
        for key in ("productivity_summary_events", "events"):
            value = parsed.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping):
                        yield item
                return
        yield parsed
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, Mapping):
                yield item


def _parse_json_or_jsonl(text: str) -> object:
    if not text.strip():
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows: list[object] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows


def _load_productivity_events(paths: Sequence[str | Path]) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        display_name = path.name or "event file"
        try:
            parsed = _parse_json_or_jsonl(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            warnings.append(f"{display_name}: event file not found")
            continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            warnings.append(f"{display_name}: could not read productivity event file")
            continue
        for candidate in _event_candidates(parsed):
            if str(candidate.get("event_type") or "") != PRODUCTIVITY_EVENT_TYPE:
                continue
            try:
                events.append(validate_cloud_event(dict(candidate)))
            except CloudBundleError:
                warnings.append(f"{display_name}: skipped invalid productivity_summary event")
    events.sort(key=lambda event: str(event.get("created_at") or ""))
    return events, warnings


def _label_verdicts(pr: Mapping[str, Any]) -> list[tuple[str, str]]:
    labels = pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}
    verdicts: list[tuple[str, str]] = []
    for label in labels.get("done") or []:
        if isinstance(label, str) and label.endswith("-done"):
            verdicts.append((label[: -len("-done")], "PASS"))
    for label in labels.get("blocked") or []:
        if isinstance(label, str) and label.endswith("-blocked"):
            verdicts.append((label[: -len("-blocked")], "BLOCKED"))
    return verdicts


def _dimension_text(event: Mapping[str, Any], key: str) -> str:
    dimensions = event.get("dimensions") if isinstance(event.get("dimensions"), Mapping) else {}
    value = dimensions.get(key)
    return str(value).strip() if value not in (None, "") else ""


def _aggregation_subject(event: Mapping[str, Any]) -> str:
    return _dimension_text(event, "aggregation_subject") or "repo"


def _is_provider_subject(event: Mapping[str, Any]) -> bool:
    return _aggregation_subject(event) in PRODUCTIVITY_PROVIDER_SUBJECTS


def _headline_summary_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    candidates = [event for event in events if not _is_provider_subject(event)]
    for subject in PRODUCTIVITY_HEADLINE_SUBJECT_PRIORITY:
        matches = [event for event in candidates if _aggregation_subject(event) == subject]
        if matches:
            return matches
    return candidates


def _provider_from_event(event: Mapping[str, Any]) -> str:
    subject = _aggregation_subject(event)
    provider = (
        _dimension_text(event, "provider")
        or _dimension_text(event, "builder_provider")
        or _dimension_text(event, "reviewer_provider")
    )
    if not provider and subject in PRODUCTIVITY_PROVIDER_SUBJECTS:
        provider = _dimension_text(event, "aggregation_key")
    if subject == "lane" and provider:
        provider = reviewer_spend.provider_from_lane(provider)
    return provider or str(event.get("provider") or "").strip() or "unknown"


def _role_from_event(event: Mapping[str, Any]) -> str:
    subject = _aggregation_subject(event)
    role = _dimension_text(event, "role")
    if role:
        return role
    if subject in {"builder", "reviewer"}:
        return subject
    if subject == "lane":
        return "reviewer"
    return "unknown"


def _pull_requests(status: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    remote = status.get("remote") if isinstance(status.get("remote"), Mapping) else {}
    prs = remote.get("pull_requests") if isinstance(remote.get("pull_requests"), list) else []
    return [pr for pr in prs if isinstance(pr, Mapping)]


def _current_state(status: Mapping[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {
            "remote_available": False,
            "open_pr_count": None,
            "blocked_pr_count": None,
            "stale_pr_count": None,
            "owner_action_count": None,
            "active_lane_count": None,
            "gate_alert_count": None,
        }
    remote = status.get("remote") if isinstance(status.get("remote"), Mapping) else {}
    prs = _pull_requests(status)
    owner_queue = status.get("owner_queue") if isinstance(status.get("owner_queue"), Mapping) else {}
    gate_health = remote.get("gate_health") if isinstance(remote.get("gate_health"), Mapping) else {}
    gate_alerts = gate_health.get("alerts") if isinstance(gate_health.get("alerts"), list) else []
    adapters = status.get("agent_adapters") if isinstance(status.get("agent_adapters"), Mapping) else {}
    agent_cards = adapters.get("agents") if isinstance(adapters.get("agents"), list) else []
    supervised = status.get("supervised_pilot") if isinstance(status.get("supervised_pilot"), Mapping) else {}
    queue = supervised.get("queue") if isinstance(supervised.get("queue"), Mapping) else {}
    supervised_metrics = queue.get("metrics") if isinstance(queue.get("metrics"), Mapping) else {}
    active_from_supervised = _int(supervised_metrics.get("active_lane_count"))
    active_from_cards = sum(
        1
        for card in agent_cards
        if isinstance(card, Mapping) and str(card.get("status") or "").lower() in ACTIVE_AGENT_STATES
    )
    blocked_pr_count = sum(
        1
        for pr in prs
        if (pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}).get("blocked")
    )
    stale_pr_count = sum(1 for pr in prs if pr.get("stale"))
    owner_action_count = _int(owner_queue.get("count"))
    if owner_action_count is None:
        owner_action_count = sum(
            1
            for pr in prs
            if "needs-owner"
            in ((pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}).get("needs") or [])
        )
    return {
        "remote_available": bool(remote.get("available")),
        "open_pr_count": len(prs) if remote.get("available") else None,
        "blocked_pr_count": blocked_pr_count if remote.get("available") else None,
        "stale_pr_count": stale_pr_count if remote.get("available") else None,
        "owner_action_count": owner_action_count if remote.get("available") else None,
        "active_lane_count": active_from_supervised
        if active_from_supervised is not None
        else active_from_cards,
        "gate_alert_count": len(gate_alerts) if remote.get("available") else None,
    }


def _board_observations(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    timestamps = [_parse_timestamp(event.get("created_at")) for event in events]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    first = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    verdicts_by_key: dict[tuple[int, str, str], list[str]] = defaultdict(list)
    for event in sorted(events, key=lambda item: str(item.get("created_at") or "")):
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), Mapping) else {}
        for pr in _pull_requests(snapshot):
            pr_number = _int(pr.get("number")) or 0
            head_sha_prefix = str(pr.get("head_sha") or "")[:12]
            for lane, verdict in _label_verdicts(pr):
                key = (pr_number, lane, head_sha_prefix)
                if not verdicts_by_key[key] or verdicts_by_key[key][-1] != verdict:
                    verdicts_by_key[key].append(verdict)
    pass_count = sum(1 for history in verdicts_by_key.values() if "PASS" in history)
    blocked_count = sum(1 for history in verdicts_by_key.values() if "BLOCKED" in history)
    fix_rounds = 0
    for history in verdicts_by_key.values():
        blocked_seen = False
        for verdict in history:
            if verdict == "BLOCKED":
                blocked_seen = True
            elif verdict == "PASS" and blocked_seen:
                fix_rounds += 1
                blocked_seen = False
    return {
        "snapshot_count": len(events),
        "window_start": _timestamp(first) if first else "",
        "window_end": _timestamp(last) if last else "",
        "duration_seconds": _duration_seconds(first, last),
        "audit_pass_count": pass_count,
        "audit_blocked_count": blocked_count,
        "blocked_finding_count": blocked_count,
        "reviewer_catch_count": blocked_count,
        "fix_round_count": fix_rounds,
    }


def _spend_summary(repo: str, path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        payload = reviewer_spend.load_spend_file(path)
    except ValueError:
        return (
            {
                "available": False,
                "run_count": 0,
                "filtered_rows": 0,
                "skipped_rows": 0,
                "by_lane": [],
            },
            ["could not read reviewer spend file"],
        )
    available = path.expanduser().is_file()
    by_lane: dict[str, dict[str, Any]] = {}
    run_count = 0
    pass_count = 0
    blocked_count = 0
    infra_failure_count = 0
    filtered_rows = 0
    wall_seconds = 0.0
    cost_usd = 0.0
    total_tokens = 0
    token_seen = False
    cost_seen = False
    wall_seen = False
    for run in reviewer_spend.spend_runs(payload):
        run_repo = str(run.get("repo") or "").strip()
        if run_repo and run_repo != repo:
            filtered_rows += 1
            continue
        lane = str(run.get("lane") or "").strip() or "unknown"
        provider = reviewer_spend.provider_from_lane(lane)
        verdict = str(run.get("verdict") or "").strip().upper()
        run_count += 1
        if verdict == "PASS":
            pass_count += 1
        elif verdict == "BLOCKED":
            blocked_count += 1
        elif verdict in INFRA_VERDICTS:
            infra_failure_count += 1
        lane_group = by_lane.setdefault(
            lane,
            {
                "lane": lane,
                "provider": provider,
                "role": "reviewer",
                "reviewer_run_count": 0,
                "audit_pass_count": 0,
                "audit_blocked_count": 0,
                "infra_failure_count": 0,
                "wall_seconds": 0.0,
                "cost_usd": 0.0,
                "total_tokens": 0,
                "wall_reported": False,
                "cost_reported": False,
                "token_reported": False,
            },
        )
        lane_group["reviewer_run_count"] += 1
        if verdict == "PASS":
            lane_group["audit_pass_count"] += 1
        elif verdict == "BLOCKED":
            lane_group["audit_blocked_count"] += 1
        elif verdict in INFRA_VERDICTS:
            lane_group["infra_failure_count"] += 1
        if (value := _number(run.get("wall_seconds"))) is not None:
            wall_seen = True
            wall_seconds += float(value)
            lane_group["wall_seconds"] += float(value)
            lane_group["wall_reported"] = True
        if (value := _number(run.get("cost_usd"))) is not None:
            cost_seen = True
            cost_usd += float(value)
            lane_group["cost_usd"] += float(value)
            lane_group["cost_reported"] = True
        if (value := _int(run.get("total_tokens"))) is not None:
            token_seen = True
            total_tokens += value
            lane_group["total_tokens"] += value
            lane_group["token_reported"] = True
    normalized_lanes = []
    for group in by_lane.values():
        group["wall_seconds"] = round(group["wall_seconds"], 3)
        group["cost_usd"] = round(group["cost_usd"], 6)
        normalized_lanes.append(group)
    return (
        {
            "available": available,
            "run_count": run_count,
            "filtered_rows": filtered_rows,
            "skipped_rows": 0,
            "by_lane": sorted(normalized_lanes, key=lambda item: item["lane"]),
            "wall_seconds": round(wall_seconds, 3) if wall_seen else None,
            "cost_usd": round(cost_usd, 6) if cost_seen else None,
            "total_tokens": total_tokens if token_seen else None,
            "audit_pass_count": pass_count,
            "audit_blocked_count": blocked_count,
            "infra_failure_count": infra_failure_count,
        },
        warnings,
    )


def _cloud_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary_events = _headline_summary_events(events)
    latest = summary_events[-1] if summary_events else {}
    latest_dimensions = latest.get("dimensions") if isinstance(latest.get("dimensions"), Mapping) else {}
    latest_metrics = latest.get("metrics") if isinstance(latest.get("metrics"), Mapping) else {}
    totals: dict[str, float | int] = {}
    for event in summary_events:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), Mapping) else {}
        for key, value in metrics.items():
            if str(key) not in PRODUCTIVITY_ADDITIVE_METRICS:
                continue
            number = _number(value)
            if number is None:
                continue
            totals[str(key)] = totals.get(str(key), 0) + number
    for key, value in list(totals.items()):
        if isinstance(value, float):
            totals[key] = round(value, 6)
    return {
        "available": bool(events),
        "event_count": len(events),
        "summary_event_count": len(summary_events),
        "latest_created_at": str(latest.get("created_at") or ""),
        "latest_dimensions": {
            key: str(latest_dimensions.get(key) or "")
            for key in PRODUCTIVITY_DIMENSION_KEYS
            if latest_dimensions.get(key) not in (None, "")
        },
        "latest_metrics": dict(latest_metrics),
        "provided_metric_totals": totals,
    }


def _empty_scorecard(provider: str, role: str) -> dict[str, Any]:
    return {
        "schema": PRODUCTIVITY_SCORECARD_SCHEMA,
        "provider": provider,
        "role": role,
        "lane_ids": [],
        "sources": {
            "reviewer_spend_runs": 0,
            "cloud_events": 0,
        },
        "metrics": {
            "builder_run_count": 0,
            "reviewer_run_count": 0,
            "audit_pass_count": 0,
            "audit_blocked_count": 0,
            "reviewer_catch_count": 0,
            "blocking_bug_count": 0,
            "blocked_finding_count": 0,
            "false_blocker_count": 0,
            "missed_blocker_count": 0,
            "fix_round_count": 0,
            "checks_failed_count": 0,
            "infra_failure_count": 0,
            "wall_seconds": None,
            "cost_usd": None,
            "total_tokens": None,
        },
        "reported": {
            "cost": False,
            "tokens": False,
            "wall_seconds": False,
            "blocking_bugs": False,
            "false_blockers": False,
            "missed_blockers": False,
            "checks_failed": False,
        },
        "rates": {},
        "promotion": {},
    }


def _scorecard_key(provider: str, role: str) -> str:
    return f"{provider}\0{role}"


def _scorecard(
    rows: dict[str, dict[str, Any]],
    *,
    provider: str,
    role: str,
) -> dict[str, Any]:
    key = _scorecard_key(provider, role)
    if key not in rows:
        rows[key] = _empty_scorecard(provider, role)
    return rows[key]


def _add_lane_id(row: dict[str, Any], lane_id: str) -> None:
    if not lane_id:
        return
    lane_ids = row["lane_ids"]
    if lane_id not in lane_ids:
        lane_ids.append(lane_id)


def _add_count_metric(row: dict[str, Any], key: str, value: object) -> bool:
    number = _int(value)
    if number is None:
        return False
    row["metrics"][key] = int(row["metrics"].get(key) or 0) + number
    return True


def _add_float_metric(row: dict[str, Any], key: str, value: object) -> bool:
    number = _number(value)
    if number is None:
        return False
    current = _number(row["metrics"].get(key)) or 0
    row["metrics"][key] = round(float(current) + float(number), 6)
    return True


def _add_token_metric(row: dict[str, Any], value: object) -> bool:
    number = _int(value)
    if number is None:
        return False
    current = _int(row["metrics"].get("total_tokens")) or 0
    row["metrics"]["total_tokens"] = current + number
    row["reported"]["tokens"] = True
    return True


def _add_spend_group(row: dict[str, Any], group: Mapping[str, Any]) -> None:
    reviewer_runs = int(group.get("reviewer_run_count") or 0)
    row["sources"]["reviewer_spend_runs"] += reviewer_runs
    _add_lane_id(row, str(group.get("lane") or ""))
    for key in ("reviewer_run_count", "audit_pass_count", "audit_blocked_count"):
        _add_count_metric(row, key, group.get(key))
    blocked = _int(group.get("audit_blocked_count")) or 0
    if blocked:
        row["metrics"]["blocked_finding_count"] += blocked
    infra_failures = _int(group.get("infra_failure_count")) or 0
    if infra_failures:
        row["metrics"]["infra_failure_count"] += infra_failures
    if group.get("wall_reported") and _add_float_metric(row, "wall_seconds", group.get("wall_seconds")):
        row["reported"]["wall_seconds"] = True
    if group.get("cost_reported") and _add_float_metric(row, "cost_usd", group.get("cost_usd")):
        row["reported"]["cost"] = True
    if group.get("token_reported"):
        _add_token_metric(row, group.get("total_tokens"))


def _add_cloud_provider_event(row: dict[str, Any], event: Mapping[str, Any]) -> None:
    metrics = event.get("metrics") if isinstance(event.get("metrics"), Mapping) else {}
    dimensions = event.get("dimensions") if isinstance(event.get("dimensions"), Mapping) else {}
    row["sources"]["cloud_events"] += 1
    lane_id = str(dimensions.get("lane_id") or "").strip()
    if not lane_id and _aggregation_subject(event) == "lane":
        lane_id = str(dimensions.get("aggregation_key") or "").strip()
    _add_lane_id(row, lane_id)
    local_spend_runs = int(row["sources"].get("reviewer_spend_runs") or 0)
    for key in SCORECARD_COUNT_METRICS:
        if key not in metrics:
            continue
        if local_spend_runs and key in {
            "audit_pass_count",
            "audit_blocked_count",
            "blocked_finding_count",
            "reviewer_run_count",
        }:
            continue
        if _add_count_metric(row, key, metrics.get(key)):
            if key == "blocking_bug_count":
                row["reported"]["blocking_bugs"] = True
            elif key == "false_blocker_count":
                row["reported"]["false_blockers"] = True
            elif key == "missed_blocker_count":
                row["reported"]["missed_blockers"] = True
            elif key == "checks_failed_count":
                row["reported"]["checks_failed"] = True
                row["metrics"]["infra_failure_count"] += _int(metrics.get(key)) or 0
    if (
        (not row["reported"]["cost"] or not local_spend_runs)
        and _add_float_metric(row, "cost_usd", metrics.get("cost_usd"))
    ):
        row["reported"]["cost"] = True
    if not row["reported"]["tokens"] or not local_spend_runs:
        _add_token_metric(row, metrics.get("total_tokens"))


def _rate(numerator: object, denominator: object) -> float | None:
    numerator_value = _number(numerator)
    denominator_value = _number(denominator)
    if numerator_value is None or denominator_value in (None, 0):
        return None
    return round(float(numerator_value) / float(denominator_value), 3)


def _finalize_scorecard(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row["metrics"]
    audit_total = int(metrics.get("audit_pass_count") or 0) + int(
        metrics.get("audit_blocked_count") or 0
    )
    reviewer_runs = int(metrics.get("reviewer_run_count") or 0)
    metrics["cost_usd"] = round(float(metrics["cost_usd"]), 6) if row["reported"]["cost"] else None
    metrics["wall_seconds"] = (
        round(float(metrics["wall_seconds"]), 3) if row["reported"]["wall_seconds"] else None
    )
    metrics["total_tokens"] = int(metrics["total_tokens"]) if row["reported"]["tokens"] else None
    row["lane_ids"] = sorted(str(item) for item in row["lane_ids"])
    row["rates"] = {
        "audit_pass_rate": _rate(metrics.get("audit_pass_count"), audit_total),
        "audit_block_rate": _rate(metrics.get("audit_blocked_count"), audit_total),
        "reviewer_catch_rate": _rate(metrics.get("reviewer_catch_count"), reviewer_runs),
        "false_blocker_rate": _rate(metrics.get("false_blocker_count"), audit_total)
        if row["reported"]["false_blockers"]
        else None,
        "missed_blocker_rate": _rate(metrics.get("missed_blocker_count"), audit_total)
        if row["reported"]["missed_blockers"]
        else None,
        "average_wall_seconds": _rate(metrics.get("wall_seconds"), reviewer_runs)
        if row["reported"]["wall_seconds"]
        else None,
    }
    row["promotion"] = _promotion_assessment(row)
    return row


def _promotion_assessment(row: Mapping[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    reported = row.get("reported") if isinstance(row.get("reported"), Mapping) else {}
    role = str(row.get("role") or "")
    reviewer_runs = int(metrics.get("reviewer_run_count") or 0)
    builder_runs = int(metrics.get("builder_run_count") or 0)
    pass_count = int(metrics.get("audit_pass_count") or 0)
    catches = int(metrics.get("reviewer_catch_count") or 0)
    infra_failures = int(metrics.get("infra_failure_count") or 0)
    false_blockers = int(metrics.get("false_blocker_count") or 0)
    missed_blockers = int(metrics.get("missed_blocker_count") or 0)
    caveats: list[str] = []
    if role == "reviewer":
        if reviewer_runs < 10:
            caveats.append("needs at least 10 adjudicated reviewer runs")
        if pass_count < 2:
            caveats.append("needs at least 2 known-clean PASS runs")
        if catches < 1 and int(metrics.get("blocking_bug_count") or 0) < 1:
            caveats.append("needs known-blocked catch evidence")
        if not reported.get("false_blockers") or not reported.get("missed_blockers"):
            caveats.append("needs manual outcome evidence for false positives and missed blockers")
    elif role == "builder":
        if builder_runs < 5:
            caveats.append("needs more builder throughput samples")
        if not reported.get("cost") or not reported.get("tokens"):
            caveats.append("needs cost/token evidence when the provider exposes it")
    else:
        caveats.append("needs explicit builder or reviewer role")
    if infra_failures:
        caveats.append("stabilize provider or workflow failures before promotion")
    if false_blockers or missed_blockers:
        caveats.append("review adjudicated false positives or missed blockers before promotion")
    if infra_failures:
        recommendation = "stabilize_infra"
    elif caveats:
        recommendation = "informational"
    else:
        recommendation = "candidate_for_policy_review"
    return {
        "recommendation": recommendation,
        "merge_authority": False,
        "policy": PROMOTION_POLICY_PATH,
        "caveats": caveats,
    }


def _provider_scorecards(
    *,
    spend: Mapping[str, Any],
    cloud_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for group in spend.get("by_lane") or []:
        if not isinstance(group, Mapping):
            continue
        provider = str(group.get("provider") or reviewer_spend.provider_from_lane(str(group.get("lane") or "")))
        row = _scorecard(rows, provider=provider or "unknown", role=str(group.get("role") or "reviewer"))
        _add_spend_group(row, group)
    for event in cloud_events:
        if not _is_provider_subject(event):
            continue
        row = _scorecard(
            rows,
            provider=_provider_from_event(event),
            role=_role_from_event(event),
        )
        _add_cloud_provider_event(row, event)
    scorecards = [_finalize_scorecard(row) for row in rows.values()]
    scorecards.sort(
        key=lambda row: (
            -int(row["metrics"].get("reviewer_catch_count") or 0),
            -int(row["metrics"].get("reviewer_run_count") or 0),
            str(row.get("provider") or ""),
            str(row.get("role") or ""),
        )
    )
    return {
        "schema": PRODUCTIVITY_SCORECARDS_SCHEMA,
        "promotion_policy": PROMOTION_POLICY_PATH,
        "scorecards": scorecards,
        "notes": [
            "Scorecards use metadata only; missing cost or token fields mean unavailable, not zero.",
            "Promotion recommendations are advisory and require docs/lane-promotion-policy.md review before merge authority.",
        ],
    }


def _public_spend_groups(groups: object) -> list[dict[str, Any]]:
    if not isinstance(groups, list):
        return []
    public_groups: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        row = {key: group.get(key) for key in PUBLIC_SPEND_LANE_KEYS if key in group}
        if not group.get("wall_reported"):
            row["wall_seconds"] = None
        if not group.get("cost_reported"):
            row["cost_usd"] = None
        if not group.get("token_reported"):
            row["total_tokens"] = None
        public_groups.append(row)
    return public_groups


def _prefer(*values: object) -> object:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _latest_snapshot(
    current_status: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if current_status:
        return current_status
    if not events:
        return None
    latest = max(events, key=lambda event: str(event.get("created_at") or ""))
    snapshot = latest.get("snapshot")
    return snapshot if isinstance(snapshot, Mapping) else None


def build_report(
    *,
    repo: str,
    repo_path: str | Path = ".",
    store_path: str | Path | None = None,
    spend_path: str | Path | None = None,
    cloud_event_paths: Sequence[str | Path] = (),
    current_status: Mapping[str, Any] | None = None,
    event_limit: int = 500,
    now: datetime | None = None,
) -> dict[str, Any]:
    repo_root = Path(repo_path)
    event_store_path = Path(store_path) if store_path else board_store.default_store_path(repo_root)
    reviewer_spend_path = (
        Path(spend_path) if spend_path else repo_root / reviewer_spend.DEFAULT_SPEND_PATH
    )
    store_report = board_store.event_report(path=event_store_path, limit=event_limit)
    board_events = [event for event in store_report.get("events") or [] if isinstance(event, Mapping)]
    board_metrics = _board_observations(board_events)
    latest_status = _latest_snapshot(current_status, board_events)
    current = _current_state(latest_status)
    spend, spend_warnings = _spend_summary(repo, reviewer_spend_path)
    cloud_events, cloud_warnings = _load_productivity_events(cloud_event_paths)
    cloud = _cloud_summary(cloud_events)
    providers = _provider_scorecards(spend=spend, cloud_events=cloud_events)
    cloud_latest = cloud.get("latest_metrics") if isinstance(cloud.get("latest_metrics"), Mapping) else {}
    cloud_totals = (
        cloud.get("provided_metric_totals")
        if isinstance(cloud.get("provided_metric_totals"), Mapping)
        else {}
    )
    spend_available = bool(spend.get("available"))
    board_history_available = bool(board_metrics.get("snapshot_count"))
    local_label_reviewer_count = (
        int(board_metrics.get("audit_pass_count") or 0)
        + int(board_metrics.get("audit_blocked_count") or 0)
    )
    local_reviewer_count = max(
        int(spend.get("run_count") or 0) if spend_available else 0,
        local_label_reviewer_count if board_history_available else 0,
    )
    local_pass_count = max(
        int(spend.get("audit_pass_count") or 0) if spend_available else 0,
        int(board_metrics.get("audit_pass_count") or 0) if board_history_available else 0,
    )
    local_blocked_count = max(
        int(spend.get("audit_blocked_count") or 0) if spend_available else 0,
        int(board_metrics.get("audit_blocked_count") or 0) if board_history_available else 0,
    )
    local_blocked_findings = int(board_metrics.get("blocked_finding_count") or 0)
    local_counts_available = spend_available or board_history_available
    local_spend_run_count = local_reviewer_count if local_counts_available else None
    local_pass_value = local_pass_count if local_counts_available else None
    local_blocked_value = local_blocked_count if local_counts_available else None
    local_blocked_value_for_quality = (
        local_blocked_findings if board_history_available else None
    )
    local_fix_round_value = (
        int(board_metrics.get("fix_round_count") or 0) if board_history_available else None
    )

    reviewer_run_count = _prefer(
        local_spend_run_count,
        cloud_latest.get("reviewer_run_count"),
        cloud_totals.get("reviewer_run_count"),
    )
    audit_pass_count = _prefer(
        local_pass_value,
        cloud_latest.get("audit_pass_count"),
        cloud_totals.get("audit_pass_count"),
    )
    audit_blocked_count = _prefer(
        local_blocked_value,
        cloud_latest.get("audit_blocked_count"),
        cloud_totals.get("audit_blocked_count"),
    )
    blocked_finding_count = _prefer(
        local_blocked_value_for_quality,
        cloud_latest.get("blocked_finding_count"),
        cloud_totals.get("blocked_finding_count"),
    )
    warnings = [*spend_warnings, *cloud_warnings]
    if int(store_report.get("event_count") or 0) > len(board_events):
        warnings.append("local board history truncated by event limit")
    evidence = _evidence_block(
        repo=repo,
        board_metrics=board_metrics,
        store_available=bool(store_report.get("available")),
        spend=spend,
        cloud=cloud,
    )

    metrics = {
        "cycle_time_seconds": _prefer(
            cloud_latest.get("cycle_time_seconds"), cloud_totals.get("cycle_time_seconds")
        ),
        "active_time_seconds": _prefer(
            cloud_latest.get("active_time_seconds"), cloud_totals.get("active_time_seconds")
        ),
        "wait_time_seconds": _prefer(
            cloud_latest.get("wait_time_seconds"), cloud_totals.get("wait_time_seconds")
        ),
        "time_to_first_review_seconds": _prefer(
            cloud_latest.get("time_to_first_review_seconds"),
            cloud_totals.get("time_to_first_review_seconds"),
        ),
        "time_to_green_seconds": _prefer(
            cloud_latest.get("time_to_green_seconds"), cloud_totals.get("time_to_green_seconds")
        ),
        "time_to_merge_seconds": _prefer(
            cloud_latest.get("time_to_merge_seconds"), cloud_totals.get("time_to_merge_seconds")
        ),
        "reviewer_run_count": reviewer_run_count,
        "audit_pass_count": audit_pass_count,
        "audit_blocked_count": audit_blocked_count,
        "reviewer_catch_count": _prefer(
            local_blocked_value_for_quality,
            cloud_latest.get("reviewer_catch_count"),
            cloud_totals.get("reviewer_catch_count"),
        ),
        "blocking_bug_count": _prefer(
            cloud_latest.get("blocking_bug_count"), cloud_totals.get("blocking_bug_count")
        ),
        "blocked_finding_count": blocked_finding_count,
        "fix_round_count": _prefer(
            local_fix_round_value,
            cloud_latest.get("fix_round_count"),
            cloud_totals.get("fix_round_count"),
        ),
        "owner_intervention_count": _prefer(
            current.get("owner_action_count"),
            cloud_latest.get("owner_intervention_count"),
            cloud_totals.get("owner_intervention_count"),
        ),
        "merged_pr_count": _prefer(
            cloud_latest.get("merged_pr_count"), cloud_totals.get("merged_pr_count")
        ),
        "abandoned_pr_count": _prefer(
            cloud_latest.get("abandoned_pr_count"), cloud_totals.get("abandoned_pr_count")
        ),
        "reverted_pr_count": _prefer(
            cloud_latest.get("reverted_pr_count"), cloud_totals.get("reverted_pr_count")
        ),
        "cost_usd": _prefer(
            spend.get("cost_usd"), cloud_latest.get("cost_usd"), cloud_totals.get("cost_usd")
        ),
        "total_tokens": _prefer(
            spend.get("total_tokens"),
            cloud_latest.get("total_tokens"),
            cloud_totals.get("total_tokens"),
        ),
        "reviewer_wall_seconds": spend.get("wall_seconds"),
    }
    has_productivity_signal = any(
        value not in (None, "", 0)
        for value in [
            board_metrics.get("snapshot_count"),
            spend.get("run_count"),
            cloud.get("event_count"),
        ]
    )
    status = "pass" if has_productivity_signal else "warn"
    next_action = _next_action(
        repo=repo,
        current_status=latest_status,
        board_metrics=board_metrics,
        spend=spend,
        cloud=cloud,
    )
    return {
        "schema": PRODUCTIVITY_REPORT_SCHEMA,
        "repo": repo,
        "generated_at": _timestamp(now or _now()),
        "status": status,
        "source": {
            "board_events": {
                "available": bool(store_report.get("available")),
                "event_count": int(store_report.get("event_count") or 0),
                "used_events": len(board_events),
                "path": lane_status.LOCAL_PATH_REDACTION,
                "path_redacted": True,
                "message": str(store_report.get("message") or ""),
            },
            "reviewer_spend": {
                "available": bool(spend.get("available")),
                "run_count": int(spend.get("run_count") or 0),
                "filtered_rows": int(spend.get("filtered_rows") or 0),
                "path": lane_status.LOCAL_PATH_REDACTION,
                "path_redacted": True,
            },
            "cloud_productivity": {
                "available": bool(cloud.get("available")),
                "event_count": int(cloud.get("event_count") or 0),
            },
            "remote_available": bool(current.get("remote_available")),
        },
        "window": {
            "local_history": {
                "start": board_metrics.get("window_start") or "",
                "end": board_metrics.get("window_end") or "",
                "duration_seconds": board_metrics.get("duration_seconds"),
            },
            "cloud_latest": {
                "created_at": cloud.get("latest_created_at") or "",
                "dimensions": cloud.get("latest_dimensions") or {},
            },
        },
        "current": current,
        "metrics": metrics,
        "quality": {
            "audit_pass_count": audit_pass_count,
            "audit_blocked_count": audit_blocked_count,
            "reviewer_catch_count": metrics["reviewer_catch_count"],
            "blocking_bug_count": metrics["blocking_bug_count"],
            "blocked_finding_count": blocked_finding_count,
            "fix_round_count": metrics["fix_round_count"],
        },
        "spend": {
            "reviewer_run_count": reviewer_run_count,
            "wall_seconds": spend.get("wall_seconds"),
            "cost_usd": metrics["cost_usd"],
            "total_tokens": metrics["total_tokens"],
            "by_lane": _public_spend_groups(spend.get("by_lane")),
        },
        "providers": providers,
        "cloud_aggregate": cloud,
        "warnings": warnings,
        "evidence": evidence,
        "next_action": next_action,
    }


def _next_action(
    *,
    repo: str,
    current_status: Mapping[str, Any] | None,
    board_metrics: Mapping[str, Any],
    spend: Mapping[str, Any],
    cloud: Mapping[str, Any],
) -> str:
    status_action = ""
    if current_status:
        status_action = str(current_status.get("next_action") or "")
    current_counts = _current_state(current_status)
    if (
        status_action
        and status_action not in {"no active lanes", "local lanes visible; connect them to PR evidence"}
    ):
        return status_action
    if status_action == "local lanes visible; connect them to PR evidence" and current_counts.get(
        "active_lane_count"
    ):
        return status_action
    if not board_metrics.get("snapshot_count"):
        return (
            f"run code-mower board serve --repo {repo} --record-events to build "
            f"local history, or code-mower board record --repo {repo} for one snapshot"
        )
    if not spend.get("run_count"):
        return "capture reviewer spend rows from audit wrappers for cost/latency"
    if not cloud.get("event_count"):
        return "add or upload productivity_summary events to compare release windows"
    return "continue supervised loop and compare the next report window"


def _evidence_block(
    *,
    repo: str,
    board_metrics: Mapping[str, Any],
    store_available: bool,
    spend: Mapping[str, Any],
    cloud: Mapping[str, Any],
) -> dict[str, Any]:
    """Separate command success from evidence readiness.

    Empty or partial evidence stays a successful command; this additive block
    explicitly reports insufficient evidence and the Board recording and
    event-store next steps needed to fill it.
    """

    board_history = bool(store_available) and int(board_metrics.get("snapshot_count") or 0) > 0
    reviewer_spend_ready = int(spend.get("run_count") or 0) > 0
    cloud_events_ready = int(cloud.get("event_count") or 0) > 0
    missing: list[str] = []
    steps: list[str] = []
    if not board_history:
        missing.append("board history")
        steps.append(
            f"run code-mower board serve --repo {repo} --record-events to build "
            f"local history, or code-mower board record --repo {repo} for one snapshot"
        )
    if not reviewer_spend_ready:
        missing.append("reviewer spend")
        steps.append("capture reviewer spend rows from audit wrappers for cost/latency")
    if not cloud_events_ready:
        missing.append("cloud events")
        steps.append("add or upload productivity_summary events to compare release windows")
    ready = not missing
    if ready:
        detail = "board history, reviewer spend, and cloud events are all present"
    else:
        detail = "insufficient evidence (" + ", ".join(missing) + " missing): " + "; ".join(steps)
    return {
        "ready": ready,
        "board_history": board_history,
        "reviewer_spend": reviewer_spend_ready,
        "cloud_events": cloud_events_ready,
        "missing": missing,
        "detail": detail,
    }


def board_payload(
    *,
    repo: str,
    repo_path: str | Path = ".",
    store_path: str | Path | None = None,
    spend_path: str | Path | None = None,
    current_status: Mapping[str, Any] | None = None,
    event_limit: int = 500,
) -> dict[str, Any]:
    report = build_report(
        repo=repo,
        repo_path=repo_path,
        store_path=store_path,
        spend_path=spend_path,
        current_status=current_status,
        event_limit=event_limit,
    )
    return {
        "schema": PRODUCTIVITY_BOARD_SCHEMA,
        "status": report["status"],
        "generated_at": report["generated_at"],
        "current": report["current"],
        "window": report["window"],
        "metrics": report["metrics"],
        "quality": report["quality"],
        "spend": report["spend"],
        "providers": report["providers"],
        "source": report["source"],
        "warnings": report["warnings"],
        "evidence": report["evidence"],
        "next_action": report["next_action"],
    }


def render_text(report: Mapping[str, Any]) -> str:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), Mapping) else {}
    current = report.get("current") if isinstance(report.get("current"), Mapping) else {}
    window = report.get("window") if isinstance(report.get("window"), Mapping) else {}
    local_window = (
        window.get("local_history") if isinstance(window.get("local_history"), Mapping) else {}
    )
    spend = report.get("spend") if isinstance(report.get("spend"), Mapping) else {}
    cloud = report.get("cloud_aggregate") if isinstance(report.get("cloud_aggregate"), Mapping) else {}
    providers = report.get("providers") if isinstance(report.get("providers"), Mapping) else {}
    scorecards = providers.get("scorecards") if isinstance(providers.get("scorecards"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    evidence = report.get("evidence") if isinstance(report.get("evidence"), Mapping) else {}
    lines = [
        f"Code Mower productivity report for {report.get('repo') or ''}",
        f"Status: {report.get('status') or 'warn'}",
        "",
        (
            "Current: "
            f"open PRs {_known_number(current.get('open_pr_count'))}, "
            f"active lanes {_known_number(current.get('active_lane_count'))}, "
            f"blocked PRs {_known_number(current.get('blocked_pr_count'))}, "
            f"stale PRs {_known_number(current.get('stale_pr_count'))}, "
            f"owner actions {_known_number(current.get('owner_action_count'))}"
        ),
        (
            "Window: "
            f"{local_window.get('start') or 'unknown'} to {local_window.get('end') or 'unknown'} "
            f"({_known_seconds(local_window.get('duration_seconds'))}, "
            f"{_known_number(report.get('source', {}).get('board_events', {}).get('used_events'))} snapshots)"
        ),
        (
            "Throughput: "
            f"merged PRs {_known_number(metrics.get('merged_pr_count'))}, "
            f"cycle {_known_seconds(metrics.get('cycle_time_seconds'))}, "
            f"active {_known_seconds(metrics.get('active_time_seconds'))}, "
            f"wait {_known_seconds(metrics.get('wait_time_seconds'))}"
        ),
        (
            "Quality: "
            f"reviewer runs {_known_number(metrics.get('reviewer_run_count'))}, "
            f"PASS {_known_number(metrics.get('audit_pass_count'))}, "
            f"BLOCKED {_known_number(metrics.get('audit_blocked_count'))}, "
            f"catches {_known_number(metrics.get('reviewer_catch_count'))}, "
            f"fix rounds {_known_number(metrics.get('fix_round_count'))}"
        ),
        (
            "Cost/latency: "
            f"{_known_seconds(spend.get('wall_seconds'))} reviewer wall, "
            f"{_known_number(spend.get('total_tokens'))} tokens, "
            f"{_known_money(spend.get('cost_usd'))}"
        ),
        f"Cloud aggregates: {_known_number(cloud.get('event_count'))} productivity_summary event(s)",
    ]
    if scorecards:
        lines.append("Provider scorecards:")
        for row in scorecards[:5]:
            if not isinstance(row, Mapping):
                continue
            row_metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
            promotion = row.get("promotion") if isinstance(row.get("promotion"), Mapping) else {}
            lines.append(
                "- "
                f"{row.get('provider') or 'unknown'} {row.get('role') or 'unknown'}: "
                f"reviewer runs {_known_number(row_metrics.get('reviewer_run_count'))}, "
                f"PASS {_known_number(row_metrics.get('audit_pass_count'))}, "
                f"BLOCKED {_known_number(row_metrics.get('audit_blocked_count'))}, "
                f"catches {_known_number(row_metrics.get('reviewer_catch_count'))}, "
                f"blocked findings {_known_number(row_metrics.get('blocked_finding_count'))}, "
                f"cost {_known_money(row_metrics.get('cost_usd'))}, "
                f"promotion {promotion.get('recommendation') or 'informational'}"
            )
    if warnings:
        lines.append("Warnings: " + "; ".join(str(warning) for warning in warnings[:5]))
    if evidence:
        lines.append(f"Evidence: {evidence.get('detail') or 'unknown'}")
    lines.extend(["", f"Next: {report.get('next_action') or 'inspect'}"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower productivity")
    subparsers = parser.add_subparsers(dest="command", required=True)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--repo", required=True)
    report_parser.add_argument("--repo-path", default=".")
    report_parser.add_argument("--store-path")
    report_parser.add_argument("--spend-path")
    report_parser.add_argument(
        "--cloud-event",
        "--cloud-events",
        dest="cloud_events",
        action="append",
        default=[],
        help="metadata-only productivity_summary event file; may be repeated",
    )
    report_parser.add_argument("--event-limit", type=int, default=500)
    report_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv or ()))
    if args.command == "report":
        if args.event_limit < 0:
            print("error: --event-limit must be non-negative", file=sys.stderr)
            return 2
        report = build_report(
            repo=args.repo,
            repo_path=args.repo_path,
            store_path=args.store_path,
            spend_path=args.spend_path,
            cloud_event_paths=args.cloud_events,
            event_limit=args.event_limit,
        )
        output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render_text(report)
        print(output, end="")
        return 0
    raise AssertionError(f"unhandled productivity command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
