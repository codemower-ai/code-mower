"""Deterministic, no-network/no-Git transport calibration and acceptance controls."""

from dataclasses import replace
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from code_mower.devin_review import HostedReview, ReviewInput, local_review, normalize
from code_mower.devin_sessions import DevinClient
from code_mower.remote_session import RemoteError, _key


def finding(severity, title, detail):
    return dict(severity=severity, title=title, detail=detail, file="handler.py", line=12)


# Adjudicated synthetic contract controls, not evidence of live model accuracy.
CONTROLS = [
    (
        "clean-empty",
        "PASS",
        dict(verdict="pass", summary="No defects in the bounded change.", findings=[]),
    ),
    (
        "clean-advisory",
        "PASS",
        dict(
            verdict="pass",
            summary="Only an optional readability improvement.",
            findings=[
                finding("P3", "Name the retry delay", "A named constant would clarify the delay.")
            ],
        ),
    ),
    (
        "blocked-auth",
        "BLOCKED",
        dict(
            verdict="blocked",
            summary="An authorization check is missing.",
            findings=[
                finding(
                    "P1",
                    "Check ownership before returning a record",
                    "The handler returns another account record without checking ownership.",
                )
            ],
        ),
    ),
    (
        "blocked-null",
        "BLOCKED",
        dict(
            verdict="pass",
            summary="The empty input path crashes.",
            findings=[
                finding(
                    "P2",
                    "Guard the missing record",
                    "An empty lookup returns None and the next line dereferences it.",
                )
            ],
        ),
    ),
]


def make_review():
    head = "a" * 40
    return ReviewInput(
        "owner/repo",
        9,
        head,
        "independent-human",
        dict(
            revision="b" * 32,
            head=head,
            required=True,
            state="available",
            expires_at="2099-01-01T00:00:00Z",
        ),
        ("handler.py",),
    )


def hosted(tmp_path, review, current, output):
    state = {"status": "running"}
    calls = []

    def runner(method, url, body, headers):
        calls.append((method, body))
        return dict(session_id="review-1", **state, structured_output=output)

    service = HostedReview.devin(
        tmp_path / "state",
        DevinClient("org-example", "test-key", api_runner=runner),
        review,
        current,
    )
    return (service, state, calls)


class DevinReviewTests(unittest.TestCase):
    def test_calibration_transport_parity(self):
        for name, expected, output in CONTROLS:
            with self.subTest(name=name, expected=expected, output=output):
                with TemporaryDirectory() as directory:
                    tmp_path = Path(directory).resolve()
                    review = make_review()
                    local = local_review(
                        review,
                        lambda review=review: review,
                        lambda output=output: (json.dumps(output), 0),
                    )
                    remote, state, calls = hosted(
                        tmp_path, review, lambda review=review: review, output
                    )
                    self.assertEqual(
                        remote.dispatch("Approved bounded review input")["mode"], "dry_run"
                    )
                    self.assertFalse(calls)
                    self.assertFalse((tmp_path / "state").exists())
                    remote.dispatch("Approved bounded review input", apply=True)
                    self.assertIs(calls[0][1]["structured_output_required"], True)
                    self.assertEqual(calls[0][1]["max_acu_limit"], 1)
                    state.update(status="exit", status_detail="finished")
                    metadata = remote.collect(apply=True)
                    evidence = remote.accept()
                    self.assertEqual(
                        evidence.accept(lambda review=review: review),
                        local.accept(lambda review=review: review),
                    )
                    self.assertEqual(evidence.result.verdict, expected)
                    self.assertFalse(evidence.merge_authority)
                    self.assertFalse(local.merge_authority)
                    self.assertNotIn("summary", json.dumps(metadata))
                    self.assertNotIn("handler.py", json.dumps(metadata))
                    self.assertEqual(len([c for c in calls if c[0] == "POST"]), 1)

    def test_stale_inputs_before_after_and_at_consumption(self):
        for change in ["head", "context", "author", "missing-author", "expired", "unavailable"]:
            with self.subTest(change=change):
                with TemporaryDirectory() as directory:
                    tmp_path = Path(directory).resolve()
                    review = make_review()
                    if change == "head":
                        changed = replace(review, head="c" * 40)
                    elif change == "author":
                        changed = replace(review, author="devin-ai-integration[bot]")
                    elif change == "missing-author":
                        changed = replace(review, author="")
                    else:
                        update = (
                            {"revision": "c" * 32}
                            if change == "context"
                            else {"expires_at": "2000-01-01T00:00:00Z"}
                            if change == "expired"
                            else {"state": "required_unavailable"}
                        )
                        changed = replace(review, context={**review.context, **update})
                    current = [review]
                    output = CONTROLS[0][2]
                    local = local_review(
                        review,
                        lambda current=current: current[0],
                        lambda output=output: (json.dumps(output), 0),
                    )
                    remote, state, calls = hosted(
                        tmp_path, review, lambda current=current: current[0], output
                    )
                    remote.dispatch("approved", apply=True)
                    state.update(status="exit", status_detail="finished")
                    remote.collect(apply=True)
                    saved = remote.accept()
                    current[0] = changed
                    for action in (
                        lambda local=local, current=current: local.accept(
                            lambda current=current: current[0]
                        ),
                        remote.accept,
                        lambda saved=saved, current=current: saved.accept(
                            lambda current=current: current[0]
                        ),
                        lambda remote=remote: remote.collect(apply=True),
                        lambda remote=remote: remote.dispatch("approved", apply=True),
                    ):
                        with self.assertRaisesRegex(RemoteError, "review_binding_mismatch"):
                            action()
                    count = len(calls)
                    run_before = Mock(side_effect=AssertionError("must not execute"))
                    with self.assertRaises(RemoteError):
                        local_review(review, lambda current=current: current[0], run_before)
                    run_before.assert_not_called()
                    self.assertEqual(len(calls), count)
                    current[0] = review

                    def run(current=current, changed=changed, output=output):
                        current[0] = changed
                        return (json.dumps(output), 0)

                    with self.assertRaises(RemoteError):
                        local_review(review, lambda current=current: current[0], run)

    def test_malformed_or_ambiguous_output(self):
        for output in [
            None,
            {},
            " ",
            '{"verdict":"pass","verdict":"blocked"}',
            "```json\n{}\n```\n```json\n{}\n```",
            dict(verdict="blocked", summary="Missing findings.", findings=[]),
            dict(verdict="pass", summary=123, findings=[]),
            dict(
                verdict="pass",
                summary="Invalid line type.",
                findings=[{**CONTROLS[2][2]["findings"][0], "line": True}],
            ),
        ]:
            with self.subTest(output=output):
                with self.assertRaisesRegex(RemoteError, "invalid_review_output"):
                    normalize(output, {"handler.py"})

    def test_completed_evidence_is_invalidated_by_remote_state(self):
        for status in ["running", "error", "suspended", "exit"]:
            with self.subTest(status=status):
                with TemporaryDirectory() as directory:
                    tmp_path = Path(directory).resolve()
                    review = make_review()
                    remote, state, _ = hosted(
                        tmp_path, review, lambda review=review: review, CONTROLS[0][2]
                    )
                    remote.dispatch("approved", apply=True)
                    state.update(status="exit", status_detail="finished")
                    remote.collect(apply=True)
                    evidence = remote.accept()
                    state.clear()
                    state.update(status=status)
                    with self.assertRaises(RemoteError):
                        evidence.accept(lambda review=review: review)
                    self.assertIs(remote.remote.private_result(remote.session), None)

    def test_pending_delivery_never_exposes_collected_result(self):
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory).resolve()
            review = make_review()
            remote, state, _ = hosted(
                tmp_path, review, lambda review=review: review, CONTROLS[0][2]
            )
            remote.dispatch("approved", apply=True)
            state.update(status="exit", status_detail="finished")
            remote.collect(apply=True)
            with remote.remote.store.locked(_key(remote.session)) as locked:
                record = locked.read()
                record["operations"]["cancel:request"] = {
                    "state": "pending",
                    "fingerprint": "private",
                }
                locked.write(record)
            self.assertIs(remote.remote.private_result(remote.session), None)
            with self.assertRaises(RemoteError):
                remote.accept()

    def test_context_changes_during_remote_completion(self):
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory).resolve()
            review = make_review()
            current = [review]
            remote, state, _ = hosted(
                tmp_path, review, lambda current=current: current[0], CONTROLS[0][2]
            )
            remote.dispatch("approved", apply=True)
            state.update(status="exit", status_detail="finished")
            original = remote.remote.provider.get

            def get(binding):
                result = original(binding)
                current[0] = replace(review, context={**review.context, "revision": "c" * 32})
                return result

            with patch.object(remote.remote.provider, "get", side_effect=get):
                with self.assertRaisesRegex(RemoteError, "review_binding_mismatch"):
                    remote.collect(apply=True)
            with self.assertRaises(RemoteError):
                remote.accept()

    def test_unknown_delivery_does_not_collect(self):
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory).resolve()
            review = make_review()
            remote, state, _ = hosted(
                tmp_path, review, lambda review=review: review, CONTROLS[0][2]
            )
            remote.dispatch("approved", apply=True)
            state.update(status="exit", status_detail="finished")
            with remote.remote.store.locked(_key(remote.session)) as locked:
                record = locked.read()
                record["operations"]["message:request"] = {
                    "state": "pending",
                    "fingerprint": "private",
                }
                locked.write(record)
            self.assertEqual(remote.collect(apply=True)["state"], "uncertain")
            self.assertIs(remote.remote.private_result(remote.session), None)

    def test_hosted_missing_or_malformed_completion(self):
        for output in [None, {}, {"verdict": "pass"}]:
            with self.subTest(output=output):
                with TemporaryDirectory() as directory:
                    tmp_path = Path(directory).resolve()
                    review = make_review()
                    remote, state, _ = hosted(
                        tmp_path, review, lambda review=review: review, output
                    )
                    remote.dispatch("approved", apply=True)
                    state.update(status="exit", status_detail="finished")
                    remote.collect(apply=True)
                    with self.assertRaises(RemoteError):
                        remote.accept()

    def test_failed_refresh_revokes_cached_completion(self):
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory).resolve()
            review = make_review()
            remote, state, _ = hosted(
                tmp_path, review, lambda review=review: review, CONTROLS[0][2]
            )
            remote.dispatch("approved", apply=True)
            state.update(status="exit", status_detail="finished")
            remote.collect(apply=True)
            evidence = remote.accept()

            def unavailable(binding):
                raise RuntimeError("PRIVATE_PROVIDER_CANARY")

            with patch.object(remote.remote.provider, "get", side_effect=unavailable):
                with self.assertRaises(RemoteError) as error:
                    evidence.accept(lambda review=review: review)
            self.assertNotIn("PRIVATE_PROVIDER_CANARY", str(error.exception))
            self.assertIs(remote.remote.private_result(remote.session), None)

    def test_noncomplete_then_complete_requires_new_collection(self):
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory).resolve()
            review = make_review()
            remote, state, _ = hosted(
                tmp_path, review, lambda review=review: review, CONTROLS[0][2]
            )
            remote.dispatch("approved", apply=True)
            state.update(status="exit", status_detail="finished")
            remote.collect(apply=True)
            state.clear()
            state.update(status="running")
            with self.assertRaises(RemoteError):
                remote.accept()
            state.update(status="exit", status_detail="finished")
            with self.assertRaises(RemoteError):
                remote.accept()
            remote.collect(apply=True)
            self.assertEqual(remote.accept().accept(lambda review=review: review).verdict, "PASS")

    def test_resumed_session_cannot_repurpose_review_binding(self):
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory).resolve()
            review = make_review()
            remote, state, _ = hosted(
                tmp_path, review, lambda review=review: review, CONTROLS[0][2]
            )
            remote.dispatch("approved", apply=True)
            state.update(status="exit", status_detail="finished")
            remote.collect(apply=True)
            remote.remote.run(
                "message", remote.session, request="followup", prose="Changed task", apply=True
            )
            remote.collect(apply=True)
            with self.assertRaisesRegex(RemoteError, "review_not_ready"):
                remote.accept()


if __name__ == "__main__":
    unittest.main()
