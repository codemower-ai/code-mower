"""Owning rows G/H: genuine private state, supervisor lifetime and publication."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from code_mower import lane_delivery as delivery, lane_handoff as handoff
from code_mower.audit_labeler_lib import lineage_decision, lineage_identity
from code_mower.builder_lineage import Authorities, History, Target
from code_mower.builder_lineage_producer import Observation, ProducerStore, Snapshot
from lineage_consumer_fixtures import AUTHORS, REPO, complete_pr, git, pinned_repo, policy


class DeliveryIO:
    def __init__(self, checkout, base):
        self.checkout, self.base = checkout, base
        self.public, self.active, self.effects = [], ['builder:codex'], []
        self.drop_readback = False

    def target(self):
        return Target(REPO, 42, 'codex/topic', git(self.checkout, 'rev-parse', 'HEAD'))

    def snapshot(self, target):
        self.effects.append('snapshot')
        return Snapshot(self.target(), 'source-bot', tuple(self.active))

    def _json(self, endpoint):
        assert endpoint == 'repos/owner/repo/pulls/42'
        return complete_pr({'base': {'repo': {'full_name': REPO}, 'sha': self.base}},
            branch='codex/topic', head=self.target().head_sha, author='source-bot', labels=self.active)

    def history(self, target):
        self.effects.append('history')
        return History(self.public)

    def post(self, target, body):
        self.effects.append('post')
        if not self.drop_readback:
            self.public.append({'user': {'login': AUTHORS[0]}, 'body': body})

    def labels(self, target, desired, remove, add):
        self.effects.append('labels')
        self.active = [label for label in self.active if label not in remove]
        if add:
            self.active.append(desired)


class ProducerActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source-repo'
        self.config = policy()
        self.base = pinned_repo(self.source, self.config)
        git(self.source, 'checkout', '-qb', 'codex/topic')
        self.rounds = self.root/'round-state'
        self.store = self.root/'lineage-store'
        self.intents = self.root/'intents'
        self.io = DeliveryIO(self.source, self.base)
        self.identity = lineage_identity(self.config)

    def clone(self, name, source):
        dest = self.root/name
        subprocess.run(['git', 'clone', '-q', str(source), str(dest)], check=True)
        return dest

    def stop_source(self):
        writer = handoff.LocalWriter(self.rounds, 'source-round')
        writer.register(repo=REPO, lane='codex', checkout=self.source)
        result = delivery.supervise_process([sys.executable, '-c', 'pass'],
            log_path=self.root/'source.log', timeout_seconds=10, cwd=self.source, writer=writer)
        self.assertEqual(result.exit_code, 0)
        return {'transport': 'local_process', 'state_dir': str(self.rounds), 'writer': 'source-round'}

    def accept(self, source, origin, destination, ownership=None):
        target = self.io.target()
        accepted = delivery.validate_handoff(source_lane=origin, destination_lane=destination,
            target_pr=REPO+'#42', expected_head=target.head_sha, observed_head=target.head_sha,
            running_lane=destination, repo=REPO, target_branch=target.branch,
            source_branch_prefixes=['codex/'] if origin == 'codex' else ['claude/'],
            source_ownership=ownership)
        self.assertTrue(handoff.prepare(accepted, source, self.intents,
            head=lambda _: target.head_sha)['accepted'])
        self.assertTrue(handoff.reserve_launch(accepted, self.intents,
            head=lambda _: target.head_sha))
        path = self.root/(destination+'-handoff.json')
        path.write_text(json.dumps(accepted.as_dict()))
        return path

    def run_round(self, lane, round_id, *, handoff_file=None, create=False):
        before = self.io.target()
        before_file = self.root/(round_id+'-before.json')
        before_file.write_text(json.dumps({'snapshot_complete': True, 'kind': 'pr', 'number': '42',
            'pr_number': '42', 'pr_state': 'OPEN', 'branch': before.branch,
            'head_sha': before.head_sha, 'author': 'source-bot', 'labels': self.io.active}))
        output = self.root/(round_id+'-event.json')
        args = ['supervise', '--cwd', str(self.io.checkout), '--log', str(self.root/(round_id+'.log')),
            '--timeout-seconds', '10', '--writer', round_id, '--writer-state-dir', str(self.rounds),
            '--writer-repo', REPO, '--writer-lane', lane, '--lineage-before', str(before_file),
            '--lineage-base', self.base, '--lineage-store', str(self.store),
            '--lineage-writer', lane+'-actual-writer', '--lineage-output', str(output)]
        if handoff_file:
            args += ['--lineage-handoff', str(handoff_file), '--lineage-handoff-root', str(self.intents)]
        if create:
            args += ['--lineage-create']
        args += ['--', 'git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', round_id]
        with patch('code_mower.builder_lineage_producer.GitHub', return_value=self.io):
            code = delivery.main(args)
        return code, output

    def test_actual_entrypoint_takeover_continuation_then_third_writer(self):
        old = self.stop_source()
        accepted = self.accept(old, 'codex', 'claude')
        self.io.checkout = self.clone('claude-repo', self.source)
        code, output = self.run_round('claude', 'round-one', handoff_file=accepted, create=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.read_text())['tool']['executor'], 'claude_cli')
        self.assertEqual(self.io.active, ['builder:claude'])
        code, output = self.run_round('claude', 'round-two')
        self.assertEqual(code, 0)
        current = self.io.target()
        record = ProducerStore(self.store).read(current)
        chain, decision = lineage_decision(current, self.identity, Authorities(AUTHORS), self.io.history(current),
            author='source-bot', labels=self.io.active, private=record['episodes'])
        self.assertEqual(len(chain.episodes), 2)
        accepted = self.accept({'transport': 'local_process', 'state_dir': str(self.rounds), 'writer': 'round-two'},
            'claude', 'devin', Observation(chain, decision))
        self.io.checkout = self.clone('devin-repo', self.io.checkout)
        code, output = self.run_round('devin', 'round-three', handoff_file=accepted)
        self.assertEqual(code, 0)
        record = ProducerStore(self.store).read(self.io.target())
        self.assertEqual([row['sequence'] for row in record['episodes']], [1, 2, 3])
        self.assertEqual(self.io.active, ['builder:devin'])
        event = json.loads(output.read_text())
        self.assertEqual(event['tool']['executor'], 'devin_cli')
        self.assertEqual(event['dimensions']['lineage']['contributors'], ['claude', 'codex', 'devin'])
        self.assertEqual(self.io.effects.count('post'), 3)
        self.assertEqual(self.io.effects.count('labels'), 2)

    def test_public_readback_failure_keeps_post_but_no_labels_or_attribution(self):
        old = self.stop_source()
        accepted = self.accept(old, 'codex', 'claude')
        self.io.checkout = self.clone('destination', self.source)
        self.io.drop_readback = True
        code, output = self.run_round('claude', 'missing-readback', handoff_file=accepted, create=True)
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.io.effects.count('post'), 1)
        self.assertNotIn('labels', self.io.effects)

    def test_cumulative_32_rounds_and_overflow_refuse_before_next_launch(self):
        old = self.stop_source()
        accepted = self.accept(old, 'codex', 'claude')
        self.io.checkout = self.clone('cumulative-destination', self.source)
        for sequence in range(1, 33):
            code, output = self.run_round('claude', f'cumulative-{sequence}',
                handoff_file=accepted if sequence == 1 else None, create=sequence == 1)
            self.assertEqual(code, 0, sequence)
            self.assertTrue(output.exists())
        current = self.io.target()
        record = ProducerStore(self.store).read(current)
        chain, decision = lineage_decision(current, self.identity, Authorities(AUTHORS), self.io.history(current),
            author='source-bot', labels=self.io.active, private=record['episodes'])
        self.assertEqual(chain.raw_arrival_count, 560)
        self.assertEqual(len(chain.episodes), 32)
        self.assertEqual(decision.status, 'ready')
        before_effects = (self.io.effects.count('post'), self.io.effects.count('labels'))
        # The next authenticated arrival exceeds the budget before a destination
        # process or a later broken marker may establish any new delivery.
        self.io.public += [self.io.public[0], {'user': {'login': AUTHORS[0]},
            'body': '<!-- CODE_MOWER_BUILDER_LINEAGE: broken -->'}]
        code, output = self.run_round('claude', 'over-budget')
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.io.target(), current)
        self.assertEqual((self.io.effects.count('post'), self.io.effects.count('labels')), before_effects)

    def test_invalid_selected_ownership_never_falls_back_to_prefix(self):
        from code_mower.builder_lineage import Chain
        from lineage_consumer_fixtures import takeover
        target = Target(REPO, 42, 'codex/topic', f'{1:040x}')
        chain, decision = lineage_decision(target, self.identity, Authorities(AUTHORS), History([]),
            author='source-bot', labels=['builder:claude'], private=[takeover()])
        proof = Observation(chain, decision)
        empty_chain = Chain.from_arrivals(target, [])
        waiting_target = Target(REPO, 42, 'codex/topic', f'{2:040x}')
        waiting_chain, waiting = lineage_decision(waiting_target, self.identity, Authorities(AUTHORS), History([]),
            author='source-bot', labels=['builder:claude'], private=[takeover()])
        _, conflict = lineage_decision(target, self.identity, Authorities(AUTHORS), History([]),
            author='source-bot', labels=['builder:claude'])
        other_target = Target('other/repo', 42, 'codex/topic', target.head_sha)
        _, other = lineage_decision(other_target, self.identity, Authorities(AUTHORS), History([]),
            author='source-bot', labels=['builder:codex'])
        invalid = [dict(chain=chain, decision=decision), Observation(empty_chain, decision),
            Observation(waiting_chain, waiting), Observation(chain, conflict), Observation(chain, other)]
        for selected in invalid:
            with self.subTest(proof=type(selected).__name__), self.assertRaises(delivery.LaneDeliveryError):
                delivery.validate_handoff(source_lane='claude', destination_lane='devin',
                    target_pr=REPO+'#42', expected_head=target.head_sha, observed_head=target.head_sha,
                    running_lane='devin', repo=REPO, target_branch='codex/topic',
                    source_branch_prefixes=['codex/'], source_ownership=selected)
        accepted = delivery.validate_handoff(source_lane='claude', destination_lane='devin',
            target_pr=REPO+'#42', expected_head=target.head_sha, observed_head=target.head_sha,
            running_lane='devin', repo=REPO, target_branch='codex/topic',
            source_branch_prefixes=['claude/'], source_ownership=proof)
        self.assertEqual(accepted.source_lane, 'claude')

    def test_selected_missing_private_store_and_replayed_round_have_no_effects(self):
        before = self.io.target()
        code, output = self.run_round('codex', 'missing-private')
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        self.assertEqual(self.io.target(), before)
        self.assertNotIn('post', self.io.effects)
        old = self.stop_source()
        accepted = self.accept(old, 'codex', 'claude')
        self.io.checkout = self.clone('replay-destination', self.source)
        self.assertEqual(self.run_round('claude', 'first-round', handoff_file=accepted, create=True)[0], 0)
        before = self.io.target()
        mutations = self.io.effects.count('post'), self.io.effects.count('labels')
        code, _ = self.run_round('claude', 'first-round')
        self.assertEqual(code, 2)
        self.assertEqual(self.io.target(), before)
        self.assertEqual((self.io.effects.count('post'), self.io.effects.count('labels')), mutations)
