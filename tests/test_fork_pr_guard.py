"""Fork posture for write-capable pull_request_target jobs (issue #664).

Generated review clear-stale and Code Mower gate jobs must skip fork
``pull_request_target`` events while keeping manual dispatches (and gate
``workflow_run`` events) enabled.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init
from code_mower import package_content as code_mower_package_content


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "src/code_mower/templates/code-mower.example.yml"

EXPECTED_GUARD = (
    "github.event_name != 'pull_request_target' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


def _workflow_on(workflow: dict) -> dict:
    return workflow.get("on") or workflow[True]


def _guard_allows(condition: str, *, event_name: str, repository: str,
                  head_repo: str | None) -> bool:
    """Evaluate a two-branch ``A || B`` job guard against a synthetic event."""

    def resolve(term: str) -> str | None:
        term = term.strip()
        if term.startswith(("'", '"')):
            return term.strip("'\"")
        context: object = {"event_name": event_name, "repository": repository,
                           "event": {"pull_request": {"head": {"repo": {"full_name": head_repo}}}}}
        node = {"github": context}
        for part in term.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    branches = [branch.strip() for branch in condition.split("||")]
    results = []
    for branch in branches:
        if "!=" in branch:
            left, right = (side.strip() for side in branch.split("!=", 1))
            results.append(resolve(left) != resolve(right))
        elif "==" in branch:
            left, right = (side.strip() for side in branch.split("==", 1))
            results.append(resolve(left) == resolve(right))
        else:  # pragma: no cover - guard shape changed
            raise AssertionError(f"unsupported guard branch: {branch!r}")
    return any(results)


def _generated_workflows() -> dict[str, str]:
    cfg = code_mower_config.load_config(CONFIG_PATH)
    plan = code_mower_init.render_init_plan(
        cfg,
        package_mode=True,
        package_command="code-mower",
        builders=code_mower_init._parse_builder_lanes("codex,claude,cursor"),
    )
    texts: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp) / "generated"
        code_mower_init.apply_init_plan(plan, output_dir)
        for name in (
            "codex-clear-stale.yml",
            "claude-clear-stale.yml",
            "code-mower-gate.yml",
        ):
            texts[name] = output_dir.joinpath(
                f".github/workflows/{name}").read_text(encoding="utf-8")
    return texts


class ForkPrGuardTests(unittest.TestCase):
    def test_canonical_and_mirror_clear_stale_templates_match(self) -> None:
        canonical = ROOT.joinpath(
            "templates/workflows/review-clear-stale.yml.j2").read_text(
                encoding="utf-8")
        mirror = ROOT.joinpath(
            "src/code_mower/templates/workflows/review-clear-stale.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertEqual(canonical, mirror)
        self.assertIn(f"if: {EXPECTED_GUARD}", canonical)

    def test_canonical_and_mirror_gate_templates_match(self) -> None:
        canonical = ROOT.joinpath(
            "templates/workflows/code-mower-gate.yml.j2").read_text(
                encoding="utf-8")
        mirror = ROOT.joinpath(
            "src/code_mower/templates/workflows/code-mower-gate.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertEqual(canonical, mirror)
        self.assertIn(f"if: {EXPECTED_GUARD}", canonical)

    def test_packaged_clear_stale_fallback_carries_guard(self) -> None:
        with mock.patch.object(
            code_mower_package_content,
            "_workflow_template_file_text",
            return_value=None,
        ):
            fallback = code_mower_package_content._workflow_template_text(
                "templates/workflows/review-clear-stale.yml.j2")
        self.assertIn(f"if: {EXPECTED_GUARD}", fallback)

    def test_generated_clear_stale_workflows_guard_fork_prs(self) -> None:
        texts = _generated_workflows()
        for name in ("codex-clear-stale.yml", "claude-clear-stale.yml"):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load(texts[name])
                on_config = _workflow_on(workflow)
                self.assertIn("pull_request_target", on_config)
                self.assertIn("workflow_dispatch", on_config)
                job = workflow["jobs"]["clear-stale"]
                self.assertEqual(job["if"], EXPECTED_GUARD)
                self.assertIn("secrets.GITHUB_TOKEN", texts[name])
                self.assertTrue(
                    _guard_allows(job["if"], event_name="pull_request_target",
                                  repository="owner/repo", head_repo="owner/repo"))
                self.assertFalse(
                    _guard_allows(job["if"], event_name="pull_request_target",
                                  repository="owner/repo", head_repo="owner/other-repo"))
                self.assertTrue(
                    _guard_allows(job["if"], event_name="workflow_dispatch",
                                  repository="owner/repo", head_repo=None))

    def test_generated_gate_workflow_guards_fork_prs(self) -> None:
        text = _generated_workflows()["code-mower-gate.yml"]
        workflow = yaml.safe_load(text)
        on_config = _workflow_on(workflow)
        self.assertIn("pull_request_target", on_config)
        self.assertIn("workflow_dispatch", on_config)
        self.assertIn("workflow_run", on_config)
        job = workflow["jobs"]["gate"]
        self.assertEqual(job["name"], "publish Code Mower gate status")
        self.assertEqual(job["if"], EXPECTED_GUARD)
        self.assertIn("CODE_MOWER_GATE_AUTOMERGE_TOKEN", text)
        self.assertTrue(
            _guard_allows(job["if"], event_name="pull_request_target",
                          repository="owner/repo", head_repo="owner/repo"))
        self.assertFalse(
            _guard_allows(job["if"], event_name="pull_request_target",
                          repository="owner/repo", head_repo="owner/other-repo"))
        self.assertTrue(
            _guard_allows(job["if"], event_name="workflow_dispatch",
                          repository="owner/repo", head_repo=None))
        self.assertTrue(
            _guard_allows(job["if"], event_name="workflow_run",
                          repository="owner/repo", head_repo=None))


if __name__ == "__main__":
    unittest.main()
