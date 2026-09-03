from __future__ import annotations

import json
import subprocess
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from code_mower import controller


NOW = "2026-09-03T12:00:00Z"


def _config() -> dict[str, object]:
    return {
        "version": 1,
        "project": {"name": "demo", "state_dir": ".code-mower"},
        "repositories": [{"slug": "owner/repo", "default_branch": "main"}],
        "owner_surface": {
            "ready_label": "tier:R",
            "needs_owner_label": "needs-owner",
            "builder_wip_cap": "2",
        },
        "merge_authority_excludes_author": True,
        "builder_identity": {
            "labels": {
                "builder:codex": "codex",
                "builder:claude": "claude",
                "builder:cursor": "cursor",
            }
        },
        "lanes": {
            "codex": {
                "type": "audit",
                "driver": "local_cli",
                "provider": "codex",
                "merge_authority": True,
                "labels": {
                    "needs": "needs-codex-audit",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                },
            },
            "claude_audit": {
                "type": "audit",
                "driver": "local_cli",
                "provider": "claude",
                "trailer_lane": "claude",
                "merge_authority": True,
                "labels": {
                    "needs": "needs-claude-audit",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                },
            },
        },
    }


def _without_merge_reviewers() -> dict[str, object]:
    config = _config()
    lanes = config["lanes"]
    assert isinstance(lanes, dict)
    for lane in lanes.values():
        assert isinstance(lane, dict)
        lane["merge_authority"] = False
    return config


def _only_codex_merge_reviewer() -> dict[str, object]:
    config = _config()
    lanes = config["lanes"]
    assert isinstance(lanes, dict)
    claude_lane = lanes["claude_audit"]
    assert isinstance(claude_lane, dict)
    claude_lane["merge_authority"] = False
    return config


def _status(prs: list[dict[str, object]] | None = None, *, available: bool = True) -> dict[str, object]:
    return {
        "schema": "code_mower.laneStatus.v1",
        "repo": "owner/repo",
        "generated_at": NOW,
        "remote": {
            "available": available,
            "errors": [] if available else ["pull_requests: gh pr failed"],
            "pull_requests": prs or [],
            "workflow_runs": [],
            "gate_health": {"status": "pass", "alerts": []},
        },
        "local_boards": {"available": False, "boards": []},
        "local_processes": {"available": False, "processes": []},
        "next_action": "no active lanes",
        "next_detail": "",
    }


def _pr(
    *,
    number: int = 42,
    builder: str = "builder:cursor",
    done: list[str] | None = None,
    needs: list[str] | None = None,
    blocked: list[str] | None = None,
    checks: list[dict[str, str]] | None = None,
    stale: bool = False,
    draft: bool = False,
    merge_state: str = "CLEAN",
    next_action: str = "ready for merge or auto-merge",
    next_detail: str = "",
) -> dict[str, object]:
    return {
        "number": number,
        "url": f"https://github.com/owner/repo/pull/{number}",
        "branch": f"codex/pr-{number}",
        "head_sha": "abcdef0123456789",
        "author": "bot",
        "is_draft": draft,
        "merge_state": merge_state,
        "updated_at": NOW,
        "labels": {
            "builder": [builder],
            "dispatched": [],
            "needs": needs or [],
            "done": done or [],
            "blocked": blocked or [],
        },
        "checks": checks or [{"name": "code-mower/gate", "state": "success"}],
        "stale": stale,
        "next_action": next_action,
        "next_detail": next_detail,
    }


def _options(mode: str = "dry_run", **overrides: object) -> controller.ControllerOptions:
    values = {
        "repo": "owner/repo",
        "mode": mode,
        "gate_required": False,
        "auto_merge_enabled": False,
        "merge_token_ready": False,
        **overrides,
    }
    return controller.ControllerOptions(**values)  # type: ignore[arg-type]


def _evaluate(
    prs: list[dict[str, object]] | None = None,
    *,
    ready_issues: list[dict[str, object]] | None = None,
    mode: str = "dry_run",
    **overrides: object,
) -> dict[str, object]:
    return controller.evaluate_controller_report(
        status_report=_status(prs),
        ready_issues={"available": True, "errors": [], "issues": ready_issues or []},
        config=_config(),
        options=_options(mode, **overrides),
    )


def test_no_work_emits_queue_state_snapshot_event() -> None:
    report = _evaluate([])
    assert report["decision"]["decision_state"] == "no_work"
    assert report["decision"]["next_action"] == "no active lanes"

    event = controller.build_controller_event(report=report, source="test")

    assert event["event_type"] == "queue_state_snapshot"
    assert event["dimensions"]["supervised_pilot_schema"] == controller.SUPERVISED_PILOT_SCHEMA
    assert event["tool"]["role"] == "controller"


def test_ready_issue_selects_one_builder_dispatch_without_mutation() -> None:
    report = _evaluate(
        [],
        ready_issues=[
            {
                "number": 7,
                "url": "https://github.com/owner/repo/issues/7",
                "author": "owner",
                "updated_at": NOW,
                "labels": ["tier:R", "builder:codex"],
                "builder_lane": "codex",
                "assigned": False,
                "dispatched": False,
                "owner_action": False,
            }
        ],
    )

    assert report["decision"]["decision_state"] == "dispatch_builder"
    assert report["decision"]["issue_number"] == 7
    assert report["decision"]["lane_id"] == "codex"
    assert report["decision"]["would_mutate"] is False


def test_blocked_audit_stops_controller() -> None:
    report = _evaluate([_pr(blocked=["claude-audit-blocked"])])

    assert report["decision"]["decision_state"] == "blocked_audit"
    assert report["decision"]["stop_condition"] == "blocked_audit"
    assert controller.build_controller_event(report=report)["status"] == "blocked"


def test_stale_evidence_requeues_before_merge() -> None:
    report = _evaluate(
        [
            _pr(
                needs=["needs-codex-audit"],
                stale=True,
                next_action="requeue stale audit",
                next_detail="stale audit request for codex; check the audit runner/dispatcher",
            )
        ]
    )

    assert report["decision"]["decision_state"] == "stale_evidence"
    assert report["decision"]["next_action"] == "requeue stale audit"
    assert report["decision"]["stop_condition"] == "stale_evidence"


def test_owner_label_becomes_owner_intervention_event() -> None:
    report = _evaluate([_pr(needs=["needs-owner"])])
    event = controller.build_controller_event(report=report)

    assert report["decision"]["decision_state"] == "owner_action"
    assert report["decision"]["owner_action_kind"] == "needs_owner_label"
    assert event["event_type"] == "owner_intervention"
    assert event["dimensions"]["owner_action_kind"] == "needs_owner_label"
    assert event["dimensions"]["would_mutate"] is False
    assert event["dimensions"]["promoted_reviewers_passed"] is False
    assert "issue_number" not in event["dimensions"]


def test_promoted_mode_refuses_when_required_gate_is_not_verified() -> None:
    report = _evaluate(
        [_pr(done=["codex-audit-done", "claude-audit-done"])],
        mode="promoted",
        gate_required=False,
        auto_merge_enabled=True,
        merge_token_ready=True,
    )

    assert report["decision"]["decision_state"] == "owner_action"
    assert report["decision"]["owner_action_kind"] == "required_gate_missing"
    assert report["decision"]["next_action"] == "require code-mower/gate in branch protection"


def test_promoted_mode_refuses_when_no_merge_reviewers_configured() -> None:
    report = controller.evaluate_controller_report(
        status_report=_status([_pr(done=[])]),
        ready_issues={"available": True, "errors": [], "issues": []},
        config=_without_merge_reviewers(),
        options=_options(
            "promoted",
            gate_required=True,
            auto_merge_enabled=True,
            merge_token_ready=True,
        ),
    )

    assert report["decision"]["decision_state"] == "owner_action"
    assert report["decision"]["owner_action_kind"] == "reviewer_lanes_missing"
    assert report["decision"]["stop_condition"] == "reviewer_lanes_missing"


def test_author_exclusion_deadlock_becomes_owner_action_before_waiting() -> None:
    report = controller.evaluate_controller_report(
        status_report=_status([_pr(builder="builder:codex", needs=["needs-codex-audit"])]),
        ready_issues={"available": True, "errors": [], "issues": []},
        config=_only_codex_merge_reviewer(),
        options=_options("manual"),
    )

    assert report["decision"]["decision_state"] == "owner_action"
    assert report["decision"]["owner_action_kind"] == "reviewer_lanes_missing"
    assert report["decision"]["stop_condition"] == "reviewer_lanes_missing"
    assert report["decision"]["author_lane_excluded"] is True
    assert report["decision"]["reviewer_outcomes"] == []


def test_promoted_mode_requires_exact_code_mower_gate_check() -> None:
    report = _evaluate(
        [
            _pr(
                done=["codex-audit-done", "claude-audit-done"],
                checks=[{"name": "code-mower/gate-health", "state": "success"}],
            )
        ],
        mode="promoted",
        gate_required=True,
        auto_merge_enabled=True,
        merge_token_ready=True,
    )

    assert report["decision"]["decision_state"] == "owner_action"
    assert report["decision"]["owner_action_kind"] == "required_gate_not_green"
    assert report["decision"]["gate_status"] == "missing"


def test_manual_mode_refuses_ready_without_peer_reviewer_pass() -> None:
    report = _evaluate([_pr(done=[])], mode="manual")

    assert report["decision"]["decision_state"] == "waiting_for_evidence"
    assert report["decision"]["next_action"] == "waiting for peer reviewer pass"
    assert report["decision"]["stop_condition"] == "peer_reviewer_missing"
    event = controller.build_controller_event(report=report)
    assert event["event_type"] == "controller_decision"
    assert event["status"] == "observed"


def test_green_promoted_merge_excludes_author_lane_and_requests_auto_merge() -> None:
    report = _evaluate(
        [_pr(builder="builder:codex", done=["claude-audit-done"])],
        mode="promoted",
        gate_required=True,
        auto_merge_enabled=True,
        merge_token_ready=True,
    )
    event = controller.build_controller_event(report=report)

    assert report["decision"]["decision_state"] == "ready_to_merge"
    assert report["decision"]["next_action"] == "enable pull request auto merge"
    assert report["decision"]["author_lane_excluded"] is True
    assert report["decision"]["promoted_reviewers_passed"] is True
    assert event["event_type"] == "merge_decision"
    assert event["status"] == "ready"


def test_cli_writes_metadata_only_event_file(tmp_path: Path) -> None:
    config_path = tmp_path / "code-mower.yml"
    config_path.write_text(
        """
version: 1
project:
  name: demo
  state_dir: .code-mower
repositories:
  - slug: owner/repo
    default_branch: main
owner_surface:
  ready_label: tier:R
builder_identity:
  labels:
    builder:codex: codex
lanes:
  codex:
    type: audit
    driver: local_cli
    provider: codex
    merge_authority: true
    labels:
      needs: needs-codex-audit
      done: codex-audit-done
      blocked: codex-audit-blocked
""".lstrip(),
        encoding="utf-8",
    )
    event_path = tmp_path / "controller-event.json"

    def gh_json(args: list[str]) -> object:
        if args[:2] == ["pr", "list"]:
            return []
        if args[:2] == ["run", "list"]:
            return []
        if args[:2] == ["issue", "list"]:
            return []
        raise AssertionError(args)

    def command_runner(_args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "")

    out = StringIO()
    with redirect_stdout(out):
        code = controller.main(
            [
                "run",
                "--repo",
                "owner/repo",
                "--config",
                str(config_path),
                "--event-file",
                str(event_path),
                "--json",
            ],
            gh_json_runner=gh_json,  # type: ignore[call-arg]
            command_runner=command_runner,  # type: ignore[call-arg]
        )

    assert code == 0
    payload = json.loads(out.getvalue())
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert payload["event"]["event_type"] == "queue_state_snapshot"
    assert event["event_type"] == "queue_state_snapshot"
    serialized = json.dumps(event).lower()
    for forbidden in (
        "source_code",
        "raw_diffs",
        "raw_stdout_stderr",
        "auth_probe_output",
        "secret",
        "transcript",
    ):
        assert forbidden not in serialized


def test_cli_reports_cloud_event_validation_errors_without_traceback(tmp_path: Path) -> None:
    config_path = tmp_path / "code-mower.yml"
    config_path.write_text(
        """
version: 1
project:
  name: demo
  state_dir: .code-mower
repositories:
  - slug: owner/repo
    default_branch: main
lanes:
  codex:
    type: audit
    driver: local_cli
    provider: codex
    merge_authority: true
    labels:
      needs: needs-codex-audit
      done: codex-audit-done
      blocked: codex-audit-blocked
""".lstrip(),
        encoding="utf-8",
    )

    def gh_json(args: list[str]) -> object:
        if args[:2] == ["pr", "list"]:
            return []
        if args[:2] == ["run", "list"]:
            return []
        if args[:2] == ["issue", "list"]:
            return []
        raise AssertionError(args)

    def command_runner(_args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "")

    err = StringIO()
    with redirect_stderr(err):
        code = controller.main(
            [
                "run",
                "--repo",
                "owner/repo",
            "--config",
            str(config_path),
            "--source",
            "Bearer abcdefghijklmnop",
            ],
            gh_json_runner=gh_json,
            command_runner=command_runner,
        )

    assert code == 1
    assert "secret-like value" in err.getvalue()
    assert "abcdefghijklmnop" not in err.getvalue()
