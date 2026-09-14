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
# Implicit discovery reads the repository's active policy, which lives on the
# trusted base ref rather than in the PR-head checkout an audit runs against.
DEFAULT_BASE_REF = "origin/main"
# The trusted base could not be read at all, which is different from a base that
# verifiably tracks no configuration. An unknown policy supports no authority
# claim, so this source always renders informational with an actionable reason.
TRUSTED_BASE_UNAVAILABLE = "trusted_base_unavailable"
TRUSTED_BASE_UNAVAILABLE_ACTION = (
    "fetch the base ref this audit compares against (for example "
    "`git fetch origin main`) or pass --code-mower-config with the repository "
    "configuration to report, then rerun the audit"
)


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


def _trusted_base_config(repo_root: Path, base_ref: str) -> tuple[Mapping[str, Any] | None, str]:
    """Read `code-mower.yml` as the trusted base ref has it, never the checkout.

    An audit runs against a PR-head checkout, so the configuration sitting in the
    working tree is the change under review. Reporting a posture from it would
    let an unmerged promotion or demotion take effect in the comment header
    before it is approved, so implicit discovery reads the base ref the same way
    :func:`context_audit.required_for_repo` does.

    Only a successful trusted-tree lookup that proves the file is absent selects
    the maintained lane defaults: that base configures no lane, so the default is
    the repository's active policy. Everything else -- a missing or invalid base
    ref, a failed or timed-out Git command, tracked configuration that does not
    parse -- leaves the trusted answer unknown. Unknown is not evidence of a
    posture, so it is reported as unavailable rather than resolved either to the
    maintained default (which would grant starter authority nothing verified) or
    to the proposed head configuration (which is exactly what must not be read).
    """
    import subprocess

    from .config import _YamlSubsetParser

    try:
        listing = subprocess.run(
            ["git", "ls-tree", "--name-only", base_ref, "--", REPOSITORY_CONFIG_FILENAME],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, TRUSTED_BASE_UNAVAILABLE
    if not listing.stdout.strip():
        return None, "packaged_default"
    try:
        shown = subprocess.run(
            ["git", "show", f"{base_ref}:{REPOSITORY_CONFIG_FILENAME}"],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=10,
        )
        parsed = _YamlSubsetParser(shown.stdout).parse()
        if not isinstance(parsed, Mapping):
            raise ConfigError("top-level config must be a mapping")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, ConfigError):
        return None, TRUSTED_BASE_UNAVAILABLE
    return parsed, "trusted_base_config"


def resolve_repository_config(
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    base_ref: str = DEFAULT_BASE_REF,
) -> tuple[Mapping[str, Any] | None, str]:
    """Load the repository configuration that decides this run's posture.

    An explicitly selected configuration is authoritative: it is never replaced
    by the packaged starter, and an unreadable one is an error rather than a
    silent downgrade to a different posture. Without an explicit selection, a Git
    checkout is read at its trusted base ref rather than at the head under
    review, and only a base that verifiably configures nothing falls back to the
    maintained lane defaults; a base that could not be read at all reports
    :data:`TRUSTED_BASE_UNAVAILABLE` instead of a default posture. A directory
    that is not a Git checkout has no base ref to trust, so its own file is the
    configuration it runs under.
    """
    from . import config as code_mower_config

    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"selected repository configuration not found: {path}")
        return code_mower_config.load_config(path), "explicit_repository_config"
    if repo_root is not None:
        root = Path(repo_root).expanduser()
        if base_ref and (root / ".git").exists():
            return _trusted_base_config(root, base_ref)
        candidate = root / REPOSITORY_CONFIG_FILENAME
        if candidate.is_file():
            return code_mower_config.load_config(candidate), "repository_config"
    return None, "packaged_default"


def effective_merge_authority(
    product: str,
    *,
    config_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    base_ref: str = DEFAULT_BASE_REF,
    lane: str | None = None,
    override: bool | None = None,
) -> dict[str, Any]:
    """Resolve the posture an audit wrapper should render for this run.

    The configured decision is computed first, and an operator override is read
    against it rather than instead of it. An override can only narrow: a flag or
    environment value asking for merge authority cannot grant it to an
    informational lane, a denied role policy, an unavailable capability or an
    unqualified role, and it never skips an explicitly selected configuration
    that could not be read. An override asking for informational is always
    honoured, and whichever source decided the rendered posture is named.

    A trusted base that could not be read leaves the repository's policy unknown,
    so the posture renders informational with a bounded action rather than
    granting the packaged starter's defaults; a positive override cannot widen
    that either.
    """
    config, config_source = resolve_repository_config(
        config_path=config_path, repo_root=repo_root, base_ref=base_ref
    )
    payload = review_authority(
        product, config=config, lane=lane, config_source=config_source
    )
    if config_source == TRUSTED_BASE_UNAVAILABLE:
        # No trusted policy was read, so nothing here is evidence of merge
        # authority. The lane defaults computed above describe the packaged
        # starter, not this repository, so the rendered posture is the bounded
        # non-authoritative one and says what would make it resolvable.
        payload["configured_merge_authority"] = False
        payload["merge_authority"] = False
        payload["scope"] = "informational"
        payload["policy_source"] = "unavailable"
        payload["reason"] = TRUSTED_BASE_UNAVAILABLE
        payload["action"] = TRUSTED_BASE_UNAVAILABLE_ACTION
        payload["label"] = authority_label(payload)
    if override is None:
        return payload
    payload["operator_override"] = override
    if not override or payload["merge_authority"]:
        # Narrowing to informational, or agreeing with the computed posture: the
        # operator decided the rendered result either way.
        payload["merge_authority"] = override
        payload["policy_source"] = "operator"
        payload["config_source"] = "operator_override"
        payload["reason"] = "operator_override"
        if not override:
            payload["scope"] = "informational"
    else:
        # A positive override cannot widen what the configuration narrowed; the
        # computed reason stays the rendered one so the header is not a claim the
        # repository's policy does not support.
        payload["override_ignored"] = True
    payload["label"] = authority_label(payload)
    return payload
