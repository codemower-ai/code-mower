"""Role qualification is distinct from selection, policy, and readiness."""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from code_mower import config, init, participants, role_eligibility as roles, session
from code_mower.devin_readiness import devin_readiness
from code_mower.provider_capabilities import normalize_lane
from code_mower.devin_work_orders import DevinWorkOrders
from code_mower.remote_session import RemoteError
from test_devin_work_orders import HEAD, WorkOrderCase


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src/code_mower/templates/code-mower.example.yml"


def policy(role, **settings):
    return {"role_policy": {"devin": {role: settings}}}


class RoleDecisionTests(unittest.TestCase):
    def test_bounded_builder_requires_policy_qualification_and_runtime_independently(self):
        ready = roles.decide_role("devin", "builder", transport="devin_api_v3", bounded=True, runtime="ready")
        self.assertEqual((ready["status"], ready["qualification"], ready["scope"]),
                         ("eligible", "qualified", "bounded"))
        for change, reason in (
            ({"bounded": False}, "bounded_work_required"),
            ({"runtime": "unchecked"}, "runtime_unchecked"),
            ({"runtime": "unavailable"}, "runtime_unavailable"),
            ({"config": policy("builder", enabled=False)}, "policy_denied"),
            ({"config": policy("builder", qualification="missing-record")}, "qualification_missing"),
        ):
            with self.subTest(reason=reason):
                decision = roles.decide_role("devin", "builder", **{
                    "transport": "devin_api_v3", "bounded": True, "runtime": "ready", **change,
                })
                self.assertEqual(decision["reason"], reason)
                with self.assertRaises(config.ConfigError):
                    roles.require_role(decision, execution=True)

    def test_builder_evidence_cannot_promote_another_role_or_transport(self):
        for transport in ("devin_cli", "devin_api_v3"):
            for role in ("orchestrator", "reviewer"):
                with self.subTest(transport=transport, role=role):
                    decision = roles.decide_role(
                        "devin", role, transport=transport, runtime="ready", merge_authority=role == "reviewer",
                        config=policy(role, enabled=True, qualification="devin-hosted-builder-v140"),
                    )
                    self.assertEqual(decision["status"], "ineligible")
                    with self.assertRaises(config.ConfigError):
                        roles.require_role(decision)

    def test_separate_maintained_evidence_still_cannot_enable_unsupported_transport(self):
        reference = "separate-reviewed-role"
        for transport, mode, expected in (("devin_cli", "local_runner", "eligible"),
                                          ("devin_api_v3", "evidence_only", "ineligible")):
            record = roles.Qualification("devin", "reviewer", transport, mode, "unrestricted",
                                         "https://example.test/qualification")
            with mock.patch.object(roles, "QUALIFICATIONS", {reference: record}):
                decision = roles.decide_role("devin", "reviewer", transport=transport,
                    runtime="ready", merge_authority=True, qualification=reference)
                self.assertEqual(decision["status"], expected)

    def test_explicit_lane_record_cannot_override_narrowing_repository_policy(self):
        decision = roles.decide_role(
            "devin", "builder", transport="devin_api_v3", bounded=True, runtime="ready",
            qualification="devin-hosted-builder-v140",
            config=policy("builder", qualification="revoked-reference"),
        )
        self.assertEqual(decision["reason"], "qualification_missing")

    def test_expired_revoked_or_changed_capability_evidence_is_stale(self):
        now = datetime(2026, 9, 14, tzinfo=timezone.utc)
        key = "devin-hosted-builder-v140"
        record = roles.QUALIFICATIONS[key]
        for changed in (replace(record, expires_at=now - timedelta(seconds=1)),
                        replace(record, active=False), replace(record, capability="old-mode")):
            with mock.patch.object(roles, "QUALIFICATIONS", {key: changed}):
                decision = roles.decide_role("devin", "builder", transport="devin_api_v3",
                                             runtime="ready", bounded=True, now=now)
                self.assertEqual(decision["reason"], "qualification_stale")

    def test_other_participants_keep_existing_effective_policy(self):
        for product in ("claude", "codex", "cursor", "grok-bot", "antigravity", "muse"):
            for role in roles.ROLES:
                decision = roles.decide_role(product, role, runtime="ready")
                self.assertEqual(decision["status"], "eligible")
                self.assertEqual(decision["qualification"], "repository_policy")
        for product in roles.REVIEW_ONLY:
            self.assertEqual(roles.decide_role(product, "orchestrator")["status"], "ineligible")
            self.assertEqual(roles.decide_role(product, "builder")["status"], "ineligible")
        for transport in ("devin_cli", "devin_api_v3"):
            decision = roles.decide_role("devin", "reviewer", transport=transport, runtime="ready")
            self.assertEqual((decision["status"], decision["scope"]), ("eligible", "informational"))

    def test_policy_cannot_contain_self_attested_evidence(self):
        for value in (True, {"qualified": True}, {"verified": True}, {"qualification": "private value"}):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                roles.role_policy({"role_policy": {"devin": {"orchestrator": value}}})

    def test_closed_schema_covers_every_returned_decision(self):
        schema = json.loads((ROOT / "src/code_mower/role_eligibility.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        for product in roles.PRODUCTS:
            for role in roles.ROLES:
                decision = roles.decide_role(product, role, bounded=True)
                self.assertEqual(set(decision), set(schema["required"]))
                self.assertEqual(set(decision), set(schema["properties"]))
                for key, value in decision.items():
                    rule = schema["properties"][key]
                    self.assertIn(value, rule.get("enum", [rule.get("const")]))


class SessionRoleAdmissionTests(unittest.TestCase):
    def test_unqualified_host_or_handoff_fails_before_any_local_write(self):
        for arguments in (("--host", "devin"), ("--host", "devin-api-v3"),
                          ("--host", "codex", "--orchestrator", "devin")):
            with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                previous = Path.cwd()
                try:
                    os.chdir(tmp)
                    self.assertEqual(session.main(["start", "--repo", "owner/repo", *arguments]), 1)
                    self.assertEqual(list(Path(tmp).iterdir()), [])
                    self.assertIn("cannot act as orchestrator", errors.getvalue())
                finally:
                    os.chdir(previous)

    def test_historical_devin_brief_cannot_resume_context_mutations(self):
        brief = session.build_session(repo="owner/repo", host="codex", selected=("codex",), config={})
        brief.update(id="a" * 32, host="devin", orchestrator="devin",
                     lease={"state": "absent", "mutating": False})
        brief.pop("role_eligibility")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old-brief.json"
            path.write_text(json.dumps(brief))
            args = argparse.Namespace(session_file=path, context_state_dir=Path(tmp) / "private",
                                      config=None, repo_path=Path(tmp), context_command="fetch")
            with mock.patch.object(session.context_session, "association_store", side_effect=AssertionError("state access")):
                with self.assertRaisesRegex(config.ConfigError, "cannot act as orchestrator"):
                    session._run_context_command(args)
            args.context_command = "status"
            with mock.patch.object(session.context_session, "read", return_value=None), \
                    mock.patch.object(session.session_lease, "verify_live_lease", return_value={"mutating": False}):
                _payload, code = session._run_context_command(args)
                self.assertEqual(code, 0)

    def test_human_start_lists_exact_identity_safe_lease_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(tmp)
                (Path(tmp) / ".git").mkdir()
                with redirect_stdout(output):
                    self.assertEqual(session.main(["start", "--repo", "owner/repo", "--host", "codex"]), 0)
                saved = next((Path(tmp) / ".code-mower/sessions").glob("*.json"))
                brief = json.loads(saved.read_text())
                self.assertIn("Inspect lease: code-mower session lease show", output.getvalue())
                command = "code-mower session lease release --session-id " + brief["id"]
                self.assertIn(command, output.getvalue())
                self.assertNotIn(command, saved.read_text())
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(session.main(["lease", "release", "--session-id", brief["id"]]), 0)
            finally:
                os.chdir(previous)

    def test_default_hosts_and_informational_devin_selection_stay_usable(self):
        for host in ("claude", "codex", "cursor"):
            brief = session.build_session(repo="owner/repo", host=host,
                selected=("claude", "codex", "devin_api_v3"), config={})
            self.assertEqual(brief["orchestrator"], host)
            devin = brief["participants"][-1]
            self.assertFalse(devin["can_coordinate"])
            self.assertEqual(devin["builder"]["eligibility"]["scope"], "bounded")
            self.assertFalse(devin["reviewer"]["merge_authority"])
            self.assertEqual(devin["reviewer"]["eligibility"]["scope"], "informational")

    def test_explicit_devin_review_flags_do_not_self_qualify(self):
        for lane_id in ("devin_cli", "devin"):
            lane = participants.reference_review_config(lane_id)
            lane.update(merge_authority=True, informational=False)
            with self.assertRaises(config.ConfigError):
                normalize_lane(lane_id, lane)
            source = config.load_config(STARTER)
            source["lanes"][lane_id] = lane
            self.assertTrue(any(issue.path == "lanes." + lane_id for issue in config.validate_config(source)))
            with self.assertRaises(config.ConfigError):
                init.render_init_plan(source, config_path=str(STARTER))

    def test_setup_uses_the_same_builder_policy_without_changing_defaults(self):
        source = config.load_config(STARTER)
        ordinary = init.render_init_plan(source, config_path=str(STARTER), builders=("codex", "claude"))
        self.assertEqual(set(ordinary.data["builder_loop"]["role_eligibility"]), {"claude", "codex"})
        disabled = copy.deepcopy(source)
        disabled["role_policy"] = {"codex": {"builder": {"enabled": False}}}
        with self.assertRaises(config.ConfigError):
            init.render_init_plan(disabled, config_path=str(STARTER), builders=("codex",))
        selected = participants.config_with_participants(source, ("claude", "codex", "devin"))
        self.assertFalse(selected["lanes"]["devin_cli"]["merge_authority"])

    def test_readiness_exposes_shared_closed_decisions_without_promoting_roles(self):
        findings = devin_readiness(transport="devin_api_v3", config={}, env={})
        finding = next(row for row in findings if row.name == "provider.devin.role_eligibility")
        decisions = finding.detail["roles"]
        self.assertEqual(decisions["builder"]["qualification"], "qualified")
        self.assertEqual(decisions["orchestrator"]["status"], "ineligible")
        self.assertEqual(decisions["merge_reviewer"]["status"], "ineligible")
        self.assertEqual(decisions["reviewer"]["scope"], "informational")


class HostedRoleAdmissionTests(WorkOrderCase):
    def test_denied_dispatch_never_opens_state_or_calls_a_provider(self):
        for configuration, runtime in ((None, "ready"),
                                       (policy("builder", enabled=False), "ready"),
                                       (policy("builder", qualification="missing"), "ready"),
                                       ({}, "unavailable"), ({}, "unchecked")):
            self.service.role_config, self.service.runtime = configuration, runtime
            before = sorted(self.root.rglob("*"))
            with mock.patch.object(self.service.store, "locked", side_effect=AssertionError("unexpected write")):
                with self.assertRaisesRegex(RemoteError, "role_not_eligible"):
                    self.run_order("dispatch")
            self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_unchecked_runtime_can_preview_but_cannot_start_work(self):
        self.service = DevinWorkOrders(self.root / "builder", self.remote, self.github, config={})
        with mock.patch.object(self.service.store, "locked", side_effect=AssertionError("unexpected write")):
            self.assertTrue(self.service.run("dispatch", self.order)["apply_required"])
            with self.assertRaisesRegex(RemoteError, "role_not_eligible"):
                self.run_order("dispatch")

    def test_omitted_runtime_cannot_start_work_but_old_bindings_remain_manageable(self):
        self.run_order("dispatch")
        self.service = DevinWorkOrders(self.root / "builder", self.remote, self.github)
        self.assertEqual(self.run_order("status")["session"]["state"], "running")
        with mock.patch.object(self.service.store, "locked", side_effect=AssertionError("unexpected write")):
            with self.assertRaisesRegex(RemoteError, "role_not_eligible"):
                self.run_order("clarify", request="clarify-one", prose="bounded answer")
        self.assertEqual(self.run_order("cancel", request="cancel-old")["session"]["state"], "terminated")

    def test_expired_eligibility_preserves_binding_inspection_collection_and_cancel(self):
        self.run_order("dispatch")
        self.complete()
        self.run_order("collect")
        original_binding = self.service._binding(self.order)
        self.service.role_config = policy("builder", qualification="no-longer-current")
        self.service.runtime = "unavailable"
        self.assertEqual(self.service._binding(self.order), original_binding)
        self.assertEqual(self.run_order("status")["session"]["state"], "complete")
        self.assertEqual(self.run_order("collect")["verified_pr"]["head_sha"], HEAD)
        with mock.patch.object(self.service.store, "locked", side_effect=AssertionError("unexpected write")):
            with self.assertRaisesRegex(RemoteError, "role_not_eligible"):
                self.run_order("fix", request="fix-one", prose="approved fix", reviewed_head=HEAD)
        self.assertEqual(self.run_order("cancel", request="cancel-one")["session"]["state"], "terminated")


if __name__ == "__main__":
    unittest.main()
