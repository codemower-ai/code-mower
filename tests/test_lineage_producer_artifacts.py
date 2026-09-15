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
from lineage_producer_fixtures import AUTHORITY, POLICY, TRANSPORT, comments, episode, target

ROOT = Path(__file__).resolve().parents[1]
BASE = "e818a3b639dfe903bdc16aff3674af98a5a08233"
CORE_HASH = "2842d106bc7c3ecb95007b7de6ff3447d8ea3a49e8995e422ff279463ae5653c"
ASSETS = ("workflows/builder-lineage-producer.yml.j2", "lanes/lineage-producer.sh")


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.wheels = cls.root / "wheels"
        cls.installed = cls.root / "installed"
        result = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(cls.wheels)],
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
import json, os, sys
from pathlib import Path
fixture = json.loads(Path(os.environ['FIXTURE']).read_text())
endpoint = sys.argv[2]
with Path(os.environ['EFFECTS']).open('a') as sink:
    sink.write(json.dumps(sys.argv[1:]) + '\\n')
assert sys.argv[1] == 'api' and len(sys.argv) == 3
mode = fixture['mode']
if '/pulls/' in endpoint:
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
        source = self.installed / "code_mower/templates" / relative
        entry = {"source": "explicit-staged-asset", "copy_from_path": str(source)}
        result = init._materialize_generated_file(entry, relative, self.root / "rendered",
                                                  source_root=self.root)
        self.assertFalse(result.placeholder)
        return init._render_workflow_template(result.text, {})

    def test_real_rendered_workflow_and_runner_failure_rows(self):
        workflow = yaml.safe_load(self.materialized(ASSETS[0]))
        self.assertEqual(set(workflow["permissions"].values()), {"read"})
        body = workflow["jobs"]["attribute"]["steps"][0]["run"]
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
                    fixture.write_text(json.dumps({"mode": mode, "comments": comments([episode()]),
                        "pr": {"state": "open", "number": 42, "base": {"repo": {"full_name": observed.repo}},
                               "head": {"ref": observed.branch, "sha": observed.head_sha}, "user": {"login": "source-bot"}}}))
                    roles = {"role_policy": {"claude": {"builder": {"enabled": False}}}} if mode == "policy-denied" else {}
                    env = os.environ | {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                        "FIXTURE": str(fixture), "EFFECTS": str(case / "effects.jsonl"),
                        "LINEAGE_TARGET_JSON": json.dumps(observed.__dict__) if hasattr(observed, '__dict__') else json.dumps(
                            {"repo": observed.repo, "pr_number": observed.pr_number, "branch": observed.branch, "head_sha": observed.head_sha}),
                        "LINEAGE_POLICY_JSON": json.dumps({"base_sha": BASE, "identity": POLICY.to_mapping(), "roles": roles}),
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
        for path in ("src/code_mower/lane_delivery.py", "src/code_mower/lane_handoff.py", "src/code_mower/builder_runs.py"):
            old = subprocess.check_output(["git", "show", BASE+":"+path], cwd=ROOT, text=True)
            before, after = ast.parse(old), ast.parse((ROOT/path).read_text())
            old_nodes = {node.name: ast.dump(node) for node in before.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
            new_nodes = {node.name: ast.dump(node) for node in after.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
            self.assertEqual(old_nodes, {key: new_nodes[key] for key in old_nodes})
            old_live = [ast.dump(n) for n in before.body if not isinstance(n, (ast.ClassDef, ast.FunctionDef))]
            new_live = [ast.dump(n) for n in after.body if not isinstance(n, (ast.ClassDef, ast.FunctionDef))]
            self.assertEqual(old_live, new_live)
        expected = package.committed_package_manifest_text(package.generate_committed_package_manifest(ROOT))
        self.assertEqual(expected, (ROOT/"code-mower-package-manifest.json").read_text())

    def test_default_init_emitted_helper_isolated_without_producer(self):
        config = load_config(ROOT / "src/code_mower/templates/code-mower.example.yml")
        plan = init.render_init_plan(config, package_mode=True, repo_root=ROOT)
        output = self.root / "default-init"
        init.apply_init_plan(plan, output, source_root=ROOT)
        all_paths = [p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()]
        self.assertFalse(any('lineage-producer' in p or 'builder_lineage_producer' in p for p in all_paths))
        # Existing live init/runner/workflow inputs are byte-identical to accepted base.
        paths = ['src/code_mower/init.py', 'tools/lanes/run_mac_lane.sh',
                 'templates/lanes/run_mac_lane.sh', 'src/code_mower/templates/lanes/run_mac_lane.sh']
        paths.extend(p.relative_to(ROOT).as_posix() for p in (ROOT/'.github/workflows').glob('*'))
        for path in paths:
            self.assertEqual(subprocess.check_output(['git', 'show', BASE+':'+path], cwd=ROOT), (ROOT/path).read_bytes())
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
