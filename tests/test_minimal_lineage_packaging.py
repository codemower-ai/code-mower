"""Pure import boundary, byte parity, and real isolated init materialization."""
import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from code_mower import init, package
from code_mower.config import load_config
from minimal_lineage_fixtures import episode, identity_mapping, target

ROOT = Path(__file__).resolve().parents[1]
PURE_STDLIB = {"__future__", "collections", "dataclasses", "json", "re"}


class PackagingTests(unittest.TestCase):
    def test_canonical_and_tools_bytes_match(self):
        self.assertEqual((ROOT / "src/code_mower/builder_lineage.py").read_bytes(),
                         (ROOT / "tools/builder_lineage.py").read_bytes())

    def test_entire_import_graph_is_stdlib_only_and_has_no_io_execution(self):
        for relative in ("src/code_mower/builder_lineage.py", "tools/builder_lineage.py"):
            tree = ast.parse((ROOT / relative).read_text())
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, "no project/transitive adapter imports")
                    imports.add(node.module.split(".")[0])
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, {"open", "exec", "eval", "__import__", "compile", "input"})
                    elif isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, {"open", "read_text", "read_bytes", "write_text",
                                                        "write_bytes", "getenv", "system", "popen"})
            # Every reachable dependency terminates in the stdlib; there is no
            # project helper whose own imports could transitively perform I/O.
            self.assertEqual(imports, PURE_STDLIB)

    def test_canonical_manifest_regeneration_equality(self):
        expected = package.committed_package_manifest_text(package.generate_committed_package_manifest(ROOT))
        self.assertEqual((ROOT / "code-mower-package-manifest.json").read_text(), expected)
        manifest = json.loads(expected)
        targets = {entry["target"] for entry in manifest["files_written"]}
        self.assertIn("src/code_mower/builder_lineage.py", targets)
        self.assertIn("tools/builder_lineage.py", targets)

    def test_init_materialized_helper_import_parse_resolve_without_package_or_repo(self):
        config = load_config(ROOT / "src/code_mower/templates/code-mower.example.yml")
        plan = init.render_init_plan(config, package_mode=True, repo_root=ROOT)
        with tempfile.TemporaryDirectory(dir=ROOT) as scratch:
            output = Path(scratch) / "product"
            init.apply_init_plan(plan, output, source_root=ROOT)
            helper = output / "tools/builder_lineage.py"
            self.assertEqual(helper.read_bytes(), (ROOT / "src/code_mower/builder_lineage.py").read_bytes())
            fixture = dict(target=dict(repo=target().repo, pr_number=target().pr_number,
                                       branch=target().branch, head_sha=target().head_sha),
                           episode=episode().to_mapping(), identity=identity_mapping())
            # -I -S removes the checkout/PYTHONPATH and installed package hooks.
            # Only the generated tools directory is exposed to this fresh process.
            program = '''
import importlib.util
import json
from pathlib import Path
import sys
assert importlib.util.find_spec("code_mower") is None
assert "code_mower" not in sys.modules
sys.path.insert(0, str(Path.cwd() / "tools"))
import builder_lineage as core
assert Path(core.__file__).resolve() == Path.cwd() / "tools/builder_lineage.py"
value = json.load(sys.stdin)
target = core.Target.from_mapping(value["target"])
chain = core.Chain.from_arrivals(target, [value["episode"]])
body = core.render(chain)
history = core.History([{"user": {"login": "owner"}, "body": body}])
parsed = core.Chain.from_arrivals(target, core.parse_markers(history, core.Authorities(["owner"])))
assert parsed == chain
identity = core.Identity.from_mapping(value["identity"])
decision = core.resolve(parsed, identity, "devin-bot[bot]", ["builder:codex"])
assert decision.reason == "verified_lineage"
assert decision.current_writer == "codex"
assert not core.admit(decision, "codex")
assert not core.admit(decision, "devin")
assert core.admit(decision, "claude")
assert importlib.util.find_spec("code_mower") is None
print("isolated import/parse/resolve/admit passed")
'''
            result = subprocess.run(["python", "-I", "-S", "-c", program], cwd=output,
                                    input=json.dumps(fixture), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "isolated import/parse/resolve/admit passed")


if __name__ == "__main__":
    unittest.main()
