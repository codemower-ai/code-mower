"""Opt-in guidance and literal installed-package adoption regressions for #915."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import tomllib
from unittest import mock

import yaml

from code_mower import cli, init, package, release_readiness
from code_mower.config import load_config
from code_mower.context_graph_lifecycle import _refuse_broad_exposure

ROOT = Path(__file__).resolve().parents[1]


class GraphifyGuidanceTests(unittest.TestCase):
    def test_documented_acquisition_neutralizes_ambient_pip_sources(self):
        doc = (ROOT / "docs/graphify-setup.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
        acquisition = next(block for block in blocks if '-m venv' in block)
        commands = [
            line for line in acquisition.replace("\\\n", "").splitlines()
            if " -m pip " in line
        ]
        self.assertEqual(len(commands), 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = root / "operator owned" / "venv"
            wheels = root / "operator owned" / "wheels"
            (environment / "bin").mkdir(parents=True)
            wheels.mkdir()
            wheel = wheels / "graphifyy-0.9.58-py3-none-any.whl"
            wheel.touch()
            # Record only the pip command boundary: never invoke pip or Graphify.
            recorder = root / "record.py"
            recorder.write_text(
                "import json, os, sys\n"
                "print(json.dumps({'args': sys.argv[1:], 'env': "
                "{k: v for k, v in os.environ.items() if k.startswith('PIP_')}}))\n"
            )
            interpreter = environment / "bin" / "python"
            interpreter.write_text(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I '
                f'{shlex.quote(str(recorder))} "$@"\n'
            )
            interpreter.chmod(0o755)
            ambient = {
                "PATH": os.defpath,
                "GRAPHIFY_ENV": str(environment),
                "GRAPHIFY_WHEELS": str(wheels),
                "PIP_INDEX_URL": "https://ambient.invalid/simple/",
                "PIP_EXTRA_INDEX_URL": "https://extra.invalid/simple/",
                "PIP_FIND_LINKS": str(root / "ambient wheels"),
                "PIP_NO_INDEX": "1",
                "PIP_CONFIG_FILE": str(root / "ambient-pip.conf"),
            }
            expected = (
                ["download", "--no-cache-dir", "--index-url", "https://pypi.org/simple/",
                 "--only-binary=:all:", "--no-deps", "--dest", str(wheels), "graphifyy==0.9.58"],
                ["install", "--no-cache-dir", "--index-url", "https://pypi.org/simple/", str(wheel)],
            )
            for command, arguments in zip(commands, expected, strict=True):
                with self.subTest(operation=arguments[0]):
                    result = subprocess.run(
                        ["bash", "-c", command], cwd=root, env=ambient,
                        capture_output=True, text=True, check=True,
                    )
                    recorded = json.loads(result.stdout)
                    self.assertEqual(recorded["env"], {"PIP_CONFIG_FILE": os.devnull})
                    self.assertEqual(recorded["args"], ["-m", "pip", "--isolated", *arguments])

    def test_documented_provider_paths_stay_outside_selected_checkout(self):
        doc = (ROOT / "docs/graphify-setup.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
        paths = next(block for block in blocks if 'GRAPHIFY_ROOT=' in block)
        acquisition = next(block for block in blocks if '-m venv' in block)
        build = next(block for block in blocks if 'context-graph build' in block)
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "checkout"
            repository.mkdir()
            provider_root = Path(tmp) / "operator owned" / "graphify-0.9.58"
            script = paths.replace('/absolute/operator-owned/graphify-0.9.58', str(provider_root))
            script += '\nprintf "%s\\n" "$GRAPHIFY_ROOT" "$GRAPHIFY_ENV" "$GRAPHIFY_WHEELS" "$GRAPHIFY_INDEXER"\n'
            result = subprocess.run(
                ["bash", "-c", script], cwd=repository,
                capture_output=True, text=True, check=True,
            )
            root, environment, wheels, indexer = map(Path, result.stdout.splitlines())
            self.assertEqual(root, provider_root)
            self.assertEqual(environment, root / "venv")
            self.assertEqual(wheels, root / "wheels")
            self.assertEqual(indexer, environment / "bin" / "graphify")
            for path in (root, environment, wheels, indexer):
                self.assertTrue(path.is_absolute())
                _refuse_broad_exposure(path, repository=repository)
            self.assertEqual(list(repository.iterdir()), [])
        self.assertIn('python3.12 -m venv "$GRAPHIFY_ENV"', acquisition)
        self.assertIn('--dest "$GRAPHIFY_WHEELS"', acquisition)
        self.assertIn('"$GRAPHIFY_ENV/bin/python" - "$GRAPHIFY_WHEELS"', acquisition)
        self.assertIn('wheel, = Path(sys.argv[1]).glob', acquisition)
        self.assertLess(acquisition.index('digest mismatch'), acquisition.index('pip --isolated install'))
        self.assertNotIn('context-graph', acquisition)
        self.assertNotIn('-m pip', build)
        self.assertIn('--indexer "$GRAPHIFY_INDEXER"', build)
        self.assertIn('--pin-file "$GRAPHIFY_ROOT/pin.json"', build)
        self.assertIn('--revision "$GRAPHIFY_REVISION"', build)

    def test_opt_in_changes_only_guidance(self):
        config = load_config(package.packaged_starter_config_path())
        baseline = init.render_init_plan(config).data
        selected = init.render_init_plan(config, graphify=True).data
        guidance = selected.pop("optional_integrations")["graphify"]
        self.assertEqual(selected, baseline)
        self.assertEqual(guidance["mode"], "guidance_only")
        self.assertEqual(guidance["package_spec"], "graphifyy==0.9.58")
        self.assertNotIn("graphify", json.dumps(baseline).lower())
        self.assertNotIn("graphify", json.dumps(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"].get("dependencies", [])).lower())

    def test_fresh_guidance_preview_never_launches_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), mock.patch(
                    "subprocess.run", side_effect=AssertionError("preview launched a process")
                ):
                    self.assertEqual(cli.main(["init", "--graphify", "--json"]), 0, err.getvalue())
                data = json.loads(out.getvalue())
                self.assertEqual(data["config_source"]["kind"], "packaged_starter")
                self.assertEqual(list(Path(tmp).iterdir()), [])
                self.assertIn("optional_integrations", data)
            finally:
                os.chdir(previous)

    def test_versioned_gates_bind_requested_identity(self):
        for version, tag in (("1.4.1", "v1.4.1"), ("1.5.0", "v1.5.0")):
            assertions = release_readiness._post_merge_runbook_assertions(version, tag)
            self.assertIn(f'test "$(git rev-list -n 1 {tag})" = "$RELEASE_SHA"', assertions)
            self.assertIn(f'test -s "$RELEASE_CHECKOUT/docs/{tag.replace(".", "")}-release-notes.md"', assertions)
            self.assertNotIn("v1.4.0", "\n".join(assertions))


class LegacyProvenanceGuidanceTests(unittest.TestCase):
    def test_pr_workflow_emits_only_skipped_guidance_without_trusted_inputs(self):
        template = (ROOT / "templates/workflows/builder-provenance.yml.j2").read_text()
        workflow = yaml.safe_load(template)
        self.assertEqual(workflow[True], {"pull_request": {
            "types": ["opened", "edited", "synchronize", "reopened", "ready_for_review"],
        }})
        steps = workflow["jobs"]["lineage-guidance"]["steps"]
        self.assertEqual(len(steps), 2)
        record, upload = steps
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No package install, PR payload, credential or lineage environment.
            result = subprocess.run(
                ["bash", "-c", record["run"]], cwd=root, env={"PATH": os.defpath},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = root / upload["with"]["path"]
            payload = json.loads(artifact.read_text())
            self.assertEqual(payload["mode"], "builder-provenance-guidance")
            self.assertEqual(payload["status"], "skipped")
            self.assertIn("builder-lineage-producer.yml.j2", payload["next_step"])
            self.assertEqual([p for p in root.rglob('*') if p.is_file()], [artifact])
            self.assertNotIn("event_type", payload)
        self.assertEqual(upload["with"]["name"], "code-mower-builder-provenance-skipped")
        self.assertIs(upload["with"]["include-hidden-files"], True)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")


class InstalledPromptPackTests(unittest.TestCase):
    def test_literal_starter_and_explicit_config_walkthrough(self):
        """Exercise installed code, with no provider login or network doctor probes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supplied = os.environ.get("CODE_MOWER_QUALIFICATION_WHEEL")
            if supplied:
                wheel = Path(supplied)
                self.assertTrue(wheel.is_absolute() and wheel.is_file())
            else:
                # This historical v1.4.1 walkthrough only qualifies a supplied
                # v1.4.1 artifact (see docs/v141-qualification.md). The
                # checkout has since moved past 1.4.1, so building it here
                # would install and assert against whatever version main
                # currently carries, not v1.4.1; skip rather than misreport.
                self.skipTest(
                    "no CODE_MOWER_QUALIFICATION_WHEEL supplied; this historical "
                    "v1.4.1 walkthrough does not build the current checkout"
                )
            installed = root / "installed"
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
                 "--target", str(installed), str(wheel)],
                cwd=root, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Only the harness comes from this file. Product imports resolve to
            # the downloaded/built wheel, never to checkout modules.
            program = r'''
import io, json, os, shutil, sys
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
sys.meta_path = [f for f in sys.meta_path if '__editable__' not in str(f)]
sys.path.insert(0, sys.argv[1])
import code_mower
from code_mower import cli, package
from code_mower.config import load_config
assert Path(code_mower.__file__).resolve().is_relative_to(Path(sys.argv[1]))
assert code_mower.__version__ == '1.4.1'
empty_store = Path.cwd() / 'empty-provider-store'
empty_store.mkdir()
def run(args, doctor=False):
    if doctor:
        args += ['--provider-config-dir', str(empty_store)]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = cli.main(args)
    if doctor:
        # Explicitly selected hosted transport has no credentials in this
        # fixture. Configuration/remediation is tested, not live readiness.
        assert status in (0, 1), (args, status, err.getvalue())
        assert not err.getvalue(), err.getvalue()
    else:
        assert status == 0, (args, status, err.getvalue(), out.getvalue())
    if doctor and '--json' in args:
        report = json.loads(out.getvalue())
        for check in report['checks']:
            assert sys.argv[1] not in str(check.get('remediation', ''))
    return out.getvalue()
for explicit in (False, True):
    repo = Path.cwd() / ('explicit' if explicit else 'fresh')
    repo.mkdir()
    previous = Path.cwd()
    os.chdir(repo)
    try:
        # Non-default profile proves --easy is never substituted.
        profile = 'deep_review'
        if explicit:
            source = Path('custom.yml')
            shutil.copyfile(package.packaged_starter_config_path(), source)
            selector = ['custom.yml']
        else:
            source = Path('code-mower.yml')
            selector = ['--packaged-starter']
        before = source.read_bytes() if source.exists() else None
        discovery = json.loads(run(['doctor', *selector, '--profile', profile, '--devin', '--json'], True))
        assert discovery['mode'] == 'doctor'
        for mode in ('--dry-run', '--apply'):
            command = ['init', *selector, '--profile', profile, '--set-transport', 'devin=devin_api_v3', mode, '--json']
            if mode == '--apply':
                command += ['--output-dir', '.code-mower.generated', '--skip-actionlint', '--skip-github-labels']
            payload = json.loads(run(command))
            if mode == '--dry-run':
                assert payload['profile']['id'] == profile
            else:
                staged_plan = json.loads(Path('.code-mower.generated/code-mower-init-plan.json').read_text())
                assert staged_plan['profile']['id'] == profile
            assert (source.read_bytes() if source.exists() else None) == before
        # Simulate the reviewed setup installation, then change verification selector.
        shutil.copyfile('.code-mower.generated/code-mower.yml', source)
        config = load_config(source)
        assert config['session_defaults']['transports']['devin'] == 'devin_api_v3'
        for json_mode in (False, True):
            command = ['doctor', str(source), '--profile', profile, '--devin']
            if json_mode:
                command += ['--json']
            rendered = run(command, True)
            assert f'doctor {source} --profile {profile} --devin' in rendered, rendered
            assert '--packaged-starter' not in rendered, rendered
        # Starter discovery stays immutable, even after repository adoption.
        unchanged = json.loads(run(['doctor', '--packaged-starter', '--profile', profile, '--devin', '--json'], True))
        selection = next(row for row in unchanged['checks'] if row['name'] == 'provider.devin.selection')
        assert selection['status'] == 'skip'
    finally:
        os.chdir(previous)
'''
            result = subprocess.run(
                [sys.executable, "-I", "-c", program, str(installed)], cwd=root,
                env={"PATH": os.defpath}, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
