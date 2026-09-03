from __future__ import annotations

import json
from pathlib import Path

from code_mower import claude_audit_pr
from code_mower import code_mower_telemetry
from code_mower import codex_audit_pr
from code_mower.cloud_client import build_cloud_bundle, build_upload_payload
from code_mower.cloud_client.bundle import validate_metadata_payload


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
CONTRACT_PATH = FIXTURE_ROOT / "provider_verdict_contracts.json"
ARTIFACT_ROOT = FIXTURE_ROOT / "verdict_artifacts"


def _contracts() -> dict[str, object]:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert payload["schema"] == "code_mower.providerFixtureContracts.v1"
    return payload


def test_codex_golden_verdict_fixtures_lock_parser_contracts() -> None:
    cases = _contracts()["codex"]
    assert isinstance(cases, dict)

    parsed_pass = codex_audit_pr.parse_structured_codex_verdict(cases["pass"])
    assert parsed_pass.verdict == "PASS"
    assert parsed_pass.blocker_count == 0
    assert parsed_pass.p3_count == 0
    assert "Findings: none." in parsed_pass.prose

    parsed_blocked = codex_audit_pr.parse_structured_codex_verdict(cases["blocked"])
    assert parsed_blocked.verdict == "BLOCKED"
    assert parsed_blocked.p1_count == 1
    assert parsed_blocked.blocker_count == 1
    assert "Bypass skips required reviewer verdict" in parsed_blocked.prose

    parsed_placeholder = codex_audit_pr.parse_structured_codex_verdict(
        cases["placeholder"]
    )
    assert parsed_placeholder.verdict == "UNKNOWN"
    assert parsed_placeholder.p1_count == 1
    assert parsed_placeholder.quarantine_reason == "fixture-shaped structured verdict"

    parsed_malformed = codex_audit_pr.parse_structured_codex_verdict(
        cases["malformed_extra_key"]
    )
    assert parsed_malformed.verdict == "UNKNOWN"
    assert "unsupported keys: extra" in parsed_malformed.prose


def test_claude_golden_verdict_fixtures_lock_parser_contracts() -> None:
    cases = _contracts()["claude"]
    assert isinstance(cases, dict)

    parsed_pass = claude_audit_pr.parse_structured_claude_verdict(cases["pass"])
    assert parsed_pass.verdict == "PASS"
    assert parsed_pass.blocker_count == 0
    assert parsed_pass.p3_count == 0
    assert "Findings: none." in parsed_pass.prose

    parsed_blocked = claude_audit_pr.parse_structured_claude_verdict(cases["blocked"])
    assert parsed_blocked.verdict == "BLOCKED"
    assert parsed_blocked.p1_count == 1
    assert parsed_blocked.blocker_count == 1
    assert "Bypass skips required reviewer verdict" in parsed_blocked.prose

    parsed_placeholder = claude_audit_pr.parse_structured_claude_verdict(
        cases["placeholder"]
    )
    guarded, reason = claude_audit_pr._apply_claude_verdict_guardrails(
        parsed_placeholder,
        ("src/app.py",),
    )
    assert guarded.verdict == "UNKNOWN"
    assert reason == "structured verdict summary matched a schema placeholder value"

    parsed_malformed = claude_audit_pr.parse_structured_claude_verdict(
        cases["malformed_extra_key"]
    )
    assert parsed_malformed.verdict == "UNKNOWN"
    assert "unsupported keys: extra" in parsed_malformed.prose


def test_verdict_artifact_provider_aliases_match_registry_ids() -> None:
    assert code_mower_telemetry._lane_provider("antigravity-cli-audit") == "antigravity"
    assert code_mower_telemetry._lane_provider("coderabbit-cli-audit") == "coderabbit"
    assert code_mower_telemetry._lane_provider("cursor-bugbot-audit") == "cursor_bugbot"
    assert code_mower_telemetry._lane_provider("grok-audit") == "grok_build"
    assert code_mower_telemetry._lane_provider("muse-cli-audit") == "muse"


def test_verdict_artifact_fixtures_export_metadata_only_events(tmp_path: Path) -> None:
    events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(
        ARTIFACT_ROOT,
        repo="codemower-ai/code-mower",
        include_git_ref=True,
    )

    assert len(events) == 9
    for event in events:
        validate_metadata_payload(event)
        rendered = json.dumps(event, sort_keys=True)
        assert "comment_body" not in rendered
        assert "raw_diff" not in rendered
        assert "transcript" not in rendered

    by_case = {
        (event["dimensions"]["lane_id"], event["dimensions"]["pr_number"]): event
        for event in events
    }
    codex_pass = by_case[("codex-audit", 1)]
    assert codex_pass["provider"] == "codex"
    assert codex_pass["status"] == "pass"
    assert codex_pass["metrics"]["duration_seconds"] == 4.5
    assert codex_pass["metrics"]["p1_count"] == 0
    assert codex_pass["dimensions"]["head_sha_short"] == "111111111111"
    assert codex_pass["dimensions"]["audit_comment_identity_source"] == "trailer"

    claude_blocked = by_case[("claude-audit", 2)]
    assert claude_blocked["provider"] == "claude"
    assert claude_blocked["status"] == "blocked"
    assert claude_blocked["metrics"]["p1_count"] == 1

    codex_unknown = by_case[("codex-audit", 3)]
    assert codex_unknown["status"] == "unknown"
    assert codex_unknown["dimensions"]["audit_comment_lane_id"] == "codex-audit"

    gitar_pass = by_case[("gitar-audit", 10)]
    assert gitar_pass["provider"] == "gitar"
    assert gitar_pass["status"] == "pass"
    assert gitar_pass["metrics"]["duration_seconds"] == 12.3

    cursor_bugbot_blocked = by_case[("cursor-bugbot-audit", 11)]
    assert cursor_bugbot_blocked["provider"] == "cursor_bugbot"
    assert cursor_bugbot_blocked["status"] == "blocked"
    assert cursor_bugbot_blocked["metrics"]["p1_count"] == 1

    antigravity_pass = by_case[("antigravity-cli-audit", 12)]
    assert antigravity_pass["provider"] == "antigravity"
    assert antigravity_pass["status"] == "pass"
    assert antigravity_pass["metrics"]["duration_seconds"] == 45.2

    devin_blocked = by_case[("devin-audit", 13)]
    assert devin_blocked["provider"] == "devin"
    assert devin_blocked["status"] == "blocked"
    assert devin_blocked["metrics"]["p1_count"] == 1

    muse_pass = by_case[("muse-audit", 14)]
    assert muse_pass["provider"] == "muse"
    assert muse_pass["status"] == "pass"

    grok_build_pass = by_case[("grok-audit", 15)]
    assert grok_build_pass["provider"] == "grok_build"
    assert grok_build_pass["status"] == "pass"

    build_cloud_bundle(
        reports=[],
        events=events,
        output_dir=tmp_path / "bundle",
        repo_slug="codemower-ai/code-mower",
    )
    upload = build_upload_payload(bundle_dir=tmp_path / "bundle")
    assert upload["upload_mode"] == "metadata_only"
    assert len(upload["events"]) == 9
    assert len(upload["reports"]) == 0
    for event in upload["events"]:
        validate_metadata_payload(event)
