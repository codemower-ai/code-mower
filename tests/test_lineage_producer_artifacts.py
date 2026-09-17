"""Execute staged artifacts against an isolated locally built/installed package."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import yaml
from code_mower import init, package
from code_mower.config import load_config
from lineage_producer_fixtures import ACCEPTED_BASELINE, AUTHORITY, TRANSPORT, comments, episode, target
from lineage_consumer_fixtures import policy, policy_text
from code_mower.audit_labeler_lib import lineage_identity

ROOT = Path(__file__).resolve().parents[1]
BASE = "e818a3b639dfe903bdc16aff3674af98a5a08233"
CORE_HASH = "2842d106bc7c3ecb95007b7de6ff3447d8ea3a49e8995e422ff279463ae5653c"
ASSETS = ("workflows/builder-lineage-producer.yml.j2", "lanes/lineage-producer.sh")


class ArtifactTests(unittest.TestCase):
    def accepted_baseline(self):
        baseline = ACCEPTED_BASELINE
        self.assertIsInstance(baseline, dict, 'Accepted baseline must be a complete mapping')
        self.assertEqual(set(baseline), {'accepted_base', 'modules', 'unchanged_files'},
                         'Accepted baseline fields are missing or unsupported')
        self.assertEqual(baseline['accepted_base'], BASE, 'Accepted baseline commit differs')
        modules = baseline['modules']
        self.assertIsInstance(modules, dict, 'Accepted module baseline must be a mapping')
        counts = {'src/code_mower/builder_runs.py': (29, 23),
                  'src/code_mower/lane_delivery.py': (33, 54),
                  'src/code_mower/lane_handoff.py': (9, 13)}
        self.assertEqual(set(modules), set(counts), 'Accepted module inventory differs')
        for path, (definition_count, live_count) in counts.items():
            module = modules[path]
            self.assertIsInstance(module, dict, f'{path}: malformed accepted module baseline')
            self.assertEqual(set(module), {'definitions', 'live_segments'},
                             f'{path}: accepted source-segment fields differ')
            self.assertIsInstance(module['definitions'], dict, f'{path}: definitions must be a mapping')
            self.assertEqual(len(module['definitions']), definition_count,
                             f'{path}: accepted definitions are missing or extra')
            self.assertIsInstance(module['live_segments'], list, f'{path}: live segments must be an ordered list')
            self.assertEqual(len(module['live_segments']), live_count,
                             f'{path}: accepted live segments are missing or extra')
            for name in module['definitions']:
                self.assertTrue(isinstance(name, str) and name.isidentifier(),
                                f'{path}: malformed accepted definition name')
            for digest in [*module['definitions'].values(), *module['live_segments']]:
                self.assertIsInstance(digest, str, f'{path}: accepted source digest must be text')
                self.assertRegex(digest, r'^[0-9a-f]{64}\Z', f'{path}: malformed accepted source digest')
        files = baseline['unchanged_files']
        self.assertIsInstance(files, dict, 'Accepted unchanged-file baseline must be a mapping')
        self.assertEqual(len(files), 17, 'Accepted unchanged-file inventory must contain all 17 files')
        for path, digest in files.items():
            self.assertIsInstance(path, str, 'Accepted file path must be text')
            self.assertIsInstance(digest, str, f'{path}: accepted file digest must be text')
            self.assertRegex(digest, r'^[0-9a-f]{64}\Z', f'{path}: malformed accepted file digest')
        serialized = (json.dumps(baseline, indent=2, sort_keys=True) + '\n').encode('utf-8')
        # Refreshed for the #1004 / PR #1005 authorized regeneration of the Claude and
        # Codex audit labeler workflows; every other accepted digest is unchanged.
        self.assertEqual(hashlib.sha256(serialized).hexdigest(),
                         '62f88bf88658f4e936624873248ca7247474b3193900f73ed905d4e9d6f4f78a',
                         'Complete accepted baseline differs from the independently approved value')
        return baseline

    def module_source_hashes(self, path):
        source = (ROOT/path).read_bytes().decode('utf-8')
        lines = source.splitlines(keepends=True)
        definitions, live_segments = {}, []
        for node in ast.parse(source, filename=path).body:
            is_definition = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]) if is_definition else node.lineno
            segment = ''.join(lines[start - 1:node.end_lineno]).encode('utf-8')
            digest = hashlib.sha256(segment).hexdigest()
            if is_definition:
                self.assertNotIn(node.name, definitions, f'{path}: duplicate top-level definition {node.name}')
                definitions[node.name] = digest
            else:
                live_segments.append(digest)
        return definitions, live_segments

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.wheels = cls.root / "wheels"
        cls.installed = cls.root / "installed"
        supplied_wheel = os.environ.get("CODE_MOWER_QUALIFICATION_WHEEL")
        if supplied_wheel:
            wheel = Path(supplied_wheel)
            if not wheel.is_absolute() or not wheel.is_file() or wheel.suffix != ".whl":
                raise AssertionError("qualification requires an existing absolute wheel path")
        else:
            result = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(cls.wheels), str(ROOT)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            wheel, = cls.wheels.glob("*.whl")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile", "--target", str(cls.installed), str(wheel)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.bin = cls.root / "bin"
        cls.bin.mkdir()
        bootstrap = f'''
import datetime
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(cls.installed)!r})
import code_mower.builder_lineage_producer as producer
assert Path(producer.__file__).resolve().is_relative_to(Path({str(cls.installed)!r}))
assert {str(ROOT / 'src')!r} not in sys.path
class Clock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 1, tzinfo=tz)
with patch('datetime.datetime', Clock):
    exec(compile(sys.stdin.read(), '<staged-artifact>', 'exec'))
'''
        wrapper = cls.bin / "python"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -I -S -c " + shlex.quote(bootstrap) + "\n")
        wrapper.chmod(0o755)
        gh = cls.bin / "gh"
        gh_code = cls.bin / "gh_fixture.py"
        gh_code.write_text( '''
import base64, json, os, sys
from pathlib import Path
fixture = json.loads(Path(os.environ['FIXTURE']).read_text())
endpoint = sys.argv[2]
with Path(os.environ['EFFECTS']).open('a') as sink:
    sink.write(json.dumps(sys.argv[1:]) + '\\n')
assert sys.argv[1] == 'api' and len(sys.argv) == 3
mode = fixture['mode']
if '/git/trees/' in endpoint:
    raw = {'truncated': False, 'tree': [{'path': 'code-mower.yml', 'type': 'blob', 'sha': 'c' * 40}]}
elif '/git/blobs/' in endpoint:
    raw = {'encoding': 'base64', 'content': base64.b64encode(fixture['policy'].encode()).decode()}
elif '/pulls/' in endpoint:
    raw = fixture['pr']
    if mode == 'target-failed': sys.exit(1)
    if mode == 'target-null': raw = None
    if mode == 'branch-missing': del raw['head']['ref']
    if mode == 'branch-case': raw['head']['ref'] = raw['head']['ref'].lower()
    if mode == 'head-race':
        reads = Path(os.environ['EFFECTS']).read_text().count('/pulls/')
        if reads > 1: raw['head']['sha'] = 'f' * 40
elif '/labels?' in endpoint:
    raw = [{'name': 'builder:codex'}]
    if mode == 'labels-failed': sys.exit(1)
    if mode == 'labels-null': raw = None
    if mode == 'labels-malformed': raw = [{'name': False}]
else:
    assert '/comments?' in endpoint
    page = int(endpoint.rsplit('page=', 1)[1])
    raw = fixture['comments'] if page == 1 else []
    if mode == 'history-failed': sys.exit(1)
    if mode == 'history-null': raw = None
    if mode == 'history-object': raw = {}
    if mode == 'history-mixed': raw = [None, {'user': None}]
    if mode == 'history-bad-body': raw = [{'body': None}]
    if mode == 'page-cap': raw = [{}] * 100
print(json.dumps(raw))
''')
        gh.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -I -S " + shlex.quote(str(gh_code)) + " \"$@\"\n")
        gh.chmod(0o755)

    def materialized(self, relative):
        # Render through the installed package too: post-publication replay
        # must not substitute even a checkout's rendering helper.
        program = """
import sys
from pathlib import Path
sys.meta_path = [f for f in sys.meta_path if '__editable__' not in str(f)]
sys.path.insert(0, sys.argv[1])
from code_mower import init
assert Path(init.__file__).resolve().is_relative_to(Path(sys.argv[1]))
source = Path(sys.argv[1]) / 'code_mower/templates' / sys.argv[2]
print(init._render_workflow_template(source.read_text(), {}), end='')
"""
        result = subprocess.run([sys.executable, '-I', '-c', program,
                                 str(self.installed), relative], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_real_rendered_workflow_and_runner_failure_rows(self):
        workflow = yaml.safe_load(self.materialized(ASSETS[0]))
        self.assertEqual(set(workflow["permissions"].values()), {"read"})
        body = workflow["jobs"]["attribute"]["steps"][-1]["run"]
        scripts = [body, self.materialized(ASSETS[1])]
        for index, script in enumerate(scripts):
            for mode in ("success", "target-failed", "target-null", "branch-missing", "branch-case",
                         "labels-failed", "labels-null", "labels-malformed", "history-failed", "history-null",
                         "history-object", "history-mixed", "history-bad-body", "page-cap", "head-race",
                         "no-authority", "policy-denied"):
                with self.subTest(artifact=index, mode=mode):
                    case = self.root / f"case-{index}-{mode}"
                    case.mkdir()
                    fixture = case / "fixture.json"
                    observed = target()
                    roles = policy()
                    if mode == "policy-denied":
                        roles["role_policy"] = {"claude": {"builder": {"enabled": False}}}
                    fixture.write_text(json.dumps({"mode": mode, "policy": policy_text(roles), "comments": comments([episode()]),
                        "pr": {"state": "open", "number": 42, "base": {"repo": {"full_name": observed.repo}, "sha": BASE}, "labels": [{"name": "builder:codex"}],
                               "head": {"ref": observed.branch, "sha": observed.head_sha}, "user": {"login": "source-bot"}}}))
                    env = os.environ | {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                        "FIXTURE": str(fixture), "EFFECTS": str(case / "effects.jsonl"),
                        "LINEAGE_TARGET_JSON": json.dumps(observed.__dict__) if hasattr(observed, '__dict__') else json.dumps(
                            {"repo": observed.repo, "pr_number": observed.pr_number, "branch": observed.branch, "head_sha": observed.head_sha}),
                        "LINEAGE_POLICY_JSON": json.dumps({"base_sha": BASE, "identity": lineage_identity(roles).to_mapping(), "roles": roles}),
                        "LINEAGE_AUTHORITY_JSON": json.dumps([] if mode == "no-authority" else sorted(AUTHORITY.accounts)),
                        "LINEAGE_TRANSPORT_JSON": json.dumps(TRANSPORT.__dict__), "LINEAGE_OUTPUT": str(case/"event.json")}
                    result = subprocess.run(["bash", "-c", script], cwd=case, env=env,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode == 0, mode == "success", result.stderr)
                    self.assertEqual((case/"event.json").exists(), mode == "success")
                    if mode == "success":
                        event = json.loads((case/"event.json").read_text())
                        self.assertEqual(event["dimensions"]["lineage"]["current_writer"], "claude")
                        self.assertEqual(event["tool"]["executor"], "claude_cli")
                        self.assertEqual(event["created_at"], "2026-01-01T00:00:00+00:00")
                    if mode == "page-cap":
                        effects = [json.loads(line) for line in (case/"effects.jsonl").read_text().splitlines()]
                        queries = [parse_qs(urlsplit(args[1]).query) for args in effects
                                   if args[0] == 'api' and urlsplit(args[1]).path.endswith('/comments')]
                        requested_pages = [query['page'] for query in queries]
                        self.assertEqual(requested_pages, [[str(page)] for page in range(1, 10)])
                        self.assertNotIn(['10'], requested_pages)

    def test_manifest_mirror_core_and_existing_definition_parity(self):
        for asset in ASSETS:
            self.assertEqual((ROOT/"templates"/asset).read_bytes(), (ROOT/"src/code_mower/templates"/asset).read_bytes())
            self.assertEqual((ROOT/"templates"/asset).read_bytes(), (self.installed/"code_mower/templates"/asset).read_bytes())
        for path in ("src/code_mower/builder_lineage.py", "tools/builder_lineage.py"):
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(), CORE_HASH)
        for path, baseline in self.accepted_baseline()['modules'].items():
            definitions, live_segments = self.module_source_hashes(path)
            self.assertFalse(set(baseline['definitions']) - set(definitions),
                             f'{path}: missing accepted definitions: {sorted(set(baseline["definitions"]) - set(definitions))}')
            activated = {
                'src/code_mower/builder_runs.py': {'main'},
                'src/code_mower/lane_delivery.py': {'validate_handoff', 'main', '_add_handoff_parser', '_add_supervise_parser', '_handoff_main', '_supervise_main'},
                'src/code_mower/lane_handoff.py': {'lineage_handoff'},
            }
            for name, digest in baseline['definitions'].items():
                if name not in activated[path]:
                    self.assertEqual(definitions[name], digest, f'{path}: frozen definition changed: {name}')
            # Stage 3 activates main after the imported producer definitions.
            tree = ast.parse((ROOT/path).read_text())
            if path != 'src/code_mower/lane_handoff.py':
                self.assertIsInstance(tree.body[-1], ast.If)

        expected = package.committed_package_manifest_text(package.generate_committed_package_manifest(ROOT))
        self.assertEqual(expected, (ROOT/"code-mower-package-manifest.json").read_text())

    def test_default_init_emits_activation_with_standalone_pure_helper(self):
        config = load_config(ROOT / "src/code_mower/templates/code-mower.example.yml")
        plan = init.render_init_plan(config, package_mode=True, repo_root=ROOT)
        output = self.root / "default-init"
        init.apply_init_plan(plan, output, source_root=ROOT)
        all_paths = [p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()]
        self.assertIn('.github/workflows/builder-lineage-producer.yml', all_paths)
        self.assertIn('tools/lanes/lineage-producer.sh', all_paths)
        # Existing live init/runner/workflow inputs are byte-identical to accepted base.
        paths = ['src/code_mower/init.py', 'tools/lanes/run_mac_lane.sh',
                 'templates/lanes/run_mac_lane.sh', 'src/code_mower/templates/lanes/run_mac_lane.sh']
        paths.extend(p.relative_to(ROOT).as_posix() for p in (ROOT/'.github/workflows').glob('*'))
        baseline = self.accepted_baseline()['unchanged_files']
        self.assertCountEqual(paths, baseline, 'Actual init/runner/workflow inventory differs from accepted baseline')
        activated = {'src/code_mower/init.py', 'tools/lanes/run_mac_lane.sh',
            'templates/lanes/run_mac_lane.sh', 'src/code_mower/templates/lanes/run_mac_lane.sh',
            '.github/workflows/code-mower-gate.yml'}
        for path in paths:
            if path not in activated:
                content = (ROOT/path).read_bytes()
                if path == '.github/workflows/release.yml':
                    # #915 changes only the release tag binding; all other
                    # accepted workflow bytes remain frozen by this comparison.
                    content = content.replace(b'refs/tags/v1.4.1', b'refs/tags/v1.4.0')
                self.assertEqual(hashlib.sha256(content).hexdigest(), baseline[path],
                                 f'{path}: frozen accepted file bytes differ')
        # Normal init emits the pure tools helper, not the package delivery modules.
        helper = output/'tools/builder_lineage.py'
        self.assertTrue(helper.is_file())
        program = '''
import importlib.util
import sys
from pathlib import Path
assert importlib.util.find_spec('code_mower') is None
emitted = Path.cwd()/'tools/builder_lineage.py'
sys.path.insert(0, str(emitted.parent))
import builder_lineage
assert Path(builder_lineage.__file__).resolve() == emitted.resolve()
assert 'builder_lineage_producer' not in sys.modules
assert 'code_mower.builder_lineage_producer' not in sys.modules
assert 'code_mower' not in sys.modules
assert importlib.util.find_spec('code_mower') is None
assert importlib.util.find_spec('builder_lineage_producer') is None
assert not (emitted.parent/'builder_lineage_producer.py').exists()
assert not (Path.cwd()/'src/code_mower/builder_lineage_producer.py').exists()
'''
        result = subprocess.run([sys.executable, '-I', '-S', '-c', program], cwd=output,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_published_140_refuses_emitted_artifact_before_any_effect(self):
        """#915 closeout must replace this refusal with installed published 1.4.1
        takeover/continuation/third-writer/readback plus consumer admission evidence.
        Source merge alone does not qualify installed release activation.
        """
        legacy = self.root/'legacy-140'
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-compile',
            '--target', str(legacy), 'code-mower==1.4.0'], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        import importlib.metadata
        distributions = list(importlib.metadata.distributions(path=[str(legacy)]))
        self.assertEqual([(d.metadata['Name'], d.version) for d in distributions], [('code-mower', '1.4.0')])
        legacy_bin = self.root/'legacy-bin'
        legacy_bin.mkdir()
        bootstrap = f'''
import sys
from pathlib import Path
sys.path.insert(0, {str(legacy)!r})
import code_mower
assert Path(code_mower.__file__).resolve().is_relative_to(Path({str(legacy)!r}))
assert {str(ROOT/'src')!r} not in sys.path
exec(compile(sys.stdin.read(), '<emitted-installed-artifact>', 'exec'))
'''
        wrapper = legacy_bin/'python'
        wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -I -S -c '+shlex.quote(bootstrap)+'\n')
        wrapper.chmod(0o755)
        effects = self.root/'legacy-effects'
        gh = legacy_bin/'gh'
        gh.write_text('#!/bin/sh\nprintf effect >> '+shlex.quote(str(effects))+'\nexit 99\n')
        gh.chmod(0o755)
        output = self.root/'legacy-event.json'
        workflow = yaml.safe_load(self.materialized(ASSETS[0]))
        for script in (workflow['jobs']['attribute']['steps'][-1]['run'], self.materialized(ASSETS[1])):
            result = subprocess.run(['bash', '-c', script], cwd=self.root,
                env=os.environ | {'PATH': str(legacy_bin)+os.pathsep+os.environ['PATH'], 'LINEAGE_OUTPUT': str(output)},
                capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unsupported installed lineage capability', result.stderr.lower())
            self.assertFalse(effects.exists())
            self.assertFalse(output.exists())
        lane_wrapper = legacy_bin/'lane-delivery'
        launcher = f"import sys; sys.path.insert(0, {str(legacy)!r}); from code_mower.lane_delivery import main; raise SystemExit(main())"
        lane_wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -I -c '+shlex.quote(launcher)+' "$@"\n')
        lane_wrapper.chmod(0o755)
        from lineage_consumer_fixtures import fixture_shell_env
        runner_env = os.environ | fixture_shell_env(legacy_bin) | {
            'PATH': str(legacy_bin)+os.pathsep+os.environ['PATH'],
            'CODE_MOWER_LANE_DELIVERY_CMD': str(lane_wrapper)}
        result = subprocess.run(['bash', str(ROOT/'tools/lanes/run_mac_lane.sh'),
            '--lane', 'codex', '--repo', 'owner/repo', '--target', 'pr:42', '--max-minutes', '1'],
            cwd=self.root, env=runner_env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsupported installed lineage capability', result.stderr.lower())
        self.assertFalse(effects.exists())

    def test_installed_candidate_supervisor_and_public_readback_business(self):
        program = f'''
import sys, unittest
from pathlib import Path
sys.path = [p for p in sys.path if p != {str(ROOT/'src')!r}]
sys.meta_path = [finder for finder in sys.meta_path if '__editable__' not in str(finder)]
sys.path.insert(0, {str(self.installed)!r})
sys.path.insert(1, {str(ROOT/'tests')!r})
import code_mower.lane_delivery as delivery
assert Path(delivery.__file__).resolve().is_relative_to(Path({str(self.installed)!r}))
from test_lineage_consumer_activation import ProducerActivation
suite = unittest.TestSuite(ProducerActivation(name) for name in (
 'test_actual_entrypoint_takeover_continuation_then_third_writer',
 'test_public_readback_failure_keeps_post_but_no_labels_or_attribution'))
result = unittest.TextTestRunner().run(suite)
raise SystemExit(not result.wasSuccessful())
'''
        result = subprocess.run([sys.executable, '-I', '-c', program], cwd=self.root,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
