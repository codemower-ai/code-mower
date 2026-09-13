from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import code_mower.cloud_client.operations as cloud_operations
from code_mower.cloud_client import (
    BUNDLE_MANIFEST_FILENAME,
    CURRENT_PROFILE_FILENAME,
    CloudBundleError,
    CloudTokenResolution,
    DEFAULT_SETUP_INSTALL_ID,
    EVENT_SCHEMA,
    UPLOAD_IDENTITY_SCHEMA,
    build_board_snapshot_event,
    build_provenance_summary,
    build_provider_catalog_snapshot_events,
    build_cloud_bundle,
    build_upload_payload,
    bundle_manifest_identity,
    default_setup_path,
    dogfood_upload,
    normalize_event,
    parse_event_args,
    parse_repo_sync_spec,
    read_bundle_manifest,
    repo_slug_from_remote,
    repo_sync_output_name,
    render_cloud_doctor_text,
    resolve_cloud_identity,
    resolve_cloud_token,
    run_cloud_doctor,
    run_cloud_setup,
    safe_config_stem,
    token_prefix,
    validate_cloud_event,
)
from code_mower import cloud as cloud_cli

# The package lane loads this module with plain unittest, which has no pytest
# available, so exception expectations come from unittest itself.
assert_raises = unittest.TestCase().assertRaises


def _board_snapshot_fixture() -> dict[str, object]:
    return {
        "schema": "code_mower.laneStatus.v1",
        "repo": "owner/repo",
        "generated_at": "2026-09-02T12:00:00Z",
        "next_action": "ready for merge or auto-merge",
        "board": {"schema": "code_mower.board.v1"},
        "remote": {
            "available": True,
            "pull_requests": [
                {
                    "number": 7,
                    "title": "sensitive local PR title",
                    "url": "https://github.com/owner/repo/pull/7",
                    "branch": "codex/board",
                    "author": "codex-bot",
                    "is_draft": False,
                    "merge_state": "CLEAN",
                    "updated_at": "2026-09-02T12:00:00Z",
                    "head_sha": "abcdef0123456789abcdef0123456789abcdef01",
                    "labels": {"builder": ["builder:codex"], "done": ["claude-audit-done"]},
                    "checks": [{"name": "code-mower/gate", "state": "success"}],
                    "stale": False,
                    "next_action": "ready for merge or auto-merge",
                    "gate_rerun_command": "gh workflow run code-mower-gate.yml --repo owner/repo",
                }
            ],
            "workflow_runs": [
                {
                    "id": 77,
                    "workflow": "Code Mower gate",
                    "title": "internal workflow title",
                    "status": "completed",
                    "conclusion": "success",
                    "event": "pull_request",
                    "branch": "codex/board",
                    "updated_at": "2026-09-02T12:00:00Z",
                    "url": "https://github.com/owner/repo/actions/runs/77",
                }
            ],
            "gate_health": {"status": "pass", "alerts": []},
        },
        "owner_queue": {
            "entries": [
                {
                    "kind": "needs-owner",
                    "title": "private owner note",
                    "priority": 0,
                    "pr_number": 7,
                    "branch": "codex/board",
                    "author": "codex-bot",
                    "updated_at": "2026-09-02T12:00:00Z",
                    "head_sha_prefix": "abcdef012345",
                    "next_action": "owner decision",
                    "labels": ["needs-owner"],
                }
            ]
        },
        "agent_adapters": {
            "agents": [
                {
                    "provider": "codex",
                    "role": "builder",
                    "status": "running",
                    "lane": "builder:codex",
                    "pr_number": 7,
                    "pid": 999,
                    "cwd": "/tmp/private/checkout",
                    "title": "private prompt fragment",
                    "next_action": "awaiting peer audit",
                }
            ]
        },
        "timelines": {
            "verdicts": {"entries": [{"lane": "claude-audit", "verdict": "PASS"}]},
            "spend": {
                "available": True,
                "groups": [
                    {
                        "lane": "claude-audit",
                        "verdict": "PASS",
                        "runs": 1,
                        "wall_seconds_total": 12.5,
                        "wall_seconds_avg": 12.5,
                        "cost_usd_total": 0.125,
                        "total_tokens": 1000,
                    }
                ],
            },
        },
        "supervised_pilot": {
            "schema": "code_mower.supervisedPilot.v1",
            "enabled": True,
            "controller_mode": "dry_run",
            "cycle_state": "ready",
            "generated_at": "2026-09-02T12:00:00Z",
            "decision": {
                "decision_state": "ready_for_merge",
                "stop_condition": "",
                "next_action": "ready for merge or auto-merge",
                "next_detail": "all promoted reviewers passed",
                "lane_id": "codex",
                "pr_number": 7,
                "gate_status": "success",
                "author_lane_excluded": True,
                "promoted_reviewers_passed": True,
                "reviewer_outcomes": [
                    {
                        "lane_id": "claude",
                        "config_lane_id": "claude_audit",
                        "verdict": "PASS",
                        "promoted": True,
                    }
                ],
            },
            "queue": {
                "metrics": {
                    "active_lane_count": 1,
                    "blocked_pr_count": 0,
                    "open_pr_count": 1,
                    "owner_action_count": 0,
                    "ready_issue_count": 1,
                    "ready_pr_count": 1,
                    "stale_evidence_count": 0,
                },
                "active_lanes": {"codex": 1},
            },
            "active_prs": [
                {
                    "number": 7,
                    "title": "super private supervised PR title",
                    "url": "https://github.com/owner/repo/pull/7",
                    "branch": "codex/board",
                    "author": "codex-bot",
                    "is_draft": False,
                    "merge_state": "CLEAN",
                    "updated_at": "2026-09-02T12:00:00Z",
                    "head_sha": "abcdef0123456789abcdef0123456789abcdef01",
                    "labels": {"builder": ["builder:codex"], "done": ["claude-audit-done"]},
                    "checks": [{"name": "code-mower/gate", "state": "success"}],
                    "next_action": "ready for merge or auto-merge",
                }
            ],
            "active_issues": [
                {
                    "number": 8,
                    "title": "super private supervised issue title",
                    "url": "https://github.com/owner/repo/issues/8",
                    "author": "owner",
                    "updated_at": "2026-09-02T12:00:00Z",
                    "labels": ["tier:R", "builder:claude"],
                    "builder_lane": "claude",
                    "assigned": False,
                    "dispatched": False,
                    "owner_action": False,
                }
            ],
        },
    }


def test_cloud_catch_up_summary_separates_history_from_calibration() -> None:
    runs = [
        {
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "createdAt": "2026-06-15T00:00:00Z",
            "updatedAt": "2026-06-15T00:01:00Z",
        },
        {
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "createdAt": "2026-06-16T00:00:00Z",
            "updatedAt": "2026-06-16T00:02:00Z",
        },
        {
            "name": "Dogfood",
            "status": "in_progress",
            "conclusion": "",
            "createdAt": "2026-06-17T00:00:00Z",
            "updatedAt": "2026-06-17T00:03:00Z",
        },
    ]

    summary = cloud_operations.build_catch_up_summary(
        repo_slug="owner/repo",
        runs=runs,
        events=[{"event_type": "workflow_run"} for _ in runs],
        requested_limit=50,
        include_git_ref=False,
    )

    assert summary["repo_slug"] == "owner/repo"
    assert summary["requested_limit"] == 50
    assert summary["run_count"] == 3
    assert summary["event_count"] == 3
    assert summary["provenance"] == "imported_history"
    assert summary["source_category"] == "history"
    assert summary["history_only"] is True
    assert summary["calibration_evidence"] is False
    assert summary["trust_guidance"] == {
        "use_for": "historical activity context and dashboard coverage backfill",
        "do_not_use_for": "reviewer or lens accuracy, lane promotion, or merge-gate policy",
        "next_step": (
            "run current dogfood uploads plus reviewer-runs or calibration evidence "
            "before making provider/lens decisions"
        ),
    }
    assert summary["git_ref_included"] is False
    assert summary["workflow_counts"] == {"CI": 2, "Dogfood": 1}
    assert summary["status_counts"] == {"completed": 2, "in_progress": 1}
    assert summary["conclusion_counts"] == {
        "failure": 1,
        "success": 1,
        "unknown": 1,
    }
    assert summary["oldest_run_at"] == "2026-06-15T00:00:00Z"
    assert summary["newest_run_at"] == "2026-06-17T00:00:00Z"
    assert summary["last_updated_at"] == "2026-06-17T00:03:00Z"


def test_cloud_setup_round_trip_writes_private_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "tokens" / "install.env"

        result = run_cloud_setup(
            token="cmw_live_test_secret_token",
            token_file=None,
            token_stdin=False,
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            endpoint="https://codemower.com/api/ingest",
            team_id="team",
            install_id="install",
            out=target,
            force=False,
            dry_run=False,
        )

        assert result["status"] == "written"
        assert target.stat().st_mode & 0o777 == 0o600
        text = target.read_text(encoding="utf-8")
        assert "CODE_MOWER_CLOUD_TOKEN" in text
        assert "cmw_live_test_secret_token" in text
        current = target.parent / CURRENT_PROFILE_FILENAME
        assert current.read_text(encoding="utf-8").strip() == "install.env"


def test_cloud_setup_helpers_keep_safe_defaults() -> None:
    assert safe_config_stem("  weird path/name  ") == "weird-path-name"
    assert safe_config_stem("") == DEFAULT_SETUP_INSTALL_ID
    rendered = str(default_setup_path("agent@local"))
    assert rendered.endswith("/.config/code-mower/tokens/agent-local.env")
    assert "sixteen-char-tok" not in token_prefix("sixteen-char-tok")


def test_cloud_token_resolver_env_wins_over_stored_file(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_RESOLVE_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "install.env").write_text(
        f"export {token_env}='cmw_live_file_secret'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(token_env, "cmw_live_env_secret")

    resolution = resolve_cloud_token(token_env=token_env, token_dir=token_dir)

    assert resolution.status == "ok"
    assert resolution.source == "env"
    assert resolution.token == "cmw_live_env_secret"


def test_cloud_token_resolver_uses_install_id_after_restart(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_INSTALL_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "codex-code-mower.env").write_text(
        "\n".join(
            [
                f"export {token_env}='cmw_live_install_secret'",
                "export CODE_MOWER_CLOUD_TEAM_ID='team'",
                "export CODE_MOWER_INSTALL_ID='codex-code-mower'",
                "export CODE_MOWER_CLOUD_ENDPOINT='https://codemower.com/api/ingest'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(token_env, raising=False)

    resolution = resolve_cloud_token(
        token_env=token_env,
        token_dir=token_dir,
        install_id="codex-code-mower",
    )

    assert resolution.status == "ok"
    assert resolution.source == "install_id"
    assert resolution.token == "cmw_live_install_secret"
    assert resolution.team_id == "team"
    assert resolution.install_id == "codex-code-mower"


def test_conflicting_ambient_cloud_variables_cannot_satisfy_an_install_gate(
    monkeypatch, tmp_path
) -> None:
    token_env = "CODE_MOWER_TEST_AMBIENT_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "codex-code-mower.env").write_text(
        "\n".join(
            [
                f"export {token_env}='cmw_live_install_secret'",
                "export CODE_MOWER_CLOUD_TEAM_ID='stored-team'",
                "export CODE_MOWER_INSTALL_ID='codex-code-mower'",
                "export CODE_MOWER_CLOUD_ENDPOINT='https://codemower.com/api/ingest'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(token_env, "cmw_live_ambient_secret")
    monkeypatch.setenv("CODE_MOWER_CLOUD_TEAM_ID", "stored-team")
    monkeypatch.setenv("CODE_MOWER_INSTALL_ID", "codex-code-mower")
    monkeypatch.setenv("CODE_MOWER_CLOUD_ENDPOINT", "https://attacker.example/api")

    ambient = resolve_cloud_token(
        token_env=token_env,
        token_dir=token_dir,
        install_id="codex-code-mower",
    )

    # The ambient values mirror the asserted identities, so only the source
    # discriminates a reflected environment from the stored install profile.
    assert ambient.source == "env"
    assert ambient.team_id == "stored-team"
    assert ambient.install_id == "codex-code-mower"
    assert ambient.endpoint == "https://attacker.example/api"

    monkeypatch.delenv(token_env)
    monkeypatch.delenv("CODE_MOWER_CLOUD_ENDPOINT")
    selected = resolve_cloud_token(
        token_env=token_env,
        token_dir=token_dir,
        install_id="codex-code-mower",
    )

    assert selected.source == "install_id"
    assert selected.token == "cmw_live_install_secret"
    assert selected.endpoint == "https://codemower.com/api/ingest"


def test_cloud_token_resolver_refuses_ambiguous_profiles(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_AMBIGUOUS_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    for name in ("one.env", "two.env"):
        (token_dir / name).write_text(
            f"export {token_env}='cmw_live_{name}_secret'\n",
            encoding="utf-8",
        )
    monkeypatch.delenv(token_env, raising=False)

    resolution = resolve_cloud_token(token_env=token_env, token_dir=token_dir)
    encoded = json.dumps(resolution.safe_detail())

    assert resolution.status == "ambiguous"
    assert resolution.token == ""
    assert resolution.token_files == ("one.env", "two.env")
    assert "cmw_live_" not in encoded


def test_cloud_token_resolver_rejects_wrong_env_file(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_EXPECTED_TOKEN"
    token_file = tmp_path / "token.env"
    wrong_token = "cmw_live_" + "wrong_secret"
    token_file.write_text(
        f"export OTHER_TOKEN='{wrong_token}'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(token_env, raising=False)

    resolution = resolve_cloud_token(token_env=token_env, token_file=token_file)
    encoded = json.dumps(resolution.safe_detail()) + resolution.message

    assert resolution.status == "malformed"
    assert resolution.token == ""
    assert "OTHER_TOKEN" not in encoded
    assert wrong_token not in encoded


def test_cloud_token_resolver_uses_current_profile(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_CURRENT_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "one.env").write_text(
        f"export {token_env}='cmw_live_one_secret'\n",
        encoding="utf-8",
    )
    (token_dir / "two.env").write_text(
        f"export {token_env}='cmw_live_two_secret'\n",
        encoding="utf-8",
    )
    (token_dir / CURRENT_PROFILE_FILENAME).write_text("two.env\n", encoding="utf-8")
    monkeypatch.delenv(token_env, raising=False)

    resolution = resolve_cloud_token(token_env=token_env, token_dir=token_dir)

    assert resolution.status == "ok"
    assert resolution.source == "current_profile"
    assert resolution.token == "cmw_live_two_secret"


def test_cloud_event_args_accept_jsonl_and_normalize_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_file = root / "events.jsonl"
        event_file.write_text(
            "\n".join(
                [
                    json.dumps({"repo_slug": "owner/repo", "status": "observed"}),
                    json.dumps(
                        {
                            "event_type": "workflow_run",
                            "repo_slug": "owner/repo",
                            "status": "success",
                            "dimensions": {"workflow_name": "CI"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        parsed = parse_event_args([f"reviewer_run={event_file}"])

        assert len(parsed) == 2
        assert parsed[0]["schema"] == EVENT_SCHEMA
        assert parsed[0]["event_type"] == "reviewer_run"
        assert parsed[1]["event_type"] == "workflow_run"


def test_cloud_event_normalization_rejects_unsafe_metadata() -> None:
    try:
        normalize_event(
            {
                "repo_slug": "owner/repo",
                "status": "observed",
                "dimensions": {"output_preview": "secret-ish"},
            },
            "reviewer_run",
        )
    except CloudBundleError as exc:
        assert "unsafe field" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsafe metadata rejection")


def test_cloud_event_boundary_accepts_normalized_event() -> None:
    event = normalize_event(
        {
            "event_type": "reviewer_run",
            "repo_slug": "owner/repo",
            "provider": "codex",
            "status": "pass",
            "metrics": {"wall_seconds": 12.0},
            "dimensions": {"lane_id": "codex-audit"},
        },
        "reviewer_run",
    )

    validated = validate_cloud_event(event)

    assert validated is event
    assert event["schema"] == EVENT_SCHEMA
    assert isinstance(event["tool"], dict)


def test_cloud_event_boundary_rejects_secret_like_value() -> None:
    try:
        normalize_event(
            {
                "repo_slug": "owner/repo",
                "status": "observed",
                "dimensions": {"note": "GITHUB_" + "TOKEN=ghp_hidden_value"},
            },
            "reviewer_run",
        )
    except CloudBundleError as exc:
        assert "secret-like value" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected secret-like metadata rejection")


def test_board_snapshot_event_summarizes_without_local_private_fields() -> None:
    event = build_board_snapshot_event(
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        snapshot=_board_snapshot_fixture(),
    )

    serialized = json.dumps(event)
    assert event["schema"] == EVENT_SCHEMA
    assert event["event_type"] == "board_snapshot"
    assert event["provider"] == "code-mower"
    assert event["lens"] == "board"
    assert event["metrics"]["open_pr_count"] == 1
    assert event["metrics"]["owner_queue_count"] == 1
    assert event["metrics"]["agent_card_count"] == 1
    assert event["dimensions"]["snapshot_schema"] == "code_mower.cloudBoardSnapshot.v1"
    assert event["dimensions"]["pull_requests"][0]["head_sha_prefix"] == "abcdef012345"
    assert event["dimensions"]["agent_cards"][0]["provider"] == "codex"
    assert event["dimensions"]["timelines"]["spend_groups"][0]["total_tokens"] == 1000
    assert event["metrics"]["supervised_open_pr_count"] == 1
    assert event["metrics"]["supervised_ready_issue_count"] == 1
    assert event["dimensions"]["supervised_pilot"]["decision"]["decision_state"] == "ready_for_merge"
    assert event["dimensions"]["supervised_pilot"]["queue"]["active_lanes"] == {"codex": 1}
    assert event["dimensions"]["supervised_pilot"]["active_issues"][0]["builder_lane"] == "claude"
    assert "sensitive local PR title" not in serialized
    assert "super private supervised PR title" not in serialized
    assert "super private supervised issue title" not in serialized
    assert "private owner note" not in serialized
    assert "private prompt fragment" not in serialized
    assert "gate_rerun_command" not in serialized
    assert "/tmp/private/checkout" not in serialized
    assert "pid" not in serialized
    assert "abcdef0123456789abcdef" not in serialized


def test_board_snapshot_event_keeps_local_cards_when_github_unavailable() -> None:
    event = build_board_snapshot_event(
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        snapshot={
            "schema": "code_mower.laneStatus.v1",
            "repo": "owner/repo",
            "generated_at": "2026-09-02T12:00:00Z",
            "next_action": "waiting for remote status",
            "board": {"schema": "code_mower.board.v1"},
            "remote": {"available": False},
            "agent_adapters": {
                "agents": [
                    {
                        "provider": "claude",
                        "role": "reviewer",
                        "status": "running",
                        "lane": "claude-audit",
                        "cwd": "local-private-checkout",
                        "pid": 12345,
                    }
                ]
            },
            "timelines": {"spend": {"available": False}},
        },
    )

    serialized = json.dumps(event)
    assert event["dimensions"]["remote_available"] is False
    assert event["metrics"]["open_pr_count"] == 0
    assert event["metrics"]["agent_card_count"] == 1
    assert event["dimensions"]["agent_cards"][0]["provider"] == "claude"
    assert "local-private-checkout" not in serialized
    assert '"pid"' not in serialized


def test_board_snapshot_upload_dry_run_exports_one_metadata_event(monkeypatch, tmp_path) -> None:
    fixture = _board_snapshot_fixture()
    timelines = fixture["timelines"]
    fixture_without_timelines = dict(fixture)
    fixture_without_timelines.pop("timelines")
    monkeypatch.setattr(cloud_operations.board, "status_payload", lambda _config: fixture_without_timelines)
    monkeypatch.setattr(cloud_operations.board, "timelines_payload", lambda _config: timelines)

    result = cloud_operations.board_snapshot_upload(
        repo_path=tmp_path,
        output_dir=tmp_path / "board-snapshot",
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        endpoint="http://localhost:3000/api/ingest",
        token_env="CODE_MOWER_TEST_BOARD_TOKEN",
        yes=False,
        timeout=0.1,
    )

    payload = build_upload_payload(bundle_dir=tmp_path / "board-snapshot")
    assert result["status"] == "dry_run"
    assert result["event_count"] == 1
    assert result["export"]["event_types"] == {"board_snapshot": 1}
    assert result["upload"]["event_types"] == {"board_snapshot": 1}
    assert result["upload"]["report_count"] == 0
    assert payload["events"][0]["event_type"] == "board_snapshot"


def test_board_snapshot_upload_posts_with_explicit_yes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cloud_operations.board, "status_payload", lambda _config: _board_snapshot_fixture())
    monkeypatch.setattr(cloud_operations.board, "timelines_payload", lambda _config: {})
    monkeypatch.setenv("CODE_MOWER_TEST_BOARD_TOKEN", "cmw_live_board_secret")
    captured: dict[str, object] = {}

    def fake_post_upload_payload(**kwargs):
        captured["token"] = kwargs["token"]
        captured["payload"] = kwargs["payload"]
        return {"mode": "cloud-upload", "status": 200, "response": {"ok": True}}

    monkeypatch.setattr(cloud_operations, "post_upload_payload", fake_post_upload_payload)

    result = cloud_operations.board_snapshot_upload(
        repo_path=tmp_path,
        output_dir=tmp_path / "board-snapshot",
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        endpoint="https://codemower.com/api/ingest",
        token_env="CODE_MOWER_TEST_BOARD_TOKEN",
        yes=True,
        timeout=0.1,
    )

    serialized = json.dumps(result)
    assert result["status"] == "uploaded"
    assert captured["token"] == "cmw_live_board_secret"
    assert captured["payload"]["events"][0]["event_type"] == "board_snapshot"
    assert "cmw_live_board_secret" not in serialized


def _init_git_checkout(path: Path, *, executable: bool = False) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=path, check=True)
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    if executable:
        script = path / "tool.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        subprocess.run(["git", "add", "tool.sh"], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "first"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _board_snapshot_dry_run(monkeypatch, repo_path: Path, output_dir: Path, **kwargs):
    monkeypatch.setattr(
        cloud_operations.board,
        "status_payload",
        kwargs.pop("status_payload", lambda _config: _board_snapshot_fixture()),
    )
    monkeypatch.setattr(cloud_operations.board, "timelines_payload", lambda _config: {})
    return cloud_operations.board_snapshot_upload(
        repo_path=repo_path,
        output_dir=output_dir,
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        endpoint="http://localhost:3000/api/ingest",
        token_env="CODE_MOWER_TEST_BOARD_TOKEN",
        yes=kwargs.pop("yes", False),
        timeout=0.1,
        **kwargs,
    )


def test_board_snapshot_reports_producer_owned_manifest_identity(monkeypatch, tmp_path) -> None:
    output_dir = tmp_path / "board-snapshot"
    result = _board_snapshot_dry_run(monkeypatch, tmp_path, output_dir)

    manifest_bytes = (output_dir / BUNDLE_MANIFEST_FILENAME).read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    identity = result["manifest"]
    assert identity["schema"] == UPLOAD_IDENTITY_SCHEMA
    assert identity["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert identity["event_count"] == 1
    assert identity["event_type_counts"] == {"board_snapshot": 1}
    assert identity["event_ids"] == [manifest["events"][0]["event_id"]]
    assert result["upload"]["event_types"] == {"board_snapshot": 1}


def test_manifest_identity_detects_a_same_shape_substitution(monkeypatch, tmp_path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = _board_snapshot_dry_run(monkeypatch, tmp_path, first_dir)
    second = _board_snapshot_dry_run(monkeypatch, tmp_path, second_dir)

    assert first["manifest"]["event_type_counts"] == second["manifest"]["event_type_counts"]
    assert first["manifest"]["event_count"] == second["manifest"]["event_count"]
    # A different bundle of the very same shape is still a different manifest:
    # a validator comparing producer-owned identity rejects the substitution.
    assert first["manifest"]["event_ids"] != second["manifest"]["event_ids"]
    assert first["manifest"]["manifest_sha256"] != second["manifest"]["manifest_sha256"]

    swapped = read_bundle_manifest(second_dir)
    identity = bundle_manifest_identity(*swapped)
    assert identity == second["manifest"]
    assert identity != first["manifest"]


def test_manifest_identity_rejects_malformed_and_repeated_event_rows() -> None:
    base = {"schema": "code_mower.cloudBundle.v1", "events": []}
    row = {"event_id": "evt-1", "event_type": "board_snapshot"}

    with assert_raises(CloudBundleError):
        bundle_manifest_identity({**base, "events": {}}, b"{}")
    with assert_raises(CloudBundleError):
        bundle_manifest_identity({**base, "events": [row, "board_snapshot"]}, b"{}")
    with assert_raises(CloudBundleError):
        bundle_manifest_identity({**base, "events": [row, dict(row)]}, b"{}")
    with assert_raises(CloudBundleError):
        bundle_manifest_identity(
            {**base, "events": [{"event_id": "", "event_type": "board_snapshot"}]},
            b"{}",
        )


def test_board_snapshot_records_and_enforces_source_git_provenance(monkeypatch, tmp_path) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)

    result = _board_snapshot_dry_run(
        monkeypatch,
        repo_path,
        tmp_path / "clean",
        require_head_sha=head_sha,
        require_clean=True,
    )
    assert result["git"] == {
        "available": True,
        "head_sha": head_sha,
        "clean": True,
        "dirty_entry_count": 0,
    }
    manifest = json.loads(
        (tmp_path / "clean" / BUNDLE_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    event = manifest["events"][0]
    assert event["dimensions"]["source_git"] == {
        "available": True,
        "head_sha": head_sha,
        "clean": True,
        "dirty_entry_count": 0,
    }

    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(
            monkeypatch,
            repo_path,
            tmp_path / "wrong-head",
            require_head_sha="0" * 40,
            require_clean=True,
        )

    (repo_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(
            monkeypatch,
            repo_path,
            tmp_path / "dirty",
            require_head_sha=head_sha,
            require_clean=True,
        )
    subprocess.run(["git", "checkout", "--", "tracked.txt"], cwd=repo_path, check=True)

    (repo_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(
            monkeypatch,
            repo_path,
            tmp_path / "untracked",
            require_head_sha=head_sha,
            require_clean=True,
        )
    (repo_path / "untracked.txt").unlink()


def test_board_snapshot_rejects_a_checkout_that_moves_during_collection(monkeypatch, tmp_path) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)
    mutations: list[int] = []

    def moving_status(_config):
        (repo_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
        mutations.append(
            subprocess.run(
                ["git", "add", "tracked.txt"],
                cwd=repo_path,
                capture_output=True,
            ).returncode
        )
        return _board_snapshot_fixture()

    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(
            monkeypatch,
            repo_path,
            tmp_path / "moved",
            status_payload=moving_status,
            require_head_sha=head_sha,
            require_clean=True,
        )
    assert mutations == [0]


def test_board_snapshot_reads_only_the_materialized_exact_commit(monkeypatch, tmp_path) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)
    sources: list[Path] = []
    observed: list[str] = []
    write_failures: list[str] = []
    metadata_paths: list[dict[str, str]] = []

    def substituting_status(config):
        tracked = repo_path / "tracked.txt"
        restored = tracked.read_text(encoding="utf-8")
        # A tracked file is changed and restored directly in the original
        # checkout, which no index lock prevents, while the data is read.
        tracked.write_text("substituted\n", encoding="utf-8")
        source = Path(config.repo_path)
        sources.append(source)
        observed.append((source / "tracked.txt").read_text(encoding="utf-8"))
        metadata_paths.append(cloud_operations.board.resolved_metadata_paths(config))
        try:
            (source / "tracked.txt").write_text("mutated\n", encoding="utf-8")
        except OSError as exc:
            write_failures.append(type(exc).__name__)
        tracked.write_text(restored, encoding="utf-8")
        return _board_snapshot_fixture()

    result = _board_snapshot_dry_run(
        monkeypatch,
        repo_path,
        tmp_path / "materialized",
        status_payload=substituting_status,
        require_head_sha=head_sha,
        require_clean=True,
    )

    # Collection can only observe the exact materialized commit, never the
    # substituted content that existed in the original at the same moment.
    assert observed == ["one\n"]
    assert sources and sources[0] != repo_path
    # Mutating the private materialization is refused for the whole interval.
    assert write_failures == ["PermissionError"]
    # The live Code Mower metadata inputs stay bound to the original checkout.
    assert metadata_paths and all(
        str(repo_path) in path for path in metadata_paths[0].values()
    )
    assert result["git"] == {
        "available": True,
        "head_sha": head_sha,
        "clean": True,
        "dirty_entry_count": 0,
    }
    # The private materialization is removed once collection is over.
    assert not sources[0].exists()


def test_board_snapshot_materializes_from_a_repository_subdirectory(monkeypatch, tmp_path) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)
    nested = repo_path / "nested"
    nested.mkdir()
    sources: list[Path] = []
    observed_tracked: list[str] = []

    def recording_status(config):
        source = Path(config.repo_path)
        sources.append(source)
        observed_tracked.append((source / "tracked.txt").read_text(encoding="utf-8"))
        return _board_snapshot_fixture()

    # Git discovers the enclosing repository from a subdirectory, so strict
    # collection must materialize the commit from the repository root.
    result = _board_snapshot_dry_run(
        monkeypatch,
        nested,
        tmp_path / "nested-out",
        status_payload=recording_status,
        require_head_sha=head_sha,
        require_clean=True,
    )
    assert sources and sources[0] != nested
    assert observed_tracked == ["one\n"]
    assert result["git"]["head_sha"] == head_sha


def _strict_collection_inputs(monkeypatch, repo_path: Path, output_dir: Path, **kwargs):
    """Collect strictly and report what the collection source actually was."""

    collected: dict[str, object] = {}

    def recording_status(config):
        source = Path(config.repo_path)
        collected["source"] = source
        collected["metadata"] = cloud_operations.board.resolved_metadata_paths(config)
        collected["tracked"] = (source / "tracked.txt").read_text(encoding="utf-8")
        collected["modes"] = {
            path.name: stat.S_IMODE(path.stat().st_mode)
            for path in sorted(source.glob("*"))
        }
        return _board_snapshot_fixture()

    result = _board_snapshot_dry_run(
        monkeypatch,
        repo_path,
        output_dir,
        status_payload=recording_status,
        **kwargs,
    )
    collected["result"] = result
    return collected


def test_strict_board_snapshot_inputs_are_identical_from_root_and_nested_paths(
    monkeypatch, tmp_path
) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)
    nested = repo_path / "nested"
    nested.mkdir()

    from_root = _strict_collection_inputs(
        monkeypatch,
        repo_path,
        tmp_path / "root-out",
        require_head_sha=head_sha,
        require_clean=True,
    )
    from_nested = _strict_collection_inputs(
        monkeypatch,
        nested,
        tmp_path / "nested-out",
        require_head_sha=head_sha,
        require_clean=True,
    )

    # The same repository and commit must yield the same live metadata inputs
    # and the same exact provenance from either invocation directory.
    assert from_nested["metadata"] == from_root["metadata"]
    assert all(
        str(repo_path.resolve()) in path for path in from_root["metadata"].values()
    )
    assert from_nested["tracked"] == from_root["tracked"] == "one\n"
    assert from_root["source"] != repo_path and from_nested["source"] != nested
    expected_git = {
        "available": True,
        "head_sha": head_sha,
        "clean": True,
        "dirty_entry_count": 0,
    }
    assert from_root["result"]["git"] == from_nested["result"]["git"] == expected_git
    assert not Path(from_root["source"]).exists()
    assert not Path(from_nested["source"]).exists()


def test_strict_board_snapshot_keeps_explicit_metadata_paths_from_a_nested_path(
    monkeypatch, tmp_path
) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)
    nested = repo_path / "nested"
    nested.mkdir()
    explicit_store = tmp_path / "explicit-store.json"

    collected = _strict_collection_inputs(
        monkeypatch,
        nested,
        tmp_path / "explicit-out",
        store_path=explicit_store,
        require_head_sha=head_sha,
        require_clean=True,
    )

    assert collected["metadata"]["store_path"] == str(explicit_store)


def test_strict_board_snapshot_preserves_tracked_executable_modes(
    monkeypatch, tmp_path
) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path, executable=True)
    nested = repo_path / "nested"
    nested.mkdir()

    collected = _strict_collection_inputs(
        monkeypatch,
        nested,
        tmp_path / "modes-out",
        require_head_sha=head_sha,
        require_clean=True,
    )

    modes = collected["modes"]
    # A tracked executable stays executable, no path stays writable, and the
    # materialization is still exactly the required clean commit.
    assert modes["tool.sh"] & 0o111
    assert not modes["tool.sh"] & 0o222
    assert not modes["tracked.txt"] & 0o222
    assert modes["tracked.txt"] & 0o400
    assert collected["result"]["git"]["head_sha"] == head_sha
    assert collected["result"]["git"]["clean"] is True
    assert not Path(collected["source"]).exists()


def test_strict_board_snapshot_cleans_up_a_materialization_with_executables(
    monkeypatch, tmp_path
) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path, executable=True)
    nested = repo_path / "nested"
    nested.mkdir()
    monkeypatch.setattr(cloud_operations, "post_upload_payload", _refusing_post)
    sources: list[Path] = []

    def failing_status(config):
        sources.append(Path(config.repo_path))
        raise CloudBundleError("collection failed")

    # A failure during collection removes the read-only materialization,
    # executable bits and all, and never reaches the network.
    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(
            monkeypatch,
            nested,
            tmp_path / "exec-failure",
            status_payload=failing_status,
            require_head_sha=head_sha,
            require_clean=True,
            yes=True,
        )
    assert sources and not sources[0].exists()


def test_board_snapshot_materialization_is_cleaned_up_after_a_failure(monkeypatch, tmp_path) -> None:
    repo_path = tmp_path / "checkout"
    head_sha = _init_git_checkout(repo_path)
    sources: list[Path] = []

    def failing_status(config):
        sources.append(Path(config.repo_path))
        raise CloudBundleError("collection failed")

    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(
            monkeypatch,
            repo_path,
            tmp_path / "failed",
            status_payload=failing_status,
            require_head_sha=head_sha,
            require_clean=True,
        )
    assert sources and not sources[0].exists()


def test_non_strict_board_snapshot_reads_the_original_checkout(monkeypatch, tmp_path) -> None:
    repo_path = tmp_path / "checkout"
    _init_git_checkout(repo_path)
    sources: list[Path] = []

    def recording_status(config):
        sources.append(Path(config.repo_path))
        return _board_snapshot_fixture()

    _board_snapshot_dry_run(
        monkeypatch,
        repo_path,
        tmp_path / "plain",
        status_payload=recording_status,
    )
    assert sources == [repo_path.resolve()]


def test_board_snapshot_rejects_a_manifest_replaced_after_export(monkeypatch, tmp_path) -> None:
    substitute_dir = tmp_path / "substitute"
    substitute = _board_snapshot_dry_run(monkeypatch, tmp_path, substitute_dir)
    replacement = (substitute_dir / BUNDLE_MANIFEST_FILENAME).read_bytes()

    output_dir = tmp_path / "board-snapshot"
    real_doctor = cloud_operations.run_cloud_doctor

    def replacing_doctor(**kwargs):
        (output_dir / BUNDLE_MANIFEST_FILENAME).write_bytes(replacement)
        return real_doctor(**kwargs)

    monkeypatch.setattr(cloud_operations, "run_cloud_doctor", replacing_doctor)
    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(monkeypatch, tmp_path, output_dir)
    assert substitute["manifest"]["manifest_sha256"] == hashlib.sha256(replacement).hexdigest()


def test_cloud_export_owns_the_identity_of_the_manifest_bytes_it_writes(tmp_path) -> None:
    output_dir = tmp_path / "bundle"
    export = build_cloud_bundle(
        reports=[],
        events=[
            {
                "event_type": "dogfood_upload",
                "repo_slug": "owner/repo",
                "dimensions": {"lane": "unit-test"},
            }
        ],
        output_dir=output_dir,
        repo_slug="owner/repo",
    )
    manifest_bytes = (output_dir / BUNDLE_MANIFEST_FILENAME).read_bytes()
    assert export["manifest_identity"] == bundle_manifest_identity(
        json.loads(manifest_bytes.decode("utf-8")),
        manifest_bytes,
    )


def test_resolve_cloud_identity_rejects_a_profile_that_disagrees_with_explicit_values(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CODE_MOWER_CLOUD_TEAM_ID", raising=False)
    monkeypatch.delenv("CODE_MOWER_INSTALL_ID", raising=False)
    resolution = CloudTokenResolution(
        status="ok",
        token_env="CODE_MOWER_CLOUD_TOKEN",
        source="install_id",
        token="cmw_live_secret",
        team_id="stored-team",
        install_id="stored-install",
    )

    assert resolve_cloud_identity(
        team_id="stored-team",
        install_id="stored-install",
        resolution=resolution,
    ) == ("stored-team", "stored-install")

    with assert_raises(CloudBundleError):
        resolve_cloud_identity(
            team_id="other-team",
            install_id="stored-install",
            resolution=resolution,
        )
    with assert_raises(CloudBundleError):
        resolve_cloud_identity(
            team_id="stored-team",
            install_id="other-install",
            resolution=resolution,
        )

    # A selected profile that records no team identity cannot authorize an
    # explicitly identified payload with its own token.
    partial = CloudTokenResolution(
        status="ok",
        token_env="CODE_MOWER_CLOUD_TOKEN",
        source="install_id",
        token="cmw_live_secret",
        install_id="stored-install",
    )
    with assert_raises(CloudBundleError):
        resolve_cloud_identity(
            team_id="explicit-team",
            install_id="stored-install",
            resolution=partial,
        )
    identityless = CloudTokenResolution(
        status="ok",
        token_env="CODE_MOWER_CLOUD_TOKEN",
        source="install_id",
        token="cmw_live_secret",
    )
    with assert_raises(CloudBundleError):
        resolve_cloud_identity(
            team_id="explicit-team",
            install_id="explicit-install",
            resolution=identityless,
        )
    # An anonymous caller asserting no identity keeps working.
    assert resolve_cloud_identity(
        team_id="",
        install_id="",
        resolution=identityless,
    ) == ("", "")
    # A legacy environment-sourced token is not a selected profile, so it still
    # leaves explicit values in force.
    legacy = CloudTokenResolution(
        status="ok",
        token_env="CODE_MOWER_CLOUD_TOKEN",
        source="env",
        token="cmw_live_secret",
    )
    assert resolve_cloud_identity(
        team_id="explicit-team",
        install_id="explicit-install",
        resolution=legacy,
    ) == ("explicit-team", "explicit-install")


def test_board_snapshot_refuses_to_preview_a_conflicting_profile_identity(
    monkeypatch, tmp_path
) -> None:
    conflicting = CloudTokenResolution(
        status="ok",
        token_env="CODE_MOWER_TEST_BOARD_TOKEN",
        source="install_id",
        token="cmw_live_board_secret",
        endpoint="http://localhost:3000/api/ingest",
        team_id="stored-team",
        install_id="stored-install",
    )
    monkeypatch.setattr(
        cloud_operations,
        "_resolve_upload_profile",
        lambda **_kwargs: (conflicting, "http://localhost:3000/api/ingest"),
    )
    output_dir = tmp_path / "conflicting"

    with assert_raises(CloudBundleError):
        _board_snapshot_dry_run(monkeypatch, tmp_path, output_dir)
    assert not output_dir.exists()


def _board_resolution(team_id: str = "", install_id: str = "") -> CloudTokenResolution:
    return CloudTokenResolution(
        status="ok",
        token_env="CODE_MOWER_TEST_BOARD_TOKEN",
        source="install_id",
        token="cmw_live_board_secret",
        endpoint="http://localhost:3000/api/ingest",
        team_id=team_id,
        install_id=install_id,
    )


def _board_profile_sequence(monkeypatch, *resolutions: CloudTokenResolution) -> None:
    """Resolve the stored install profile differently on each producer call."""

    remaining = list(resolutions)

    def _resolve(**_kwargs):
        resolution = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return resolution, "http://localhost:3000/api/ingest"

    monkeypatch.setattr(cloud_operations, "_resolve_upload_profile", _resolve)


def _refusing_post(*_args, **_kwargs):
    raise AssertionError("must refuse before any network upload")


def test_board_snapshot_accepts_a_stable_matching_profile(monkeypatch, tmp_path) -> None:
    matching = _board_resolution(team_id="team", install_id="install")
    _board_profile_sequence(monkeypatch, matching, matching)
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        cloud_operations,
        "post_upload_payload",
        lambda *, payload, endpoint, token, timeout: (
            posted.append(payload) or {"status": 200, "endpoint": endpoint}
        ),
    )

    preview = _board_snapshot_dry_run(monkeypatch, tmp_path, tmp_path / "preview")
    assert preview["status"] == "dry_run"
    assert posted == []

    applied = _board_snapshot_dry_run(monkeypatch, tmp_path, tmp_path / "applied", yes=True)
    assert applied["status"] == "uploaded"
    assert len(posted) == 1
    assert posted[0]["team_id"] == "team"
    assert posted[0]["install_id"] == "install"


def test_board_snapshot_rejects_a_profile_replaced_after_preflight(monkeypatch, tmp_path) -> None:
    matching = _board_resolution(team_id="team", install_id="install")
    replacements = (
        _board_resolution(team_id="other-team", install_id="other-install"),
        _board_resolution(install_id="install"),
        _board_resolution(),
    )
    monkeypatch.setattr(cloud_operations, "post_upload_payload", _refusing_post)

    for index, replacement in enumerate(replacements):
        for applied in (False, True):
            _board_profile_sequence(monkeypatch, matching, replacement)
            output_dir = tmp_path / f"replaced-{index}-{applied}"
            with assert_raises(CloudBundleError) as caught:
                _board_snapshot_dry_run(monkeypatch, tmp_path, output_dir, yes=applied)
            message = str(caught.exception)
            assert "profile" in message
            # Nothing protected reaches the error text.
            for secret in ("cmw_live_board_secret", "localhost:3000", "other-team", "other-install"):
                assert secret not in message


def _generic_upload_bundle(tmp_path: Path, *, team_id: str, install_id: str) -> Path:
    output_dir = tmp_path / f"bundle-{team_id or 'anon'}-{install_id or 'anon'}"
    build_cloud_bundle(
        reports=[],
        events=[
            {
                "event_type": "dogfood_upload",
                "repo_slug": "owner/repo",
                "dimensions": {"lane": "unit-test"},
            }
        ],
        output_dir=output_dir,
        repo_slug="owner/repo",
        team_id=team_id,
        install_id=install_id,
        anonymous=False,
    )
    return output_dir


def _run_generic_upload(
    monkeypatch, bundle_dir: Path, *, resolution, applied: bool
) -> tuple[int, str]:
    monkeypatch.delenv("CODE_MOWER_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("CODE_MOWER_CLOUD_ENDPOINT", raising=False)
    monkeypatch.setattr(cloud_cli, "resolve_cloud_token", lambda **_kwargs: resolution)
    argv = [
        "upload",
        str(bundle_dir),
        "--endpoint",
        "http://localhost:3000/api/ingest",
        "--json",
    ]
    if applied:
        argv.append("--yes")
    stderr = StringIO()
    with redirect_stdout(StringIO()), redirect_stderr(stderr):
        return cloud_cli.main(argv), stderr.getvalue()


def test_generic_cloud_upload_accepts_a_matching_stored_profile(monkeypatch, tmp_path) -> None:
    bundle_dir = _generic_upload_bundle(tmp_path, team_id="team", install_id="install")
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        cloud_cli,
        "post_upload_payload",
        lambda *, payload, endpoint, token, timeout: (
            posted.append(payload) or {"status": 200, "endpoint": endpoint}
        ),
    )
    resolution = _board_resolution(team_id="team", install_id="install")

    assert _run_generic_upload(
        monkeypatch, bundle_dir, resolution=resolution, applied=False
    ) == (0, "")
    assert posted == []
    assert _run_generic_upload(
        monkeypatch, bundle_dir, resolution=resolution, applied=True
    ) == (0, "")
    assert len(posted) == 1


def test_generic_cloud_upload_rejects_a_substituted_stored_profile(monkeypatch, tmp_path) -> None:
    bundle_dir = _generic_upload_bundle(tmp_path, team_id="team", install_id="install")
    monkeypatch.setattr(cloud_cli, "post_upload_payload", _refusing_post)

    for resolution in (
        _board_resolution(team_id="other-team", install_id="other-install"),
        _board_resolution(install_id="install"),
        _board_resolution(),
    ):
        for applied in (False, True):
            code, stderr = _run_generic_upload(
                monkeypatch, bundle_dir, resolution=resolution, applied=applied
            )
            assert code == 1
            assert "profile" in stderr
            for secret in (
                "cmw_live_board_secret",
                "localhost:3000",
                "other-team",
                "other-install",
            ):
                assert secret not in stderr


def test_generic_cloud_upload_keeps_anonymous_bundles_working(monkeypatch, tmp_path) -> None:
    bundle_dir = _generic_upload_bundle(tmp_path, team_id="", install_id="")
    monkeypatch.setattr(cloud_cli, "post_upload_payload", _refusing_post)
    resolution = _board_resolution()

    assert _run_generic_upload(
        monkeypatch, bundle_dir, resolution=resolution, applied=False
    ) == (0, "")


def test_cloud_repo_slug_from_remote_supports_common_github_forms() -> None:
    assert repo_slug_from_remote("git@github.com:codemower-ai/code-mower.git") == "codemower-ai/code-mower"
    assert repo_slug_from_remote("https://github.com/codemower-ai/code-mower.git") == "codemower-ai/code-mower"
    assert repo_slug_from_remote("ssh://example.com/nope") == ""


def test_cloud_export_builds_metadata_only_bundle_from_client_module() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reviewer-value-report.md"
        report.write_text("# Value report\n", encoding="utf-8")

        result = build_cloud_bundle(
            reports=[(report, "value-report")],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "codemower-ai/code-mower",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "tool_version": "0.139.0",
                        "provider": "openai",
                        "model": "gpt-5",
                        "integration": "cli",
                        "lens": "base",
                    },
                    "metrics": {"latency_ms": 42},
                }
            ],
            output_dir=root / "bundle",
            repo_slug="codemower-ai/code-mower",
        )

        assert result["mode"] == "cloud-export"
        assert result["upload_ready"] is True
        assert result["upload_status"] == "ready_for_dry_run"
        assert (root / "bundle" / BUNDLE_MANIFEST_FILENAME).is_file()
        upload = build_upload_payload(bundle_dir=root / "bundle")
        assert upload["upload_mode"] == "metadata_only"
        assert upload["repo_slug"] == "codemower-ai/code-mower"
        assert upload["events"][0]["provider"] == "codex"
        assert upload["events"][0]["tool"]["tool_name"] == "codex"
        assert upload["events"][0]["tool"]["model"] == "gpt-5"
        assert upload["provenance"]["events_with_tool_provenance"] == 1
        assert upload["provenance"]["events_with_model_provenance"] == 1
        assert upload["provenance"]["events_with_tool_version_provenance"] == 1
        assert upload["provenance"]["tools"][0]["tool_name"] == "codex"


def test_cloud_export_spend_flag_emits_reviewer_run_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spend = root / "reviewer-spend.json"
        spend.write_text(
            json.dumps(
                {
                    "schema": "code_mower.reviewerSpend.v1",
                    "runs": [
                        {
                            "run_id": "spend-run-1",
                            "created_at": "2026-08-16T12:00:00Z",
                            "lane": "claude-audit",
                            "repo": "owner/repo",
                            "pr_number": 7,
                            "head_sha": "def456",
                            "model": "sonnet",
                            "wall_seconds": 9.0,
                            "verdict": "BLOCKED",
                            "input_tokens": 22,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        output_dir = root / "bundle"
        stdout = StringIO()

        with redirect_stdout(stdout):
            code = cloud_cli.main(
                [
                    "export",
                    "--spend",
                    str(spend),
                    "--output-dir",
                    str(output_dir),
                    "--repo-slug",
                    "owner/repo",
                    "--json",
                ]
            )

        assert code == 0
        payload = build_upload_payload(bundle_dir=output_dir)
        event = payload["events"][0]
        assert event["event_type"] == "reviewer_run"
        assert event["provider"] == "claude"
        assert event["metrics"]["wall_seconds"] == 9.0
        assert event["metrics"]["input_tokens"] == 22
        assert event["dimensions"]["head_sha"] == "def456"


def test_cloud_export_defaults_value_report_snapshot_to_code_mower_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        result = build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "value_report_snapshot",
                    "repo_slug": "codemower-ai/code-mower",
                    "source": "unit-test-value-report",
                    "status": "observed",
                    "metrics": {"report_count": 1},
                }
            ],
            output_dir=root / "bundle",
            repo_slug="codemower-ai/code-mower",
        )

        assert result["provenance"]["benchmark_events_missing_model_provenance"] == 0
        assert result["provenance"]["benchmark_events_missing_tool_version_provenance"] == 0
        upload = build_upload_payload(bundle_dir=root / "bundle")
        event = upload["events"][0]
        assert event["provider"] == "code-mower"
        assert event["tool"]["tool_name"] == "code-mower"
        assert event["tool"]["model_source"] == "not_applicable"
        assert event["tool"]["version_source"] == "package_version"


def test_cloud_export_accepts_work_order_provenance_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event = root / "work-order.cloud-event.json"
        event.write_text(
            json.dumps(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "work-order-1",
                    "event_type": "work_order",
                    "created_at": "2026-06-23T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "source": "code-mower-work-order",
                    "provider": "code-mower",
                    "lens": "planning",
                    "status": "drafted",
                    "tool": {
                        "role": "planner",
                        "tool_name": "code-mower",
                        "tool_version": "0.5.0-test",
                        "provider": "code-mower",
                        "model_source": "not_applicable",
                        "version_source": "package_version",
                        "integration": "work-order",
                        "lens": "planning",
                        "source": "code-mower-work-order",
                    },
                    "metrics": {"role_lens_count": 2, "review_lane_count": 1},
                    "dimensions": {
                        "source_type": "github_issue",
                        "issue_repo": "owner/repo",
                        "issue_number": "123",
                        "issue_url": "https://github.com/owner/repo/issues/123",
                    },
                }
            ),
            encoding="utf-8",
        )

        result = build_cloud_bundle(
            reports=[],
            events=parse_event_args([f"work_order={event}"]),
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        assert result["upload_ready"] is True
        assert result["upload_status"] == "ready_for_dry_run"
        assert result["event_types"] == {"work_order": 1}
        upload = build_upload_payload(bundle_dir=root / "bundle")
        assert upload["events"][0]["event_type"] == "work_order"
        assert upload["events"][0]["dimensions"]["issue_number"] == "123"
        assert upload["provenance"]["tools"][0]["tool_name"] == "code-mower"


def test_provider_catalog_snapshot_events_are_metadata_only(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    events = build_provider_catalog_snapshot_events(
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
    )

    assert events
    codex = next(event for event in events if event["dimensions"]["lane_id"] == "codex")
    assert codex["event_type"] == "provider_catalog_snapshot"
    assert codex["repo_slug"] == "owner/repo"
    assert codex["source"] == "unit-test"
    assert codex["tool"]["tool_name"] == "codex"
    assert codex["tool"]["provider"] == "codex"
    assert codex["tool"]["model_source"] == "missing"
    assert codex["tool"]["version_source"] == "missing"
    assert codex["dimensions"]["catalog_snapshot"] is True
    assert codex["dimensions"]["merge_authority"] is True
    assert "token_env" not in codex["dimensions"]
    assert "auth" not in codex["dimensions"]


def test_provenance_summary_treats_vendor_hidden_model_as_known_source() -> None:
    summary = build_provenance_summary(
        [
            normalize_event(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "evt-vendor-hidden",
                    "event_type": "provider_catalog_snapshot",
                    "created_at": "2026-01-01T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "team_id": "team",
                    "install_id": "install",
                    "source": "unit-test",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gitar",
                        "provider": "gitar",
                        "model": "",
                        "model_source": "vendor_hidden",
                        "version_source": "not_probed",
                    },
                },
                "provider_catalog_snapshot",
            )
        ]
    )

    assert summary["events_with_model_provenance"] == 1
    assert summary["events_missing_model_provenance"] == 0
    assert summary["events_with_tool_version_provenance"] == 1
    assert summary["events_missing_tool_version_provenance"] == 0
    assert summary["inventory_event_count"] == 1
    assert summary["benchmark_event_count"] == 0
    assert summary["benchmark_events_with_model_provenance"] == 0
    assert summary["benchmark_events_missing_model_provenance"] == 0
    assert summary["benchmark_model_missing_providers"] == []
    assert summary["tools"][0]["model_sources"] == ["vendor_hidden"]
    assert summary["tools"][0]["version_sources"] == ["not_probed"]


def test_provenance_summary_preserves_source_quality_fields() -> None:
    summary = build_provenance_summary(
        [
            normalize_event(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "evt-code-mower",
                    "event_type": "dogfood_upload",
                    "created_at": "2026-01-01T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "team_id": "team",
                    "install_id": "install",
                    "source": "unit-test",
                    "tool": {
                        "role": "reporter",
                        "tool_name": "code-mower",
                        "tool_version": "0.5.0b34",
                        "provider": "code-mower",
                        "model": "",
                        "model_source": "not_applicable",
                        "version_source": "package_version",
                    },
                },
                "dogfood_upload",
            ),
            normalize_event(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "evt-gemini",
                    "event_type": "provider_catalog_snapshot",
                    "created_at": "2026-01-01T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "team_id": "team",
                    "install_id": "install",
                    "source": "unit-test",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gemini",
                        "tool_version": "0.45.2",
                        "provider": "gemini",
                        "model": "",
                        "model_source": "missing",
                        "version_source": "cli_version_probe",
                    },
                },
                "provider_catalog_snapshot",
            ),
        ]
    )

    assert summary["events_with_model_provenance"] == 1
    assert summary["events_missing_model_provenance"] == 1
    assert summary["events_with_tool_version_provenance"] == 2
    assert summary["events_missing_tool_version_provenance"] == 0
    assert summary["inventory_event_count"] == 1
    assert summary["benchmark_event_count"] == 1
    assert summary["benchmark_events_with_model_provenance"] == 1
    assert summary["benchmark_events_missing_model_provenance"] == 0
    assert summary["benchmark_events_with_tool_version_provenance"] == 1
    assert summary["benchmark_events_missing_tool_version_provenance"] == 0
    assert summary["benchmark_model_missing_providers"] == []
    rows = {row["tool_name"]: row for row in summary["tools"]}
    assert rows["code-mower"]["model_sources"] == ["not_applicable"]
    assert rows["code-mower"]["version_sources"] == ["package_version"]
    assert rows["gemini"]["model_sources"] == ["missing"]
    assert rows["gemini"]["version_sources"] == ["cli_version_probe"]


def test_dogfood_dry_run_preserves_version_probe(monkeypatch, tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    spend_dir = tmp_path / ".code-mower"
    spend_dir.mkdir()
    (spend_dir / "reviewer-spend.json").write_text(
        json.dumps(
            {
                "schema": "code_mower.reviewerSpend.v1",
                "runs": [
                    {
                        "run_id": "spend-run-1",
                        "created_at": "2026-08-16T12:00:00Z",
                        "lane": "codex-audit",
                        "repo": "owner/repo",
                        "pr_number": 42,
                        "head_sha": "abc123",
                        "model": "gpt-5",
                        "wall_seconds": 7.5,
                        "verdict": "PASS",
                        "total_tokens": 1000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[bool] = []

    def fake_catalog_events(**kwargs):
        calls.append(bool(kwargs["include_version_probe"]))
        return []

    monkeypatch.setattr(
        "code_mower.cloud_client.operations.build_provider_catalog_snapshot_events",
        fake_catalog_events,
    )

    result = dogfood_upload(
        repo_path=tmp_path,
        output_dir=tmp_path / ".code-mower/cloud-dogfood-bundle",
        reports=[],
        events=[],
        spend_path=None,
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        endpoint="https://codemower.com/api/ingest",
        token_env="CODE_MOWER_TEST_EMPTY_TOKEN",
        include_reports=False,
        yes=False,
        timeout=0.1,
    )

    assert result["status"] == "dry_run"
    assert calls == [True]
    assert result["export"]["event_types"]["reviewer_run"] == 1
    assert result["upload"]["event_types"]["reviewer_run"] == 1
    payload = build_upload_payload(bundle_dir=tmp_path / ".code-mower/cloud-dogfood-bundle")
    reviewer_event = next(event for event in payload["events"] if event["event_type"] == "reviewer_run")
    assert reviewer_event["metrics"]["wall_seconds"] == 7.5
    assert reviewer_event["metrics"]["total_tokens"] == 1000
    assert reviewer_event["dimensions"]["head_sha"] == "abc123"


def test_dogfood_upload_doctor_uses_original_token_selector(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_env = "CODE_MOWER_TEST_DOGFOOD_CURRENT_TOKEN"
    token = "cmw_live_dogfood_secret"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "renamed.env").write_text(
        "\n".join(
            [
                f"export {token_env}='{token}'",
                "export CODE_MOWER_INSTALL_ID='profile-install'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (token_dir / CURRENT_PROFILE_FILENAME).write_text("renamed.env\n", encoding="utf-8")
    monkeypatch.delenv(token_env, raising=False)
    monkeypatch.setattr(
        "code_mower.cloud_client.operations.build_provider_catalog_snapshot_events",
        lambda **kwargs: [],
    )
    captured: dict[str, str] = {}

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    def fake_post_upload_payload(**kwargs):
        captured["token"] = kwargs["token"]
        return {
            "mode": "cloud-upload",
            "endpoint": kwargs["endpoint"],
            "status": 200,
            "response": {"ok": True},
        }

    monkeypatch.setattr(cloud_operations, "post_upload_payload", fake_post_upload_payload)

    result = dogfood_upload(
        repo_path=tmp_path,
        output_dir=tmp_path / "bundle",
        reports=[],
        events=[],
        spend_path=None,
        repo_slug="owner/repo",
        team_id="",
        install_id="",
        source="unit-test",
        endpoint="https://codemower.com/api/ingest",
        token_env=token_env,
        token_dir=token_dir,
        include_reports=False,
        yes=True,
        timeout=0.1,
    )

    assert result["status"] == "uploaded"
    assert result["doctor"]["status"] == "pass"
    assert captured["token"] == token


def test_cloud_doctor_runs_from_client_module() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = run_cloud_doctor(
            bundle_dir=Path(tmp) / "missing-bundle",
            endpoint="http://localhost:3000/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        assert report["mode"] == "cloud-doctor"
        assert report["status"] == "pass"
        assert report["warnings"] == 2
        rendered = render_cloud_doctor_text(report)
        assert "Code Mower cloud doctor" in rendered
        assert "http://localhost:3000" in rendered


def test_cloud_doctor_uses_stored_install_profile_after_restart(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_DOCTOR_STORED_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "codex-code-mower.env").write_text(
        f"export {token_env}='cmw_live_doctor_secret'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(token_env, raising=False)

    build_cloud_bundle(
        reports=[],
        events=[],
        output_dir=tmp_path / "bundle",
        repo_slug="owner/repo",
        team_id="team",
        install_id="codex-code-mower",
        anonymous=False,
    )

    report = run_cloud_doctor(
        bundle_dir=tmp_path / "bundle",
        endpoint="https://codemower.com/api/ingest",
        token_env=token_env,
        token_dir=token_dir,
        install_id="codex-code-mower",
    )
    token_check = next(check for check in report["checks"] if check["name"] == "token")

    assert report["status"] == "pass"
    assert token_check["status"] == "pass"
    assert token_check["detail"]["source"] == "install_id"
    assert token_check["detail"]["shell"].startswith("source /")
    assert "cmw_live_doctor_secret" not in json.dumps(report)


def test_cloud_doctor_reports_ambiguous_profiles_without_secrets(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_DOCTOR_AMBIGUOUS_TOKEN"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    for name in ("one.env", "two.env"):
        (token_dir / name).write_text(
            f"export {token_env}='cmw_live_{name}_doctor_secret'\n",
            encoding="utf-8",
        )
    monkeypatch.delenv(token_env, raising=False)

    report = run_cloud_doctor(
        bundle_dir=tmp_path / "bundle",
        endpoint="https://codemower.com/api/ingest",
        token_env=token_env,
        token_dir=token_dir,
        require_token=False,
    )
    token_check = next(check for check in report["checks"] if check["name"] == "token")
    encoded = json.dumps(report)

    assert report["status"] == "pass"
    assert token_check["status"] == "warn"
    assert token_check["detail"]["token_files"] == ["one.env", "two.env"]
    assert "cmw_live_" not in encoded


def test_cloud_upload_uses_token_file_after_restart(monkeypatch, tmp_path) -> None:
    token_env = "CODE_MOWER_TEST_UPLOAD_STORED_TOKEN"
    token = "cmw_live_upload_secret"
    token_file = tmp_path / "token.env"
    token_file.write_text(f"export {token_env}='{token}'\n", encoding="utf-8")
    monkeypatch.delenv(token_env, raising=False)
    captured: dict[str, str] = {}

    build_cloud_bundle(
        reports=[],
        events=[],
        output_dir=tmp_path / "bundle",
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        anonymous=False,
    )

    def fake_post_upload_payload(**kwargs):
        captured["token"] = kwargs["token"]
        return {
            "mode": "cloud-upload",
            "endpoint": kwargs["endpoint"],
            "status": 200,
            "response": {"ok": True},
        }

    monkeypatch.setattr(cloud_cli, "post_upload_payload", fake_post_upload_payload)
    out = StringIO()
    with redirect_stdout(out):
        status = cloud_cli.main(
            [
                "upload",
                str(tmp_path / "bundle"),
                "--endpoint",
                "https://codemower.com/api/ingest",
                "--token-env",
                token_env,
                "--token-file",
                str(token_file),
                "--yes",
                "--json",
            ]
        )

    assert status == 0
    assert captured["token"] == token
    assert token not in out.getvalue()


def test_cloud_upload_current_profile_not_bundle_install_id(
    monkeypatch,
    tmp_path,
) -> None:
    token_env = "CODE_MOWER_TEST_UPLOAD_CURRENT_TOKEN"
    token = "cmw_live_current_secret"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "actual.env").write_text(
        f"export {token_env}='{token}'\n",
        encoding="utf-8",
    )
    (token_dir / CURRENT_PROFILE_FILENAME).write_text("actual.env\n", encoding="utf-8")
    monkeypatch.delenv(token_env, raising=False)
    captured: dict[str, str] = {}

    build_cloud_bundle(
        reports=[],
        events=[],
        output_dir=tmp_path / "bundle",
        repo_slug="owner/repo",
        team_id="team",
        install_id="bundle-install",
        anonymous=False,
    )

    def fake_post_upload_payload(**kwargs):
        captured["token"] = kwargs["token"]
        return {
            "mode": "cloud-upload",
            "endpoint": kwargs["endpoint"],
            "status": 200,
            "response": {"ok": True},
        }

    monkeypatch.setattr(cloud_cli, "post_upload_payload", fake_post_upload_payload)
    out = StringIO()
    with redirect_stdout(out):
        status = cloud_cli.main(
            [
                "upload",
                str(tmp_path / "bundle"),
                "--endpoint",
                "https://codemower.com/api/ingest",
                "--token-env",
                token_env,
                "--token-dir",
                str(token_dir),
                "--yes",
                "--json",
            ]
        )

    assert status == 0
    assert captured["token"] == token
    assert token not in out.getvalue()


def test_cloud_upload_reports_the_identity_of_the_bytes_it_sends(
    monkeypatch, tmp_path
) -> None:
    token_env = "CODE_MOWER_TEST_UPLOAD_IDENTITY_TOKEN"
    monkeypatch.setenv(token_env, "cmw_live_identity_secret")
    bundle_dir = tmp_path / "bundle"
    build_cloud_bundle(
        reports=[],
        events=[
            build_board_snapshot_event(
                repo_slug="owner/repo",
                team_id="team",
                install_id="install",
                source="unit-test",
                snapshot=_board_snapshot_fixture(),
            )
        ],
        output_dir=bundle_dir,
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        anonymous=False,
    )
    manifest, manifest_bytes = read_bundle_manifest(bundle_dir)
    expected = bundle_manifest_identity(manifest, manifest_bytes)

    def run(*extra: str) -> dict:
        out = StringIO()
        with redirect_stdout(out):
            status = cloud_cli.main(
                [
                    "upload",
                    str(bundle_dir),
                    "--endpoint",
                    "https://codemower.com/api/ingest",
                    "--token-env",
                    token_env,
                    "--json",
                    *extra,
                ]
            )
        assert status == 0
        return json.loads(out.getvalue())

    monkeypatch.setattr(
        cloud_cli,
        "post_upload_payload",
        lambda **kwargs: {
            "mode": "cloud-upload",
            "endpoint": kwargs["endpoint"],
            "status": 200,
            "response": {"ok": True},
        },
    )
    preview = run("--dry-run")
    applied = run("--yes")

    assert preview["manifest"] == expected
    assert applied["manifest"] == expected
    assert expected["schema"] == UPLOAD_IDENTITY_SCHEMA
    assert expected["event_type_counts"] == {"board_snapshot": 1}

    # A same-shape manifest swapped in afterwards reports a different identity.
    substitute = tmp_path / "substitute"
    build_cloud_bundle(
        reports=[],
        events=[
            build_board_snapshot_event(
                repo_slug="owner/repo",
                team_id="team",
                install_id="install",
                source="unit-test",
                snapshot=_board_snapshot_fixture(),
            )
        ],
        output_dir=substitute,
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        anonymous=False,
    )
    (bundle_dir / "code-mower-cloud-bundle.json").write_bytes(
        (substitute / "code-mower-cloud-bundle.json").read_bytes()
    )
    swapped = run("--dry-run")

    assert swapped["manifest"]["event_count"] == expected["event_count"]
    assert swapped["manifest"]["event_type_counts"] == expected["event_type_counts"]
    assert swapped["manifest"]["manifest_sha256"] != expected["manifest_sha256"]
    assert swapped["manifest"]["event_ids"] != expected["event_ids"]


def test_cloud_doctor_warns_when_model_provenance_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "provider": "codex",
                        "tool_version": "0.142.0",
                        "model_source": "missing",
                    },
                }
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert report["status"] == "pass"
        assert check["status"] == "warn"
        assert check["detail"]["providers"] == ["codex"]
        assert check["detail"]["model_env_by_provider"]["codex"] == [
            "CODEX_MODEL",
            "CODE_MOWER_CODEX_MODEL",
            "OPENAI_MODEL",
        ]
        assert check["detail"]["model_env_commands"] == [
            "code-mower providers provenance-env --provider codex --shell"
        ]
        assert "code-mower providers provenance-env --provider codex --shell" in check[
            "remediation"
        ]
        assert "CODE_MOWER_CODEX_MODEL" in check["remediation"]


def test_cloud_doctor_does_not_warn_for_inventory_only_model_gaps() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "provider_catalog_snapshot",
                    "repo_slug": "owner/repo",
                    "provider": "gemini",
                    "lens": "base",
                    "status": "observed",
                    "dimensions": {"catalog_snapshot": True},
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gemini",
                        "provider": "gemini",
                        "tool_version": "0.45.2",
                        "model_source": "missing",
                    },
                }
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert report["status"] == "pass"
        assert check["status"] == "pass"
        assert "benchmark evidence events" in check["message"]
        assert check["detail"]["raw_missing_model_events"] == 1
        assert check["detail"]["benchmark_missing_model_events"] == 0


def test_cloud_doctor_fans_out_multiple_model_provenance_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "provider": "codex",
                        "tool_version": "0.142.0",
                        "model_source": "missing",
                    },
                },
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "gemini",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gemini",
                        "provider": "gemini",
                        "tool_version": "0.45.2",
                        "model_source": "missing",
                    },
                },
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert check["status"] == "warn"
        assert check["detail"]["model_env_commands"] == [
            "code-mower providers provenance-env --provider codex --shell",
            "code-mower providers provenance-env --provider gemini --shell",
        ]
        assert check["detail"]["model_env_command_all"] == (
            "code-mower providers provenance-env --provider codex "
            "--provider gemini --shell"
        )
        assert "detail.model_env_commands" in check["remediation"]
        assert "detail.model_env_command_all" in check["remediation"]


def test_cloud_doctor_passes_when_model_provenance_is_complete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "provider": "codex",
                        "tool_version": "0.142.0",
                        "model": "gpt-5",
                        "model_source": "env",
                    },
                }
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert check["status"] == "pass"


def test_cloud_repo_sync_helpers_live_in_client_module() -> None:
    assert parse_repo_sync_spec("/tmp/repo") == ("", Path("/tmp/repo"))
    assert parse_repo_sync_spec("owner/repo=/tmp/repo") == ("owner/repo", Path("/tmp/repo"))
    assert repo_sync_output_name("Owner/Repo", Path("/tmp/repo"), 2) == "owner--repo-3"


def test_cloud_repo_sync_data_class_summary_separates_sources() -> None:
    summary = cloud_operations.build_repo_sync_data_class_summary(
        [
            {
                "steps": [
                    {
                        "mode": "cloud-dogfood",
                        "export": {"event_count": 16},
                    },
                    {
                        "mode": "cloud-catch-up",
                        "catch_up": {"event_count": 50},
                    },
                    {
                        "mode": "cloud-reviewer-runs",
                        "event_count": 7,
                    },
                ]
            }
        ]
    )

    assert summary["current_dogfood"]["steps"] == 1
    assert summary["current_dogfood"]["events"] == 16
    assert summary["imported_history"]["steps"] == 1
    assert summary["imported_history"]["events"] == 50
    assert summary["imported_history"]["trust_guidance"] == cloud_operations.CATCH_UP_TRUST_GUIDANCE
    assert summary["reviewer_evidence"]["steps"] == 1
    assert summary["reviewer_evidence"]["events"] == 7


def test_cloud_py_keeps_legacy_operation_aliases() -> None:
    assert cloud_cli._dogfood_upload is dogfood_upload
    assert cloud_cli._board_snapshot_upload is cloud_operations.board_snapshot_upload
    assert cloud_cli._repo_sync_output_name("owner/repo", Path("/tmp/repo"), 0) == "owner--repo-1"


def test_cloud_repo_sync_yes_reports_no_events_when_no_steps_upload(monkeypatch, tmp_path) -> None:
    def no_events_step(**kwargs):
        return {
            "mode": "cloud-reviewer-runs",
            "status": "no_events",
            "repo_slug": kwargs["repo_slug"],
        }

    monkeypatch.setattr(cloud_operations, "reviewer_runs_upload", no_events_step)

    result = cloud_operations.repo_sync_upload(
        repo_specs=["owner/repo=/tmp/repo"],
        output_dir=tmp_path / "repo-sync",
        modes=["reviewer-runs"],
        team_id="team",
        install_id="install",
        source_prefix="test",
        limit=1,
        endpoint="http://localhost:3000/api/ingest",
        token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
        include_reports=False,
        include_git_ref=False,
        yes=True,
        timeout=1.0,
    )

    assert result["status"] == "no_events"
    assert result["repos"][0]["steps"][0]["status"] == "no_events"
