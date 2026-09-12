"""Offline Slack transport contracts. No authentication, storage, or dispatch occurs here.

Trusted boundary code supplies verification and an independently resolved grant.
Never serialize a request, grant, intent, inbox, or outbox to Board/cloud.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

ACK_DEADLINE_MS = 3000
MAX_BYTES = 65536
OPERATIONS = {
    'start': 'dispatch', 'status': 'status', 'message': 'message',
    'clarification_reply': 'message', 'cancel': 'cancel', 'completion': 'collect',
}


class ContractError(ValueError):
    """Fixed diagnostics, never echo input or underlying exceptions."""


@lru_cache(maxsize=1)
def _schema():
    return json.loads(Path(__file__).with_name('slack_contract.schema.json').read_text())


def _check(value, rule, depth=0):
    if depth > 12:
        raise ContractError('invalid_contract')
    if '$ref' in rule:
        return _check(value, _schema()['$defs'][rule['$ref'].rsplit('/', 1)[1]], depth + 1)
    if 'const' in rule and (type(value) is not type(rule['const']) or value != rule['const']):
        raise ContractError('invalid_contract')
    if 'enum' in rule and (type(value) is not str or value not in rule['enum']):
        raise ContractError('invalid_contract')
    kind = rule.get('type')
    expected = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if kind and type(value) is not expected[kind]:
        raise ContractError('invalid_contract')
    if kind == 'object':
        if len(value) != len(rule['required']) or set(value) != set(rule['required']):
            raise ContractError('invalid_contract')
        for key, child in value.items():
            _check(child, rule['properties'][key], depth + 1)
    elif kind == 'array':
        if not rule['minItems'] <= len(value) <= rule['maxItems']:
            raise ContractError('invalid_contract')
        for child in value:
            _check(child, rule['items'], depth + 1)
        if rule.get('uniqueItems') and len(set(value)) != len(value):
            raise ContractError('invalid_contract')
    elif kind == 'string':
        if len(value) > rule.get('maxLength', MAX_BYTES) or (
            'pattern' in rule and re.fullmatch(rule['pattern'], value) is None
        ):
            raise ContractError('invalid_contract')
        try:
            if len(value.encode('utf-8')) > MAX_BYTES:
                raise ContractError('invalid_contract')
        except UnicodeError:
            raise ContractError('invalid_contract') from None
    elif kind == 'integer' and not rule['minimum'] <= value <= rule['maximum']:
        raise ContractError('invalid_contract')


def validate(kind: str, value: object) -> dict:
    """Validate the packaged schema's closed subset and cross-field v1 invariants.

    Accepts already decoded objects; use decode() for bounded, duplicate-safe JSON.
    Returns a detached copy. This is not a general JSON Schema implementation.
    """
    if type(kind) is not str or kind not in _schema()['$defs']:
        raise ContractError('invalid_contract')
    _check(value, _schema()['$defs'][kind])
    if kind == 'request':
        operation = value['operation']
        if ((value['kind'] == 'completion_response') != (operation == 'completion')
                or (value['kind'] == 'modal_submission' and operation not in {'start', 'clarification_reply'})
                or (operation in {'start', 'message', 'clarification_reply'} and not value['text'].strip())
                or (operation in {'status', 'cancel', 'completion'} and value['text'] != '')):
            raise ContractError('invalid_contract')
    if kind in {'request', 'outbox'} and value['scope'] == 'public' and value['identity']['visibility'] != 'public':
        raise ContractError('unauthorized')
    return json.loads(json.dumps(value, ensure_ascii=True))


def decode(kind: str, raw: bytes) -> dict:
    """Bound payloads before parsing; reject duplicate JSON keys and nonfinite values."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError('invalid_contract')
            result[key] = value
        return result
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ContractError('invalid_contract')
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return validate(kind, value)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ContractError('invalid_contract') from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalize(request: dict, grant: dict, *, verified: bool,
              registered_runners: frozenset[str], origin: str = 'slack') -> dict:
    """Return a private intent, not authority to execute it or an executable SDK call.

    `verified` must come from #917, never from a request field. Grant and runner
    registration must come from trusted orchestrator policy, not Slack input.
    """
    if verified is not True:
        raise ContractError('unverified_request')
    request, grant = validate('request', request), validate('policy', grant)
    if (origin not in ('slack', 'orchestrator')
            or (request['operation'] == 'completion') != (origin == 'orchestrator')):
        raise ContractError('unauthorized')
    identity = request['identity']
    if (identity != grant['identity'] or request['operation'] not in grant['operations']
            or identity['runner'] == 'slack' or identity['runner'] not in registered_runners
            or (request['scope'] == 'public' and not grant['allow_public'])):
        raise ContractError('unauthorized')
    # Retry attempt is deliberately excluded; changed content is a conflict, not new work.
    key = _digest([identity['team'], identity['installation'], request['delivery']])
    fingerprint = _digest({k: v for k, v in request.items() if k != 'retry'})
    return validate('intent', dict(schema='code_mower.remote_session.v1', operation=OPERATIONS[request['operation']],
                session=identity['session'], runner=identity['runner'], request=key,
                fingerprint=fingerprint, scope=request['scope']))


def duplicate(inbox: dict, intent: dict) -> bool:
    """Compare a prior private reservation; never authorize replay of uncertain work."""
    inbox = validate('inbox', inbox)
    intent = validate('intent', intent)
    if inbox['key'] != intent['request'] or inbox['fingerprint'] != intent['fingerprint']:
        raise ContractError('request_conflict')
    return True


def board_event(kind: str, operation: str, scope: str, lifecycle: dict) -> dict:
    """Only closed lifecycle metadata enters this projection, never private records."""
    lifecycle = validate('lifecycle', lifecycle)
    return validate('event', dict(schema='code_mower.slack_event.v1', transport='slack',
                    kind=kind, operation=operation, scope=scope,
                    **{k: lifecycle[k] for k in ('state', 'reason', 'next_action')}))
