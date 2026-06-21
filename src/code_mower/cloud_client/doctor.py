"""Cloud upload readiness diagnostics."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

from code_mower.provider_registry import REFERENCE_PROVIDERS

from .bundle import BUNDLE_MANIFEST_FILENAME
from .endpoints import (
    dashboard_url_for_endpoint,
    health_url_for_endpoint,
    is_local_http_endpoint,
    probe_cloud_service,
    validate_upload_endpoint,
)
from .errors import CloudBundleError
from .upload import build_upload_payload


MODEL_PROVENANCE_ENV_EXAMPLES = (
    "CODE_MOWER_CODEX_MODEL",
    "CODE_MOWER_GEMINI_MODEL",
    "CODE_MOWER_ANTIGRAVITY_MODEL",
    "CODE_MOWER_HERMES_MODEL",
)


def _validate_upload_endpoint(endpoint: str) -> None:
    try:
        validate_upload_endpoint(endpoint)
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc


def _provider_model_env_names(providers: list[str]) -> dict[str, list[str]]:
    requested = set(providers)
    env_by_provider: dict[str, list[str]] = {}
    for lane in REFERENCE_PROVIDERS.values():
        if lane.provider not in requested:
            continue
        config = lane.provider_config
        env_names: list[str] = []
        model_env = config.get("model_env")
        if isinstance(model_env, str) and model_env:
            env_names.append(model_env)
        model_env_any = config.get("model_env_any", ())
        if isinstance(model_env_any, str):
            env_names.append(model_env_any)
        elif isinstance(model_env_any, (list, tuple)):
            env_names.extend(str(name) for name in model_env_any if str(name))
        if env_names:
            existing = env_by_provider.setdefault(lane.provider, [])
            existing.extend(env_names)
    return {
        provider: sorted(dict.fromkeys(env_names))
        for provider, env_names in sorted(env_by_provider.items())
    }


def _provider_model_env_commands(providers: list[str]) -> list[str]:
    if not providers:
        return ["code-mower providers provenance-env --shell"]
    return [
        f"code-mower providers provenance-env --provider {shlex.quote(provider)} --shell"
        for provider in sorted(providers)
    ]


def _provider_model_env_command_all(providers: list[str]) -> str:
    if not providers:
        return "code-mower providers provenance-env --shell"
    provider_args = " ".join(
        f"--provider {shlex.quote(provider)}" for provider in sorted(providers)
    )
    return f"code-mower providers provenance-env {provider_args} --shell"


def run_cloud_doctor(
    *,
    bundle_dir: Path,
    endpoint: str,
    token_env: str,
    require_token: bool = True,
    probe_service: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    endpoint_is_valid = False

    try:
        _validate_upload_endpoint(endpoint)
        endpoint_is_valid = True
        checks.append(
            {
                "name": "endpoint",
                "status": "pass",
                "message": f"upload endpoint is allowed: {endpoint}",
            }
        )
    except CloudBundleError as exc:
        checks.append(
            {
                "name": "endpoint",
                "status": "fail",
                "message": str(exc),
            }
        )

    if probe_service and endpoint_is_valid:
        checks.append(probe_cloud_service(endpoint, timeout=timeout))
    elif endpoint_is_valid:
        checks.append(
            {
                "name": "service",
                "status": "skip",
                "message": "service health probe skipped; pass --probe-service to check it",
                "detail": {"health_url": health_url_for_endpoint(endpoint)},
            }
        )

    token_present = bool(os.environ.get(token_env, ""))
    if token_present:
        checks.append(
            {
                "name": "token",
                "status": "pass",
                "message": f"{token_env} is set",
            }
        )
    elif is_local_http_endpoint(endpoint):
        checks.append(
            {
                "name": "token",
                "status": "warn",
                "message": (
                    f"{token_env} is not set; local configless ingest may still work"
                ),
            }
        )
    elif not require_token:
        checks.append(
            {
                "name": "token",
                "status": "warn",
                "message": f"{token_env} is not set; upload will remain disabled",
                "remediation": (
                    "Run `code-mower cloud setup --token-stdin` or export "
                    f"{token_env} before using --yes."
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "token",
                "status": "fail",
                "message": f"{token_env} is not set",
                "remediation": (
                    "Set CODE_MOWER_CLOUD_TOKEN to a team ingest token before "
                    "running cloud upload --yes."
                ),
            }
        )

    manifest_path = bundle_dir / BUNDLE_MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            payload = build_upload_payload(bundle_dir=bundle_dir, include_reports=False)
            provenance = payload.get("provenance", {})
            checks.append(
                {
                    "name": "bundle",
                    "status": "pass",
                    "message": (
                        f"bundle is readable with {len(payload['reports'])} report summaries "
                        f"and {len(payload['events'])} structured events"
                    ),
                }
            )
            if isinstance(provenance, dict):
                missing_model_events = int(
                    provenance.get("events_missing_model_provenance") or 0
                )
                provenance_tools = provenance.get("tools", [])
                if not isinstance(provenance_tools, list):
                    provenance_tools = []
                missing_model_tools = sorted(
                    {
                        str(tool.get("provider") or tool.get("tool_name") or "unknown")
                        for tool in provenance_tools
                        if isinstance(tool, dict)
                        and "missing" in set(tool.get("model_sources") or [])
                    }
                )
                model_env_by_provider = _provider_model_env_names(missing_model_tools)
                model_env_examples = sorted(
                    {
                        name
                        for names in model_env_by_provider.values()
                        for name in names
                        if name.startswith("CODE_MOWER_")
                    }
                    or set(MODEL_PROVENANCE_ENV_EXAMPLES)
                )
                model_env_commands = _provider_model_env_commands(missing_model_tools)
                model_env_command_all = _provider_model_env_command_all(
                    missing_model_tools
                )
                if missing_model_events:
                    if len(model_env_commands) == 1:
                        remediation_command = model_env_commands[0]
                        remediation = (
                            "Run "
                            + remediation_command
                            + " to print safe export templates, then set "
                            "provider-specific model env vars before export or dogfood. "
                            "Examples: "
                            + ", ".join(model_env_examples)
                            + "."
                        )
                    else:
                        remediation = (
                            "Run the provider-specific commands in "
                            "`detail.model_env_commands` to print safe export templates, "
                            "or run the combined `detail.model_env_command_all` command. "
                            "Then set model env vars before export or dogfood. Examples: "
                            + ", ".join(model_env_examples)
                            + "."
                        )
                    checks.append(
                        {
                            "name": "model-provenance",
                            "status": "warn",
                            "message": (
                                f"{missing_model_events} events are missing model provenance"
                            ),
                            "detail": {
                                "providers": missing_model_tools,
                                "model_env_by_provider": model_env_by_provider,
                                "model_env_examples": model_env_examples,
                                "model_env_commands": model_env_commands,
                                "model_env_command_all": model_env_command_all,
                            },
                            "remediation": remediation,
                        }
                    )
                else:
                    checks.append(
                        {
                            "name": "model-provenance",
                            "status": "pass",
                            "message": "bundle has model provenance for all structured events",
                        }
                    )
        except CloudBundleError as exc:
            checks.append(
                {
                    "name": "bundle",
                    "status": "fail",
                    "message": str(exc),
                }
            )
    else:
        checks.append(
            {
                "name": "bundle",
                "status": "warn",
                "message": (
                    f"bundle manifest not found at {manifest_path}; run cloud export first"
                ),
            }
        )

    failures = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    return {
        "mode": "cloud-doctor",
        "status": "fail" if failures else "pass",
        "endpoint": endpoint,
        "dashboard_url": dashboard_url_for_endpoint(endpoint),
        "health_url": health_url_for_endpoint(endpoint),
        "token_env": token_env,
        "bundle_dir": str(bundle_dir),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "next_steps": [
            {
                "id": "open-dashboard",
                "label": "Open the Code Mower Cloud dashboard",
                "url": dashboard_url_for_endpoint(endpoint),
            },
            {
                "id": "setup-token",
                "label": "Store a dashboard-issued token locally",
                "command": (
                    "code-mower cloud setup --token-stdin "
                    "--team-id YOUR_TEAM_SLUG --install-id YOUR_INSTALL_ID"
                ),
            },
            {
                "id": "dry-run-upload",
                "label": "Preview the metadata upload without network transfer",
                "command": f"code-mower cloud upload {shlex.quote(str(bundle_dir))} --dry-run --json",
            },
            {
                "id": "upload",
                "label": "Upload metadata after inspecting the dry run",
                "command": f"code-mower cloud upload {shlex.quote(str(bundle_dir))} --yes --json",
            },
        ],
    }


def render_cloud_doctor_text(report: dict[str, Any]) -> str:
    lines = [
        "Code Mower cloud doctor",
        f"Status: {report['status']}",
        f"Endpoint: {report['endpoint']}",
        f"Dashboard: {report.get('dashboard_url', '')}",
        f"Token env: {report['token_env']}",
        f"Bundle: {report['bundle_dir']}",
        "",
    ]
    for check in report["checks"]:
        lines.append(
            f"- {check['status'].upper()} {check['name']}: {check['message']}"
        )
        if check.get("remediation"):
            lines.append(f"  remediation: {check['remediation']}")
    return "\n".join(lines) + "\n"
