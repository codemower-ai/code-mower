"""Role admission, independent of transport selection and runtime readiness.

Qualification records are maintained evidence, not assertions accepted from a
provider, config boolean, or UI. Repository policy can narrow the maintained
decision. Adding a qualification requires its own reviewed evidence change.
The pure decision is shared by session/setup and execution adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from .yaml_subset import ConfigError


SCHEMA = "code_mower.roleEligibility.v1"
ROLES = frozenset({"builder", "orchestrator", "reviewer"})
PRODUCTS = frozenset({
    "claude", "codex", "devin", "cursor", "grok-bot", "antigravity", "muse",
    "gitar", "cursor-bugbot", "qodo", "greptile",
})
REVIEW_ONLY = frozenset({"gitar", "cursor-bugbot", "qodo", "greptile"})


@dataclass(frozen=True)
class Qualification:
    product: str
    role: str
    transport: str
    capability: str
    scope: str
    evidence: str
    expires_at: datetime | None = None
    active: bool = True


# These are two distinct, bounded builder baselines. Neither is an orchestrator
# or reviewer qualification. No elapsed-time expiry was established by those
# decisions; revocation or a changed capability invalidates them independently.
QUALIFICATIONS = MappingProxyType({
    "devin-cli-builder-v1": Qualification(
        "devin", "builder", "devin_cli", "local_runner", "bounded",
        "https://github.com/codemower-ai/code-mower/issues/659",
    ),
    "devin-hosted-builder-v140": Qualification(
        "devin", "builder", "devin_api_v3", "agent_handoff", "bounded",
        "https://github.com/codemower-ai/code-mower/issues/900#issuecomment-5657839697",
    ),
})
DEFAULT_QUALIFICATIONS = MappingProxyType({
    ("devin", "builder", "devin_cli"): "devin-cli-builder-v1",
    ("devin", "builder", "devin_api_v3"): "devin-hosted-builder-v140",
})


def role_policy(config: Mapping[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate a narrowing policy, without reading files or accepting evidence."""
    if config is not None and not isinstance(config, Mapping):
        raise ConfigError("role_policy requires a repository configuration mapping")
    raw = (config or {}).get("role_policy", {})
    if not isinstance(raw, Mapping) or set(raw) - PRODUCTS:
        raise ConfigError("role_policy must map known participants to role settings")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for product, roles in raw.items():
        if not isinstance(roles, Mapping) or set(roles) - ROLES:
            raise ConfigError("role_policy roles must be builder, orchestrator, or reviewer")
        result[product] = {}
        for role, settings in roles.items():
            if not isinstance(settings, Mapping) or set(settings) - {"enabled", "qualification"}:
                raise ConfigError("role_policy settings allow only enabled and a maintained qualification ID")
            if "enabled" in settings and type(settings["enabled"]) is not bool:
                raise ConfigError("role_policy enabled must be true or false")
            reference = settings.get("qualification")
            if reference is not None and (
                not isinstance(reference, str) or not reference or len(reference) > 100
                or not all(c.isascii() and (c.isalnum() or c in "_-") for c in reference)
            ):
                raise ConfigError("role_policy qualification must name a maintained qualification ID")
            result[product][role] = dict(settings)
    return result


def decide_role(
    product: str, role: str, *, transport: str | None = None,
    config: Mapping[str, Any] | None = None, runtime: str = "unchecked",
    merge_authority: bool = False, bounded: bool = False,
    qualification: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Return closed metadata. Only execution may require runtime to be ready.

    Non-Devin roles retain their existing repository authority: this change
    does not recalibrate or promote those lanes. Runtime observations are
    supplied by the trusted execution adapter, never by task/provider prose.
    """
    if (not isinstance(product, str) or not isinstance(role, str)
            or product not in PRODUCTS or role not in ROLES
            or not isinstance(runtime, str)
            or runtime not in {"ready", "unchecked", "unavailable"}
            or type(merge_authority) is not bool or type(bounded) is not bool):
        raise ConfigError("invalid role eligibility request")
    settings = role_policy(config).get(product, {}).get(role, {})
    scope = "informational" if role == "reviewer" and not merge_authority else "unrestricted"
    mode = "agent_handoff"
    selected_transport = "agent_handoff"
    capable = role == "reviewer" or product not in REVIEW_ONLY
    if product == "devin":
        # Lazy import keeps transport validation and role decisions independent
        # while allowing the former to require the latter for review admission.
        from .provider_capabilities import resolve_transport
        execution = resolve_transport(transport or "devin_cli")
        selected_transport = execution.transport
        mode = getattr(execution.capabilities, {"orchestrator": "coordinate", "builder": "build", "reviewer": "review"}[role])
        capable = mode != "unavailable" and not (merge_authority and mode == "evidence_only")
        if role == "builder":
            scope = "bounded"
    qualification_state = "repository_policy"
    if product == "devin":
        qualification_state = "not_required" if scope == "informational" else "missing"
        if scope != "informational":
            reference = qualification if qualification is not None else settings.get(
                "qualification", DEFAULT_QUALIFICATIONS.get((product, role, selected_transport))
            )
            # A lane's explicit reference cannot override a narrower repository
            # reference. Both declarations must select the same maintained record.
            policy_reference = settings.get("qualification")
            if qualification is not None and policy_reference is not None and qualification != policy_reference:
                reference = None
            record = QUALIFICATIONS.get(reference) if isinstance(reference, str) else None
            if record is not None and (record.product, record.role, record.transport, record.scope) == (
                product, role, selected_transport, scope,
            ):
                instant = now or datetime.now(timezone.utc)
                if instant.tzinfo is None:
                    raise ConfigError("role eligibility time must include a timezone")
                stale = (not record.active or record.capability != mode
                         or (record.expires_at is not None and instant >= record.expires_at))
                qualification_state = "stale" if stale else "qualified"
    policy = "allowed" if settings.get("enabled", True) else "denied"
    if policy == "denied":
        reason = "policy_denied"
    elif not capable:
        reason = "capability_unavailable"
    elif qualification_state in {"missing", "stale"}:
        reason = "qualification_" + qualification_state
    elif product == "devin" and role == "builder" and not bounded:
        reason = "bounded_work_required"
    elif runtime == "unavailable":
        reason = "runtime_unavailable"
    elif runtime == "unchecked":
        reason = "runtime_unchecked"
    else:
        reason = "ready"
    return {
        "schema": SCHEMA, "product": product, "role": role,
        "transport": selected_transport, "scope": scope,
        "capability": "supported" if capable else "unavailable",
        "qualification": qualification_state, "policy": policy, "runtime": runtime,
        "status": "eligible" if reason == "ready" else "pending" if reason == "runtime_unchecked" else "ineligible",
        "reason": reason,
    }


def require_role(decision: Mapping[str, Any], *, execution: bool = False) -> None:
    """Require qualification before planning writes, and readiness before execution."""
    if decision["status"] == "eligible" or (decision["status"] == "pending" and not execution):
        return
    product, role, reason = decision["product"], decision["role"], decision["reason"]
    if reason in {"qualification_missing", "qualification_stale"}:
        detail = "missing or stale role-specific qualification"
    elif reason == "policy_denied":
        detail = "the repository role policy disables this role"
    elif reason == "bounded_work_required":
        detail = "only explicitly bounded builder work is qualified"
    elif reason == "capability_unavailable":
        detail = "the selected transport does not support this role"
    else:
        detail = "runtime readiness has not been verified"
    raise ConfigError(
        f"{product} cannot act as {role}: {detail}; select a qualified, ready participant "
        "or supply separately reviewed role qualification. No participant was substituted."
    )
