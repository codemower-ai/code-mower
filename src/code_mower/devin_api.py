#!/usr/bin/env python3
"""Release-campaign compatibility adapter and local Devin credentials."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .devin_sessions import (
    API_BASE as API_BASE,
    MAX_RESPONSE_BYTES as MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS as REQUEST_TIMEOUT_SECONDS,
    ApiRunner, DevinApiError, DevinClient, make_api_request as make_api_request,
)

from .campaign_adapters import (
    ADOPTION_RESULT_JSON_SCHEMA,
    DEFAULT_PACKAGE_SOURCE,
    build_qualification_prompt,
)

DEVIN_API_KEY_ENV = "DEVIN_API_KEY"
DEVIN_ORG_ID_ENV = "DEVIN_ORG_ID"
DEVIN_REPOSITORIES_ENV = "CODE_MOWER_DEVIN_REPOSITORIES"

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


def _campaign_error(code: str) -> str:
    return "devin_api_rejected" if code in {
        "invalid_request", "authentication_required", "permission_denied", "devin_api_rejected"
    } else "devin_api_unavailable"


def create_devin_session(
    org_id: str, payload: Mapping[str, Any], api_key: str, *,
    api_runner: ApiRunner | None = None, checkpoint=None,
) -> tuple[str, str]:
    """Compatibility wrapper. Campaigns supply a durable checkpoint callback."""
    try:
        client = DevinClient(org_id, api_key, api_runner=api_runner)
        return client.create(payload, checkpoint=checkpoint or (lambda attempt: None)), ""
    except DevinApiError as exc:
        return "", _campaign_error(exc.code)


def poll_devin_session(
    org_id: str, session_id: str, api_key: str, *, api_runner: ApiRunner | None = None,
) -> tuple[str, dict[str, Any] | None, str]:
    """Map shared lifecycle snapshots to the established campaign result contract."""
    try:
        session = DevinClient(org_id, api_key, api_runner=api_runner).get(session_id)
    except DevinApiError as exc:
        return "failed", None, _campaign_error(exc.code)
    if session.state in {"failed", "suspended"}:
        return "failed", None, "devin_session_failed"
    # Approval must still be resolved even if an intermediate result exists.
    if session.state == "owner_action" and session.reason == "approval_required":
        return "owner_action", None, "devin_waiting_for_owner"
    if session.structured_output is not None:
        return "complete", session.structured_output, ""
    if session.state == "owner_action":
        return "owner_action", None, "devin_waiting_for_owner"
    if session.state in {"terminated", "complete", "archived"}:
        return "failed", None, "hosted_result_rejected"
    return "running", None, ""
