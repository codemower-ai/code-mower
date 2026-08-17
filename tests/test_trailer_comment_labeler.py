from __future__ import annotations

from code_mower.lane_configs import load_lane_config
from code_mower.trailer_comment_labeler import resolve_label_decision


HEAD_SHA = "abcdef0123456789abcdef0123456789abcdef01"


def _event(author: str, body: str) -> dict:
    return {
        "action": "created",
        "issue": {"number": 42, "pull_request": {}},
        "comment": {"user": {"login": author}, "body": body},
    }


def test_configured_shared_author_requires_matching_lane_trailer(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_BOT_AUTHORS", "operator")
    config = load_lane_config("codex")
    body = (
        "Codex Audit - PASS\n"
        f"Head SHA: {HEAD_SHA}\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )

    decision, reason = resolve_label_decision(
        _event("operator", body),
        current_head_sha=HEAD_SHA,
        config=config,
    )

    assert decision is not None
    assert decision.add_label == "codex-audit-done"
    assert reason == "label done"


def test_configured_shared_author_without_lane_trailer_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_BOT_AUTHORS", "operator")
    config = load_lane_config("codex")
    body = f"Codex Audit - PASS\nHead SHA: {HEAD_SHA}\n"

    decision, reason = resolve_label_decision(
        _event("operator", body),
        current_head_sha=HEAD_SHA,
        config=config,
    )

    assert decision is None
    assert "missing matching CODEX_AUDIT_STATE trailer" in reason


def test_default_lane_bot_keeps_legacy_prose_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    body = f"Codex Audit - PASS\nHead SHA: {HEAD_SHA}\n"

    decision, reason = resolve_label_decision(
        _event("codex-audit-bot", body),
        current_head_sha=HEAD_SHA,
        config=config,
    )

    assert decision is not None
    assert decision.add_label == "codex-audit-done"
    assert reason == "label done"
