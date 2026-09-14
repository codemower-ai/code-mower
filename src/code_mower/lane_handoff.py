"""Private takeover intents and control of Code Mower's existing supervisors.

No provider creates or process discovery live here. Remote cancellation uses
RemoteSessions; local cancellation asks the supervisor that owns the process
group to stop it. A dead/unreachable supervisor is uncertainty, not proof of exit.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Callable

from .context_store import ContextStore, strict_json
from .lane_delivery import Handoff, LaneDeliveryError
from .remote_session import DevinProvider, FakeProvider, RemoteSessions


def default_root() -> Path:
    return (Path.home() / ".local/share/code-mower/lane-handoffs").resolve()


def key(value: object) -> str:
    return "h" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:62]


def read_source(path: Path) -> dict:
    if any((parent / ".git").exists() for parent in path.resolve().parents):
        raise LaneDeliveryError("handoff source binding must stay outside Git")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 16384):
            raise LaneDeliveryError("handoff source must be a private operator-owned file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            source = strict_json(stream.read(16385))
        if not isinstance(source, dict):
            raise LaneDeliveryError("handoff source binding must be an object")
        expected = {"transport", "state_dir", "writer"}
        if source.get("transport") == "remote_session":
            expected = {"transport", "state_dir", "provider", "session"}
        elif source.get("transport") != "local_process":
            raise LaneDeliveryError("handoff source transport is unsupported")
        if set(source) != expected or any(not isinstance(v, str) or not v for v in source.values()):
            raise LaneDeliveryError("handoff source binding is invalid")
        if not Path(source["state_dir"]).is_absolute():
            raise LaneDeliveryError("handoff source state directory must be absolute")
        return source
    finally:
        os.close(fd)


class LocalWriter:
    """A private control mailbox for a known supervise_process invocation."""

    def __init__(self, root: Path, writer: str):
        self.store, self.key = ContextStore(root), key(writer)

    def register(self, *, repo: str, lane: str, checkout: Path) -> None:
        with self.store.locked(self.key) as locked:
            if locked.read() is not None:
                raise LaneDeliveryError("local writer already registered; do not repeat launch")
            locked.write({"schema": "code_mower.localWriter.v1", "repo": repo,
                          "lane": lane, "checkout": str(checkout.resolve()),
                          "stop_requested": False, "quiescent": False, "finished": False})

    def started(self, pid: int, pgid: int) -> None:
        with self.store.locked(self.key) as locked:
            record = locked.read()
            if record is None:
                raise LaneDeliveryError("local writer registration unavailable")
            record.update(pid=pid, pgid=pgid)
            locked.write(record)

    def stop_requested(self) -> bool:
        with self.store.locked(self.key) as locked:
            record = locked.read()
            return record is None or record.get("stop_requested") is not False

    def finish(self, *, quiescent: bool) -> None:
        with self.store.locked(self.key) as locked:
            record = locked.read()
            if record is not None:
                record.update(finished=True, quiescent=quiescent)
                locked.write(record)

    def stop(self, handoff: Handoff, *, timeout: float = 20) -> str:
        deadline = time.monotonic() + timeout
        while True:
            with self.store.locked(self.key) as locked:
                record = locked.read()
                if (record is None or record.get("schema") != "code_mower.localWriter.v1"
                        or record.get("repo", "").lower() != handoff.target_pr.split("#")[0].lower()
                        or record.get("lane") != handoff.source_lane):
                    raise LaneDeliveryError("local source writer binding mismatch")
                if record.get("stop_requested") is not True:
                    record["stop_requested"] = True
                    locked.write(record)
                if record.get("finished") is True and record.get("quiescent") is True:
                    checkout = Path(record["checkout"])
                    break
            if time.monotonic() >= deadline:
                raise LaneDeliveryError("source writer quiescence unverified; inspect the known supervisor")
            time.sleep(0.05)
        # A source may have committed locally without pushing while cancellation
        # was pending. That is head movement too, even when GitHub is unchanged.
        if checkout != checkout.resolve() or not (checkout / ".git").is_dir() or (checkout / ".git").is_symlink():
            raise LaneDeliveryError("source checkout unavailable")
        head = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], timeout=10, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        branch = subprocess.check_output(
            ["git", "-C", str(checkout), "branch", "--show-current"], timeout=10, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if head != handoff.expected_head or branch != handoff.target_branch:
            raise LaneDeliveryError("source checkout moved after the accepted head")
        return "terminated"


def remote_engine(source: dict) -> RemoteSessions:
    root = Path(source["state_dir"])
    if source["provider"] == "fake":
        provider = FakeProvider(root / "fake-provider")
    elif source["provider"] == "devin":
        from .devin_api import credentials_from_env
        from .devin_sessions import DevinClient
        credentials = credentials_from_env()
        if not credentials.has_credentials:
            raise LaneDeliveryError("source provider authentication unavailable")
        provider = DevinProvider(DevinClient(credentials.org_id, credentials.api_key))
    else:
        raise LaneDeliveryError("source provider is unsupported")
    return RemoteSessions(root, provider)


def quiesce(source: dict, handoff: Handoff, request: str) -> str:
    if source["transport"] == "local_process":
        return LocalWriter(Path(source["state_dir"]), source["writer"]).stop(handoff)
    if source["provider"] not in {"fake", handoff.source_lane}:
        raise LaneDeliveryError("source provider does not match source lane")
    return remote_engine(source).retire_writer(
        source["session"], repo=handoff.target_pr.split("#")[0], request=request,
    )


def observe_head(handoff: Handoff) -> str:
    repo, number = handoff.target_pr.split("#")
    result = subprocess.check_output(
        ["gh", "pr", "view", number, "--repo", repo, "--json", "headRefOid"],
        timeout=30, text=True, stderr=subprocess.DEVNULL,
    )
    return json.loads(result)["headRefOid"]


def prepare(handoff: Handoff, source: dict, root: Path, *,
            stop: Callable = quiesce, head: Callable = observe_head) -> dict:
    """Reserve before cancellation; replay cannot repeat acceptance or launch."""
    identity = key([handoff.target_pr.lower(), handoff.expected_head])
    fingerprint = key([handoff.as_dict(), source])
    with ContextStore(root).locked(identity) as locked:
        record = locked.read()
        if record is not None:
            if record.get("fingerprint") != fingerprint:
                raise LaneDeliveryError("handoff conflicts with an existing intent for this head")
            return {"accepted": record.get("accepted") is True, "duplicate": True,
                    "owner_action": record.get("accepted") is not True, "notify": False,
                    "launch_allowed": False, "handoff": handoff.as_dict()}
        record = {"fingerprint": fingerprint, "accepted": False, "launch_reserved": False,
                  "handoff": handoff.as_dict(), "source": source}
        locked.write(record)
        try:
            writer = stop(source, handoff, identity)
            if writer not in {"suspended", "terminated"} or head(handoff) != handoff.expected_head:
                raise LaneDeliveryError("source writer or exact head could not be verified")
        except Exception:
            # The durable intent makes a failed/uncertain cancellation one owner
            # action. A retry must not repeat it or fabricate an acceptance.
            return {"accepted": False, "duplicate": False, "owner_action": True,
                    "notify": True, "launch_allowed": False, "handoff": handoff.as_dict()}
        record.update(accepted=True, writer_state=writer)
        locked.write(record)
        return {"accepted": True, "duplicate": False, "owner_action": False,
                "notify": True, "launch_allowed": True, "handoff": handoff.as_dict()}


def reserve_launch(handoff: Handoff, root: Path, *, head: Callable = observe_head,
                   stop: Callable = quiesce) -> bool:
    identity = key([handoff.target_pr.lower(), handoff.expected_head])
    with ContextStore(root).locked(identity) as locked:
        record = locked.read()
        if (record is None or record.get("accepted") is not True
                or record.get("handoff") != handoff.as_dict()):
            raise LaneDeliveryError("handoff has no verified acceptance")
        if record.get("launch_reserved") is True:
            return False
        if (stop(record["source"], handoff, identity) not in {"suspended", "terminated"}
                or head(handoff) != handoff.expected_head):
            raise LaneDeliveryError("handoff head moved before destination launch")
        record["launch_reserved"] = True
        locked.write(record)
        return True
