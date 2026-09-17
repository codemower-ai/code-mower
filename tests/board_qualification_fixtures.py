"""Live-shaped metadata for #951. No provider payload or private prose is exported."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_mower import builder_lineage
from code_mower.board_local_observation import (
    LocalEvidenceObservation, LocalObservationInput, LocalPolicyObservation,
    LocalProcessObservation, LocalRunObservation, LocalWorkObservation, WorkBinding,
    observe_local_work, worktree_identity,
)
from code_mower.board_observation import validate
from code_mower.board_remote_observation import RemoteRun, hosted_work_input, remote_work_input
from code_mower.remote_session import RemoteObservation, RemoteWorkObservation, public_projection

NOW = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
REPO = "owner/repo"
SESSION = "a" * 32
HEAD = "b" * 40
OLD_HEAD = "c" * 40
PRIVATE = "PRIVATE_SESSION_PROVIDER_CONTEXT_SLACK_GRAPHIFY_SOURCE_PATH"


def resolver(**_kwargs):
    return {"state": "active", "current": True, "lease": {"state": "active"},
            "session": {"id": SESSION, "repo": REPO}}


def work(root: Path, *, pr: bool = True, id: str = "work951"):
    return LocalWorkObservation(
        WorkBinding(SESSION, id, REPO, worktree_identity(root), 42 if pr else None,
                    HEAD if pr else None),
        "issue-951", NOW, assigned_provider="codex",
    )


def evidence(item, kind, state, **kwargs):
    return LocalEvidenceObservation(kind, state, item.binding, NOW,
                                    source_kind={"review": "review", "ci": "ci",
                                                 "gate": "gate"}.get(kind, "github"), **kwargs)


def complete(item):
    return LocalRunObservation("implementation", item.binding, "codex", "builder",
                               "implementation_complete", "observed", NOW)


def project(root, snapshot, *, now=NOW):
    record = observe_local_work(repository=REPO, start=root, snapshot=snapshot,
                                now=now, current_session_resolver=resolver)
    assert record is not None
    return validate(record)


def lineage(binding, *, contributor="codex", head=None):
    target = builder_lineage.Target(REPO, binding.pr_number, f"{contributor}/42-work",
                                    head or binding.head_sha)
    identity = builder_lineage.Identity({
        "enabled": True, "labels": {f"builder:{contributor}": contributor},
        "branch_prefixes": {f"{contributor}/": contributor}, "require_verified_lineage": True,
    })
    return builder_lineage.resolve(builder_lineage.Chain.from_arrivals(target, ()),
                                    identity, "", [f"builder:{contributor}"])


def artifact(binding, *, lane="claude"):
    return {"schema": "code_mower.auditVerdictArtifact.v1", "lane_id": lane,
            "repo": REPO, "pr_number": binding.pr_number, "head_sha_start": binding.head_sha,
            "head_sha_end": binding.head_sha, "verdict": "pass",
            "trailer": "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            "comment_body": PRIVATE, "created_at": NOW.isoformat(), "posted_comment_url": None}


def records(root: Path):
    """Distinct scenarios deliberately share identity: each is one snapshot, not a campaign."""
    item = work(root)
    result = {}

    def add(name, value):
        result[name] = project(root, LocalObservationInput(work=value))

    fresh = work(root, pr=False)
    running = LocalRunObservation("run951", fresh.binding, "codex", "builder",
                                  "observed_running", "observed", NOW, heartbeat_at=NOW)
    add("fresh_without_pr", replace(fresh, runs=(running,)))
    for name, review in (("review_requested", "unknown"), ("review_running", "running"),
                         ("changes_requested", "blocked")):
        add(name, replace(item, runs=(complete(item),), evidence=(
            evidence(item, "review_request", "requested"), evidence(item, "review", review),
        )))
    add("implementation_complete", replace(item, runs=(complete(item),)))
    add("behind_and_review", replace(item, runs=(complete(item),), evidence=(
        evidence(item, "review_request", "requested"),
    ), policy=LocalPolicyObservation(item.binding, NOW, ("update_required",))))
    add("independent_evidence", replace(item, runs=(complete(item),), evidence=(
        evidence(item, "review", "pass"), evidence(item, "ci", "pass", coverage="sampled"),
        evidence(item, "gate_publisher", "pass"), evidence(item, "gate", "pending"),
        evidence(item, "merge", "blocked"),
    ), policy=LocalPolicyObservation(item.binding, NOW, ("human_review_required",))))
    add("simultaneous_failures", replace(item, runs=(complete(item),), evidence=(
        evidence(item, "review", "blocked"), evidence(item, "ci", "failed"),
        evidence(item, "gate", "failed"),
    )))
    for name, state, age, available in (
        ("remote_fresh", "running", 0, True), ("stale", "running", 600, True),
        ("unreachable", "running", 600, False), ("waiting", "waiting_for_user", 0, True),
        ("failed", "failed", 0, True), ("cancelled", "terminated", 0, True),
        ("historical_running", "running", 3600, True),
    ):
        observed = NOW - timedelta(seconds=age)
        lifecycle = public_projection({"state": state, "counts": {"dispatch": 1},
                                       "provider_id": PRIVATE, "context": PRIVATE,
                                       "usage": {"cost_usd": 0, "settled": False}})
        remote = RemoteObservation("private-generation", "devin", lifecycle,
                                    observed, NOW, available)
        result[name] = project(root, remote_work_input(item, round_number=0,
            runs=(RemoteRun(item.binding, 0, remote),), now=NOW))
    for name, available in (("complete_provider_active", True), ("complete_provider_unknown", False)):
        remote = RemoteObservation("private-generation", "devin",
            public_projection({"state": "running"}) if available else None,
            NOW if available else None, NOW, available)
        hosted = RemoteWorkObservation("private-work-generation", REPO, 951, 0, remote,
            pr_number=42, head_sha=HEAD, pr_state="open", github_available=True,
            implementation_verified=True)
        result[name] = project(root, hosted_work_input(item, hosted, expected_round=0, now=NOW))
    cancellation = RemoteObservation("private-generation", "devin", public_projection({
        "state": "running", "counts": {"dispatch": 1, "cancel": 1},
    }), NOW, NOW, True)
    result["cancel_accepted_before_exit"] = project(root, remote_work_input(item, round_number=0,
        runs=(RemoteRun(item.binding, 0, cancellation),), now=NOW))
    result["no_work"] = project(root, LocalObservationInput(
        work_queue_complete=True, run_registry_complete=True))
    result["unlinked"] = project(root, LocalObservationInput(processes=(
        LocalProcessObservation("launcher951", "codex", NOW),
    )))
    return result


def payload(record_list):
    return {
        "schema": "code_mower.laneStatus.v1", "repo": REPO, "generated_at": NOW.isoformat(),
        "board": {"cache": {"state": "fresh", "age_seconds": 0, "ttl_seconds": 15,
                             "generation": 1, "refresh_in_progress": False},
                  "version": {"serving_version": "1.4.1", "installed_version": "1.4.1",
                              "restart_recommended": False}},
        "remote": {"available": True, "pull_requests": [], "workflow_runs": [],
                   "gate_health": {"alerts": []}},
        "observations": {"available": True, "records": record_list, "warnings": [],
                         "rejected": 0, "coverage": "complete", "coverage_complete": True,
                         "coverage_gaps": []},
    }


if __name__ == "__main__":
    import argparse
    import json
    from code_mower import board

    parser = argparse.ArgumentParser(description="Render sanitized Board qualification fixtures")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # The source checkout supplies only an opaque worktree digest, never its path.
    cases = records(Path(__file__).resolve().parents[1])
    (args.output / "cases.json").write_text(json.dumps({
        "now": NOW.isoformat(), "cases": {name: payload([record]) for name, record in cases.items()},
    }, indent=2) + "\n")
    (args.output / "board.html").write_text(board.render_board_html(board.BoardConfig(repo=REPO)))
