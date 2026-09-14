"""Effective configured review authority for rendered status and audit comments.

Authority is never conferred by installation, a lane label, or a provider
identity. It is the maintained role decision in :mod:`role_eligibility`,
narrowed by the review lane declaration this run actually selected, so rendered
status must describe that computed result instead of a wrapper default. No new
policy is decided here: the lane declaration and ``decide_role`` are read, and
only the wording is produced.

Historical comments keep the wording they recorded when they were posted. This
module answers a different question -- what the current configured posture is --
so a past ``merge-authority lane`` header stays readable evidence without
becoming a claim about the posture of this run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .participants import PARTICIPANTS, reference_review_config
from .provider_capabilities import normalize_lane
from .provider_registry import REFERENCE_PROVIDERS
from .role_eligibility import decide_role
from .yaml_subset import ConfigError

SCHEMA = "code_mower.reviewAuthority.v1"

# Comment and session wording. The audit header wording is unchanged so that
# existing comment parsers and recorded fixtures keep matching.
MERGE_AUTHORITY_LABEL = "merge-authority lane"
INFORMATIONAL_LABEL = "informational only"
SESSION_MERGE_AUTHORITY_LABEL = "merge-authority lane"
SESSION_INFORMATIONAL_LABEL = "informational lane"

REPOSITORY_CONFIG_FILENAME = "code-mower.yml"


def authority_label(payload: Mapping[str, Any], *, session: bool = False) -> str:
    """Render the posture wording for a resolved authority payload."""
    if payload.get("merge_authority"):
        return SESSION_MERGE_AUTHORITY_LABEL if session else MERGE_AUTHORITY_LABEL
    return SESSION_INFORMATIONAL_LABEL if session else INFORMATIONAL_LABEL


def review_authority(
    product: str,
    *,
    config: Mapping[str, Any] | None = None,
    lane: str | None = None,
    transport: str | None = None,
    config_source: str = "packaged_default",
) -> dict[str, Any]:
    """Return the effective review posture for `product` under `config`.

    The lane declaration decides the configured posture, and the maintained role
    decision can only narrow it: a lane that declares merge authority but whose
    reviewer role is denied, incapable, or unqualified renders as informational
    rather than claiming an authority this run does not have. The reverse is not
    possible here, because an informational lane is passed to ``decide_role`` as
    informational and no eligible decision can widen it.
    """
    lane_id = lane or (PARTICIPANTS[product].review_lane if product in PARTICIPANTS else None)
    if not lane_id:
        raise ConfigError(f"{product} has no review lane to report authority for")
    lanes = config.get("lanes") if isinstance(config, Mapping) else None
    declared = lanes.get(lane_id) if isinstance(lanes, Mapping) else None
    if isinstance(declared, Mapping):
        policy_source = "repository"
        declaration = normalize_lane(lane_id, declared, config=config)
    elif lane_id in REFERENCE_PROVIDERS:
        policy_source = "starter"
        declaration = normalize_lane(lane_id, reference_review_config(lane_id), config=config)
    else:
        raise ConfigError(f"unknown review lane {lane_id!r}")
    configured = bool(declaration.get("merge_authority")) and not bool(
        declaration.get("informational")
    )
    qualification = declaration.get("role_qualification")
    decision = decide_role(
        product,
        "reviewer",
        transport=transport,
        config=config,
        merge_authority=configured,
        qualification=qualification if isinstance(qualification, str) else None,
    )
    narrowed = decision["scope"] != "unrestricted" or decision["status"] == "ineligible"
    merge_authority = configured and not narrowed
    if not configured:
        reason = "lane_informational"
    elif narrowed:
        reason = decision["reason"] if decision["status"] == "ineligible" else "role_scope_informational"
    else:
        reason = "lane_merge_authority"
    payload = {
        "schema": SCHEMA,
        "product": product,
        "lane": lane_id,
        "policy_source": policy_source,
        "config_source": config_source,
        "configured_merge_authority": configured,
        "merge_authority": merge_authority,
        "scope": decision["scope"],
        "reason": reason,
        "eligibility": decision,
    }
    payload["label"] = authority_label(payload)
    return payload


def resolve_repository_config(
    *, config_path: str | Path | None = None, repo_root: str | Path | None = None
) -> tuple[Mapping[str, Any] | None, str]:
    """Load the repository configuration that decides this run's posture.

    An explicitly selected configuration is authoritative: it is never replaced
    by the packaged starter, and an unreadable one is an error rather than a
    silent downgrade to a different posture. Without an explicit selection, a
    repository configuration in the checkout is used when present, and only a
    checkout that configures nothing falls back to the maintained lane defaults.
    """
    from . import config as code_mower_config

    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"selected repository configuration not found: {path}")
        return code_mower_config.load_config(path), "explicit_repository_config"
    if repo_root is not None:
        candidate = Path(repo_root).expanduser() / REPOSITORY_CONFIG_FILENAME
        if candidate.is_file():
            return code_mower_config.load_config(candidate), "repository_config"
    return None, "packaged_default"


def effective_merge_authority(
    product: str,
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    lane: str | None = None,
    override: bool | None = None,
) -> dict[str, Any]:
    """Resolve the posture an audit wrapper should render for this run.

    An explicit operator override is still honoured, and is reported as such so
    the rendered posture always names where it came from.
    """
    if override is not None:
        payload = {
            "schema": SCHEMA,
            "product": product,
            "lane": lane or (PARTICIPANTS[product].review_lane if product in PARTICIPANTS else None),
            "policy_source": "operator",
            "config_source": "operator_override",
            "configured_merge_authority": override,
            "merge_authority": override,
            "scope": "unrestricted" if override else "informational",
            "reason": "operator_override",
        }
        payload["label"] = authority_label(payload)
        return payload
    config, config_source = resolve_repository_config(
        config_path=config_path, repo_root=repo_root
    )
    return review_authority(
        product, config=config, lane=lane, config_source=config_source
    )
