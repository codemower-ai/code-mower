"""Owning row A: exact trusted wrapper admission before provider execution."""
from pathlib import Path
import tempfile
import unittest

from lineage_consumer_fixtures import complete_pr, marker_history, policy, wrapper_boundary


class AdmissionConsumers(unittest.TestCase):
    def test_actual_wrappers_share_conflict_empty_takeover_and_stale_decisions(self):
        cases = [
            ('matched', policy(), complete_pr(branch='codex/topic', labels=['builder:codex']), [], {'claude', 'devin'}),
            ('conflict', policy(), complete_pr(branch='codex/topic', labels=['builder:claude']), [], set()),
            ('custom', policy(), complete_pr(branch='feature/cx-topic', labels=['builder:codex']), [], {'claude', 'devin'}),
            ('no-contract', policy({}), complete_pr(branch='codex/topic', labels=['builder:claude']), [], {'codex', 'devin'}),
            ('takeover', policy(), complete_pr(branch='codex/topic', head=f'{1:040x}', labels=['builder:claude']), marker_history(), {'devin'}),
            ('continuation', policy(), complete_pr(branch='codex/topic', head=f'{2:040x}', labels=['builder:claude']), marker_history(2), {'devin'}),
            ('stale', policy(), complete_pr(branch='codex/topic', labels=['builder:claude']), marker_history(), set()),
        ]
        for name, config, pr, history, allowed in cases:
            pr['head']['repo'] = {'full_name': 'owner/repo'}
            for lane in ('codex', 'claude', 'devin'):
                with self.subTest(case=name, lane=lane), tempfile.TemporaryDirectory() as tmp:
                    self.assertEqual(wrapper_boundary(Path(tmp)/'repo', lane, config, pr, history), lane in allowed)

    def test_raw_history_and_missing_target_refuse_before_any_provider(self):
        for history in (None, {}, [None], [{'body': None}]):
            for lane in ('codex', 'claude', 'devin'):
                with self.subTest(history=history, lane=lane), tempfile.TemporaryDirectory() as tmp:
                    pr = complete_pr(branch='human/fix')
                    pr['head']['repo'] = {'full_name': 'owner/repo'}
                    self.assertFalse(wrapper_boundary(Path(tmp)/'repo', lane, policy(), pr, history))

    def test_wrapper_cumulative_history_budget_and_strict_announced_markers(self):
        from code_mower.builder_lineage import Chain, Target, render
        from lineage_consumer_fixtures import cumulative_history, AUTHORS, REPO
        public, private = cumulative_history()
        target = Target(REPO, 42, 'codex/topic', f'{32:040x}')
        tail = {'user': {'login': AUTHORS[0]}, 'body': render(Chain.from_arrivals(target, private))}
        for history, allowed in ((public+[tail], True), (public+[tail, public[0]], False)):
            with self.subTest(size=len(history)), tempfile.TemporaryDirectory() as tmp:
                pr = complete_pr(branch=target.branch, head=target.head_sha, labels=['builder:claude'])
                pr['head']['repo'] = {'full_name': REPO}
                self.assertEqual(wrapper_boundary(Path(tmp)/'repo', 'devin', policy(), pr, history), allowed)
        for body in ('<!-- CODE_MOWER_BUILDER_LINEAGE -->', '<!-- CODE_MOWER_BUILDER_LINEAGE: -->',
                     '<!-- CODE_MOWER_BUILDER_LINEAGE: {"episodes":[],"episodes":[]} -->'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                pr = complete_pr(branch='codex/topic', labels=['builder:codex'])
                pr['head']['repo'] = {'full_name': REPO}
                self.assertFalse(wrapper_boundary(Path(tmp)/'repo', 'claude', policy(), pr,
                    [{'user': {'login': AUTHORS[0]}, 'body': body}]))

    def test_true_empty_no_authority_and_unknown_marker_author_controls(self):
        from lineage_consumer_fixtures import AUTHORS
        for prefixes in ({}, {'codex/': 'codex'}):
            cfg = policy(prefixes)
            cfg['decisions']['authorities'] = []
            for history in ([], [{'user': {'login': AUTHORS[0]}, 'body': '<!-- CODE_MOWER_BUILDER_LINEAGE: broken -->'}],
                            [{'user': None}, {}]):
                with self.subTest(prefixes=prefixes, history=history), tempfile.TemporaryDirectory() as tmp:
                    pr = complete_pr(branch='codex/topic', labels=['builder:codex'])
                    pr['head']['repo'] = {'full_name': 'owner/repo'}
                    self.assertTrue(wrapper_boundary(Path(tmp)/'repo', 'claude', cfg, pr, history))

    def test_all_wrappers_reject_malformed_markers_and_wrong_exact_target(self):
        from lineage_consumer_fixtures import malformed_marker_histories
        for lane in ('codex', 'claude', 'devin'):
            for history in malformed_marker_histories():
                with self.subTest(lane=lane, history=history), tempfile.TemporaryDirectory() as tmp:
                    pr = complete_pr(branch='codex/topic', labels=['builder:codex'])
                    pr['head']['repo'] = {'full_name': 'owner/repo'}
                    self.assertFalse(wrapper_boundary(Path(tmp)/'repo', lane, policy(), pr, history))
            for changes in ({'number': 43}, {'head': {'ref': '', 'sha': 'b'*40}}):
                with self.subTest(lane=lane, target=changes), tempfile.TemporaryDirectory() as tmp:
                    pr = complete_pr(changes, branch='codex/topic', labels=['builder:codex'])
                    pr['head']['repo'] = {'full_name': 'owner/repo'}
                    self.assertFalse(wrapper_boundary(Path(tmp)/'repo', lane, policy(), pr, []))
