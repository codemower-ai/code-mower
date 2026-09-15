"""The actual trailer labeler route, over the lowest GitHub request.

``trailer_comment_labeler.main`` fetches the verdict history itself and hands
it to ``lineage_context`` before deciding a label. That fetch used to apply
``or []`` and filter non-dicts away, so a falsey or malformed *successful*
page became an empty history -- and an empty history is an ordinary answer, so
the run went on to mutate a label anyway. These drive the real route and mock
only the lowest request, because validating a value handed straight to a
helper would walk past the normalisation that caused the problem.

Written against ``unittest`` on purpose: CI runs these through
``python -m unittest discover -s tests``, where pytest is not installed, so a
pytest-only file would be silently undiscovered rather than loudly missing.
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_mower import audit_labeler_lib as labeler_lib  # noqa: E402
from code_mower import trailer_comment_labeler  # noqa: E402

from test_trailer_comment_labeler import HEAD_SHA, _event  # noqa: E402

MALFORMED_TRAILER_HISTORIES = (
    None,
    False,
    {},
    [{"user": {"login": "codex-audit-bot"}, "body": "hi"}, "not a comment"],
    [{"user": {"login": "codex-audit-bot"}, "body": 12345}],
    [{"user": {"login": "codex-audit-bot"}, "body": None}],
    [{"user": "codex-audit-bot", "body": "hi"}],
    [{"user": {"login": {"name": "codex-audit-bot"}}, "body": "hi"}],
    [{"user": {"login": None}, "body": "hi"}],
)


def _verdict_body() -> str:
    return (
        "Codex Audit - PASS\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )


class TrailerHistoryMustBeReadable(unittest.TestCase):
    """The real `main` -> lower request -> `lineage_context` -> label route."""

    def _trailer_main(self, api):
        """Run the real `main`; `fetch_issue_comments` is deliberately untouched."""

        applied: list = []
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(
                json.dumps(_event("codex-audit-bot", _verdict_body())),
                encoding="utf-8",
            )
            env = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_TOKEN": "token",
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(
                        trailer_comment_labeler,
                        "fetch_pull_request",
                        lambda *_a, **_k: {"head": {"sha": HEAD_SHA}},
                    ), \
                    mock.patch.object(
                        labeler_lib, "github_request_with_fallback", api
                    ), \
                    mock.patch.object(
                        trailer_comment_labeler,
                        "apply_label_decision",
                        lambda repo, decision, **_k: applied.append((repo, decision)),
                    ):
                os.environ.pop("CODEX_BOT_AUTHORS", None)
                code = trailer_comment_labeler.main(["--lane", "codex"])
        return code, applied

    def test_an_unreadable_history_moves_no_label(self):
        for history in MALFORMED_TRAILER_HISTORIES:
            with self.subTest(history=history):
                _, applied = self._trailer_main(
                    lambda *_a, _history=history, **_k: _history
                )
                self.assertEqual(
                    applied, [], "an unreadable history may move no label"
                )

    def test_githubs_own_comment_schema_still_decides(self):
        """Positive control, valid fixtures only: none of this is an error."""

        valid = [
            {"user": None, "body": "a deleted account said this"},
            {"user": {"login": "someone"}},
            {"user": {"login": "codex-audit-bot"}, "body": _verdict_body()},
        ]
        code, applied = self._trailer_main(
            lambda method, path, **_k: valid if "page=1" in path else []
        )
        self.assertEqual(code, 0)
        self.assertTrue(applied, "a valid history still reaches a label decision")

    def test_a_genuinely_empty_history_still_decides(self):
        code, applied = self._trailer_main(lambda *_a, **_k: [])
        self.assertEqual(code, 0)
        self.assertTrue(
            applied, "no comment history is ordinary; the event still decides"
        )

    def test_an_announced_empty_lineage_marker_is_not_absence(self):
        """A trusted marker declaring no episodes contradicts itself."""

        from code_mower import builder_lineage

        marker = builder_lineage.lineage_comment_marker(())
        history = [
            {"user": {"login": "codemower-ai"}, "body": "Lineage\n\n" + marker},
            {"user": {"login": "codex-audit-bot"}, "body": _verdict_body()},
        ]
        with mock.patch.dict(
            os.environ, {"CODE_MOWER_DECISION_AUTHORITIES": "codemower-ai"}, clear=False
        ):
            _, applied = self._trailer_main(
                lambda method, path, **_k: history if "page=1" in path else []
            )
        self.assertEqual(applied, [], "announced lineage may not read as absence")


class TheseCasesRunWithoutPytest(unittest.TestCase):
    """CI runs `python -m unittest discover -s tests`, with no pytest installed.

    A pytest-only module is not an error there -- it is silently undiscovered,
    which is worse than a failure, because the coverage simply stops existing.
    So the discovery CI performs is exercised here with unittest's own loader.
    """

    MODULE = "test_trailer_lineage_history"

    def test_the_module_imports_no_pytest(self):
        """Checked structurally: a mention in prose is not a dependency."""

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("pytest", imported)

    def test_unittest_discovery_finds_every_case(self):
        suite = unittest.TestLoader().discover(
            str(Path(__file__).resolve().parent),
            pattern=Path(__file__).name,
            top_level_dir=str(Path(__file__).resolve().parent),
        )
        found = {case.id().rsplit(".", 1)[-1] for case in _flatten(suite)}
        for name in (
            "test_an_unreadable_history_moves_no_label",
            "test_githubs_own_comment_schema_still_decides",
            "test_a_genuinely_empty_history_still_decides",
            "test_an_announced_empty_lineage_marker_is_not_absence",
        ):
            with self.subTest(case=name):
                self.assertIn(name, found)

    def test_unittest_executes_the_cases_it_discovers(self):
        # Loaded by name rather than by discovery, so this class -- and this
        # very test -- stays out of the suite being run.
        suite = unittest.TestLoader().loadTestsFromName(
            f"{self.MODULE}.{TrailerHistoryMustBeReadable.__name__}"
        )
        self.assertGreater(suite.countTestCases(), 0)
        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0
        ).run(suite)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
