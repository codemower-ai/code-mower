"""Owning rows B/C/D: real label entrypoints, external reads and writes only."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from code_mower import saas_reviewer_labeler as saas, trailer_comment_labeler as trailer
from code_mower.audit_labeler_lib import lineage_identity
from lineage_consumer_fixtures import AUTHORS, HEAD, REPO, complete_pr, policy


class LabelConsumers(unittest.TestCase):
    def test_greptile_both_structural_requeues_resolve_empty_history(self):
        for branch, prefixes, expected in (
            ('codex/topic', None, False), ('claude/topic', None, True),
            ('feature/cx-topic', None, False), ('codex/topic', {}, True),
        ):
            for missing_id in (True, False):
                with self.subTest(branch=branch, prefixes=prefixes, missing_id=missing_id):
                    labels = ['builder:claude', 'greptile-audit-done', 'greptile-audit-blocked']
                    pr = complete_pr(branch=branch, labels=labels)
                    review = {'user': {'login': 'greptile-apps[bot]'}, 'commit_id': HEAD}
                    if not missing_id:
                        review['id'] = 10
                    event = {'action': 'submitted', 'pull_request': pr, 'review': review}
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp)/'event.json'
                        path.write_text(json.dumps(event))
                        env = {'GITHUB_EVENT_PATH': str(path), 'GITHUB_EVENT_NAME': 'pull_request_review',
                            'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 'fixture',
                            'CODE_MOWER_AUTHOR_EXCLUSION_JSON': json.dumps(lineage_identity(policy(prefixes)).to_mapping()),
                            'CODE_MOWER_DECISION_AUTHORITIES': ','.join(AUTHORS)}
                        out = io.StringIO()
                        with patch.dict(os.environ, env, clear=True), redirect_stdout(out), \
                             patch.object(saas, 'fetch_pull_request', return_value=pr), \
                             patch.object(saas, 'github_request_with_fallback', return_value=[]), \
                             patch.object(saas, 'fetch_review_comments', side_effect=saas.ReviewCommentsTruncated('capped')), \
                             patch.object(saas, 'apply_label_decision') as apply:
                            self.assertEqual(saas.main(['--adapter', 'greptile']), 0)
                        self.assertEqual(apply.call_count, int(expected), out.getvalue())
                        if not expected:
                            self.assertIn('identity_branch_conflict', out.getvalue())
                        else:
                            self.assertEqual(apply.call_args.args[1].add_label, 'needs-greptile-audit')

    def test_trailer_raw_history_refuses_before_terminal_event_merge(self):
        for history in (None, {}, [None], [{'body': None}], [{'user': {'login': AUTHORS[0]},
                'body': '<!-- CODE_MOWER_BUILDER_LINEAGE: broken -->'}]):
            with self.subTest(history=history), tempfile.TemporaryDirectory() as tmp:
                pr = complete_pr(branch='claude/topic', labels=['builder:claude'])
                body = f'Codex Audit - PASS\nHead SHA: `{HEAD}`\n<!-- CODEX_AUDIT_STATE: codex-audit-done -->'
                event = {'action': 'created', 'issue': {'number': 42, 'pull_request': {}},
                    'comment': {'id': 1, 'user': {'login': 'codex-audit-bot'}, 'body': body}}
                path = Path(tmp)/'event.json'
                path.write_text(json.dumps(event))
                env = {'GITHUB_EVENT_PATH': str(path), 'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 'fixture',
                    'CODE_MOWER_AUTHOR_EXCLUSION_JSON': json.dumps(lineage_identity(policy()).to_mapping()),
                    'CODE_MOWER_DECISION_AUTHORITIES': ','.join(AUTHORS)}
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(trailer, 'fetch_pull_request', return_value=pr), \
                     patch.object(trailer, 'fetch_issue_comments', return_value=history), \
                     patch.object(trailer, 'apply_label_decision') as apply:
                    self.assertEqual(trailer.main(['--lane', 'codex']), 0)
                apply.assert_not_called()

    def test_check_run_fallback_and_all_normal_saas_sinks_keep_admission(self):
        for branch, allowed in (('codex/topic', False), ('claude/topic', True)):
            for route in ('check-success', 'check-lookup-failed', 'review', 'comment', 'replay'):
                with self.subTest(branch=branch, route=route), tempfile.TemporaryDirectory() as tmp:
                    adapter = 'gitar' if route in ('comment', 'replay') else 'greptile'
                    labels = ['builder:claude', 'needs-'+adapter+'-audit']
                    pr = complete_pr(branch=branch, labels=labels)
                    comment = {'id': 9, 'user': {'login': 'gitar-ai[bot]'},
                        'body': '<b>Code Review</b><kbd>Approved</kbd>'}
                    if route.startswith('check'):
                        event_type = 'check_run'
                        event = {'action': 'completed', 'check_run': {'status': 'completed', 'conclusion': 'success',
                            'name': 'Greptile Review', 'app': {'slug': 'greptile-apps'}, 'head_sha': HEAD,
                            'pull_requests': [{'number': 42}]}}
                    elif route == 'review':
                        event_type = 'pull_request_review'
                        event = {'action': 'submitted', 'pull_request': pr, 'review': {'id': 8,
                            'user': {'login': 'greptile-apps[bot]'}, 'commit_id': HEAD}}
                    else:
                        event_type = 'issue_comment' if route == 'comment' else 'issues'
                        event = {'action': 'created' if route == 'comment' else 'labeled',
                            'issue': {'number': 42, 'pull_request': {}}, 'comment': comment,
                            'label': {'name': 'needs-gitar-audit'}}
                    path = Path(tmp)/'event.json'
                    path.write_text(json.dumps(event))
                    env = {'GITHUB_EVENT_PATH': str(path), 'GITHUB_EVENT_NAME': event_type,
                        'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 'fixture',
                        'CODE_MOWER_AUTHOR_EXCLUSION_JSON': json.dumps(lineage_identity(policy()).to_mapping()),
                        'CODE_MOWER_DECISION_AUTHORITIES': ','.join(AUTHORS)}
                    out = io.StringIO()
                    with patch.dict(os.environ, env, clear=True), redirect_stdout(out), \
                         patch.object(saas, 'fetch_pull_request', return_value=pr), \
                         patch.object(saas, 'github_request_with_fallback', return_value=[]), \
                         patch.object(saas, 'fetch_review_comments', return_value=[]), \
                         patch.object(saas, 'fetch_issue_comments', return_value=[comment]), \
                         patch.object(saas, 'fetch_pull_request_reviews', **({'side_effect': saas.ReviewCommentsTruncated('capped')} if route == 'check-lookup-failed' else {'return_value': []})), \
                         patch.object(saas, 'apply_label_decision') as apply:
                        self.assertEqual(saas.main(['--adapter', adapter]), 0)
                    self.assertEqual(apply.call_count, int(allowed), out.getvalue())
                    if route == 'check-lookup-failed' and allowed:
                        self.assertEqual(apply.call_args.args[1].add_label, 'needs-greptile-audit')
