#!/usr/bin/env python3
"""Shared local audit handoff log and active-lock helper.

The audit lanes are intentionally human-orchestrated: Claude, Codex, and
occasionally paid providers can all be asked to review the same PR. This
helper gives local agents a durable "someone is already doing this" check
without depending on chat memory.

Default storage lives outside the repo to avoid dirty worktrees:

    ~/.cache/cube-agent-audits/events.jsonl
    ~/.cache/cube-agent-audits/locks/*.json

Set AUDIT_HANDOFF_LOG_DIR to override the directory in tests or local
experiments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8+ in normal use
    ZoneInfo = None  # type: ignore[assignment]


DEFAULT_DIR = Path("~/.cache/cube-agent-audits").expanduser()
EVENTS_FILE = "events.jsonl"
LOCKS_DIR = "locks"
ACTIVE_EXIT_CODE = 20


def audit_dir() -> Path:
    return Path(os.environ.get("AUDIT_HANDOFF_LOG_DIR", str(DEFAULT_DIR))).expanduser()


def now_record() -> Dict[str, str]:
    # Microsecond precision on `utc` (cube-snap#TBD / ctvd#TBD).
    # Previously second-precision, which was sufficient for human-
    # readable display but caused a same-second race in the sweep-hook
    # cross-check (cube-snap#204 P3, fixed via array-index ordering).
    # The display field (`pt`) stays at second precision because the
    # session-start sweep header renders it directly to the operator,
    # and microseconds add noise without value at that surface.
    now_utc = datetime.now(timezone.utc)
    record = {"utc": now_utc.isoformat(timespec="microseconds")}
    try:
        if ZoneInfo is not None:
            record["pt"] = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S PT")
        else:
            record["pt"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S PT")
    except Exception:
        record["pt"] = record["utc"]
    return record


def display_time(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("pt") or value.get("utc") or value)
    return str(value)


def _safe_key_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "unknown"


def lock_id_for(*, lane: str, repo: str, pr: int, head: str) -> str:
    short_head = head[:12] if head and head != "unknown" else "unknown"
    return "__".join(
        (
            _safe_key_part(lane),
            _safe_key_part(repo),
            f"pr{pr}",
            _safe_key_part(short_head),
        )
    )


def lock_path(lock_id: str) -> Path:
    return audit_dir() / LOCKS_DIR / f"{lock_id}.json"


def events_path() -> Path:
    return audit_dir() / EVENTS_FILE


def ensure_dirs() -> None:
    (audit_dir() / LOCKS_DIR).mkdir(parents=True, exist_ok=True)


def append_event(event: Dict[str, Any]) -> None:
    ensure_dirs()
    enriched = {"time": now_record(), **event}
    with events_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(enriched, sort_keys=True) + "\n")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def process_alive(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resolve_pr_head(repo: str, pr: int) -> str:
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def start_lock(args: argparse.Namespace) -> int:
    ensure_dirs()
    head = args.head or resolve_pr_head(args.repo, args.pr)
    lock_id = lock_id_for(lane=args.lane, repo=args.repo, pr=args.pr, head=head)
    path = lock_path(lock_id)

    if path.exists():
        existing = load_json(path)
        if process_alive(existing.get("pid")):
            print(
                (
                    "active audit already running: "
                    f"{existing.get('lane')} {existing.get('repo')}#{existing.get('pr')} "
                    f"@ {str(existing.get('head', 'unknown'))[:12]} "
                    f"pid={existing.get('pid')} actor={existing.get('actor')} "
                    f"started={display_time(existing.get('started'))}"
                ),
                file=sys.stderr,
            )
            append_event({"event": "duplicate_refused", "lockId": lock_id, "active": existing})
            return ACTIVE_EXIT_CODE
        append_event({"event": "stale_lock_reaped", "lockId": lock_id, "stale": existing})
        path.unlink()

    record = {
        "lockId": lock_id,
        "event": "started",
        "lane": args.lane,
        "repo": args.repo,
        "pr": args.pr,
        "head": head,
        "actor": args.actor,
        "trigger": args.trigger,
        "pid": args.pid,
        "cwd": args.cwd,
        "command": args.command,
        "started": now_record(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags)
    except FileExistsError:
        # Another process won the race after our existence check.
        return start_lock(args)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True)
        fh.write("\n")
    append_event(record)
    print(lock_id)
    return 0


def finish_lock(args: argparse.Namespace) -> int:
    path = lock_path(args.lock_id)
    record: Dict[str, Any] = {"lockId": args.lock_id}
    if path.exists():
        record.update(load_json(path))
        path.unlink()
    append_event(
        {
            "event": "finished",
            "lockId": args.lock_id,
            "status": args.status,
            "exitCode": args.exit_code,
            "lock": record,
        }
    )
    return 0


def record_event(args: argparse.Namespace) -> int:
    """Append a single event to the shared log without acquiring a lock.

    Use case: post-hoc recording of work that's already done — e.g. Claude
    or Codex finishing a manual cross-review and wanting the other agent's
    Monitor on the log to notice in real time. Reviews aren't long-running
    operations that need duplicate-prevention locking like audits do, so we
    skip the lock dance and just write the event.

    The event shape mirrors what `finish_lock` writes (`"event": "finished"`)
    so a Monitor filter like `grep '"event": "finished"'` catches both
    audit-finished and review-finished events transparently. The `lane`
    field disambiguates (`codex-audit` vs `claude-review` vs
    `codex-review`).
    """
    head = args.head or resolve_pr_head(args.repo, args.pr)
    event: Dict[str, Any] = {
        "event": args.event,
        "lane": args.lane,
        "repo": args.repo,
        "pr": args.pr,
        "head": head,
        "actor": args.actor,
    }
    if args.verdict is not None:
        event["verdict"] = args.verdict
    if args.notes:
        event["notes"] = args.notes
    append_event(event)
    return 0


def active_locks() -> Iterable[Dict[str, Any]]:
    locks_dir = audit_dir() / LOCKS_DIR
    if not locks_dir.exists():
        return []
    active = []
    for path in sorted(locks_dir.glob("*.json")):
        record = load_json(path)
        if process_alive(record.get("pid")):
            active.append(record)
        else:
            append_event({"event": "stale_lock_reaped", "lockId": record.get("lockId", path.stem), "stale": record})
            path.unlink()
    return active


def status(args: argparse.Namespace) -> int:
    rows = []
    for record in active_locks():
        if args.lane and record.get("lane") != args.lane:
            continue
        if args.repo and record.get("repo") != args.repo:
            continue
        if args.pr is not None and int(record.get("pr", -1)) != args.pr:
            continue
        rows.append(record)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("no active audit locks")
        return 0
    for record in rows:
        print(
            f"{record.get('lane')} {record.get('repo')}#{record.get('pr')} "
            f"@ {str(record.get('head', 'unknown'))[:12]} "
            f"pid={record.get('pid')} actor={record.get('actor')} "
            f"started={display_time(record.get('started'))}"
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Create an active audit lock and log a start event.")
    start.add_argument("--lane", required=True)
    start.add_argument("--repo", required=True)
    start.add_argument("--pr", type=int, required=True)
    start.add_argument("--head")
    start.add_argument("--actor", default=os.environ.get("AUDIT_ACTOR") or os.environ.get("USER") or "unknown")
    start.add_argument("--trigger", default="manual")
    start.add_argument("--pid", type=int, default=os.getpid())
    start.add_argument("--cwd", default=os.getcwd())
    start.add_argument("--command", default="")
    start.set_defaults(func=start_lock)

    finish = sub.add_parser("finish", help="Remove an active audit lock and log a finish event.")
    finish.add_argument("--lock-id", required=True)
    finish.add_argument("--status", required=True)
    finish.add_argument("--exit-code", type=int, required=True)
    finish.set_defaults(func=finish_lock)

    stat = sub.add_parser("status", help="List active audit locks.")
    stat.add_argument("--lane")
    stat.add_argument("--repo")
    stat.add_argument("--pr", type=int)
    stat.add_argument("--json", action="store_true")
    stat.set_defaults(func=status)

    rec = sub.add_parser(
        "record",
        help="Append a single event (no lock). For lock-free, post-hoc "
             "recording of completed work like manual reviews.",
    )
    rec.add_argument("--lane", required=True,
                     help="e.g. claude-review, codex-review, codex-audit")
    rec.add_argument("--repo", required=True)
    rec.add_argument("--pr", type=int, required=True)
    rec.add_argument("--head",
                     help="Head SHA at the time of the event. If omitted, "
                          "fetched from gh api.")
    rec.add_argument("--event", required=True,
                     help="Event kind, typically 'finished'. Monitor filters "
                          "match on this field.")
    rec.add_argument("--verdict",
                     help="e.g. pass, blocked. Free-form; not interpreted.")
    rec.add_argument("--notes",
                     help="Free-form note to attach to the event.")
    rec.add_argument("--actor",
                     default=os.environ.get("AUDIT_ACTOR")
                             or os.environ.get("USER") or "unknown")
    rec.set_defaults(func=record_event)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
