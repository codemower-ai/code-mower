"""Bounded authenticated Slack ingress; private receipt only, never dispatch.

Adapters must bound the read itself and preserve header multiplicity and raw bytes.
All dependencies are trusted, deadline-aware, private facilities; see docs/slack-ingress.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
import math
import re
from typing import Protocol
from urllib.parse import parse_qsl

from .slack_contract import ACK_DEADLINE_MS, MAX_BYTES, ContractError, normalize, validate

MAX_HEADERS = 32
MAX_HEADER_BYTES = 8192
MAX_FIELDS = 32
MAX_DEPTH = 10
MAX_NODES = 256
OPERATIONS = frozenset({'start', 'status', 'message', 'clarification_reply', 'cancel'})


class Clock(Protocol):
    def time(self) -> float: ...
    def monotonic(self) -> float: ...


@dataclass(frozen=True, repr=False)
class Submission:
    """Ephemeral routing input. Never log, persist, or derive a grant from these IDs."""
    kind: str
    operation: str
    app: str
    team: str
    actor: str
    channel: str
    correlation: str
    text: str
    delivery: str
    installed_team: str
    enterprise: str
    is_enterprise_install: bool
    view_team: str = ''


class Bindings(Protocol):
    def resolve(self, submission: Submission, *, deadline: float) -> dict:
        """Resolve an independent slack_contract policy; deny unknown/revoked bindings.

        Bind every routing field to the tenant/actor/conversation/session. Modal
        correlation must resolve server-held context, never private_metadata.
        Look up installations by app and installed_team (not the action team).
        Match enterprise/install scope and both teams against that installation
        and the one-time view context; deny unresolved cross-install contexts.
        Enterprise is payload context, not proof of the installed team's org
        in Slack Connect. Resolve that relationship from private server state.
        Retain the same correlation mapping for retries throughout dedupe TTL.
        Do not perform work, remote calls, or side effects here.
        """
        ...


@dataclass(frozen=True, repr=False)
class Receipt:
    """Private durable data; no raw transport payload, Slack IDs, or capabilities.

    request/intent are validated slack_contract records, or None for URL checks.
    Keep content <=24h and dedupe >=2 days; never replay on content expiry.
    """
    key: str
    fingerprint: str
    request: dict | None
    intent: dict | None
    content_expires_at: int
    dedupe_expires_at: int


class Reservation(Enum):
    ACCEPTED = 'accepted'
    DUPLICATE = 'duplicate'
    CONFLICT = 'conflict'


class ReceiptStore(Protocol):
    def reserve(self, receipt: Receipt, *, deadline: float) -> Reservation:
        """Atomically commit before returning ACCEPTED; no dispatch or handoff.

        Match key AND fingerprint for DUPLICATE, in every state (including
        uncertain and expired content); differing fingerprint is CONFLICT.
        Do not overwrite duplicates, extend retention, or acknowledge volatile
        buffering. Meet the monotonic deadline or raise; ambiguous commit fails
        closed and a retry must reconcile via the same atomic reservation.
        """
        ...


@dataclass(frozen=True, repr=False)
class Response:
    status: int
    body: bytes
    content_type: str = 'application/json; charset=utf-8'


def _response(status, value):
    return Response(status, json.dumps(value, separators=(',', ':')).encode())


def _error(status, code):
    return _response(status, validate('error', {
        'schema': 'code_mower.slack_error.v1', 'scope': 'private', 'code': code,
    }))


def _invalid():
    raise ContractError('invalid_contract')


def _closed(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _invalid()


def _string(value, limit=256, *, empty=False):
    if type(value) is not str or len(value) > limit or (not empty and not value) or '\x00' in value:
        _invalid()
    value.encode('utf-8', errors='strict')
    return value


def _scalars(value, strings=(), booleans=(), nullable=()):
    for key in strings:
        if key in value:
            _string(value[key], empty=True)
    for key in booleans:
        if key in value and type(value[key]) is not bool:
            _invalid()
    for key in nullable:
        if key in value and value[key] is not None:
            _string(value[key], empty=True)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _json(raw):
    # Bound nesting BEFORE the decoder allocates recursive containers.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                _invalid()
        elif byte in (93, 125):
            depth -= 1
    def pairs(items):
        result = {}
        if len(items) > MAX_FIELDS:
            _invalid()
        for key, value in items:
            if key in result:
                _invalid()
            result[key] = value
        return result
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_constant=lambda _: _invalid())
    count = 0
    def bound(item):
        nonlocal count
        count += 1
        if count > MAX_NODES:
            _invalid()
        if type(item) is dict:
            for key, child in item.items():
                _string(key, 128)
                bound(child)
        elif type(item) is list:
            if len(item) > MAX_FIELDS:
                _invalid()
            for child in item:
                bound(child)
        elif type(item) is str:
            _string(item, 16000, empty=True)
        elif item is not None and type(item) not in (bool, int):
            _invalid()
    bound(value)
    return value


def _form(raw):
    text = raw.decode('utf-8')
    if re.search(r'%(?![0-9a-fA-F]{2})', text):
        _invalid()
    result = {}
    for key, value in parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                                encoding='utf-8', errors='strict', max_num_fields=MAX_FIELDS):
        _string(key, 128)
        _string(value, MAX_BYTES if key == 'payload' else 16000, empty=True)
        if key in result:
            _invalid()
        result[key] = value
    return result


def _modal(value):
    _closed(value, ('type', 'team', 'user', 'api_app_id', 'view'),
            ('token', 'enterprise', 'is_enterprise_install', 'trigger_id', 'response_urls'))
    if value['type'] != 'view_submission' or value.get('is_enterprise_install', False) is not False:
        _invalid()
    enterprise = ''
    if value.get('enterprise') is not None:
        _closed(value['enterprise'], ('id',), ('name',))
        enterprise = _string(value['enterprise']['id'])
        _scalars(value['enterprise'], ('name',))
    if value.get('response_urls', []) != []:
        _invalid()
    _closed(value['team'], ('id',), ('domain',))
    _closed(value['user'], ('id',), ('name', 'username', 'team_id'))
    _scalars(value, ('token', 'trigger_id'))
    _scalars(value['team'], ('domain',))
    _scalars(value['user'], ('name', 'username', 'team_id'))
    view = value['view']
    _closed(view, ('id', 'type', 'callback_id', 'state'),
            ('hash', 'app_installed_team_id', 'team_id', 'app_id', 'bot_id', 'title', 'submit', 'close', 'blocks',
             'private_metadata', 'external_id', 'root_view_id', 'previous_view_id',
             'clear_on_close', 'notify_on_close'))
    _scalars(view, ('team_id', 'app_id', 'bot_id', 'private_metadata', 'external_id'),
             ('clear_on_close', 'notify_on_close'), ('root_view_id', 'previous_view_id'))
    team = _string(value['team']['id'])
    installed_team = _string(view.get('app_installed_team_id', team))
    view_team = _string(view['team_id']) if 'team_id' in view else ''
    if 'hash' in view:
        _string(view['hash'])
    if ((view_team and view_team not in {team, installed_team})
            or view.get('app_id', value['api_app_id']) != value['api_app_id']
            or value['user'].get('team_id', value['team']['id']) != value['team']['id']):
        _invalid()
    if view['type'] != 'modal' or view['callback_id'] not in ('start', 'clarification_reply'):
        _invalid()
    # The only supported modal is one private plain-text input. Reject hidden
    # metadata/routes, unknown blocks/actions, and richer arbitrary Block Kit.
    if view.get('private_metadata', '') != '' or view.get('external_id', '') != '':
        _invalid()
    for key in ('title', 'submit', 'close'):
        if view.get(key) is not None:
            _closed(view[key], ('type', 'text'), ('emoji',))
            _scalars(view[key], ('text',), ('emoji',))
            if view[key]['type'] != 'plain_text':
                _invalid()
    if 'blocks' in view:
        blocks = view['blocks']
        if type(blocks) is not list or len(blocks) != 1:
            _invalid()
        block = blocks[0]
        _closed(block, ('type', 'block_id', 'element', 'label'), ('optional',))
        _scalars(block, booleans=('optional',))
        if block['type'] != 'input' or block['block_id'] != 'input':
            _invalid()
        _closed(block['label'], ('type', 'text'), ('emoji',))
        _scalars(block['label'], ('text',), ('emoji',))
        if block['label']['type'] != 'plain_text':
            _invalid()
        _closed(block['element'], ('type', 'action_id'), ('multiline', 'initial_value'))
        _scalars(block['element'], booleans=('multiline',))
        if 'initial_value' in block['element']:
            _string(block['element']['initial_value'], 16000, empty=True)
        if block['element']['type'] != 'plain_text_input' or block['element']['action_id'] != 'text':
            _invalid()
    _closed(view['state'], ('values',))
    _closed(view['state']['values'], ('input',))
    _closed(view['state']['values']['input'], ('text',))
    field = view['state']['values']['input']['text']
    _closed(field, ('type', 'value'))
    if field['type'] != 'plain_text_input':
        _invalid()
    app = _string(value['api_app_id'])
    correlation = _string(view['id'])
    return Submission('modal_submission', view['callback_id'], app, team,
                      _string(value['user']['id']), '', correlation,
                      _string(field['value'], 16000),
                      _digest([app, installed_team, correlation]),
                      installed_team, enterprise, False, view_team)


def _submission(form):
    if 'payload' in form:
        _closed(form, ('payload',))
        return _modal(_json(form['payload'].encode()))
    _closed(form, ('command', 'text', 'api_app_id', 'team_id', 'user_id', 'channel_id', 'trigger_id'),
            ('token', 'team_domain', 'enterprise_id', 'enterprise_name', 'channel_name',
             'user_name', 'response_url', 'is_enterprise_install'))
    if form['command'] != '/code-mower' or form.get('is_enterprise_install', 'false') != 'false':
        _invalid()
    enterprise = _string(form.get('enterprise_id', ''), empty=True)
    _scalars(form, ('enterprise_name',))
    if form.get('enterprise_name') and not enterprise:
        _invalid()
    parts = form['text'].split(maxsplit=1)
    if not parts or parts[0] not in OPERATIONS:
        _invalid()
    operation = parts[0]
    text = parts[1] if len(parts) == 2 else ''
    app, team = _string(form['api_app_id']), _string(form['team_id'])
    return Submission('command', operation, app, team, _string(form['user_id']),
                      _string(form['channel_id']), '', text,
                      _digest([app, team, _string(form['trigger_id'])]),
                      team, enterprise, False)


class Ingress:
    """Call handle with an absolute monotonic deadline starting at HTTP arrival.

    The adapter must enforce timeouts on reads/writes and dependencies: synchronous
    Python cannot preempt a blocking store. This API rejects late results but
    cannot guarantee a misbehaving adapter returns in three seconds.
    """
    def __init__(self, *, signing_secret: bytes, clock: Clock, bindings: Bindings,
                 store: ReceiptStore, registered_runners: frozenset[str]):
        if type(signing_secret) is not bytes or not 1 <= len(signing_secret) <= 1024:
            raise ValueError('invalid_configuration')
        self._secret = signing_secret
        self._clock = clock
        self._bindings = bindings
        self._store = store
        self._runners = registered_runners

    def _remaining(self, deadline):
        now = self._clock.monotonic()
        if not math.isfinite(now) or now >= deadline:
            raise TimeoutError

    def handle(self, *, method: str, headers: tuple[tuple[str, str], ...],
               raw_body: bytes, deadline: float) -> Response:
        """Return a closed projection. No logging, dispatch, SDK, or network calls.

        Pass only the allowlisted seam headers; reject duplicates before any
        HTTP library coalesces them. Unknown transport headers are adapter-owned.
        """
        failure = 'invalid_contract'
        status = 400
        try:
            start = self._clock.monotonic()
            if type(deadline) not in (float, int) or not math.isfinite(deadline) or not math.isfinite(start) or deadline > start + ACK_DEADLINE_MS / 1000:
                _invalid()
            self._remaining(deadline)
            if method != 'POST' or type(raw_body) is not bytes or len(raw_body) > MAX_BYTES:
                _invalid()
            if type(headers) is not tuple or len(headers) > MAX_HEADERS:
                _invalid()
            parsed = {}
            size = 0
            allowed = {'content-type', 'x-slack-signature', 'x-slack-request-timestamp',
                       'x-slack-retry-num', 'x-slack-retry-reason'}
            for entry in headers:
                if type(entry) is not tuple or len(entry) != 2:
                    _invalid()
                key, value = entry
                _string(key, 128)
                _string(value, 1024)
                size += len(key.encode()) + len(value.encode())
                key = key.lower()
                if key not in allowed or key in parsed or size > MAX_HEADER_BYTES or '\r' in value or '\n' in value:
                    _invalid()
                parsed[key] = value
            _closed(parsed, ('content-type', 'x-slack-signature', 'x-slack-request-timestamp'),
                    ('x-slack-retry-num', 'x-slack-retry-reason'))
            failure, status = 'unverified_request', 401
            timestamp = parsed['x-slack-request-timestamp']
            signature = parsed['x-slack-signature']
            if not re.fullmatch(r'[1-9][0-9]{0,10}', timestamp) or not re.fullmatch(r'v0=[0-9a-f]{64}', signature):
                raise ValueError
            now = self._clock.time()
            if not math.isfinite(now) or abs(now - int(timestamp)) > 300:
                raise ValueError
            expected = 'v0=' + hmac.new(self._secret, b'v0:' + timestamp.encode('ascii') + b':' + raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            failure, status = 'invalid_contract', 400
            retry = parsed.get('x-slack-retry-num', '0')
            if not re.fullmatch(r'0|[1-9][0-9]?|100', retry):
                _invalid()
            if parsed.get('x-slack-retry-reason', 'http_timeout') not in {'http_timeout', 'too_many_redirects', 'connection_failed', 'ssl_error', 'http_error', 'unknown_error'}:
                _invalid()
            content_type = parsed['content-type'].lower()
            if content_type in ('application/json', 'application/json; charset=utf-8'):
                value = _json(raw_body)
                _closed(value, ('type', 'challenge'), ('token',))
                _scalars(value, ('token',))
                if value['type'] != 'url_verification':
                    _invalid()
                challenge = _string(value['challenge'])
                if re.fullmatch(r'[A-Za-z0-9_-]{1,256}', challenge) is None:
                    _invalid()
                key = _digest(['url_verification', challenge])
                receipt = Receipt(key, key, None, None, int(now) + 3600, int(now) + 172800)
                response = _response(200, {'challenge': challenge})
            elif content_type in ('application/x-www-form-urlencoded', 'application/x-www-form-urlencoded; charset=utf-8'):
                submission = _submission(_form(raw_body))
                self._remaining(deadline)
                failure, status = 'unauthorized', 403
                grant = validate('policy', self._bindings.resolve(submission, deadline=deadline))
                request = validate('request', dict(schema='code_mower.slack_ingress.v1',
                    kind=submission.kind, operation=submission.operation, identity=grant['identity'],
                    delivery=submission.delivery, retry=int(retry), scope='private', text=submission.text))
                intent = normalize(request, grant, verified=True, registered_runners=self._runners)
                receipt = Receipt(intent['request'], intent['fingerprint'], request, intent,
                                  int(now) + 3600, int(now) + 172800)
                response = (_response(200, {'response_type': 'ephemeral', 'text': 'Received.'})
                            if submission.kind == 'command' else Response(200, b''))
            else:
                _invalid()
            failure, status = 'delivery_uncertain', 503
            self._remaining(deadline)
            result = self._store.reserve(receipt, deadline=deadline)
            self._remaining(deadline)
            if result is Reservation.CONFLICT:
                return _error(409, 'request_conflict')
            if result is not Reservation.ACCEPTED and result is not Reservation.DUPLICATE:
                raise ValueError
            return response
        except TimeoutError:
            return _error(503, 'delivery_uncertain')
        except Exception:
            # Never propagate dependency/decoder diagnostics or exception chains.
            return _error(status, failure)
