"""Exercise the CLI handoff, publication boundary and metadata provenance."""

import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_mower import context_command as command, context_packets, work_orders
from code_mower.context_contract import ContextError
from code_mower.context_delivery import read_binding
from code_mower.context_review import INPUT_HEADER, marker, parse
import test_context_delivery as fixtures


@unittest.skipUnless(os.name == 'posix', 'private store requires POSIX')
class ContextCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContextDeliveryTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.spec = {key: f.spec[key] for key in ('repository', 'work_item', 'policy')}
        self.attach_spec = {**self.spec, 'pr': 42, 'packet': f.result['packet_handle']}
        self.events = []
        self.comments = []

    def invoke(self, args, spec=None, *, actor='controller', fail_publication=False):
        f = self.fixture
        def github(method, path, **kwargs):
            self.events.append((method, path, kwargs.get('body')))
            if path == '/user':
                return {'login': actor}
            return {}
        def publish(repo, pr, body, **kwargs):
            self.events.append(('comment', body))
            self.comments.append({'user': {'login': actor}, 'body': body})
            if fail_publication:
                raise RuntimeError('private remote detail')
            return {'id': 123}
        with ExitStack() as stack:
            stdout = stack.enter_context(redirect_stdout(io.StringIO()))
            stderr = stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.object(command, 'ContextStore', return_value=f.store))
            stack.enter_context(mock.patch.object(context_packets, '_backend', return_value=f.backend))
            stack.enter_context(mock.patch.object(command, '_decision_authorities_for_repo', return_value=('controller',)))
            credentials = stack.enter_context(mock.patch.object(command, '_github', return_value='test-authorization'))
            stack.enter_context(mock.patch.object(command, '_gh_request', side_effect=github))
            stack.enter_context(mock.patch.object(command, 'post_pr_comment', side_effect=publish))
            stack.enter_context(mock.patch.object(command, 'fetch_pull_request', return_value={'head': {'sha': f.head}}))
            stack.enter_context(mock.patch.object(command, 'fetch_issue_comments', return_value=self.comments))
            stack.enter_context(mock.patch.object(command.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(spec).encode()))))
            result = work_orders.context_main(args)
        return result, stdout.getvalue(), stderr.getvalue(), credentials.call_count

    def test_attach_publishes_pending_before_content_free_input_and_delivers_same_packet(self):
        code, out, err, _ = self.invoke(['attach', '--connection', 'example', '--host', 'codex', '--request-stdin'], self.attach_spec)
        self.assertEqual(code, 0, err)
        metadata = json.loads(out)
        self.assertEqual(self.events[1][0], 'POST')
        self.assertEqual(self.events[1][2]['state'], 'pending')
        self.assertEqual(self.events[2][0], 'comment')
        body = self.comments[0]['body']
        self.assertTrue(body.startswith(INPUT_HEADER + '\n'))
        for value in ('one@example.invalid', 'EXAMPLE-1', self.fixture.result['packet_handle'], '"connection"', 'packet_sha256'):
            self.assertNotIn(value, body + out)
        self.assertEqual(parse(body)['revision'], metadata['revision'])
        texts = []
        for recipient in ('codex:builder', 'claude:reviewer'):
            code, text, err, _ = self.invoke(['deliver', '--revision', metadata['revision'], '--recipient', recipient])
            self.assertEqual(code, 0, err)
            texts.append(text)
        self.assertEqual(texts[0], texts[1])
        code, text, err, credentials = self.invoke(['deliver', '--packet', self.fixture.result['packet_handle'],
            '--connection', 'example', '--request-stdin', '--recipient', 'claude:builder'], self.spec)
        self.assertEqual(code, 0, err)
        self.assertEqual(credentials, 0)
        self.assertEqual(text, texts[0])
        self.assertEqual(self.fixture.backend.searches, 1)

    def test_control_authority_required_before_publication(self):
        code, out, err, _ = self.invoke(['attach', '--connection', 'example', '--host', 'claude', '--request-stdin'],
                                       self.attach_spec, actor='untrusted')
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.comments)
        self.assertIn('control authority', err)

    def test_uncertain_publication_never_enables_local_delivery(self):
        code, out, err, _ = self.invoke(['attach', '--connection', 'example', '--host', 'claude', '--request-stdin'],
                                       self.attach_spec, fail_publication=True)
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertNotIn('private remote detail', err)
        current = parse(self.comments[0]['body'])
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, current['revision'])
        code, out, _, _ = self.invoke(['deliver', '--revision', current['revision'], '--recipient', 'claude:reviewer'])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')

    def test_changed_input_and_missing_feedback_never_expose_evidence(self):
        current = self.fixture.attach()
        self.comments.append({'user': {'login': 'controller'}, 'body': INPUT_HEADER + '\n\n' + marker(current)})
        code, out, _, _ = self.invoke(['feedback', '--revision', current['revision'], '--recipient', 'codex:builder', '--reviewer', 'claude'])
        self.assertEqual((code, out), (1, ''))
        self.comments[0]['body'] = INPUT_HEADER + '\n\n' + marker({**current, 'revision': 'c' * 32})
        code, out, _, _ = self.invoke(['deliver', '--revision', current['revision'], '--recipient', 'codex:builder'])
        self.assertEqual((code, out), (1, ''))

    def test_revision_cannot_silently_ignore_a_different_explicit_connection(self):
        current = self.fixture.attach()
        self.comments.append({'user': {'login': 'controller'}, 'body': INPUT_HEADER + '\n\n' + marker(current)})
        code, out, _, credentials = self.invoke(['deliver', '--revision', current['revision'],
            '--connection', 'second', '--recipient', 'codex:builder'])
        self.assertEqual((code, out, credentials), (1, '', 0))


class ContextWorkOrderTests(unittest.TestCase):
    def test_work_order_tracks_only_opaque_packet_and_does_not_expand_cloud_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'work-order.md'
            result = work_orders.draft_work_order(title='Bounded fix', source_text='Fix one regression.',
                repo='owner/repo', role_lenses=['qa'], review_lanes=['claude-audit'], output=output,
                context_packet='a' * 32)
            self.assertEqual(result['context_packet'], 'a' * 32)
            self.assertIn('context deliver', output.read_text())
            self.assertIn('a' * 32, output.read_text())
            event = json.loads(Path(result['cloud_event_path']).read_text())
            self.assertNotIn('context_packet', json.dumps(event))
            invalid = Path(tmp) / 'invalid.md'
            with self.assertRaises(ContextError):
                work_orders.draft_work_order(title='Bounded fix', source_text='Fix one regression.',
                    repo='owner/repo', role_lenses=['qa'], review_lanes=['claude-audit'], output=invalid,
                    context_packet='../private-packet.json')
            self.assertFalse(invalid.exists())
