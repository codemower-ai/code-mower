"""Local builder delivery, process cleanup, and recovery handoffs (#751)."""

from __future__ import annotations

import contextlib
import errno
import gc
import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
import warnings
from pathlib import Path

from code_mower import lane_delivery


ROOT = Path(__file__).resolve().parents[1]
RUNNER_TEMPLATE = ROOT / "templates/lanes/run_mac_lane.sh"
PACKAGED_RUNNER_TEMPLATE = ROOT / "src/code_mower/templates/lanes/run_mac_lane.sh"
REPO_RUNNER = ROOT / "tools/lanes/run_mac_lane.sh"
SHA_A = "a" * 40
SHA_B = "b" * 40


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    """Return whether ``pid`` is gone within ``timeout`` seconds."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _state(**overrides: object) -> lane_delivery.TargetState:
    payload: dict[str, object] = {"kind": "issue", "number": "751"}
    payload.update(overrides)
    return lane_delivery.TargetState.from_mapping(payload)


class DeliveryClassificationTests(unittest.TestCase):
    def test_exit_zero_without_delivery_is_not_a_delivery(self) -> None:
        before = _state()
        after = _state()

        outcome = lane_delivery.classify_delivery(before, after, provider_exit=0)

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_NONE)
        self.assertEqual(outcome.reason, "exit_zero_without_delivery")

    def test_exit_zero_with_unchanged_head_is_not_a_delivery(self) -> None:
        before = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A)
        after = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A)

        outcome = lane_delivery.classify_delivery(before, after, provider_exit=0)

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "exit_zero_without_delivery")

    def test_new_pull_request_is_a_delivery(self) -> None:
        outcome = lane_delivery.classify_delivery(
            _state(),
            _state(pr_number="901", head_sha=SHA_A),
            provider_exit=0,
        )

        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_PR_OPENED)

    def test_advanced_head_is_a_delivery(self) -> None:
        before = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A)
        after = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_B)

        outcome = lane_delivery.classify_delivery(before, after, provider_exit=0)

        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_HEAD_ADVANCED)

    def test_nonzero_provider_exit_never_delivers(self) -> None:
        outcome = lane_delivery.classify_delivery(
            _state(),
            _state(pr_number="901", head_sha=SHA_A),
            provider_exit=1,
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "provider_exit_nonzero")

    def test_declared_no_change_needs_a_runner_owned_comment(self) -> None:
        unvalidated = lane_delivery.classify_delivery(
            _state(), _state(), provider_exit=0, declared_outcome="no_change"
        )
        validated = lane_delivery.classify_delivery(
            _state(),
            _state(runner_comment_id="12345"),
            provider_exit=0,
            declared_outcome="no_change",
        )

        self.assertFalse(unvalidated.delivered)
        self.assertEqual(unvalidated.reason, "no_change_missing_runner_comment")
        self.assertTrue(validated.delivered)
        self.assertEqual(validated.transition, lane_delivery.TRANSITION_NO_CHANGE)

    def test_declared_owner_action_needs_label_and_comment(self) -> None:
        missing_label = lane_delivery.classify_delivery(
            _state(),
            _state(runner_comment_id="1"),
            provider_exit=0,
            declared_outcome="owner_action",
        )
        missing_comment = lane_delivery.classify_delivery(
            _state(),
            _state(labels=["needs-owner"]),
            provider_exit=0,
            declared_outcome="owner_action",
        )
        validated = lane_delivery.classify_delivery(
            _state(),
            _state(labels=["needs-owner"], runner_comment_id="1"),
            provider_exit=0,
            declared_outcome="owner_action",
        )

        self.assertEqual(missing_label.reason, "owner_action_missing_label")
        self.assertEqual(missing_comment.reason, "owner_action_missing_runner_comment")
        self.assertTrue(validated.delivered)
        self.assertEqual(validated.transition, lane_delivery.TRANSITION_OWNER_ACTION)

    def test_unbounded_declared_outcome_is_rejected(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.classify_delivery(
                _state(), _state(), provider_exit=0, declared_outcome="shipped"
            )

    def test_failed_before_snapshot_does_not_fabricate_a_pr_opened(self) -> None:
        # The before lookup failed, so pr_number is empty because it is unknown
        # and not because no PR exists. The after snapshot sees the PR that was
        # already open, and the naive comparison reads that as pr_opened.
        before = _state(snapshot_complete=False)
        after = _state(pr_number="901", head_sha=SHA_A)

        outcome = lane_delivery.classify_delivery(before, after, provider_exit=0)

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_UNKNOWN)
        self.assertEqual(outcome.reason, "target_snapshot_unavailable")

    def test_failed_before_snapshot_does_not_fabricate_a_head_advance(self) -> None:
        before = _state(
            kind="pr", number="900", pr_number="900", snapshot_complete=False
        )
        after = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A)

        outcome = lane_delivery.classify_delivery(before, after, provider_exit=0)

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_UNKNOWN)
        self.assertEqual(outcome.reason, "target_snapshot_unavailable")

    def test_failed_after_snapshot_is_not_a_delivery(self) -> None:
        before = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A)
        after = _state(
            kind="pr",
            number="900",
            pr_number="900",
            head_sha=SHA_B,
            snapshot_complete=False,
        )

        outcome = lane_delivery.classify_delivery(before, after, provider_exit=0)

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "target_snapshot_unavailable")

    def test_incomplete_snapshot_also_rejects_a_declared_outcome(self) -> None:
        # A declared outcome is validated against the comment and label in the
        # after snapshot, which an incomplete snapshot cannot vouch for either.
        outcome = lane_delivery.classify_delivery(
            _state(),
            _state(
                labels=["needs-owner"],
                runner_comment_id="1",
                snapshot_complete=False,
            ),
            provider_exit=0,
            declared_outcome="owner_action",
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "target_snapshot_unavailable")

    def test_complete_snapshots_still_classify_normally(self) -> None:
        outcome = lane_delivery.classify_delivery(
            _state(snapshot_complete=True),
            _state(pr_number="901", head_sha=SHA_A, snapshot_complete=True),
            provider_exit=0,
        )

        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_PR_OPENED)

    def test_snapshot_complete_must_be_a_boolean(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            _state(snapshot_complete="true")

    def test_classify_cli_requires_an_explicit_snapshot_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.json"
            after = root / "after.json"
            silent = _state().as_dict()
            silent.pop("snapshot_complete")
            before.write_text(json.dumps(silent), encoding="utf-8")
            after.write_text(
                json.dumps(_state(pr_number="901", head_sha=SHA_A).as_dict()),
                encoding="utf-8",
            )

            rc = lane_delivery.main(
                [
                    "classify",
                    "--before",
                    str(before),
                    "--after",
                    str(after),
                    "--provider-exit",
                    "0",
                ]
            )

        self.assertEqual(rc, 2)

    def test_classify_cli_exits_nonzero_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.json"
            after = root / "after.json"
            before.write_text(json.dumps(_state().as_dict()), encoding="utf-8")
            after.write_text(json.dumps(_state().as_dict()), encoding="utf-8")

            rc = lane_delivery.main(
                [
                    "classify",
                    "--before",
                    str(before),
                    "--after",
                    str(after),
                    "--provider-exit",
                    "0",
                ]
            )

        self.assertEqual(rc, 3)


class ProcessGroupCleanupTests(unittest.TestCase):
    def test_terminate_escalates_from_term_to_kill(self) -> None:
        sent: list[int] = []

        def fake_killpg(_pgid: int, sig: int) -> None:
            sent.append(sig)

        signals = lane_delivery.terminate_process_group(
            424242,
            grace_seconds=0.05,
            poll_interval=0.01,
            killpg=fake_killpg,
            is_group_alive=lambda _pgid: True,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(signals, ("SIGTERM", "SIGKILL"))
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])

    def test_terminate_stops_at_term_when_the_group_drains(self) -> None:
        signals = lane_delivery.terminate_process_group(
            424242,
            grace_seconds=1.0,
            poll_interval=0.01,
            killpg=lambda _pgid, _sig: None,
            is_group_alive=lambda _pgid: False,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(signals, ("SIGTERM",))

    def test_terminate_refuses_the_runners_own_process_group(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.terminate_process_group(os.getpgrp())
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.terminate_process_group(0)

    def _terminate_with_unkillable_group(
        self, raised: OSError
    ) -> tuple[list[int], tuple[str, ...]]:
        sent: list[int] = []

        def fake_killpg(_pgid: int, sig: int) -> None:
            sent.append(sig)
            if sig == signal.SIGKILL:
                raise raised

        signals = lane_delivery.terminate_process_group(
            424242,
            grace_seconds=0.05,
            poll_interval=0.01,
            killpg=fake_killpg,
            is_group_alive=lambda _pgid: True,
            sleep=lambda _seconds: None,
        )
        return sent, signals

    def test_terminate_survives_a_group_that_vanishes_mid_cleanup(self) -> None:
        """Cleanup races the group it tears down, so it must not raise.

        ``SIGKILL`` after the grace period lands on a group that may already be
        gone: Linux reports that as ``ESRCH`` and Darwin's ``killpg`` as
        ``EPERM``. Neither may escape as an exception, and neither may be
        recorded as a signal that was actually delivered.
        """

        for label, raised in (
            ("ESRCH", OSError(errno.ESRCH, "No such process")),
            ("EPERM", PermissionError(errno.EPERM, "Operation not permitted")),
        ):
            with self.subTest(errno=label):
                sent, signals = self._terminate_with_unkillable_group(raised)

                self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
                self.assertEqual(signals, ("SIGTERM",))

    def test_timeout_reaps_the_whole_provider_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_pid_file = root / "child.pid"
            script = (
                "sleep 120 & echo $! > "
                + str(child_pid_file)
                + "; sleep 120"
            )

            result = lane_delivery.supervise_process(
                ["bash", "-c", script],
                log_path=root / "run.log",
                timeout_seconds=1.0,
                term_grace_seconds=1.0,
            )

            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, lane_delivery.EXIT_TIMEOUT)
            self.assertIn("SIGTERM", result.signals_sent)

            grandchild = int(child_pid_file.read_text(encoding="utf-8").strip())

        self.assertTrue(
            _wait_for_pid_exit(grandchild),
            "orphaned provider transport survived the timeout",
        )

    def test_output_overflow_terminates_the_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "run.log"
            child_pid_file = root / "child.pid"
            script = (
                "sleep 120 & echo $! > "
                + str(child_pid_file)
                + "; while true; do printf 'noise %s\\n' $RANDOM; done"
            )

            result = lane_delivery.supervise_process(
                ["bash", "-c", script],
                log_path=log_path,
                timeout_seconds=30.0,
                max_log_bytes=4096,
                term_grace_seconds=1.0,
            )

            self.assertTrue(result.overflowed)
            self.assertEqual(result.exit_code, lane_delivery.EXIT_OUTPUT_OVERFLOW)
            self.assertIn("SIGTERM", result.signals_sent)
            self.assertLessEqual(log_path.stat().st_size, 4096)

            grandchild = int(child_pid_file.read_text(encoding="utf-8").strip())

        self.assertTrue(
            _wait_for_pid_exit(grandchild),
            "orphaned provider transport survived the output overflow",
        )

    def test_interruption_cleanup(self) -> None:
        """Interrupting the runner reaps the group and closes every pipe."""

        # Restored by ``supervise_process``, so a signal that lands after the
        # call cannot fall through to SIG_DFL and kill the test runner.
        previous = signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                child_pid_file = root / "child.pid"
                script = (
                    "sleep 120 & echo $! > "
                    + str(child_pid_file)
                    + f"; kill -TERM {os.getpid()}; sleep 120"
                )

                gc.collect()
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    result = lane_delivery.supervise_process(
                        ["bash", "-c", script],
                        log_path=root / "run.log",
                        timeout_seconds=30.0,
                        term_grace_seconds=1.0,
                    )
                    gc.collect()

                leaked = [
                    str(entry.message)
                    for entry in caught
                    if issubclass(entry.category, ResourceWarning)
                ]
                grandchild = int(child_pid_file.read_text(encoding="utf-8").strip())

            self.assertTrue(result.interrupted)
            self.assertEqual(result.reason, "interrupted")
            self.assertEqual(result.exit_code, lane_delivery.EXIT_INTERRUPTED)
            self.assertIn("SIGTERM", result.signals_sent)
            self.assertEqual(leaked, [])
            self.assertTrue(
                _wait_for_pid_exit(grandchild),
                "orphaned provider transport survived the interruption",
            )
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_clean_provider_exit_is_reported_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = lane_delivery.supervise_process(
                ["bash", "-c", "printf 'done\\n'; exit 7"],
                log_path=Path(tmp) / "run.log",
                timeout_seconds=30.0,
            )

        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.reason, "completed")

    def test_supervise_cli_dispatches_to_the_provider(self) -> None:
        """The remainder must not share the subparsers destination.

        Naming it ``command`` made argparse overwrite the selected subcommand
        with the provider argv, so ``main`` never reached ``_supervise_main``
        and every supervised run raised instead of running the provider. The
        rest of this class calls ``supervise_process`` directly, so nothing
        covered the argv the runner actually passes.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "run.log"
            status_path = root / "status.json"

            rc = lane_delivery.main(
                [
                    "supervise",
                    "--log",
                    str(log_path),
                    "--timeout-seconds",
                    "30",
                    "--status-file",
                    str(status_path),
                    "--",
                    "bash",
                    "-c",
                    "printf 'done\\n'; exit 7",
                ]
            )

            log = log_path.read_text(encoding="utf-8")
            status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(rc, 7)
        self.assertIn("done", log)
        self.assertEqual(status["exit_code"], 7)
        self.assertEqual(status["reason"], "completed")

    def test_supervise_cli_keeps_provider_flags_out_of_its_own_options(self) -> None:
        """Provider argv reusing a supervisor flag name reaches the provider."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "run.log"

            rc = lane_delivery.main(
                [
                    "supervise",
                    "--log",
                    str(log_path),
                    "--timeout-seconds",
                    "30",
                    "--",
                    "bash",
                    "-c",
                    'printf "%s\\n" "$@"',
                    "provider",
                    "--log",
                    "/provider/owned/path",
                ]
            )

            log = log_path.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertEqual(log.split(), ["--log", "/provider/owned/path"])

    def test_supervise_cli_reports_a_timeout_as_the_timeout_code(self) -> None:
        """A capped run must not flatten into a generic nonzero provider exit."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"

            rc = lane_delivery.main(
                [
                    "supervise",
                    "--log",
                    str(root / "run.log"),
                    "--timeout-seconds",
                    "1",
                    "--status-file",
                    str(status_path),
                    "--",
                    "bash",
                    "-c",
                    "sleep 120",
                ]
            )

            status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(rc, lane_delivery.EXIT_TIMEOUT)
        self.assertEqual(status["reason"], "timeout")

    def test_supervise_cli_rejects_an_empty_provider_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = lane_delivery.main(
                [
                    "supervise",
                    "--log",
                    str(Path(tmp) / "run.log"),
                    "--timeout-seconds",
                    "30",
                    "--",
                ]
            )

        self.assertEqual(rc, 2)


class RecoveryHandoffTests(unittest.TestCase):
    def _valid(self, **overrides: object) -> lane_delivery.Handoff:
        kwargs: dict[str, object] = {
            "source_lane": "codex",
            "destination_lane": "claude",
            "target_pr": "owner/repo#750",
            "expected_head": SHA_A,
            "running_lane": "claude",
            "repo": "owner/repo",
            "observed_head": SHA_A,
            "target_branch": "codex/750-fix",
            "source_branch_prefixes": ["codex/"],
        }
        kwargs.update(overrides)
        return lane_delivery.validate_handoff(**kwargs)  # type: ignore[arg-type]

    def test_explicit_handoff_is_accepted(self) -> None:
        handoff = self._valid()

        self.assertEqual(handoff.source_lane, "codex")
        self.assertEqual(handoff.destination_lane, "claude")
        self.assertEqual(handoff.target_pr, "owner/repo#750")
        self.assertEqual(handoff.expected_head, SHA_A)

    def test_handoff_rejects_same_lane_source_and_destination(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(source_lane="claude")

    def test_handoff_rejects_a_destination_that_is_not_running(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(destination_lane="cursor")

    def test_handoff_rejects_a_pr_from_another_repository(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(target_pr="other/repo#750")

    def test_handoff_rejects_a_stale_expected_head(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(observed_head=SHA_B)

    def test_handoff_requires_every_field(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(source_lane="")
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(expected_head="")
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            self._valid(target_branch="")

    def test_handoff_rejects_a_branch_the_source_lane_does_not_own(self) -> None:
        # Naming a cooperating lane as the source must not turn into authority
        # over any foreign branch: another builder's, or a bot's.
        for branch in (
            "claude/751-other",
            "dependabot/npm_and_yarn/left-pad-1.3.0",
            "main",
            "codex-lookalike/750-fix",
        ):
            with self.subTest(branch=branch):
                with self.assertRaises(lane_delivery.LaneDeliveryError) as caught:
                    self._valid(target_branch=branch)
                self.assertIn("not owned by source lane", str(caught.exception))

    def test_handoff_rejects_a_source_lane_with_no_configured_prefixes(self) -> None:
        # An unknown source lane resolves to no prefixes, and a lane that owns
        # nothing has nothing to hand over.
        for prefixes in ([], [""], ()):
            with self.subTest(prefixes=prefixes):
                with self.assertRaises(lane_delivery.LaneDeliveryError) as caught:
                    self._valid(source_branch_prefixes=prefixes)
                self.assertIn("no configured branch prefixes", str(caught.exception))

    def test_handoff_accepts_any_configured_prefix_of_the_source_lane(self) -> None:
        handoff = self._valid(
            target_branch="codex-fix/750",
            source_branch_prefixes=["codex/", "codex-fix/"],
        )

        self.assertEqual(handoff.target_branch, "codex-fix/750")

    def test_handoff_cli_requires_the_source_lanes_own_prefixes(self) -> None:
        def argv(**overrides: str) -> list[str]:
            fields = {
                "--lane": "claude",
                "--repo": "owner/repo",
                "--source-lane": "codex",
                "--destination-lane": "claude",
                "--target-pr": "owner/repo#750",
                "--expected-head": SHA_A,
                "--observed-head": SHA_A,
                "--target-branch": "codex/750-fix",
            }
            fields.update({f"--{k.replace('_', '-')}": v for k, v in overrides.items()})
            return ["handoff", *(part for pair in fields.items() for part in pair)]

        def run(args: list[str]) -> int:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                return lane_delivery.main(args)

        # Omitting the prefixes is a usage error, not a silently unchecked
        # handoff: argparse exits 2 before validation runs.
        with self.assertRaises(SystemExit) as missing:
            run(argv())
        self.assertEqual(missing.exception.code, 2)

        self.assertEqual(run([*argv(), "--source-branch-prefix", "codex/"]), 0)
        self.assertEqual(
            run(
                [
                    *argv(target_branch="dependabot/npm_and_yarn/left-pad-1.3.0"),
                    "--source-branch-prefix",
                    "codex/",
                ]
            ),
            2,
        )

    def test_lane_prefix_authority_is_unchanged(self) -> None:
        authority = lane_delivery.authorize_branch_write(
            lane="claude",
            branch="claude/751-thing",
            lane_branch_prefixes=["claude/"],
        )

        self.assertEqual(authority, "lane_prefix")

    def test_implicit_cross_lane_takeover_is_rejected(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError) as caught:
            lane_delivery.authorize_branch_write(
                lane="claude",
                branch="codex/750-fix",
                lane_branch_prefixes=["claude/"],
            )

        self.assertIn("implicit cross-lane takeover", str(caught.exception))

    def test_explicit_handoff_authorizes_exactly_the_named_branch(self) -> None:
        handoff = self._valid()

        authority = lane_delivery.authorize_branch_write(
            lane="claude",
            branch="codex/750-fix",
            lane_branch_prefixes=["claude/"],
            handoff=handoff,
        )
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.authorize_branch_write(
                lane="claude",
                branch="codex/999-other",
                lane_branch_prefixes=["claude/"],
                handoff=handoff,
            )

        self.assertEqual(authority, "explicit_handoff")


class AuthMaterialExposureTests(unittest.TestCase):
    def test_prompt_auth_material_discovery_is_detected(self) -> None:
        cases = {
            "gh_auth_token_command": "Run gh auth token to get the value.",
            "git_credential_command": "Use git credential fill for the password.",
            "credential_helper_output": "Read the credential.helper output.",
            "gh_hosts_file": "Look in gh/hosts.yml for the entry.",
            "netrc_file": "Check ~/.netrc first.",
            "keychain_lookup": "Try security find-generic-password -s github.",
            "private_key_file": "Fall back to id_rsa if needed.",
            "token_file_read": "cat ~/.config/token.json before starting.",
            "token_env_echo": "echo $GITHUB_TOKEN to confirm it is set.",
            "token_env_assignment": "Export GH_TOKEN=abc before running gh.",
        }

        for rule, text in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, lane_delivery.scan_auth_material(text))

    def test_clean_prompt_guidance_passes(self) -> None:
        text = (
            "The runner brokers the GitHub comments and labels this contract "
            "needs. Your shell already has authenticated GitHub access; never "
            "go looking for, read, or print any authentication material."
        )

        self.assertEqual(lane_delivery.scan_auth_material(text), ())
        lane_delivery.assert_prompt_free_of_auth_material(text)

    def test_shipped_lane_docs_and_runner_prompt_are_clean(self) -> None:
        prompt = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "docs/lanes").glob("*.md"))
        )

        self.assertEqual(lane_delivery.scan_auth_material(prompt), ())

    def test_scan_prompt_cli_rejects_a_dirty_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "clean.md"
            dirty = Path(tmp) / "dirty.md"
            clean.write_text("Open a PR and report the head SHA.", encoding="utf-8")
            dirty.write_text("First run gh auth token.", encoding="utf-8")

            self.assertEqual(
                lane_delivery.main(["scan-prompt", "--prompt-file", str(clean)]), 0
            )
            # 1 is a rule match. 2 means the scanner itself did not run, and the
            # runner must not report that as "the prompt was dirty".
            self.assertEqual(
                lane_delivery.main(["scan-prompt", "--prompt-file", str(dirty)]), 1
            )
            missing = Path(tmp) / "missing.md"
            self.assertEqual(
                lane_delivery.main(["scan-prompt", "--prompt-file", str(missing)]), 2
            )


class DeliveryOutcomeRecordTests(unittest.TestCase):
    # Not ``_outcome``: ``unittest.TestCase`` binds an ``_Outcome`` instance to
    # ``self._outcome`` in ``run()``, which shadows a helper of that name.
    def _pr_opened_outcome(self) -> lane_delivery.DeliveryOutcome:
        return lane_delivery.classify_delivery(
            _state(), _state(pr_number="901", head_sha=SHA_A), provider_exit=0
        )

    def test_helpers_do_not_shadow_testcase_internals(self) -> None:
        self.assertIsInstance(self._pr_opened_outcome(), lane_delivery.DeliveryOutcome)

        # ``unittest`` binds these on the instance, so a class-level helper
        # sharing one of their names is silently replaced at run time.
        reserved = {
            name for name in vars(unittest.TestCase()) if not name.startswith("__")
        }
        reserved.add("_outcome")
        for klass in (
            DeliveryClassificationTests,
            ProcessGroupCleanupTests,
            RecoveryHandoffTests,
            AuthMaterialExposureTests,
            DeliveryOutcomeRecordTests,
            RunnerScriptContractTests,
        ):
            with self.subTest(klass=klass.__name__):
                self.assertEqual(set(vars(klass)) & reserved, set())

    def test_record_carries_only_delivery_metadata(self) -> None:
        event = lane_delivery.build_delivery_outcome_event(
            lane="claude",
            repo="owner/repo",
            kind="issue",
            number="751",
            outcome=self._pr_opened_outcome(),
            supervision_reason="completed",
            signals_sent=["SIGTERM"],
            elapsed_seconds=91.5,
            user_interventions=0,
            handoff=lane_delivery.Handoff(
                source_lane="codex",
                destination_lane="claude",
                target_pr="owner/repo#750",
                expected_head=SHA_A,
                target_branch="codex/750-fix",
            ),
        )

        self.assertEqual(event["schema"], lane_delivery.DELIVERY_OUTCOME_SCHEMA)
        self.assertEqual(event["provider"]["exit_code"], 0)
        self.assertEqual(event["provider"]["supervision"], "completed")
        self.assertEqual(event["delivery"]["transition"], "pr_opened")
        self.assertEqual(event["handoff"]["source_lane"], "codex")
        self.assertEqual(event["metrics"]["elapsed_seconds"], 91.5)
        self.assertEqual(event["metrics"]["user_interventions"], 0)
        self.assertEqual(
            set(event),
            {
                "schema",
                "event_id",
                "created_at",
                "code_mower_version",
                "lane",
                "repo",
                "target",
                "provider",
                "delivery",
                "handoff",
                "metrics",
            },
        )

    def test_record_rejects_transcripts_paths_and_secrets(self) -> None:
        cases = (
            "line one\nline two",
            "/opt/lane/checkout",
            "GITHUB_TOKEN=abc",
            "Bearer abcdef012345",
            "x" * 400,
        )

        for value in cases:
            with self.subTest(value=value[:20]):
                with self.assertRaises(lane_delivery.LaneDeliveryError):
                    lane_delivery.build_delivery_outcome_event(
                        lane="claude",
                        repo="owner/repo",
                        kind="issue",
                        number="751",
                        outcome=self._pr_opened_outcome(),
                        supervision_reason=value,
                    )

    def test_record_rejects_credential_shaped_bearer_and_token_prefixes(self) -> None:
        cases = (
            "Bearer abcdef012345",
            "bearer abcdef012345",
            "BEARER AbCd1234EfGh5678",
            "authorization Bearer ya29.a0Ae4lvC1x-2y",
            "bearer ghp_0123456789abcdefghij",
            "ghp_0123456789abcdefghijklmnop",
            "gho_0123456789abcdefghijklmnop",
        )

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(lane_delivery.LaneDeliveryError):
                    lane_delivery.build_delivery_outcome_event(
                        lane="claude",
                        repo="owner/repo",
                        kind="issue",
                        number="751",
                        outcome=self._pr_opened_outcome(),
                        supervision_reason=value,
                    )

    def test_record_accepts_prose_containing_the_word_bearer(self) -> None:
        cases = (
            "bearer",
            "bearer of bad news",
            "bearer authentication is unchanged",
            "the bearer must sign",
            "bearer scheme rejected by provider",
        )

        for value in cases:
            with self.subTest(value=value):
                event = lane_delivery.build_delivery_outcome_event(
                    lane="claude",
                    repo="owner/repo",
                    kind="issue",
                    number="751",
                    outcome=self._pr_opened_outcome(),
                    supervision_reason=value,
                )
                self.assertEqual(event["provider"]["supervision"], value)

    def test_record_round_trips_as_json(self) -> None:
        event = lane_delivery.build_delivery_outcome_event(
            lane="claude",
            repo="owner/repo",
            kind="issue",
            number="751",
            outcome=self._pr_opened_outcome(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "outcome.json"
            lane_delivery.write_delivery_outcome_event(event, output)
            reloaded = json.loads(output.read_text(encoding="utf-8"))
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_delivery.write_delivery_outcome_event(event, output)

        self.assertEqual(reloaded["delivery"]["delivered"], True)


class RunnerScriptContractTests(unittest.TestCase):
    def test_template_copies_stay_identical(self) -> None:
        self.assertEqual(
            RUNNER_TEMPLATE.read_text(encoding="utf-8"),
            PACKAGED_RUNNER_TEMPLATE.read_text(encoding="utf-8"),
        )

    def test_runner_scripts_resolve_lane_delivery_explicitly(self) -> None:
        # Ambient PATH resolution is the whole problem: a consumer repo can
        # have an older installed code-mower, and a source checkout must run
        # the tree it ships in. The order is pin, then source checkout, then an
        # installed CLI that actually implements the command.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                pin = text.index('${CODE_MOWER_LANE_DELIVERY_CMD:-}')
                source = text.index('${repo_root}/src/code_mower/lane_delivery.py')
                installed = text.index("code-mower lane-delivery --help")
                self.assertLess(pin, source)
                self.assertLess(source, installed)
                # An installed CLI that predates the command disables the
                # contract for that run instead of failing every unit.
                self.assertIn("installed-cli-too-old", text)
                self.assertIn("lane-delivery contract inactive", text)

    def test_runner_scripts_treat_the_pin_as_one_executable(self) -> None:
        # The pin is an executable path or name, like every other command
        # override. Splitting an environment string into argv truncates any
        # executable whose path contains a space, so the expansion stays quoted
        # and the pin is resolved before the contract uses it.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(
                    'lane_delivery=( "${CODE_MOWER_LANE_DELIVERY_CMD}" )', text
                )
                self.assertNotIn("lane_delivery=( ${CODE_MOWER_LANE_DELIVERY_CMD} )", text)
                self.assertIn(
                    'command -v "${CODE_MOWER_LANE_DELIVERY_CMD}"', text
                )
                self.assertIn(
                    "CODE_MOWER_LANE_DELIVERY_CMD must name one executable, "
                    "not a command line",
                    text,
                )

    def test_repo_runner_accepts_a_pinned_path_containing_spaces(self) -> None:
        # A pinned wrapper under a directory with a space in its name resolves;
        # a command line pinned in the same variable does not, and says so
        # rather than failing later as a missing scanner or a matched rule.
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "Code Mower bin"
            spaced.mkdir()
            wrapper = spaced / "lane-delivery"
            wrapper.write_text(
                "#!/usr/bin/env bash\nexit 0\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)

            def run(pin: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        str(REPO_RUNNER),
                        "--lane",
                        "claude",
                        "--repo",
                        "owner",
                        "--max-minutes",
                        "1",
                    ],
                    env={**os.environ, "CODE_MOWER_LANE_DELIVERY_CMD": pin},
                    text=True,
                    capture_output=True,
                    check=False,
                )

            # `--repo owner` is rejected after command resolution, so reaching
            # that refusal proves the pin resolved without being truncated.
            accepted = run(str(wrapper))
            self.assertNotIn("must name one executable", accepted.stderr)
            self.assertIn("--repo must be OWNER/REPO", accepted.stderr)

            rejected = run(f"{wrapper} -m code_mower.lane_delivery")
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("must name one executable", rejected.stderr)
            self.assertNotIn("--repo must be OWNER/REPO", rejected.stderr)

    def test_runner_scripts_separate_scan_failure_from_a_rule_match(self) -> None:
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("the auth-material scanner failed to run", text)
                self.assertIn(
                    "the runner guidance carries auth-material discovery guidance", text
                )

    def test_runner_scripts_keep_the_provider_exit_code(self) -> None:
        # 3 means "exited zero and delivered nothing". A provider that failed
        # on its own keeps its own code so the caller can still diagnose it.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                keep = text.index('[ "$rc" -ne 0 ] && exit "$rc"')
                self.assertLess(keep, text.index("exit 3", keep - 200))
                # Timeout and overflow come from the supervision reason, not
                # from raw 124/125, which a provider may also return.
                self.assertIn('[ "$supervision_reason" = "timeout" ] && timed_out=1', text)
                self.assertIn(
                    '[ "$supervision_reason" = "output_overflow" ] && overflowed=1', text
                )

    def test_runner_scripts_wire_the_delivery_contract(self) -> None:
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("lane_delivery=(code-mower lane-delivery)", text)
                self.assertIn("capture_target_state", text)
                self.assertIn("scan-prompt --prompt-file", text)
                self.assertIn("run_provider", text)
                self.assertIn("--handoff-source-lane", text)
                self.assertIn("--handoff-expected-head", text)
                self.assertIn("Implicit cross-lane takeover", text)
                self.assertIn("no validated delivery", text)

    def test_runner_scripts_prove_the_handoff_source_owns_the_branch(self) -> None:
        # The source lane's prefixes come from the runner's own identity config
        # keyed by the named source lane, never from the caller, and they are
        # resolved before the handoff is validated.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                derive = text.index("handoff_source_prefix_args=()")
                lookup = text.index('--arg lane "$handoff_source_lane_key"')
                guard = text.index("has no configured branch prefixes")
                call = text.index('"${lane_delivery[@]}" handoff')
                self.assertLess(lookup, derive)
                self.assertLess(derive, guard)
                self.assertLess(guard, call)
                self.assertIn(
                    '"${handoff_source_prefix_args[@]}" \\', text[call : call + 600]
                )
                self.assertIn("$branch_prefixes_json", text[lookup - 300 : lookup])

    def test_runner_scripts_require_a_summary_for_a_bounded_outcome(self) -> None:
        # A bounded outcome without an actionable one-line summary must not
        # produce a runner-owned comment, because classification reads that
        # comment as the delivery.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                # The old form accepted a missing or non-string summary.
                self.assertNotIn('(.summary // "") | tostring', text)
                self.assertIn('if (.summary | type) == "string"', text)
                void = text.index("declared_outcome_voided=\"$declared_outcome\"")
                post = text.index('gh "$outcome_subcommand" comment')
                self.assertLess(void, post)
                self.assertIn(
                    ".summary must be a non-empty one-line string", text
                )
                self.assertIn("voided declared outcome", text)

    def test_runner_scripts_do_not_exempt_the_wall_clock_cap(self) -> None:
        # A timed-out provider that delivered nothing is an unfinished unit.
        # The cap branch must not return success before the classification is
        # consulted, or the caller cannot see the unit is unfinished.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                cap_branch_start = text.index('if [ "$timed_out" -eq 1 ]; then\n  cap_note=')
                cap_branch = text[
                    cap_branch_start : text.index("\nfi\n", cap_branch_start)
                ]
                self.assertNotIn("exit", cap_branch)
                self.assertLess(
                    text.index('if [ "$delivery_rc" -ne 0 ]; then'),
                    text.rindex('if [ "$timed_out" -eq 1 ]; then'),
                )

    def test_runner_scripts_fail_closed_on_snapshot_lookups(self) -> None:
        # A failed GitHub read must be recorded as an incomplete snapshot, not
        # as an empty value that the classifier reads as real absence.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("snapshot_complete: $complete", text)
                self.assertIn("complete=false", text)
                self.assertIn("the pre-run target snapshot is incomplete", text)
                self.assertNotIn("2>/dev/null || printf '{}'", text)
                self.assertNotIn("2>/dev/null || printf '[]'", text)
                self.assertNotIn('lane_pr_for_issue "$num" 2>/dev/null || true', text)

    def test_runner_scripts_parse_as_bash(self) -> None:
        for path in (REPO_RUNNER,):
            with self.subTest(path=path.name):
                completed = subprocess.run(
                    ["bash", "-n", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
