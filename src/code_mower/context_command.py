"""Private evidence handoff and explicit publication of context input metadata."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import context_review
from .claude_audit_pr import _decision_authorities_for_repo
from .context_contract import ContextError, ContextRequest, _object, normalize_policy
from .context_delivery import SUPPORTED_HOSTS, SUPPORTED_RECIPIENTS, attach, deliver, read_binding, render_evidence
from .context_packets import load_authorized
from .context_store import ContextStore, strict_json
from .provider_runners import fetch_issue_comments, fetch_pull_request, post_pr_comment
from .provider_runners.github_auth import resolve_github_token_from_env_or_gh
from .provider_runners.github_pr import _gh_request


def _github():
    token = resolve_github_token_from_env_or_gh()
    if not token:
        raise ContextError("GitHub authorization is required for review input metadata")
    return token


def _private_spec():
    return strict_json(sys.stdin.buffer.read(262_145))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="code-mower context")
    sub = parser.add_subparsers(dest="command", required=True)
    attach_parser = sub.add_parser("attach", help="Attach evidence to a PR and publish only its input revision metadata")
    attach_parser.add_argument("--connection", required=True)
    attach_parser.add_argument("--unavailable", action="store_true", help="Explicitly declare unavailable context; optional work may continue with a fresh code-only review")
    attach_parser.add_argument("--request-stdin", action="store_true", required=True)
    attach_parser.add_argument("--host", choices=SUPPORTED_HOSTS, default=os.environ.get("CODE_MOWER_HOST"))
    for verb in ("deliver", "feedback"):
        command = sub.add_parser(verb, help="Output private evidence or findings only after authorization")
        command.add_argument("--revision", help="Previously attached PR input revision")
        command.add_argument("--packet", help="Packet handle for a work order before its PR exists")
        command.add_argument("--connection")
        command.add_argument("--request-stdin", action="store_true")
        command.add_argument("--recipient", required=True, help="Approved host:role, such as codex:builder")
        if verb == "feedback":
            command.add_argument("--reviewer", choices=SUPPORTED_HOSTS, required=True)
    for command in (attach_parser, *[sub.choices[name] for name in ("deliver", "feedback")]):
        command.add_argument("--state-dir", type=Path)
        command.add_argument("--repo-path", type=Path, default=Path.cwd(), help="Target repository checkout for trusted base configuration")
        command.add_argument("--base-ref", default="origin/main", help="Trusted review/configuration base, for example origin/develop")
    args = parser.parse_args(argv)
    try:
        store = ContextStore(args.state_dir)
        if args.command == "deliver" and args.packet:
            if args.revision or not args.connection or not args.request_stdin:
                raise ContextError("packet delivery requires a connection and private request on stdin")
            spec = _object(_private_spec(), {"repository", "work_item", "policy"})
            if args.recipient not in SUPPORTED_RECIPIENTS or args.recipient.endswith(":reviewer"):
                raise ContextError("independent reviewers consume an attached review revision")
            packet = load_authorized(store, args.connection, args.packet, spec["policy"],
                ContextRequest(spec["repository"], spec["work_item"], args.recipient))
            print(render_evidence(packet, args.packet), end="")
            return 0
        if args.command in ('deliver', 'feedback') and (args.connection or args.request_stdin):
            raise ContextError('an attached revision already binds its connection and work item')
        authorities = _decision_authorities_for_repo(args.repo_path, (), trusted_ref=args.base_ref)
        token = _github()
        if args.command == "attach":
            fields = {"repository", "work_item", "policy", "pr"}
            spec = _object(_private_spec(), fields if args.unavailable else fields | {"packet"})
            policy = normalize_policy(spec['policy'])
            if policy is None or policy['connection'] != args.connection:
                raise ContextError('select the connection named by the work-item policy')
            if args.host not in SUPPORTED_HOSTS:
                raise ContextError("supply the calling host when attaching context")
            if type(spec["pr"]) is not int or spec["pr"] < 1:
                raise ContextError("context attachment requires a PR number")
            actor = _gh_request("GET", "/user", token=token)
            if str(actor.get("login", "")).lower() not in {name.lower() for name in authorities}:
                raise ContextError("the GitHub actor must be a configured Code Mower control authority on the trusted base")
            pr = fetch_pull_request(spec["repository"], spec["pr"], token=token)
            head = pr["head"]["sha"]
            def publish(metadata):
                latest = fetch_pull_request(spec["repository"], spec["pr"], token=token)
                if latest["head"]["sha"] != head:
                    raise ContextError("PR head changed before context attachment; retry against its current head")
                # Mark the dependent gate pending before enabling the handoff.
                _gh_request("POST", f"/repos/{spec['repository']}/statuses/{head}", token=token, body={
                    "context": "code-mower/gate", "state": "pending",
                    "description": "Context input changed; waiting for current review",
                })
                post_pr_comment(spec["repository"], spec["pr"], context_review.INPUT_HEADER + "\n\n"
                    + "Selected evidence changed. Independent reviews must match this input and the current code head.\n\n"
                    + context_review.marker(metadata), token=token)
            if args.unavailable:
                from .context_audit import required_for_repo
                required = policy['required'] or required_for_repo(args.repo_path, args.base_ref)
                metadata = {'revision': uuid.uuid4().hex, 'head': head, 'required': required,
                    'state': 'required_unavailable' if required else 'optional_unavailable',
                    'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=policy['max_age_seconds'])).isoformat()}
                # No credential read, provider call, evidence binding or fallback.
                # The current trusted gate independently rejects policy downgrade.
                publish(metadata)
            else:
                metadata = attach(store, args.connection, spec["packet"], spec["policy"],
                    ContextRequest(spec["repository"], spec["work_item"], args.host + ":orchestrator"),
                    pr=spec["pr"], head=head, publish=publish)
            print(json.dumps({"status": "attached", **metadata}, sort_keys=True))
            return 0
        if not args.revision or args.packet:
            raise ContextError("this delivery requires an attached review revision")
        binding = read_binding(store, args.revision)
        current = context_review.latest_input(fetch_issue_comments(binding["repository"], binding["pr"], token=token),
                                             authorities=authorities)
        if current is None:
            raise ContextError("no trusted current context input is declared")
        head = fetch_pull_request(binding["repository"], binding["pr"], token=token)["head"]["sha"]
        delivery = deliver(store, args.revision, repository=binding["repository"], pr=binding["pr"], head=head,
                           recipient=args.recipient, current=current)
        if args.command == "feedback":
            feedback = delivery.binding["feedback"].get(args.reviewer)
            if feedback is None:
                raise ContextError("no authorized feedback is available for this reviewer")
            print(feedback)
        else:
            print(delivery.text, end="")
        return 0
    except Exception:
        print("context delivery unavailable; verify the selected connection, current input and control authority", file=sys.stderr)
        return 1
