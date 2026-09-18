"""Trusted acquisition adapter for the common consumer decision.

Policy is loaded through the existing immutable-base repository boundary.
Public-only is the deliberate default for remote consumers. A selected private
store is mandatory evidence and its read errors are never treated as absence.
"""
from __future__ import annotations

from pathlib import Path
import re

from ..audit_labeler_lib import lineage_admission, lineage_decision, lineage_snapshot, lineage_identity, lineage_authorities
from ..builder_lineage import Authorities, ContractError, History
from .. import config as repository_config
from ..decisions import decision_authorities_from_env
from ..review_authority import resolve_repository_config



def trusted_policy(checkout, base_sha, *, extra_authorities=()):
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise ContractError("Immutable accepted policy revision required")
    if not (Path(checkout) / ".git").exists():
        raise ContractError("Trusted Git policy checkout required")
    config, origin = resolve_repository_config(repo_root=Path(checkout), base_ref=base_sha)
    if origin == "packaged_default":
        config = {}
    elif origin != "trusted_base_config" or config is None:
        raise ContractError("Trusted immutable-base policy unavailable")
    elif repository_config.validate_config(config):
        raise ContractError("Trusted repository policy is invalid")
    identity = lineage_identity(config)
    authority = lineage_authorities(config, extra=(*decision_authorities_from_env(), *extra_authorities))
    return config, identity, authority


def acquire(repo, number, payload, *, checkout, base_sha, fetch_comments,
            reviewer=None, reviewer_accounts=(), extra_authorities=(), private_store=None):
    _, identity, authorities = trusted_policy(checkout, base_sha,
                                              extra_authorities=extra_authorities)
    if reviewer is not None:
        identity = identity.with_reviewer_floor(reviewer, reviewer_accounts)
    target, author, labels = lineage_snapshot(repo, number, payload)
    history = History(fetch_comments())
    private = () if private_store is None else private_store.read(target)["episodes"]
    bound, decision = lineage_decision(target, identity, authorities, history,
                                     author=author, labels=labels, private=private)
    if reviewer is not None:
        lineage_admission(decision, reviewer)
    return bound, decision


def require_capabilities():
    """Explicit installed boundary; #915 qualified published 1.4.1 activation."""
    try:
        from .. import builder_lineage as core, builder_lineage_producer as producer
        from .. import lane_delivery, lane_handoff, builder_runs
        for module, names in ((core, ('Target', 'History', 'Chain', 'resolve', 'admit')),
            (producer, ('Observation', 'observe', 'publish', 'ProducerStore', 'Transport', 'staged_record')),
            (lane_delivery, ('LineageRound', 'LineageCreationRound', 'lineage_continuation',
                             'lineage_creation', 'lineage_target_state', 'lineage_writer_id',
                             'reserve_creation_branch')),
            (lane_handoff, ('lineage_handoff',)), (builder_runs, ('record_lineage_builder',))):
            if any(not callable(getattr(module, name, None)) for name in names):
                raise ImportError
    except (ImportError, AttributeError):
        raise ContractError('Unsupported installed lineage capability; release activation requires #915') from None


def remote_policy(io, target, base_sha):
    """Read policy at the immutable base selected from authenticated PR metadata."""
    import base64
    from ..config import _YamlSubsetParser
    raw = io._json(f'repos/{target.repo}/pulls/{target.pr_number}')
    observed, _, _ = lineage_snapshot(target.repo, target.pr_number, raw)
    if observed != target or raw.get('base', {}).get('sha') != base_sha:
        raise ContractError('Selected immutable base or target differs')
    # The authenticated tree distinguishes a proved absent config from failed retrieval.
    tree = io._json(f'repos/{target.repo}/git/trees/{base_sha}')
    if not isinstance(tree, dict) or tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
        raise ContractError('Trusted policy tree unreadable')
    matches = [item for item in tree['tree'] if isinstance(item, dict) and item.get('path') == 'code-mower.yml']
    if not matches:
        return {}, lineage_identity({}), Authorities(())
    if len(matches) != 1 or matches[0].get('type') != 'blob':
        raise ContractError('Trusted policy is not a regular blob')
    blob = io._json(f"repos/{target.repo}/git/blobs/{matches[0]['sha']}")
    if blob.get('encoding') != 'base64':
        raise ContractError('Trusted policy encoding unavailable')
    config = _YamlSubsetParser(base64.b64decode(blob['content']).decode('utf-8')).parse()
    if repository_config.validate_config(config):
        raise ContractError('Trusted repository policy is invalid')
    return config, lineage_identity(config), lineage_authorities(config)


def installed_record(environ, *, io=None, clock=None):
    """Validate caller claims against authenticated immutable policy before attribution."""
    require_capabilities()
    from ..builder_lineage import Target
    from ..builder_lineage_producer import GitHub, decode_transport, staged_record
    io = io if io is not None else GitHub()
    target = Target.from_mapping(decode_transport(environ['LINEAGE_TARGET_JSON']))
    supplied = decode_transport(environ['LINEAGE_POLICY_JSON'])
    config, identity, authorities = remote_policy(io, target, supplied['base_sha'])
    if (supplied != {'base_sha': supplied['base_sha'], 'identity': identity.to_mapping(), 'roles': config}
            or Authorities(decode_transport(environ['LINEAGE_AUTHORITY_JSON'])) != authorities):
        raise ContractError('Caller policy or authority differs from immutable accepted base')
    return staged_record(environ, io=io, clock=clock)
