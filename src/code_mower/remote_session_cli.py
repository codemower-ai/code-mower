"""Small provider-neutral remote-session CLI. Stdlib-only until live credentials resolve."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .context_contract import ContextError
from .devin_sessions import DevinApiError, DevinClient
from .remote_session import (
    SCHEMA, DevinProvider, FakeProvider, RemoteError, RemoteSessions, default_root,
)

COMMANDS = ("dispatch", "status", "message", "cancel", "collect")


def register(sub):
    for command in COMMANDS:
        parser = sub.add_parser(command, help=f"{command} private remote work")
        parser.add_argument("session", help="stable local request alias (private, not a brief path)")
        parser.add_argument("--provider", choices=("fake", "devin"), default="fake")
        parser.add_argument("--remote-state-dir", type=Path, default=default_root())
        parser.add_argument("--json", action="store_true")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true", help="authorize mutation")
        mode.add_argument("--dry-run", action="store_true", help="no provider or state access")
        if command in {"dispatch", "message"}:
            parser.add_argument("--input-file", type=Path, help="private UTF-8 task/message file")
        if command == "dispatch":
            parser.add_argument("--repo", required=True)
            parser.add_argument("--max-acu-limit", type=int, default=10)
        if command in {"message", "cancel"}:
            parser.add_argument("--request", required=True, help="stable idempotency key")
            parser.add_argument("--acknowledge-delivered", action="store_true",
                                help="after provider inspection, mark uncertain request delivered without replay")


def run(args):
    try:
        # Preview does not resolve credentials, read prose, create locks, or contact providers.
        if args.dry_run or (args.command != "status" and not args.apply):
            payload = {"schema": SCHEMA, "mode": "dry_run", "operation": args.command,
                       "apply_required": args.command != "status"}
        else:
            prose = ""
            input_file = getattr(args, "input_file", None)
            if input_file is not None:
                with input_file.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise RemoteError("invalid_request")
                prose = raw.decode("utf-8")
            if args.provider == "devin":
                from .devin_api import credentials_from_env, repository_scope_acknowledged
                credentials = credentials_from_env()
                if not credentials.has_credentials:
                    raise RemoteError("authentication_required")
                if args.command == "dispatch" and not repository_scope_acknowledged(args.repo):
                    raise RemoteError("repository_scope_acknowledgement_required")
                provider = DevinProvider(DevinClient(credentials.org_id, credentials.api_key))
            else:
                provider = FakeProvider(args.remote_state_dir / "fake-provider")
            payload = RemoteSessions(args.remote_state_dir, provider).run(
                args.command, args.session, request=getattr(args, "request", ""), prose=prose,
                repo=getattr(args, "repo", ""), limit=getattr(args, "max_acu_limit", 10),
                apply=args.apply,
                acknowledge_delivered=getattr(args, "acknowledge_delivered", False),
            )
        print(json.dumps(payload, sort_keys=True))
        return 0
    except (RemoteError, DevinApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ContextError, OSError, ValueError, TypeError, KeyError):
        print("error: private_state_or_input_unavailable; inspect local permissions and input", file=sys.stderr)
        return 1
