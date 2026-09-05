#!/usr/bin/env python3
"""Maintained release-campaign adapters for local provider CLIs.

One shared qualification prompt plus small provider-specific argv builders
drive the real noninteractive command surfaces. Every provider CLI is invoked
through argv with ``shell=False``; prompts travel on stdin or a prompt file,
never through a shell. Provider stdout/stderr are parsed transiently and are
never persisted, uploaded, or echoed: only a closed, validated
``code_mower.adoptionResult.v1`` document is ever written to ``--output``.

The campaign runner invokes this module as::

    {python} -m code_mower.campaign_adapters --provider <name> \
        --provider-bin {command} --release-tag {release_tag} \
        --package-spec {package_spec} \
        --qualification-context {qualification_context} \
        --starting-version {starting_version} \
        --timeout-seconds {adapter_timeout} --output {output}

``{command}`` resolves to the installed provider CLI (the campaign refuses to
run when it is missing), ``{python}`` to the running interpreter, and
``{adapter_timeout}`` to the campaign timeout minus a margin so this adapter's
own provider timeout always fires first. The adapter never runs Code Mower's
own qualification locally and relabels it: the named provider CLI performs the
qualification described by the prompt and emits the result.

Verified noninteractive surfaces:

================= ==================== ====================================================
provider          verified CLI         invocation surface
================= ==================== ====================================================
``codex``         codex-cli 0.147.0    ``exec`` with stdin (``-``), ephemeral
                                      workspace-write approval, network config,
                                      ``--json``, schema/last-message output, ``-C``
``claude``        Claude Code 2.1.258  ``--print`` with stdin, ``--output-format json``,
                                      explicit tool/permission controls, ``--json-schema``
``antigravity``   agy 1.1.26          ``--print`` with a prompt file, ``--sandbox``,
                                      noninteractive approval, ``--new-project``,
                                      ``--add-dir``, ``--print-timeout``
``muse``          Muse Code 1.0.3      ``exec`` with ``--json``, ``--prompt-file``,
                                      ``--workspace``
================= ==================== ====================================================

Auth and home behavior follow each provider's existing wrapper: ambient login
state is used as-is, credentials are never copied into prompts or state, and
Antigravity/Muse refuse to run without their ambient-home opt-in or a
provider key, exactly like the audit wrappers.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import gemini_cli_audit_pr as code_mower_gemini_cli
    from code_mower import muse_cli_audit_pr as code_mower_muse_cli
    from code_mower.provider_runners import (
        DEFAULT_HOME_ENV_KEYS,
        build_allowlisted_child_env,
    )
    from code_mower.release_qualify import (
        _parse_exact_package_spec,
        _validate_qualification_context,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        validate_adoption_result_payload,
    )
else:
    from . import gemini_cli_audit_pr as code_mower_gemini_cli
    from . import muse_cli_audit_pr as code_mower_muse_cli
    from .provider_runners import DEFAULT_HOME_ENV_KEYS, build_allowlisted_child_env
    from .release_qualify import (
        _parse_exact_package_spec,
        _validate_qualification_context,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        validate_adoption_result_payload,
    )

SUPPORTED_ADAPTER_PROVIDERS = ("codex", "claude", "antigravity", "muse")

#: CLI versions the argv shapes above were verified against. Newer CLIs keep
#: working while the flags exist; a removed flag fails closed here.
VERIFIED_CLI_VERSIONS = {
    "codex": "codex-cli 0.147.0",
    "claude": "Claude Code 2.1.258",
    "antigravity": "agy 1.1.26",
    "muse": "Muse Code 1.0.3",
}

#: Outer campaign timeout minus this margin is the provider subprocess budget,
#: so the adapter's own timeout always fires before the campaign kills it.
INNER_TIMEOUT_MARGIN_SECONDS = 30
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 870

CODEX_MODEL_ENV_NAMES = ("CODE_MOWER_CODEX_MODEL", "CODEX_MODEL", "OPENAI_MODEL")
CODEX_CAMPAIGN_HOME_ENV = "CODE_MOWER_CODEX_CAMPAIGN_HOME"
CODEX_CAMPAIGN_CONFIG = """cli_auth_credentials_store = \"keyring\"
default_permissions = \"campaign\"

[features]
secret_auth_storage = true

[permissions.campaign.filesystem]
\":root\" = \"deny\"
\":minimal\" = \"read\"
\":workspace_roots\" = \"write\"

[permissions.campaign.network]
enabled = true
"""
CLAUDE_MODEL_ENV_NAME = "CLAUDE_AUDIT_MODEL"
CLAUDE_DEFAULT_MODEL = "sonnet"
CLAUDE_BUDGET_ENV_NAME = "CLAUDE_AUDIT_MAX_BUDGET_USD"
CLAUDE_DEFAULT_MAX_BUDGET_USD = "5.00"
CLAUDE_SANDBOX_SETTINGS: dict[str, Any] = {
    "permissions": {"allow": ["Bash"]},
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": ["~"], "denyWrite": ["~"]},
        "network": {"allowedDomains": ["pypi.org", "files.pythonhosted.org"]},
    },
}
ANTIGRAVITY_MODEL_ENV_NAMES = ("CODE_MOWER_ANTIGRAVITY_MODEL", "ANTIGRAVITY_MODEL")
ANTIGRAVITY_AMBIENT_HOME_ENV = "ANTIGRAVITY_CLI_USE_AMBIENT_HOME"
MUSE_MODEL_ENV_NAMES = ("CODE_MOWER_MUSE_MODEL", "MUSE_MODEL", "META_MUSE_MODEL")
MUSE_REASONING_ENV_NAMES = ("CODE_MOWER_MUSE_REASONING_EFFORT", "MUSE_REASONING_EFFORT")
MUSE_AMBIENT_HOME_ENV = "MUSE_CLI_USE_AMBIENT_HOME"
MUSE_DEFAULT_MAX_MODEL_STEPS = 12
ADAPTER_ENV_ALLOWLIST = (
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "USER",
    "LOGNAME",
    "SHELL",
    # Linux Secret Service/keyring coordinates. These identify the local
    # session bus; they are not credentials and are required for Codex's
    # keyring-only campaign home to retrieve its stored login.
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
)

#: Guidance schema handed to providers with structured-output support (Codex
#: ``--output-schema``, Claude ``--json-schema``). Best effort only: the
#: closed validator in :mod:`code_mower.release_qualify` is authoritative.
ADOPTION_RESULT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "code_mower.adoptionResult.v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "timestamp_utc",
        "release_tag",
        "package_identity",
        "normalized_version",
        "qualification_context",
        "starting_version",
        "ending_version",
        "provider",
        "executor",
        "host_class",
        "runtime_class",
        "execution_state",
        "elapsed_seconds",
        "outcome",
        "steps",
    ],
    "properties": {
        "schema": {"type": "string"},
        "timestamp_utc": {"type": "string"},
        "release_tag": {"type": "string"},
        "package_identity": {"type": "string"},
        "normalized_version": {"type": "string"},
        "qualification_context": {
            "type": "string",
            "enum": ["cold_install", "upgrade", "unknown"],
        },
        "starting_version": {"type": "string"},
        "ending_version": {"type": "string"},
        "provider": {"type": "string"},
        "executor": {"type": "string"},
        "host_class": {
            "type": "string",
            "enum": ["local", "ci", "github_actions", "unknown"],
        },
        "runtime_class": {"type": "string"},
        "execution_state": {"type": "string", "enum": ["planned", "executed"]},
        "elapsed_seconds": {"type": "number", "minimum": 0},
        "outcome": {
            "type": "string",
            "enum": ["pass", "pass_with_warnings", "fail", "incomplete"],
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "status",
                    "elapsed_seconds",
                    "warning_count",
                    "owner_action_count",
                ],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "Built-in qualification step id (board, doctor, "
                            "lanes_status, overhead, package_install) or a namespaced "
                            "<namespace>__<name> provider extension"
                        ),
                        "pattern": (
                            "^(board|doctor|lanes_status|overhead|package_install|"
                            "[a-z][a-z0-9_]{0,31}__[a-z][a-z0-9_]{0,31})$"
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pass", "fail", "warn", "unavailable", "planned"],
                    },
                    "elapsed_seconds": {"type": "number", "minimum": 0},
                    "warning_count": {"type": "integer", "minimum": 0},
                    "owner_action_count": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env_value(names: Sequence[str]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_qualification_prompt(
    *,
    provider: str,
    release_tag: str,
    package_spec: str,
    package_identity: str,
    normalized_version: str,
    qualification_context: str,
    starting_version: str,
    python_bin: str = "python3",
    target_runtime: str = "",
) -> str:
    """Build the shared release-qualification prompt for one provider run.

    The prompt carries only campaign identity (provider, tag, spec, context,
    versions). It never includes credentials, home-directory paths, tokens, or
    local checkout paths: the agent works in a fresh disposable directory it
    creates itself.
    """
    python_cmd = shlex.quote(python_bin or "python3")
    if qualification_context == "upgrade":
        install_plan = (
            "1. In the current disposable directory, run "
            f"`{python_cmd} -m venv .venv`. Use only `.venv/bin/python` and installed "
            "entry points for the remaining steps.\n"
            f"2. Install the starting version with `.venv/bin/python -m pip install "
            f"{package_identity}=={starting_version}` to rehearse "
            f"an upgrade from exactly that version.\n"
            f'3. Upgrade with `.venv/bin/python -m pip install "{package_spec}"`.'
        )
    else:
        install_plan = (
            "1. In the current disposable directory, run "
            f"`{python_cmd} -m venv .venv`. Use only `.venv/bin/python` and installed "
            "entry points for the remaining steps.\n"
            f"2. Install the exact release with `.venv/bin/python -m pip install "
            f'"{package_spec}"`. No other version is acceptable.'
        )
    lines = [
        f"You are the {provider} release-qualification agent for Code Mower.",
        "Qualify exactly one release in a disposable environment you create.",
        "Do all work inside a fresh temporary directory; do not read or modify",
        "any existing checkout, home directory, credential file, or environment",
        "variable holding a secret. Never print secrets, tokens, file paths,",
        "commands you ran, or raw logs in your final answer.",
        "",
        "Binding (echo these values back exactly in your result):",
        f"- provider: {provider}",
        f"- executor: {provider}",
        f"- release_tag: {release_tag}",
        f"- package_identity: {package_identity}",
        f"- normalized_version: {normalized_version}",
        f"- package_spec: {package_spec}",
        f"- qualification_context: {qualification_context}",
        f"- starting_version: {starting_version if starting_version else '(empty)'}",
    ]
    if target_runtime:
        lines.append(f"- target_runtime: {target_runtime}")
    lines.extend(
        [
            "",
            "Procedure (measure wall-clock seconds for each step):",
        install_plan,
        f"4. Assert the installed {package_identity} version is exactly",
        f"   {normalized_version}, using `.venv/bin/python -c` and",
        "   `importlib.metadata.version` to read installed metadata.",
        "5. Smoke-check the installed distribution using its `.venv/bin/`",
        "   entry point (for code-mower run `.venv/bin/code-mower --help` and",
        "   `.venv/bin/code-mower doctor --help`) and confirm the",
        "   operational surfaces respond.",
        "6. Report failures honestly: a failed step has status fail and the",
        "   overall outcome follows the rule below. Never invent timings or",
        "   claim work you did not perform.",
        "",
        "Final answer: output ONLY one JSON object with schema",
        "code_mower.adoptionResult.v1 and exactly these fields:",
        "schema, timestamp_utc, release_tag, package_identity,",
        "normalized_version, qualification_context, starting_version,",
        "ending_version, provider, executor, host_class, runtime_class,",
        "execution_state, elapsed_seconds, outcome, steps.",
        "Rules: schema is code_mower.adoptionResult.v1; timestamp_utc is a UTC",
        "timestamp like 2026-09-04T08:00:00Z; qualification_context is one of",
        "cold_install, upgrade, unknown; execution_state is executed;",
        f"ending_version is exactly {normalized_version}, without a leading v,",
        "on pass/pass_with_warnings (and is empty on an incomplete run);",
        "host_class is one of local, ci, github_actions, unknown;",
        (
            f"runtime_class is exactly {target_runtime}; provider and"
            if target_runtime
            else "runtime_class is python_<major>.<minor>; provider and"
        ),
        "executor are lowercase safe identifiers; steps is a non-empty list of",
        "{id, status, elapsed_seconds, warning_count, owner_action_count} with",
        "id one of board, doctor, lanes_status, overhead, package_install or a namespaced",
        "<namespace>__<name> provider extension (both halves lowercase safe",
        "identifiers); any other id is rejected. Status is one of",
        "pass, fail, warn, unavailable, planned. A step with a",
        "nonzero warning_count must use status warn. Derive outcome only from",
        "the step status strings: fail if any status is fail;",
        "pass_with_warnings if any status is warn or unavailable; otherwise",
        "pass. In particular, all-pass steps require outcome pass. No extra",
        "fields, no prose, no fences.",
    ])
    return "\n".join(lines) + "\n"


def build_codex_argv(
    *,
    codex_bin: str,
    schema_path: str,
    last_message_path: str,
    workdir: str,
    model: str = "",
) -> list[str]:
    """Argv for ``codex exec``: stdin prompt, JSONL events, output schema.

    The agent works in the disposable ``workdir`` (``-C``). The
    ``--approve-for-me`` route supplies Codex's workspace-write sandbox so it
    can create a virtualenv, install the release, and run smoke checks there.
    Outbound network access is enabled inside that sandbox so it can download
    packages; it is not domain-restricted. The run stays ephemeral and
    workspace-isolated -- never
    ``danger-full-access``. The final
    agent message is additionally written by Codex itself to
    ``last_message_path`` (``--output-last-message``); that file is parsed
    transiently and never leaves the adapter's temporary directory.
    """
    argv = [
        codex_bin,
        "exec",
        "--ephemeral",
        "--approve-for-me",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--skip-git-repo-check",
        "--json",
        "--output-schema",
        schema_path,
        "--output-last-message",
        last_message_path,
        "-C",
        workdir,
    ]
    if model:
        argv.extend(["--model", model])
    argv.append("-")
    return argv


def build_claude_argv(
    *,
    claude_bin: str,
    model: str,
    max_budget_usd: str,
    schema_json: str,
    workspace_dir: str = "",
) -> list[str]:
    """Argv for ``claude --print``: stdin prompt, single JSON envelope.

    Mirrors the audit wrapper's isolation flags: no session persistence,
    local settings only, an empty MCP config, and no slash commands. The
    agent gets exactly one tool -- ``Bash`` -- inside Claude's OS-level
    sandbox. Sandboxed commands are auto-approved, the sandbox must be
    available, its unsandboxed escape hatch is disabled, home reads/writes are
    denied, and network access is limited to the package index. The disabled
    escape hatch makes anything outside that boundary fail closed.
    """
    argv = [
        claude_bin,
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--setting-sources",
        "local",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--restricted",
        "--tools",
        "Bash",
        "--settings",
        json.dumps(CLAUDE_SANDBOX_SETTINGS, separators=(",", ":"), sort_keys=True),
    ]
    if workspace_dir:
        argv.extend(["--add-dir", workspace_dir])
    argv.extend(
        [
            "--model",
            model,
            "--max-budget-usd",
            max_budget_usd,
            "--json-schema",
            schema_json,
        ]
    )
    return argv


def build_antigravity_argv(
    *,
    agy_bin: str,
    workspace_dir: str,
    prompt_file: str,
    timeout_seconds: int,
    model: str = "",
) -> list[str]:
    """Argv for ``agy --print`` with prompt-file transport and project isolation.

    Mirrors the Antigravity audit wrapper (via the Gemini CLI wrapper): the
    prompt lives in a file inside the workspace and the agent is pointed at it
    with a short instruction, sandboxed to that workspace. Headless agy cannot
    prompt for command permission, so permission checks are auto-approved only
    inside that retained sandbox. Every qualification run passes
    ``--new-project`` so the session executes inside a fresh project boundary
    and never inherits active conversations or resume semantics.
    """
    argv = [
        agy_bin,
        "--sandbox",
        "--dangerously-skip-permissions",
        "--new-project",
        "--add-dir",
        workspace_dir,
        "--print-timeout",
        f"{timeout_seconds}s",
    ]
    if model:
        argv.extend(["--model", model])
    argv.extend(
        [
            "--print",
            f"Read {prompt_file} from the allowed workspace. Follow it as "
            "the complete Code Mower release-qualification prompt. Return only "
            "the requested JSON result.",
        ]
    )
    return argv


def build_muse_argv(
    *,
    muse_bin: str,
    prompt_file: str,
    workspace_dir: str,
    max_model_steps: int,
    model: str = "",
    reasoning_effort: str = "",
    api_key_via_stdin: bool = False,
) -> list[str]:
    """Argv for ``muse exec``: JSONL events with a prompt file.

    Meta provider, no approvals (headless), and policy-gated workspace tools
    rooted at the disposable workspace. The managed shell and workspace
    writes stay enabled -- every qualification step (virtualenv, install,
    version read-back, smoke checks) executes commands there -- while the
    workspace sandbox, no foreign personal context, no session log, and
    disabled web tools keep the run isolated.
    """
    argv = [
        muse_bin,
        "exec",
        "--json",
        "--prompt-file",
        prompt_file,
        "--workspace",
        workspace_dir,
        "--provider",
        "meta",
        "--approval-mode",
        "never",
        "--disable-web-tools",
        "--no-foreign-personal-context",
        "--no-session-log",
        "--max-model-steps",
        str(max(1, max_model_steps)),
    ]
    if api_key_via_stdin:
        argv.append("--api-key-stdin")
    if model:
        argv.extend(["--model", model])
    if reasoning_effort:
        argv.extend(["--reasoning-effort", reasoning_effort])
    return argv


# ----- Transient result extraction (never persisted) -----


def _extract_claude_result(stdout: str) -> dict[str, Any] | None:
    """Pull the adoption result out of a Claude ``--output-format json`` envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, Mapping):
        if envelope.get("is_error") is True:
            return None
        structured = envelope.get("structured_output")
        if isinstance(structured, Mapping):
            return dict(structured)
        result = envelope.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        if isinstance(result, str) and result.strip():
            parsed = code_mower_gemini_cli.parse_response_json(result)
            if parsed is not None:
                return parsed
        return None
    return code_mower_gemini_cli.parse_response_json(stdout)


def _extract_antigravity_result(stdout: str) -> dict[str, Any] | None:
    """Pull the adoption result out of ``agy --print`` output.

    Mirrors the audit wrapper's parse order: a JSON envelope's ``response``
    field first, then the whole output as JSON.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, Mapping):
        response = envelope.get("response")
        if isinstance(response, Mapping):
            return dict(response)
        if isinstance(response, str) and response.strip():
            parsed = code_mower_gemini_cli.parse_response_json(response)
            if parsed is not None:
                return parsed
    return code_mower_gemini_cli.parse_response_json(stdout)


def _extract_muse_result(stdout: str) -> dict[str, Any] | None:
    """Pull the adoption result out of ``muse exec --json`` JSONL events."""
    response_text, _meta = code_mower_muse_cli.muse_jsonl_response(stdout)
    if response_text:
        parsed = code_mower_gemini_cli.parse_response_json(response_text)
        if parsed is not None:
            return parsed
    return code_mower_gemini_cli.parse_response_json(stdout)


def check_structured_result_capability(provider: str) -> bool:
    """Validate structured-result extraction and schema compliance using an offline fixture.

    Uses a zero-network, zero-token in-memory fixture to verify that Code Mower
    can correctly parse and validate adoption results from this provider.
    """
    from datetime import datetime, timedelta, timezone
    from code_mower.release_qualify import validate_adoption_result_payload

    canonical = provider.lower().replace("-", "_")
    recent = datetime.now(timezone.utc) - timedelta(seconds=60)
    sample_payload = {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": recent.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release_tag": "v1.0.8",
        "package_identity": "code-mower",
        "normalized_version": "1.0.8",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.8",
        "provider": canonical,
        "executor": canonical,
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 1.0,
        "outcome": "pass",
        "steps": [
            {
                "id": "doctor",
                "status": "pass",
                "elapsed_seconds": 1.0,
                "warning_count": 0,
                "owner_action_count": 0,
            }
        ],
    }
    payload_str = json.dumps(sample_payload)
    try:
        if canonical in {"claude_audit", "claude"}:
            claude_envelope = json.dumps({"is_error": False, "result": payload_str})
            extracted = _extract_claude_result(claude_envelope)
        elif canonical in {"antigravity", "antigravity_cli"}:
            extracted = _extract_antigravity_result(payload_str)
        elif canonical in {"muse", "muse_cli"}:
            muse_event = json.dumps({"payload_type": "run.output.delta", "payload": {"text": payload_str}})
            extracted = _extract_muse_result(muse_event)
        else:
            extracted = code_mower_gemini_cli.parse_response_json(payload_str)
        if not isinstance(extracted, Mapping):
            return False
        validate_adoption_result_payload(extracted)
        return True
    except Exception:
        return False


def check_antigravity_readiness(
    agy_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Detect whether installed agy CLI supports --new-project for campaign isolation.

    Fails closed with bounded actionable metadata (never leaking stdout/stderr,
    prompts, paths, auth details, or secrets) when the installed agy lacks the flag.
    """
    if not agy_bin:
        return {
            "ready": False,
            "provider": "antigravity",
            "capability": "new_project",
            "required_flag": "--new-project",
            "error": "command_not_found",
            "actionable": True,
            "message": "antigravity CLI is not installed",
            "remediation": "Install agy CLI on PATH or specify the executable in code-mower.yml.",
        }

    try:
        if runner is not None:
            try:
                completed = runner([agy_bin, "--help"], timeout=timeout_seconds)
            except TypeError:
                completed = runner([agy_bin, "--help"])
        else:
            completed = subprocess.run(
                [agy_bin, "--help"],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        has_new_project = (completed.returncode == 0) and ("--new-project" in output)
    except (subprocess.TimeoutExpired, OSError):
        has_new_project = False

    if not has_new_project:
        return {
            "ready": False,
            "provider": "antigravity",
            "capability": "new_project",
            "required_flag": "--new-project",
            "error": "missing_new_project_capability",
            "actionable": True,
            "message": "installed agy CLI lacks required --new-project flag for campaign isolation",
            "remediation": "Upgrade agy CLI to a version whose --help exposes --new-project.",
        }

    return {
        "ready": True,
        "provider": "antigravity",
        "capability": "new_project",
        "required_flag": "--new-project",
        "error": "",
        "actionable": False,
        "message": "agy CLI supports --new-project campaign isolation",
        "remediation": "",
    }


def check_antigravity_new_project_capability(
    agy_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout_seconds: int = 10,
) -> bool:
    """Return True if installed agy supports --new-project, False otherwise."""
    return bool(
        check_antigravity_readiness(
            agy_bin,
            runner=runner,
            timeout_seconds=timeout_seconds,
        ).get("ready")
    )


def validate_bound_result(
    candidate: Any,
    *,
    provider: str,
    release_tag: str,
    package_identity: str,
    qualification_context: str,
    starting_version: str,
    target_runtime: str = "",
) -> dict[str, Any]:
    """Closed-validate a transient candidate and bind it to this campaign.

    Raises ``ValueError`` with a bounded message (never provider output) when
    the candidate is not a schema-valid adoption result for exactly this
    provider, release tag, package, context, and starting version.
    """
    if not isinstance(candidate, dict):
        raise ValueError("provider did not emit a JSON result object")
    validate_adoption_result_payload(candidate, expected_package_identity=package_identity)
    if candidate.get("provider") != provider:
        raise ValueError("provider result identity mismatch")
    if candidate.get("executor") != provider:
        raise ValueError("provider result identity mismatch")
    if candidate.get("release_tag") != release_tag:
        raise ValueError("provider result identity mismatch")
    if candidate.get("qualification_context") != qualification_context:
        raise ValueError("provider result identity mismatch")
    if str(candidate.get("starting_version") or "") != starting_version:
        raise ValueError("provider result identity mismatch")
    if target_runtime and candidate.get("runtime_class") != target_runtime:
        raise ValueError("provider result identity mismatch")
    return candidate


def _write_result_atomically(output_path: Path, result: Mapping[str, Any]) -> None:
    """Publish the validated result with a single atomic rename."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(output_path.parent),
        prefix=".tmp.",
        suffix=".json",
        delete=False,
    ) as handle:
        json.dump(dict(result), handle, indent=2, sort_keys=True)
        handle.write("\n")
        staging = Path(handle.name)
    os.replace(staging, output_path)


#: ``(argv, prompt_input, timeout, workdir, child_env) -> CompletedProcess``.
#: Never uses a shell; stdout/stderr are returned for transient parsing only.
ProviderRunner = Callable[
    [Sequence[str], str | None, int, Path, Mapping[str, str]],
    "subprocess.CompletedProcess[str]",
]


def run_provider_command(
    argv: Sequence[str],
    prompt_input: str | None,
    timeout_seconds: int,
    workdir: Path,
    child_env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Default argv-only provider runner with an explicit minimal environment."""
    return subprocess.run(
        list(argv),
        input=prompt_input,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        cwd=str(workdir),
        env=dict(child_env),
    )


def _default_codex_campaign_home() -> Path:
    configured = os.environ.get(CODEX_CAMPAIGN_HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "code-mower" / "provider-homes" / "codex"


def prepare_codex_campaign_home(codex_home: Path | None = None) -> Path:
    """Create the non-secret, keyring-backed home used by campaign Codex runs."""
    home = (codex_home or _default_codex_campaign_home()).expanduser().resolve()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    if (home / "auth.json").exists():
        raise ValueError("isolated Codex home contains readable file credentials")
    config_path = home / "config.toml"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(home),
        prefix=".config.",
        suffix=".toml",
        delete=False,
    ) as handle:
        handle.write(CODEX_CAMPAIGN_CONFIG)
        staging = Path(handle.name)
    staging.chmod(0o600)
    os.replace(staging, config_path)
    return home


def build_adapter_child_env(provider: str, *, codex_home: Path | None = None) -> dict[str, str]:
    """Return the minimal ambient environment required by one provider CLI.

    Provider API keys, GitHub tokens, Code Mower cloud tokens, and unrelated
    shell state never reach the child. Claude, Antigravity, and Muse retain the
    ambient home only for their existing login stores. Codex retains only the
    OS HOME needed to locate the platform keyring while CODEX_HOME points at
    Code Mower's isolated config and state directory. Muse's explicit key uses
    stdin.
    """
    allowlist = list(ADAPTER_ENV_ALLOWLIST)
    child_env = build_allowlisted_child_env(
        allowlist,
        preserve_ambient_home=True,
        ambient_home_keys=("HOME",) if provider == "codex" else DEFAULT_HOME_ENV_KEYS,
    )
    if provider == "codex":
        home = (codex_home or _default_codex_campaign_home()).expanduser().resolve()
        child_env["CODEX_HOME"] = str(home)
    return child_env


def _fail(provider: str, reason: str) -> int:
    """Report a bounded adapter failure. Never echoes provider output or paths."""
    print(f"error: {provider} campaign adapter: {reason}", file=sys.stderr)
    return 1


def _resolve_provider_bin(provider: str, provider_bin: str) -> str | None:
    """Resolve the provider CLI without executing it."""
    if not provider_bin:
        return None
    located = shutil.which(provider_bin)
    if located:
        return located
    candidate = Path(provider_bin).expanduser()
    if candidate.is_file():
        return str(candidate)
    return None


def _check_campaign_identity(
    *,
    release_tag: str,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
) -> tuple[str, str]:
    """Return (package_identity, normalized_version) or raise ValueError."""
    valid, normalized_version, tag_error = _validate_tag_format(release_tag)
    if not valid:
        raise ValueError(f"invalid release tag: {tag_error}")
    package_identity, spec_version = _parse_exact_package_spec(package_spec)
    if spec_version != normalized_version:
        raise ValueError("release tag and package spec versions disagree")
    _validate_qualification_context(qualification_context)
    _validate_starting_version(starting_version)
    if qualification_context == "upgrade":
        if not starting_version:
            raise ValueError("upgrade qualification requires starting_version")
        if _version_key(starting_version) >= _version_key(normalized_version):
            raise ValueError("starting_version must be lower than the release version")
    elif starting_version:
        raise ValueError("starting_version is only valid for upgrade qualification")
    return package_identity, normalized_version


def run_campaign_adapter(
    *,
    provider: str,
    provider_bin: str,
    release_tag: str,
    package_spec: str,
    qualification_context: str,
    starting_version: str,
    output_path: Path,
    timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    model: str = "",
    claude_max_budget_usd: str = "",
    muse_max_model_steps: int = MUSE_DEFAULT_MAX_MODEL_STEPS,
    muse_reasoning_effort: str = "",
    codex_home: Path | None = None,
    python_bin: str = "",
    target_runtime: str = "",
    provider_runner: ProviderRunner = run_provider_command,
    capability_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    """Run one maintained provider adapter. Returns a process exit code.

    Exit 0 if and only if the provider CLI ran and a validated, bound
    adoption result was written to ``output_path``. Every other outcome exits
    non-zero with a bounded stderr message and writes nothing: raw provider
    output is diagnostic-only and transient.
    """
    if provider not in SUPPORTED_ADAPTER_PROVIDERS:
        return _fail(provider, "unsupported campaign adapter provider")
    if timeout_seconds <= 0:
        return _fail(provider, "timeout must be a positive number of seconds")

    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        return _fail(provider, "could not clear the prior result file")

    resolved_bin = _resolve_provider_bin(provider, provider_bin)
    if resolved_bin is None:
        return _fail(provider, "provider CLI is not installed")

    try:
        package_identity, normalized_version = _check_campaign_identity(
            release_tag=release_tag,
            package_spec=package_spec,
            qualification_context=qualification_context,
            starting_version=starting_version,
        )
    except ValueError as exc:
        return _fail(provider, str(exc)[:180])

    prompt = build_qualification_prompt(
        provider=provider,
        release_tag=release_tag,
        package_spec=package_spec,
        package_identity=package_identity,
        normalized_version=normalized_version,
        qualification_context=qualification_context,
        starting_version=starting_version,
        python_bin=python_bin,
        target_runtime=target_runtime,
    )
    prepared_codex_home: Path | None = None
    if provider == "codex":
        try:
            prepared_codex_home = prepare_codex_campaign_home(codex_home)
        except (OSError, ValueError):
            return _fail(provider, "isolated keyring-backed auth home is not usable")
    child_env = build_adapter_child_env(provider, codex_home=prepared_codex_home)

    muse_api_key = ""
    if provider == "antigravity" and not _env_flag_enabled(ANTIGRAVITY_AMBIENT_HOME_ENV):
        return _fail(
            provider,
            f"local OAuth requires {ANTIGRAVITY_AMBIENT_HOME_ENV}=1 in a trusted environment",
        )
    if provider == "muse":
        muse_api_key = code_mower_muse_cli.resolve_muse_api_key()
        if not muse_api_key and not _env_flag_enabled(MUSE_AMBIENT_HOME_ENV):
            return _fail(
                provider,
                f"provider auth requires META_API_KEY or {MUSE_AMBIENT_HOME_ENV}=1",
            )
        if not muse_reasoning_effort:
            muse_reasoning_effort = _first_env_value(MUSE_REASONING_ENV_NAMES)

    try:
        with tempfile.TemporaryDirectory(prefix="code-mower-campaign-") as temp_name:
            workdir = Path(temp_name)
            workspace_dir = workdir / "workspace"
            workspace_dir.mkdir()
            if provider == "codex":
                schema_path = workdir / "adoption-result.schema.json"
                schema_path.write_text(
                    json.dumps(ADOPTION_RESULT_JSON_SCHEMA, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                last_message_path = workdir / "last-message.md"
                codex_model = model or _first_env_value(CODEX_MODEL_ENV_NAMES)
                argv = build_codex_argv(
                    codex_bin=resolved_bin,
                    schema_path=str(schema_path),
                    last_message_path=str(last_message_path),
                    workdir=str(workdir),
                    model=codex_model,
                )
                completed = provider_runner(argv, prompt, timeout_seconds, workdir, child_env)
                if completed.returncode != 0:
                    return _fail(
                        provider,
                        f"provider CLI exited {completed.returncode}",
                    )
                try:
                    last_message = last_message_path.read_text(encoding="utf-8")
                except OSError:
                    return _fail(provider, "provider CLI wrote no result message")
                candidate: Any = code_mower_gemini_cli.parse_response_json(last_message)
            elif provider == "claude":
                claude_model = model or os.environ.get(CLAUDE_MODEL_ENV_NAME, "").strip()
                claude_model = claude_model or CLAUDE_DEFAULT_MODEL
                budget = (
                    claude_max_budget_usd.strip()
                    or os.environ.get(CLAUDE_BUDGET_ENV_NAME, "").strip()
                    or CLAUDE_DEFAULT_MAX_BUDGET_USD
                )
                argv = build_claude_argv(
                    claude_bin=resolved_bin,
                    model=claude_model,
                    max_budget_usd=budget,
                    schema_json=json.dumps(ADOPTION_RESULT_JSON_SCHEMA, separators=(",", ":")),
                    workspace_dir=str(workdir),
                )
                completed = provider_runner(argv, prompt, timeout_seconds, workdir, child_env)
                if completed.returncode != 0:
                    return _fail(
                        provider,
                        f"provider CLI exited {completed.returncode}",
                    )
                candidate = _extract_claude_result(completed.stdout)
            elif provider == "antigravity":
                readiness = check_antigravity_readiness(
                    resolved_bin,
                    runner=capability_runner,
                )
                if not readiness["ready"]:
                    return _fail(provider, readiness["message"])
                prompt_path = workspace_dir / "campaign.prompt-input.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                agy_model = model or _first_env_value(ANTIGRAVITY_MODEL_ENV_NAMES)
                argv = build_antigravity_argv(
                    agy_bin=resolved_bin,
                    workspace_dir=str(workspace_dir),
                    prompt_file=str(prompt_path),
                    timeout_seconds=timeout_seconds,
                    model=agy_model,
                )
                completed = provider_runner(argv, None, timeout_seconds, workspace_dir, child_env)
                if completed.returncode != 0:
                    return _fail(
                        provider,
                        f"provider CLI exited {completed.returncode}",
                    )
                candidate = _extract_antigravity_result(completed.stdout)
            else:  # provider == "muse"
                prompt_path = workspace_dir / "campaign.prompt-input.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                muse_model = model or _first_env_value(MUSE_MODEL_ENV_NAMES)
                argv = build_muse_argv(
                    muse_bin=resolved_bin,
                    prompt_file=str(prompt_path),
                    workspace_dir=str(workspace_dir),
                    max_model_steps=muse_max_model_steps,
                    model=muse_model,
                    reasoning_effort=muse_reasoning_effort,
                    api_key_via_stdin=bool(muse_api_key),
                )
                # The API key travels only on stdin to the provider CLI; it is
                # never written to disk, the prompt, or campaign state.
                stdin_text: str | None = muse_api_key if muse_api_key else None
                completed = provider_runner(
                    argv, stdin_text, timeout_seconds, workspace_dir, child_env
                )
                if completed.returncode != 0:
                    return _fail(
                        provider,
                        f"provider CLI exited {completed.returncode}",
                    )
                candidate = _extract_muse_result(completed.stdout)
    except subprocess.TimeoutExpired:
        return _fail(provider, f"provider CLI exceeded {timeout_seconds}s")
    except OSError:
        return _fail(provider, "provider CLI is not installed")

    try:
        result = validate_bound_result(
            candidate,
            provider=provider,
            release_tag=release_tag,
            package_identity=package_identity,
            qualification_context=qualification_context,
            starting_version=starting_version,
            target_runtime=target_runtime,
        )
    except ValueError as exc:
        return _fail(provider, str(exc)[:180])

    try:
        _write_result_atomically(output_path, result)
    except OSError:
        return _fail(provider, "could not write the validated result file")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Maintained release-campaign adapter: invoke one provider CLI to "
            "qualify a release and write a validated adoption result."
        )
    )
    parser.add_argument("--provider", required=True, choices=SUPPORTED_ADAPTER_PROVIDERS)
    parser.add_argument("--provider-bin", required=True)
    parser.add_argument("--python-bin", default="")
    parser.add_argument("--target-runtime", default="")
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--package-spec", required=True)
    parser.add_argument("--qualification-context", required=True)
    parser.add_argument("--starting-version", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_PROVIDER_TIMEOUT_SECONDS)
    parser.add_argument("--model", default="")
    parser.add_argument("--claude-max-budget-usd", default="")
    parser.add_argument("--muse-max-model-steps", type=int, default=MUSE_DEFAULT_MAX_MODEL_STEPS)
    parser.add_argument("--muse-reasoning-effort", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be a positive number of seconds")
    if args.muse_max_model_steps <= 0:
        parser.error("muse-max-model-steps must be a positive number of steps")
    return run_campaign_adapter(
        provider=args.provider,
        provider_bin=args.provider_bin,
        release_tag=args.release_tag,
        package_spec=args.package_spec,
        qualification_context=args.qualification_context,
        starting_version=args.starting_version,
        output_path=args.output,
        timeout_seconds=args.timeout_seconds,
        model=args.model,
        claude_max_budget_usd=args.claude_max_budget_usd,
        muse_max_model_steps=args.muse_max_model_steps,
        muse_reasoning_effort=args.muse_reasoning_effort,
        python_bin=args.python_bin,
        target_runtime=args.target_runtime,
    )


if __name__ == "__main__":
    raise SystemExit(main())
