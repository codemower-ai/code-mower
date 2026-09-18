"""Explicit optional Slack preparation; default installation never calls this."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import slack_readiness

MANIFEST = Path(__file__).parent / "templates" / "slack" / "hosted-app-manifest.json"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally reflects unrecognized arguments, including secrets.
        self.exit(2, "Slack command arguments are invalid; use --help.\n")


def main(argv=None):
    parser = Parser(prog="code-mower slack", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Prepare an optional hosted app manifest; no remote changes.")
    setup.add_argument("--manifest", type=Path, required=True, help="New file to create; existing files are never overwritten.")
    consent = setup.add_mutually_exclusive_group(required=True)
    consent.add_argument("--yes", action="store_true", help="Explicitly opt in to local preparation.")
    consent.add_argument("--interactive", action="store_true", help="Confirm local preparation interactively.")
    doctor = commands.add_parser("doctor", help="Read-only, redacted Slack readiness; no dispatch.")
    source = doctor.add_mutually_exclusive_group()
    source.add_argument("--probe", type=Path, help="Explicit trusted private host executable implementing the read-only probe protocol.")
    source.add_argument("--snapshot", type=Path, help="Inspect a bounded offline observation; never establishes live readiness.")
    doctor.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        result = (slack_readiness.probe(args.probe) if args.probe else
                  slack_readiness.read_snapshot(args.snapshot) if args.snapshot else
                  slack_readiness.report())
        print(json.dumps(result, sort_keys=True) if args.json else slack_readiness.render(result))
        return 0 if result["ready"] else 1
    try:
        if args.interactive:
            if not sys.stdin.isatty():
                print("Interactive setup requires a terminal; scripted opt-in uses --yes.")
                return 1
            if input("Prepare optional Slack setup for one private workspace? [y/N] ").strip().lower() != "y":
                print("Slack setup not prepared.")
                return 0
        raw = MANIFEST.read_bytes()
        # Exclusive creation also rejects symlinks. The manifest contains only
        # public routes and static scope configuration, never installation data.
        fd = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
    except (OSError, EOFError):
        print("Slack setup could not create a new manifest; choose an unused file in an existing writable directory.")
        return 1
    print("Optional Slack manifest prepared. Follow docs/slack-setup.md for administrator OAuth, immutable policy, supervision and caps. No service or login was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
