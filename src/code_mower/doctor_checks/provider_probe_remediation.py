"""Provider auth smoke-probe remediation text."""

from __future__ import annotations

from typing import Any, Mapping


def local_cli_probe_remediation(
    command: str,
    probe_args: tuple[str, ...],
    lane: Mapping[str, Any],
    *,
    auth_error_detected: bool = False,
) -> str:
    provider = str(lane.get("provider") or "").strip()
    if auth_error_detected and provider == "claude":
        return (
            "Run `claude auth status`, then run "
            "`claude -p \"Reply with exactly: ok\" --output-format json`. "
            "If status says logged in but the prompt returns 401, run "
            "`code-mower claude-bounce --json` first to distinguish inherited "
            "environment issues from stale local credentials. If clean_env also "
            "returns 401, refresh Claude Code OAuth by removing stale local "
            "Claude credentials and running `claude` to log in again. For "
            "automation, prefer a provider/API-key credential path over "
            "interactive Claude.ai OAuth."
        )
    if auth_error_detected:
        return (
            f"Run `{command} {' '.join(probe_args)}` manually, refresh provider "
            "auth credentials or API keys, then rerun doctor --probe-runtime."
        )
    return (
        f"Run `{command} {' '.join(probe_args)}` manually, fix CLI "
        "installation or auth, then rerun doctor --probe-runtime."
    )


__all__ = ("local_cli_probe_remediation",)
