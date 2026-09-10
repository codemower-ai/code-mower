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

from . import session_lease
from .config import ConfigError, _format_issues, load_config, validate_config
from .participants import (
    PARTICIPANTS,
    configured_participants,
    parse_participants,
    participant_id,
    reference_review_config,
)


DEFAULT_STATE_DIR = ".code-mower/sessions"

# Briefs that hold no lease are still useful for planning and review; the
# instruction says so rather than leaving the absence to be inferred.
READ_ONLY_LEASE_INSTRUCTION = (
    "This brief holds no mutating orchestrator lease: read, plan, and report from it, but "
    "run `code-mower session start` without `--dry-run`/`--no-lease` before coordinating changes."
)


JIRA_TRACKER_CONTRACT_INSTRUCTIONS = (
    "Code Mower's Jira REST transport is authoritative for queue reads and all "
    "Jira mutations.",
    "Atlassian Rovo MCP, if available to this host, is optional local read/context "
    "enrichment only; it carries no queue or mutation authority for this session.",
    "Every Jira write must flow through the guarded `code-mower tracker mutate` or "
    "`code-mower tracker pr-sync` commands; do not write through Rovo MCP tools or "
    "any other path.",
    "Report only the configured Jira project key or ID; never surface issue body "
    "text, comments, attachments, or credentials in this session brief.",
)


def _jira_tracker_section(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bounded, metadata-only Jira operating contract shared by every
    orchestrator host, or None when the tracker is not `jira_cloud`."""
    tracker = config.get("tracker")
    if not isinstance(tracker, Mapping) or tracker.get("kind") != "jira_cloud":
        return None
    jira_cloud = tracker.get("jira_cloud")
    jira_cloud = jira_cloud if isinstance(jira_cloud, Mapping) else {}
    project = str(jira_cloud.get("project_key") or jira_cloud.get("project_id") or "")
    return {
        "kind": "jira_cloud",
        "project": project,
        "authority": "code_mower_jira_rest",
        "read_context": "atlassian_rovo_mcp_optional",
        "mutation_commands": ["code-mower tracker mutate", "code-mower tracker pr-sync"],
        "instructions": list(JIRA_TRACKER_CONTRACT_INSTRUCTIONS),
    }


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
    payload: dict[str, Any] = {
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
    tracker_section = _jira_tracker_section(config)
    if tracker_section is not None:
        payload["tracker"] = tracker_section
    return payload


def render_session(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower session: {payload['repo']}",
        f"Orchestrator: {PARTICIPANTS[payload['orchestrator']].name} (host: {payload['host']})",
        f"Status: {payload['status']}",
    ]
    lease = payload.get("lease")
    if isinstance(lease, Mapping):
        if lease.get("mutating"):
            holder = lease.get("orchestrator")
            holder = PARTICIPANTS[holder].name if holder in PARTICIPANTS else str(holder)
            lines.append(
                f"Lease: held by {holder} until {lease['expires_at']} (session {lease['session_id']})"
            )
        else:
            lines.append("Lease: none (read-only brief; no mutating orchestration authority)")
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
    tracker = payload.get("tracker")
    if isinstance(tracker, Mapping) and tracker.get("kind") == "jira_cloud":
        lines.append("")
        project = tracker.get("project") or "(unconfigured)"
        lines.append(f"Tracker: jira_cloud (project {project})")
        lines.extend(tracker.get("instructions", []))
    if payload["status"] == "handoff_required":
        lines.append("Pass this brief to the selected orchestrator before beginning work.")
    return "\n".join(lines) + "\n"


def _mark_read_only(payload: dict[str, Any]) -> None:
    """Record that this brief carries no mutating orchestration authority."""
    payload["lease"] = {"state": session_lease.STATE_ABSENT, "mutating": False}
    payload["instructions"].append(READ_ONLY_LEASE_INSTRUCTION)


def _acquire_startup_lease(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Take the single mutating lease for this session, before anything is saved.

    Acquisition happens up front so a refused session leaves no brief behind for
    a second orchestrator to act on.
    """
    if not args.lease:
        if args.force_lease:
            raise ConfigError("--force-lease takes over a lease; it cannot be combined with --no-lease")
        _mark_read_only(payload)
        return None
    record = session_lease.acquire_lease(
        repo=payload["repo"],
        orchestrator=payload["orchestrator"],
        session_id=payload["id"],
        state_dir=args.state_dir,
        ttl_minutes=args.lease_ttl_minutes,
        force=args.force_lease,
    )
    payload["lease"] = {**record, "state": session_lease.STATE_HELD, "mutating": True}
    return record


def _run_lease_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.lease_command == "show":
        return session_lease.inspect_lease(state_dir=args.state_dir)
    if args.lease_command == "renew":
        return session_lease.renew_lease(
            state_dir=args.state_dir,
            session_id=args.session_id,
            ttl_minutes=args.lease_ttl_minutes,
        )
    return session_lease.release_lease(
        state_dir=args.state_dir, session_id=args.session_id, force=args.force,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="prepare an operating brief for the current agent")
    start.add_argument("--repo", required=True)
    start.add_argument("--with", dest="participants", help="comma-separated participants; defaults to saved setup or Claude + Codex")
    start.add_argument("--host", help="calling agent identity; normally supplied by the agent or CODE_MOWER_HOST")
    start.add_argument("--orchestrator", help="explicit coordinator override; otherwise the calling agent")
    start.add_argument("--config", help="repository configuration; defaults to code-mower.yml when present")
    start.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    start.add_argument("--dry-run", action="store_true", help="preview without saving a session")
    start.add_argument(
        "--no-lease", dest="lease", action="store_false",
        help="generate a read-only brief without taking the mutating orchestrator lease",
    )
    start.add_argument(
        "--force-lease", action="store_true",
        help="take over a live lease held by another session; use only after an owner decision",
    )
    start.add_argument(
        "--lease-ttl-minutes", type=int, default=session_lease.DEFAULT_TTL_MINUTES,
        help="how long the acquired lease stays live before it can be taken over",
    )
    start.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="read a saved operating brief")
    show.add_argument("session_file", type=Path)
    show.add_argument("--json", action="store_true")
    lease = sub.add_parser("lease", help="inspect, renew, or release the local mutating session lease")
    lease_sub = lease.add_subparsers(dest="lease_command", required=True)
    lease_show = lease_sub.add_parser("show", help="report the current lease without changing it")
    lease_renew = lease_sub.add_parser("renew", help="extend the calling session's own lease")
    lease_renew.add_argument("--session-id", required=True, help="the holding session's id")
    lease_renew.add_argument(
        "--lease-ttl-minutes", type=int, default=session_lease.DEFAULT_TTL_MINUTES,
    )
    lease_release = lease_sub.add_parser("release", help="give up the lease")
    lease_release.add_argument("--session-id", help="the holding session's id")
    lease_release.add_argument(
        "--force", action="store_true",
        help="release a live lease owned by another session; use only after an owner decision",
    )
    for lease_parser in (lease_show, lease_renew, lease_release):
        lease_parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
        lease_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    render = render_session
    try:
        if args.command == "show":
            payload = json.loads(args.session_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema") != "code_mower.session.v1":
                raise ConfigError("not a Code Mower session file")
        elif args.command == "lease":
            payload = _run_lease_command(args)
            render = session_lease.render_lease
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
            if args.dry_run:
                _mark_read_only(payload)
            else:
                payload["id"] = uuid.uuid4().hex
                payload["created_at"] = datetime.now(timezone.utc).isoformat()
                record = _acquire_startup_lease(args, payload)
                destination = Path(args.state_dir) / f"{payload['id']}.json"
                payload["session_file"] = str(destination.resolve())
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with destination.open("x", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                except OSError:
                    # A brief that was never written has no orchestrator, so the
                    # lease this call just took must not outlive the failure.
                    if record is not None:
                        session_lease.release_lease(
                            state_dir=args.state_dir, session_id=payload["id"],
                        )
                    raise
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else render(payload), end="\n" if args.json else "")
        return 0
    except (ConfigError, OSError, ValueError, KeyError, TypeError, session_lease.SessionLeaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
