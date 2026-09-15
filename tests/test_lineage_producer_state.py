"""Private record, supervised exit, role admission and chained delivery rows."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from code_mower import lane_delivery, lane_handoff
from code_mower.builder_lineage import ContractError, History
from code_mower.builder_lineage_producer import ProducerRefusal, ProducerStore, _delivery
from lineage_producer_fixtures import (
    AUTHORITY, BRANCH, MemoryStore, POLICY, TRANSPORT, episode, round_fixture, sha, target,
)


class StateTests(unittest.TestCase):
    def setUp(self):
        MemoryStore.records, MemoryStore.effects = {}, []
        self.addCleanup(patch.stopall)
        patch("code_mower.builder_lineage_producer.ContextStore", MemoryStore).start()
        patch("code_mower.lane_handoff.ContextStore", MemoryStore).start()
        self.checkout = patch("code_mower.lane_delivery._lineage_checkout").start()
        self.store = ProducerStore(Path("/producer-state"))

    def record(self, receipt, *, n=1, create=False, **overrides):
        kwargs = dict(author="source-bot", labels=["builder:codex"], config={},
                      runtime_observation=lambda: "ready", create=create)
        kwargs.update(overrides)
        return self.store.record(receipt, target(n), POLICY, AUTHORITY, History([]), **kwargs)

    def takeover(self):
        handoff = lane_delivery.validate_handoff(source_lane="codex", destination_lane="claude",
            target_pr="owner/repo#42", expected_head=sha(0), running_lane="claude",
            repo="owner/repo", observed_head=sha(0), target_branch=BRANCH,
            source_branch_prefixes=["codex/"])
        source = {"transport": "local_process", "state_dir": "/source-state", "writer": "source-writer"}
        old = lane_handoff.LocalWriter(Path(source["state_dir"]), source["writer"])
        old.register(repo="owner/repo", lane="codex", checkout=Path("/source-checkout"))
        old.started(10, 10)
        old.finish(quiescent=True)
        self.assertTrue(lane_handoff.prepare(handoff, source, Path("/handoffs"),
            stop=lambda *args: "terminated", head=lambda h: sha(0))["accepted"])
        self.assertTrue(lane_handoff.reserve_launch(handoff, Path("/handoffs"),
            stop=lambda *args: "terminated", head=lambda h: sha(0)))
        current = round_fixture("/rounds")
        current.started(11, 11)
        current.finish(quiescent=True)
        return handoff, source, current

    def receipt(self):
        handoff, source, current = self.takeover()
        return lane_handoff.lineage_handoff(handoff, Path("/handoffs"), current,
                                           target(), source_branch_prefixes=["codex/"])

    def test_takeover_then_same_writer_and_idempotent_replay(self):
        receipt = self.receipt()
        self.assertTrue(self.record(receipt, create=True))
        writes = len(MemoryStore.effects)
        self.assertFalse(self.record(receipt))
        self.assertEqual(writes, len(MemoryStore.effects))
        previous = self.store.read(target())
        current = round_fixture("/rounds", 2)
        current.started(12, 12)
        current.finish(quiescent=True)
        continuation = lane_delivery.lineage_continuation(current, target(2), previous)
        self.assertEqual(continuation.episode.writer_state, "same_writer")
        self.assertTrue(self.record(continuation, n=2))
        self.assertEqual(len(self.store.read(target(2))["episodes"]), 2)
        with self.assertRaises(ContractError):
            self.record(receipt, n=2)

    def test_missing_falsey_malformed_legacy_store_refuses_without_write(self):
        receipt = self.receipt()
        key = ("/producer-state", self.store._key(target()))
        for raw in (None, {}, [], False, 0, "", {"schema": "code_mower.builderLineage.v1"}):
            with self.subTest(raw=raw):
                MemoryStore.records[key] = raw
                before = len(MemoryStore.effects)
                with self.assertRaises((ValueError, TypeError)):
                    self.record(receipt)
                self.assertEqual(before, len(MemoryStore.effects))
        for raw in ({}, False, []):
            MemoryStore.records[key] = raw
            with self.assertRaises(ValueError):
                self.record(receipt, create=True)

    def test_wrong_target_exact_branch_and_conflicting_replay(self):
        receipt = self.receipt()
        self.record(receipt, create=True)
        for changes in ({"branch": BRANCH.lower()}, {"repo": "other/repo"}, {"pr_number": 43}, {"head_sha": sha(9)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.record(receipt, target(**changes), POLICY, AUTHORITY, History([]),
                    author="source-bot", labels=[], config={}, runtime_observation=lambda: "ready")
        bad = _delivery(episode(resulting_head=sha(4)), receipt.writer, receipt.round_id, TRANSPORT)
        with self.assertRaises(ValueError):
            self.record(bad, n=4)

    def test_state_strings_are_not_independent_exit_proof(self):
        handoff, source, current = self.takeover()
        intent_key = ("/handoffs", lane_handoff.key([handoff.target_pr.lower(), handoff.expected_head]))
        original = deepcopy(MemoryStore.records[intent_key])
        for state in ("completed", "cancelled", "suspended", "running", None):
            MemoryStore.records[intent_key] = original | {"writer_state": state}
            with self.subTest(state=state), self.assertRaises(ProducerRefusal):
                lane_handoff.lineage_handoff(handoff, Path("/handoffs"), current,
                                            target(), source_branch_prefixes=["codex/"])
        MemoryStore.records[intent_key] = original
        source_key = (source["state_dir"], lane_handoff.key(source["writer"]))
        original_source = deepcopy(MemoryStore.records[source_key])
        for changes in ({"quiescent": False}, {"finished": False}, {"lane": "claude"},
                        {"pid": None}, {"pgid": True}, {"repo": "wrong/repo"}):
            MemoryStore.records[source_key] = original_source | changes
            with self.subTest(changes=changes), self.assertRaises(ProducerRefusal):
                lane_handoff.lineage_handoff(handoff, Path("/handoffs"), current,
                                            target(), source_branch_prefixes=["codex/"])

    def test_continuation_wrong_writer_unstopped_round_and_stale_binding(self):
        self.record(self.receipt(), create=True)
        previous = self.store.read(target())
        current = round_fixture("/rounds", 2, writer="wrong-writer")
        current.started(12, 12)
        current.finish(quiescent=True)
        with self.assertRaises(ProducerRefusal):
            lane_delivery.lineage_continuation(current, target(2), previous)
        current = round_fixture("/other-rounds", 2)
        for state in ({}, {"finished": True}, {"finished": True, "quiescent": True}):
            key = ("/other-rounds", current.control.key)
            MemoryStore.records[key].update(state)
            with self.assertRaises(ProducerRefusal):
                lane_delivery.lineage_continuation(current, target(2), previous)
        current.started(12, 12)
        current.finish(quiescent=True)
        for changes in ({"writer": "other"}, {"round_id": "round-2"},
                        {"episodes": [episode(resulting_head=sha(9)).to_mapping()]}):
            with self.assertRaises(ValueError):
                lane_delivery.lineage_continuation(current, target(2), previous | changes)

    def test_role_runtime_and_capability_fail_before_storage_or_launch(self):
        for config, runtime in (({"role_policy": {"claude": {"builder": {"enabled": False}}}}, "ready"),
                                ({}, "unchecked"), ({}, "unavailable")):
            with self.subTest(config=config, runtime=runtime), self.assertRaises(ValueError):
                round_fixture("/rounds", config=config, runtime=runtime)
            self.assertEqual(MemoryStore.effects, [])
        receipt = _delivery(episode(), "writer", "round", TRANSPORT)
        with self.assertRaises(ValueError):
            self.record(receipt, create=True, config={"role_policy": {"claude": {"builder": {"enabled": False}}}})
        self.assertEqual(MemoryStore.effects, [])

    def test_real_supervisor_observer_path_with_external_process_io_stub(self):
        # The existing supervisor's callback is the evidence source; no caller
        # state string is accepted by the conversion. This finite completed
        # process tests the real supervisor and its stopped/reaped callbacks.
        import sys
        import tempfile
        current = round_fixture("/rounds")
        with tempfile.TemporaryDirectory() as tmp:
            result = lane_delivery.supervise_process([sys.executable, "-c", "print('finite')"],
                log_path=Path(tmp)/"round.log", timeout_seconds=5, writer=current)
        self.assertEqual(result.exit_code, 0)
        self.assertIs(current.observed(target()), current)
