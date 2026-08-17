from __future__ import annotations

from code_mower.audit_labeler_lib import GitHubToken
from code_mower.lane_configs import load_lane_config
from code_mower.provider_runners import bind_actions_run_comment_id
from code_mower.trailer_comment_labeler import resolve_label_decision


HEAD_SHA = "abcdef0123456789abcdef0123456789abcdef01"


def _event(author: str, body: str, *, action: str = "created", comment_id: int = 9001) -> dict:
    return {
        "action": action,
        "issue": {"number": 42, "pull_request": {}},
        "comment": {"id": comment_id, "user": {"login": author}, "body": body},
    }


def _bound_actions_body(body: str, *, comment_id: int = 9001) -> str:
    return bind_actions_run_comment_id(body, comment_id)


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


def test_github_actions_bot_requires_run_attestation(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    body = (
        "Codex Audit - PASS\n"
        f"Head SHA: {HEAD_SHA}\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )

    decision, reason = resolve_label_decision(
        _event("github-actions[bot]", body),
        current_head_sha=HEAD_SHA,
        config=config,
    )

    assert decision is None
    assert reason == "github-actions[bot] audit comment is not run-attested"


def test_github_actions_bot_accepts_empty_run_prs_with_commit_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    body = _bound_actions_body(
        "Codex Audit - PASS\n"
        f"Head SHA: {HEAD_SHA}\n"
        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )

    decision, reason = resolve_label_decision(
        _event("github-actions[bot]", body),
        current_head_sha=HEAD_SHA,
        config=config,
        repo="owner/repo",
        tokens=(GitHubToken("TEST_TOKEN", "token"),),
        github_actions_workflows=(".github/workflows/local-cli-audit.yml",),
        actions_run_lookup=lambda _run_id: {
            "event": "pull_request_target",
            "head_sha": HEAD_SHA,
            "path": ".github/workflows/local-cli-audit.yml",
            "pull_requests": [],
        },
        commit_pull_requests_lookup=lambda _head_sha: [
            {"number": 42, "head": {"sha": HEAD_SHA}},
        ],
    )

    assert decision is not None
    assert decision.add_label == "codex-audit-done"
    assert reason == "label done"


def test_github_actions_bot_rejects_replayed_run_marker(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    body = _bound_actions_body(
        "Codex Audit - PASS\n"
        f"Head SHA: {HEAD_SHA}\n"
        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->",
        comment_id=1111,
    )

    decision, reason = resolve_label_decision(
        _event("github-actions[bot]", body, action="edited", comment_id=2222),
        current_head_sha=HEAD_SHA,
        config=config,
        repo="owner/repo",
        tokens=(GitHubToken("TEST_TOKEN", "token"),),
        github_actions_workflows=(".github/workflows/local-cli-audit.yml",),
        actions_run_lookup=lambda _run_id: {
            "event": "pull_request_target",
            "head_sha": HEAD_SHA,
            "path": ".github/workflows/local-cli-audit.yml",
            "pull_requests": [],
        },
        commit_pull_requests_lookup=lambda _head_sha: [
            {"number": 42, "head": {"sha": HEAD_SHA}},
        ],
    )

    assert decision is None
    assert reason == "github-actions[bot] audit comment is not run-attested"


def test_github_actions_bot_rejects_edited_body_with_stale_digest(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    body = _bound_actions_body(
        "Codex Audit - BLOCKED\n"
        f"Head SHA: {HEAD_SHA}\n"
        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
    ).replace("codex-audit-blocked", "codex-audit-done")

    decision, reason = resolve_label_decision(
        _event("github-actions[bot]", body, action="edited", comment_id=9001),
        current_head_sha=HEAD_SHA,
        config=config,
        repo="owner/repo",
        tokens=(GitHubToken("TEST_TOKEN", "token"),),
        github_actions_workflows=(".github/workflows/local-cli-audit.yml",),
        actions_run_lookup=lambda _run_id: {
            "event": "pull_request_target",
            "head_sha": HEAD_SHA,
            "path": ".github/workflows/local-cli-audit.yml",
            "pull_requests": [],
        },
        commit_pull_requests_lookup=lambda _head_sha: [
            {"number": 42, "head": {"sha": HEAD_SHA}},
        ],
    )

    assert decision is None
    assert reason == "github-actions[bot] audit comment is not run-attested"


def test_github_actions_bot_rejects_empty_run_prs_without_commit_match(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    body = _bound_actions_body(
        "Codex Audit - PASS\n"
        f"Head SHA: {HEAD_SHA}\n"
        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )

    decision, reason = resolve_label_decision(
        _event("github-actions[bot]", body),
        current_head_sha=HEAD_SHA,
        config=config,
        repo="owner/repo",
        tokens=(GitHubToken("TEST_TOKEN", "token"),),
        github_actions_workflows=(".github/workflows/local-cli-audit.yml",),
        actions_run_lookup=lambda _run_id: {
            "event": "pull_request_target",
            "head_sha": HEAD_SHA,
            "path": ".github/workflows/local-cli-audit.yml",
            "pull_requests": [],
        },
        commit_pull_requests_lookup=lambda _head_sha: [
            {"number": 99, "head": {"sha": HEAD_SHA}},
        ],
    )

    assert decision is None
    assert reason == "github-actions[bot] audit comment is not run-attested"
