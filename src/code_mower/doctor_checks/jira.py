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
- ``tracker.jira.mutations`` -- mutation readiness and write guards posture
  (both mutation guards: writes_enabled config guard and runtime --apply guard;
  allowed operations, configured transitions, target status mapping, permissions).
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .models import STATUS_FAIL, STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck


if TYPE_CHECKING:  # pragma: no cover - typing only; runtime import is lazy.
    from code_mower import jira_cloud as jira_cloud_module

JIRA_CONFIG_CHECK = "tracker.jira.config"
JIRA_CREDENTIALS_CHECK = "tracker.jira.credentials"
JIRA_READ_CHECK = "tracker.jira.read"
JIRA_MUTATIONS_CHECK = "tracker.jira.mutations"

JiraClientFactory = Callable[..., Any]

_SETUP_JIRA_DOC = "docs/jira-cloud-setup.md"


def _site_identity(value: str) -> tuple[str, str, int | None, str] | None:
    """Return the comparable, credential-free identity for a Jira site URL."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    return (
        "https",
        parsed.hostname.lower(),
        port,
        parsed.path.rstrip("/"),
    )


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
    from code_mower import tracker_contract

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

    project_key = block.get("project_key")
    if project_key is not None and (
        not isinstance(project_key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", project_key)
    ):
        return (
            "jira_cloud project_key is malformed",
            "Set tracker.jira_cloud.project_key to an alphanumeric display key. "
            f"See {_SETUP_JIRA_DOC}.",
        )

    issue_type_id = block.get("issue_type_id")
    if issue_type_id is not None and (
        not isinstance(issue_type_id, str) or not issue_type_id.strip()
    ):
        return (
            "jira_cloud issue_type_id is malformed",
            "Set tracker.jira_cloud.issue_type_id to a non-empty string issue type id. "
            f"See {_SETUP_JIRA_DOC}.",
        )

    jql = block.get("jql")
    if jql is not None and (
        not isinstance(jql, str) or len(jql) > 2000 or "\n" in jql or "\r" in jql
    ):
        return (
            "jira_cloud jql query is malformed",
            "Set tracker.jira_cloud.jql to a single line of at most 2000 characters. "
            f"See {_SETUP_JIRA_DOC}.",
        )

    status_category_map = block.get("status_category_map")
    if status_category_map is not None:
        if not isinstance(status_category_map, Mapping):
            return (
                "jira_cloud status_category_map must be a mapping",
                f"Map categories ({sorted(tracker_contract.LIFECYCLE_CATEGORIES)}) to lists of status IDs. "
                f"See {_SETUP_JIRA_DOC}.",
            )
        for cat, sids in status_category_map.items():
            if cat not in tracker_contract.LIFECYCLE_CATEGORIES:
                return (
                    f"jira_cloud status_category_map has invalid category '{cat}'",
                    f"Category must be one of {sorted(tracker_contract.LIFECYCLE_CATEGORIES)}. "
                    f"See {_SETUP_JIRA_DOC}.",
                )
            if not isinstance(sids, (list, tuple)) or not sids or not all(
                isinstance(sid, str) and sid for sid in sids
            ):
                return (
                    f"jira_cloud status_category_map for category '{cat}' must be a list of status id strings",
                    f"Set status IDs for category '{cat}' to a list of numeric string IDs. "
                    f"See {_SETUP_JIRA_DOC}.",
                )

    field_mappings = block.get("field_mappings")
    if field_mappings is not None:
        if not isinstance(field_mappings, Mapping):
            return (
                "jira_cloud field_mappings must be a mapping",
                f"Map safe target fields ({sorted(tracker_contract.SAFE_FIELD_MAPPING_TARGETS)}) to Jira fields. "
                f"See {_SETUP_JIRA_DOC}.",
            )
        for target, src in field_mappings.items():
            if target not in tracker_contract.SAFE_FIELD_MAPPING_TARGETS:
                return (
                    f"jira_cloud field_mappings targets unsupported field '{target}'",
                    f"Target must be one of {sorted(tracker_contract.SAFE_FIELD_MAPPING_TARGETS)}. "
                    f"See {_SETUP_JIRA_DOC}.",
                )
            if not isinstance(src, str) or not src.strip():
                return (
                    f"jira_cloud field_mappings for '{target}' must be a non-empty string field id",
                    f"Set source field id for '{target}' to a non-empty string. "
                    f"See {_SETUP_JIRA_DOC}.",
                )

    mutations = block.get("mutations")
    if mutations is not None:
        if not isinstance(mutations, Mapping):
            return (
                "jira_cloud mutations must be a mapping",
                f"Configure mutations with writes_enabled, allowed_operations, and transitions. "
                f"See {_SETUP_JIRA_DOC}.",
            )
        extra_mutation_keys = set(mutations) - {"writes_enabled", "allowed_operations", "transitions"}
        if extra_mutation_keys:
            bad_key = sorted(extra_mutation_keys)[0]
            return (
                f"jira_cloud mutations has unknown key '{bad_key}'",
                f"Allowed keys are writes_enabled, allowed_operations, transitions. "
                f"See {_SETUP_JIRA_DOC}.",
            )
        if "writes_enabled" in mutations and not isinstance(mutations["writes_enabled"], bool):
            return (
                "jira_cloud mutations writes_enabled must be a boolean",
                f"Set tracker.jira_cloud.mutations.writes_enabled to true or false. "
                f"See {_SETUP_JIRA_DOC}.",
            )
        allowed_ops = mutations.get("allowed_operations")
        if allowed_ops is not None:
            if not isinstance(allowed_ops, (list, tuple)):
                return (
                    "jira_cloud mutations allowed_operations must be a list",
                    f"Allowed operations must be a subset of {sorted(tracker_contract.ALLOWED_MUTATION_OPERATIONS)}. "
                    f"See {_SETUP_JIRA_DOC}.",
                )
            for op in allowed_ops:
                if op not in tracker_contract.ALLOWED_MUTATION_OPERATIONS:
                    return (
                        f"jira_cloud mutations operation '{op}' is not supported",
                        f"Operation must be one of {sorted(tracker_contract.ALLOWED_MUTATION_OPERATIONS)} (delete is excluded). "
                        f"See {_SETUP_JIRA_DOC}.",
                    )
        transitions = mutations.get("transitions")
        if transitions is not None:
            if not isinstance(transitions, Mapping):
                return (
                    "jira_cloud mutations transitions must be a mapping",
                    f"Map lifecycle categories to numeric Jira transition IDs. "
                    f"See {_SETUP_JIRA_DOC}.",
                )
            for cat, tid in transitions.items():
                if cat not in tracker_contract.LIFECYCLE_CATEGORIES:
                    return (
                        f"jira_cloud mutations transition category '{cat}' is invalid",
                        f"Category must be one of {sorted(tracker_contract.LIFECYCLE_CATEGORIES)}. "
                        f"See {_SETUP_JIRA_DOC}.",
                    )
                if not isinstance(tid, str) or not re.fullmatch(r"[0-9]{1,32}", tid):
                    return (
                        f"jira_cloud mutations transition id for category '{cat}' must be a numeric string",
                        f"Set transition ID to a numeric string (for example '31'). "
                        f"See {_SETUP_JIRA_DOC}.",
                    )
                # Check status_category_map has target status for this category
                status_map = status_category_map if isinstance(status_category_map, Mapping) else {}
                cat_targets = status_map.get(cat)
                if not isinstance(cat_targets, (list, tuple)) or not cat_targets:
                    return (
                        f"configured transition for category '{cat}' requires target status IDs in status_category_map",
                        f"Configure target status IDs for category '{cat}' in tracker.jira_cloud.status_category_map. "
                        f"See {_SETUP_JIRA_DOC}.",
                    )

    extra_keys = set(block) - {
        "site_url",
        "cloud_id",
        "project_id",
        "project_key",
        "issue_type_id",
        "jql",
        "status_category_map",
        "field_mappings",
        "mutations",
    }
    if extra_keys:
        bad_key = sorted(extra_keys)[0]
        return (
            f"unknown tracker.jira_cloud configuration key '{bad_key}'",
            f"Remove unsupported key '{bad_key}' from tracker.jira_cloud. "
            f"See {_SETUP_JIRA_DOC}.",
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
        checks.append(
            DoctorCheck(
                name=JIRA_MUTATIONS_CHECK,
                status=STATUS_SKIP,
                message="skipped Jira mutations check until credentials resolve",
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
    read_check = _probe_jira_read(
        block,
        resolution,
        client_factory=client_factory,
        http_timeout=http_timeout,
    )
    checks.append(read_check)
    if read_check.status == STATUS_PASS:
        checks.append(_check_jira_mutations(block, read_check))
    elif read_check.status == STATUS_WARN:
        checks.append(
            DoctorCheck(
                name=JIRA_MUTATIONS_CHECK,
                status=STATUS_SKIP,
                message="skipped Jira mutations check due to rate limiting",
                detail={"tracker_kind": "jira_cloud", "reason": "rate-limited"},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name=JIRA_MUTATIONS_CHECK,
                status=STATUS_SKIP,
                message="skipped Jira mutations check until live read probe passes",
                detail={"tracker_kind": "jira_cloud", "reason": "read_probe_failed"},
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
        server_info = client.get_server_info()
    except jira_cloud_module.JiraApiError as exc:
        return _map_probe_error(exc, "serverInfo", _fail)
    if _site_identity(str(server_info.get("base_url") or "")) != _site_identity(
        site_url
    ):
        return _fail(
            "Jira cloud tenant identity does not match the configured site",
            "Set tracker.jira_cloud.cloud_id and site_url to the same Jira Cloud "
            f"tenant. See {_SETUP_JIRA_DOC}.",
            "wrong-cloud",
        )
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
    if permissions.get("BROWSE_PROJECTS") is not True:
        return _fail(
            "Jira account cannot browse the configured project",
            "Grant the account Browse Projects permission on the configured "
            "project, then re-run `code-mower doctor --adoption`.",
            "forbidden",
        )

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
    transition_target_mismatches: list[dict[str, str]] = []
    mutations = block.get("mutations")
    configured_transitions = mutations.get("transitions") if isinstance(mutations, Mapping) else None
    if isinstance(configured_transitions, Mapping) and configured_transitions and issues:
        sample_key = issues[0].get("key")
        if sample_key:
            try:
                available_transitions = client.get_transitions(sample_key)
                for cat, tid in configured_transitions.items():
                    for t in available_transitions:
                        if t.get("id") == str(tid):
                            to_status = t.get("to_status_id", "")
                            cat_targets = (
                                configured_map.get(cat, [])
                                if isinstance(configured_map, Mapping)
                                else []
                            )
                            if to_status and to_status not in cat_targets:
                                transition_target_mismatches.append(
                                    {
                                        "category": cat,
                                        "transition_id": str(tid),
                                        "to_status_id": to_status,
                                    }
                                )
            except jira_cloud_module.JiraApiError:
                pass

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
            "known_status_ids": live_ids,
            "transition_target_mismatches": transition_target_mismatches,
        },
    )


def _check_jira_mutations(
    block: Mapping[str, Any],
    read_check: DoctorCheck,
) -> DoctorCheck:
    mutations = block.get("mutations")
    if not isinstance(mutations, Mapping):
        return DoctorCheck(
            name=JIRA_MUTATIONS_CHECK,
            status=STATUS_PASS,
            message="Jira mutations disabled (read-only tracker posture)",
            detail={
                "tracker_kind": "jira_cloud",
                "mutations_configured": False,
                "writes_enabled": False,
            },
        )

    writes_enabled = mutations.get("writes_enabled") is True
    allowed_operations = list(mutations.get("allowed_operations") or [])
    transitions = dict(mutations.get("transitions") or {})
    status_category_map = block.get("status_category_map")
    status_map = dict(status_category_map) if isinstance(status_category_map, Mapping) else {}

    detail_in = read_check.detail if isinstance(read_check.detail, Mapping) else {}
    permission_probe = dict(detail_in.get("permission_probe") or {})
    known_status_ids = set(detail_in.get("known_status_ids") or [])
    mismatches = list(detail_in.get("transition_target_mismatches") or [])

    if mismatches:
        m = mismatches[0]
        return DoctorCheck(
            name=JIRA_MUTATIONS_CHECK,
            status=STATUS_FAIL,
            message=(
                f"transition '{m.get('transition_id')}' for category '{m.get('category')}' "
                f"leads to status '{m.get('to_status_id')}', which is not configured in status_category_map"
            ),
            detail={
                "tracker_kind": "jira_cloud",
                "reason": "transition_target_mismatch",
                "mismatches": mismatches,
            },
            remediation=(
                f"Add status '{m.get('to_status_id')}' to tracker.jira_cloud.status_category_map.{m.get('category')} "
                f"or update the configured transition ID in tracker.jira_cloud.mutations.transitions."
            ),
        )

    # Check required permissions for allowed_operations and configured transitions
    needed_ops = set(allowed_operations)
    if transitions:
        needed_ops.add("transition")
    op_permissions = {
        "assign": "EDIT_ISSUES",
        "transition": "TRANSITION_ISSUES",
        "comment": "ADD_COMMENTS",
        "link": "EDIT_ISSUES",
    }
    missing_perms: list[str] = []
    for op in sorted(needed_ops):
        perm = op_permissions.get(op)
        if perm and permission_probe.get(perm) is not True:
            if perm not in missing_perms:
                missing_perms.append(perm)

    if missing_perms:
        return DoctorCheck(
            name=JIRA_MUTATIONS_CHECK,
            status=STATUS_FAIL,
            message=f"Jira account lacks permission(s) for configured mutations: {', '.join(missing_perms)}",
            detail={
                "tracker_kind": "jira_cloud",
                "reason": "permission_denied",
                "missing_permissions": missing_perms,
                "permission_probe": permission_probe,
            },
            remediation=(
                f"Grant the Jira account {', '.join(missing_perms)} on the configured project, "
                f"or remove unsupported operations from tracker.jira_cloud.mutations.allowed_operations. "
                f"See {_SETUP_JIRA_DOC}."
            ),
        )

    # Check configured transitions have target statuses that exist in Jira
    if known_status_ids:
        for cat in transitions:
            cat_targets = status_map.get(cat, [])
            if isinstance(cat_targets, (list, tuple)):
                for sid in cat_targets:
                    if str(sid) not in known_status_ids:
                        return DoctorCheck(
                            name=JIRA_MUTATIONS_CHECK,
                            status=STATUS_FAIL,
                            message=(
                                f"target status ID '{sid}' for category '{cat}' does not exist in Jira project"
                            ),
                            detail={
                                "tracker_kind": "jira_cloud",
                                "reason": "status_not_found",
                                "category": cat,
                                "status_id": str(sid),
                            },
                            remediation=(
                                f"Update tracker.jira_cloud.status_category_map.{cat} to use valid status IDs "
                                f"from your Jira project workflow. See {_SETUP_JIRA_DOC}."
                            ),
                        )

    msg = (
        "Jira mutations configured (writes enabled)"
        if writes_enabled
        else "Jira mutations configured (writes disabled by config guard)"
    )
    return DoctorCheck(
        name=JIRA_MUTATIONS_CHECK,
        status=STATUS_PASS,
        message=msg,
        detail={
            "tracker_kind": "jira_cloud",
            "mutations_configured": True,
            "writes_enabled": writes_enabled,
            "allowed_operations": allowed_operations,
            "transitions": transitions,
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
    if exc.code == "jira_rejected":
        return fail(
            "Jira rejected the read probe request",
            "Verify the Jira tracker identity and read configuration, then "
            "re-run `code-mower doctor --adoption`.",
            "rejected",
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
    "JIRA_MUTATIONS_CHECK",
    "check_jira_tracker_readiness",
)
