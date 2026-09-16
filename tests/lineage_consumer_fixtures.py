"""Finite exact trusted positives shared by owning entrypoint rows A–H.

Only Git/GitHub/provider/clock boundaries are simulated. Pure decisions are real.
"""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import yaml

from code_mower.builder_lineage import Authorities, Chain, Episode, History, Target, render, admit
from code_mower.audit_labeler_lib import lineage_decision, lineage_identity, lineage_projection

REPO = 'owner/repo'
HEAD = 'b' * 40
BASE = 'a' * 40
AUTHORS = ('lineage-publisher[bot]',)
# Committed owning selector inventory; independent of repository Git history.
ROWS = {
    'A': ('test_lineage_consumer_admission.py::AdmissionConsumers::test_actual_wrappers_share_conflict_empty_takeover_and_stale_decisions',
          'test_lineage_consumer_admission.py::AdmissionConsumers::test_wrapper_cumulative_history_budget_and_strict_announced_markers',
          'test_devin_review.py::LineageReviewLifecycleTests',
          'test_devin_cli_audit_pr.py::TestPublicLineageRefusals'),
    'B': ('test_lineage_consumer_labels.py::LabelConsumers::test_check_run_fallback_and_all_normal_saas_sinks_keep_admission',
          'test_lineage_consumer_labels.py::RawFetchLabelConsumers::test_real_fetcher_rejects_bad_history_before_every_label_route'),
    'C': ('test_lineage_consumer_labels.py::LabelConsumers::test_greptile_both_structural_requeues_resolve_empty_history',
          'test_lineage_consumer_labels.py::RawFetchLabelConsumers::test_every_real_sink_preserves_conflict_and_legitimate_controls'),
    'D': ('test_lineage_consumer_labels.py::LabelConsumers::test_trailer_raw_history_refuses_before_terminal_event_merge',),
    'E': ('test_lineage_consumer_projection.py::ProjectionConsumers::test_materialized_and_maintained_gate_empty_conflict_stale_and_all_contributors',
          'test_lineage_consumer_projection.py::ProjectionConsumers::test_gate_cumulative_arrivals_are_bounded_before_duplicate_reduction'),
    'F': ('test_lineage_consumer_projection.py::ProjectionConsumers::test_pivot_through_actual_status_controller_and_board',
          'test_lineage_consumer_projection.py::ProjectionConsumers::test_status_global_budget_retains_unknown_targets',
          'test_lineage_consumer_projection.py::ProjectionConsumers::test_custom_prefix_and_no_contract_pass_real_gates_and_status_controller_board'),
    'G': ('test_lineage_consumer_activation.py::ProducerActivation::test_actual_entrypoint_takeover_continuation_then_third_writer',
          'test_lineage_consumer_activation.py::ProducerActivation::test_cumulative_32_rounds_and_overflow_refuse_before_next_launch'),
    'H': ('test_lineage_producer_artifacts.py::ArtifactTests::test_installed_candidate_supervisor_and_public_readback_business',
          'test_lineage_producer_artifacts.py::ArtifactTests::test_published_140_refuses_emitted_artifact_before_any_effect'),
}


def policy(prefixes=None):
    return {'version': '1', 'project': {'name': 'fixture', 'state_dir': '.code-mower'},
        'repositories': [{'slug': REPO, 'default_branch': 'main'}],
        'lanes': {}, 'merge_authority_excludes_author': True,
        'decisions': {'authorities': list(AUTHORS)},
        'builder_identity': {'labels': {f'builder:{lane}': lane for lane in ('codex', 'claude', 'devin')},
            'authors': {'source-bot': 'codex'},
            'branch_prefixes': {'codex/': 'codex', 'claude/': 'claude', 'feature/cx-': 'codex'} if prefixes is None else prefixes}}


def complete_pr(payload=None, *, number=42, branch='human/fix', head=HEAD, author='human', labels=()):
    raw = {'number': number, 'state': 'open', 'head': {'ref': branch, 'sha': head},
           'base': {'repo': {'full_name': REPO}, 'sha': BASE},
           'user': {'login': author}, 'labels': [{'name': item} for item in labels]}
    if payload:
        raw.update(deepcopy(payload))
    return raw


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


class FixtureDumper(yaml.SafeDumper):
    def represent_sequence(self, tag, sequence, flow_style=None):
        # The repository subset represents scalar lists inline, including
        # alternatives nested in token_env_any. Preserve values exactly.
        return super().represent_sequence(tag, sequence,
            flow_style=all(not isinstance(item, (dict, list)) for item in sequence))

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def policy_text(config):
    return yaml.dump(config, Dumper=FixtureDumper, width=1000000)


def fixture_shell_env(directory):
    """Constrain no-argument mktemp, including CI shells with TMPDIR unset."""
    import shlex
    path = Path(directory)/'fixture-shell.sh'
    fallback = shlex.quote(str(Path(directory).resolve()))
    path.write_text('mktemp() { if [ "$#" -eq 0 ]; then local fixture_tmp="${TMPDIR:-}"; '
        '[ -n "$fixture_tmp" ] || fixture_tmp=' + fallback + '; '
        'command mktemp "${fixture_tmp%/}/tmp.XXXXXXXX"; else command mktemp "$@"; fi; }\n')
    return {'BASH_ENV': str(path)}


def pinned_repo(root, config=None):
    root = Path(root)
    if not (root / '.git').exists():
        root.mkdir(parents=True, exist_ok=True)
        git(root, 'init', '-q', '--initial-branch', 'main')
        if config is not None:
            (root / 'code-mower.yml').write_text(policy_text(config))
            git(root, 'add', 'code-mower.yml')
        git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'Trusted fixture base')
        git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return git(root, 'rev-parse', 'origin/main')


def status_lineage(pr, config):
    target = Target(REPO, pr['number'], pr['branch'], pr['head_sha'])
    _, decision = lineage_decision(target, lineage_identity(config), Authorities([]), History([]),
        author=pr['author'], labels=[s for group in pr['labels'].values() for s in group])
    value = lineage_projection(decision)
    value.update(repo=REPO, pr_number=target.pr_number, branch=target.branch,
        admitted_reviewers=[lane for lane in ('codex', 'claude', 'devin') if admit(decision, lane)])
    return value


def takeover(n=1):
    return Episode(sequence=n, repo=REPO, pr_number=42, branch='codex/topic',
        source_lane='codex' if n == 1 else 'claude', destination_lane='claude',
        expected_head=f'{n-1:040x}', resulting_head=f'{n:040x}',
        kind='handoff' if n == 1 else 'continuation', writer_state='terminated' if n == 1 else 'same_writer')


def marker_history(n=1):
    chain = Chain.from_arrivals(Target(REPO, 42, 'codex/topic', f'{n:040x}'), [takeover(i) for i in range(1, n+1)])
    return [{'user': {'login': AUTHORS[0]}, 'body': render(chain)}]


class ProviderReached(BaseException):
    """Stop at the external provider boundary without invoking a model."""


def wrapper_boundary(root, lane, config, pr, history):
    from contextlib import ExitStack
    from unittest.mock import patch
    from code_mower import codex_audit_pr as codex, claude_audit_pr as claude, devin_cli_audit_pr as devin
    from code_mower.provider_runners import github_pr
    base = pinned_repo(root, config)
    module = {'codex': codex, 'claude': claude, 'devin': devin}[lane]
    pr = deepcopy(pr)
    symbolic = pr['head']['sha']
    actual = git(root, 'rev-parse', 'HEAD')
    pr['head']['sha'] = actual
    if isinstance(history, list):
        history = json.loads(json.dumps(history).replace(symbolic, actual))
    with ExitStack() as stack:
        stack.enter_context(patch.object(module, 'fetch_pull_request', return_value=pr))
        post = stack.enter_context(patch.object(module, 'post_pr_comment'))
        stack.enter_context(patch.object(github_pr, 'fetch_issue_comments', return_value=history))
        if lane != 'devin':
            stack.enter_context(patch.object(module, 'fetch_issue_comments', return_value=history))
        if lane == 'codex':
            cfg = codex.AuditConfig('fixture', {REPO: root}, include_plan_context=False, include_decision_context=False)
            for name, value in (('preflight_codex_cli', 'fixture'), ('_discover_venv', None),
                ('_fetch_pr_head', None), ('_fetch_base_ref', None), ('_remove_worktree', None),
                ('_create_temp_worktree', root)):
                stack.enter_context(patch.object(module, name, return_value=value))
            diagnostics = codex.ReviewContextDiagnostics(base_ref=base, head_sha=pr['head']['sha'],
                changed_file_count=1, diff_bytes=40, requested_max_bytes=1000,
                hard_limit_bytes=1000, included_diff_bytes=40, effective_budget_usd='2')
            stack.enter_context(patch.object(module, '_build_review_context_diagnostics', return_value=diagnostics))
            provider = stack.enter_context(patch.object(module, 'run_codex_review', side_effect=ProviderReached))
            def invoke():
                return codex.audit_pr(cfg, REPO, 42)
        elif lane == 'claude':
            cfg = claude.ClaudeAuditConfig('fixture', {REPO: root}, include_plan_context=False, include_decision_context=False)
            diff = claude.DiffContext('src/a.py | 1 +', 'diff --git a/src/a.py b/src/a.py\n+a=1',
                ('src/a.py',), False, 1000, 1000, 40, 40, fetched_base_ref=base)
            stack.enter_context(patch.object(module, '_build_diff_context', return_value=diff))
            provider = stack.enter_context(patch.object(module, 'run_claude_audit', side_effect=ProviderReached))
            def invoke():
                return claude.audit_pr(cfg, REPO, 42)
        else:
            cfg = devin.AuditConfig(github_token='fixture', repo=REPO, pr_number=42,
                repo_paths={REPO: root}, base_ref=base)
            stack.enter_context(patch.object(module, '_resolve_diff', return_value=('diff --git a/src/a.py b/src/a.py\n+a=1', ('src/a.py',))))
            provider = stack.enter_context(patch.object(module, '_run_devin_cli', side_effect=ProviderReached))
            def invoke():
                return devin._do_audit_pr(cfg)
        try:
            invoke()
        except ProviderReached:
            assert provider.call_count == 1
            post.assert_not_called()
            return True
        except ValueError:
            provider.assert_not_called()
            post.assert_not_called()
            return False
        raise AssertionError('Entrypoint neither refused nor reached provider')


def runner_lineage_env(root, config, pr):
    """Complete immutable policy and exact snapshots at external runner boundaries."""
    import sys
    root = Path(root)
    fixture = root/'runner-lineage.json'
    config = deepcopy(config)
    config.setdefault('decisions', {})['authorities'] = list(AUTHORS)
    fixture.write_text(json.dumps({'policy': policy_text(config), 'pr': pr}))
    code = root/'runner-lineage-boundary.py'
    code.write_text('''import base64, json, os, sys
from pathlib import Path
f = json.loads(Path(os.environ['RUNNER_LINEAGE_FIXTURE']).read_text())
p = f['pr']
if (Path(os.environ['HOME'])/'lane-delivered').exists(): p['head']['sha'] = 'b'*40
args = sys.argv[2:]
if sys.argv[1] == 'gh':
    endpoint = args[1]
    if '/git/trees/' in endpoint:
        raw = {'truncated': False, 'tree': [{'path': 'code-mower.yml', 'type': 'blob', 'sha': 'd'*40}]}
    elif '/git/blobs/' in endpoint:
        raw = {'encoding': 'base64', 'content': base64.b64encode(f['policy'].encode()).decode()}
    elif '/pulls/' in endpoint:
        raw = p
    elif '/comments?' in endpoint: raw = []
    elif '/labels?' in endpoint: raw = p['labels']
    else: raise AssertionError(endpoint)
    print(json.dumps(raw))
else:
    if args[:1] == ['-C']: args = args[2:]
    if args[:1] == ['ls-tree']: print('code-mower.yml')
    elif args[:1] == ['show']: print(f['policy'])
    elif args == ['branch', '--show-current']: print(p['head']['ref'])
    elif args[:1] == ['rev-parse'] and args[1] != '--git-path':
        print(p['head']['sha'] if args[1] == 'HEAD' else p['base']['sha'])
    else: sys.exit(91)
''')
    return {'RUNNER_LINEAGE_FIXTURE': str(fixture), 'RUNNER_LINEAGE_BOUNDARY': str(code),
        'LANE_PYTHON': sys.executable}


RUNNER_GH_BOUNDARY = '''if [ "${1:-}" = api ] && [ "${2:-}" != --paginate ] && [ -n "${RUNNER_LINEAGE_FIXTURE:-}" ]; then
  exec "$LANE_PYTHON" "$RUNNER_LINEAGE_BOUNDARY" gh "$@"
fi
'''
RUNNER_GIT_BOUNDARY = '''if [ -n "${RUNNER_LINEAGE_FIXTURE:-}" ]; then
  set +e
  "$LANE_PYTHON" "$RUNNER_LINEAGE_BOUNDARY" git "$@"
  fixture_rc=$?
  set -e
  [ "$fixture_rc" -eq 91 ] || exit "$fixture_rc"
fi
'''


def cumulative_history():
    """32 cumulative public snapshots: 528 arrivals, plus 32 private records."""
    public = [marker_history(i)[0] for i in range(1, 33)]
    return public, [takeover(i) for i in range(1, 33)]


def malformed_marker_histories():
    from code_mower.builder_lineage import LINEAGE_SCHEMA
    valid = marker_history()[0]['body']
    bodies = [
        '<!-- CODE_MOWER_BUILDER_LINEAGE -->',
        '<!-- CODE_MOWER_BUILDER_LINEAGE: broken -->',
        '<!-- CODE_MOWER_BUILDER_LINEAGE: '+json.dumps({'schema': LINEAGE_SCHEMA, 'episodes': []})+' -->',
        valid+valid,
        '<!-- CODE_MOWER_BUILDER_LINEAGE: {"schema":"'+LINEAGE_SCHEMA+'","episodes":[{"nested":{"x":1,"x":2}}]} -->',
        valid.replace('owner/repo', 'wrong/repo'),
    ]
    return [[{'user': {'login': AUTHORS[0]}, 'body': body}] for body in bodies]
