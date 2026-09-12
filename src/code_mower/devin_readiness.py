"""Optional Devin setup readiness for the local CLI and hosted v3 postures.

Devin stays an explicit addition to the Claude + Codex default: a repository
that never selected it produces no findings here. When it is selected, this
module is the single place that describes which transport is active, which
authentication belongs to it, which create/view/manage permissions the owner
must grant, which capabilities the integration actually supports, and what to
run next.

Findings are metadata only. Credential values, service-user identity,
organization identifier, the configured repository inventory, local paths, and
raw provider output never appear in a finding.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .config import ConfigError
from .devin_api import (
    DEVIN_API_KEY_ENV,
    DEVIN_ORG_ID_ENV,
    DEVIN_REPOSITORIES_ENV,
    credentials_from_env,
    repository_scope_acknowledged,
)
from .local_cli_commands import candidate_local_cli_commands
from .participants import (
    DEFAULT_PARTICIPANTS,
    TRANSPORT_PARTICIPANT_ALIASES,
    configured_participants,
    configured_transports,
    selected_transports,
)
from .provider_capabilities import TRANSPORTS, devin_lane_transport_name

SCHEMA = "code_mower.devinReadiness.v1"

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

LOCAL_TRANSPORT = "devin_cli"
HOSTED_TRANSPORT = "devin_api_v3"

POSTURE_LOCAL_CLI = "local_cli"
POSTURE_HOSTED_API = "hosted_api"
POSTURE_UNAVAILABLE = "unavailable"

POSTURES = {LOCAL_TRANSPORT: POSTURE_LOCAL_CLI, HOSTED_TRANSPORT: POSTURE_HOSTED_API}

CLI_COMMAND_ENV = "CODE_MOWER_DEVIN_CLI_COMMAND"
DEFAULT_CLI_COMMAND = "devin"

# Coordinating machines never execute a local reviewer CLI, so readiness skips
# the local executable requirement wherever lane runtime skips it. The doctor
# check package owns the canonical set and cannot be imported here without a
# cycle, so a test pins these values to it.
OBSERVER_POSTURES = frozenset({"hosted-builders", "orchestrator-only"})
DEFAULT_ADOPTION_POSTURE = "reviewer-gate"

# A transport switch is generated as a targeted mutation of one product's
# transport. Rewriting the participant list instead would delete every unrelated
# participant and profile lane the repository selected.
TRANSPORT_OPTION = "--set-transport"
SELECTABLE_TRANSPORTS = (LOCAL_TRANSPORT, HOSTED_TRANSPORT)
CANONICAL_LANES = frozenset(
    entry.review_lane
    for entry in TRANSPORTS.values()
    if entry.product == "devin" and entry.review_lane
)

# `init --apply` stages a reviewable tree; it never rewrites the configuration it
# read, so a switch is only active once the generated configuration is installed
# through the normal setup PR. A test pins this directory to init's own default.
GENERATED_OUTPUT_DIR = ".code-mower.generated"

# The public declaration fields a lane names for each transport. A repository that
# named its own Devin lanes edits these fields itself: no generated command can
# retarget a lane it cannot name without rebuilding the profile around it.
LANE_PROVIDERS = {LOCAL_TRANSPORT: "devin_cli", HOSTED_TRANSPORT: "devin"}

# The credential resolver reports where it searched, including a home-relative
# profile path and filename, and repeats it in its own remediation. Readiness
# output stays path-free, so those fields are dropped and remediation for every
# unresolved outcome is written here instead of being forwarded.
PATH_DETAIL_FIELDS = frozenset({"profile_file", "candidate_files"})

HOSTED_CREDENTIAL_CAUSES = {
    "malformed": (
        "The stored hosted credentials could not be parsed. Rewrite the credential "
        f"profile as `NAME=value` lines for service-user {DEVIN_API_KEY_ENV} and "
        f"opaque {DEVIN_ORG_ID_ENV} (format `org-...`, not a GitHub owner name)"
    ),
    "insecure_permissions": (
        "The stored hosted credentials are readable beyond their owner. Restrict the "
        "credential profile to owner-only permissions (`chmod 600`) and rotate the "
        "service-user key"
    ),
}
HOSTED_CREDENTIAL_CAUSE_DEFAULT = (
    f"Set service-user {DEVIN_API_KEY_ENV} and opaque {DEVIN_ORG_ID_ENV} "
    "(format `org-...`, not a GitHub owner name) in the environment or a "
    "protected credential profile"
)

# Devin exposes no read-only endpoint that proves session or GitHub connection
# scope before paid work starts, so these requirements are reported as an owner
# action instead of being probed.
PERMISSION_REQUIREMENTS = {
    LOCAL_TRANSPORT: (
        "create: an authenticated ambient Devin CLI login may create local runs in "
        "the lane's dedicated checkout only",
        "view: run output stays local; doctor reports bounded auth status and never "
        "persists or uploads it",
        "manage: no hosted session lifecycle exists; stopping a local run is a local "
        "process action",
    ),
    HOSTED_TRANSPORT: (
        "create: the dedicated service user needs organization session-create "
        "permission and GitHub connection access to the exact OWNER/REPO target",
        "view: session-read permission for lifecycle state, structured results, and "
        "ACU usage",
        "manage: session-manage permission for terminate and archive; Code Mower "
        "never retries a paid mutation",
    ),
}

# Reported next actions stay path-free because they are also carried in finding
# detail, which is metadata: the configuration path and profile that produced the
# finding appear only in locally rendered remediation.
POSTURE_NEXT_ACTIONS = (
    "local CLI: select devin-cli for this configuration and profile, install `devin` "
    f"on PATH (or set {CLI_COMMAND_ENV}), and run `devin auth login` in a trusted "
    "environment",
    "hosted API: select devin-api-v3 for this configuration and profile, then set "
    f"service-user {DEVIN_API_KEY_ENV}, opaque {DEVIN_ORG_ID_ENV}, and the exact "
    f"OWNER/REPO in {DEVIN_REPOSITORIES_ENV}",
    "unavailable: keep the Claude + Codex default; report the unavailable capability "
    "and hand the work to a selected participant instead of substituting a product",
)


@dataclass(frozen=True)
class ReadinessFinding:
    """One privacy-safe readiness statement about the selected Devin posture."""

    name: str
    status: str
    message: str
    lane: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    remediation: str = ""


def selected_devin_transport(
    config: Mapping[str, Any] | None,
    *,
    lanes: tuple[str, ...] = (),
    profile: str | None = "recommended",
) -> str | None:
    """Return the selected Devin transport, or None when Devin is not selected.

    Participant selection is authoritative. An active Devin review lane selects
    Devin too, so a repository that enabled the lane without saving participants
    still receives readiness guidance.
    """
    if not isinstance(config, Mapping):
        return None
    selected_lane_transports = _lane_transports(config, lanes)
    lane_transports = set(selected_lane_transports.values())
    explicit = _explicit_transport_selected(config)
    if len(lane_transports) > 1 and not explicit:
        # Lane order is not a selection, so an unscoped pair of transports is
        # reported rather than silently resolved to whichever lane came first.
        raise ConfigError(
            "Both Devin review transports are active; scope the check to one "
            "profile with --profile or set session_defaults.transports.devin to "
            "devin_cli or devin_api_v3"
        )
    lane_transport = next(iter(selected_lane_transports.values()), None)
    if lane_transport is None and "devin" not in configured_participants(config):
        return None
    configured = configured_transports(config, profile=profile)["devin"]
    if lane_transport is not None and not explicit:
        return lane_transport
    return configured


def _lane_transports(
    config: Mapping[str, Any], lanes: tuple[str, ...]
) -> dict[str, str]:
    """Map each selected Devin lane to the transport its declaration names.

    Selection follows the lane declaration rather than the lane identifier, so a
    valid custom-named lane such as `team_devin` selects its declared transport
    exactly as the canonical lanes do.
    """
    lane_configs = config.get("lanes")
    declarations = lane_configs if isinstance(lane_configs, Mapping) else {}
    resolved: dict[str, str] = {}
    for lane in lanes:
        declaration = declarations.get(lane)
        transport = devin_lane_transport_name(
            lane, declaration if isinstance(declaration, Mapping) else None
        )
        if transport is not None:
            resolved[lane] = transport
    return resolved


def _explicit_transport_selected(config: Mapping[str, Any]) -> bool:
    """Report whether the repository names a Devin transport itself.

    Without an explicit alias or `session_defaults.transports.devin`, an active
    hosted review lane is the only statement of intent, so lane inference must
    win over the local default even when profile inference is disabled.
    """
    defaults = config.get("session_defaults")
    if not isinstance(defaults, Mapping):
        return False
    transports = defaults.get("transports")
    if isinstance(transports, Mapping) and "devin" in transports:
        return True
    selected = defaults.get("participants")
    if isinstance(selected, list) and all(isinstance(name, str) for name in selected):
        return "devin" in selected_transports(tuple(selected))
    return False


@dataclass(frozen=True)
class _Pin:
    """The configuration, profile, and target that produced a finding.

    Every actionable command a finding renders is generated from these inputs, so
    an operator following remediation inspects or changes exactly the posture the
    finding describes instead of the default starter configuration.
    """

    config_path: str = ""
    profile: str = ""
    repo_slug: str = ""
    # A generated transport switch can only replace canonical Devin lanes; a
    # custom-named lane is edited by its owner instead of rewritten.
    targeted_switch: bool = True
    custom_lanes: tuple[str, ...] = ()

    def doctor(self, *, devin: bool = False, flags: tuple[str, ...] = ()) -> str:
        return doctor_command(
            config_path=self.config_path,
            profile=self.profile,
            devin=devin,
            flags=flags,
        )

    def readiness(self, *, repo_slug: str | None = None) -> str:
        return readiness_command(
            config_path=self.config_path,
            profile=self.profile,
            repo_slug=self.repo_slug if repo_slug is None else repo_slug,
        )

    def select(self, transport: str) -> str:
        return select_transport_command(
            transport,
            config_path=self.config_path,
            profile=self.profile,
            targeted=self.targeted_switch,
            lanes=self.custom_lanes,
        )


@dataclass(frozen=True)
class _LocalDiscovery:
    """How the selected local lane looks for its executable."""

    candidates: tuple[str, ...]
    command_env: str
    lane_configured: bool


def _cli_commands(
    lane_config: Mapping[str, Any] | None, env: Mapping[str, str]
) -> _LocalDiscovery:
    """Return the discovery the lane performs, whose candidates may be paths.

    A configured lane's own discovery order is the whole answer, because lane
    runtime resolves exactly those candidates: appending the historical override
    or the default would let readiness pass on an executable the lane would
    never run, and naming them in guidance would send the operator to a variable
    the lane ignores. Those two only answer for a caller with no lane
    configuration. Every lookup reads the injected environment so readiness never
    depends on the host.
    """
    if isinstance(lane_config, Mapping):
        candidates = list(candidate_local_cli_commands(lane_config, env=env))
        provider_config = lane_config.get("provider_config")
        command_env = (
            str(provider_config.get("command_env") or "")
            if isinstance(provider_config, Mapping)
            else ""
        )
        lane_configured = True
    else:
        candidates = []
        override = str(env.get(CLI_COMMAND_ENV) or "")
        if override:
            candidates.append(override)
        candidates.append(DEFAULT_CLI_COMMAND)
        command_env = CLI_COMMAND_ENV
        lane_configured = False
    ordered: list[str] = []
    for command in candidates:
        if command not in ordered:
            ordered.append(command)
    return _LocalDiscovery(tuple(ordered), command_env, lane_configured)


def _selection_finding(
    transport: str,
    lane: str | None,
    pin: _Pin,
    *,
    lane_ids: tuple[str, ...] = (),
) -> ReadinessFinding:
    posture = POSTURES[transport]
    if transport == LOCAL_TRANSPORT:
        authentication = "ambient Devin Desktop/CLI login"
        remediation = (
            "Keep local execution, or switch to hosted service-user credentials: "
            f"{pin.select(HOSTED_TRANSPORT)}. Selection never grants review or merge "
            "authority."
        )
    else:
        authentication = "hosted v3 service-user credentials with exact repository scope"
        remediation = (
            "Keep hosted execution, or switch to the local Devin CLI login: "
            f"{pin.select(LOCAL_TRANSPORT)}. Selection never grants review or merge "
            "authority."
        )
    detail: dict[str, Any] = {
        "schema": SCHEMA,
        "posture": posture,
        "transport": transport,
        "authentication": authentication,
        "default_participants": list(DEFAULT_PARTICIPANTS),
    }
    if len(lane_ids) > 1:
        # The posture belongs to the product, so it is stated once; the lanes that
        # share it are named here instead of one of them being reported as the
        # lane every product finding came from.
        detail["lanes"] = list(lane_ids)
    return ReadinessFinding(
        name="provider.devin.selection",
        status=STATUS_PASS,
        message=f"Devin selected as an optional participant: {transport} ({posture}); "
        f"authentication is the {authentication}",
        lane=lane,
        detail=detail,
        remediation=remediation,
    )


def _capability_finding(transport: str, lane: str | None) -> ReadinessFinding:
    brief = TRANSPORTS[transport].brief()
    gaps = brief["capability_gaps"]
    modes = ", ".join(f"{name}={mode}" for name, mode in brief["capabilities"].items())
    return ReadinessFinding(
        name="provider.devin.capabilities",
        status=STATUS_PASS,
        message=f"{transport} capability modes: {modes}",
        lane=lane,
        detail={
            "schema": SCHEMA,
            "posture": POSTURES[transport],
            "transport": transport,
            "capabilities": dict(brief["capabilities"]),
            "capability_gaps": list(gaps),
        },
        remediation=(
            "Assign only declared capability modes. Unavailable capabilities ("
            + ", ".join(gaps)
            + ") pause dependent work; report them instead of substituting another product."
        ),
    )


def _local_cli_finding(
    lane: str | None,
    *,
    lane_config: Mapping[str, Any] | None,
    env: Mapping[str, str],
    pin: _Pin,
    adoption_posture: str = DEFAULT_ADOPTION_POSTURE,
) -> ReadinessFinding:
    discovery = _cli_commands(lane_config, env)
    candidates = discovery.candidates
    # Reporting stays a basename so no local path leaves the machine, even though
    # a configured candidate may be an absolute path.
    basenames = tuple(dict.fromkeys(os.path.basename(name) for name in candidates))
    detail: dict[str, Any] = {
        "schema": SCHEMA,
        "posture": POSTURE_LOCAL_CLI,
        "transport": LOCAL_TRANSPORT,
        "command": basenames[0],
        "commands": list(basenames),
        "authentication": "ambient_cli_login",
        "adoption_posture": adoption_posture,
        "discovery": "lane_configured" if discovery.lane_configured else "default",
    }
    if discovery.command_env:
        detail["command_env"] = discovery.command_env
    if adoption_posture in OBSERVER_POSTURES:
        # Skipping before any lookup keeps the statement literally true: nothing
        # about this machine's executables was inspected.
        return ReadinessFinding(
            name="provider.devin.local_cli",
            status=STATUS_SKIP,
            message=f"{adoption_posture} posture does not execute the local Devin CLI "
            "on this machine, so its availability was not inspected",
            lane=lane,
            detail=detail,
            remediation=(
                f"Rerun {pin.doctor(devin=True)} without the hosted-builder or "
                "orchestrator-only posture, on the machine that executes the lane."
            ),
        )
    resolved = next((command for command in candidates if shutil.which(command)), None)
    if resolved is not None:
        command = os.path.basename(resolved)
        detail["command"] = command
        return ReadinessFinding(
            name="provider.devin.local_cli",
            status=STATUS_PASS,
            message=f"{command} is available for local execution; its ambient login is "
            "separate from hosted service-user credentials",
            lane=lane,
            detail=detail,
            remediation=(
                "Run `devin auth login` in a trusted environment if the ambient session "
                f"expired, and {pin.doctor(flags=('--probe-runtime',))} for bounded auth "
                "status."
            ),
        )
    if discovery.lane_configured:
        # Naming the historical override here would send the operator to a
        # variable this lane's runtime never reads.
        named = ", ".join(f"`{name}`" for name in basenames)
        install = f"Install the Devin CLI as one of the lane's configured commands ({named})"
        if discovery.command_env:
            install += f" or point {discovery.command_env} at the executable to run"
        remediation = (
            f"{install}, run `devin auth login` in a trusted environment, then rerun "
            f"{pin.doctor(devin=True)}. Hosted credentials do not enable this "
            f"transport; to select it instead: {pin.select(HOSTED_TRANSPORT)}."
        )
    else:
        remediation = (
            f"Install the Devin CLI as `{DEFAULT_CLI_COMMAND}` on PATH or set "
            f"{CLI_COMMAND_ENV} to its absolute path, run `devin auth login` in a trusted "
            f"environment, then rerun {pin.doctor(devin=True)}. Hosted credentials do "
            "not enable this transport; to select it instead: "
            f"{pin.select(HOSTED_TRANSPORT)}."
        )
    return ReadinessFinding(
        name="provider.devin.local_cli",
        status=STATUS_WARN,
        message=f"{basenames[0]} was not found, so local Devin execution is unavailable",
        lane=lane,
        detail=detail,
        remediation=remediation,
    )


def _hosted_credential_remediation(status: str, *, pin: _Pin) -> str:
    """Return the next action for an unresolved hosted credential outcome.

    A credential profile is not the Code Mower configuration profile, so several
    stored credential profiles are resolved by naming one with
    `--provider-profile`, while the same configuration and `--profile` keep
    describing the same posture. Profile names, filenames, and locations stay out
    of the answer.
    """
    if status == "ambiguous":
        return (
            "Several stored credential profiles could satisfy hosted Devin. Rerun "
            f"{pin.readiness()} with `--provider-profile NAME` naming the credential "
            "profile to use, or set service-user "
            f"{DEVIN_API_KEY_ENV} and {DEVIN_ORG_ID_ENV} in the environment. The Code "
            "Mower --profile still selects the configuration profile."
        )
    cause = HOSTED_CREDENTIAL_CAUSES.get(status)
    if cause is not None:
        return f"{cause}, then rerun {pin.readiness()}."
    return (
        f"{HOSTED_CREDENTIAL_CAUSE_DEFAULT}, then rerun {pin.readiness()}. If this "
        f"machine should run the local CLI instead: {pin.select(LOCAL_TRANSPORT)}."
    )


def _hosted_credential_finding(
    lane: str | None,
    *,
    env: Mapping[str, str] | None,
    credential_file: Path | None,
    profile: str,
    config_dir: Path | None,
    pin: _Pin,
) -> ReadinessFinding:
    credentials = credentials_from_env(
        env, credential_file=credential_file, profile=profile, config_dir=config_dir
    )
    resolution = credentials.resolution
    detail: dict[str, Any] = {
        "schema": SCHEMA,
        "posture": POSTURE_HOSTED_API,
        "transport": HOSTED_TRANSPORT,
        "required_variables": [DEVIN_API_KEY_ENV, DEVIN_ORG_ID_ENV],
        "authentication": "service_user_credentials",
    }
    if resolution is not None:
        detail.update(
            {
                key: value
                for key, value in resolution.safe_detail().items()
                if key not in PATH_DETAIL_FIELDS
            }
        )
    if credentials.has_credentials:
        return ReadinessFinding(
            name="provider.devin.hosted_credentials",
            status=STATUS_PASS,
            message="hosted service-user credentials resolved from "
            f"{credentials.source or 'env'}; values and identities are not reported",
            lane=lane,
            detail=detail,
            remediation=(
                "Keep the credentials on a dedicated service user, rotate them outside "
                "the repository, and never commit or echo them."
            ),
        )
    status = STATUS_WARN if credentials.status == "missing" else STATUS_FAIL
    # The resolver reports its own status where a variable name would go for
    # outcomes like an ambiguous credential profile, so only a genuinely required
    # variable is named as unresolved.
    missing = (
        credentials.missing
        if credentials.missing in {DEVIN_API_KEY_ENV, DEVIN_ORG_ID_ENV}
        else ""
    )
    message = f"hosted service-user credentials are {credentials.status}"
    if missing:
        message += f"; first unresolved variable: {missing}"
        detail["missing_variable"] = missing
    return ReadinessFinding(
        name="provider.devin.hosted_credentials",
        status=status,
        message=message,
        lane=lane,
        detail=detail,
        remediation=_hosted_credential_remediation(credentials.status, pin=pin),
    )


def _repository_scope_finding(
    lane: str | None,
    *,
    repo_slug: str,
    env: Mapping[str, str] | None,
    credential_file: Path | None,
    profile: str,
    config_dir: Path | None,
    pin: _Pin,
) -> ReadinessFinding:
    detail: dict[str, Any] = {
        "schema": SCHEMA,
        "posture": POSTURE_HOSTED_API,
        "transport": HOSTED_TRANSPORT,
        "scope_variable": DEVIN_REPOSITORIES_ENV,
        "exact_slug_required": True,
    }
    if not repo_slug:
        return ReadinessFinding(
            name="provider.devin.repository_scope",
            status=STATUS_SKIP,
            message="no repository target selected, so exact hosted repository scope was "
            "not inspected",
            lane=lane,
            detail=detail,
            remediation=(
                f"Rerun {pin.readiness(repo_slug='OWNER/REPO')} to check the exact "
                "hosted repository acknowledgement before dispatching paid work."
            ),
        )
    acknowledged = repository_scope_acknowledged(
        repo_slug,
        env=env,
        credential_file=credential_file,
        profile=profile,
        config_dir=config_dir,
    )
    detail["repository"] = repo_slug
    detail["acknowledged"] = acknowledged
    if acknowledged:
        return ReadinessFinding(
            name="provider.devin.repository_scope",
            status=STATUS_PASS,
            message=f"exact hosted repository scope acknowledges {repo_slug}",
            lane=lane,
            detail=detail,
            remediation=(
                "Re-acknowledge the exact slug whenever the hosted target changes; the "
                "configured inventory is never printed or uploaded."
            ),
        )
    return ReadinessFinding(
        name="provider.devin.repository_scope",
        status=STATUS_WARN,
        message=f"exact hosted repository scope does not acknowledge {repo_slug}",
        lane=lane,
        detail=detail,
        remediation=(
            f"Add the exact `{repo_slug}` entry to {DEVIN_REPOSITORIES_ENV} (comma "
            "separated, full OWNER/REPO, so a same-name fork is never accepted) and "
            f"confirm the service user's GitHub connection reaches it, then rerun "
            f"{pin.readiness()}."
        ),
    )


def _permission_finding(transport: str, lane: str | None) -> ReadinessFinding:
    return ReadinessFinding(
        name="provider.devin.permissions",
        # Reported, not probed: Devin exposes no read-only permission preflight,
        # so this states the requirement instead of warning about every repo.
        status=STATUS_SKIP,
        message=f"{transport} create/view/manage permission requirements cannot be "
        "verified read-only; confirm them with the account owner",
        lane=lane,
        detail={
            "schema": SCHEMA,
            "posture": POSTURES[transport],
            "transport": transport,
            "requirements": list(PERMISSION_REQUIREMENTS[transport]),
            "owner_action": True,
            "owner_action_kind": "devin_permissions",
        },
        remediation=(
            "Grant these requirements to the selected identity: "
            + "; ".join(PERMISSION_REQUIREMENTS[transport])
            + ". Never report or store the identity, credential, or organization value."
        ),
    )


def _lifecycle_finding(transport: str, lane: str | None) -> ReadinessFinding:
    if transport == LOCAL_TRANSPORT:
        return ReadinessFinding(
            name="provider.devin.lifecycle",
            status=STATUS_PASS,
            message="local execution has no hosted session lifecycle: remote message and "
            "cancel are unavailable",
            lane=lane,
            detail={
                "schema": SCHEMA,
                "posture": POSTURE_LOCAL_CLI,
                "transport": LOCAL_TRANSPORT,
                "commands": ["code-mower session start", "code-mower lanes status"],
            },
            remediation=(
                "Coordinate local work through `code-mower session start` and recover a "
                "stalled run by rerunning the lane; use the hosted transport when a "
                "remote session lifecycle is required."
            ),
        )
    return ReadinessFinding(
        name="provider.devin.lifecycle",
        status=STATUS_PASS,
        message="hosted remote sessions support status, message, cancel, and collect; "
        "lifecycle commands preview by default and require --apply, and uncertain "
        "dispatch recovers through status, never a second dispatch",
        lane=lane,
        detail={
            "schema": SCHEMA,
            "posture": POSTURE_HOSTED_API,
            "transport": HOSTED_TRANSPORT,
            "commands": [
                "code-mower session dispatch ALIAS --provider devin --repo OWNER/REPO "
                "--input-file FILE --apply",
                "code-mower session status ALIAS --provider devin",
                "code-mower session message ALIAS --provider devin --request KEY "
                "--input-file FILE --apply",
                "code-mower session cancel ALIAS --provider devin --request KEY --apply",
                "code-mower session collect ALIAS --provider devin --apply",
            ],
        },
        remediation=(
            "Recover an uncertain dispatch with `code-mower session status ALIAS "
            "--provider devin` and reuse the original input; never redispatch. Resolve a "
            "pending request with `--acknowledge-delivered --apply` after inspecting the "
            "provider, and keep `--max-acu-limit` within the approved cap."
        ),
    )


def _unselected_findings(pin: _Pin) -> tuple[ReadinessFinding, ...]:
    return (
        ReadinessFinding(
            name="provider.devin.selection",
            status=STATUS_SKIP,
            message="Devin is not selected; the default participants remain "
            + ", ".join(DEFAULT_PARTICIPANTS),
            detail={
                "schema": SCHEMA,
                "posture": POSTURE_UNAVAILABLE,
                "default_participants": list(DEFAULT_PARTICIPANTS),
            },
            remediation=(
                "Add Devin by naming its transport in this configuration: "
                f"{pin.select(LOCAL_TRANSPORT)}. Name `devin={HOSTED_TRANSPORT}` in "
                "the same commands for hosted execution instead."
            ),
        ),
        ReadinessFinding(
            name="provider.devin.postures",
            status=STATUS_SKIP,
            message="optional Devin postures and their next actions: "
            + "; ".join(POSTURE_NEXT_ACTIONS),
            detail={
                "schema": SCHEMA,
                "postures": [POSTURE_LOCAL_CLI, POSTURE_HOSTED_API, POSTURE_UNAVAILABLE],
                "next_actions": list(POSTURE_NEXT_ACTIONS),
            },
            remediation=(
                "Choose one posture before assigning Devin work; hosted credentials do "
                "not enable local execution, and a local login does not authorize hosted "
                f"sessions. To select the local CLI: {pin.select(LOCAL_TRANSPORT)}. Name "
                f"`devin={HOSTED_TRANSPORT}` in the same commands for the hosted API."
            ),
        ),
    )


def _pinned(command: str, *, config_path: str, profile: str) -> str:
    """Return `command` scoped to exactly one configuration and profile.

    Both inputs are shell-quoted because a configuration path and a profile name
    may contain spaces, and an unquoted command would inspect something else.
    """
    if config_path:
        command += f" {shlex.quote(config_path)}"
    if profile:
        command += f" --profile {shlex.quote(profile)}"
    return command


def _unpinned_guidance(shown: str) -> str:
    """Return guidance to reuse the caller's own inputs for `shown`."""
    return (
        f"the same `{shown}` invocation, using the same configuration and --profile "
        "selected here"
    )


def doctor_command(
    *,
    config_path: str = "",
    profile: str = "",
    repo_slug: str = "",
    devin: bool = False,
    flags: tuple[str, ...] = (),
) -> str:
    """Return the doctor command that inspects exactly this posture.

    A generated command must pin the configuration and profile it describes,
    because an unpinned check can read another profile's Devin lane, and a bare
    rerun can report a different posture than the finding that asked for it. A
    caller without those inputs gets guidance to reuse its own instead of a
    command that silently inspects something else.
    """
    shown = "code-mower doctor" + (" --devin" if devin else "")
    shown += "".join(f" {flag}" for flag in flags)
    if not profile:
        return _unpinned_guidance(shown)
    command = _pinned("code-mower doctor", config_path=config_path, profile=profile)
    if devin:
        command += " --devin"
    command += "".join(f" {flag}" for flag in flags)
    if repo_slug:
        command += f" --repo {shlex.quote(repo_slug)}"
    return f"`{command}`"


def readiness_command(
    *,
    config_path: str = "",
    profile: str = "",
    repo_slug: str = "",
) -> str:
    """Return the `--devin` doctor command that inspects exactly this posture."""
    return doctor_command(
        config_path=config_path, profile=profile, repo_slug=repo_slug, devin=True
    )


def select_transport_command(
    transport: str,
    *,
    config_path: str = "",
    profile: str = "",
    targeted: bool = True,
    lanes: tuple[str, ...] = (),
) -> str:
    """Return the steps that select `transport` for this configuration.

    A transport switch is only actionable against the configuration and profile
    the finding describes: a bare `code-mower init` writes the default starter
    configuration under the recommended profile instead. It must also change
    nothing else, so it names the product's transport rather than a participant
    list, which would drop every unrelated participant and profile lane.

    `init` never rewrites the configuration it read: `--apply` stages a reviewable
    generated tree, so the steps are a dry-run preview, an apply into an explicit
    output directory, an install of the reviewed output through the normal setup
    PR, and only then a rerun of readiness. Claiming the posture switched because
    files were staged would misreport the active configuration.

    When the profile's Devin lanes are custom-named, no generated command can
    retarget them, so bounded manual guidance names those lanes instead. The Code
    Mower `--profile` selects the configuration profile and is never the credential
    `--provider-profile`.
    """
    if transport not in SELECTABLE_TRANSPORTS:
        raise ConfigError("Devin transport must be devin_cli or devin_api_v3")
    if not targeted:
        return custom_lane_guidance(
            transport, config_path=config_path, profile=profile, lanes=lanes
        )
    selection = f"{TRANSPORT_OPTION} devin={transport}"
    if not profile:
        return _unpinned_guidance(f"code-mower init {selection}")
    pinned = _pinned("code-mower init", config_path=config_path, profile=profile)
    staged = shlex.quote(GENERATED_OUTPUT_DIR)
    return (
        f"preview it with `{pinned} {selection} --dry-run`, stage it with `{pinned} "
        f"{selection} --apply --output-dir {staged}`, then review the generated "
        "configuration and support files and install them through the normal setup PR "
        f"before rerunning {doctor_command(config_path=config_path, profile=profile, devin=True)}"
        "; staging writes only that review tree, so the active posture keeps reporting "
        "the installed configuration until the generated one replaces it, and the "
        "saved selection is repository-wide, so every profile selecting Devin moves "
        "to this transport"
    )


def custom_lane_guidance(
    transport: str,
    *,
    config_path: str = "",
    profile: str = "",
    lanes: tuple[str, ...] = (),
) -> str:
    """Return bounded manual guidance for retargeting custom-named Devin lanes.

    A repository that named its own Devin lanes owns them. The participant picker
    selects products and canonical reviewer lanes, so it cannot transform a named
    lane and would rebuild the profile around the lanes it does know, dropping the
    rest; no generated selection is safe here. The answer names only the configured
    lane IDs and the public declaration fields they must carry.

    Editing those four fields alone is not enough to retarget a lane. A lane copied
    from the maintained configuration carries a `capabilities` block and may pin
    `provider_config.campaign_transport`, both of which are rejected once they
    disagree with the new transport, and a saved `session_defaults.transports.devin`
    or transport-specific participant alias keeps selecting the old transport. The
    guidance names those settings too, still without reading or echoing the
    configuration.

    That saved selection is repository-wide, so it cannot be aligned for one
    profile in isolation: another profile selecting its own named Devin lane would
    then be contradicted by it. The guidance therefore states the coherent
    contract — align every Devin-selecting profile's named lanes before saving a
    transport, or, when profiles intentionally keep different Devin transports,
    save no transport-specific selection or alias at all and let each profile's
    lane declaration select its own. Profiles are described, never enumerated.
    """
    if transport not in SELECTABLE_TRANSPORTS:
        raise ConfigError("Devin transport must be devin_cli or devin_api_v3")
    entry = TRANSPORTS[transport]
    named = (
        ", ".join(f"`{lane}`" for lane in lanes)
        if lanes
        else "this profile's custom-named Devin lanes"
    )
    where = f" in {shlex.quote(config_path)}" if config_path else ""
    if profile:
        where += f" under profile {shlex.quote(profile)}"
    action = (
        f"edit {named}{where} so each declares `product: devin`, `provider: "
        f"{LANE_PROVIDERS[transport]}`, `transport: {transport}`, and `driver: "
        f"{entry.driver}`; drop each lane's `capabilities` block to use the "
        f"maintained defaults for {transport} (or restate them for it) and retarget "
        f"any `provider_config.campaign_transport` to {transport}; the saved "
        "selection is repository-wide, so inspect every profile that selects Devin "
        "and retarget its named lanes the same way before setting "
        f"`session_defaults.transports.devin: {transport}` and replacing any Devin "
        f"participant alias with `{TRANSPORT_PARTICIPANT_ALIASES[transport]}` — if "
        "profiles intentionally keep different Devin transports, save no "
        "`session_defaults.transports.devin` and no transport-specific alias (name "
        "the product as `devin`) so each profile's lane declaration selects its own "
        "transport; leave every other lane field, participant, and profile lane as "
        "configured, and review the diff"
    )
    if not profile:
        return f"{action}, then rerun {_unpinned_guidance('code-mower doctor --devin')}"
    return (
        f"{action}, then rerun "
        f"{doctor_command(config_path=config_path, profile=profile, devin=True)}; no "
        "generated command can retarget a lane this repository named"
    )


def setup_instructions(
    transport: str,
    *,
    config_path: str = "",
    profile: str = "",
    repo_slug: str = "",
) -> tuple[str, ...]:
    """Return host guidance for the selected optional Devin posture."""
    if transport not in TRANSPORTS:
        raise ConfigError("Devin transport must be devin_cli or devin_api_v3")
    if transport == LOCAL_TRANSPORT:
        check = readiness_command(config_path=config_path, profile=profile)
        authentication = (
            "Devin executes locally through devin_cli: its ambient Devin Desktop/CLI "
            "login is the only authentication, and hosted service-user credentials do "
            f"not enable it. Confirm readiness with {check}."
        )
    else:
        check = readiness_command(
            config_path=config_path, profile=profile, repo_slug=repo_slug or "OWNER/REPO"
        )
        authentication = (
            "Devin executes hosted through devin_api_v3: it needs dedicated service-user "
            f"credentials ({DEVIN_API_KEY_ENV}, {DEVIN_ORG_ID_ENV}) plus the exact "
            f"OWNER/REPO acknowledged in {DEVIN_REPOSITORIES_ENV}, and a local CLI login "
            f"does not authorize it. Confirm readiness with {check}."
        )
    return (
        authentication,
        "Confirm the selected Devin identity holds its create, view, and manage "
        "permissions before assigning work; Code Mower cannot verify them read-only and "
        "never reports identities or credential values.",
    )


def devin_readiness(
    config: Mapping[str, Any] | None,
    *,
    lanes: tuple[str, ...] = (),
    repo_slug: str = "",
    transport: str | None = None,
    env: Mapping[str, str] | None = None,
    credential_file: Path | None = None,
    profile: str = "",
    config_profile: str | None = "recommended",
    config_dir: Path | None = None,
    config_path: str = "",
    lane_config: Mapping[str, Any] | None = None,
    lane_id: str = "",
    lane_configs: tuple[tuple[str, Mapping[str, Any] | None], ...] = (),
    adoption_posture: str = DEFAULT_ADOPTION_POSTURE,
    include_unselected: bool = False,
) -> tuple[ReadinessFinding, ...]:
    """Return the readiness findings for the selected optional Devin posture.

    ``profile`` names the stored credential profile; ``config_profile`` names the
    configuration profile whose lanes decide which transport is selected.
    ``lane_configs`` are the selected effective Devin lanes, which a valid
    configuration may name anything and may declare more than one of on the same
    transport. Reporting a canonical lane instead would attribute doctor and
    Board metadata to a lane the repository does not have, and reporting one of
    several lanes as the source of a product-level answer would be equally
    untrue: the posture, capabilities, permissions, lifecycle, and hosted
    credential answers belong to the product and are stated once, while every
    executable answer is stated per lane against its own configuration. A
    repository without Devin produces no findings unless the caller explicitly
    asks for the unselected guidance.
    """
    selected = transport or selected_devin_transport(
        config, lanes=lanes, profile=config_profile
    )
    if selected is not None and selected not in TRANSPORTS:
        raise ConfigError("Devin transport must be devin_cli or devin_api_v3")
    if lane_configs:
        selected_lanes = tuple(lane_configs)
    elif selected is not None:
        selected_lanes = ((lane_id or TRANSPORTS[selected].review_lane, lane_config),)
    else:
        selected_lanes = ()
    lane_ids = tuple(name for name, _ in selected_lanes)
    custom_lanes = tuple(name for name in lane_ids if name not in CANONICAL_LANES)
    pin = _Pin(
        config_path=config_path,
        profile=config_profile or "",
        repo_slug=repo_slug,
        targeted_switch=not custom_lanes,
        custom_lanes=custom_lanes,
    )
    if selected is None:
        return _unselected_findings(pin) if include_unselected else ()
    # One selected lane owns every finding; several sharing the transport own only
    # their own runtime findings.
    lane = lane_ids[0] if len(lane_ids) == 1 else None
    findings = [
        _selection_finding(selected, lane, pin, lane_ids=lane_ids),
        _capability_finding(selected, lane),
    ]
    if selected == LOCAL_TRANSPORT:
        findings.extend(
            _local_cli_finding(
                name,
                lane_config=configured,
                env=os.environ if env is None else env,
                pin=pin,
                adoption_posture=adoption_posture,
            )
            for name, configured in selected_lanes or ((lane, None),)
        )
    else:
        findings.append(
            _hosted_credential_finding(
                lane,
                env=env,
                credential_file=credential_file,
                profile=profile,
                config_dir=config_dir,
                pin=pin,
            )
        )
        findings.append(
            _repository_scope_finding(
                lane,
                repo_slug=repo_slug,
                env=env,
                credential_file=credential_file,
                profile=profile,
                config_dir=config_dir,
                pin=pin,
            )
        )
    findings.append(_permission_finding(selected, lane))
    findings.append(_lifecycle_finding(selected, lane))
    return tuple(findings)
