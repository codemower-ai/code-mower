"""Role qualification is distinct from selection, policy, and readiness."""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import tempfile
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from code_mower import config, init, lane_delivery, participants, remote_session_cli, role_eligibility as roles, session
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

    def test_future_reviewer_record_still_respects_repository_narrowing(self):
        key = "separate-reviewed-role"
        record = roles.Qualification("devin", "reviewer", "devin_cli", "local_runner", "unrestricted",
                                     "https://example.test/qualification")
        lane = participants.reference_review_config("devin_cli")
        lane.update(merge_authority=True, informational=False, role_qualification=key)
        with mock.patch.object(roles, "QUALIFICATIONS", {key: record}):
            self.assertTrue(normalize_lane("devin_cli", lane, config={})["merge_authority"])
            for narrowing in (policy("reviewer", enabled=False), policy("reviewer", qualification="revoked")):
                with self.assertRaises(config.ConfigError):
                    normalize_lane("devin_cli", lane, config=narrowing)

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

    def test_explicit_qualification_narrows_otherwise_preserved_provider_policy(self):
        for product in ("codex", "claude", "cursor"):
            decision = roles.decide_role(product, "builder", runtime="ready",
                config={"role_policy": {product: {"builder": {"qualification": "missing"}}}})
            self.assertEqual(decision["reason"], "qualification_missing")

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


class LocalBuilderRoleAdmissionTests(unittest.TestCase):
    def test_local_admission_is_read_only_and_checks_runtime_policy_and_transport(self):
        for content, runtime, expected in (
            (None, "ready", 0), (None, "unchecked", 2), (None, "unavailable", 2),
            ("role_policy:\n  devin:\n    builder:\n      enabled: false\n", "ready", 2),
            ("role_policy:\n  devin:\n    builder:\n      qualification: missing\n", "ready", 2),
            ("session_defaults:\n  transports:\n    devin: devin_api_v3\n", "ready", 2),
        ):
            with self.subTest(content=content, runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if content is not None:
                    (root / "code-mower.yml").write_text(content)
                before = sorted(root.rglob("*"))
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                    result = lane_delivery.main(["admit-builder", "--checkout", str(root), "--lane", "devin",
                                                 "--runtime-readiness", runtime])
                self.assertEqual(result, expected)
                self.assertEqual(sorted(root.rglob("*")), before)
                if result == 0:
                    self.assertEqual(json.loads(output.getvalue())["qualification"], "qualified")

    def test_local_admission_rejects_nonregular_or_unreadable_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "code-mower.yml"
            path.mkdir()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(lane_delivery.main(["admit-builder", "--checkout", str(root),
                    "--lane", "devin", "--runtime-readiness", "ready"]), 2)
            self.assertIn("regular trusted repository configuration", errors.getvalue())
            path.rmdir()
            path.write_text("private malformed config value")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(lane_delivery.main(["admit-builder", "--checkout", str(root),
                    "--lane", "devin", "--runtime-readiness", "ready"]), 2)
            self.assertNotIn("private malformed", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_generated_runner_rejects_revoked_policy_before_provider_launch(self):
        from test_devin_builder_lane import _generate, _FAKE_GIT, _lane_delivery_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated"
            _generate(generated)
            work_root = root / "work"
            checkout = work_root / "devin/owner__repo"
            (checkout / ".git/hooks").mkdir(parents=True)
            (checkout / "code-mower.yml").write_text("role_policy:\n  devin:\n    builder:\n      enabled: false\n")
            binaries = root / "bin"
            binaries.mkdir()
            gh_script = """#!/usr/bin/env bash
set -eu
case "$1 $2" in
  "repo view") printf 'main\n' ;;
  "pr list") printf '[]\n' ;;
  "api user") printf 'owner\n' ;;
  "issue view")
    case " $* " in
      *"--json labels"*) printf '["tier:R","builder:devin","dispatched:devin"]\n' ;;
      *) printf '{"number":12,"title":"Bounded task","body":"Implement the assigned task.","labels":[{"name":"tier:R"}],"author":{"login":"owner"},"comments":[]}\n' ;;
    esac ;;
  *) exit 2 ;;
esac
"""
            for name, body in (("git", _FAKE_GIT), ("gh", gh_script),
                               ("devin", '#!/bin/sh\n: > "$PROVIDER_MARKER"\n')):
                path = binaries / name
                path.write_text(body)
                path.chmod(0o755)
            marker = root / "provider-ran"
            result = subprocess.run([str(generated / "tools/lanes/run_mac_lane.sh"), "--lane", "devin",
                                     "--repo", "owner/repo", "--target", "issue:12", "--max-minutes", "1"],
                                    cwd=generated, env={**os.environ, **_lane_delivery_env(),
                                        "LANE_WORK_ROOT": str(work_root), "LANE_TRUSTED_AUTHORS": "owner",
                                        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                                        "PROVIDER_MARKER": str(marker)},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("repository role policy disables", result.stderr)
            self.assertFalse(marker.exists())
            source = (ROOT / "src/code_mower/templates/lanes/run_mac_lane.sh").read_text()
            self.assertLess(source.index('"${lane_delivery[@]}" admit-builder'), source.index("--reserve-launch"))


class RawRemoteRoleAdmissionTests(unittest.TestCase):
    def test_denied_devin_cli_new_work_has_no_credential_prose_or_state_access(self):
        for command in ("dispatch", "message"):
            for configuration, runtime in ((None, "ready"), (policy("builder", enabled=False), "ready"),
                                           (policy("builder", qualification="missing"), "ready"),
                                           ({}, "unchecked"), ({}, "unavailable")):
                with self.subTest(command=command, runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    args = [command, "private-work", "--provider", "devin", "--apply",
                            "--remote-state-dir", str(root / "state"), "--input-file", str(root / "absent"),
                            "--runtime-readiness", runtime]
                    args += ["--repo", "owner/repo"] if command == "dispatch" else ["--request", "message-one"]
                    if configuration is not None:
                        path = root / "policy.yml"
                        settings = configuration.get("role_policy", {}).get("devin", {}).get("builder", {})
                        content = "role_policy:\n  devin:\n    builder:\n" + "".join(
                            f"      {key}: {str(value).lower() if isinstance(value, bool) else value}\n"
                            for key, value in settings.items()
                        ) if settings else "version: 1\n"
                        path.write_text(content)
                        args += ["--config", str(path)]
                    before = sorted(root.rglob("*"))
                    errors = io.StringIO()
                    with mock.patch("code_mower.devin_api.credentials_from_env", side_effect=AssertionError("credential access")), \
                            mock.patch.object(remote_session_cli, "RemoteSessions", side_effect=AssertionError("state access")), \
                            redirect_stdout(io.StringIO()), redirect_stderr(errors):
                        self.assertEqual(session.main(args), 1)
                    self.assertIn("role_not_eligible", errors.getvalue())
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertEqual(sorted(root.rglob("*")), before)

    def test_eligible_cli_work_keeps_request_identity_and_bounded_input(self):
        credentials = mock.Mock(has_credentials=True, org_id="test", api_key="test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configuration = root / "code-mower.yml"
            configuration.write_text("version: 1\n")
            prose = root / "input.txt"
            prose.write_text("approved bounded work")
            for command in ("dispatch", "message"):
                args = [command, "work", "--provider", "devin", "--apply", "--config", str(configuration),
                        "--runtime-readiness", "ready", "--input-file", str(prose)]
                args += ["--repo", "owner/repo", "--max-acu-limit", "1"] if command == "dispatch" else ["--request", "message-one"]
                with mock.patch("code_mower.devin_api.credentials_from_env", return_value=credentials), \
                        mock.patch("code_mower.devin_api.repository_scope_acknowledged", return_value=True), \
                        mock.patch.object(remote_session_cli, "DevinClient"), \
                        mock.patch.object(remote_session_cli, "DevinProvider"), \
                        mock.patch.object(remote_session_cli, "RemoteSessions") as remote, redirect_stdout(io.StringIO()):
                    remote.return_value.run.return_value = {"state": "running"}
                    self.assertEqual(session.main(args), 0)
                    call = remote.return_value.run.call_args
                    self.assertEqual(call.args, (command, "work"))
                    self.assertEqual(call.kwargs["prose"], "approved bounded work")
                    self.assertTrue(call.kwargs["apply"])
                    if command == "dispatch":
                        self.assertEqual((call.kwargs["repo"], call.kwargs["limit"]), ("owner/repo", 1))
                    else:
                        self.assertEqual(call.kwargs["request"], "message-one")

    def test_existing_devin_binding_controls_do_not_require_builder_admission(self):
        credentials = mock.Mock(has_credentials=True, org_id="test", api_key="test")
        for command in ("status", "collect", "cancel"):
            args = [command, "old-work", "--provider", "devin", "--apply"]
            if command == "cancel":
                args += ["--request", "cancel-one"]
            with mock.patch("code_mower.devin_api.credentials_from_env", return_value=credentials), \
                    mock.patch.object(remote_session_cli, "DevinClient"), \
                    mock.patch.object(remote_session_cli, "DevinProvider"), \
                    mock.patch.object(remote_session_cli, "RemoteSessions") as remote, \
                    mock.patch.object(remote_session_cli, "require_builder", side_effect=AssertionError("unexpected admission")), \
                    redirect_stdout(io.StringIO()):
                remote.return_value.run.return_value = {"state": "running"}
                self.assertEqual(session.main(args), 0)
                self.assertEqual(remote.return_value.run.call_args.args, (command, "old-work"))
                if command == "cancel":
                    self.assertEqual(remote.return_value.run.call_args.kwargs["request"], "cancel-one")


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
