"""Bounded authentication readiness probes for local campaign adapters.

A maintained local adapter can hold an installed CLI and a valid argv contract
while the isolated provider home it actually runs under holds no login. Applied
dispatch then fails with a generic adapter error after paid work has started.

Where a provider exposes a safe, read-only login-status command, doctor runs it
in exactly the environment the adapter builds (for Codex: the isolated
``CODEX_HOME`` plus the real OS ``HOME`` needed to reach the platform keyring)
and reports a bounded owner action instead. Providers without such a command
stay capability-only: no probe runs and readiness is never guessed.

A nonzero exit alone is never evidence of a missing login: an older or newer
CLI without the subcommand, a broken keyring backend, or a transient config
error all exit nonzero while the owner is in fact logged in. Only a provider-
configured combination of an expected logged-out exit code and a narrowly
allowlisted logged-out output marker is reported as unauthenticated; every
other nonzero exit degrades to the non-blocking probe-unavailable skip and
leaves the provider campaign-ready.

Probe stdout/stderr is never persisted, and the logged-out marker match is
performed transiently on a bounded prefix of that output. Only the bounded
state word, a registered error code, and non-content output shape reach doctor
JSON, so account names, tokens, credential contents, and local paths cannot
leak.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Mapping, Sequence

from .models import STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck
from .privacy import auth_probe_output_detail


CAMPAIGN_AUTH_CHECK_NAME = "doctor.campaign.auth"

#: ``provider_config`` keys describing one provider's safe login-status probe.
CAMPAIGN_AUTH_PROBE_ARGS_KEY = "campaign_auth_probe_args"
CAMPAIGN_AUTH_PROBE_TIMEOUT_KEY = "campaign_auth_probe_timeout_seconds"
CAMPAIGN_AUTH_LOGGED_OUT_EXIT_CODES_KEY = "campaign_auth_logged_out_exit_codes"
CAMPAIGN_AUTH_LOGGED_OUT_MARKERS_KEY = "campaign_auth_logged_out_markers"
DEFAULT_CAMPAIGN_AUTH_PROBE_TIMEOUT_SECONDS = 20

#: Only this many characters of probe output are inspected for a logged-out
#: marker. The text is matched in memory and never stored or emitted.
CAMPAIGN_AUTH_MARKER_SCAN_LIMIT = 4096

#: Set to 0/false/no/off to leave every local adapter capability-only.
CAMPAIGN_AUTH_PROBE_ENV = "CODE_MOWER_CAMPAIGN_AUTH_PROBE"

AUTH_STATE_AUTHENTICATED = "authenticated"
AUTH_STATE_UNAUTHENTICATED = "unauthenticated"
AUTH_STATE_UNKNOWN = "unknown"
AUTH_STATE_SKIPPED = "skipped"

#: Bounded, non-secret probe error codes. A probe result may only ever carry
#: one of these in ``error`` -- never provider output or an exception message.
AUTH_ERROR_UNAUTHENTICATED = "campaign_auth_unauthenticated"
AUTH_ERROR_PROBE_TIMEOUT = "campaign_auth_probe_timeout"
AUTH_ERROR_PROBE_UNAVAILABLE = "campaign_auth_probe_unavailable"

#: ``(argv, timeout_seconds, child_env) -> CompletedProcess``. Never a shell.
CampaignAuthProbeRunner = Callable[
    [Sequence[str], int, Mapping[str, str]],
    "subprocess.CompletedProcess[str]",
]


def run_campaign_auth_probe(
    argv: Sequence[str],
    timeout_seconds: int,
    child_env: Mapping[str, str],
) -> "subprocess.CompletedProcess[str]":
    """Default argv-only probe runner with an explicit minimal environment."""
    return subprocess.run(
        list(argv),
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        env=dict(child_env),
    )


def campaign_auth_probe_args(lane: Any) -> tuple[str, ...]:
    """Return one lane's safe login-status argv tail, or () when it has none."""
    provider_config = getattr(lane, "provider_config", None)
    if not isinstance(provider_config, Mapping):
        return ()
    raw = provider_config.get(CAMPAIGN_AUTH_PROBE_ARGS_KEY)
    if not isinstance(raw, (list, tuple)) or not raw:
        return ()
    return tuple(str(part) for part in raw)


def campaign_auth_probe_timeout(lane: Any) -> int:
    """Return the bounded probe timeout in seconds."""
    provider_config = getattr(lane, "provider_config", None)
    raw = (
        provider_config.get(CAMPAIGN_AUTH_PROBE_TIMEOUT_KEY)
        if isinstance(provider_config, Mapping)
        else None
    )
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CAMPAIGN_AUTH_PROBE_TIMEOUT_SECONDS
    return max(1, timeout)


def campaign_auth_logged_out_exit_codes(lane: Any) -> tuple[int, ...]:
    """Return the exit codes one lane uses to report a logged-out home."""
    provider_config = getattr(lane, "provider_config", None)
    if not isinstance(provider_config, Mapping):
        return ()
    raw = provider_config.get(CAMPAIGN_AUTH_LOGGED_OUT_EXIT_CODES_KEY)
    if not isinstance(raw, (list, tuple)):
        return ()
    codes: list[int] = []
    for part in raw:
        try:
            codes.append(int(part))
        except (TypeError, ValueError):
            continue
    return tuple(codes)


def campaign_auth_logged_out_markers(lane: Any) -> tuple[str, ...]:
    """Return one lane's allowlisted logged-out output markers, lowercased."""
    provider_config = getattr(lane, "provider_config", None)
    if not isinstance(provider_config, Mapping):
        return ()
    raw = provider_config.get(CAMPAIGN_AUTH_LOGGED_OUT_MARKERS_KEY)
    if not isinstance(raw, (list, tuple)):
        return ()
    markers = tuple(str(part).strip().lower() for part in raw)
    return tuple(marker for marker in markers if marker)


def campaign_auth_confirmed_logged_out(lane: Any, returncode: int, output: str) -> bool:
    """Return whether a nonzero probe result is a confirmed logged-out home.

    Both signals must be declared by the provider and both must match: the
    expected logged-out exit code, and one narrowly allowlisted logged-out
    marker in a bounded prefix of the probe output. Anything else -- an
    unsupported subcommand, a keyring or config failure, unexpected output --
    is a probe failure, not proof that the owner is logged out.
    """
    exit_codes = campaign_auth_logged_out_exit_codes(lane)
    markers = campaign_auth_logged_out_markers(lane)
    if not exit_codes or not markers or returncode not in exit_codes:
        return False
    haystack = output[:CAMPAIGN_AUTH_MARKER_SCAN_LIMIT].lower()
    return any(marker in haystack for marker in markers)


def campaign_auth_probe_requested(env: Mapping[str, str] | None = None) -> bool:
    """Return whether auth probing is enabled for this doctor run."""
    current_env = os.environ if env is None else env
    value = str(current_env.get(CAMPAIGN_AUTH_PROBE_ENV, "")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def campaign_auth_probe_env(provider: str) -> tuple[dict[str, str], str]:
    """Return the adapter's own child environment, or a bounded error code.

    The probe must observe the same isolated provider home the adapter uses,
    so it reuses the adapter's environment builder rather than a copy of it.
    """
    from code_mower.campaign_adapters import (
        build_adapter_child_env,
        prepare_codex_campaign_home,
    )

    try:
        codex_home = prepare_codex_campaign_home() if provider == "codex" else None
        return build_adapter_child_env(provider, codex_home=codex_home), ""
    except (OSError, ValueError):
        return {}, AUTH_ERROR_PROBE_UNAVAILABLE


def _remediation(canonical: str, state: str) -> str:
    if state == AUTH_STATE_UNAUTHENTICATED:
        return (
            f"Authenticate the isolated {canonical} campaign home once using the "
            "provider login command in docs/release-qualification.md "
            "(Provider Adapter Setup), then re-run `code-mower doctor --adoption`."
        )
    return (
        f"Could not verify {canonical} campaign authentication; verify the "
        f"{canonical} login yourself before dispatching a campaign, or set "
        f"{CAMPAIGN_AUTH_PROBE_ENV}=0 to leave this lane capability-only."
    )


def _detail(
    *,
    canonical: str,
    lane: Any,
    state: str,
    enabled: bool,
    timeout_seconds: int,
    error: str = "",
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "provider": canonical,
        "lane": getattr(lane, "lane_id", canonical),
        "driver": getattr(lane, "driver", ""),
        "auth_probe": state,
        "auth_probe_timeout_seconds": timeout_seconds,
        "enabled": enabled,
    }
    if error:
        detail["error"] = error
    return detail


def check_campaign_auth_readiness(
    *,
    lane: Any,
    canonical: str,
    enabled: bool,
    command: str,
    env: Mapping[str, str] | None = None,
    probe_runner: CampaignAuthProbeRunner | None = None,
) -> DoctorCheck | None:
    """Probe one ready local adapter's isolated login state.

    Returns ``None`` when the provider exposes no safe status command, which
    keeps that lane capability-only instead of guessing it is authenticated.
    """
    if not command:
        return None

    current_env = os.environ if env is None else env
    if canonical == "antigravity":
        opted_in = current_env.get("ANTIGRAVITY_CLI_USE_AMBIENT_HOME", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if opted_in:
            return DoctorCheck(
                name=CAMPAIGN_AUTH_CHECK_NAME,
                status=STATUS_PASS,
                lane=canonical,
                message="antigravity ambient-home auth opt-in configured",
                detail={
                    "provider": canonical,
                    "lane": getattr(lane, "lane_id", canonical),
                    "driver": getattr(lane, "driver", "local_cli"),
                    "auth_probe": "ambient_opt_in",
                    "auth_ready": True,
                    "ambient_home_opt_in": True,
                    "enabled": enabled,
                },
            )
        detail = {
            "provider": canonical,
            "lane": getattr(lane, "lane_id", canonical),
            "driver": getattr(lane, "driver", "local_cli"),
            "auth_probe": "missing_opt_in",
            "auth_ready": False,
            "ambient_home_opt_in": False,
            "missing_variable": "ANTIGRAVITY_CLI_USE_AMBIENT_HOME",
            "enabled": enabled,
            "actionable": enabled,
            "optional": not enabled,
        }
        if enabled:
            detail["owner_action"] = True
        return DoctorCheck(
            name=CAMPAIGN_AUTH_CHECK_NAME,
            status=STATUS_WARN,
            lane=canonical,
            message="antigravity campaign auth requires ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1 in trusted environments",
            detail=detail,
            remediation=(
                "Set ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1 in trusted environments to "
                "allow local OAuth state."
            ),
        )

    if canonical == "muse":
        from .. import muse_cli_audit_pr as code_mower_muse_cli

        has_key = bool(code_mower_muse_cli.resolve_muse_api_key(current_env))
        has_ambient = current_env.get("MUSE_CLI_USE_AMBIENT_HOME", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if has_key or has_ambient:
            return DoctorCheck(
                name=CAMPAIGN_AUTH_CHECK_NAME,
                status=STATUS_PASS,
                lane=canonical,
                message="muse campaign authentication configured",
                detail={
                    "provider": canonical,
                    "lane": getattr(lane, "lane_id", canonical),
                    "driver": getattr(lane, "driver", "local_cli"),
                    "auth_probe": "api_key" if has_key else "ambient_opt_in",
                    "auth_ready": True,
                    "enabled": enabled,
                },
            )
        detail = {
            "provider": canonical,
            "lane": getattr(lane, "lane_id", canonical),
            "driver": getattr(lane, "driver", "local_cli"),
            "auth_probe": "missing_auth",
            "auth_ready": False,
            "enabled": enabled,
            "actionable": enabled,
            "optional": not enabled,
        }
        if enabled:
            detail["owner_action"] = True
        return DoctorCheck(
            name=CAMPAIGN_AUTH_CHECK_NAME,
            status=STATUS_WARN,
            lane=canonical,
            message="muse campaign auth requires META_API_KEY or MUSE_CLI_USE_AMBIENT_HOME=1",
            detail=detail,
            remediation=(
                "Set META_API_KEY or point META_API_KEY_FILE at a local key file, or set "
                "MUSE_CLI_USE_AMBIENT_HOME=1 in trusted environments."
            ),
        )

    probe_args = campaign_auth_probe_args(lane)
    if not probe_args:
        return None

    timeout_seconds = campaign_auth_probe_timeout(lane)
    if not campaign_auth_probe_requested(env):
        return DoctorCheck(
            name=CAMPAIGN_AUTH_CHECK_NAME,
            status=STATUS_SKIP,
            lane=canonical,
            message=f"skipped {canonical} campaign authentication probe ({CAMPAIGN_AUTH_PROBE_ENV})",
            detail=_detail(
                canonical=canonical,
                lane=lane,
                state=AUTH_STATE_SKIPPED,
                enabled=enabled,
                timeout_seconds=timeout_seconds,
            ),
        )

    provider = str(getattr(lane, "provider", "") or canonical)
    child_env, env_error = campaign_auth_probe_env(provider)
    output = ""
    if env_error:
        error = env_error
    else:
        runner = run_campaign_auth_probe if probe_runner is None else probe_runner
        try:
            completed = runner([command, *probe_args], timeout_seconds, child_env)
            returncode = int(completed.returncode)
        except subprocess.TimeoutExpired:
            error = AUTH_ERROR_PROBE_TIMEOUT
        except (OSError, TypeError, ValueError):
            error = AUTH_ERROR_PROBE_UNAVAILABLE
        else:
            output = f"{completed.stdout or ''}{completed.stderr or ''}"
            if returncode == 0:
                error = ""
            elif campaign_auth_confirmed_logged_out(lane, returncode, output):
                error = AUTH_ERROR_UNAUTHENTICATED
            else:
                # A nonzero exit without the provider's logged-out signature
                # (unsupported subcommand, keyring or config failure) is a
                # probe failure, so the provider stays campaign-ready.
                error = AUTH_ERROR_PROBE_UNAVAILABLE

    if not error:
        return DoctorCheck(
            name=CAMPAIGN_AUTH_CHECK_NAME,
            status=STATUS_PASS,
            lane=canonical,
            message=f"{canonical} isolated campaign home is authenticated",
            detail={
                **_detail(
                    canonical=canonical,
                    lane=lane,
                    state=AUTH_STATE_AUTHENTICATED,
                    enabled=enabled,
                    timeout_seconds=timeout_seconds,
                ),
                **auth_probe_output_detail(output),
            },
        )

    if error != AUTH_ERROR_UNAUTHENTICATED:
        # A timeout, a missing keyring, or an unusable isolated home is not
        # evidence of a missing login, so it never becomes an owner action.
        detail = _detail(
            canonical=canonical,
            lane=lane,
            state=AUTH_STATE_UNKNOWN,
            enabled=enabled,
            timeout_seconds=timeout_seconds,
            error=error,
        )
        detail.update(auth_probe_output_detail(output))
        detail["actionable"] = False
        detail["optional"] = True
        return DoctorCheck(
            name=CAMPAIGN_AUTH_CHECK_NAME,
            status=STATUS_SKIP,
            lane=canonical,
            message=f"{canonical} campaign authentication could not be verified",
            detail=detail,
            remediation=_remediation(canonical, AUTH_STATE_UNKNOWN),
        )

    detail = _detail(
        canonical=canonical,
        lane=lane,
        state=AUTH_STATE_UNAUTHENTICATED,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
        error=error,
    )
    detail.update(auth_probe_output_detail(output))
    detail["actionable"] = enabled
    detail["optional"] = not enabled
    if enabled:
        detail["owner_action"] = True
    return DoctorCheck(
        name=CAMPAIGN_AUTH_CHECK_NAME,
        status=STATUS_WARN,
        lane=canonical,
        message=f"{canonical} isolated campaign home is not authenticated",
        detail=detail,
        remediation=_remediation(canonical, AUTH_STATE_UNAUTHENTICATED),
    )


__all__ = (
    "AUTH_ERROR_PROBE_TIMEOUT",
    "AUTH_ERROR_PROBE_UNAVAILABLE",
    "AUTH_ERROR_UNAUTHENTICATED",
    "AUTH_STATE_AUTHENTICATED",
    "AUTH_STATE_SKIPPED",
    "AUTH_STATE_UNAUTHENTICATED",
    "AUTH_STATE_UNKNOWN",
    "CAMPAIGN_AUTH_CHECK_NAME",
    "CAMPAIGN_AUTH_LOGGED_OUT_EXIT_CODES_KEY",
    "CAMPAIGN_AUTH_LOGGED_OUT_MARKERS_KEY",
    "CAMPAIGN_AUTH_MARKER_SCAN_LIMIT",
    "CAMPAIGN_AUTH_PROBE_ARGS_KEY",
    "CAMPAIGN_AUTH_PROBE_ENV",
    "CAMPAIGN_AUTH_PROBE_TIMEOUT_KEY",
    "DEFAULT_CAMPAIGN_AUTH_PROBE_TIMEOUT_SECONDS",
    "campaign_auth_confirmed_logged_out",
    "campaign_auth_logged_out_exit_codes",
    "campaign_auth_logged_out_markers",
    "campaign_auth_probe_args",
    "campaign_auth_probe_env",
    "campaign_auth_probe_requested",
    "campaign_auth_probe_timeout",
    "check_campaign_auth_readiness",
    "run_campaign_auth_probe",
)
