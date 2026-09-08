"""Read-only Jira Cloud tracker checks for ``doctor --adoption``.

These checks validate a configured ``tracker.jira_cloud`` block without any
mutation and without leaking secrets: diagnostics carry filenames and closed
reason codes only, never token values, emails, absolute paths, response
bodies, or issue prose. Live probes run only when the repository configures
a Jira Cloud tracker; GitHub-only repositories get no new checks.

Stable JSON check ids:

- ``tracker.jira.config`` -- tracker block shape (missing/malformed).
- ``tracker.jira.credentials`` -- credential posture (missing, malformed,
  ambiguous, insecure, or ready).
- ``tracker.jira.read`` -- live read probe (expired/unauthorized,
  forbidden, wrong-cloud, wrong-project, rate-limited, or ready).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .models import STATUS_FAIL, STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck


if TYPE_CHECKING:  # pragma: no cover - typing only; runtime import is lazy.
    from code_mower import jira_cloud as jira_cloud_module

JIRA_CONFIG_CHECK = "tracker.jira.config"
JIRA_CREDENTIALS_CHECK = "tracker.jira.credentials"
JIRA_READ_CHECK = "tracker.jira.read"

JiraClientFactory = Callable[..., Any]

_SETUP_JIRA_DOC = "docs/tracker-data-contract.md (Read-only Jira Cloud probe)"


def _skip_config(message: str) -> DoctorCheck:
    return DoctorCheck(
        name=JIRA_CONFIG_CHECK,
        status=STATUS_SKIP,
        message=message,
        detail={"tracker_kind": "github", "jira_configured": False},
    )


def _config_fail(message: str, remediation: str) -> DoctorCheck:
    return DoctorCheck(
        name=JIRA_CONFIG_CHECK,
        status=STATUS_FAIL,
        message=message,
        detail={"tracker_kind": "jira_cloud", "jira_configured": True},
        remediation=remediation,
    )


def _tracker_block(config: Mapping[str, Any] | None) -> tuple[str, Mapping[str, Any] | None]:
    if not isinstance(config, Mapping):
        return "github", None
    tracker = config.get("tracker")
    if not isinstance(tracker, Mapping):
        return "github", None
    kind = str(tracker.get("kind") or "github")
    block = tracker.get("jira_cloud")
    return kind, block if isinstance(block, Mapping) else None


def _validate_jira_config(block: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return (message, remediation) for the first config defect, else None."""
    from code_mower import jira_cloud as jira_cloud_module

    site_url = block.get("site_url")
    cloud_id = block.get("cloud_id")
    project_id = block.get("project_id")
    if not isinstance(site_url, str) or not site_url.startswith("https://"):
        return (
            "jira_cloud site_url must be an HTTPS Jira Cloud site URL",
            "Set tracker.jira_cloud.site_url to the HTTPS site URL "
            f"(for example {jira_cloud_module.SITE_EXAMPLE}). "
            f"See {_SETUP_JIRA_DOC}.",
        )
    if not isinstance(cloud_id, str) or not jira_cloud_module._CLOUD_ID_RE.fullmatch(cloud_id):
        return (
            "jira_cloud cloud_id is missing or malformed",
            "Set tracker.jira_cloud.cloud_id to the tenant cloud id "
            "(a bounded token of letters, digits, or hyphens). "
            f"See {_SETUP_JIRA_DOC}.",
        )
    if not isinstance(project_id, str) or not jira_cloud_module._PROJECT_ID_RE.fullmatch(
        project_id
    ):
        return (
            "jira_cloud project_id is missing or malformed",
            "Set tracker.jira_cloud.project_id to the immutable numeric "
            f"project id. See {_SETUP_JIRA_DOC}.",
        )
    return None


_CREDENTIAL_STATUS_TEXT = {
    "missing": "Jira credentials are missing",
    "malformed": "Jira credentials are malformed",
    "ambiguous": "multiple Jira credential profiles match",
    "insecure_permissions": "Jira credential file permissions are too broad",
}


def check_jira_tracker_readiness(
    *,
    config: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
    credential_file: Path | None = None,
    profile: str = "",
    config_dir: Path | None = None,
    client_factory: JiraClientFactory | None = None,
    keychain_runner: jira_cloud_module.KeychainRunner | None = None,
    http_timeout: int = 5,
) -> tuple[DoctorCheck, ...]:
    """Validate Jira Cloud tracker posture for ``doctor --adoption``.

    ``client_factory`` builds the live probe client and defaults to the real
    gateway client; tests inject a fake so CI performs no live Jira calls.
    """
    from code_mower import jira_cloud as jira_cloud_module

    current_env = os.environ if env is None else env
    kind, block = _tracker_block(config)
    if kind != "jira_cloud":
        return ()
    if block is None:
        return (
            _config_fail(
                "tracker.kind is jira_cloud but tracker.jira_cloud is missing",
                "Add a tracker.jira_cloud block with site_url, cloud_id, and "
                f"project_id. See {_SETUP_JIRA_DOC}.",
            ),
        )
    defect = _validate_jira_config(block)
    if defect is not None:
        message, remediation = defect
        return (_config_fail(message, remediation),)

    checks: list[DoctorCheck] = [
        DoctorCheck(
            name=JIRA_CONFIG_CHECK,
            status=STATUS_PASS,
            message="jira_cloud tracker block is configured",
            detail={"tracker_kind": "jira_cloud", "jira_configured": True},
        )
    ]

    resolution = jira_cloud_module.resolve_jira_credentials(
        credential_file=credential_file,
        profile=profile,
        config_dir=config_dir,
        env=current_env,
        keychain_runner=keychain_runner,
    )
    if not resolution.has_credentials:
        label = _CREDENTIAL_STATUS_TEXT.get(resolution.status, "Jira credentials are not ready")
        checks.append(
            DoctorCheck(
                name=JIRA_CREDENTIALS_CHECK,
                status=STATUS_FAIL,
                message=f"{label}: {resolution.message}",
                detail={**resolution.safe_detail(), "tracker_kind": "jira_cloud"},
                remediation=resolution.remediation,
            )
        )
        checks.append(
            DoctorCheck(
                name=JIRA_READ_CHECK,
                status=STATUS_SKIP,
                message="skipped Jira read probe until credentials resolve",
                detail={"tracker_kind": "jira_cloud", "reason": "credentials_not_ready"},
            )
        )
        return tuple(checks)

    checks.append(
        DoctorCheck(
            name=JIRA_CREDENTIALS_CHECK,
            status=STATUS_PASS,
            message=f"Jira credentials resolved ({resolution.source})",
            detail={**resolution.safe_detail(), "tracker_kind": "jira_cloud"},
        )
    )
    checks.append(
        _probe_jira_read(
            block,
            resolution,
            client_factory=client_factory,
            http_timeout=http_timeout,
        )
    )
    return tuple(checks)


def _probe_jira_read(
    block: Mapping[str, Any],
    resolution: Any,
    *,
    client_factory: JiraClientFactory | None,
    http_timeout: int,
) -> DoctorCheck:
    from code_mower import jira_cloud as jira_cloud_module

    cloud_id = str(block.get("cloud_id"))
    project_id = str(block.get("project_id"))
    site_url = str(block.get("site_url"))
    issue_type_id = block.get("issue_type_id")
    probe_target = str(issue_type_id) if isinstance(issue_type_id, str) and issue_type_id else ""

    def _fail(message: str, remediation: str, reason: str) -> DoctorCheck:
        return DoctorCheck(
            name=JIRA_READ_CHECK,
            status=STATUS_FAIL,
            message=message,
            detail={
                "tracker_kind": "jira_cloud",
                "reason": reason,
                "credential_source": resolution.source,
            },
            remediation=remediation,
        )

    if client_factory is None:

        def _default_factory(
            *,
            cloud_id: str,
            email: str,
            token: str,
            site_url: str = "",
            timeout_seconds: float = 5,
        ) -> jira_cloud_module.JiraReadClient:
            return jira_cloud_module.JiraReadClient(
                cloud_id=cloud_id,
                email=email,
                token=token,
                site_url=site_url,
                timeout_seconds=float(timeout_seconds),
            )

        factory: JiraClientFactory = _default_factory
    else:
        factory = client_factory
    try:
        client = factory(
            cloud_id=cloud_id,
            email=resolution.email,
            token=resolution.token,
            site_url=site_url,
            timeout_seconds=float(http_timeout),
        )
    except (ValueError, TypeError):
        return _fail(
            "Jira read probe could not start: tracker identity is malformed",
            f"Fix tracker.jira_cloud identity fields. See {_SETUP_JIRA_DOC}.",
            "malformed",
        )

    try:
        client.get_server_info()
    except jira_cloud_module.JiraApiError as exc:
        return _map_probe_error(exc, "serverInfo", _fail)
    try:
        project = client.get_project(project_id)
    except jira_cloud_module.JiraApiError as exc:
        if exc.code == "jira_not_found":
            return _fail(
                "Jira project was not found: wrong project for this cloud tenant",
                "Set tracker.jira_cloud.project_id to the immutable numeric "
                f"project id visible in {_SETUP_JIRA_DOC}.",
                "wrong-project",
            )
        return _map_probe_error(exc, "project", _fail)

    configured_key = block.get("project_key")
    if isinstance(configured_key, str) and configured_key:
        live_key = project.get("key", "")
        if live_key and live_key != configured_key:
            return _fail(
                "Jira project key does not match the configured display key",
                "Update tracker.jira_cloud.project_key to the live project key "
                "or remove it (project_id remains the query primitive).",
                "wrong-project",
            )

    try:
        statuses = client.get_statuses()
        categories = client.get_status_categories()
    except jira_cloud_module.JiraApiError as exc:
        return _map_probe_error(exc, "status", _fail)

    if probe_target:
        try:
            known_types = client.get_issue_types(project_id)
        except jira_cloud_module.JiraApiError as exc:
            return _map_probe_error(exc, "createMeta", _fail)
        if probe_target not in {item.get("id") for item in known_types}:
            return _fail(
                "Jira issue type probe target is unknown for this project",
                "Set tracker.jira_cloud.issue_type_id to one of the live "
                "create-metadata issue type ids.",
                "wrong-project",
            )
        try:
            required_fields = client.get_required_create_fields(project_id, probe_target)
        except jira_cloud_module.JiraApiError as exc:
            return _map_probe_error(exc, "createMeta", _fail)
    else:
        required_fields = []

    try:
        permissions = client.check_permissions(project_id)
    except jira_cloud_module.JiraApiError as exc:
        return _map_probe_error(exc, "permissions", _fail)

    try:
        jql = block.get("jql")
        query = str(jql).strip() if isinstance(jql, str) and jql.strip() else None
        if query is None:
            query = jira_cloud_module.build_project_jql(project_id=project_id)
        result = client.search_issues(query, max_issues=5)
    except (ValueError, jira_cloud_module.JiraApiError) as exc:
        if isinstance(exc, ValueError):
            return _fail(
                "Jira queue query is malformed",
                "Set tracker.jira_cloud.jql to a bounded single line "
                f"(see {_SETUP_JIRA_DOC}).",
                "malformed",
            )
        return _map_probe_error(exc, "search", _fail)

    configured_map = block.get("status_category_map")
    live_ids = [item.get("id", "") for item in statuses]
    unmapped = 0
    if isinstance(configured_map, Mapping):
        for status_id in live_ids:
            if jira_cloud_module.map_status_to_lifecycle(status_id, statuses, configured_map) is None:
                unmapped += 1

    issues = result.get("issues") if isinstance(result, dict) else []
    return DoctorCheck(
        name=JIRA_READ_CHECK,
        status=STATUS_PASS,
        message="Jira read probe passed (metadata only, no writes)",
        detail={
            "tracker_kind": "jira_cloud",
            "reason": "ready",
            "credential_source": resolution.source,
            "project_key": project.get("key", ""),
            "status_count": len(statuses),
            "status_category_count": len(categories),
            "unmapped_status_count": unmapped,
            "permission_probe": dict(permissions),
            "required_create_fields": list(required_fields),
            "sample_issue_count": len(issues) if isinstance(issues, list) else 0,
        },
    )


def _map_probe_error(
    exc: Any,
    probe: str,
    fail: Callable[[str, str, str], DoctorCheck],
) -> DoctorCheck:
    from code_mower import jira_cloud as jira_cloud_module

    if exc.code == "jira_unauthorized":
        return fail(
            "Jira token is expired, revoked, or invalid",
            "Rotate the API token for the configured account email, update "
            f"{jira_cloud_module.JIRA_TOKEN_ENV} or the stored profile, then "
            "re-run `code-mower doctor --adoption`.",
            "expired/unauthorized",
        )
    if exc.code == "jira_forbidden":
        return fail(
            "Jira account is forbidden from reading this tenant or project",
            "Grant the account Browse Projects permission on the configured "
            "project, then re-run `code-mower doctor --adoption`.",
            "forbidden",
        )
    if exc.code == "jira_not_found":
        if probe == "serverInfo":
            return fail(
                "Jira cloud tenant was not found: wrong cloud for this token",
                "Set tracker.jira_cloud.cloud_id to the tenant that issued "
                f"the token. See {_SETUP_JIRA_DOC}.",
                "wrong-cloud",
            )
        return fail(
            "Jira read probe target was not found",
            "Verify tracker.jira_cloud project identity against the live "
            f"tenant. See {_SETUP_JIRA_DOC}.",
            "wrong-project",
        )
    if exc.code == "jira_rate_limited":
        return DoctorCheck(
            name=JIRA_READ_CHECK,
            status=STATUS_WARN,
            message="Jira rate limit reached; probe deferred without writes",
            detail={"tracker_kind": "jira_cloud", "reason": "rate-limited", "probe": probe},
            remediation=(
                "Wait for gateway capacity to return, then re-run "
                "`code-mower doctor --adoption`. No dispatch or write was attempted."
            ),
        )
    if exc.code == "jira_cancelled":
        return DoctorCheck(
            name=JIRA_READ_CHECK,
            status=STATUS_SKIP,
            message="Jira read probe was cancelled",
            detail={"tracker_kind": "jira_cloud", "reason": "cancelled", "probe": probe},
        )
    return fail(
        "Jira read probe is unavailable (transient gateway or network failure)",
        "Re-run `code-mower doctor --adoption`. A rate-limited or failed "
        "probe never weakens GitHub gate semantics or duplicates dispatches.",
        "unavailable",
    )


__all__ = (
    "JIRA_CONFIG_CHECK",
    "JIRA_CREDENTIALS_CHECK",
    "JIRA_READ_CHECK",
    "check_jira_tracker_readiness",
)
