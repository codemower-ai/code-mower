"""``code-mower context-graph``: build, refresh, inspect and remove a local graph.

The lifecycle in ``context_graph_lifecycle`` is deliberately not wired into any
default path. This command is how an operator opts in, one checkout at a time,
and it asks for everything explicitly rather than discovering it: the provider
pin comes from a file the operator names, and the indexer executable comes from
an install the operator already made. Nothing here downloads, installs, or
resolves a provider.

Output is metadata only -- revisions, digests, counts, and states. No indexed
content, provider output, or local path of the private state directory is
printed unless the operator asks for it with ``--show-local-paths``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import context_graph_lifecycle as lifecycle
from .context_contract import ContextError
from .context_store import strict_json

MAX_PIN_BYTES = 8192


def _load_pin(path: Path | None) -> lifecycle.GraphifyPin | None:
    if path is None:
        return None
    try:
        # Bounded at the stream, not after the fact: a bound checked on bytes
        # already in memory is not a bound on what the file can cost to read.
        with path.open("rb") as stream:
            raw = stream.read(MAX_PIN_BYTES + 1)
    except OSError:
        raise ContextError("local graph provider pin file is unreadable") from None
    if len(raw) > MAX_PIN_BYTES:
        raise ContextError("local graph provider pin file exceeds its bound")
    return lifecycle.load_pin(strict_json(raw))


def _require_pin(path: Path | None) -> lifecycle.GraphifyPin:
    pin = _load_pin(path)
    if pin is None:
        raise ContextError("building a local graph requires an exact provider pin")
    return pin


def _emit(payload: dict, *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(text, end="")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="code-mower context-graph",
        description="Manage an optional revision-bound local repository graph.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build and publish a generation for the current revision")
    refresh = sub.add_parser("refresh", help="Explicitly rebuild and atomically publish a new generation")
    status = sub.add_parser("status", help="Report whether the published generation may be used")
    remove = sub.add_parser("remove", help="Delete this checkout's private local graph state")
    doctor = sub.add_parser("doctor", help="Check the local graph posture without building anything")

    for command in (build, refresh, status, remove, doctor):
        command.add_argument("--repo-path", type=Path, default=Path.cwd(), help="Checkout to bind")
        command.add_argument("--state-dir", type=Path, help="Private state root; defaults to the context store")
        command.add_argument("--json", action="store_true", help="Emit a machine-readable summary")
    for command in (build, refresh, status, doctor):
        command.add_argument("--revision", default="HEAD", help="Revision to bind, for example a commit or tag")
    for command in (build, refresh, doctor):
        command.add_argument("--pin-file", type=Path, help="JSON file naming one exact provider release")
    for command in (build, refresh):
        command.add_argument("--indexer", required=True, help="Path to the already-installed pinned provider CLI")
        command.add_argument("--keep-previous", action="store_true",
                             help="Retain superseded generations instead of pruning them")
    status.add_argument("--allow-partial", action="store_true",
                        help="Treat a provider-declared partial build as usable")
    remove.add_argument("--show-local-paths", action="store_true", help="Include the private state path in output")

    args = parser.parse_args(argv)
    try:
        if args.command in ("build", "refresh"):
            pin = _require_pin(args.pin_file)
            if args.command == "build" and lifecycle.graph_status(
                args.repo_path, root=args.state_dir, revision=args.revision
            ).usable:
                # ``build`` is the first-time verb. A usable generation already
                # binds this revision, so rebuilding it is ``refresh`` -- an
                # explicit choice, never something ``build`` does by surprise.
                raise ContextError("a current generation already binds this revision; use refresh to rebuild")
            manifest = lifecycle.build_graph(
                args.repo_path,
                pin=pin,
                indexer=lifecycle.subprocess_indexer(args.indexer),
                root=args.state_dir,
                revision=args.revision,
                keep_previous=args.keep_previous,
            )
            summary = {"status": "published", **manifest.shareable_summary()}
            _emit(summary, as_json=args.json,
                  text=lifecycle.render_status_text(
                      lifecycle.GenerationStatus(state="current", generation=manifest.generation, manifest=manifest)))
            return 0
        if args.command == "status":
            report = lifecycle.graph_status(
                args.repo_path,
                root=args.state_dir,
                revision=args.revision,
                require_complete=not args.allow_partial,
            )
            _emit(report.shareable_summary(), as_json=args.json, text=lifecycle.render_status_text(report))
            # A non-current graph is a normal, reportable condition, not a
            # command failure; exit 1 so a script can branch on usability.
            return 0 if report.usable else 1
        if args.command == "remove":
            state = lifecycle.GraphStateRoot(args.repo_path, root=args.state_dir)
            path = str(state.path) if args.show_local_paths else None
            removed = lifecycle.remove_graph(args.repo_path, root=args.state_dir)
            payload = {"schema": "code_mower.contextGraphRemove.v1", "removed": removed}
            if path is not None:
                payload["path"] = path
            _emit(payload, as_json=args.json,
                  text=("Removed local graph state.\n" if removed else "No local graph state to remove.\n"))
            return 0
        report = lifecycle.doctor_report(
            args.repo_path, pin=_load_pin(args.pin_file), root=args.state_dir, revision=args.revision
        )
        lines = [f"Local graph doctor: {report['status']}"]
        lines.extend(f"  [{check['status']}] {check['check']}: {check['message']}" for check in report["checks"])
        _emit(report, as_json=args.json, text="\n".join(lines) + "\n")
        return 0 if report["status"] != "fail" else 1
    except ContextError as error:
        print(f"local graph unavailable: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Never let a provider or filesystem failure surface indexed content.
        print("local graph unavailable; verify the pin, the checkout and the private state directory", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - direct invocation
    raise SystemExit(main())
