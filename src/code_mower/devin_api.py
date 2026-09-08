#!/usr/bin/env python3
"""Bounded Devin Sessions API v3 transport for release campaigns."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from .campaign_adapters import (
    ADOPTION_RESULT_JSON_SCHEMA,
    DEFAULT_PACKAGE_SOURCE,
    build_qualification_prompt,
)

API_BASE = "https://api.devin.ai"
DEVIN_API_KEY_ENV = "DEVIN_API_KEY"
DEVIN_ORG_ID_ENV = "DEVIN_ORG_ID"
DEVIN_REPOSITORIES_ENV = "CODE_MOWER_DEVIN_REPOSITORIES"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 512 * 1024

_ORG_ID_RE = re.compile(r"^org-[A-Za-z0-9_-]+$")
# Session ids are opaque API references. Accept any bounded RFC 3986
# unreserved token instead of assuming Devin will retain one prefix forever.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

SAFE_ERROR_CODES = frozenset(
    {
        "devin_api_rejected",
        "devin_api_unavailable",
        "devin_session_failed",
        "devin_waiting_for_owner",
        "hosted_result_rejected",
    }
)

ApiRunner = Callable[[str, str, Mapping[str, Any] | None, Mapping[str, str]], Any]


class DevinApiError(Exception):
    """API failure carrying only a closed, persistence-safe reason code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in SAFE_ERROR_CODES else "devin_api_unavailable"
        super().__init__(self.code)


def validate_devin_org_id(org_id: str) -> bool:
    """Validate that org_id matches Devin's required format."""
    return bool(_ORG_ID_RE.fullmatch(org_id))


_validate_org_id = validate_devin_org_id


def _validate_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id))


def repository_scope_acknowledged(
    repo_slug: str,
    *,
    env: Mapping[str, str] | None = None,
    credential_file: Path | str | None = None,
    profile: str = "",
    config_dir: Path | str | None = None,
) -> bool:
    """Require an exact local acknowledgement for the requested GitHub repo.

    Devin v3 accepts an exact ``repos`` list on create but exposes no read-only
    endpoint that proves GitHub connection scope before paid work starts. This
    local acknowledgement closes that gap. The full configured inventory is
    never returned, printed, persisted, or uploaded.
    """
    if not _REPO_RE.fullmatch(repo_slug):
        return False
    current_env = os.environ if env is None else env
    configured_raw = str(current_env.get(DEVIN_REPOSITORIES_ENV) or "").strip()
    if not configured_raw:
        configured_raw = str(current_env.get("DEVIN_REPOSITORIES") or "").strip()

    if not configured_raw:
        from .provider_credentials import resolve_provider_credentials

        c_file = Path(credential_file) if credential_file else None
        c_dir = Path(config_dir) if config_dir else None
        resolution = resolve_provider_credentials(
            "devin",
            credential_file=c_file,
            profile=profile,
            config_dir=c_dir,
            env=current_env,
        )
        if resolution.has_credentials:
            configured_raw = str(
                resolution.credentials.get(DEVIN_REPOSITORIES_ENV)
                or resolution.credentials.get("DEVIN_REPOSITORIES")
                or ""
            ).strip()

    configured = {
        item.strip().casefold()
        for item in configured_raw.split(",")
        if item.strip()
    }
    return repo_slug.casefold() in configured


class DevinCredentials(tuple):
    """3-tuple of ``(api_key, org_id, missing_variable)`` preserving resolver diagnostics."""

    api_key: str
    org_id: str
    missing: str
    status: str
    message: str
    remediation: str
    source: str
    resolution: Any

    def __new__(
        cls,
        api_key: str,
        org_id: str,
        missing: str,
        *,
        status: str = "ok",
        message: str = "",
        remediation: str = "",
        source: str = "",
        resolution: Any = None,
    ) -> DevinCredentials:
        obj = super().__new__(cls, (api_key, org_id, missing))
        obj.api_key = api_key
        obj.org_id = org_id
        obj.missing = missing
        obj.status = status
        obj.message = message
        obj.remediation = remediation
        obj.source = source
        obj.resolution = resolution
        return obj

    @property
    def has_credentials(self) -> bool:
        return bool(self.status == "ok" and self.api_key and self.org_id and not self.missing)


def credentials_from_env(
    env: Mapping[str, str] | None = None,
    *,
    credential_file: Path | str | None = None,
    profile: str = "",
    config_dir: Path | str | None = None,
) -> DevinCredentials:
    """Return ``(api_key, org_id, missing_variable)`` without logging values."""
    current_env = os.environ if env is None else env
    from .provider_credentials import resolve_provider_credentials

    c_file = Path(credential_file) if credential_file else None
    c_dir = Path(config_dir) if config_dir else None
    resolution = resolve_provider_credentials(
        "devin",
        credential_file=c_file,
        profile=profile,
        config_dir=c_dir,
        env=current_env,
    )
    if resolution.has_credentials:
        r_key = str(resolution.credentials.get(DEVIN_API_KEY_ENV) or "").strip()
        r_org = str(resolution.credentials.get(DEVIN_ORG_ID_ENV) or "").strip()
        if r_key and r_org and _validate_org_id(r_org):
            return DevinCredentials(
                r_key,
                r_org,
                "",
                status="ok",
                message=resolution.message,
                remediation="",
                source=resolution.source,
                resolution=resolution,
            )
        missing_var = DEVIN_API_KEY_ENV if not r_key else DEVIN_ORG_ID_ENV
        return DevinCredentials(
            "",
            "",
            missing_var,
            status="malformed",
            message=resolution.message or f"Devin credential profile does not define a valid {missing_var}",
            remediation=resolution.remediation or f"Set a valid {missing_var} in profile or environment.",
            source=resolution.source,
            resolution=resolution,
        )

    missing = (
        resolution.missing_variables[0]
        if resolution.missing_variables
        else (DEVIN_API_KEY_ENV if resolution.status == "missing" else resolution.status)
    )
    return DevinCredentials(
        "",
        "",
        missing,
        status=resolution.status,
        message=resolution.message,
        remediation=resolution.remediation,
        source=resolution.source,
        resolution=resolution,
    )


def make_api_request(
    method: str,
    path: str,
    api_key: str,
    body: Mapping[str, Any] | None = None,
    *,
    api_runner: ApiRunner | None = None,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Make one request and return a bounded JSON object.

    The injected runner keeps tests entirely offline. Raw error bodies and
    exception messages never cross this boundary.
    """
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        if api_runner is not None:
            value = api_runner(method, url, body, headers)
        else:
            encoded = (
                json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
                if body is not None
                else None
            )
            request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DevinApiError("devin_api_unavailable")
            value = json.loads(raw.decode("utf-8"))
    except DevinApiError:
        raise
    except urllib.error.HTTPError as exc:
        code = "devin_api_rejected" if 400 <= exc.code < 500 else "devin_api_unavailable"
        raise DevinApiError(code) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
        raise DevinApiError("devin_api_unavailable") from exc
    if not isinstance(value, dict):
        raise DevinApiError("devin_api_unavailable")
    return dict(value)


def build_devin_session_payload(
    *,
    campaign_id: str,
    release_tag: str,
    package_spec: str,
    package_identity: str,
    normalized_version: str,
    qualification_context: str,
    starting_version: str,
    repo_slug: str,
    package_source: str = DEFAULT_PACKAGE_SOURCE,
    target_runtime: str = "",
) -> dict[str, Any]:
    """Build the exact-repo create body using Devin's v3 field names."""
    if not _REPO_RE.fullmatch(repo_slug):
        raise ValueError("repo_slug must be OWNER/REPO")
    prompt = build_qualification_prompt(
        provider="devin",
        release_tag=release_tag,
        package_spec=package_spec,
        package_identity=package_identity,
        normalized_version=normalized_version,
        qualification_context=qualification_context,
        starting_version=starting_version,
        package_source=package_source,
        python_bin="python3",
        target_runtime=target_runtime,
    )
    reconciliation_tag = hashlib.sha256(
        f"{campaign_id}:{release_tag}:{repo_slug}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "prompt": prompt,
        "repos": [repo_slug],
        "structured_output_required": True,
        "structured_output_schema": copy.deepcopy(ADOPTION_RESULT_JSON_SCHEMA),
        "tags": ["code-mower", "release-qualification", f"cm-{reconciliation_tag}"],
        "title": f"Code Mower {release_tag} qualification"[:128],
    }


def create_devin_session(
    org_id: str,
    payload: Mapping[str, Any],
    api_key: str,
    *,
    api_runner: ApiRunner | None = None,
) -> tuple[str, str]:
    """Create one paid session, returning a bounded reason on failure."""
    if not _validate_org_id(org_id):
        return "", "devin_api_rejected"
    try:
        data = make_api_request(
            "POST",
            f"/v3/organizations/{org_id}/sessions",
            api_key,
            payload,
            api_runner=api_runner,
        )
    except DevinApiError as exc:
        return "", exc.code
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _validate_session_id(session_id):
        return "", "devin_api_unavailable"
    return session_id, ""


def poll_devin_session(
    org_id: str,
    session_id: str,
    api_key: str,
    *,
    api_runner: ApiRunner | None = None,
) -> tuple[str, dict[str, Any] | None, str]:
    """Read one session snapshot.

    Returns ``(state, structured_output, reason)`` where state is one of
    ``running``, ``complete``, ``owner_action``, or ``failed``. Campaign watch
    owns repeated polling and the one-hour deadline.
    """
    if not _validate_org_id(org_id) or not _validate_session_id(session_id):
        return "failed", None, "devin_api_rejected"
    try:
        data = make_api_request(
            "GET",
            f"/v3/organizations/{org_id}/sessions/{session_id}",
            api_key,
            api_runner=api_runner,
        )
    except DevinApiError as exc:
        return "failed", None, exc.code
    status = str(data.get("status") or "")
    detail = str(data.get("status_detail") or "")
    if status in {"error", "suspended"} or detail in {
        "error",
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "total_session_limit_exceeded",
    }:
        return "failed", None, "devin_session_failed"
    if detail == "waiting_for_approval":
        return "owner_action", None, "devin_waiting_for_owner"
    result = data.get("structured_output")
    if isinstance(result, dict):
        return "complete", dict(result), ""
    if detail == "waiting_for_user":
        return "owner_action", None, "devin_waiting_for_owner"
    if status == "exit" or detail == "finished":
        return "failed", None, "hosted_result_rejected"
    return "running", None, ""
