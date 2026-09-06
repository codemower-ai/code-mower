"""Create a local operating brief for the agent hosting a Code Mower session."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import ConfigError, _format_issues, load_config, validate_config
from .participants import (
    PARTICIPANTS,
    configured_participants,
    parse_participants,
    participant_id,
    reference_review_config,
)


def build_session(
    *, repo: str, host: str, selected: tuple[str, ...],
    config: Mapping[str, Any], orchestrator: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ConfigError("--repo must be a GitHub OWNER/REPO slug")
    host = participant_id(host)
    coordinator = participant_id(orchestrator) if orchestrator else host
    if not PARTICIPANTS[host].orchestrator or not PARTICIPANTS[coordinator].orchestrator:
        raise ConfigError("the host and orchestrator must be agent tools, not reviewer-only services")
    selected = parse_participants(",".join(selected))
    lanes = config.get("lanes", {})
    if not isinstance(lanes, Mapping):
        raise ConfigError("lanes must be a mapping")
    members = []
    for name in selected:
        item = PARTICIPANTS[name]
        review = None
        if item.review_lane:
            lane = lanes.get(item.review_lane, reference_review_config(item.review_lane))
            if not isinstance(lane, Mapping):
                raise ConfigError(f"lane {item.review_lane!r} must be a mapping")
            review = {
                "lane": item.review_lane,
                "merge_authority": bool(lane.get("merge_authority")),
                "informational": bool(lane.get("informational")),
                "policy_source": "repository" if item.review_lane in lanes else "starter",
                "readiness": "unchecked",
            }
        members.append({
            "id": name, "name": item.name,
            "can_coordinate": item.orchestrator,
            "builder": ({"lane": item.builder_lane, "handoff": "agent", "readiness": "unchecked"}
                        if item.builder else None),
            "reviewer": review, "note": item.note,
        })
    return {
        "schema": "code_mower.session.v1",
        "repo": repo, "host": host, "orchestrator": coordinator,
        "participants": members,
        "mode": "agent_coordinated",
        "status": "prepared" if host == coordinator else "handoff_required",
        "instructions": [
            "The selected orchestrator coordinates this session; this command does not launch provider processes.",
            "Check participant authentication, permissions, and transport readiness before assigning work.",
            "Assign builds and reviews only to selected participants; report unavailable capabilities instead of substituting another product.",
            "Assign one builder per branch and hand off bounded work through an available tool or existing Code Mower dispatcher.",
            "Request independent reviews against the current PR head; a builder's own review cannot satisfy its peer-review requirement.",
            "Preserve repository merge policy. Selection and orchestration do not confer review or merge authority.",
            "Record results through existing builder/reviewer evidence contracts and use code-mower lanes status for progress.",
        ],
    }


def render_session(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower session: {payload['repo']}",
        f"Orchestrator: {PARTICIPANTS[payload['orchestrator']].name} (host: {payload['host']})",
        f"Status: {payload['status']}",
    ]
    if payload.get("session_file"):
        lines.append(f"Session file: {payload['session_file']}")
    for member in payload["participants"]:
        roles = []
        if member["builder"]:
            roles.append("builder via agent handoff")
        if member["reviewer"]:
            review = member["reviewer"]
            policy = "merge-authority lane" if review["merge_authority"] else "informational lane"
            roles.append(f"reviewer: {review['lane']} ({policy})")
        lines.append(f"- {member['name']}: {', '.join(roles)}")
        if member["note"]:
            lines.append(f"  {member['note']}")
    lines.extend(["", *payload["instructions"]])
    if payload["status"] == "handoff_required":
        lines.append("Pass this brief to the selected orchestrator before beginning work.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="prepare an operating brief for the current agent")
    start.add_argument("--repo", required=True)
    start.add_argument("--with", dest="participants", help="comma-separated participants; defaults to saved setup or Claude + Codex")
    start.add_argument("--host", help="calling agent identity; normally supplied by the agent or CODE_MOWER_HOST")
    start.add_argument("--orchestrator", help="explicit coordinator override; otherwise the calling agent")
    start.add_argument("--config", help="repository configuration; defaults to code-mower.yml when present")
    start.add_argument("--state-dir", default=".code-mower/sessions")
    start.add_argument("--dry-run", action="store_true", help="preview without saving a session")
    start.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="read a saved operating brief")
    show.add_argument("session_file", type=Path)
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            payload = json.loads(args.session_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema") != "code_mower.session.v1":
                raise ConfigError("not a Code Mower session file")
        else:
            host = args.host or os.environ.get("CODE_MOWER_HOST")
            if not host:
                raise ConfigError("the calling agent must supply --host (for example codex or claude), or set CODE_MOWER_HOST")
            path = Path(args.config) if args.config else Path("code-mower.yml")
            config = load_config(path) if args.config or path.is_file() else {}
            if config and (issues := validate_config(config)):
                raise ConfigError("invalid repository configuration:\n" + _format_issues(issues))
            selected = (
                parse_participants(args.participants) if args.participants is not None
                else configured_participants(config)
            )
            payload = build_session(
                repo=args.repo, host=host, selected=selected,
                config=config, orchestrator=args.orchestrator,
            )
            if not args.dry_run:
                payload["id"] = uuid.uuid4().hex
                payload["created_at"] = datetime.now(timezone.utc).isoformat()
                destination = Path(args.state_dir) / f"{payload['id']}.json"
                payload["session_file"] = str(destination.resolve())
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.write("\n")
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else render_session(payload), end="\n" if args.json else "")
        return 0
    except (ConfigError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
