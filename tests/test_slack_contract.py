"""Sanitized, offline transport contracts; also runnable with unittest discovery."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from code_mower.slack_contract import (
    ACK_DEADLINE_MS, ContractError, OPERATIONS, board_event, decode, duplicate,
    normalize, validate,
)

ROOT = Path(__file__).resolve().parents[1]


class SlackContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'tests/fixtures/slack_contracts.json').read_text())
        self.request = self.fixture['requests'][0]
        self.grant = self.fixture['grant']

    def normalize(self, request=None, **kwargs):
        return normalize(request or self.request, self.grant, verified=True,
                         registered_runners=frozenset({'codex_remote', 'claude_remote'}), **kwargs)

    def test_acceptance_fixtures_and_mapping(self):
        for request in self.fixture['requests']:
            with self.subTest(operation=request['operation'], kind=request['kind']):
                intent = self.normalize(request, origin='orchestrator' if request['operation'] == 'completion' else 'slack')
                self.assertEqual(intent['schema'], 'code_mower.remote_session.v1')
                self.assertEqual(intent['operation'], OPERATIONS[request['operation']])
                self.assertNotIn('text', intent)
                self.assertEqual(decode('request', json.dumps(request).encode()), request)
        self.assertEqual(ACK_DEADLINE_MS, 3000)
        with self.assertRaises(ContractError):
            self.normalize(self.fixture['requests'][5])
        for runner in ('codex_remote', 'claude_remote'):
            self.request['identity']['runner'] = runner
            self.grant['identity']['runner'] = runner
            self.assertEqual(self.normalize()['runner'], runner)

    def test_duplicate_and_changed_input(self):
        intent = self.normalize()
        inbox = dict(schema='code_mower.slack_inbox.v1',key=intent['request'],
                     fingerprint=intent['fingerprint'],state='uncertain',expires_at=2000000000)
        self.request['retry'] = 3
        self.assertEqual(intent, self.normalize())
        self.assertTrue(duplicate(inbox, self.normalize()))
        self.request['text'] = 'Different sanitized input.'
        with self.assertRaisesRegex(ContractError, '^request_conflict$'):
            duplicate(inbox, self.normalize())
        self.request['delivery'] = 'new_delivery'
        self.assertNotEqual(intent['request'], self.normalize()['request'])

    def test_cross_tenant_and_binding_rejections(self):
        for key in self.request['identity']:
            request = copy.deepcopy(self.request)
            request['identity'][key] = 'private' if key == 'visibility' else 'other_example'
            with self.subTest(key=key), self.assertRaises(ContractError):
                self.normalize(request)
        for key, value in [('active', False), ('operations', ['status'])]:
            grant = copy.deepcopy(self.grant)
            grant[key] = value
            with self.assertRaises(ContractError):
                normalize(self.request, grant, verified=True, registered_runners={'codex_remote'})
        with self.assertRaisesRegex(ContractError, '^unverified_request$'):
            normalize(self.request, self.grant, verified=False, registered_runners={'codex_remote'})

    def test_visibility_is_explicit_and_least_privilege(self):
        self.assertEqual(self.normalize()['scope'], 'private')
        self.request['scope'] = 'public'
        self.assertEqual(self.normalize()['scope'], 'public')
        self.grant['allow_public'] = False
        with self.assertRaises(ContractError):
            self.normalize()
        self.grant['allow_public'] = True
        for visibility in ('private', 'direct'):
            self.request['identity']['visibility'] = visibility
            self.grant['identity']['visibility'] = visibility
            with self.assertRaises(ContractError):
                self.normalize()
            self.request['scope'] = 'private'
            self.assertEqual(self.normalize()['scope'], 'private')
            self.request['scope'] = 'public'

    def test_closed_fields_types_and_limits(self):
        for key, value in [('extra', 'PRIVATE_CANARY'), ('operation', 'approve'),
                           ('retry', True), ('retry', -1), ('retry', 101),
                           ('text', '\ud800'), ('text', 'x' * 16001),
                           ('kind', 'completion_response'), ('text', ' ')]:
            request = dict(self.request, **{key: value})
            with self.subTest(key=key), self.assertRaises(ContractError) as error:
                self.normalize(request)
            self.assertNotIn('PRIVATE_CANARY', str(error.exception))
        for raw in (b'{"x":1,"x":2}', b'NaN', b'[' * 2000, b'\xff', b'x' * 65537):
            with self.assertRaisesRegex(ContractError, '^invalid_contract$'):
                decode('request', raw)
        for field in self.request:
            request = dict(self.request)
            del request[field]
            with self.assertRaises(ContractError):
                self.normalize(request)
        for op in ('status', 'cancel', 'completion'):
            request = dict(self.request, operation=op)
            with self.assertRaises(ContractError):
                self.normalize(request)

    def test_metadata_only_events_and_remote_schema_parity(self):
        lifecycle = self.fixture['lifecycle']
        remote = json.loads((ROOT / 'src/code_mower/remote_session.schema.json').read_text())
        slack = json.loads((ROOT / 'src/code_mower/slack_contract.schema.json').read_text())
        self.assertEqual(slack['$defs']['lifecycle'], remote['$defs']['metadata'])
        for key in ('state', 'reason', 'next_action'):
            for value in remote['$defs']['metadata']['properties'][key]['enum']:
                record = dict(lifecycle, **{key: value})
                event = board_event('lifecycle', 'status', 'private', record)
                self.assertEqual(event[key], value)
                self.assertEqual(set(event), {'schema', 'transport', 'kind', 'operation',
                                             'scope', 'state', 'reason', 'next_action'})
        for field in ('text', 'prompt', 'source', 'diff', 'token', 'signature', 'payload',
                      'private_context', 'repository', 'result', 'modal', 'path'):
            with self.assertRaises(ContractError):
                board_event('lifecycle', 'status', 'private', dict(lifecycle, **{field:'PRIVATE_CANARY'}))
            event = board_event('lifecycle', 'status', 'private', lifecycle)
            with self.assertRaises(ContractError):
                validate('event', dict(event, **{field:'PRIVATE_CANARY'}))
        with self.assertRaises(ContractError):
            board_event('lifecycle', 'status', 'private', dict(lifecycle, state='PRIVATE_CANARY'))

    def test_outbox_retention_and_safe_errors(self):
        intent = self.normalize()
        outbox = dict(schema='code_mower.slack_outbox.v1',key=intent['request'],
                      identity=self.request['identity'],scope='private',state='pending',
                      lifecycle=self.fixture['lifecycle'],expires_at=2000000000)
        self.assertEqual(validate('outbox', outbox), outbox)
        validate('retention', self.fixture['retention'])
        for key, value in [('private_content_seconds',86401), ('dedupe_seconds',300),
                           ('outbox_seconds',86401)]:
            with self.assertRaises(ContractError):
                validate('retention', dict(self.fixture['retention'], **{key:value}))
        validate('error', dict(schema='code_mower.slack_error.v1',code='unauthorized',scope='private'))
        with self.assertRaises(ContractError):
            validate('error', dict(schema='code_mower.slack_error.v1',code='PRIVATE_CANARY',scope='private'))

    def test_no_participant_authority_or_provider_imports(self):
        from code_mower.package_manifest import PACKAGE_FILES
        from code_mower.participants import DEFAULT_PARTICIPANTS, PARTICIPANTS
        from code_mower.provider_registry import REFERENCE_PROVIDERS
        targets = {target for _, target, _ in PACKAGE_FILES}
        for target in ('src/code_mower/slack_contract.py',
                       'src/code_mower/slack_contract.schema.json', 'docs/slack-contract.md'):
            self.assertIn(target, targets)
        self.assertEqual(DEFAULT_PARTICIPANTS, ('claude', 'codex'))
        self.assertNotIn('slack', PARTICIPANTS)
        self.assertNotIn('slack', REFERENCE_PROVIDERS)
        self.request['identity']['runner'] = self.grant['identity']['runner'] = 'slack'
        with self.assertRaises(ContractError):
            normalize(self.request, self.grant, verified=True, registered_runners={'slack'})
        code = """
import sys
sys.path.insert(0, 'src')
from code_mower.slack_contract import validate
validate('error', dict(schema='code_mower.slack_error.v1', code='expired', scope='private'))
assert 'pytest' not in sys.modules
assert 'code_mower.remote_session' not in sys.modules
assert not any('devin' in name or name.startswith(('slack_sdk', 'httpx')) for name in sys.modules)
"""
        subprocess.run([sys.executable, '-I', '-c', code], cwd=ROOT, check=True)


if __name__ == '__main__':
    unittest.main()
