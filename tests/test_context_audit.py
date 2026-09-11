"""Real private-store handoff through both audit wrappers; no provider calls."""

import io
import json
import os
import subprocess
import unittest
from contextlib import ExitStack, redirect_stderr
from unittest import mock

from code_mower import claude_audit_pr as claude
from code_mower import codex_audit_pr as codex
from code_mower import context_audit, context_packets
from code_mower.context_delivery import read_binding
from code_mower.context_review import INPUT_HEADER, marker
from code_mower.provider_runners import verdict_artifacts
import test_context_delivery as fixtures


@unittest.skipUnless(os.name == 'posix', 'private store requires POSIX')
class ContextAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContextDeliveryTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.current = self.fixture.attach()
        self.comments = [{'user': {'login': 'controller'}, 'body': INPUT_HEADER + '\n\n' + marker(self.current)}]
        self.prose = 'Summary: Private source says preserve the special routing constraint. No blocking regressions found.'

    def run_review(self, host, *, during_review=None, unavailable=False, truncated=False, unattached_required=False):
        f = self.fixture
        cap = claude if host == 'claude' else codex
        config_type = claude.ClaudeAuditConfig if host == 'claude' else codex.AuditConfig
        config = config_type('test-authorization', {'owner/repo': f.store.root},
                             include_plan_context=False, include_decision_context=False)
        pr = {'head': {'sha': f.head, 'ref': 'human/fix'}, 'title': 'Fix'}
        expected = f.delivery(self.current).text
        if unavailable:
            self.current = {**self.current, 'state': 'required_unavailable'}
            self.comments[0]['body'] = INPUT_HEADER + '\n\n' + marker(self.current)
        if unattached_required:
            self.comments = []
        model_calls = []
        def review(config, prompt_or_worktree, trusted_context=''):
            model_calls.append(host)
            if host == 'claude':
                self.assertIn(expected, prompt_or_worktree)
                self.assertNotIn('one@example.invalid', prompt_or_worktree)
            else:
                self.assertEqual(config.private_evidence, expected)
                self.assertNotIn(expected, trusted_context)
            if during_review:
                during_review()
            if host == 'claude':
                return claude.ClaudeVerdict(verdict='PASS', prose=self.prose), self.prose, ''
            return self.prose, ''
        with ExitStack() as stack:
            err = stack.enter_context(redirect_stderr(io.StringIO()))
            stack.enter_context(mock.patch.dict(os.environ, {'PYTEST_CURRENT_TEST': '', 'GITHUB_RUN_ID': '',
                'CODE_MOWER_VERDICT_ARTIFACT_DIR': str(f.store.root / 'verdicts')}))
            stack.enter_context(mock.patch.object(context_audit, 'ContextStore', return_value=f.store))
            if unattached_required:
                stack.enter_context(mock.patch.object(context_audit, 'required_for_repo', return_value=True))
            stack.enter_context(mock.patch.object(context_packets, '_backend', return_value=f.backend))
            stack.enter_context(mock.patch.object(cap, 'fetch_pull_request', return_value=pr))
            stack.enter_context(mock.patch.object(cap, 'fetch_issue_comments', side_effect=lambda *a, **k: self.comments))
            stack.enter_context(mock.patch.object(cap, '_decision_authorities_for_repo', return_value=('controller',)))
            post = stack.enter_context(mock.patch.object(cap, 'post_pr_comment', return_value={'html_url': 'https://github.test/comment/1'}))
            if host == 'claude':
                diff = claude.DiffContext('src/app.py | 1 +', 'diff --git a/src/app.py b/src/app.py',
                    ('src/app.py',), truncated, 1000, 1000, 40, 40)
                stack.enter_context(mock.patch.object(claude, '_build_diff_context', return_value=diff))
                stack.enter_context(mock.patch.object(claude.code_mower_prompts, 'load_review_prompt', return_value=''))
                stack.enter_context(mock.patch.object(claude, 'run_claude_audit', side_effect=review))
                sidecar = stack.enter_context(mock.patch.object(claude, '_write_claude_raw_output_sidecar'))
                dump = stack.enter_context(mock.patch.object(claude, '_dump_claude_cli_failure', return_value=None))
            else:
                diag = codex.ReviewContextDiagnostics(base_ref=config.base_ref, head_sha=f.head,
                    changed_file_count=1, diff_bytes=2000 if truncated else 40, requested_max_bytes=1000,
                    hard_limit_bytes=1000, included_diff_bytes=1000 if truncated else 40,
                    effective_budget_usd='2')
                for name, value in (('preflight_codex_cli', 'test-cli'), ('_discover_venv', None),
                    ('_fetch_pr_head', None), ('_fetch_base_ref', None), ('_remove_worktree', None),
                    ('_create_temp_worktree', f.store.root), ('_build_review_context_diagnostics', diag)):
                    stack.enter_context(mock.patch.object(codex, name, return_value=value))
                stack.enter_context(mock.patch.object(codex, 'run_codex_review', side_effect=review))
                structure = stack.enter_context(mock.patch.object(codex, 'run_codex_verdict_structuring',
                    return_value=(codex.CodexVerdict(verdict='PASS', prose=self.prose), self.prose, '')))
                dump = stack.enter_context(mock.patch.object(codex, 'dump_cli_failure', return_value=None))
            result = cap.audit_pr(config, 'owner/repo', 42)
            if host == 'claude' and not unavailable and not unattached_required:
                sidecar.assert_not_called()
            if not unavailable and not unattached_required:
                dump.assert_not_called()
            if host == 'codex' and (unavailable or truncated or unattached_required):
                structure.assert_not_called()
            self.assertEqual(post.call_count, 1)
            public = result.comment_body + err.getvalue() + result.verdict_artifact_path.read_text()
            for private in (self.prose, expected, 'one@example.invalid'):
                self.assertNotIn(private, public)
            if not unattached_required:
                self.assertIn(self.current['revision'], result.comment_body)
        return result, model_calls

    def test_both_reviewers_receive_same_packet_and_keep_findings_private(self):
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                refresh_before = self.fixture.backend.calls.count('refresh')
                result, calls = self.run_review(host)
                self.assertEqual(result.verdict, 'PASS')
                self.assertEqual(calls, [host])
                self.assertEqual(self.fixture.backend.calls.count('refresh') - refresh_before, 3)
                feedback = read_binding(self.fixture.store, self.current['revision'])['feedback']
                self.assertEqual(feedback[host], self.prose)
        self.assertEqual(self.fixture.backend.searches, 1)

    def test_required_missing_context_skips_both_models(self):
        for host in ('claude', 'codex'):
            original = self.current
            result, calls = self.run_review(host, unavailable=True)
            self.assertEqual(result.verdict, 'UNKNOWN')
            self.assertEqual(calls, [])
            self.current = original
            self.comments[0]['body'] = INPUT_HEADER + '\n\n' + marker(original)

    def test_truncation_never_falls_back_to_context_free_review(self):
        for host in ('claude', 'codex'):
            result, calls = self.run_review(host, truncated=True)
            self.assertEqual(result.verdict, 'UNKNOWN')
            self.assertEqual(calls, [])

    def test_trusted_required_policy_blocks_audit_even_before_first_attachment(self):
        for host in ('claude', 'codex'):
            result, calls = self.run_review(host, unattached_required=True)
            self.assertEqual(result.verdict, 'UNKNOWN')
            self.assertEqual(calls, [])

    def test_required_policy_comes_from_trusted_base_not_proposed_files(self):
        repo = self.fixture.store.root.parent / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True)
        git('init', '-q')
        (repo / 'code-mower.yml').write_text('context:\n  schema: code_mower.contextPolicy.v1\n'
            '  connection: example\n  policy_version: v1\n  required: true\n')
        git('add', 'code-mower.yml')
        git('-c', 'user.name=Example', '-c', 'user.email=example@example.invalid', 'commit', '-qm', 'Trusted policy')
        (repo / 'code-mower.yml').write_text('context: null\n')
        self.assertTrue(context_audit.required_for_repo(repo, 'HEAD'))
        with self.assertRaisesRegex(Exception, 'trusted context policy'):
            context_audit.required_for_repo(repo, 'does-not-exist')

    def test_revocation_after_paid_review_does_not_accept_or_persist_findings(self):
        result, calls = self.run_review('claude', during_review=lambda: setattr(self.fixture.backend, 'revoked', True))
        self.assertEqual(result.verdict, 'UNKNOWN')
        self.assertEqual(calls, ['claude'])
        self.assertEqual(read_binding(self.fixture.store, self.current['revision'])['feedback'], {})

    def test_changed_context_during_review_does_not_accept_old_verdict(self):
        def change():
            replacement = {**self.current, 'revision': 'd' * 32}
            self.comments[0]['body'] = INPUT_HEADER + '\n\n' + marker(replacement)
        result, _ = self.run_review('codex', during_review=change)
        self.assertEqual(result.verdict, 'UNKNOWN')
        self.assertEqual(read_binding(self.fixture.store, self.current['revision'])['feedback'], {})

    def test_context_bound_artifact_cannot_be_reposted_without_fresh_audit(self):
        result, _ = self.run_review('claude')
        self.assertEqual(json.loads(result.verdict_artifact_path.read_text())['verdict'], 'PASS')
        with mock.patch.object(verdict_artifacts, 'post_pr_comment') as post:
            with self.assertRaisesRegex(ValueError, 'fresh authorized audit'):
                verdict_artifacts.repost_audit_verdict_artifact(result.verdict_artifact_path, token='test-authorization')
            post.assert_not_called()
