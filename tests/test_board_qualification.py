"""Convergence regressions: maintained producers -> closed contract -> shipped UI."""
from __future__ import annotations

import copy
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_mower import board, builder_lineage, cli, lane_delivery, session_current
from code_mower.board_local_observation import (
    LocalObservationInput, LocalPolicyObservation, LocalWorkObservation, WorkBinding,
    observe_local_work, review_from_audit_artifact, run_from_delivery_outcome, worktree_identity,
)
from code_mower.board_observation import derive_primary, ordered_reasons, validate
from board_qualification_fixtures import (
    HEAD, NOW, OLD_HEAD, PRIVATE, REPO, artifact, complete, evidence, lineage, payload,
    project, records, work,
)
from test_board import _eval_board_view, _render_board_sequence


class BoardQualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".git").mkdir()
        self.work = work(self.root)

    def test_live_shaped_matrix_uses_the_closed_contract(self):
        cases = records(self.root)
        self.assertEqual(len(cases), 20)
        for name, record in cases.items():
            with self.subTest(name=name):
                self.assertEqual(validate(record), record)
                output = json.dumps(record)
                for private in (PRIVATE, "private-generation", "private-work-generation", str(self.root)):
                    self.assertNotIn(private, output)
                if record["work"]:
                    for metric in record["work"]["measurements"].values():
                        self.assertEqual(metric, {"value": None, "coverage": "unavailable",
                                                  "observed": 0, "total": None})
        self.assertIsNone(cases["fresh_without_pr"]["work"]["pull_request"]["number"])
        self.assertEqual(cases["behind_and_review"]["work"]["reasons"],
                         ["update_required", "review_requested"])
        self.assertEqual(cases["simultaneous_failures"]["work"]["reasons"],
                         ["changes_requested", "ci_failed", "gate_failed"])
        for name, actor, action in (("review_requested", "reviewer", "review_current_head"),
                                    ("review_running", "reviewer", "finish_review"),
                                    ("changes_requested", "builder", "address_findings")):
            self.assertEqual(cases[name]["work"]["primary"], {"actor": actor, "action": action})
        for name, age, freshness in (("stale", 600, "stale"), ("unreachable", 600, "unavailable"),
                                     ("historical_running", 3600, "stale")):
            source = next(s for s in cases[name]["sources"] if s["kind"] == "remote_session")
            self.assertEqual(source["observed_at"], (NOW-timedelta(seconds=age)).isoformat().replace("+00:00", "Z"))
            self.assertEqual(source["freshness"], freshness)
        for name in ("complete_provider_active", "complete_provider_unknown"):
            value = cases[name]["work"]
            self.assertIn("implementation_complete", [r["phase"] for r in value["runs"]])
            self.assertEqual(value["evidence"]["review"]["state"], "unknown")
            self.assertEqual(value["evidence"]["merge"]["state"], "open")
            self.assertNotIn(value["stage"], {"ready_to_merge", "merged"})
        accepted = cases["cancel_accepted_before_exit"]["work"]["runs"][0]
        self.assertEqual(accepted["phase"], "observed_running")
        self.assertEqual(accepted["lifecycle"]["counts"]["cancel"], 1)
        self.assertEqual(cases["cancelled"]["work"]["runs"][0]["phase"], "cancelled")

    def test_exact_head_stale_ci_and_gate_remain_visible(self):
        for kind in ("review", "ci", "gate"):
            for changed in ("head", "time"):
                with self.subTest(kind=kind, changed=changed):
                    item = evidence(self.work, kind, "pass")
                    item = (replace(item, binding=replace(item.binding, head_sha=OLD_HEAD))
                            if changed == "head" else replace(item, observed_at=NOW - timedelta(hours=1)))
                    record = project(self.root, LocalObservationInput(work=replace(self.work,
                        runs=(complete(self.work),), evidence=(item,))))
                    self.assertEqual(record["work"]["evidence"][kind]["state"], "stale")
                    self.assertNotEqual(record["work"]["stage"], "ready_to_merge")
                    self.assertEqual(record["work"]["evidence"][kind]["head_sha"], item.binding.head_sha)

    def test_passes_require_full_ci_and_independent_human_policy(self):
        item = replace(self.work, runs=(complete(self.work),), evidence=tuple(
            evidence(self.work, kind, state) for kind, state in (
                ("review", "pass"), ("ci", "pass"), ("gate", "pass"), ("merge", "ready"))))
        result = project(self.root, LocalObservationInput(work=item))["work"]
        self.assertEqual(result["stage"], "ready_to_merge")
        self.assertNotIn("human_review_required", result["reasons"])
        for changed in (
            replace(item, evidence=tuple(replace(e, coverage="sampled") if e.kind == "ci" else e
                                         for e in item.evidence)),
            replace(item, policy=LocalPolicyObservation(item.binding, NOW, ("human_review_required",))),
            replace(item, policy=LocalPolicyObservation(item.binding, NOW, ("update_required",))),
            replace(item, runs=(replace(complete(item), phase="observed_running", heartbeat_at=NOW),)),
            replace(item, evidence=tuple(replace(e, observed_at=NOW-timedelta(hours=1))
                                         if e.kind == "merge" else e for e in item.evidence)),
        ):
            result = project(self.root, LocalObservationInput(work=changed))["work"]
            self.assertNotEqual(result["stage"], "ready_to_merge")
        # A PASS on its own does not invent an owner decision.
        result = project(self.root, LocalObservationInput(work=replace(self.work,
            evidence=(evidence(self.work, "review", "pass"),))))["work"]
        self.assertNotIn("human_review_required", result["reasons"])
        self.assertNotEqual(result["primary"]["actor"], "owner")

    def test_routes_are_deterministic_and_routine_work_is_not_an_owner_decision(self):
        groups = (
            (("review_requested", "update_required"), "builder", "update_branch"),
            (("gate_failed", "ci_failed", "changes_requested"), "builder", "address_findings"),
            (("provider_failed", "source_unavailable", "stale_observation"), "orchestrator", "restore_source"),
            (("review_requested", "human_review_required", "approval_required"), "owner", "respond_to_approval"),
        )
        for reasons, actor, action in groups:
            for order in itertools.permutations(reasons):
                self.assertEqual(derive_primary(order), {"actor": actor, "action": action})
                self.assertEqual(ordered_reasons(order), ordered_reasons(reasons))

    def test_exact_head_contributors_and_effective_authority_are_required_for_audit(self):
        binding = self.work.binding
        verdict = artifact(binding)
        admitted = review_from_audit_artifact(verdict, binding=binding, observed_at=NOW,
                                              lineage=lineage(binding))
        self.assertEqual(admitted.state, "pass")
        for decision, policy in (
            (None, None), (lineage(binding, head=OLD_HEAD), None),
            (builder_lineage.resolve_identity_only(builder_lineage.Identity({}), "", [], "codex/work"), None),
            (lineage(binding, contributor="claude"), None),
            (lineage(binding), {"role_policy": {"claude": {"reviewer": {"enabled": False}}}}),
        ):
            with self.subTest(decision=decision, policy=policy), self.assertRaisesRegex(ValueError, "evidence_unavailable"):
                review_from_audit_artifact(verdict, binding=binding, observed_at=NOW,
                                          lineage=decision, policy=policy)
        with self.assertRaisesRegex(ValueError, "evidence_unavailable"):
            review_from_audit_artifact(artifact(binding, lane="devin"), binding=binding,
                                      observed_at=NOW, lineage=lineage(binding))
        # A former contributor is excluded after a handoff as well.
        target = builder_lineage.Target(REPO, 42, "codex/42-work", HEAD)
        chain = builder_lineage.Chain.from_arrivals(target, [builder_lineage.Episode(
            sequence=1, repo=REPO, pr_number=42, branch=target.branch, source_lane="claude",
            destination_lane="codex", expected_head=OLD_HEAD, resulting_head=HEAD,
            writer_state="terminated")])
        decision = builder_lineage.resolve(chain, builder_lineage.Identity({"enabled": True,
            "labels": {"builder:codex": "codex"}}), "", ["builder:codex"])
        with self.assertRaisesRegex(ValueError, "evidence_unavailable"):
            review_from_audit_artifact(verdict, binding=binding, observed_at=NOW, lineage=decision)

    def test_restart_and_worktree_fences_preserve_sources_without_renewing_them(self):
        cases = records(self.root)
        directory = self.root / ".code-mower/board/observations"
        directory.mkdir(parents=True)
        saved = directory / "observation.json"
        saved.write_text(json.dumps(cases["unreachable"]))
        before = saved.read_bytes(), saved.stat().st_mtime_ns
        config = board.BoardConfig(repo=REPO, repo_path=str(self.root))
        for _restart in range(2):
            read = board.observations_payload(config)
            self.assertEqual(read["records"], [cases["unreachable"]])
        self.assertEqual(before, (saved.read_bytes(), saved.stat().st_mtime_ns))
        other = self.root / "second-worktree"
        other.mkdir()
        (other / ".git").mkdir()
        with self.assertRaises(AssertionError):
            project(other, LocalObservationInput(work=self.work))
        self.assertNotEqual(worktree_identity(self.root), worktree_identity(other))
        if shutil.which("node"):
            second = project(other, LocalObservationInput(work=work(other)))
            rows = _eval_board_view("workRows(ARGS[0], ARGS[1])",
                payload([cases["fresh_without_pr"], second]), int(NOW.timestamp() * 1000))
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["key"], rows[1]["key"])

    @unittest.skipUnless(shutil.which("node"), "Node executes the shipped Board renderer")
    def test_measurements_always_name_coverage_and_never_impute_missing_cost(self):
        readings = _eval_board_view("ARGS[0].map(m => measurementText(m, 'usd'))", [
            {"value": None, "coverage": "unavailable", "observed": 0, "total": None},
            {"value": 0, "coverage": "unavailable", "observed": 0, "total": None},
            {"value": 2, "coverage": "partial", "observed": 1, "total": 3},
            {"value": 2, "coverage": "complete", "observed": 3, "total": 3},
            {"value": 0},
        ])
        self.assertEqual(readings[0], "not recorded")
        self.assertEqual(readings[1], "not recorded")
        self.assertIn("1 of 3 recorded", readings[2])
        self.assertIn("3 of 3 recorded (complete coverage)", readings[3])
        self.assertEqual(readings[4], "not recorded")

    @unittest.skipUnless(shutil.which("node"), "Node executes the shipped Board renderer")
    def test_projection_and_privacy_across_the_shipped_views(self):
        cases = records(self.root)
        frames = _render_board_sequence([{"payload": payload([r])} for r in cases.values()], now=NOW)
        for (name, record), frame in zip(cases.items(), frames, strict=True):
            with self.subTest(name=name):
                output = json.dumps(frame)
                self.assertNotIn(PRIVATE, output)
                self.assertNotIn(record["scope"]["session_id"] or "not-a-session", frame.get("chrome", ""))
                self.assertIn("sources:", frame["worklist"])
                self.assertIn("responsible:", frame["worklist"])
                self.assertIn("next:", frame["worklist"])
        frame = frames[list(cases).index("independent_evidence")]
        for label in ("gate publisher run", "code-mower/gate verdict", "review verdict",
                      "Sampled checks only", "merge state", "human policy"):
            self.assertIn(label, frame["worklist"])
        grouped = cases["behind_and_review"]
        rows = _eval_board_view("workRows(ARGS[0], ARGS[1])", payload([grouped, copy.deepcopy(grouped)]),
                                int(NOW.timestamp() * 1000))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_label"], "builder")
        self.assertEqual(rows[0]["reasons"], ["update_required", "review_requested"])
        frame = frames[list(cases).index("behind_and_review")]
        self.assertNotIn('Owner decisions</span><b class="warn">1', frame.get("summary", ""))

    def test_maintained_local_execution_through_real_session_resolver(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["session", "start", "--repo", REPO, "--host", "codex",
                             "--with", "codex,claude", "--json"])
        self.assertEqual(code, 0, err.getvalue())
        session = json.loads(out.getvalue())
        # Execute a bounded, credential-free process through the maintained lane supervisor.
        started = time.monotonic()
        run = lane_delivery.supervise_process([sys.executable, "-c", "print('fixture delivery')"],
            log_path=self.root / "private-run.log", timeout_seconds=5, cwd=self.root)
        elapsed = time.monotonic() - started
        self.assertEqual(run.exit_code, 0)
        before = lane_delivery.TargetState("issue", "951")
        after = replace(before, pr_number="42", head_sha=HEAD, pr_state="OPEN")
        outcome = lane_delivery.classify_delivery(before, after, provider_exit=run.exit_code,
                                                   supervision_reason=run.reason)
        self.assertTrue(outcome.delivered)
        now = datetime.now(timezone.utc)
        event = lane_delivery.build_delivery_outcome_event(lane="codex", repo=REPO,
            kind="issue", number="951", outcome=outcome, elapsed_seconds=elapsed)
        binding = WorkBinding(session["id"], "work951", REPO, worktree_identity(self.root), 42, HEAD)
        observed = run_from_delivery_outcome(event, binding=binding, target_kind="issue",
                                             target_number=951, observed_at=now)
        local_work = LocalWorkObservation(binding, "issue-951", now, runs=(observed,))
        snapshot = LocalObservationInput(work=local_work)
        record = observe_local_work(repository=REPO, start=self.root, snapshot=snapshot, now=now)
        self.assertEqual(record["scope"]["session_id"], session["id"])
        self.assertEqual(record["work"]["runs"][0]["phase"], "implementation_complete")
        self.assertEqual(record["work"]["evidence"]["review"]["state"], "unknown")
        # Exit zero without the observed transition is still failed delivery.
        outcome = lane_delivery.classify_delivery(before, before, provider_exit=0)
        self.assertFalse(outcome.delivered)
        # Interruption before delivery is cancellation, not successful implementation.
        cancelled = lane_delivery.build_delivery_outcome_event(lane="codex", repo=REPO,
            kind="issue", number="951", outcome=outcome, supervision_reason="interrupted")
        observed = run_from_delivery_outcome(cancelled, binding=binding, target_kind="issue",
                                             target_number=951, observed_at=datetime.now(timezone.utc))
        self.assertEqual(observed.phase, "cancelled")
        self.assertEqual(session_current.resolve_current_session(start=self.root)["state"], "active")


if __name__ == "__main__":
    unittest.main()
