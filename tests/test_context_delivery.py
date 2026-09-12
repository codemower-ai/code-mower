"""One approved evidence payload for every host and independent recipient."""

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from code_mower.context_connections import connect, disconnect
from code_mower.context_contract import ContextError, ContextRequest, load_packet
from code_mower.context_delivery import (SUPPORTED_HOSTS, SUPPORTED_RECIPIENTS, attach, deliver, public_verdict,
                                         read_binding, render_evidence, save_feedback)
from code_mower.context_packets import fetch
from code_mower.context_store import ContextStore
from test_context_connections import MemoryVault
from test_context_packets import RetrievalBackend
from test_coworker_retrieval import POLICY


@unittest.skipUnless(os.name == 'posix', 'private store requires POSIX')
class ContextDeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = ContextStore(Path(tmp.name).resolve() / 'private', vault=MemoryVault())
        self.backend = RetrievalBackend()
        self.head = 'a' * 40
        self.recipients = [f'{host}:{role}' for host in ('claude', 'codex', 'devin')
                           for role in ('orchestrator', 'builder', 'reviewer')]
        connect(self.store, 'example', {'principal': 'one@example.invalid', 'workspace': 'example',
                'repositories': ['owner/repo'], 'recipients': self.recipients}, backend=self.backend)
        self.spec = {'repository': 'owner/repo', 'work_item': 'EXAMPLE-1', 'recipient': 'codex:orchestrator',
                     'query': 'bug triage', 'source': 'jira', 'policy': POLICY}
        self.result = fetch(self.store, 'example', self.spec, backend=self.backend)
        self.published = []

    def attach(self, host='codex', publish=None):
        return attach(self.store, 'example', self.result['packet_handle'], POLICY,
            ContextRequest('owner/repo', 'EXAMPLE-1', host + ':orchestrator'), pr=42, head=self.head,
            publish=publish or self.published.append, backend=self.backend)

    def delivery(self, current, recipient='claude:reviewer', **kwargs):
        return deliver(self.store, current['revision'], repository='owner/repo', pr=42, head=self.head,
                       recipient=recipient, current=current, backend=self.backend, **kwargs)

    def test_devin_roles_are_supported_recipients(self):
        self.assertEqual(SUPPORTED_HOSTS, ('claude', 'codex', 'devin'))
        self.assertEqual(SUPPORTED_RECIPIENTS, frozenset(self.recipients))

    def test_every_host_delivers_identical_evidence_to_builder_and_reviewer(self):
        for host in SUPPORTED_HOSTS:
            current = self.attach(host)
            received = [self.delivery(current, recipient) for recipient in self.recipients]
            self.assertEqual(len({item.text for item in received}), 1)
            for private in ('one@example.invalid', '"identity"', '"connection"', '"generation"', 'credential'):
                self.assertNotIn(private, received[0].text)
            self.assertEqual(received[0].metadata, current)
        self.assertEqual(self.backend.searches, 1)

    def test_publication_failure_does_not_deliver_unpublished_evidence(self):
        def fail(metadata):
            self.published.append(metadata)
            raise RuntimeError('failed remote write')
        for _ in range(9):
            with self.assertRaises(RuntimeError):
                self.attach(publish=fail)
            with self.assertRaises(ContextError):
                self.delivery(self.published[-1])
        current = self.attach()
        self.assertTrue(self.delivery(current).text)

    def test_changed_input_recipient_revocation_and_disconnected_cache_are_rejected(self):
        current = self.attach()
        for invalid in ({**current, 'revision': 'c' * 32}, {**current, 'head': 'd' * 40}):
            with self.assertRaises(ContextError):
                self.delivery(invalid)
        with self.assertRaises(ContextError):
            self.delivery(current, 'unsupported:reviewer')
        self.backend.wrong_identity = True
        with self.assertRaises(ContextError):
            self.delivery(current, 'devin:reviewer')
        self.backend.wrong_identity = False
        self.backend.revoked = True
        with self.assertRaises(ContextError):
            self.delivery(current)
        disconnect(self.store, 'example', backend=self.backend)
        self.assertEqual(list(self.store.root.glob('.d-*.json')), [])
        with self.assertRaises(ContextError):
            self.delivery(current)

    def test_connection_without_devin_recipients_never_delivers_to_devin(self):
        disconnect(self.store, 'example', backend=self.backend)
        narrowed = [recipient for recipient in self.recipients if not recipient.startswith('devin:')]
        connect(self.store, 'example', {'principal': 'one@example.invalid', 'workspace': 'example',
                'repositories': ['owner/repo'], 'recipients': narrowed}, backend=self.backend)
        self.result = fetch(self.store, 'example', self.spec, backend=self.backend)
        with self.assertRaises(ContextError):
            self.attach('devin')
        current = self.attach()
        for role in ('orchestrator', 'builder', 'reviewer'):
            with self.assertRaises(ContextError):
                self.delivery(current, f'devin:{role}')
        self.assertTrue(self.delivery(current, 'codex:builder').text)

    def test_retrieval_refresh_deletes_old_binding_and_feedback(self):
        current = self.attach()
        delivery = self.delivery(current)
        save_feedback(self.store, delivery, 'claude', 'Private quoted evidence belongs here.')
        self.assertIn('Private quoted evidence', read_binding(self.store, current['revision'])['feedback']['claude'])
        fetch(self.store, 'example', self.spec, backend=self.backend, refresh=True)
        with self.assertRaises(ContextError):
            self.delivery(current)
        self.assertEqual(list(self.store.root.glob('.d-*.json')), [])

    def test_public_verdict_has_only_metadata_and_never_model_authored_findings(self):
        current = self.attach()
        delivery = self.delivery(current)
        prose = 'Private citation and source excerpt.'
        save_feedback(self.store, delivery, 'claude', prose)
        body = public_verdict(delivery, provider='Claude', head=self.head, verdict='BLOCKED',
                             counts=[0, 1, 0, 0], trailer='<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->')
        self.assertNotIn(prose, body)
        self.assertNotIn('one@example.invalid', body)
        self.assertIn(current['revision'], body)
        self.assertIn('Claude Audit: BLOCKED', body)
        devin = self.delivery(current, 'devin:reviewer')
        save_feedback(self.store, devin, 'devin', prose)
        body = public_verdict(devin, provider='Devin', head=self.head, verdict='PASS',
                             counts=[0, 0, 0, 0], trailer='<!-- DEVIN_AUDIT_STATE: devin-audit-pass -->')
        self.assertNotIn(prose, body)
        self.assertIn('Devin Audit: PASS', body)
        with self.assertRaises(ContextError):
            save_feedback(self.store, devin, 'unsupported', prose)

    def test_local_repository_graph_uses_common_renderer_without_oauth_identity(self):
        from test_context_contract import NOW, connection, packet, policy
        auth = connection('repository')
        auth['recipients'] = self.recipients
        payload = packet(auth)
        path = self.store.root / 'local-graph.json'
        raw = json.dumps(payload).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        texts = []
        for recipient in self.recipients:
            validated = load_packet(private_root=self.store.root,
                reference={'path': path.name, 'sha256': hashlib.sha256(raw).hexdigest()}, policy=policy(),
                request=ContextRequest('owner/repo', 'work-item-one', recipient, 'revision-one'),
                authorize=lambda: auth, now=NOW)
            self.assertEqual(validated.revision_state, 'matching')
            texts.append(render_evidence(validated, 'b' * 32))
        self.assertEqual(len(set(texts)), 1)
        self.assertIn('synthetic-graph', texts[0])
        self.assertIn('Synthetic evidence: parser calls validator.', texts[0])
        self.assertNotIn('/example/repository', texts[0])
        self.assertNotIn('principal', texts[0])
