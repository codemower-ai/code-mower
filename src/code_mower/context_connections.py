"""Optional private Coworker lifecycle. No default installation imports its SDK."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from .context_contract import (
    CAPABILITY_VERSION, CONNECTION_SCHEMA, ContextError, _identifier, _object,
    _strings, _text, _timestamp, validate_connection,
)
from .context_store import ContextStore, strict_json

LOCAL_SCHEMA = "code_mower.contextLocalConnection.v1"
ENDPOINT = "https://odin.coworker.ai/mcp"


def _backend():
    try:
        from .coworker_mcp import CoworkerBackend
    except ImportError:
        raise ContextError("install code-mower[coworker] to use the optional Coworker connection") from None
    return CoworkerBackend()


def connection_spec(value):
    spec = _object(value, {"principal", "workspace", "repositories", "recipients"})
    return {
        "principal": _text(spec["principal"]), "workspace": _text(spec["workspace"]),
        "repositories": list(_strings(spec["repositories"])),
        "recipients": list(_strings(spec["recipients"])),
    }


def _state(value, name):
    if value is None:
        raise ContextError("context connection is missing; run context connect")
    _object(value, {"schema", "connection", "provider", "kind", "generation", "state",
                    "identity", "subject", "expires_at", "credential_id", "repositories",
                    "recipients", "capability_status"})
    if (value["schema"] != LOCAL_SCHEMA or value["connection"] != name
            or value["provider"] != "coworker" or value["kind"] != "organization"
            or value["state"] not in {"verified", "needs_auth", "disconnected"}):
        raise ContextError("unsupported private context state; reconnect")
    identity = _object(value["identity"], {"principal", "workspace", "endpoint"})
    if identity["endpoint"] != ENDPOINT:
        raise ContextError("context endpoint does not match its provider")
    connection_spec({**{k: identity[k] for k in ("principal", "workspace")},
                     **{k: value[k] for k in ("repositories", "recipients")}})
    _text(value["subject"])
    _timestamp(value["expires_at"])
    for key in ("credential_id", "generation"):
        try:
            if uuid.UUID(hex=value[key]).hex != value[key]:
                raise ValueError("invalid")
        except (ValueError, TypeError, AttributeError):
            raise ContextError("invalid private connection reference; reconnect") from None
    statuses = _object(value["capability_status"], {"search", "memory"})
    if any(item not in {"unverified", "available", "unavailable"} for item in statuses.values()):
        raise ContextError("invalid context capability status")
    return value


def _proof_matches(proof, expected):
    if (proof.principal != expected["principal"] or proof.workspace != expected["workspace"]
            or (expected.get("subject") is not None and proof.subject != expected["subject"])
            or not isinstance(proof.credentials, dict) or not proof.credentials
            or type(proof.expires_at) is not int
            or proof.expires_at <= int(datetime.now(timezone.utc).timestamp())):
        raise ContextError("Coworker account, workspace, or authorization does not match; reconnect")
    _text(proof.subject)


def _summary(state):
    return {"schema": "code_mower.contextConnectionSummary.v1", "provider": "coworker",
            "kind": "organization", "status": state["state"],
            **state["capability_status"], "credential_storage": "os"}


def connect(store: ContextStore, name: str, spec, *, backend=None, open_url=webbrowser.open):
    name = _identifier(name)
    spec = connection_spec(spec)
    backend = backend or _backend()
    with store.locked(name, timeout_seconds=1) as locked:
        old = locked.read()
        if old is not None:
            old = _state(old, name)
            if old["state"] == "verified":
                raise ContextError("connection already exists; disconnect before changing accounts or scope")
        credential_id = old["credential_id"] if old else uuid.uuid4().hex
        # Establish vault availability before asking the operator to grant OAuth.
        locked.vault.get(credential_id)
        proof = backend.login(spec, open_url)
        _proof_matches(proof, spec)
        state = {
            "schema": LOCAL_SCHEMA, "connection": name, "provider": "coworker", "kind": "organization",
            "generation": uuid.uuid4().hex, "state": "verified",
            "identity": {"principal": proof.principal, "workspace": proof.workspace, "endpoint": ENDPOINT},
            "subject": proof.subject, "expires_at": datetime.fromtimestamp(proof.expires_at, timezone.utc).isoformat(),
            "credential_id": credential_id, "repositories": spec["repositories"], "recipients": spec["recipients"],
            "capability_status": {"search": "unverified", "memory": "unverified"},
        }
        # Replace a disabled connection's own credential atomically; no other
        # connection or host credential settings are read or changed.
        locked.vault.put(credential_id, proof.credentials)
        try:
            locked.write(state)
        except Exception:
            locked.vault.delete(credential_id)
            raise
        return _summary(state)


def _invalidate(locked, state, *, status="needs_auth"):
    state = {**state, "state": status, "generation": uuid.uuid4().hex}
    locked.write(state)
    return state


def authorize(store: ContextStore, name: str, *, backend=None, explicit_retry=False):
    backend = backend or _backend()
    with store.locked(name) as locked:
        state = _state(locked.read(), name)
        if state["state"] == "disconnected" or (state["state"] != "verified" and not explicit_retry):
            raise ContextError("context authorization is disabled; run context verify or reconnect")
        expected = {**state["identity"], "subject": state["subject"]}
        try:
            credentials = locked.vault.get(state["credential_id"])
            if not credentials:
                raise ContextError("context credentials are missing; reconnect")
            proof = backend.refresh(expected, credentials)
            _proof_matches(proof, expected)
            locked.vault.put(state["credential_id"], proof.credentials)
            state = {**state, "state": "verified", "expires_at": datetime.fromtimestamp(proof.expires_at, timezone.utc).isoformat()}
            locked.write(state)
        except Exception:
            _invalidate(locked, state)
            raise ContextError("context authorization failed; cached evidence is invalid; run context verify or reconnect") from None
        envelope = {
            "schema": CONNECTION_SCHEMA, "capability_version": CAPABILITY_VERSION,
            **{key: state[key] for key in ("connection", "provider", "kind", "generation", "state", "identity",
                                         "repositories", "recipients", "expires_at")},
            "capabilities": {"search": state["capability_status"]["search"] == "available",
                             "memory": state["capability_status"]["memory"] == "available", "revision_binding": False},
        }
        return dict(validate_connection(envelope, now=datetime.now(timezone.utc)))


def status(store: ContextStore, name: str):
    with store.locked(name) as locked:
        state = _state(locked.read(), name)
        summary = _summary(state)
        summary["authorization"] = "unchecked"  # An offline status is never authorization.
        summary["expired"] = _timestamp(state["expires_at"]) <= datetime.now(timezone.utc)
        return summary


def disconnect(store: ContextStore, name: str, *, backend=None):
    with store.locked(name) as locked:
        state = _state(locked.read(), name)
        # First commit the tombstone. Remote/network/vault failure cannot turn
        # this connection or its prior packet generations back on.
        state = _invalidate(locked, state, status="disconnected")
        remote, cleanup = "unknown", "complete"
        try:
            credentials = locked.vault.get(state["credential_id"])
            if credentials:
                (backend or _backend()).revoke(credentials)
                remote = "confirmed"
            else:
                remote = "no_local_credential"
        except Exception:
            pass
        try:
            locked.vault.delete(state["credential_id"])
        except Exception:
            cleanup = "needs_attention"
        return {**_summary(state), "remote_revocation": remote, "credential_cleanup": cleanup}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="code-mower context")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("connect", help="Connect a private organization account using OAuth")
    login.add_argument("provider", choices=["coworker"])
    login.add_argument("--spec-stdin", action="store_true", help="Read private identity/scope JSON from stdin")
    login.add_argument("--no-browser", action="store_true", help="Print the local sign-in URL instead of opening it")
    for command in (login, commands.add_parser("verify"), commands.add_parser("status"), commands.add_parser("disconnect")):
        command.add_argument("--connection", required=True)
        command.add_argument("--state-dir", type=Path)
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        store = ContextStore(args.state_dir)
        if args.command == "connect":
            if args.spec_stdin:
                spec = strict_json(sys.stdin.buffer.read(262_145))
            else:
                if not sys.stdin.isatty():
                    raise ContextError("interactive connection needs a terminal, or private JSON with --spec-stdin")
                spec = {
                    "principal": getpass.getpass("Coworker account email (kept local): "),
                    "workspace": getpass.getpass("Workspace identifier from the Coworker URL (kept local): "),
                    "repositories": input("Approved repositories, comma-separated: ").strip().split(","),
                    "recipients": input("Approved recipients, e.g. codex:builder,claude:reviewer: ").strip().split(","),
                }
            def open_url(url):
                if args.no_browser:
                    print("Open this sign-in URL locally (do not publish it):\n" + url, file=sys.stderr, flush=True)
                elif not webbrowser.open(url):
                    raise ContextError("could not open sign-in; retry with --no-browser")
            result = connect(store, args.connection, spec, open_url=open_url)
        elif args.command == "verify":
            authorize(store, args.connection, explicit_retry=True)
            result = {**status(store, args.connection), "authorization": "verified_online"}
        elif args.command == "status":
            result = status(store, args.connection)
        else:
            result = disconnect(store, args.connection)
        print(json.dumps(result, sort_keys=True) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items() if k != "schema"))
        return 0
    except (ContextError, OSError, ValueError):
        # Never render arbitrary provider, keychain, input, or filesystem errors.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, ContextError) else "context operation failed; check private connection setup"
        print(json.dumps({"status": "unavailable", "message": message}) if args.json else message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
