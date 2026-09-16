"""Owning rows E/F: emitted standalone gate and status/controller/Board."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

from code_mower import config as config_module, init, lane_status, controller, board
from code_mower.audit_labeler_lib import lineage_identity
from lineage_consumer_fixtures import AUTHORS, HEAD, REPO, complete_pr, marker_history, policy
from test_controller import _config, _options

ROOT = Path(__file__).resolve().parents[1]


class ProjectionConsumers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cfg = config_module.load_config(ROOT/'src/code_mower/templates/code-mower.example.yml')
        plan = init.render_init_plan(cfg, package_mode=True, repo_root=ROOT)
        cls.materialized = cls.root/'materialized'
        init.apply_init_plan(plan, cls.materialized, source_root=ROOT)

    def gate(self, pr, history, *, config=None, source=None):
        source = source or self.materialized/'.github/workflows/code-mower-gate.yml'
        raw = source.read_text()
        body = raw.split('python3 - "${labels_file}" "${comments_file}" "${events_file}" "${pr_file}" "${audit_runs_file}" <<\'PY\'\n', 1)[1].split('\n          PY', 1)[0]
        program = ('import sys, importlib.util\nfrom pathlib import Path\n'
            'assert importlib.util.find_spec("code_mower") is None\n'
            'sys.path.insert(0, str(Path.cwd()))\n') + textwrap.dedent(body)
        files = []
        for name, payload in (('labels', pr['labels']), ('comments', history), ('events', []),
                ('pr', pr), ('runs', [])):
            path = self.root/(name+'.json')
            path.write_text(json.dumps(payload))
            files.append(str(path))
        lanes = [{'id': lane, 'author_lane': lane, 'done': lane+'-audit-done',
            'blocked': lane+'-audit-blocked', 'bot_authors': lane+'-audit-bot'} for lane in ('codex', 'claude')]
        env = os.environ | {'CODE_MOWER_AUTHOR_EXCLUSION_JSON': json.dumps(lineage_identity(config or policy()).to_mapping()),
            'CODE_MOWER_GATE_LANES_JSON': json.dumps(lanes), 'CODE_MOWER_DECISION_AUTHORITIES': ','.join(AUTHORS),
            'HEAD_SHA': pr['head']['sha'], 'PR_NUMBER': '42', 'GITHUB_REPOSITORY': REPO,
            'CODE_MOWER_OWNER_LOGIN': '', 'CODE_MOWER_OWNER_LOGIN_OVERRIDE': '',
            'CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE': ''}
        result = subprocess.run([sys.executable, '-I', '-S', '-c', program, *files],
            cwd=self.materialized, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {parts[0]: shlex.split(parts[1])[0] for line in result.stdout.splitlines()
            if '=' in line and (parts := line.split('=', 1)) and parts[1]}

    def test_materialized_and_maintained_gate_empty_conflict_stale_and_all_contributors(self):
        sources = [self.materialized/'.github/workflows/code-mower-gate.yml', ROOT/'.github/workflows/code-mower-gate.yml',
            ROOT/'templates/workflows/code-mower-gate.yml.j2', ROOT/'src/code_mower/templates/workflows/code-mower-gate.yml.j2']
        for source in sources:
            for name, pr, history, state, detail in (
                ('conflict', complete_pr(branch='codex/topic', labels=['builder:claude']), [[]], 'failure', 'identity_branch_conflict'),
                ('matched', complete_pr(branch='codex/topic', labels=['builder:codex']), [[]], 'pending', 'waiting for audit'),
                ('stale', complete_pr(branch='codex/topic', labels=['builder:claude']), [marker_history()], 'pending', 'lineage_head_pending'),
                ('contributors', complete_pr(branch='codex/topic', head=f'{2:040x}', labels=['builder:claude']), [marker_history(2)], 'failure', 'no independent'),
            ):
                with self.subTest(source=source.name, case=name):
                    result = self.gate(pr, history, source=source)
                    self.assertEqual(result['gate_state'], state, result)
                    self.assertIn(detail, result['gate_description'])

    def test_gate_validates_raw_pages_before_flattening(self):
        for history in (None, {}, [None], [[None]], [[{'body': None}]], [[], {}]):
            with self.subTest(history=history):
                result = self.gate(complete_pr(branch='codex/topic'), history)
                self.assertEqual(result['gate_state'], 'failure')

    def test_pivot_through_actual_status_controller_and_board(self):
        cfg = _config()
        cfg['builder_identity'] = policy()['builder_identity']
        calls = []
        def gh(args):
            calls.append(args)
            if args[:2] == ['pr', 'list']:
                return [{'number': 42, 'headRefName': 'codex/topic', 'headRefOid': HEAD,
                    'author': {'login': 'human'}, 'labels': [{'name': 'builder:claude'}],
                    'mergeStateStatus': 'CLEAN', 'statusCheckRollup': []}]
            if args[0] == 'api':
                return []
            return []
        report = lane_status.collect_status(repo=REPO, gh_json_runner=gh, lineage_config=cfg,
            command_runner=lambda args: subprocess.CompletedProcess(args, 0, '', ''))
        pr = report['remote']['pull_requests'][0]
        self.assertEqual(pr['lineage']['status'], 'conflict')
        self.assertIsNone(pr['lineage']['current_writer'])
        evaluated = controller.evaluate_controller_report(status_report=report,
            ready_issues={'available': True, 'errors': [], 'issues': []}, config=cfg, options=_options())
        decision = evaluated['decision']
        self.assertFalse(decision['would_mutate'])
        self.assertEqual(decision['stop_condition'], 'lineage_unresolved')
        projected = board._supervised_decision_payload(decision)
        self.assertIsNone(projected['lineage']['current_writer'])
        self.assertLessEqual(sum(args[0] == 'api' for args in calls), 9)

    def test_status_global_budget_retains_unknown_targets(self):
        config = policy()
        calls = []
        def gh(args):
            if args[:2] == ['pr', 'list']:
                return [{'number': i, 'headRefName': 'codex/topic', 'headRefOid': HEAD,
                    'author': {'login': 'human'}, 'labels': [{'name': 'builder:codex'}]} for i in range(1, 11)]
            if args[0] == 'api':
                calls.append(args)
                return [{}]*100
            return []
        report = lane_status.collect_status(repo=REPO, gh_json_runner=gh, lineage_config=config,
            command_runner=lambda args: subprocess.CompletedProcess(args, 0, '', ''))
        self.assertEqual(len(report['remote']['pull_requests']), 10)
        self.assertEqual(len(calls), 64)
        self.assertTrue(all(p['lineage']['status'] == 'unknown' for p in report['remote']['pull_requests']))
        self.assertTrue(all(p['lineage']['current_writer'] is None for p in report['remote']['pull_requests']))

    def test_gate_cumulative_arrivals_are_bounded_before_duplicate_reduction(self):
        from lineage_consumer_fixtures import cumulative_history
        pr = complete_pr(branch='codex/topic', head=f'{32:040x}', labels=['builder:claude'])
        public, private = cumulative_history()
        # The hosted gate deliberately acquires public-only history. Publish the
        # same final 32 arrivals as another authenticated marker for its boundary.
        from code_mower.builder_lineage import Chain, Target, render
        tail = {'user': {'login': AUTHORS[0]}, 'body': render(Chain.from_arrivals(
            Target(REPO, 42, 'codex/topic', f'{32:040x}'), private))}
        for extra, expected in (([], 'no independent'), ([public[0]], 'contract unreadable')):
            result = self.gate(pr, [public+[tail]+extra])
            self.assertEqual(result['gate_state'], 'failure')
            self.assertIn(expected, result['gate_description'].lower())

    def test_gate_rejects_all_announced_malformed_markers(self):
        from lineage_consumer_fixtures import malformed_marker_histories
        for history in malformed_marker_histories():
            with self.subTest(history=history):
                result = self.gate(complete_pr(branch='codex/topic', labels=['builder:codex']), [history])
                self.assertEqual(result['gate_state'], 'failure')
                self.assertIn('contract unreadable', result['gate_description'])

    def test_custom_prefix_and_no_contract_pass_real_gates_and_status_controller_board(self):
        sources = [self.materialized/'.github/workflows/code-mower-gate.yml', ROOT/'.github/workflows/code-mower-gate.yml',
            ROOT/'templates/workflows/code-mower-gate.yml.j2', ROOT/'src/code_mower/templates/workflows/code-mower-gate.yml.j2']
        for branch, prefixes, writer, reviewer in (
            ('feature/cx-topic', None, 'codex', 'claude'),
            ('codex/topic', {}, 'claude', 'codex'),
        ):
            cfg = _config()
            cfg['builder_identity'] = policy(prefixes)['builder_identity']
            labels = [f'builder:{writer}', f'{reviewer}-audit-done']
            pr = complete_pr(branch=branch, labels=labels)
            comment = {'user': {'login': f'{reviewer}-audit-bot'},
                'body': f'Head SHA: `{HEAD}`\n<!-- {reviewer.upper()}_AUDIT_STATE: {reviewer}-audit-done -->'}
            for source in sources:
                with self.subTest(branch=branch, source=source):
                    result = self.gate(pr, [[comment]], config=cfg, source=source)
                    self.assertEqual(result['gate_state'], 'success', result)
            def gh(args, branch=branch, pr=pr):
                if args[:2] == ['pr', 'list']:
                    return [{'number': 42, 'headRefName': branch, 'headRefOid': HEAD,
                        'author': {'login': 'human'}, 'labels': pr['labels'], 'isDraft': False,
                        'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [
                            {'__typename': 'CheckRun', 'name': 'Code Mower Gate', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}]
                return []
            report = lane_status.collect_status(repo=REPO, gh_json_runner=gh, lineage_config=cfg,
                command_runner=lambda args: subprocess.CompletedProcess(args, 0, '', ''))
            lineage = report['remote']['pull_requests'][0]['lineage']
            self.assertEqual(lineage['status'], 'ready')
            self.assertEqual(lineage['current_writer'], writer)
            self.assertIn(reviewer, lineage['admitted_reviewers'])
            self.assertNotIn(writer, lineage['admitted_reviewers'])
            evaluated = controller.evaluate_controller_report(status_report=report,
                ready_issues={'available': True, 'errors': [], 'issues': []}, config=cfg, options=_options())
            decision = evaluated['decision']
            self.assertEqual(decision['decision_state'], 'ready_to_merge', decision)
            self.assertFalse(decision['would_mutate'])
            projected = board._supervised_decision_payload(decision)
            self.assertEqual(projected['lineage']['status'], 'ready')
            self.assertEqual(projected['lineage']['current_writer'], writer)
            self.assertFalse(projected['would_mutate'])
