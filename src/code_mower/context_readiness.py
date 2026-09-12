"""Shared, metadata-only readiness for optional context. Offline by default."""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from .context_connections import _state, authorize
from .context_contract import ContextError, _timestamp, normalize_policy
from .context_store import ContextStore

SCHEMA = 'code_mower.contextReadiness.v1'
STATES = {
    'not_configured': ('Optional context is not configured.', 'Continue the ordinary Claude/Codex workflow.'),
    'unchecked': ('Saved context has not been authorized for this operation.', 'Run doctor with --context-online, or context doctor for the selected connection with --online.'),
    'ready': ('The selected connection passed online authorization and has verified retrieval capability.', 'Start a session with --work-item, then run session context prepare; each operation reauthorizes.'),
    'identity_unverified': ('The selected connection has no verified saved identity.', 'Run code-mower context connect coworker --connection CONNECTION.'),
    'unauthorized': ('The selected connection or destination is not authorized.', 'Verify the selected connection and reconnect if its identity or permissions changed.'),
    'unavailable': ('The optional connection or its private storage is unavailable.', 'Check the private store and install code-mower[coworker] on a supported platform.'),
    'stale': ('Saved authorization has expired; offline status cannot renew it.', 'Run code-mower context doctor --connection CONNECTION --online.'),
    'incomplete': ('Identity is verified, but retrieval capability has not been established.', 'Start a session with --work-item and run session context prepare for one bounded fetch.'),
}


def summary(readiness, *, required=False, authorization='unchecked'):
    """Construct shared output from enums, never from provider/private text."""
    if readiness not in STATES or type(required) is not bool or authorization not in ('unchecked', 'verified_online', 'unavailable'):
        raise ContextError('unsupported context readiness metadata')
    message, action = STATES[readiness]
    return {'schema': SCHEMA, 'configured': readiness != 'not_configured', 'required': required,
            'readiness': readiness, 'authorization': authorization, 'message': message,
            'next_action': action, 'dependent_work': 'paused' if required and readiness != 'ready' else 'usable',
            'owner_action': required and readiness in ('identity_unverified', 'unauthorized', 'unavailable')}


def inspect_connection(policy=None, *, store=None, state_dir=None, online=False, backend=None,
                       repository=None, recipients=()):
    """Inspect only the explicit connection. Online verification never searches."""
    try:
        policy = normalize_policy(policy)
    except ContextError:
        # A malformed selected policy is never permission to drop a dependency.
        return summary('unavailable', required=True)
    if policy is None:
        return summary('not_configured')
    required = policy['required']
    try:
        store = store if store is not None else ContextStore(state_dir)
        name = policy['connection']
        with store.locked(name) as locked:
            saved = locked.read()
            if saved is None:
                return summary('identity_unverified', required=required)
            state = _state(saved, name)
        if state['state'] == 'disconnected':
            return summary('identity_unverified', required=required)
        if ((repository is not None and repository not in state['repositories'])
                or any(recipient not in state['recipients'] for recipient in recipients)):
            return summary('unauthorized', required=required)
        if online:
            if backend is None and (importlib.util.find_spec('mcp') is None or importlib.util.find_spec('keyring') is None):
                return summary('unavailable', required=required)
            try:
                authorize(store, name, backend=backend, explicit_retry=True)
            except (ContextError, OSError, ValueError):
                return summary('unauthorized', required=required, authorization='unavailable')
            with store.locked(name) as locked:
                state = _state(locked.read(), name)
                if state['state'] != 'verified':
                    return summary('unauthorized', required=required, authorization='unavailable')
            capabilities = state['capability_status']
            readiness = 'ready' if all(capabilities.get(key) == 'available' for key in ('search', 'memory')) else 'incomplete'
            return summary(readiness, required=required, authorization='verified_online')
        if state['state'] != 'verified':
            return summary('unauthorized', required=required)
        if _timestamp(state['expires_at']) <= datetime.now(timezone.utc):
            return summary('stale', required=required)
        return summary('unchecked', required=required)
    except Exception:
        return summary('unavailable', required=required)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='code-mower context doctor')
    parser.add_argument('--connection', required=True, help='Explicit private connection; never inferred from the host account')
    parser.add_argument('--required', action='store_true', help='Treat unavailable context as a dependent-work blocker')
    parser.add_argument('--online', action='store_true', help='Deliberately verify authorization online; does not search')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--repo', help='Check that this repository is approved')
    parser.add_argument('--recipient', action='append', default=[], help='Check an approved host:role')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        policy = {'schema': 'code_mower.contextPolicy.v1', 'connection': args.connection,
                  'policy_version': 'v1', 'required': args.required}
        result = inspect_connection(policy, state_dir=args.state_dir, online=args.online,
                                    repository=args.repo, recipients=args.recipient)
    except (ContextError, OSError, ValueError):
        result = summary('unavailable', required=args.required)
    print(json.dumps(result, sort_keys=True) if args.json else
          f"Context: {result['readiness']}\n{result['message']}\nNext: {result['next_action']}\n")
    return 1 if args.required and result['readiness'] != 'ready' else 0
