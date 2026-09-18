"""Closed, read-only Slack operator diagnostics. Never an execution grant.

The private host owns authentication and immutable policy resolution. Its probe
must observe that authority afresh; a registration or an uploaded report cannot
stand in for a reachable qualified supervisor. No credential discovery here.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import secrets
import selectors
import signal
import stat
import subprocess
import time

from .context_store import strict_json
from .supervisor_contract_v2 import SCHEMA as SUPERVISOR_SCHEMA

SCHEMA = "code_mower.slack_readiness.v1"
PROBE_SCHEMA = "code_mower.slack_probe.v1"
MAX_BYTES = 16384
PROBE_TIMEOUT = 5
MAX_AGE = 120

# Every diagnostic leaf is selected here, never interpolated from private input.
COMPONENTS = {
    "ingress": ("enabled", {"disabled", "unreachable"},
        "Verify owner-authorized control-plane and interaction enablement after the private logging gate."),
    "bridge": ("enabled", {"disabled", "unreachable", "mismatched"},
        "Verify explicit hosted bridge and host composition enablement with the qualified immutable package."),
    "manifest": ("matched", {"missing", "mismatched"},
        "Import the hosted manifest; preserve commands-only scopes, rotation and the fixed routes."),
    "installation": ("active", {"missing", "disabled", "revoked", "expired", "uncertain"},
        "Have an authorized administrator inspect installation health and reconnect through OAuth if needed; do not retry an uncertain rotation."),
    "oauth": ("configured", {"missing", "mismatched", "revoked"},
        "Verify the app, workspace, redirect and rotating bot credentials privately in the installation service."),
    "identity": ("bound", {"missing", "revoked", "mismatched"},
        "Bind the exact workspace user to a current member; remove and recreate a changed mapping."),
    "repository": ("authorized", {"missing", "revoked", "mismatched"},
        "Bind the repository from the authorized catalog; remove and recreate a changed alias binding."),
    "channel": ("private_verified", {"missing", "revoked", "stale", "mismatched", "shared", "public"},
        "Verify the exact private, unshared channel in the installed workspace and renew its policy within one hour."),
    "registration": ("configured", {"missing", "disabled", "stale", "mismatched"},
        "Register the maintained v2 orchestrator explicitly; registration alone does not establish reachability."),
    "supervisor": ("qualified_reachable", {"unqualified", "unreachable", "stale", "revoked", "mismatched"},
        "Restore the qualified supervisor connection and fresh heartbeat; reconcile any existing claim before allowing dispatch."),
    "transport": ("ready", {"missing", "disabled", "revoked", "unreachable", "unsupported", "uncertain"},
        "Check the hosted Devin transport and its scoped authorization privately without creating a session; reconcile uncertain work before any retry."),
    "campaign": ("active", {"missing", "disabled", "expired", "revoked", "mismatched"},
        "Record an explicit unexpired owner-approved campaign with task, aggregate, runtime and review limits."),
}
CAP_KEYS = {"task_acu", "campaign_acu", "reserved_acu", "task_limit", "reserved_tasks",
            "runtime_calls", "runtime_seconds", "review_rounds", "review_budget_usd",
            "clarification_answers", "fix_requests", "recovery_creates"}


class ReadinessError(ValueError):
    """Only fixed error codes may cross this boundary."""


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 10**12


def decode(raw: bytes) -> dict:
    try:
        if type(raw) is not bytes or len(raw) > MAX_BYTES:
            raise ValueError
        value = strict_json(raw)
        if (type(value) is not dict or set(value) != {
            "schema", "nonce", "observed_at", "expires_at", "components",
            "supervisor_product", "supervisor_contract", "caps",
        } or value["schema"] != PROBE_SCHEMA):
            raise ValueError
        nonce = value["nonce"]
        if (type(nonce) is not str or len(nonce) != 64
                or any(c not in "0123456789abcdef" for c in nonce)):
            raise ValueError
        if any(not _number(value[k]) for k in ("observed_at", "expires_at")):
            raise ValueError
        components = value["components"]
        if type(components) is not dict or set(components) != set(COMPONENTS):
            raise ValueError
        for key, (ready, failures, _) in COMPONENTS.items():
            if type(components[key]) is not str or components[key] not in {ready, *failures}:
                raise ValueError
        if (value["supervisor_product"] not in ("codex", "claude", "devin", "none")
                or value["supervisor_contract"] not in (SUPERVISOR_SCHEMA, "unsupported", "none")):
            raise ValueError
        caps = value["caps"]
        if type(caps) is not dict or set(caps) != CAP_KEYS or any(not _number(v) for v in caps.values()):
            raise ValueError
        for key in CAP_KEYS - {"review_budget_usd"}:
            if type(caps[key]) is not int:
                raise ValueError
        return value
    except Exception:
        raise ReadinessError("invalid_observation") from None


def _check(name, state, passed, remediation):
    return {"name": name, "state": state, "status": "pass" if passed else "fail",
            "remediation": "" if passed else remediation}


def report(observation: dict | None = None, *, now=None, live=False, error=None) -> dict:
    """Project safe facts, not identities, caps, nonces, times or provider output.

    Even an all-green live report is advisory: the supervisor must independently
    reauthorize at execution. Offline snapshots never establish live readiness.
    """
    checks = []
    if error not in {None, "probe_required", "probe_failed", "probe_timeout", "invalid_observation",
                     "probe_mismatch", "snapshot_unavailable"}:
        error = "invalid_observation"
    if observation is not None:
        try:
            observation = decode(json.dumps(observation, allow_nan=False).encode())
        except Exception:
            observation, error = None, "invalid_observation"
    if observation is None:
        checks.append(_check("observation", error or "probe_required", False,
            "Use the trusted private host's read-only readiness probe; never paste private admin or provider responses into diagnostics."))
        for name, (_, _, remediation) in COMPONENTS.items():
            checks.append(_check(name, "not_observed", False, remediation))
        checks.append(_check("budgets", "not_observed", False,
            "Verify the owner-approved task and aggregate campaign caps on the private host."))
    else:
        now = time.time() if now is None else now
        observed, expires = observation["observed_at"], observation["expires_at"]
        fresh = (_number(now) and 0 <= now - observed < MAX_AGE
                 and observed < expires <= observed + MAX_AGE and now < expires)
        checks.append(_check("observation", "fresh" if fresh else "stale", fresh,
            "Obtain a fresh observation from the same authorized private host; do not reuse a saved readiness result."))
        for name, (ready, _, remediation) in COMPONENTS.items():
            state = observation["components"][name]
            checks.append(_check(name, state, state == ready, remediation))
        qualified = (observation["supervisor_product"] == "codex"
                     and observation["supervisor_contract"] == SUPERVISOR_SCHEMA)
        checks.append(_check("supervisor_contract", "supported" if qualified else "unqualified", qualified,
            "Use the currently maintained Codex supervisor v2 adapter; a Claude registration or hosted Devin builder does not qualify that adapter."))
        caps = observation["caps"]
        configured = all(caps[k] > 0 for k in (
            "task_acu", "campaign_acu", "task_limit", "runtime_calls", "runtime_seconds",
            "review_rounds", "review_budget_usd", "clarification_answers"))
        bounded = (configured and caps["task_acu"] <= caps["campaign_acu"]
                   and 1 <= caps["task_acu"] <= 100 and 1 <= caps["task_limit"] <= 50
                   and 2 <= caps["runtime_calls"] <= 32 and 1 <= caps["runtime_seconds"] <= 300
                   and 1 <= caps["review_rounds"] <= 9 and 1 <= caps["clarification_answers"] <= 32
                   and 0 <= caps["fix_requests"] <= 8 and caps["recovery_creates"] == 0)
        available = (bounded and caps["reserved_acu"] + caps["task_acu"] <= caps["campaign_acu"]
                     and caps["reserved_tasks"] < caps["task_limit"])
        state = ("missing" if not configured else "mismatched" if not bounded
                 else "available" if available else "exhausted")
        checks.append(_check("budgets", state, available,
            "Configure positive task and campaign ACU caps, task count, runtime and review ceilings with zero recovery creates; retain reservations after cancellation or uncertain billing."))
    if not live:
        checks.append(_check("live_probe", "not_observed", False,
            "Run the trusted host probe explicitly; an offline snapshot is not proof of current reachability."))
    return {"schema": SCHEMA, "basis": "live_probe" if live else "offline",
            "ready": all(c["status"] == "pass" for c in checks),
            "dispatch_authorized": False, "checks": checks}


def probe(executable: Path) -> dict:
    """One explicitly selected trusted host executable, no shell/retry/log files.

    This is an operator trust boundary, not a sandbox for arbitrary commands.
    The host supplies credentials itself and must implement read-only inspection.
    Bound both pipes together and the process group, including inherited pipes.
    """
    process = None
    nonce = secrets.token_hex(32)
    started = time.time()
    deadline = time.monotonic() + PROBE_TIMEOUT
    try:
        info = executable.stat()
        if (not executable.is_absolute() or not stat.S_ISREG(info.st_mode)
                or not os.access(executable, os.X_OK)):
            raise ReadinessError("probe_failed")
        process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        request = {"schema": PROBE_SCHEMA, "nonce": nonce}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.close()
        output = bytearray()
        size = 0
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadinessError("probe_timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, MAX_BYTES + 1)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ReadinessError("invalid_observation")
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadinessError("probe_timeout")
            if process.wait(timeout=remaining) != 0:
                raise ReadinessError("probe_failed")
        value = decode(bytes(output))
        if value["nonce"] != nonce or value["observed_at"] < started:
            raise ReadinessError("probe_mismatch")
        return report(value, live=True)
    except subprocess.TimeoutExpired:
        return report(live=True, error="probe_timeout")
    except ReadinessError as exc:
        return report(live=True, error=str(exc))
    except Exception:
        return report(live=True, error="probe_failed")
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def read_snapshot(path: Path) -> dict:
    """Read bounded regular files without following a final symlink or a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ReadinessError("snapshot_unavailable")
            return report(decode(stream.read(MAX_BYTES + 1)))
    except ReadinessError as exc:
        return report(error=str(exc))
    except Exception:
        return report(error="snapshot_unavailable")


def render(value: dict) -> str:
    lines = ["Slack readiness: " + ("observed ready" if value["ready"] else "not ready"),
             "Advisory only; execution requires fresh supervisor authorization."]
    for check in value["checks"]:
        lines.append(f"{check['status']}: {check['name']}: {check['state']}")
        if check["remediation"]:
            lines.append("  " + check["remediation"])
    return "\n".join(lines)
