"""Local builder delivery, process cleanup, and recovery handoffs (#751)."""

from __future__ import annotations

import contextlib
import errno
import gc
import io
import json
import os
import re
import resource
import shutil
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


def _summary_contract(path: Path) -> tuple[str, int]:
    """The runner's bounded-outcome summary program and its length bound.

    Read out of the script itself so the test exercises the rule the runner
    actually applies, not a copy of it that can drift.
    """
    text = path.read_text(encoding="utf-8")
    opened = text.index("lane_summary_filter='") + len("lane_summary_filter='")
    program = text[opened : text.index("'", opened)]
    bound = re.search(r"^lane_summary_max_chars=(\d+)$", text, re.MULTILINE)
    assert bound is not None, f"{path} defines no lane_summary_max_chars"
    return program, int(bound.group(1))


def _pre_push_hook(path: Path) -> str:
    """The pre-push guard the runner installs, read out of the runner itself."""

    text = path.read_text(encoding="utf-8")
    opened = text.index("<<'HOOK'\n") + len("<<'HOOK'\n")
    return text[opened : text.index("\nHOOK\n", opened) + 1]


def _broker_block(path: Path) -> str:
    """The runner's bounded-outcome brokering block, read out of the runner.

    Extracted rather than transcribed so the ordering under test is the one the
    runner performs: comment first, label only behind a confirmed comment.
    """

    text = path.read_text(encoding="utf-8")
    start = text.index('if [ -n "$declared_outcome" ]; then\n  outcome_subcommand=')
    return text[start : text.index("\ndelivery_rc=0", start)]


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


def _self_cpu_seconds() -> float:
    """CPU seconds this process has burned, children excluded.

    ``supervise_process`` runs in the test process, so a supervision loop that
    spins shows up here as CPU that tracks wall time. The provider's own CPU is
    charged to ``RUSAGE_CHILDREN`` and is deliberately not counted.
    """

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


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

    def test_snapshots_of_different_targets_never_read_as_a_new_pr(self) -> None:
        # One target's open PR against another target's absent one is the exact
        # shape of pr_opened, so a caller that mixed up its snapshot files would
        # have recorded a delivery neither unit made.
        before = _state()
        after = _state(number="999", pr_number="901", head_sha=SHA_A)

        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.observed_transition(before, after)
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.classify_delivery(before, after, provider_exit=0)

    def test_snapshots_of_different_kinds_never_read_as_an_advanced_head(self) -> None:
        # Same number, different kind: issue #900 and PR #900 are two units, and
        # their unrelated heads subtract to head_advanced.
        before = _state(kind="issue", number="900", pr_number="900", head_sha=SHA_A)
        after = _state(kind="pr", number="900", pr_number="900", head_sha=SHA_B)

        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.classify_delivery(before, after, provider_exit=0)

    def test_mismatched_targets_outrank_an_incomplete_snapshot(self) -> None:
        # Fail-closed reports a unit as undelivered, which needs a unit. There
        # is none here, so the caller error is raised rather than attributed to
        # whichever target the after snapshot happens to name.
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.classify_delivery(
                _state(snapshot_complete=False),
                _state(number="999", pr_number="901", head_sha=SHA_A),
                provider_exit=0,
            )

    def test_classify_cli_refuses_mismatched_targets_without_a_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.json"
            after = root / "after.json"
            record = root / "delivery.json"
            before.write_text(json.dumps(_state().as_dict()), encoding="utf-8")
            after.write_text(
                json.dumps(
                    _state(number="999", pr_number="901", head_sha=SHA_A).as_dict()
                ),
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
                    "--lane",
                    "claude",
                    "--repo",
                    "codemower-ai/code-mower",
                    "--output",
                    str(record),
                ]
            )

            self.assertEqual(rc, 2)
            self.assertFalse(record.exists())

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

    def test_capped_run_keeps_the_delivery_it_already_made(self) -> None:
        """A cap fires on the clock, not on whether the work landed.

        The supervisor's own exit code says the run was stopped; it says
        nothing about the PR the provider pushed before that. Rejecting the
        observed transition here reported finished work as undelivered and made
        the runner's timed-out-success path unreachable.
        """

        for reason in sorted(lane_delivery.SUPERVISION_CAP_REASONS):
            with self.subTest(supervision=reason):
                opened = lane_delivery.classify_delivery(
                    _state(),
                    _state(pr_number="901", head_sha=SHA_A),
                    provider_exit=lane_delivery.EXIT_TIMEOUT,
                    supervision_reason=reason,
                )
                advanced = lane_delivery.classify_delivery(
                    _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A),
                    _state(kind="pr", number="900", pr_number="900", head_sha=SHA_B),
                    provider_exit=lane_delivery.EXIT_TIMEOUT,
                    supervision_reason=reason,
                )

                self.assertTrue(opened.delivered)
                self.assertEqual(
                    opened.transition, lane_delivery.TRANSITION_PR_OPENED
                )
                self.assertEqual(opened.reason, "observed_state_transition")
                self.assertTrue(advanced.delivered)
                self.assertEqual(
                    advanced.transition, lane_delivery.TRANSITION_HEAD_ADVANCED
                )

    def test_capped_run_without_a_transition_is_undelivered(self) -> None:
        outcome = lane_delivery.classify_delivery(
            _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A),
            _state(kind="pr", number="900", pr_number="900", head_sha=SHA_A),
            provider_exit=lane_delivery.EXIT_TIMEOUT,
            supervision_reason="timeout",
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.transition, lane_delivery.TRANSITION_NONE)
        self.assertEqual(outcome.reason, "supervision_ended_without_delivery")

    def test_capped_run_may_not_declare_a_bounded_outcome(self) -> None:
        """A killed provider can leave a half-written declaration behind."""

        outcome = lane_delivery.classify_delivery(
            _state(),
            _state(runner_comment_id="12345", labels=("needs-owner",)),
            provider_exit=lane_delivery.EXIT_TIMEOUT,
            supervision_reason="timeout",
            declared_outcome="owner_action",
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "supervision_ended_without_delivery")

    def test_a_provider_that_failed_on_its_own_still_never_delivers(self) -> None:
        outcome = lane_delivery.classify_delivery(
            _state(),
            _state(pr_number="901", head_sha=SHA_A),
            provider_exit=1,
            supervision_reason=lane_delivery.SUPERVISION_COMPLETED,
        )
        held = lane_delivery.classify_delivery(
            _state(),
            _state(pr_number="901", head_sha=SHA_A),
            provider_exit=1,
            supervision_reason=lane_delivery.SUPERVISION_DESCENDANTS_HELD_OUTPUT,
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "provider_exit_nonzero")
        # A descendant holding the pipe open did not end the run; the provider
        # chose its own exit code, so that code is still its own verdict.
        self.assertFalse(held.delivered)
        self.assertEqual(held.reason, "provider_exit_nonzero")

    def test_an_incomplete_snapshot_outranks_a_cap(self) -> None:
        outcome = lane_delivery.classify_delivery(
            _state(snapshot_complete=False),
            _state(pr_number="901", head_sha=SHA_A),
            provider_exit=lane_delivery.EXIT_TIMEOUT,
            supervision_reason="timeout",
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.reason, "target_snapshot_unavailable")

    def test_unknown_supervision_reason_is_rejected(self) -> None:
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery.classify_delivery(
                _state(),
                _state(pr_number="901", head_sha=SHA_A),
                provider_exit=lane_delivery.EXIT_TIMEOUT,
                supervision_reason="probably_fine",
            )

    def test_classify_cli_passes_the_supervision_reason_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.json"
            after = root / "after.json"
            before.write_text(json.dumps(_state().as_dict()), encoding="utf-8")
            after.write_text(
                json.dumps(_state(pr_number="901", head_sha=SHA_A).as_dict()),
                encoding="utf-8",
            )
            argv = [
                "classify",
                "--before",
                str(before),
                "--after",
                str(after),
                "--provider-exit",
                str(lane_delivery.EXIT_TIMEOUT),
            ]

            capped = lane_delivery.main([*argv, "--supervision", "timeout"])
            uncapped = lane_delivery.main(argv)

        self.assertEqual(capped, 0)
        # Same exit code, but nothing says the supervisor produced it, so it is
        # the provider's own failure and the PR does not rescue it.
        self.assertEqual(uncapped, 3)

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


class TransitionReadbackTests(unittest.TestCase):
    """What the runner asks before it writes anything to the target.

    The bounded declared outcome has to be brokered -- an owner-facing comment,
    and `needs-owner` for owner_action -- before classification runs, so the
    runner needs the transition on its own, ahead of that decision. It is the
    same rule classification uses, asked earlier.
    """

    def _transition(
        self, before: lane_delivery.TargetState, after: lane_delivery.TargetState
    ) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_path = root / "before.json"
            after_path = root / "after.json"
            before_path.write_text(json.dumps(before.as_dict()), encoding="utf-8")
            after_path.write_text(json.dumps(after.as_dict()), encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(
                io.StringIO()
            ):
                rc = lane_delivery.main(
                    [
                        "transition",
                        "--before",
                        str(before_path),
                        "--after",
                        str(after_path),
                    ]
                )
        return rc, printed.getvalue().strip()

    def test_a_new_pull_request_is_reported_as_a_delivery(self) -> None:
        rc, printed = self._transition(
            _state(), _state(pr_number="901", head_sha=SHA_A)
        )

        self.assertEqual(rc, 0)
        self.assertEqual(printed, lane_delivery.TRANSITION_PR_OPENED)

    def test_an_advanced_head_is_reported_as_a_delivery(self) -> None:
        rc, printed = self._transition(
            _state(pr_number="901", head_sha=SHA_A),
            _state(pr_number="901", head_sha=SHA_B),
        )

        self.assertEqual(rc, 0)
        self.assertEqual(printed, lane_delivery.TRANSITION_HEAD_ADVANCED)

    def test_a_delivered_transition_still_exits_zero(self) -> None:
        # The exit status reports whether the comparison could be made, never
        # what it found. The runner reads the printed value and treats a failure
        # as unknown, so returning nonzero for a real transition would hide the
        # delivery it exists to report.
        for before, after in (
            (_state(), _state(pr_number="901", head_sha=SHA_A)),
            (
                _state(pr_number="901", head_sha=SHA_A),
                _state(pr_number="901", head_sha=SHA_B),
            ),
            (_state(), _state()),
        ):
            with self.subTest(after=after.head_sha or "no head"):
                rc, _ = self._transition(before, after)
                self.assertEqual(rc, 0)

    def test_no_transition_is_reported_as_none(self) -> None:
        rc, printed = self._transition(
            _state(pr_number="901", head_sha=SHA_A),
            _state(pr_number="901", head_sha=SHA_A),
        )

        self.assertEqual(rc, 0)
        self.assertEqual(printed, lane_delivery.TRANSITION_NONE)

    def test_an_incomplete_snapshot_is_reported_as_unknown(self) -> None:
        # Not `none`: a failed GitHub read cannot be told apart from a target
        # that did not move, and the runner brokers only on an observed `none`.
        rc, printed = self._transition(
            _state(pr_number="901", head_sha=SHA_A),
            _state(snapshot_complete=False),
        )

        self.assertEqual(rc, 0)
        self.assertEqual(printed, lane_delivery.TRANSITION_UNKNOWN)

    def test_mismatched_targets_fail_instead_of_naming_a_transition(self) -> None:
        rc, printed = self._transition(
            _state(), _state(number="999", pr_number="901", head_sha=SHA_A)
        )

        self.assertEqual(rc, 2)
        self.assertEqual(printed, "")


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

    def test_a_descendant_holding_stdout_does_not_stall_the_run(self) -> None:
        """The provider's own exit ends the run, open pipe or not.

        A background descendant inherits the provider's stdout, so the pipe
        never reaches EOF. Waiting for it burned the whole lane timeout and
        then reported a timeout — rejecting the delivery — for a provider that
        had already finished. The lingering transport is what this supervisor
        cleans up, not something to wait behind.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "run.log"
            child_pid_file = root / "child.pid"
            script = (
                "sleep 120 & echo $! > "
                + str(child_pid_file)
                + "; printf 'done\\n'; exit 5"
            )

            started = time.monotonic()
            result = lane_delivery.supervise_process(
                ["bash", "-c", script],
                log_path=log_path,
                timeout_seconds=30.0,
                term_grace_seconds=1.0,
                descendant_drain_seconds=0.2,
            )
            elapsed = time.monotonic() - started

            log = log_path.read_text(encoding="utf-8")
            grandchild = int(child_pid_file.read_text(encoding="utf-8").strip())

        self.assertLess(elapsed, 15.0, "supervisor waited out the lane timeout")
        self.assertFalse(result.timed_out)
        self.assertTrue(result.descendants_held_output)
        self.assertEqual(
            result.reason, lane_delivery.SUPERVISION_DESCENDANTS_HELD_OUTPUT
        )
        # The provider chose 5, and no cap overwrote it.
        self.assertEqual(result.exit_code, 5)
        self.assertIn("done", log)
        self.assertIn("SIGTERM", result.signals_sent)
        self.assertTrue(
            _wait_for_pid_exit(grandchild),
            "orphaned provider transport survived the provider's exit",
        )

    def test_a_finished_provider_is_not_a_timeout_at_the_cap(self) -> None:
        """The drain must not turn a finished provider into a timed-out one.

        The cap can fall inside the drain window. The provider had already
        exited, so the run is not a timeout, and calling it one would replace
        its exit code and hand classification a cap that never happened.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_pid_file = root / "child.pid"
            script = (
                "sleep 120 & echo $! > " + str(child_pid_file) + "; exit 5"
            )

            result = lane_delivery.supervise_process(
                ["bash", "-c", script],
                log_path=root / "run.log",
                timeout_seconds=1.0,
                term_grace_seconds=1.0,
                # Outlives the cap, so the cap lands mid-drain.
                descendant_drain_seconds=30.0,
            )

            grandchild = int(child_pid_file.read_text(encoding="utf-8").strip())

        self.assertFalse(result.timed_out)
        self.assertTrue(result.descendants_held_output)
        self.assertEqual(result.exit_code, 5)
        self.assertTrue(
            _wait_for_pid_exit(grandchild),
            "orphaned provider transport survived the drain",
        )

    def test_a_provider_that_closes_output_early_is_waited_on_not_spun_on(
        self,
    ) -> None:
        """Closed output must leave the supervisor waiting, not spinning.

        A provider that closes stdout and stderr while it keeps working leaves
        the loop with nothing to select on. Waiting on an empty selector makes
        the wait the selector backend's decision, and a backend that returns at
        once turns the rest of the provider's run into a full-speed loop on the
        runner's own CPU. The supervisor waits on the child instead, so the run
        still ends on the provider's exit and with its exit code.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "run.log"
            # Both descriptors: stderr is a dup of the stdout pipe, so closing
            # stdout alone would not reach EOF.
            script = "printf 'working\\n'; exec 1>&- 2>&-; sleep 2; exit 6"

            started = time.monotonic()
            cpu_before = _self_cpu_seconds()
            result = lane_delivery.supervise_process(
                ["bash", "-c", script],
                log_path=log_path,
                timeout_seconds=30.0,
                term_grace_seconds=1.0,
            )
            cpu_used = _self_cpu_seconds() - cpu_before
            elapsed = time.monotonic() - started

            log = log_path.read_text(encoding="utf-8")

        # The provider ran to its own end, and its exit ended the run.
        self.assertEqual(result.exit_code, 6)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.overflowed)
        self.assertFalse(result.descendants_held_output)
        self.assertEqual(result.reason, lane_delivery.SUPERVISION_COMPLETED)
        self.assertIn("working", log)
        self.assertGreater(elapsed, 1.5, "the supervisor did not wait for the provider")
        self.assertLess(
            cpu_used,
            0.5,
            f"supervision burned {cpu_used:.2f}s of CPU over {elapsed:.2f}s of "
            "waiting: the loop is spinning on a closed output stream",
        )

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
        # 3 means "the run delivered nothing". A provider that failed on its
        # own keeps its own code so the caller can still diagnose it.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                keep = text.index(
                    '[ "$supervisor_ended" -eq 0 ] && [ "$rc" -ne 0 ] && exit "$rc"'
                )
                self.assertLess(keep, text.index("exit 3", keep - 200))
                # The ungated form returned the supervisor's own code for a run
                # the provider never ended.
                self.assertNotIn('\n  [ "$rc" -ne 0 ] && exit "$rc"', text)
                # Timeout and overflow come from the supervision reason, not
                # from raw 124/125, which a provider may also return.
                self.assertIn('[ "$supervision_reason" = "timeout" ] && timed_out=1', text)
                self.assertIn(
                    '[ "$supervision_reason" = "output_overflow" ] && overflowed=1', text
                )

    def test_runner_scripts_do_not_return_the_supervisors_exit_code(self) -> None:
        # 124, 125, and 130 belong to the supervisor, not to the provider. A
        # capped, overflowed, or interrupted run that delivered nothing exits 3
        # -- the undelivered code -- rather than reporting the supervisor's
        # code as the provider's own failure and hiding the classification the
        # caller acts on.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                derive = text.index("supervisor_ended=0")
                self.assertIn(
                    "  timeout|output_overflow|interrupted) supervisor_ended=1 ;;",
                    text,
                )
                # Derived from the recorded supervision reason, and only after
                # the fallback cap has named its own, so both paths agree on
                # who ended the run.
                self.assertLess(text.index('supervision_reason="timeout"'), derive)
                self.assertLess(
                    derive,
                    text.index(
                        '[ "$supervisor_ended" -eq 0 ] && [ "$rc" -ne 0 ] && exit "$rc"'
                    ),
                )
                # A provider the supervisor stopped does not get to declare a
                # bounded outcome either: classification refuses one, so the
                # runner must not post the owner-facing comment for it.
                self.assertIn(
                    'if [ "$rc" -eq 0 ] && [ "$supervisor_ended" -eq 0 ] \\', text
                )
                # The mirror case: overflow and interruption that still
                # delivered are finished units, so they do not return 125 or
                # 130 either -- but only a run that was actually classified may
                # be forgiven the supervisor's code. An audit round and a runner
                # without the CLI never classify, and keep their exit code.
                self.assertIn('delivery_reason="not_classified"', text)
                self.assertIn(
                    'if [ "$supervisor_ended" -eq 1 ] '
                    '&& [ "$delivery_reason" != "not_classified" ]; then',
                    text,
                )

    def test_runner_scripts_tell_the_classifier_who_ended_the_run(self) -> None:
        # Classification treats the exit code as the provider's own verdict
        # only when nothing else stopped the run, so the runner has to name the
        # supervision reason on both paths -- including the fallback cap, which
        # writes no status file and would otherwise report "completed" for a
        # run the cap killed.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(
                    '[ "$rc" -eq 124 ] && { timed_out=1; supervision_reason="timeout"; }',
                    text,
                )
                self.assertIn(
                    '[ "$rc" -eq 125 ] && { overflowed=1; supervision_reason="output_overflow"; }',
                    text,
                )
                self.assertLess(
                    text.index('supervision_reason="timeout"'),
                    text.index('--supervision "$supervision_reason"'),
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

    def test_runner_scripts_read_the_after_state_before_they_broker(self) -> None:
        # A provider that both wrote lane-outcome.json and pushed has
        # delivered. Brokering the declaration first posts a comment saying
        # nothing changed -- and, for owner_action, applies needs-owner -- on a
        # pull request that classification then reports as a delivery. The
        # after snapshot has to be taken, and the transition read from it,
        # before any runner-owned GitHub write.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                capture = text.index('capture_target_state "$after_state"\n')
                gate = text.index('"${lane_delivery[@]}" transition')
                broker = text.index('gh "$outcome_subcommand" comment')
                label = text.index('--add-label "$owner_label"')
                recapture = text.index(
                    'capture_target_state "$after_state" "$runner_comment_id"'
                )
                classify = text.index('"${lane_delivery[@]}" "${classify_args[@]}"')
                self.assertLess(capture, gate)
                self.assertLess(gate, broker)
                self.assertLess(broker, label)
                # The re-read is what proves the runner's own brokering landed:
                # a needs-owner edit that failed must classify as
                # owner_action_missing_label rather than as a delivery.
                self.assertLess(label, recapture)
                self.assertLess(recapture, classify)

    def test_runner_scripts_broker_only_when_nothing_was_delivered(self) -> None:
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                withheld = text.index('declared_outcome_withheld="$delivered_transition"')
                parsed = text.index("declared_outcome=\"$(jq -r '.outcome")
                self.assertIn('if [ "$delivered_transition" != "none" ]; then', text)
                # The declaration is only read at all on the branch where the
                # runner observed no transition.
                self.assertLess(withheld, parsed)
                # A withheld declaration is named in the undelivered note, so
                # the owner is not left wondering where their outcome went.
                self.assertIn("withheld declared outcome", text)

    def test_runner_scripts_do_not_broker_on_an_unresolved_transition(self) -> None:
        # Unknown covers an incomplete after snapshot and a lane-delivery too
        # old to answer. Neither is evidence that the run delivered nothing, and
        # only an observed `none` brokers.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn('delivered_transition="unknown"', text)
                self.assertIn('*) delivered_transition="unknown" ;;', text)
                self.assertIn("|| printf 'unknown'", text)

    def test_runner_scripts_void_a_summary_instead_of_truncating_it(self) -> None:
        # A summary that is not already one line within the bound is discarded
        # whole. Keeping its first line, or its first N characters, would post
        # an owner-facing comment carrying half of the only explanation the
        # owner gets -- and classification would read that comment as delivery.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn('split("\\n") | first', text)
                self.assertNotIn("cut -c1-280", text)
                self.assertIn(
                    'jq -r --argjson max "$lane_summary_max_chars"'
                    ' "$lane_summary_filter"',
                    text,
                )

    def test_the_summary_program_keeps_only_valid_one_line_summaries(self) -> None:
        # The rule itself, run the way the runner runs it. `jq` is a hard
        # dependency of the runner, so its absence is the test being unable to
        # observe the contract rather than the contract holding.
        jq = shutil.which("jq")
        if jq is None:  # pragma: no cover - depends on the host toolchain
            self.skipTest("jq is not installed")
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            program, bound = _summary_contract(path)
            cases: list[tuple[object, str]] = [
                # Accepted, and returned exactly as written once surrounding
                # whitespace -- which carries no content -- is stripped.
                ("closed as designed", "closed as designed"),
                ("trailing newline is still one line\n", "trailing newline is still one line"),
                ("  padded  ", "padded"),
                ("x" * bound, "x" * bound),
                # Voided: more than the one line the contract asks for.
                ("first line\nsecond line", ""),
                ("carriage\rreturn", ""),
                ("tab\tseparated", ""),
                # Voided: longer than the runner will carry, by one character.
                ("x" * (bound + 1), ""),
                # Voided: absent, blank, or not a string at all.
                ("", ""),
                ("   ", ""),
                (42, ""),
                (None, ""),
                ({"nested": "object"}, ""),
            ]
            for summary, expected in cases:
                with self.subTest(path=path.name, summary=repr(summary)[:40]):
                    with tempfile.TemporaryDirectory() as tmp:
                        outcome = Path(tmp) / "lane-outcome.json"
                        outcome.write_text(
                            json.dumps({"outcome": "no_change", "summary": summary}),
                            encoding="utf-8",
                        )
                        completed = subprocess.run(
                            [
                                jq,
                                "-r",
                                "--argjson",
                                "max",
                                str(bound),
                                program,
                                str(outcome),
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    # `$( ... )` strips the trailing newline jq adds, so this is
                    # the value the runner assigns to declared_summary.
                    self.assertEqual(completed.stdout.rstrip("\n"), expected)

    def test_the_summary_program_voids_a_declaration_with_no_summary_key(self) -> None:
        jq = shutil.which("jq")
        if jq is None:  # pragma: no cover - depends on the host toolchain
            self.skipTest("jq is not installed")
        program, bound = _summary_contract(REPO_RUNNER)
        with tempfile.TemporaryDirectory() as tmp:
            outcome = Path(tmp) / "lane-outcome.json"
            outcome.write_text(json.dumps({"outcome": "no_change"}), encoding="utf-8")
            completed = subprocess.run(
                [jq, "-r", "--argjson", "max", str(bound), program, str(outcome)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.rstrip("\n"), "")

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


PINNED_HEAD = "a" * 40
HANDED_BRANCH = "codex/751-stuck"


class PrePushGuardTests(unittest.TestCase):
    """The installed pre-push guard, driven the way git drives it.

    git feeds the hook one ``<local ref> <local sha> <remote ref> <remote sha>``
    line per ref being pushed, so these tests do the same and read the guard's
    exit status. The hook is extracted from the runner rather than transcribed,
    so what runs here is what gets installed.
    """

    def setUp(self) -> None:
        self.jq = shutil.which("jq")
        self.git = shutil.which("git")
        if self.jq is None or self.git is None:  # pragma: no cover - host toolchain
            self.skipTest("the pre-push guard needs git and jq")
        self.hook = _pre_push_hook(REPO_RUNNER)

    def _config(self, **overrides: object) -> dict[str, object]:
        config: dict[str, object] = {
            "lane": "claude",
            "mode": "fix",
            "target_pr_branch": HANDED_BRANCH,
            "allowed_prefixes": ["claude/"],
            "handoff": {
                "source_lane": "codex",
                "destination_lane": "claude",
                "target_pr": "owner/repo#750",
                "expected_head": PINNED_HEAD,
                "target_branch": HANDED_BRANCH,
            },
        }
        config.update(overrides)
        return config

    def _repo(self, config: dict[str, object]) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(
            [str(self.git), "init", "-q", str(tmp)], check=True, capture_output=True
        )
        (tmp / ".git" / "code-mower-lane-guard.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        (tmp / "pre-push").write_text(self.hook, encoding="utf-8")
        return tmp

    def _push(
        self,
        repo: Path,
        *,
        branch: str,
        local: str,
        remote: str,
        ref: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        remote_ref = ref if ref is not None else f"refs/heads/{branch}"
        return subprocess.run(
            ["bash", str(repo / "pre-push"), "origin", "git@github.com:owner/repo.git"],
            input=f"{remote_ref} {local} {remote_ref} {remote}\n",
            cwd=str(repo),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_every_runner_copy_installs_the_same_guard(self) -> None:
        packaged = _pre_push_hook(PACKAGED_RUNNER_TEMPLATE)
        self.assertEqual(_pre_push_hook(RUNNER_TEMPLATE), packaged)
        self.assertEqual(self.hook, packaged)

    def test_a_handoff_push_at_the_pinned_head_is_authorized(self) -> None:
        repo = self._repo(self._config())
        pushed = self._push(repo, branch=HANDED_BRANCH, local=SHA_B, remote=PINNED_HEAD)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

    def test_a_remote_that_moved_after_the_handoff_is_refused(self) -> None:
        # The source lane kept writing. Branch name alone still says "yes", which
        # is what let --force-with-lease overwrite a head the orchestrator never
        # inspected: the lease is taken against a freshly fetched ref.
        repo = self._repo(self._config())
        pushed = self._push(repo, branch=HANDED_BRANCH, local=SHA_B, remote="d" * 40)
        self.assertEqual(pushed.returncode, 1)
        self.assertIn("handed-over branch", pushed.stderr)
        self.assertIn(PINNED_HEAD, pushed.stderr)

    def test_a_recovery_may_push_more_than_once_onto_its_own_head(self) -> None:
        # The second push sees the remote this run just advanced. That is the
        # lane's own write, not the source lane's, so it is not a stale handoff.
        repo = self._repo(self._config())
        first = self._push(repo, branch=HANDED_BRANCH, local=SHA_B, remote=PINNED_HEAD)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._push(repo, branch=HANDED_BRANCH, local="c" * 40, remote=SHA_B)
        self.assertEqual(second.returncode, 0, second.stderr)
        # A head this run never wrote is still refused afterwards.
        foreign = self._push(repo, branch=HANDED_BRANCH, local="c" * 40, remote="e" * 40)
        self.assertEqual(foreign.returncode, 1)
        # The ledger only ever holds heads this guard authorized, on the branch
        # it authorized them for.
        ledger = (repo / ".git" / "code-mower-lane-guard-pushed").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            ledger.split("\n")[:2],
            [f"{HANDED_BRANCH} {SHA_B}", f"{HANDED_BRANCH} {'c' * 40}"],
        )

    def test_an_absent_remote_branch_is_refused(self) -> None:
        # git reports 40 zeros for a ref that does not exist on the remote. The
        # handoff pinned a real head, so this is not it.
        repo = self._repo(self._config())
        pushed = self._push(repo, branch=HANDED_BRANCH, local=SHA_B, remote="0" * 40)
        self.assertEqual(pushed.returncode, 1)
        self.assertIn("handed-over branch", pushed.stderr)

    def test_a_handoff_authorizes_only_the_branch_it_names(self) -> None:
        # While a handoff is in force it replaces branch-name authority for the
        # foreign branch: the target PR branch is not separately writable.
        repo = self._repo(self._config(target_pr_branch="codex/other"))
        pushed = self._push(repo, branch="codex/other", local=SHA_B, remote=PINNED_HEAD)
        self.assertEqual(pushed.returncode, 1)
        self.assertIn("refusing claude push to branch codex/other", pushed.stderr)

    def test_a_handoff_with_no_pinned_head_authorizes_nothing(self) -> None:
        config = self._config()
        handoff = dict(config["handoff"])  # type: ignore[arg-type]
        handoff["expected_head"] = ""
        repo = self._repo(self._config(handoff=handoff))
        pushed = self._push(repo, branch=HANDED_BRANCH, local=SHA_B, remote=PINNED_HEAD)
        self.assertEqual(pushed.returncode, 1)
        self.assertIn("records no expected head", pushed.stderr)

    def test_the_lanes_own_branches_are_unaffected_by_the_pin(self) -> None:
        # Normal single-writer enforcement is unchanged. A lane-prefixed branch
        # carries no pinned head and advances as often as the run needs.
        repo = self._repo(self._config())
        pushed = self._push(repo, branch="claude/751-work", local=SHA_B, remote=SHA_A)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

    def test_a_foreign_branch_without_a_handoff_stays_refused(self) -> None:
        repo = self._repo(
            self._config(handoff=None, target_pr_branch="claude/751-work")
        )
        pushed = self._push(repo, branch=HANDED_BRANCH, local=SHA_B, remote=SHA_A)
        self.assertEqual(pushed.returncode, 1)
        self.assertIn("refusing claude push to branch", pushed.stderr)

    def test_the_targeted_pr_branch_stays_writable_without_a_handoff(self) -> None:
        # The branch-name authority for the targeted PR is untouched when no
        # handoff is in force, and carries no pinned head to check.
        repo = self._repo(self._config(handoff=None, target_pr_branch="codex/other"))
        pushed = self._push(repo, branch="codex/other", local=SHA_B, remote=SHA_A)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)

    def test_a_non_branch_ref_is_refused(self) -> None:
        repo = self._repo(self._config())
        pushed = self._push(
            repo, branch="v1", local=SHA_B, remote=SHA_A, ref="refs/tags/v1"
        )
        self.assertEqual(pushed.returncode, 1)
        self.assertIn("non-branch ref", pushed.stderr)


BROKER_HARNESS = r"""
set -euo pipefail
LANE="claude"
REPO="owner/repo"
num="750"
kind="pr"
owner_label="needs-owner"
after_state="${SCRATCH}/after.json"
declared_summary="nothing needed changing"
declared_outcome="${DECLARED_OUTCOME}"
declared_outcome_unbrokered=""
runner_comment_id=""

gh() {
  printf 'gh %s\n' "$*" >> "${SCRATCH}/calls.log"
  if [ "$2" = "comment" ]; then
    if [ "${COMMENT_RESULT}" = "fail" ]; then
      return 1
    fi
    printf '%s\n' "${COMMENT_RESULT}"
    return 0
  fi
  if [ "${EDIT_RESULT}" = "fail" ]; then
    return 1
  fi
  return 0
}

capture_target_state() {
  printf 'capture %s\n' "${2:-none}" >> "${SCRATCH}/calls.log"
}

__BLOCK__

printf 'declared_outcome=%s\n' "$declared_outcome"
printf 'unbrokered=%s\n' "$declared_outcome_unbrokered"
printf 'comment_id=%s\n' "$runner_comment_id"
"""

COMMENT_URL = "https://github.com/owner/repo/pull/750#issuecomment-4242"


class BoundedOutcomeBrokerTests(unittest.TestCase):
    """The order the runner writes owner-facing state in.

    needs-owner is a block and the runner comment is its only explanation, so
    the block may never be applied on the strength of a comment the runner only
    attempted. The block is extracted from the runner so the ordering under test
    is the one that ships.
    """

    def _broker(
        self,
        path: Path,
        *,
        outcome: str,
        comment: str = COMMENT_URL,
        edit: str = "ok",
    ) -> tuple[dict[str, str], list[str]]:
        harness = BROKER_HARNESS.replace("__BLOCK__", _broker_block(path))
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "broker.sh"
            script.write_text(harness, encoding="utf-8")
            completed = subprocess.run(
                ["bash", str(script)],
                env={
                    **os.environ,
                    "SCRATCH": tmp,
                    "DECLARED_OUTCOME": outcome,
                    "COMMENT_RESULT": comment,
                    "EDIT_RESULT": edit,
                },
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            log = Path(tmp) / "calls.log"
            calls = (
                log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            )
        reported = dict(
            line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
        )
        return reported, calls

    def test_the_owner_label_follows_a_confirmed_comment(self) -> None:
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                reported, calls = self._broker(path, outcome="owner_action")
                self.assertEqual(len(calls), 3, calls)
                self.assertIn("pr comment 750", calls[0])
                self.assertIn("--add-label needs-owner", calls[1])
                self.assertEqual(calls[2], "capture 4242")
                self.assertEqual(reported["declared_outcome"], "owner_action")
                self.assertEqual(reported["comment_id"], "4242")
                self.assertEqual(reported["unbrokered"], "")

    def test_a_comment_that_did_not_post_applies_no_label(self) -> None:
        # The reported failure: the comment fails, the label edit would have
        # succeeded, and the target is left blocked with nothing explaining it
        # while classification rejects the declaration for having no comment.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                reported, calls = self._broker(
                    path, outcome="owner_action", comment="fail"
                )
                self.assertEqual(len(calls), 1, calls)
                self.assertIn("pr comment 750", calls[0])
                self.assertNotIn("--add-label", "\n".join(calls))
                self.assertEqual(reported["declared_outcome"], "")
                self.assertEqual(reported["unbrokered"], "owner_action")

    def test_an_unusable_comment_response_counts_as_no_comment(self) -> None:
        # Classification identifies the runner's comment by id. A response the
        # runner cannot take an id from leaves it unable to prove the comment
        # exists, which is the same position as never having posted one.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                reported, calls = self._broker(
                    path,
                    outcome="owner_action",
                    comment="https://github.com/owner/repo/pull/750",
                )
                self.assertNotIn("--add-label", "\n".join(calls))
                self.assertEqual(reported["declared_outcome"], "")
                self.assertEqual(reported["unbrokered"], "owner_action")

    def test_a_failed_label_edit_leaves_the_declaration_to_classification(self) -> None:
        # The comment landed, so the owner has the explanation. Whether the label
        # landed is read back off GitHub, not assumed: the re-read is what lets
        # classification answer, and it still has to happen.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                reported, calls = self._broker(
                    path, outcome="owner_action", edit="fail"
                )
                self.assertEqual(calls[-1], "capture 4242")
                self.assertEqual(reported["declared_outcome"], "owner_action")
                self.assertEqual(reported["unbrokered"], "")

    def test_no_change_never_touches_labels(self) -> None:
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                reported, calls = self._broker(path, outcome="no_change")
                self.assertEqual(len(calls), 2, calls)
                self.assertNotIn("--add-label", "\n".join(calls))
                self.assertEqual(calls[1], "capture 4242")
                self.assertEqual(reported["declared_outcome"], "no_change")

    def test_the_undelivered_note_names_an_unbrokered_declaration(self) -> None:
        # The owner reads the note, so a declaration dropped because its comment
        # never posted has to say so there, not only in the run log.
        for path in (RUNNER_TEMPLATE, REPO_RUNNER):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("unbrokered declared outcome", text)
                self.assertIn('"$declared_outcome_unbrokered" "$owner_label"', text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
