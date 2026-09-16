"""One approved evidence payload for every host and independent recipient."""

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from code_mower import context_graph_connection as graph_connection
from code_mower import context_graph_lifecycle as lifecycle
from code_mower.context_connections import connect, disconnect
from code_mower.context_contract import ContextError, ContextRequest, load_packet
from code_mower.context_delivery import (SUPPORTED_HOSTS, SUPPORTED_RECIPIENTS, abandon_attachment, attach,
                                         deliver, public_verdict, read_binding, render_evidence,
                                         retire_attachment, save_feedback)
from code_mower.context_packets import _index, fetch
from code_mower.context_store import ContextStore
from test_context_connections import MemoryVault
from test_context_packets import RetrievalBackend
from test_coworker_retrieval import POLICY
import test_context_graph_query as graph_fixtures


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

    def test_organization_replay_succeeds_without_a_consuming_revision(self):
        """Organization evidence has no code revision (codex:b7f5dbb1412eb89a3797)."""
        current = self.attach()
        self.assertTrue(self.delivery(current, consuming_revision=None).text)

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
        self.assertIn('## Devin audit (informational only)', body)
        forced = public_verdict(devin, provider='Devin', head=self.head, verdict='PASS', counts=[0, 0, 0, 0],
                                trailer='<!-- DEVIN_AUDIT_STATE: devin-audit-pass -->', merge_authority=True)
        self.assertIn('## Devin audit (informational only)', forced)
        self.assertIn('## Claude audit (merge-authority lane)', public_verdict(
            delivery, provider='Claude', head=self.head, verdict='PASS', counts=[0, 0, 0, 0],
            trailer='<!-- CLAUDE_AUDIT_STATE: claude-audit-pass -->'))
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

    def test_retirement_completes_after_an_interrupted_index_write(self):
        """Delivery removal writes the index without the revision before
        deleting the artifact; a crash between those writes must resolve on
        retry rather than report an inconsistent index (codex:1a4bc7b34687da726908)."""
        current = self.attach()
        revision = current['revision']
        handle = self.result['packet_handle']
        with self.store.locked('example') as locked:
            index_file, index = _index(locked)
            entry = next(item for item in index['entries'] if item['handle'] == handle)
            entry['deliveries'].remove(revision)
            index_file.write(index)
        # The artifact is still present; only the index write completed.
        retire_attachment(self.store, 'example', handle, revision)
        with self.assertRaises(ContextError):
            read_binding(self.store, revision)
        # Retrying the exact same removal is idempotent.
        retire_attachment(self.store, 'example', handle, revision)

    def test_abandon_still_refuses_a_published_binding(self):
        current = self.attach()
        with self.assertRaises(ContextError):
            abandon_attachment(self.store, 'example', self.result['packet_handle'], current['revision'])
        self.assertTrue(read_binding(self.store, current['revision'])['published'])

    def test_removal_refuses_a_binding_whose_identity_does_not_match(self):
        """The targeted binding is identity checked; a mismatched handle or
        revision inside the stored binding must still fail closed even
        though the artifact exists (codex:1a4bc7b34687da726908)."""
        current = self.attach()
        revision = current['revision']
        handle = self.result['packet_handle']
        with self.store.locked('example') as locked:
            artifact = locked.artifact('d-' + revision)
            corrupted = {**artifact.read(), 'handle': 'f' * 32}
            artifact.write(corrupted)
        with self.assertRaises(ContextError):
            retire_attachment(self.store, 'example', handle, revision)
        with self.assertRaises(ContextError):
            abandon_attachment(self.store, 'example', handle, revision)

    def test_removal_refuses_when_the_index_entry_is_missing(self):
        """A missing index entry is not the same state an interrupted
        cleanup leaves -- that only ever drops the revision from an
        existing entry's deliveries -- so it still fails closed rather than
        completing as if it were (codex:1a4bc7b34687da726908)."""
        current = self.attach()
        revision = current['revision']
        handle = self.result['packet_handle']
        with self.store.locked('example') as locked:
            index_file, index = _index(locked)
            index['entries'] = [item for item in index['entries'] if item['handle'] != handle]
            index_file.write(index)
        with self.assertRaises(ContextError):
            retire_attachment(self.store, 'example', handle, revision)


@unittest.skipUnless(os.name == 'posix', 'private store requires POSIX')
class GraphDeliveryBindingTests(unittest.TestCase):
    """A repository replay must bind to its explicit consuming revision, never the
    attachment/PR head it happens to have been published for (codex:b7f5dbb1412eb89a3797)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.repository = graph_fixtures.make_repository(root)
        private = root / 'private'
        private.mkdir(mode=0o700)
        self.manifest = lifecycle.build_graph(
            self.repository, pin=graph_fixtures.PIN,
            indexer=graph_fixtures.indexer(graph_fixtures.graph_document()), root=private,
        )
        self.store = ContextStore(private, vault=MemoryVault())
        self.recipients = [f'{host}:{role}' for host in ('claude', 'codex', 'devin')
                           for role in ('orchestrator', 'builder', 'reviewer')]
        graph_connection.connect(self.store, 'local-graph', {
            'repository_root': str(self.repository), 'repositories': ['owner/repo'],
            'recipients': self.recipients,
        })
        self.head = self.manifest.commit  # checkout A
        self.other = 'b' * 40  # a different checkout, B
        self.policy = {'schema': 'code_mower.contextPolicy.v1', 'connection': 'local-graph',
                       'policy_version': 'v1', 'required': True}
        spec = {'repository': 'owner/repo', 'work_item': 'WORK-1', 'recipient': 'codex:orchestrator',
                'query': 'parse_config', 'source': 'impact', 'policy': self.policy}
        result = fetch(self.store, 'local-graph', spec, revision=self.head)
        self.current = attach(self.store, 'local-graph', result['packet_handle'], self.policy,
            ContextRequest('owner/repo', 'WORK-1', 'codex:orchestrator'), pr=42, head=self.head,
            publish=lambda metadata: None, consuming_revision=self.head)

    def delivery(self, **kwargs):
        return deliver(self.store, self.current['revision'], repository='owner/repo', pr=42, head=self.head,
                       recipient='codex:builder', current=self.current, **kwargs)

    def test_replay_matches_the_actual_consuming_revision(self):
        self.assertIn('Private evidence', self.delivery(consuming_revision=self.head).text)

    def test_replay_refuses_a_different_consuming_revision(self):
        """Checkout B must not receive evidence authorized for checkout A."""
        with self.assertRaises(ContextError):
            self.delivery(consuming_revision=self.other)

    def test_replay_never_silently_substitutes_the_attachment_head(self):
        """A caller that cannot name its consuming revision (e.g. non-Git) fails closed."""
        with self.assertRaises(ContextError):
            self.delivery()
        with self.assertRaises(ContextError):
            self.delivery(consuming_revision=None)


@unittest.skipUnless(os.name == 'posix', 'private store requires POSIX')
class GraphAttachmentBindingTests(unittest.TestCase):
    """A fresh attachment must bind repository evidence to the actual consuming
    checkout, never merely to the PR head a caller happens to report
    (codex:65a17212478a56416b1c)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.repository = graph_fixtures.make_repository(root)
        private = root / 'private'
        private.mkdir(mode=0o700)
        self.manifest = lifecycle.build_graph(
            self.repository, pin=graph_fixtures.PIN,
            indexer=graph_fixtures.indexer(graph_fixtures.graph_document()), root=private,
        )
        self.store = ContextStore(private, vault=MemoryVault())
        self.recipients = [f'{host}:{role}' for host in ('claude', 'codex', 'devin')
                           for role in ('orchestrator', 'builder', 'reviewer')]
        graph_connection.connect(self.store, 'local-graph', {
            'repository_root': str(self.repository), 'repositories': ['owner/repo'],
            'recipients': self.recipients,
        })
        self.head = self.manifest.commit  # checkout A
        self.other = 'b' * 40  # a different checkout, B
        self.policy = {'schema': 'code_mower.contextPolicy.v1', 'connection': 'local-graph',
                       'policy_version': 'v1', 'required': True}
        spec = {'repository': 'owner/repo', 'work_item': 'WORK-1', 'recipient': 'codex:orchestrator',
                'query': 'parse_config', 'source': 'impact', 'policy': self.policy}
        self.handle = fetch(self.store, 'local-graph', spec, revision=self.head)['packet_handle']
        self.published = []

    def attach(self, *, head, **kwargs):
        return attach(self.store, 'local-graph', self.handle, self.policy,
            ContextRequest('owner/repo', 'WORK-1', 'codex:orchestrator'), pr=42, head=head,
            publish=self.published.append, **kwargs)

    def test_attachment_succeeds_when_packet_pr_and_consumer_all_match(self):
        current = self.attach(head=self.head, consuming_revision=self.head)
        self.assertEqual(current['head'], self.head)
        self.assertEqual(len(self.published), 1)

    def test_attachment_refuses_a_consumer_the_checkout_is_not_actually_on(self):
        """Checkout B must not enable a binding authorized for checkout A."""
        with self.assertRaises(ContextError):
            self.attach(head=self.head, consuming_revision=self.other)
        self.assertEqual(self.published, [])

    def test_attachment_refuses_a_pull_request_head_that_differs_from_the_packet_even_when_the_consumer_matches(self):
        """Packet A versus PR B is refused even though the consumer is A."""
        with self.assertRaises(ContextError):
            self.attach(head=self.other, consuming_revision=self.head)
        self.assertEqual(self.published, [])

    def test_attachment_never_silently_substitutes_the_pr_head_for_an_unknown_consumer(self):
        """A caller that cannot name its consuming revision (e.g. non-Git) fails closed."""
        with self.assertRaises(ContextError):
            self.attach(head=self.head)
        with self.assertRaises(ContextError):
            self.attach(head=self.head, consuming_revision=None)
        self.assertEqual(self.published, [])
