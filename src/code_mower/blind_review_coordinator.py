#!/usr/bin/env python3
"""Plan blind-review hold/release state from provider-neutral lane events."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DONE_STATES = {"done", "pass", "passed", "completed", "success"}
BLOCKED_STATES = {"blocked", "failed", "failure", "error"}
PENDING_STATES = {"needs", "pending", "queued", "running", "requested", "stale"}
EVENT_TIMESTAMP_FIELDS = ("timestamp", "updated_at", "submitted_at", "created_at")


def _strip_prefix(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


def normalize_state(value: Any) -> str:
    text = str(value or "pending").strip().lower().replace("_", "-")
    text = _strip_prefix(_strip_prefix(text, "audit-"), "review-")
    if text in DONE_STATES:
        return "done"
    if text in BLOCKED_STATES:
        return "blocked"
    if text in PENDING_STATES:
        return "pending"
    return "pending"


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("blind-review manifest must be a JSON object")
    return payload


def _required_lanes(manifest: Mapping[str, Any]) -> list[str]:
    raw_lanes = manifest.get("required_lanes", [])
    if raw_lanes is None:
        raw_lanes = []
    if not isinstance(raw_lanes, list):
        raise ValueError("required_lanes must be a JSON array of lane IDs")

    lanes: list[str] = []
    for index, lane in enumerate(raw_lanes):
        if not isinstance(lane, str) or not lane.strip():
            raise ValueError(
                f"required_lanes[{index}] must be a non-empty string lane ID"
            )
        lanes.append(lane.strip())
    return lanes


def _event_timestamp(event: Mapping[str, Any]) -> str:
    for field in EVENT_TIMESTAMP_FIELDS:
        value = event.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_event_timestamp(event: Mapping[str, Any]) -> datetime | None:
    timestamp = _event_timestamp(event)
    if not timestamp:
        return None
    normalized = timestamp
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_events(events: Any) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    lane_events: dict[str, list[Mapping[str, Any]]] = {}
    if not isinstance(events, list):
        return latest
    for event in events:
        if not isinstance(event, Mapping):
            continue
        lane = str(event.get("lane") or "")
        if not lane:
            continue
        lane_events.setdefault(lane, []).append(event)
    for lane, items in lane_events.items():
        timestamps = [_parse_event_timestamp(event) for event in items]
        if all(timestamp is not None for timestamp in timestamps):
            latest_index = max(
                range(len(items)),
                key=lambda index: (timestamps[index], index),
            )
            latest[lane] = items[latest_index]
        else:
            latest[lane] = items[-1]
    return latest


def build_blind_review_plan(manifest: Mapping[str, Any]) -> dict[str, Any]:
    required_lanes = _required_lanes(manifest)
    latest = _latest_events(manifest.get("events", []))
    expected_head = str(manifest.get("head_sha") or "")
    lane_states: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for lane in required_lanes:
        event = latest.get(lane, {})
        raw_state = event.get("state", "pending")
        state = normalize_state(raw_state)
        event_head = str(event.get("head_sha") or "")
        if expected_head and not event_head:
            warnings.append(
                f"{lane}: event head is missing while expected head is {expected_head[:12]}; holding release."
            )
            state = "pending"
        elif expected_head and event_head != expected_head:
            warnings.append(
                f"{lane}: event head {event_head[:12]} does not match expected head {expected_head[:12]}; holding release."
            )
            state = "pending"
        lane_states[lane] = {
            "state": state,
            "raw_state": str(raw_state),
            "artifact": str(event.get("artifact") or event.get("url") or ""),
            "head_sha": event_head,
            "detail": str(event.get("detail") or ""),
        }

    pending_lanes = [
        lane for lane, state in lane_states.items()
        if state["state"] == "pending"
    ]
    blocked_lanes = [
        lane for lane, state in lane_states.items()
        if state["state"] == "blocked"
    ]
    done_lanes = [
        lane for lane, state in lane_states.items()
        if state["state"] == "done"
    ]
    ready_to_release = bool(required_lanes) and not pending_lanes and not blocked_lanes
    release_order = done_lanes if ready_to_release else []
    if not required_lanes:
        warnings.append("no required_lanes configured; blind review cannot release")

    return {
        "mode": "blind-review-plan",
        "repo": manifest.get("repo", ""),
        "pr_number": manifest.get("pr_number", ""),
        "head_sha": expected_head,
        "required_lanes": required_lanes,
        "lane_states": lane_states,
        "done_lanes": done_lanes,
        "pending_lanes": pending_lanes,
        "blocked_lanes": blocked_lanes,
        "ready_to_release": ready_to_release,
        "release_order": release_order,
        "public_mode": (
            "release_all_artifacts"
            if ready_to_release
            else "hold_artifacts"
        ),
        "warnings": warnings,
    }


def render_blind_review_text(plan: Mapping[str, Any]) -> str:
    repo = plan.get("repo") or "unknown repo"
    pr_number = plan.get("pr_number") or "?"
    lines = [
        f"Blind review plan for {repo}#{pr_number}",
        f"ready_to_release: {str(plan.get('ready_to_release')).lower()}",
        "",
        "Lane states:",
    ]
    lane_states = plan.get("lane_states", {})
    if isinstance(lane_states, Mapping):
        for lane, state in lane_states.items():
            if not isinstance(state, Mapping):
                continue
            artifact = f" artifact={state.get('artifact')}" if state.get("artifact") else ""
            lines.append(f"- {lane}: {state.get('state')}{artifact}")
    if plan.get("pending_lanes"):
        lines.extend(["", "Pending lanes:"])
        lines.extend(f"- {lane}" for lane in plan.get("pending_lanes", []))
    if plan.get("blocked_lanes"):
        lines.extend(["", "Blocked lanes:"])
        lines.extend(f"- {lane}" for lane in plan.get("blocked_lanes", []))
    if plan.get("warnings"):
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in plan.get("warnings", []))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    artifacts_parser = subparsers.add_parser("artifacts")
    artifacts_parser.add_argument("manifest", type=Path)
    artifacts_parser.add_argument("--output-dir", default=".code-mower/blind-review")
    artifacts_parser.add_argument(
        "--storage-backend",
        choices=["github_actions_artifact"],
        default="github_actions_artifact",
    )
    artifacts_parser.add_argument("--retention-days", type=int, default=14)
    artifacts_parser.add_argument("--write", action="store_true")
    artifacts_parser.add_argument("--force", action="store_true")
    artifacts_parser.add_argument("--require-sources", action="store_true")
    artifacts_parser.add_argument("--json", action="store_true")
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("manifest", type=Path)
    plan_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "artifacts":
        if __package__ in {None, ""}:
            module_dir = Path(__file__).resolve().parent
            if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
                sys.path.insert(0, str(module_dir.parent))
                from code_mower import blind_review_artifacts
            else:
                sys.path.insert(0, str(module_dir.parent))
                from tools import blind_review_artifacts
        elif __package__ == "tools":
            from tools import blind_review_artifacts
        else:  # pragma: no cover - exercised after package extraction.
            from . import blind_review_artifacts
        return blind_review_artifacts.main(
            [
                str(args.manifest),
                "--output-dir",
                args.output_dir,
                "--storage-backend",
                args.storage_backend,
                "--retention-days",
                str(args.retention_days),
                *(["--write"] if args.write else []),
                *(["--force"] if args.force else []),
                *(["--require-sources"] if args.require_sources else []),
                *(["--json"] if args.json else []),
            ]
        )

    if args.command == "plan":
        try:
            plan = build_blind_review_plan(_load_manifest(args.manifest))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(render_blind_review_text(plan), end="")
        return 0

    raise AssertionError(f"unhandled blind-review command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
