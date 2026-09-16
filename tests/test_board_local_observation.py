"""Read-only producer tests for exact local Board observations."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_mower import cli, session_current
from code_mower.board_local_observation import (
    LocalEvidenceObservation,
    LocalObservationInput,
    LocalProcessObservation,
    LocalRunObservation,
    LocalWorkObservation,
    WorkBinding,
    observe_local_work,
    review_from_audit_artifact,
    run_from_delivery_outcome,
    run_from_remote_lifecycle,
    work_from_context_session,
    worktree_identity,
)
from code_mower.board_observation import validate


REPOSITORY = "codemower-ai/code-mower"
SESSION_ID = "a4ce901ecfb743609ed0b6504668aca7"
HEAD = "b" * 40
OTHER_HEAD = "c" * 40
NOW = datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc)


@contextmanager
def working_directory(path: str | Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def init_repo(path: Path) -> None:
    (path / ".git").mkdir()


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
    return result


def active_resolver(*_args, **_kwargs) -> dict:
    return {
        "schema": session_current.CURRENT_SESSION_SCHEMA,
        "state": session_current.STATE_ACTIVE,
        "current": True,
        "lease": {"state": "active", "provider": "codex", "expires_at": "2026-09-17T00:00:00Z"},
        "guidance": None,
        "session": {"id": SESSION_ID, "repo": REPOSITORY},
    }


def binding(root: Path, *, head: str | None = None, pr: int | None = None) -> WorkBinding:
    return WorkBinding(
        session_id=SESSION_ID,
        work_id="work949",
        repository=REPOSITORY,
        worktree_id=worktree_identity(root),
        pr_number=pr,
        head_sha=head,
    )


def running_lifecycle() -> dict:
    return {
        "schema": "code_mower.remote_session.v1",
        "state": "running",
        "reason": "none",
        "next_action": "status",
        "counts": {"dispatch": 1, "message": 0, "cancel": 0, "collect": 0},
    }


def complete_lifecycle() -> dict:
    return {
        "schema": "code_mower.remote_session.v1",
        "state": "complete",
        "reason": "none",
        "next_action": "none",
        "counts": {"dispatch": 1, "message": 0, "cancel": 0, "collect": 1},
    }


class LocalBoardObservationTests(unittest.TestCase):
    def test_active_assignment_is_not_inferred_running_from_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            work = LocalWorkObservation(
                binding=binding(root),
                reference="issue-949",
                observed_at=NOW,
                assigned_provider="codex",
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(work=work),
                now=NOW,
                current_session_resolver=active_resolver,
            )

            assert validate(record) == record
            assert record["work"]["evidence"]["lease"]["state"] == "held"
            assert record["work"]["runs"][0]["phase"] == "assigned"
            assert record["work"]["runs"][0]["heartbeat_at"] is None

    def test_remote_lifecycle_is_reused_without_a_second_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            run = run_from_remote_lifecycle(
                id="run949",
                binding=binding(root),
                provider="codex",
                role="builder",
                observed_at=NOW,
                heartbeat_at=NOW,
                lifecycle=running_lifecycle(),
            )
            work = LocalWorkObservation(
                binding=run.binding,
                reference="issue-949",
                observed_at=NOW,
                runs=(run,),
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(work=work),
                now=NOW,
                current_session_resolver=active_resolver,
            )

            assert record["work"]["stage"] == "building"
            assert record["work"]["runs"][0]["lifecycle"] == running_lifecycle()

    def test_maintained_delivery_outcome_is_bound_before_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            exact = binding(root, pr=949, head=HEAD)
            event = {
                "schema": "code_mower.laneDeliveryOutcome.v1",
                "event_id": "lane-delivery-abcdef123456",
                "created_at": NOW.isoformat(),
                "repo": REPOSITORY,
                "lane": "codex",
                "target": {"kind": "issue", "number": "949"},
                "provider": {"exit_code": 0},
                "delivery": {
                    "delivered": True,
                    "transition": "pr_opened",
                    "reason": "observed_state_transition",
                },
            }

            run = run_from_delivery_outcome(
                event,
                binding=exact,
                target_kind="issue",
                target_number=949,
                observed_at=NOW,
            )

            assert run.phase == "implementation_complete"
            assert run.binding == exact
            with self.assertRaisesRegex(ValueError, "run_unavailable"):
                run_from_delivery_outcome(
                    {**event, "repo": "other/repo"},
                    binding=exact,
                    target_kind="issue",
                    target_number=949,
                    observed_at=NOW,
                )

    def test_audit_artifact_discards_prose_and_requires_exact_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            exact = binding(root, pr=949, head=HEAD)
            artifact = {
                "schema": "code_mower.auditVerdictArtifact.v1",
                "lane_id": "claude",
                "repo": REPOSITORY,
                "pr_number": 949,
                "head_sha_start": HEAD,
                "head_sha_end": HEAD,
                "verdict": "pass",
                "trailer": "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                "comment_body": "private review prose must not enter Board",
                "created_at": NOW.isoformat(),
                "posted_comment_url": None,
            }

            review = review_from_audit_artifact(artifact, binding=exact, observed_at=NOW)

            assert review.state == "pass"
            assert not hasattr(review, "comment_body")
            with self.assertRaisesRegex(ValueError, "evidence_unavailable"):
                review_from_audit_artifact(
                    {**artifact, "head_sha_end": OTHER_HEAD},
                    binding=exact,
                    observed_at=NOW,
                )

    def test_stale_live_run_is_not_rendered_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            stale = NOW - timedelta(minutes=20)
            run = run_from_remote_lifecycle(
                id="run949",
                binding=binding(root),
                provider="codex",
                role="builder",
                observed_at=stale,
                heartbeat_at=stale,
                lifecycle=running_lifecycle(),
            )
            work = LocalWorkObservation(
                binding=run.binding,
                reference="issue-949",
                observed_at=NOW,
                runs=(run,),
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(work=work),
                now=NOW,
                current_session_resolver=active_resolver,
            )

            assert record["work"]["runs"] == []
            assert record["work"]["reasons"] == ["stale_observation"]
            assert next(source for source in record["sources"] if source["id"] == "runobs0")[
                "freshness"
            ] == "stale"

    def test_unavailable_run_preserves_source_without_a_live_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            run = LocalRunObservation(
                id="run949",
                binding=binding(root),
                provider="codex",
                role="builder",
                phase="observed_running",
                basis="observed",
                observed_at=NOW - timedelta(minutes=2),
                heartbeat_at=NOW - timedelta(minutes=2),
                source_available=False,
                lifecycle=running_lifecycle(),
            )
            work = LocalWorkObservation(
                binding=run.binding,
                reference="issue-949",
                observed_at=NOW,
                runs=(run,),
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(work=work),
                now=NOW,
                current_session_resolver=active_resolver,
            )

            assert record["work"]["runs"] == []
            assert record["work"]["reasons"] == ["source_unavailable"]
            source = next(item for item in record["sources"] if item["id"] == "runobs0")
            assert (source["freshness"], source["coverage"]) == ("unavailable", "unavailable")

    def test_exact_head_review_ci_and_gate_join_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            exact = binding(root, pr=949, head=HEAD)
            run = run_from_remote_lifecycle(
                id="run949",
                binding=exact,
                provider="codex",
                role="builder",
                observed_at=NOW,
                lifecycle=complete_lifecycle(),
            )
            states = (
                ("review_request", "requested"),
                ("review", "pass"),
                ("ci", "pass"),
                ("gate_publisher", "pass"),
                ("gate", "pass"),
                ("merge", "ready"),
            )
            evidence = tuple(
                LocalEvidenceObservation(kind=kind, state=state, binding=exact, observed_at=NOW)
                for kind, state in states
            )
            work = LocalWorkObservation(
                binding=exact,
                reference="issue-949",
                observed_at=NOW,
                runs=(run,),
                evidence=evidence,
                assigned_provider="codex",
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(work=work),
                now=NOW,
                current_session_resolver=active_resolver,
            )

            assert record["work"]["stage"] == "ready_to_merge"
            assert record["work"]["reasons"] == ["ready_to_merge"]
            for kind in ("review", "ci", "gate"):
                assert record["work"]["evidence"][kind]["head_sha"] == HEAD

    def test_other_head_review_is_stale_not_a_current_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            exact = binding(root, pr=949, head=HEAD)
            old = binding(root, pr=949, head=OTHER_HEAD)
            work = LocalWorkObservation(
                binding=exact,
                reference="issue-949",
                observed_at=NOW,
                evidence=(
                    LocalEvidenceObservation(
                        kind="review", state="pass", binding=old, observed_at=NOW
                    ),
                ),
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(work=work),
                now=NOW,
                current_session_resolver=active_resolver,
            )

            assert record["work"]["evidence"]["review"] == {
                "state": "stale",
                "source_id": "evidenceobs0",
                "head_sha": OTHER_HEAD,
                "coverage": "full",
            }
            assert record["work"]["reasons"] == ["review_stale"]

    def test_every_binding_dimension_fails_closed_before_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other_tmp:
            root, other = Path(tmp), Path(other_tmp)
            init_repo(root)
            init_repo(other)
            good = binding(root)
            variants = (
                WorkBinding("f" * 32, good.work_id, good.repository, good.worktree_id),
                WorkBinding(good.session_id, "otherwork", good.repository, good.worktree_id),
                WorkBinding(good.session_id, good.work_id, "other/repo", good.worktree_id),
                WorkBinding(
                    good.session_id,
                    good.work_id,
                    good.repository,
                    worktree_identity(other),
                ),
            )
            for mismatch in variants:
                with self.subTest(binding=mismatch):
                    run = LocalRunObservation(
                        id="run949",
                        binding=mismatch,
                        provider="codex",
                        role="builder",
                        phase="assigned",
                        basis="configured",
                        observed_at=NOW,
                    )
                    work = LocalWorkObservation(
                        binding=good,
                        reference="issue-949",
                        observed_at=NOW,
                        runs=(run,),
                    )
                    assert observe_local_work(
                        repository=REPOSITORY,
                        start=root,
                        snapshot=LocalObservationInput(work=work),
                        now=NOW,
                        current_session_resolver=active_resolver,
                    ) is None

    def test_two_worktrees_are_distinct_and_one_scope_rejects_the_other(self) -> None:
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first, second = Path(first_tmp), Path(second_tmp)
            init_repo(first)
            init_repo(second)
            assert worktree_identity(first) != worktree_identity(second)
            foreign = LocalWorkObservation(
                binding=binding(first),
                reference="issue-949",
                observed_at=NOW,
                assigned_provider="codex",
            )

            assert observe_local_work(
                repository=REPOSITORY,
                start=second,
                snapshot=LocalObservationInput(work=foreign),
                now=NOW,
                current_session_resolver=active_resolver,
            ) is None

    def test_unlinked_processes_deduplicate_launcher_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            processes = (
                LocalProcessObservation("launcher949", "codex", NOW, "builder"),
                LocalProcessObservation("launcher949", "codex", NOW, "builder"),
                LocalProcessObservation("launcher950", "claude", NOW, "reviewer"),
            )

            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(processes=processes),
                now=NOW,
                current_session_resolver=lambda **_kwargs: {"state": "lease_absent"},
            )

            assert record["kind"] == "unlinked"
            assert [item["id"] for item in record["unlinked"]] == ["launcher949", "launcher950"]
            assert record["scope"]["session_id"] is None

    def test_no_session_and_closed_failure_states_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            for state in (
                "lease_absent",
                "lease_expired",
                "lease_malformed",
                "lease_changed",
                "brief_missing",
                "brief_invalid",
                "brief_mismatch",
                "brief_refused",
                "no_working_copy",
            ):
                with self.subTest(state=state):
                    result = observe_local_work(
                        repository=REPOSITORY,
                        start=root,
                        snapshot=LocalObservationInput(),
                        now=NOW,
                        current_session_resolver=lambda state=state, **_kwargs: {
                            "state": state,
                            "current": False,
                            "lease": {"state": "unavailable"},
                            "session": None,
                        },
                    )
                    assert result is None

    def test_no_work_requires_explicit_complete_queue_and_registry_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            incomplete = LocalObservationInput(work_queue_complete=True)
            assert observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=incomplete,
                now=NOW,
                current_session_resolver=active_resolver,
            ) is None

            complete = LocalObservationInput(
                work_queue_complete=True,
                run_registry_complete=True,
            )
            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=complete,
                now=NOW,
                current_session_resolver=active_resolver,
            )
            assert record["kind"] == "no_work"

    def test_context_adapter_discards_private_work_item_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            timestamp = NOW.isoformat()
            private = {
                "schema": "code_mower.contextSession.v1",
                "session_id": SESSION_ID,
                "repo": REPOSITORY,
                "work_item": "private roadmap prose that must not be displayed",
                "connection": None,
                "policy": None,
                "host": "codex",
                "orchestrator": "codex",
                "participants": ["codex", "claude"],
                "builder": "codex",
                "generation": 1,
                "stage": "attached",
                "query_mode": "work_item",
                "retrieval_source": "linear",
                "request_hash": "d" * 64,
                "context_state": "ready",
                "packet": "e" * 32,
                "work_order": "work-orders/private.md",
                "pr": 949,
                "head": HEAD,
                "revision": "f" * 32,
                "attachment_state": "published",
                "created_at": timestamp,
                "updated_at": timestamp,
            }

            work = work_from_context_session(
                private,
                worktree_id=worktree_identity(root),
                observed_at=NOW,
            )

            assert work.reference.startswith("work-")
            assert "private" not in work.reference
            assert work.binding.pr_number == 949
            assert work.binding.head_sha == HEAD

    def test_real_resolver_read_is_write_free_and_refuses_symlinked_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            root = Path(tmp)
            init_repo(root)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(
                    [
                        "session",
                        "start",
                        "--repo",
                        REPOSITORY,
                        "--host",
                        "codex",
                        "--with",
                        "codex,claude",
                        "--json",
                    ]
                )
            assert code == 0, err.getvalue()
            saved = json.loads(out.getvalue())
            # The real session has a generated id; no fabricated binding is used.
            before = tree_snapshot(root)
            record = observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(
                    work_queue_complete=True,
                    run_registry_complete=True,
                ),
            )
            assert record["kind"] == "no_work"
            assert record["scope"]["session_id"] == saved["id"]
            assert tree_snapshot(root) == before

            real_state = root / session_current.DEFAULT_STATE_DIR
            linked_state = root / "linked-state"
            linked_state.symlink_to(real_state.resolve(), target_is_directory=True)
            before = tree_snapshot(root)
            assert observe_local_work(
                repository=REPOSITORY,
                start=root,
                state_dir=linked_state,
                snapshot=LocalObservationInput(
                    work_queue_complete=True,
                    run_registry_complete=True,
                ),
            ) is None
            assert tree_snapshot(root) == before

    def test_changed_lease_read_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            states = iter(
                [
                    {"state": "active", "current": True},
                    {"state": "lease_changed", "current": False},
                ]
            )

            def racing(**_kwargs):
                state = next(states)
                if state["current"]:
                    # A resolver must not expose active until its own second lease read agrees.
                    return {**active_resolver(), "state": "lease_changed", "current": False}
                return state

            assert observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(
                    work_queue_complete=True,
                    run_registry_complete=True,
                ),
                now=NOW,
                current_session_resolver=racing,
            ) is None

    def test_malformed_or_conflicting_process_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            bad = (
                LocalProcessObservation("launcher949", "codex", NOW, "builder"),
                LocalProcessObservation("launcher949", "claude", NOW, "builder"),
            )
            assert observe_local_work(
                repository=REPOSITORY,
                start=root,
                snapshot=LocalObservationInput(processes=bad),
                now=NOW,
                current_session_resolver=lambda **_kwargs: {"state": "lease_absent"},
            ) is None

    def test_resolver_exceptions_and_private_values_never_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            def failed(**_kwargs):
                raise OSError("/home/private/auth-output")

            assert observe_local_work(
                repository=REPOSITORY,
                start=root,
                now=NOW,
                current_session_resolver=failed,
            ) is None

    def test_packaged_producer_is_declared(self) -> None:
        from code_mower.package_manifest import PACKAGE_FILES

        targets = {target for _source, target, _mode in PACKAGE_FILES}
        assert "src/code_mower/board_local_observation.py" in targets


if __name__ == "__main__":
    unittest.main()
