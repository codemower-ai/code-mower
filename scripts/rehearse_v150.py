"""Disposable wheel-only offline lifecycle rehearsal; never live Slack evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from release_candidate import GRAPHIFY_CHECKS, NAMES, REHEARSAL_SCHEMA, verify, verify_rehearsal


# This program runs under the fresh venv's -I interpreter. It imports only the
# retained wheel and the standard library, never checkout/test fixture modules.
GRAPHIFY_SMOKE = """
import io, json, tarfile
from contextlib import redirect_stdout
from pathlib import Path
import code_mower
from code_mower import context_graph_command as command
from code_mower import context_graph_connection as connection
from code_mower import context_graph_lifecycle as lifecycle
from code_mower import context_graph_query as query
from code_mower.context_store import ContextStore

for module in (code_mower, command, connection, lifecycle, query):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
repository, private = (Path(arg) for arg in sys.argv[1:])
private.mkdir(mode=0o700)

class NoCredentials:
    def refuse(self, *args):
        raise AssertionError('synthetic graph must not access credentials')
    get = put = delete = refuse

store = ContextStore(private, vault=NoCredentials())
connection.connect(store, 'synthetic-graph', {
    'repository_root': str(repository), 'repositories': ['public/example'],
    'recipients': ['codex:builder'],
})
policy = {'schema': 'code_mower.contextPolicy.v1', 'connection': 'synthetic-graph',
          'policy_version': 'v1', 'required': True}

def node(identifier, label, line, **extra):
    return {'id': identifier, 'label': label, 'file_type': 'code',
            'source_file': 'example.py', 'source_location': f'L{line}', **extra}

def edge(source, target, relation='calls', confidence='EXTRACTED'):
    return {'source': source, 'target': target, 'relation': relation,
            'confidence': confidence, 'source_file': 'example.py', 'source_location': 'L1'}

def document():
    return {'nodes': [node('n-target', 'synthetic_target', 1),
                      node('n-caller', 'synthetic_caller', 2)],
            'edges': [edge('n-caller', 'n-target')], 'hyperedges': [],
            'input_tokens': 0, 'output_tokens': 0, 'extracted_sources': ['example.py']}

def publish(value, distribution='graphifyy'):
    def indexer(request):
        raw = json.dumps(value).encode()
        with tarfile.open(request.output_path, 'w') as archive:
            member = tarfile.TarInfo('graph.json')
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        return lifecycle.IndexResult(completeness=lifecycle.COMPLETE, indexed_files=1)
    manifest = lifecycle.build_graph(repository, root=private, indexer=indexer,
        pin=lifecycle.GraphifyPin(distribution=distribution, version='0.9.58', wheel_sha256='a'*64))
    assert manifest.completeness == lifecycle.COMPLETE

def observe():
    state = lifecycle.GraphStateRoot(repository, root=private)
    status = lifecycle.graph_status(repository, root=private)
    assert status.usable and status.manifest.completeness == lifecycle.COMPLETE
    readiness = query.search_readiness(state, status)
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = command.main(['status', '--repo-path', str(repository),
                             '--state-dir', str(private), '--json'])
    report = json.loads(stream.getvalue())
    connected = connection.status(store, 'synthetic-graph', root=private)
    with store.locked('synthetic-graph') as locked:
        envelope = connection.authorize_locked(locked, 'synthetic-graph', root=private)
    context = query.graph_context(repository, root=private, question='impact',
        target='synthetic_target', envelope=envelope, policy=policy,
        context_repository='public/example', work_item='SYNTHETIC-1')
    return state, status, readiness, code, report, connected, context

checks = []
value = document()
value['nodes'].append(node('n-doc', 'synthetic_doc_content', 1, file_type='doc_ref'))
value['edges'] += [edge('n-target', 'n-doc', 'references'), edge('n-doc', 'n-caller', 'references')]
publish(value)
state, status, readiness, code, report, connected, context = observe()
graph = query.read_graph(state, status)
assert 'n-doc' not in graph.nodes and not graph.incomplete
assert all('n-doc' not in (item.source, item.target) for item in graph.edges)
assert code == 0 and report['search'] == connected['authorization'] == 'available'
assert readiness['search'] == 'available' and readiness['reader'] == 'compatible'
assert readiness['installed_code_mower'] == code_mower.__version__
assert context.status == query.AVAILABLE and context.packet['documents']
assert context.summary['generation_completeness'] == lifecycle.COMPLETE
assert context.summary['query_completeness'] == lifecycle.COMPLETE
assert 'synthetic_doc_content' not in json.dumps(context.packet)
checks.append('graphify_doc_ref_excluded_reader_available')

value = document()
value['edges'][0]['confidence'] = 'AMBIGUOUS'
publish(value)
_, _, readiness, code, report, connected, context = observe()
assert code == 0 and readiness['search'] == report['search'] == connected['authorization'] == 'available'
assert context.status == query.AVAILABLE and context.dependent_work == 'usable'
assert context.summary['generation_completeness'] == lifecycle.COMPLETE
assert context.summary['query_completeness'] == context.summary['completeness'] == 'partial'
assert context.packet['completeness'] == 'partial' and not context.packet['truncated']
assert context.packet['omissions'] == context.summary['omissions'] == ['unresolved_entities']
assert context.packet['documents']
checks.append('graphify_ambiguity_only_partial_usable_complete_generation')

value = document()
value['nodes'].append(node('n-unknown', 'synthetic_unknown_content', 1,
    file_type='synthetic_unknown_type', source_file='synthetic_unknown_path.py'))
publish(value, distribution='other-provider')
_, _, readiness, code, report, connected, context = observe()
assert code == 1 and report['state'] == 'current' and report['usable']
assert report['search'] == connected['search'] == connected['authorization'] == 'unavailable'
assert context.status == query.REQUIRED_UNAVAILABLE and context.dependent_work == 'paused'
assert context.packet is None and context.summary['reason'] == 'reader_incompatible'
for verdict in (readiness, report['query_reader'], connected['query_reader']):
    assert verdict['search'] == 'unavailable' and verdict['reader'] == 'incompatible'
    assert verdict['reason'] == 'reader_incompatible'
    assert verdict['remediation'] == context.summary['remediation']
    assert verdict['remediation']['generation_provider'] == 'other-provider==0.9.58'
    assert verdict['remediation']['reader_providers'] == ['graphifyy==0.9.58']
    assert 'node_type' not in verdict['remediation']
    assert 'required_code_mower' not in verdict['remediation']
    assert verdict['next_action'] == context.summary['next_action']
public = json.dumps([readiness, report, connected, context.summary])
assert len(public) < 12000
for forbidden in ('synthetic_unknown_type', 'synthetic_unknown_path', 'synthetic_unknown_content',
                  'n-unknown', 'synthetic_target', 'example.py', str(repository), str(private)):
    assert forbidden not in public
checks.append('graphify_wrong_distribution_reader_incompatible_no_leakage')
print(json.dumps(checks))
"""


def installed_code(code, git_repository=None):
    """Deny network/children, except transport-disabled Git reads of the fixture."""
    return "git_repository = " + repr(str(git_repository) if git_repository else None) + "\n" + """
import sys
def guard(event, args):
    if event == 'subprocess.Popen' and git_repository is not None:
        executable, argv, cwd, environment = args
        prefix = ['git', '-C', git_repository, '--no-optional-locks']
        if executable == 'git' and isinstance(argv, list) and argv[:4] == prefix:
            tail = argv[4:]
            safety = ['-c', 'protocol.allow=never', '-c', 'core.fsmonitor=false',
                      '-c', 'fetch.recurseSubmodules=no', '-c', 'uploadpack.allowFilter=false']
            if tail[:len(safety)] == safety:
                tail = tail[len(safety):]
            if (tail and tail[0] in ('rev-parse', 'ls-tree', 'cat-file', 'config')
                    and (tail[0] != 'config' or tail[1:4] == ['--local', '--name-only', '--get-regexp'])
                    and environment.get('GIT_ALLOW_PROTOCOL') == ''
                    and environment.get('GIT_NO_LAZY_FETCH') == '1'
                    and environment.get('GIT_CONFIG_GLOBAL') == __import__('os').devnull
                    and environment.get('GIT_CONFIG_NOSYSTEM') == '1'):
                return
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise RuntimeError('offline rehearsal forbids network and child processes')
sys.addaudithook(guard)
""" + code


def rehearse(dist, sha, work):
    manifest = verify(dist, sha)
    work.mkdir(parents=True, exist_ok=False)
    # Only infrastructure variables enter the disposable product processes.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    env.update(HOME=str(work / "home"), PIP_CONFIG_FILE=os.devnull, PYTHONNOUSERSITE="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1")
    (work / "home").mkdir()
    # Installer self-check/cache files are infrastructure state, and must not
    # contaminate the empty home used to prove product startup has no effects.
    (work / "installer-home").mkdir()
    installer_env = {**env, "HOME": str(work / "installer-home")}

    def command(*args, expected=0, environment=None):
        result = subprocess.run([str(a) for a in args], cwd=work, env=environment or env,
                                text=True, capture_output=True, timeout=240)
        if result.returncode != expected:
            # Report only isolated-program line numbers, never captured output
            # that might contain graph content or product state.
            lines = re.findall(r'File "<string>", line (\d+)', result.stderr)
            raise RuntimeError(f"rehearsal command failed (expected {expected}, got {result.returncode}): {args[0]}; isolated lines={lines}")
        return result.stdout

    def pip(python, *args):
        return command(python, "-m", "pip", "--isolated", *args, environment=installer_env)

    def installed(python, code, *args, expected=0, git_repository=None):
        return command(python, "-I", "-c", installed_code(code, git_repository), *args, expected=expected)

    def cli(python, *args, expected=0):
        return installed(python, "from code_mower.cli import main\nraise SystemExit(main(sys.argv[1:]))", *args, expected=expected)

    wheel = dist / NAMES[0]
    checks = []
    fresh = work / "fresh"
    command(sys.executable, "-m", "venv", fresh, environment=installer_env)
    py = fresh / "bin/python"
    pip(py, "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple/", wheel)
    pip(py, "check")
    assert cli(py, "--version").strip() == "code-mower 1.5.0"
    installed(py, """
import importlib.util
import code_mower
from pathlib import Path
assert Path(code_mower.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert all(importlib.util.find_spec(n) is None for n in ('slack_sdk', 'slack_bolt', 'mcp', 'keyring', 'graphify'))
""")
    preview = cli(py, "init", "--easy")
    assert "slack" not in preview.lower()
    assert list((work / "home").iterdir()) == []
    checks.append("fresh_default_slack_free_no_network_or_service")
    output = work / "slack-app.json"
    cli(py, "slack", "setup", "--manifest", output, "--yes")
    assert output.stat().st_mode & 0o777 == 0o600
    hosted_manifest = json.loads(output.read_text())
    assert hosted_manifest["oauth_config"]["scopes"]["bot"] == ["commands"]
    cli(py, "slack", "setup", "--manifest", output, "--yes", expected=1)
    checks.append("explicit_slack_setup_exclusive_private_manifest")
    report = json.loads(cli(py, "slack", "doctor", "--json", expected=1))
    assert report["ready"] is False and report["dispatch_authorized"] is False
    # Construct a synthetic all-green observation using the installed contract.
    snapshot = work / "offline.json"
    installed(py, """
import json, time
from pathlib import Path
from code_mower import slack_readiness as r
now = time.time()
value = {'schema': r.PROBE_SCHEMA, 'nonce': '0'*64, 'observed_at': now, 'expires_at': now+100,
    'components': {k:v[0] for k,v in r.COMPONENTS.items()},
    'supervisor_product': 'codex', 'supervisor_contract': r.SUPERVISOR_SCHEMA,
    'caps': {'task_acu':1,'campaign_acu':2,'reserved_acu':0,'task_limit':2,'reserved_tasks':0,
    'runtime_calls':4,'runtime_seconds':120,'review_rounds':1,'review_budget_usd':2,
    'clarification_answers':1,'fix_requests':0,'recovery_creates':0}}
Path(sys.argv[1]).write_text(json.dumps(value))
""", snapshot)
    report = json.loads(cli(py, "slack", "doctor", "--snapshot", snapshot, "--json", expected=1))
    assert report["basis"] == "offline" and not report["ready"] and not report["dispatch_authorized"]
    checks.append("all_green_offline_snapshot_cannot_claim_live_readiness")
    # Simulate disabled control-plane observations only; no live admin mutation.
    observation = json.loads(snapshot.read_text())
    observation["components"].update(ingress="disabled", bridge="disabled", installation="disabled")
    snapshot.write_text(json.dumps(observation))
    report = json.loads(cli(py, "slack", "doctor", "--snapshot", snapshot, "--json", expected=1))
    assert not report["ready"] and not report["dispatch_authorized"]
    output.unlink()  # The only local opt-in artifact; there is no Slack service.
    checks.append("offline_disabled_snapshot_and_local_manifest_removal")

    # This fixture is public synthetic source, with no remote, hooks, provider
    # executable or Graphify install. The wheel's real lifecycle seals it; only
    # the extractor is replaced by a deterministic synthetic document writer.
    repository = work / "synthetic-repository"
    command("git", "init", "--template=", "-q", "-b", "main", repository)
    (repository / "example.py").write_text("def synthetic_target(): pass\ndef synthetic_caller(): synthetic_target()\n")
    command("git", "-C", repository, "add", "example.py")
    command("git", "-C", repository, "-c", "core.hooksPath=" + os.devnull,
            "-c", "user.name=Rehearsal", "-c", "user.email=rehearsal@example.invalid",
            "commit", "--no-gpg-sign", "-q", "-m", "Synthetic public graph fixture")
    graph_checks = json.loads(installed(py, GRAPHIFY_SMOKE, repository, work / "graph-state",
                                       git_repository=repository))
    assert graph_checks == list(GRAPHIFY_CHECKS)
    checks.extend(graph_checks)

    # Preserve synthetic operator evidence outside site-packages, byte for byte.
    state = work / "operator-state"
    state.mkdir()
    for name, content in {"config.json": '{"enabled":false}\n',
                          "reservation.json": '{"reserved_acu":2,"refunded":false}\n',
                          "receipt.json": '{"synthetic":true,"reconciled":true}\n'}.items():
        (state / name).write_text(content)

    def state_hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.iterdir()}

    before = state_hashes()
    upgrade = work / "upgrade"
    command(sys.executable, "-m", "venv", upgrade, environment=installer_env)
    py = upgrade / "bin/python"
    old = work / "rollback-artifact"
    old.mkdir()
    pip(py, "download", "--no-cache-dir", "--index-url", "https://pypi.org/simple/",
        "--only-binary=:all:", "--no-deps", "--dest", old, "code-mower==1.4.2")
    old_wheel, = old.glob("code_mower-1.4.2-*.whl")
    old_digest = hashlib.sha256(old_wheel.read_bytes()).hexdigest()
    assert old_digest == "f8bf24dd8a982ed5ab28302e837cd5d2aeece6d984ed1c44fcb4688c3fb7a522"
    pip(py, "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple/", old_wheel)
    assert cli(py, "--version").strip() == "code-mower 1.4.2"
    pip(py, "install", "--no-index", "--no-deps", "--upgrade", wheel)
    assert cli(py, "--version").strip() == "code-mower 1.5.0"
    assert before == state_hashes()
    checks.append("upgrade_1_4_2_to_exact_wheel_preserves_synthetic_state")
    pip(py, "install", "--no-index", "--no-deps", "--force-reinstall", old_wheel)
    assert cli(py, "--version").strip() == "code-mower 1.4.2"
    assert before == state_hashes()
    checks.append("disposable_rollback_to_digest_verified_1_4_2_preserves_state")
    pip(py, "uninstall", "--yes", "code-mower")
    installed(py, "import importlib.util\nassert importlib.util.find_spec('code_mower') is None")
    assert before == state_hashes()
    pip(fresh / "bin/python", "uninstall", "--yes", "code-mower")
    installed(fresh / "bin/python", "import importlib.util\nassert importlib.util.find_spec('code_mower') is None")
    assert before == state_hashes()
    checks.append("uninstall_preserves_synthetic_state")
    result = {"schema": REHEARSAL_SCHEMA, "source_sha": sha,
              "artifact_sha256": manifest["artifacts"][NAMES[0]], "checks": checks,
              "rollback_wheel_sha256": old_digest, "synthetic_state_preserved": True,
              "live_slack_readiness": "not_run", "live_disable_uninstall": "not_run",
              "paid_canaries": "not_run", "status": "pass"}
    (work / "rehearsal.json").write_text(json.dumps(result, indent=2) + "\n")
    verify_rehearsal(work, manifest)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rehearse(args.dist.resolve(), args.source_sha, args.work_dir.resolve()), sort_keys=True))


if __name__ == "__main__":
    main()
