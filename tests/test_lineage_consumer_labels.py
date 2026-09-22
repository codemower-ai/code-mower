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


class RawFetchLabelConsumers(unittest.TestCase):
    """Exercise every main route through HTTP decoding, history and real sinks."""
    routes = ('check-success', 'check-lookup-failed', 'review', 'comment', 'replay',
              'missing-review-id', 'inline-failed', 'inline-capped')

    def execute(self, route, history, *, branch='claude/topic', prefixes=None,
                builder='claude'):
        from urllib.error import HTTPError
        from urllib.parse import urlsplit, parse_qs
        adapter = 'gitar' if route in ('comment', 'replay') else 'greptile'
        labels = [f'builder:{builder}', f'needs-{adapter}-audit',
                  f'{adapter}-audit-done', f'{adapter}-audit-blocked']
        pr = complete_pr(branch=branch, labels=labels)
        comment = {'id': 9, 'user': {'login': 'gitar-ai[bot]'},
                   'body': '<b>Code Review</b><kbd>Approved</kbd>'}
        if route.startswith('check'):
            event_type = 'check_run'
            event = {'action': 'completed', 'check_run': {'status': 'completed',
                'conclusion': 'success', 'name': 'Greptile Review',
                'app': {'slug': 'greptile-apps'}, 'head_sha': HEAD,
                'pull_requests': [{'number': 42}]}}
        elif route in ('comment', 'replay'):
            event_type = 'issue_comment' if route == 'comment' else 'issues'
            event = {'action': 'created' if route == 'comment' else 'labeled',
                'issue': {'number': 42, 'pull_request': {}}, 'comment': comment,
                'label': {'name': 'needs-gitar-audit'}}
        else:
            event_type = 'pull_request_review'
            review = {'user': {'login': 'greptile-apps[bot]'}, 'commit_id': HEAD}
            if route != 'missing-review-id':
                review['id'] = 8
            event = {'action': 'submitted', 'pull_request': pr, 'review': review}
        reads, effects = [], []
        def response(request, **kwargs):
            method = request.get_method()
            url = urlsplit(request.full_url)
            page = int(parse_qs(url.query).get('page', ['1'])[0])
            if method != 'GET':
                effects.append((method, url.path, json.loads(request.data) if request.data else None))
                return io.BytesIO(b'{}')
            reads.append(url.path + '?' + url.query)
            if url.path == f'/repos/{REPO}/pulls/42':
                payload = pr
            elif url.path == f'/repos/{REPO}/issues/42/comments':
                if history == 'unreadable':
                    raise HTTPError(request.full_url, 502, 'fixture unavailable', {}, io.BytesIO(b'{}'))
                if history == 'duplicate-key':
                    return io.BytesIO(b'[{"body":"","user":{"login":"x","login":"y"}}]')
                if history == 'cap':
                    payload = [
                        {'id': page * 100 + index, 'body': 'ordinary',
                         'user': {'login': 'someone'}}
                        for index in range(100)
                    ]
                elif history == 'valid':
                    payload = [comment] if route == 'replay' else []
                elif isinstance(history, list):
                    payload = history[(page-1)*100:page*100]
                else:
                    payload = history
            elif url.path.endswith('/reviews'):
                if route == 'check-lookup-failed':
                    raise HTTPError(request.full_url, 502, 'fixture unavailable', {}, io.BytesIO(b'{}'))
                payload = []
            elif url.path.endswith('/reviews/8/comments'):
                if route == 'inline-failed':
                    raise HTTPError(request.full_url, 502, 'fixture unavailable', {}, io.BytesIO(b'{}'))
                payload = [{}]*100 if route == 'inline-capped' else []
            else:
                self.fail(f'unexpected GitHub read: {url.path}')
            return io.BytesIO(json.dumps(payload).encode())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'event.json'
            path.write_text(json.dumps(event))
            env = {'GITHUB_EVENT_PATH': str(path), 'GITHUB_EVENT_NAME': event_type,
                'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 'fixture',
                'CODE_MOWER_AUTHOR_EXCLUSION_JSON': json.dumps(lineage_identity(policy(prefixes)).to_mapping()),
                'CODE_MOWER_DECISION_AUTHORITIES': ','.join(AUTHORS)}
            out = io.StringIO()
            with patch.dict(os.environ, env, clear=True), redirect_stdout(out), \
                 patch('code_mower.audit_labeler_lib.urllib.request.urlopen', side_effect=response):
                self.assertEqual(saas.main(['--adapter', adapter]), 0)
        return reads, effects, out.getvalue()

    def test_real_fetcher_rejects_bad_history_before_every_label_route(self):
        from lineage_consumer_fixtures import cumulative_history, malformed_marker_histories
        from code_mower.builder_lineage import Chain, Target, render
        public, private = cumulative_history()
        tail = {'user': {'login': AUTHORS[0]}, 'body': render(Chain.from_arrivals(
            Target(REPO, 42, 'codex/topic', f'{32:040x}'), private))}
        cases = [('null', None), ('object', {}), ('mixed', [None]),
            ('body-null', [{'body': None}]), ('unreadable', 'unreadable'),
            ('full-page-cap', 'cap'), ('duplicate-key', 'duplicate-key'),
            ('arrival-561', public+[tail, public[0]])]
        cases.extend((f'marker-{i}', raw) for i, raw in enumerate(malformed_marker_histories()))
        for route in self.routes:
            for name, history in cases:
                with self.subTest(route=route, history=name):
                    reads, effects, output = self.execute(route, history)
                    self.assertEqual(effects, [], output)
                    self.assertIn('lineage unreadable', output)
                    history_reads = [r for r in reads if '/issues/42/comments?' in r]
                    self.assertEqual(len(history_reads), 9 if history == 'cap' else 1)

    def test_every_real_sink_preserves_conflict_and_legitimate_controls(self):
        for route in self.routes:
            for branch, prefixes, builder, allowed in (
                ('codex/topic', None, 'claude', False),
                ('claude/topic', None, 'claude', True),
                ('feature/cx-topic', None, 'codex', True),
                ('codex/topic', {}, 'claude', True),
            ):
                with self.subTest(route=route, branch=branch, prefixes=prefixes):
                    reads, effects, output = self.execute(route, 'valid', branch=branch,
                        prefixes=prefixes, builder=builder)
                    self.assertTrue(any('/issues/42/comments?' in r for r in reads))
                    if not allowed:
                        self.assertEqual(effects, [], output)
                        self.assertIn('identity_branch_conflict', output)
                    else:
                        self.assertEqual([e[0] for e in effects], ['POST', 'DELETE', 'DELETE'], output)
                        expected = ('needs-greptile-audit' if route in (
                            'check-lookup-failed', 'missing-review-id', 'inline-failed', 'inline-capped')
                            else ('gitar-audit-done' if route in ('comment', 'replay') else 'greptile-audit-done'))
                        self.assertEqual(effects[0][2], {'labels': [expected]}, output)
