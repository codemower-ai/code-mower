"""The actual trailer labeler route, over the lowest GitHub request.

``trailer_comment_labeler.main`` fetches the verdict history itself and hands
it to ``lineage_context`` before deciding a label. That fetch used to apply
``or []`` and filter non-dicts away, so a falsey or malformed *successful*
page became an empty history -- and an empty history is an ordinary answer, so
the run went on to mutate a label anyway. These drive the real route and mock
only the lowest request, because validating a value handed straight to a
helper would walk past the normalisation that caused the problem.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


def _trailer_main(monkeypatch, tmp_path, api):
    """Run the real `main`; `fetch_issue_comments` is deliberately untouched."""

    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(_event("codex-audit-bot", _verdict_body())), encoding="utf-8"
    )
    applied: list = []
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        trailer_comment_labeler,
        "fetch_pull_request",
        lambda *_args, **_kwargs: {"head": {"sha": HEAD_SHA}},
    )
    monkeypatch.setattr(labeler_lib, "github_request_with_fallback", api)
    monkeypatch.setattr(
        trailer_comment_labeler,
        "apply_label_decision",
        lambda repo, decision, **_kwargs: applied.append((repo, decision)),
    )
    code = trailer_comment_labeler.main(["--lane", "codex"])
    return code, applied


@pytest.mark.parametrize("history", MALFORMED_TRAILER_HISTORIES)
def test_main_mutates_no_label_when_the_history_is_unreadable(
    history, monkeypatch, tmp_path
) -> None:
    _, applied = _trailer_main(
        monkeypatch, tmp_path, lambda *_args, **_kwargs: history
    )
    assert applied == [], "an unreadable history may move no label"


def test_main_reads_githubs_own_comment_schema(monkeypatch, tmp_path) -> None:
    """Positive control, valid fixtures only: none of this is an error."""

    valid = [
        {"user": None, "body": "a deleted account said this"},
        {"user": {"login": "someone"}},
        {"user": {"login": "codex-audit-bot"}, "body": _verdict_body()},
    ]
    code, applied = _trailer_main(
        monkeypatch,
        tmp_path,
        lambda method, path, **_kwargs: valid if "page=1" in path else [],
    )
    assert code == 0
    assert applied, "a valid history still reaches a label decision"


def test_main_still_decides_on_a_genuinely_empty_history(
    monkeypatch, tmp_path
) -> None:
    code, applied = _trailer_main(monkeypatch, tmp_path, lambda *_a, **_k: [])
    assert code == 0
    assert applied, "no comment history is ordinary; the event still decides"
