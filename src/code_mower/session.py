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

from . import context_guided, context_prepare, context_session, session_lease
from .config import ConfigError, _format_issues, load_config, validate_config
from .context_contract import ContextError, normalize_policy
from .context_store import ContextStore
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

# The brief's saved lease block is a snapshot from acquisition time, not current
# truth; `session show` re-verifies it live and falls back to this instruction
# whenever that lease is no longer the one backing this session.
STALE_LEASE_INSTRUCTION = (
    "This brief's mutating orchestrator lease is no longer held by this session: read, plan, "
    "and report from it, but run `code-mower session start` again before coordinating changes."
)


JIRA_TRACKER_CONTRACT_INSTRUCTIONS = (
    "Code Mower's Jira REST transport is authoritative for queue reads and all "
    "Jira mutations.",
    "Optional local context providers, including Atlassian Rovo MCP and explicitly "
    "authorized organization context, supply read-only evidence; they carry no queue "
    "or mutation authority for this session.",
    "Every Jira write must flow through the guarded `code-mower tracker mutate` or "
    "`code-mower tracker pr-sync` commands; do not write through context-provider tools or "
    "any other path.",
    "When implementation starts, preview and then apply a claim plus the configured "
    "`in_progress` transition through `code-mower tracker mutate`.",
    "When a non-draft pull request is ready for human review, preview and then apply "
    "the `ready_for_review` milestone through `code-mower tracker pr-sync`.",
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
        "read_context": "optional_authorized_context",
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
    if config.get("context") is not None:
        from .context_readiness import summary
        from .context_contract import normalize_policy
        payload["context"] = summary('unchecked', required=normalize_policy(config['context'])['required'])
        payload["instructions"].extend([
            "Use the explicitly selected context connection; never substitute a host's ambient account.",
            "Give approved participants the same authorized packet through `code-mower context deliver`; context confers no tools or authority.",
            "Attach the selected packet to the PR before peer review. Changed evidence requires a new context input revision and a fresh review, even on the same code head.",
            "Required context that is missing, expired or unauthorized pauses dependent work and produces UNKNOWN review input. Keep private evidence and detailed context-bound findings out of public comments and telemetry.",
        ])
    return payload


def render_session(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower session: {payload['repo']}",
        f"Orchestrator: {PARTICIPANTS[payload['orchestrator']].name} (host: {payload['host']})",
        f"Status: {payload['status']}",
    ]
    context = payload.get('context')
    if isinstance(context, Mapping):
        lines.append(f"Context: {context['readiness']}; dependent work: {context['dependent_work']}")
        lines.append('Next: ' + context['next_action'])
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


def render_context_status(payload: Mapping[str, Any]) -> str:
    return (
        f"Session context: {payload['stage']}\n"
        f"Dependent work: {payload['dependent_work']}\n"
        f"Owner action: {'yes' if payload['owner_action'] else 'no'}\n"
        f"Next: {payload['next_action']}\n"
    )


def render_context_prepare(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Session context: {payload['stage']}",
        f"Status: {payload['status']}",
        f"Dependent work: {payload['dependent_work']}",
    ]
    if payload.get("work_order"):
        lines.append(f"Work order: {payload['work_order']}")
    lines.append(f"Next: {payload['next_action']}")
    return "\n".join(lines) + "\n"


def render_context_private(payload: Mapping[str, Any]) -> str:
    text = str(payload["private_text"])
    return text if text.endswith("\n") else text + "\n"


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
        ttl_minutes=args.lease_ttl_minutes,
        force=args.force_lease,
    )
    payload["lease"] = {**record, "state": session_lease.STATE_HELD, "mutating": True}
    return record


def _run_lease_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.lease_command == "show":
        return session_lease.inspect_lease()
    if args.lease_command == "renew":
        return session_lease.renew_lease(
            session_id=args.session_id,
            ttl_minutes=args.lease_ttl_minutes,
        )
    return session_lease.release_lease(session_id=args.session_id, force=args.force)


def _private_query(selected: bool) -> str | None:
    if not selected:
        return None
    value = sys.stdin.read(2001)
    if len(value) > 2000:
        raise ContextError("private context query exceeds its size bound")
    value = value.strip()
    if not value:
        raise ContextError("--query-stdin requires a private query")
    return value


def _run_context_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    saved = context_session.load_session(args.session_file)
    store = context_session.association_store(args.context_state_dir)
    record = context_session.read(store, saved["id"])
    config_path = Path(args.config) if args.config else Path(args.repo_path) / "code-mower.yml"
    source = load_config(config_path) if config_path.is_file() else {}
    if source and (issues := validate_config(source)):
        raise ConfigError("invalid repository configuration:\n" + _format_issues(issues))
    trusted_policy = normalize_policy(source.get("context")) if source.get("context") is not None else None
    if record is not None:
        if record["policy"] != trusted_policy:
            raise ContextError("context policy changed after session start; start a new session")
        context_session.resolve_bound("repository", record["repo"], saved["repo"])
    live = session_lease.verify_live_lease(
        repo=saved["repo"], session_id=saved["id"], orchestrator=saved["orchestrator"],
        root=args.repo_path,
    )
    lease_live = bool(live.get("mutating"))
    if args.context_command == "status":
        return context_session.status(record, lease_live=lease_live), 0
    if record is None:
        raise ContextError("this session has no selected work item")
    if not lease_live:
        raise ContextError(
            "this session no longer holds the mutating lease; start or resume an authorized session"
        )
    if args.context_command in {"attach", "deliver", "feedback"}:
        packet_store = ContextStore(args.context_state_dir)
        if args.context_command == "attach":
            return context_guided.attach_session(
                store,
                packet_store,
                record,
                repo_path=args.repo_path,
                pr=args.pr,
                base_ref=args.base_ref,
                retry_uncertain=args.retry_uncertain,
            )
        if args.context_command == "deliver":
            text = context_guided.deliver_session(
                store,
                packet_store,
                record,
                repo_path=args.repo_path,
                base_ref=args.base_ref,
            )
        else:
            text = context_guided.feedback_session(
                store,
                packet_store,
                record,
                repo_path=args.repo_path,
                reviewer=args.reviewer,
                base_ref=args.base_ref,
            )
        return {"private_text": text}, 0
    tracker = source.get("tracker")
    retrieval_source = (
        "jira" if isinstance(tracker, Mapping) and tracker.get("kind") == "jira_cloud" else None
    )
    return context_prepare.prepare(
        store,
        record,
        repo_root=args.repo_path,
        context_root=args.context_state_dir,
        query=_private_query(args.query_stdin),
        source=retrieval_source,
        title=args.title,
        builder=args.builder,
        body_file=args.work_order_body_file,
        output=args.output,
        refresh=args.refresh,
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
    start.add_argument("--work-item", help="authoritative work-item identity for a guided context session")
    start.add_argument("--context-state-dir", type=Path, help="private session-context directory outside repositories")
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
        lease_parser.add_argument("--json", action="store_true")
    context = sub.add_parser("context", help="inspect or advance private context for the selected work item")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_status = context_sub.add_parser("status", help="show redacted guided-context progress")
    context_status.add_argument("session_file", type=Path)
    context_status.add_argument("--repo-path", type=Path, default=Path.cwd())
    context_status.add_argument("--config")
    context_status.add_argument("--context-state-dir", type=Path)
    context_status.add_argument("--json", action="store_true")
    context_prepare_parser = context_sub.add_parser(
        "prepare", help="retrieve bounded evidence and create the session work order"
    )
    context_prepare_parser.add_argument("session_file", type=Path)
    context_prepare_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    context_prepare_parser.add_argument("--config")
    context_prepare_parser.add_argument("--context-state-dir", type=Path)
    context_prepare_parser.add_argument(
        "--query-stdin", action="store_true",
        help="read a private query override from stdin; the selected work item is the default",
    )
    context_prepare_parser.add_argument("--work-order-body-file", type=Path)
    context_prepare_parser.add_argument("--title")
    context_prepare_parser.add_argument(
        "--builder", help="selected builder participant; defaults to the hosting agent"
    )
    context_prepare_parser.add_argument(
        "--output", type=Path, help="repository-relative local work-order path"
    )
    context_prepare_parser.add_argument(
        "--refresh", action="store_true",
        help="explicitly replace or retry a prior retrieval",
    )
    context_prepare_parser.add_argument("--json", action="store_true")
    context_attach_parser = context_sub.add_parser(
        "attach", help="attach the prepared packet to a pull request and reconcile retries"
    )
    context_attach_parser.add_argument("session_file", type=Path)
    context_attach_parser.add_argument("--pr", type=int, required=True)
    context_attach_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    context_attach_parser.add_argument("--config")
    context_attach_parser.add_argument("--context-state-dir", type=Path)
    context_attach_parser.add_argument("--base-ref", default="origin/main")
    context_attach_parser.add_argument(
        "--retry-uncertain",
        action="store_true",
        help="republish the same saved revision after an explicit remote-state check",
    )
    context_attach_parser.add_argument("--json", action="store_true")
    context_deliver_parser = context_sub.add_parser(
        "deliver", help="output the authorized packet for the session's selected builder"
    )
    context_deliver_parser.add_argument("session_file", type=Path)
    context_deliver_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    context_deliver_parser.add_argument("--config")
    context_deliver_parser.add_argument("--context-state-dir", type=Path)
    context_deliver_parser.add_argument("--base-ref", default="origin/main")
    context_deliver_parser.set_defaults(json=False)
    context_feedback_parser = context_sub.add_parser(
        "feedback", help="output one selected reviewer's authorized private findings"
    )
    context_feedback_parser.add_argument("session_file", type=Path)
    context_feedback_parser.add_argument("--reviewer", required=True)
    context_feedback_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    context_feedback_parser.add_argument("--config")
    context_feedback_parser.add_argument("--context-state-dir", type=Path)
    context_feedback_parser.add_argument("--base-ref", default="origin/main")
    context_feedback_parser.set_defaults(json=False)
    args = parser.parse_args(argv)
    render = render_session
    exit_code = 0
    try:
        if args.command == "show":
            payload = json.loads(args.session_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema") != "code_mower.session.v1":
                raise ConfigError("not a Code Mower session file")
            saved_lease = payload.get("lease")
            if isinstance(saved_lease, Mapping) and saved_lease.get("mutating"):
                live = session_lease.verify_live_lease(
                    repo=payload["repo"], session_id=payload["id"], orchestrator=payload["orchestrator"],
                )
                payload["lease"] = live
                if not live["mutating"]:
                    payload["instructions"] = [*payload["instructions"], STALE_LEASE_INSTRUCTION]
        elif args.command == "lease":
            payload = _run_lease_command(args)
            render = session_lease.render_lease
        elif args.command == "context":
            payload, exit_code = _run_context_command(args)
            if args.context_command == "status":
                render = render_context_status
            elif args.context_command in {"deliver", "feedback"}:
                render = render_context_private
            else:
                render = render_context_prepare
        else:
            if args.work_item and not args.lease:
                raise ConfigError("--work-item requires a mutating session lease; omit --no-lease")
            if args.context_state_dir is not None and not args.work_item:
                raise ConfigError("--context-state-dir requires --work-item")
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
            if args.work_item:
                payload["work_item"] = {"selected": True}
            if args.dry_run:
                _mark_read_only(payload)
            else:
                payload["id"] = uuid.uuid4().hex
                payload["created_at"] = datetime.now(timezone.utc).isoformat()
                record = _acquire_startup_lease(args, payload)
                destination = Path(args.state_dir) / f"{payload['id']}.json"
                payload["session_file"] = str(destination.resolve())
                association = None
                store = None
                try:
                    if args.work_item:
                        context_session.require_live_session(payload)
                        store = context_session.association_store(args.context_state_dir)
                        association = context_session.create(
                            store, payload, work_item=args.work_item, policy=config.get("context"),
                        )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("x", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                except (OSError, ContextError):
                    # A brief that was never written has no orchestrator, so the
                    # lease this call just took must not outlive the failure --
                    # whether the write itself failed or the destination
                    # directory could not even be created. If another session
                    # force-took the lease in this narrow window, cleanup must
                    # not delete the new holder's lease or mask the original
                    # failure.
                    if association is not None and store is not None:
                        try:
                            context_session.delete(store, payload["id"])
                        except (ContextError, OSError):
                            pass
                    if record is not None:
                        try:
                            session_lease.release_lease(session_id=payload["id"])
                        except session_lease.SessionLeaseError:
                            pass
                    raise
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else render(payload), end="\n" if args.json else "")
        return exit_code
    except (ConfigError, ContextError, OSError, ValueError, KeyError, TypeError,
            session_lease.SessionLeaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
