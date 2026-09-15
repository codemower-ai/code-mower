"""The lineage contract stays pure, stays mirrored, and stays packaged.

Three properties that are invisible to the behavioural tests and have each
already broken something:

* **Purity.** The same decision has to be computable by the package, by the
  vendored ``tools/`` copy inside a generated product repository, and by a
  reviewer host with no private state. One ``os.environ`` read or one store
  import would make that false without failing a single behaviour case.
* **Mirror parity.** CI lints and a generated product gate import the vendored
  file, not the package one, so drift there is invisible to every assertion
  that reaches into ``src/code_mower``.
* **Materialization.** A helper whose dependency is not in the generated
  support list fails at import time in the product repository, where nothing
  here would ever see it.

These are checked against the parsed module and its actual imported behaviour,
never against its source text.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from code_mower import builder_lineage, init, lineage_identity, package_manifest

from lineage_contract_fixtures import BRANCH, PR, REPO, TAKEN, chain, takeover


ROOT = Path(__file__).resolve().parents[1]

#: Modules whose decisions must be a function of their arguments alone.
PURE_MODULES = ("builder_lineage.py", "lineage_identity.py")

#: Standard-library modules a pure decision may use. Everything here is
#: computation; nothing here can reach the environment, a disk or a socket.
ALLOWED_STDLIB = {
    "__future__",
    "dataclasses",
    "hashlib",
    "json",
    "re",
    "typing",
}

#: Reaching any of these from a pure module is the failure, whether it arrives
#: as an import, an attribute or a builtin call.
FORBIDDEN_MODULES = {
    "os",
    "os.path",
    "pathlib",
    "subprocess",
    "socket",
    "shutil",
    "tempfile",
    "urllib",
    "urllib.request",
    "http",
    "requests",
}

#: Package modules a pure module may not depend on: adapters, transports,
#: stores and consumers. A pure import of one of these is how the contract
#: acquires an environment read it does not declare.
FORBIDDEN_PACKAGE_MODULES = {
    "audit_labeler_lib",
    "board",
    "context_store",
    "controller",
    "decisions",
    "devin_api",
    "lane_delivery",
    "lane_handoff",
    "lane_status",
    "provider_runners",
}

FORBIDDEN_CALLS = {"open", "input", "exec", "eval", "__import__", "compile"}


def _module_path(name: str) -> Path:
    return ROOT / "src" / "code_mower" / name


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            elif node.module:
                names.add(node.module)
    return names


class PurityTests(unittest.TestCase):
    def trees(self):
        for name in PURE_MODULES:
            yield name, ast.parse(_module_path(name).read_text(encoding="utf-8"))

    def test_pure_modules_import_nothing_that_can_reach_the_outside(self):
        for name, tree in self.trees():
            with self.subTest(module=name):
                for imported in _imported_names(tree):
                    self.assertNotIn(imported, FORBIDDEN_MODULES, imported)
                    root = imported.split(".")[0]
                    self.assertNotIn(root, FORBIDDEN_MODULES, imported)
                    if imported.startswith("."):
                        relative = imported.lstrip(".")
                        self.assertNotIn(relative, FORBIDDEN_PACKAGE_MODULES, imported)
                    elif root:
                        self.assertIn(root, ALLOWED_STDLIB | {"code_mower"}, imported)

    def test_pure_modules_make_no_io_or_dynamic_execution_calls(self):
        for name, tree in self.trees():
            with self.subTest(module=name):
                called = {
                    node.func.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                }
                self.assertEqual(called & FORBIDDEN_CALLS, set())

    def test_pure_modules_contain_no_import_inside_a_function(self):
        """A deferred import is how a store dependency hides from the header."""

        for name, tree in self.trees():
            with self.subTest(module=name):
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for inner in ast.walk(node):
                        self.assertNotIsInstance(inner, (ast.Import, ast.ImportFrom))

    def test_the_pure_contract_module_depends_on_no_package_module_at_all(self):
        tree = ast.parse(_module_path("builder_lineage.py").read_text(encoding="utf-8"))
        relative = {name for name in _imported_names(tree) if name.startswith(".")}
        self.assertEqual(relative, set())

    def test_the_identity_module_depends_only_on_the_contract_module(self):
        tree = ast.parse(_module_path("lineage_identity.py").read_text(encoding="utf-8"))
        relative = {name for name in _imported_names(tree) if name.startswith(".")}
        self.assertEqual(relative, {".builder_lineage"})

    def test_resolution_accumulates_nothing_between_calls(self):
        """Module-level tables are constants, not state that grows as it runs."""

        def snapshot():
            return {
                (module.__name__, attribute): repr(getattr(module, attribute))
                for module in (builder_lineage, lineage_identity)
                for attribute in dir(module)
                if not attribute.startswith("__")
                and isinstance(getattr(module, attribute), (list, dict, set))
            }

        before = snapshot()
        self.assertTrue(before, "expected module-level tables to compare")
        links = chain(4)
        for _ in range(3):
            builder_lineage.resolve_lineage(
                repo=REPO,
                pr_number=PR,
                branch=BRANCH,
                head_sha=links[-1].resulting_head,
                episodes=links,
                opener_lane="devin",
                label_lanes=("codex",),
            )
            lineage_identity.identity_with_lane_floor({"enabled": True}, "codex")
        self.assertEqual(snapshot(), before)


class MirrorTests(unittest.TestCase):
    def test_the_vendored_copy_is_byte_identical_to_the_canonical_module(self):
        self.assertEqual(
            (ROOT / "tools" / "builder_lineage.py").read_bytes(),
            (ROOT / "src" / "code_mower" / "builder_lineage.py").read_bytes(),
        )

    def _vendored(self):
        """Import the vendored copy standalone, the way a product gate does."""

        name = "vendored_builder_lineage"
        path = ROOT / "tools" / "builder_lineage.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # `@dataclass` resolves the defining module through `sys.modules` while
        # the class body is still executing, so a module executed from a spec
        # without being registered first fails on its first dataclass -- before
        # any of the behaviour below has a chance to run. Registering it is what
        # a real import does; restoring the previous entry afterwards keeps the
        # rest of the suite from seeing a second copy of the contract.
        previous = sys.modules.get(name)
        sys.modules[name] = module
        if previous is None:
            self.addCleanup(sys.modules.pop, name, None)
        else:
            self.addCleanup(sys.modules.__setitem__, name, previous)
        spec.loader.exec_module(module)
        return module

    def test_the_vendored_copy_is_registered_and_then_cleaned_up(self):
        """The import seam itself, so a silent regression cannot hide the rest."""

        name = "vendored_builder_lineage"
        self.assertNotIn(name, sys.modules)
        module = self._vendored()
        self.assertIs(sys.modules[name], module)
        self.assertTrue(module.ContributionEpisode.__dataclass_fields__)

    def test_the_vendored_copy_imports_with_no_package_on_the_path(self):
        vendored = self._vendored()
        self.assertEqual(vendored.MAX_EPISODES, builder_lineage.MAX_EPISODES)
        self.assertEqual(
            vendored.MAX_EPISODE_ARRIVALS, builder_lineage.MAX_EPISODE_ARRIVALS
        )

    def test_the_vendored_copy_reaches_the_same_decision(self):
        vendored = self._vendored()
        links = chain(8)
        cumulative = [
            episode.as_dict()
            for length in range(1, len(links) + 1)
            for episode in links[:length]
        ]
        kwargs = dict(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=cumulative,
            opener_lane="devin",
            label_lanes=("codex",),
        )
        mine = builder_lineage.resolve_lineage(**kwargs)
        theirs = vendored.resolve_lineage(**kwargs)
        self.assertEqual(mine.as_dict(), theirs.as_dict())
        self.assertEqual(mine.status, "resolved")
        self.assertEqual(mine.current_writer, "codex")

    def test_the_vendored_copy_refuses_the_same_broken_marker(self):
        vendored = self._vendored()
        broken = builder_lineage.lineage_comment_marker((takeover(),)).replace("-->", "")
        for module in (builder_lineage, vendored):
            with self.subTest(module=module.__name__):
                with self.assertRaises(module.LineageError):
                    module.episodes_from_comment_body(broken)

    def test_the_vendored_copy_parses_a_valid_marker_the_same_way(self):
        vendored = self._vendored()
        marker = builder_lineage.lineage_comment_marker((takeover(),))
        self.assertEqual(
            [episode.as_dict() for episode in vendored.episodes_from_comment_body(marker)],
            [takeover().as_dict()],
        )


class MaterializationTests(unittest.TestCase):
    def test_the_contract_travels_with_the_generated_gate_helper(self):
        targets = {target for target, _, _, _ in init.PRODUCT_SUPPORT_FILES}
        self.assertIn("tools/audit_labeler_lib.py", targets)
        self.assertIn("tools/builder_lineage.py", targets)
        self.assertIn("tools/decisions.py", targets)

    def test_every_generated_helper_is_copied_from_a_real_package_module(self):
        for target, source, _kind, _mode in init.PRODUCT_SUPPORT_FILES:
            if not (target.startswith("tools/") and target.endswith(".py")):
                continue
            if "/" in source:  # templated wrappers, not package modules
                continue
            with self.subTest(target=target):
                self.assertTrue(
                    (ROOT / "src" / "code_mower" / source).is_file(),
                    f"{target} copies a package module that does not exist",
                )

    def test_both_modules_are_in_the_package_inventory(self):
        by_target = {
            target: source for source, target, _ in package_manifest.PACKAGE_FILES
        }
        self.assertEqual(
            by_target["src/code_mower/builder_lineage.py"], "tools/builder_lineage.py"
        )
        self.assertEqual(
            by_target["src/code_mower/lineage_identity.py"],
            "src/code_mower/lineage_identity.py",
        )

    def test_every_packaged_source_for_the_contract_exists(self):
        for source, target, _kind in package_manifest.PACKAGE_FILES:
            if "lineage" not in target:
                continue
            with self.subTest(target=target):
                self.assertTrue((ROOT / source).is_file(), source)


STANDALONE_PROBE = '''
import json, os, sys

# Everything this repository could lend the probe is removed before the first
# import, so the materialized tree has to stand on its own.
repo = os.path.abspath(sys.argv[1])
sys.path[:] = [
    entry for entry in sys.path
    if entry and not os.path.abspath(entry).startswith(repo)
]
sys.path.insert(0, os.getcwd())
try:
    import code_mower
    package_origin = getattr(code_mower, "__file__", "") or ""
except Exception:
    package_origin = ""

import tools.builder_lineage as lineage
import tools.audit_labeler_lib as labeler

episode = lineage.ContributionEpisode(
    sequence=1, repo="acme/widget", pr_number=959, branch="devin/topic",
    source_lane="devin", destination_lane="codex",
    expected_head="a" * 40, resulting_head="b" * 40, writer_state="terminated",
)
marker = lineage.lineage_comment_marker((episode,))
parsed = lineage.episodes_from_comment_body("context\\n" + marker)
resolved = lineage.resolve_lineage(
    repo="acme/widget", pr_number=959, branch="devin/topic", head_sha="b" * 40,
    episodes=parsed, opener_lane="devin", label_lanes=("codex",),
)
try:
    lineage.lineage_comment_marker(())
    refused = False
except lineage.LineageError:
    refused = True
print(json.dumps({
    "lineage_origin": lineage.__file__,
    "labeler_origin": labeler.__file__,
    "parsed": len(parsed),
    "status": resolved.status,
    "writer": resolved.current_writer,
    "contributors": list(resolved.contributors),
    "unbound": lineage.resolve_lineage(
        repo="acme/widget", pr_number=959, branch="", head_sha="b" * 40,
        episodes=parsed,
    ).reason,
    "empty_render_refused": refused,
    "matches": labeler.builder_identity_matches(
        labels=["builder:codex"], author="codex[bot]", text="",
        config={"enabled": True, "labels": {"builder:codex": "codex"},
                "authors": {"codex[bot]": "codex"}},
    ),
    "package_origin": package_origin,
}))
'''


class MaterializedStandaloneTests(unittest.TestCase):
    """The real generated product tree, with no Code Mower package anywhere.

    A product repository's gate runs the *materialized* `tools/` copies with no
    package installed. Asserting mirror equality proves the bytes match; it does
    not prove the materialized tree imports and decides. This runs init for
    real, then a clean subprocess that can only see what init wrote.
    """

    def test_the_materialized_tools_tree_decides_on_its_own(self):
        from code_mower import config as code_mower_config

        plan = init.render_init_plan(
            code_mower_config.load_config(
                ROOT / "src/code_mower/templates/code-mower.example.yml"
            ),
            package_mode=True,
            package_command="code-mower",
        )
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp) / "product"
            init.apply_init_plan(plan, product / ".code-mower.generated")
            generated_tools = product / ".code-mower.generated" / "tools"
            self.assertTrue((generated_tools / "builder_lineage.py").is_file())

            # Exactly what a generated gate step sees: the materialized tools
            # package, and nothing of this repository.
            (generated_tools / "__init__.py").write_text("", encoding="utf-8")
            root = generated_tools.parent
            (root / "probe.py").write_text(STANDALONE_PROBE, encoding="utf-8")
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONHOME"}
                and not key.startswith("CODE_MOWER_")
            }
            result = subprocess.run(
                [sys.executable, "-E", "probe.py", str(ROOT)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=180,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertEqual(
            Path(payload["lineage_origin"]).resolve(),
            (generated_tools / "builder_lineage.py").resolve(),
        )
        self.assertEqual(
            Path(payload["labeler_origin"]).resolve(),
            (generated_tools / "audit_labeler_lib.py").resolve(),
        )
        self.assertFalse(
            payload["package_origin"].startswith(str(ROOT)),
            "the probe reached this repository's own package",
        )
        self.assertEqual(payload["parsed"], 1)
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["writer"], "codex")
        self.assertEqual(payload["contributors"], ["devin", "codex"])
        self.assertEqual(payload["unbound"], "target_invalid")
        self.assertTrue(payload["empty_render_refused"])
        self.assertEqual(payload["matches"], ["codex"])


class ContractSurfaceTests(unittest.TestCase):
    """The names later stages are being told to consume actually exist."""

    def test_the_contract_module_exports_the_stage_one_surface(self):
        for name in (
            "ContributionEpisode",
            "Lineage",
            "LineageContext",
            "LineageError",
            "ExactTarget",
            "IdentityConflictError",
            "bounded_arrivals",
            "branch_lane_from_identity",
            "builder_label_plan",
            "canonical_identity",
            "comment_author_login",
            "continuation_episode",
            "episode_from_handoff",
            "episode_from_mapping",
            "episodes_from_comment_body",
            "flatten_comment_pages",
            "lanes_from_identity",
            "lineage_comment_marker",
            "lineage_context",
            "merge_episodes",
            "pr_key",
            "published_episodes",
            "require_comment_list",
            "require_comment_record",
            "require_episode",
            "require_episode_chain",
            "require_exact_target",
            "resolve_builder_lineage",
            "resolve_configured_identity",
            "resolve_identity_only",
            "resolve_lineage",
            "resolve_lineage_context",
            "select_comment_history",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(builder_lineage, name), name)

    def test_the_identity_module_exports_the_stage_one_surface(self):
        for name in (
            "LANE_ACCOUNT_FLOOR",
            "ReviewerIdentityInvalid",
            "ReviewerNotIndependent",
            "combine_evidence",
            "identity_from_json",
            "identity_with_lane_floor",
            "marker_author_trust",
            "normalized_account_map",
            "pr_lineage",
            "require_independent_reviewer",
            "require_reviewer_lane",
            "reviewer_admission",
            "trusted_published_episodes",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(lineage_identity, name), name)

    def test_the_published_projection_carries_no_private_metadata(self):
        payload = builder_lineage.resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            episodes=(takeover(),),
            opener_lane="devin",
            label_lanes=("codex",),
        ).as_dict()
        rendered = repr(payload) + builder_lineage.lineage_comment_marker((takeover(),))
        for private in ("/Users", "/tmp", "session", "token", "prompt", "transcript"):
            with self.subTest(private=private):
                self.assertNotIn(private, rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
