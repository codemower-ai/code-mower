from __future__ import annotations

import json
from pathlib import Path

from code_mower import decisions, trailer_comment_labeler
from code_mower.audit_labeler_lib import GitHubToken, IssueCommentPaginationLimitExceeded
from code_mower.lane_configs import load_lane_config
from code_mower.provider_runners import bind_actions_run_comment_id
from code_mower.trailer_comment_labeler import resolve_label_decision


HEAD_SHA = "abcdef0123456789abcdef0123456789abcdef01"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _event(author: str, body: str, *, action: str = "created", comment_id: int = 9001) -> dict:
    return {
        "action": action,
        "issue": {"number": 42, "pull_request": {}},
        "comment": {"id": comment_id, "user": {"login": author}, "body": body},
    }


def _bound_actions_body(body: str, *, comment_id: int = 9001) -> str:
    return bind_actions_run_comment_id(body, comment_id)


def _decision_marker(*, lane: str, title: str, file_path: str) -> str:
    return decisions.render_decision_marker(
        decisions.DecisionRecord(
            id="ADR-007",
            scope="finding",
            resolves="",
            by="owner",
            finding_id=decisions.stable_finding_id(lane, title, file_path),
            ref="ADR-007",
        )
    )


def _findings_marker(*, lane: str, title: str, file_path: str, line: int = 12) -> str:
    return decisions.render_audit_findings_marker(
        lane=lane,
        findings=[
            {
                "severity": "P2",
                "title": title,
                "file": file_path,
                "line": line,
            }
        ],
        complete=True,
    )


def test_main_skips_when_comment_history_exceeds_page_cap(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    body = (
        "Codex Audit - PASS\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event("codex-audit-bot", body)), encoding="utf-8")
    applied = []

    def raise_page_cap(*_args, **_kwargs):
        raise IssueCommentPaginationLimitExceeded("too many comments")

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        trailer_comment_labeler,
        "fetch_pull_request",
        lambda *_args, **_kwargs: {"head": {"sha": HEAD_SHA}},
    )
    monkeypatch.setattr(trailer_comment_labeler, "fetch_issue_comments", raise_page_cap)
    monkeypatch.setattr(
        trailer_comment_labeler,
        "apply_label_decision",
        lambda repo, decision, **_kwargs: applied.append((repo, decision)),
    )

    assert trailer_comment_labeler.main(["--lane", "codex"]) == 0

    captured = capsys.readouterr()
    assert "latest verdict cannot be established" in captured.err
    assert "comment history pagination cap exceeded" in captured.out
    assert applied == []


def test_labeler_ignores_older_current_head_verdict_when_newer_exists(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    older_pass = (
        "Codex Audit - PASS\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )
    newer_block = (
        "Codex Audit - BLOCKED\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
    )

    decision, reason = resolve_label_decision(
        _event("codex-audit-bot", older_pass, comment_id=1001),
        current_head_sha=HEAD_SHA,
        config=config,
        issue_comments=[
            {
                "id": 1001,
                "created_at": "2026-08-18T02:52:00Z",
                "user": {"login": "codex-audit-bot"},
                "body": older_pass,
            },
            {
                "id": 1002,
                "created_at": "2026-08-18T03:01:00Z",
                "user": {"login": "codex-audit-bot"},
                "body": newer_block,
            },
        ],
    )

    assert decision is None
    assert reason == "newer current-head audit verdict already exists"


def test_labeler_counts_event_comment_when_history_omits_it(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    older_block = (
        "Codex Audit - BLOCKED\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
    )
    newer_pass = (
        "Codex Audit - PASS\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )
    event = _event("codex-audit-bot", newer_pass, comment_id=1002)
    event["comment"]["created_at"] = "2026-08-18T03:01:00Z"

    decision, reason = resolve_label_decision(
        event,
        current_head_sha=HEAD_SHA,
        config=config,
        issue_comments=[
            {
                "id": 1001,
                "created_at": "2026-08-18T02:52:00Z",
                "user": {"login": "codex-audit-bot"},
                "body": older_block,
            },
        ],
    )

    assert decision is not None
    assert decision.add_label == "codex-audit-done"
    assert reason == "label done"


def test_labeler_later_blocked_demotes_earlier_done(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    older_pass = (
        "Codex Audit - PASS\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
    )
    newer_block = (
        "Codex Audit - BLOCKED\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
    )

    decision, reason = resolve_label_decision(
        _event("codex-audit-bot", newer_block, comment_id=1002),
        current_head_sha=HEAD_SHA,
        config=config,
        issue_comments=[
            {
                "id": 1001,
                "created_at": "2026-08-18T02:52:00Z",
                "user": {"login": "codex-audit-bot"},
                "body": older_pass,
            },
            {
                "id": 1002,
                "created_at": "2026-08-18T03:01:00Z",
                "user": {"login": "codex-audit-bot"},
                "body": newer_block,
            },
        ],
    )

    assert decision is not None
    assert reason == "label blocked"
    assert decision.add_label == "codex-audit-blocked"
    assert decision.remove_labels == ("needs-codex-audit", "codex-audit-done")


def test_labeler_counts_decision_covered_p2_as_codex_done(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    transcript = json.loads(
        (FIXTURES / "sample_app_311_decision_transcript.json").read_text(
            encoding="utf-8"
        )
    )
    audit_comment = transcript[-1]

    decision, reason = resolve_label_decision(
        _event(
            "codex-audit-bot",
            audit_comment["body"],
            comment_id=int(audit_comment["id"]),
        ),
        current_head_sha=HEAD_SHA,
        config=config,
        issue_comments=transcript,
        decision_authorities=("owner",),
    )

    assert decision is not None
    assert reason == "label done"
    assert decision.add_label == "codex-audit-done"
    assert decision.remove_labels == ("needs-codex-audit", "codex-audit-blocked")


def test_labeler_does_not_promote_codex_free_text_findings(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_BOT_AUTHORS", raising=False)
    config = load_lane_config("codex")
    title = "HOST_DISPLAY_NAME class-B finding repeats"
    audit_body = (
        "Codex Audit - BLOCKED\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "Findings:\n\n"
        f"- [P2] {title} -- `src/display.py:12`\n"
        "  ADR-007 already accepted this topic.\n\n"
        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
    )
    decision_body = _decision_marker(
        lane="codex",
        title=title,
        file_path="src/display.py",
    )

    decision, reason = resolve_label_decision(
        _event("codex-audit-bot", audit_body),
        current_head_sha=HEAD_SHA,
        config=config,
        issue_comments=[
            {"id": 1, "body": decision_body, "user": {"login": "owner"}},
            {"id": 2, "body": audit_body, "user": {"login": "codex-audit-bot"}},
        ],
        decision_authorities=("owner",),
    )

    assert decision is not None
    assert reason == "label blocked"
    assert decision.add_label == "codex-audit-blocked"


def test_labeler_does_not_promote_unopted_lane_structured_findings(monkeypatch) -> None:
    monkeypatch.delenv("DEVIN_BOT_AUTHORS", raising=False)
    config = load_lane_config("devin")
    title = "HOST_DISPLAY_NAME class-B finding repeats"
    audit_body = (
        "Devin Audit - BLOCKED\n"
        f"Head SHA: `{HEAD_SHA}`\n"
        "Findings:\n\n"
        + _findings_marker(
            lane="devin",
            title=title,
            file_path="src/display.py",
        )
        + "\n\n"
        f"- [P2] {title} -- `src/display.py:12`\n"
        "  ADR-007 already accepted this topic.\n\n"
        "<!-- DEVIN_AUDIT_STATE: devin-audit-blocked -->"
    )
    decision_body = _decision_marker(
        lane="devin",
        title=title,
        file_path="src/display.py",
    )

    decision, reason = resolve_label_decision(
        _event("devin-ai-integration", audit_body),
        current_head_sha=HEAD_SHA,
        config=config,
        issue_comments=[
            {"id": 1, "body": decision_body, "user": {"login": "owner"}},
            {"id": 2, "body": audit_body, "user": {"login": "devin-ai-integration"}},
        ],
        decision_authorities=("owner",),
    )

    assert decision is not None
    assert reason == "label blocked"
    assert decision.add_label == "devin-audit-blocked"


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
