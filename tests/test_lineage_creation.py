"""Stage 1 initial issue-to-PR creation lineage: contract and supervised delivery.

Every row here is adversarial about the same thing: a creation episode may only
be minted when one supervised lane is independently observed to have stopped and
exactly one readable pull request is bound to the exact branch head that lane
left behind. The accepted #963 handoff/continuation contracts are asserted
unchanged, including the rendered public marker bytes.
"""
import argparse
from pathlib import Path
import unittest
from unittest.mock import patch

from code_mower import lane_delivery
from code_mower.builder_lineage import (
    Authorities, Chain, ContractError, Episode, History, Identity, Target, admit,
    parse_markers, render, resolve,
)
from code_mower.builder_lineage_producer import ProducerRefusal, ProducerStore, Snapshot, Transport
from lineage_producer_fixtures import MemoryStore, episode as handoff_episode, sha, target as pr_target

REPO = "owner/repo"
PR = 77
BRANCH = "claude/1020-creation"
BASE = sha(9)
CREATED = sha(10)
AUTHORITY = Authorities(["lineage-publisher[bot]"])
CLAUDE = Transport("claude", "claude", "claude_cli", "local_cli")
CODEX = Transport("codex", "codex", "codex_cli", "local_cli")
# The shared human owner login is deliberately unmapped: an issue-targeted run
# is launched by the owner, so identity alone can never name the builder.
POLICY = Identity.from_mapping({
    "enabled": True,
    "authors": {},
    "labels": {"builder:claude": "claude", "builder:codex": "codex"},
    "branch_prefixes": {"claude/": "claude", "codex/": "codex"},
    "require_verified_lineage": True,
})


def created_target(**changes):
    return Target(**(dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=CREATED) | changes))


def creation_episode(**changes):
    return Episode(**(dict(sequence=1, repo=REPO, pr_number=PR, branch=BRANCH,
                           source_lane="claude", destination_lane="claude",
                           expected_head=BASE, resulting_head=CREATED,
                           writer_state="terminated", kind="creation") | changes))


def origin(**changes):
    return lane_delivery.CreationOrigin(**(dict(repo=REPO, issue_number=1020, base_sha=BASE) | changes))


def pull_payload(**changes):
    return dict(state="open", number=PR, base={"repo": {"full_name": REPO}},
                head={"ref": BRANCH, "sha": CREATED}) | changes


class CreationContractTests(unittest.TestCase):
    """Pure contract rows; no adapter, store or transport participates."""

    def test_creation_names_one_verified_contributor_at_the_created_head(self):
        chain = Chain.from_arrivals(created_target(), [creation_episode()])
        decision = resolve(chain, POLICY, "owner-login", ["builder:claude"])
        self.assertEqual(decision.reason, "verified_lineage")
        self.assertEqual(decision.status, "ready")
        self.assertEqual(decision.current_writer, "claude")
        self.assertEqual(decision.contributors, ("claude",))
        self.assertTrue(admit(decision, "codex"))
        self.assertFalse(admit(decision, "claude"))

    def test_symmetric_codex_creation_admits_the_claude_reviewer(self):
        chain = Chain.from_arrivals(created_target(branch="codex/1020-creation"),
            [creation_episode(branch="codex/1020-creation", source_lane="codex",
                              destination_lane="codex")])
        decision = resolve(chain, POLICY, "owner-login", ["builder:codex"])
        self.assertEqual((decision.reason, decision.current_writer), ("verified_lineage", "codex"))
        self.assertTrue(admit(decision, "claude"))
        self.assertFalse(admit(decision, "codex"))

    def test_creation_survives_the_public_marker_round_trip(self):
        chain = Chain.from_arrivals(created_target(), [creation_episode()])
        history = History([{"user": {"login": "lineage-publisher[bot]"}, "body": render(chain)}])
        parsed = Chain.from_arrivals(created_target(), parse_markers(history, AUTHORITY))
        self.assertEqual(parsed, chain)
        self.assertEqual(parsed.episodes[0].kind, "creation")

    def test_creation_refuses_two_lanes_a_live_writer_and_an_unmoved_base(self):
        for reason, changes in (
                ("two lanes", dict(destination_lane="codex")),
                ("live same writer", dict(writer_state="same_writer")),
                ("unnamed writer state", dict(writer_state="running")),
                ("unmoved base", dict(resulting_head=BASE)),
                ("later episode", dict(sequence=2)),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(ContractError):
                    creation_episode(**changes)

    def test_creation_may_only_originate_a_chain(self):
        # Episode already refuses sequence 2, so no arrival stream can smuggle a
        # creation into the middle of a chain; the Chain rule is the second lock.
        continuation = creation_episode(sequence=2, kind="continuation", writer_state="same_writer",
                                        expected_head=CREATED, resulting_head=sha(11))
        chain = Chain.from_arrivals(created_target(head_sha=sha(11)),
                                    [creation_episode(), continuation])
        self.assertEqual([e.kind for e in chain.episodes], ["creation", "continuation"])
        self.assertEqual(resolve(chain, POLICY, "owner-login", ["builder:claude"]).reason,
                         "verified_lineage")
        with self.assertRaises(ContractError):
            Chain.from_arrivals(created_target(), [creation_episode(kind="takeover")])

    def test_a_creation_hands_off_to_a_second_lane_unchanged(self):
        taken = Episode(sequence=2, repo=REPO, pr_number=PR, branch=BRANCH, source_lane="claude",
                        destination_lane="codex", expected_head=CREATED, resulting_head=sha(11),
                        writer_state="terminated")
        chain = Chain.from_arrivals(created_target(head_sha=sha(11)), [creation_episode(), taken])
        decision = resolve(chain, POLICY, "owner-login", ["builder:codex"])
        self.assertEqual(decision.current_writer, "codex")
        self.assertEqual(decision.contributors, ("claude", "codex"))
        self.assertFalse(admit(decision, "claude"))
        self.assertTrue(admit(decision, "devin"))

    def test_accepted_handoff_and_continuation_marker_bytes_are_unchanged(self):
        chain = Chain.from_arrivals(pr_target(1), [handoff_episode(1)])
        self.assertEqual(render(chain),
            '<!-- CODE_MOWER_BUILDER_LINEAGE: {"episodes":[{"branch":"codex/Topic",'
            '"destination_lane":"claude","expected_head":"'
            + sha(0) + '","kind":"handoff","pr_number":42,"repo":"owner/repo",'
            '"resulting_head":"' + sha(1) + '","sequence":1,"source_lane":"codex",'
            '"writer_state":"terminated"}],"schema":"code_mower.builderLineage.v1"} -->')
        with self.assertRaises(ContractError):
            Chain.from_arrivals(pr_target(2), [handoff_episode(2)])


class FakeGitHub:
    """Finite in-memory GitHub transport; every producer validation stays real."""

    def __init__(self, *, pulls=None, target=None, labels=("builder:claude",), author="owner-login"):
        self.target = target if target is not None else created_target()
        self.pulls = [pull_payload()] if pulls is None else pulls
        self.current_labels = tuple(labels)
        self.author = author
        self.public = []
        self.effects = []

    def pulls_for_branch(self, repo, branch):
        self.effects.append(("pulls", repo, branch))
        return self.pulls

    def snapshot(self, requested):
        self.effects.append("snapshot")
        return Snapshot(self.target, self.author, self.current_labels)

    def history(self, requested):
        self.effects.append("history")
        return History(list(self.public))

    def post(self, requested, body):
        self.effects.append("post")
        self.public.append({"user": {"login": "lineage-publisher[bot]"}, "body": body})

    def labels(self, requested, desired, remove, add):
        self.effects.append("labels")
        self.current_labels = tuple(s for s in self.current_labels if s not in remove)
        if add:
            self.current_labels += (desired,)


class CreationDeliveryTests(unittest.TestCase):
    """Supervised round, created-PR discovery, private record and publication."""

    def setUp(self):
        MemoryStore.records, MemoryStore.effects = {}, []
        self.addCleanup(patch.stopall)
        patch("code_mower.builder_lineage_producer.ContextStore", MemoryStore).start()
        patch("code_mower.lane_handoff.ContextStore", MemoryStore).start()
        patch("code_mower.lane_delivery._creation_checkout",
              side_effect=lambda checkout, binding: Path(checkout)).start()
        self.store = ProducerStore(Path("/producer-state"))

    def round_fixture(self, *, transport=CLAUDE, round_id="creation-round-1", binding=None,
                      quiescent=True, started=True, finished=True):
        observer = lane_delivery.LineageCreationRound(
            Path("/rounds"), round_id, "claude--writer", binding or origin(), transport,
            Path("/creation-checkout"), config={}, runtime_observation=lambda: "ready")
        if started:
            observer.started(21, 21)
        if finished:
            observer.finish(quiescent=quiescent)
        return observer

    def record(self, delivery, target=None, **overrides):
        kwargs = dict(author="owner-login", labels=["builder:claude"], config={},
                      runtime_observation=lambda: "ready", create=True)
        kwargs.update(overrides)
        return self.store.record(delivery, target or created_target(), POLICY, AUTHORITY,
                                 History([]), **kwargs)

    def test_stopped_round_records_and_publishes_the_creation_episode(self):
        observer = self.round_fixture()
        io = FakeGitHub()
        created = lane_delivery.discover_created_pull(io, origin(), BRANCH, CREATED)
        self.assertEqual(created, created_target())
        delivery = lane_delivery.lineage_creation(observer, created, BASE)
        self.assertEqual(delivery.episode.kind, "creation")
        self.assertEqual(delivery.episode.expected_head, BASE)
        self.assertEqual(delivery.transport.lane, "claude")
        self.assertTrue(self.record(delivery))
        stored = self.store.read(created)["episodes"]
        self.assertEqual(stored, [delivery.episode.to_mapping()])

        from code_mower.builder_lineage_producer import publish
        publication = publish(io, created, POLICY, AUTHORITY, stored)
        self.assertTrue(publication.comment_posted)
        decision = publication.observation.decision
        self.assertEqual((decision.reason, decision.current_writer), ("verified_lineage", "claude"))
        self.assertEqual(decision.contributors, ("claude",))
        self.assertTrue(admit(decision, "codex"))
        self.assertEqual(io.current_labels, ("builder:claude",))

    def test_replay_of_one_round_is_idempotent_but_a_conflicting_replay_refuses(self):
        observer = self.round_fixture()
        created = lane_delivery.discover_created_pull(FakeGitHub(), origin(), BRANCH, CREATED)
        delivery = lane_delivery.lineage_creation(observer, created, BASE)
        self.assertTrue(self.record(delivery))
        writes = len(MemoryStore.effects)
        self.assertFalse(self.record(delivery, create=False))
        self.assertEqual(writes, len(MemoryStore.effects))
        moved = lane_delivery.lineage_creation(observer, created_target(head_sha=sha(12)), BASE)
        with self.assertRaises(ProducerRefusal):
            self.record(moved, created_target(head_sha=sha(12)), create=False)

    def test_creation_cannot_start_a_chain_without_the_explicit_create_selection(self):
        observer = self.round_fixture()
        created = lane_delivery.discover_created_pull(FakeGitHub(), origin(), BRANCH, CREATED)
        delivery = lane_delivery.lineage_creation(observer, created, BASE)
        writes = len(MemoryStore.effects)
        with self.assertRaises(ProducerRefusal):
            self.record(delivery, create=False)
        self.assertEqual(writes, len(MemoryStore.effects))

    def test_incomplete_writer_exit_refuses_before_any_episode(self):
        for reason, changes in (("never finished", dict(finished=False)),
                                ("not quiescent", dict(quiescent=False)),
                                ("never started", dict(started=False))):
            with self.subTest(reason=reason):
                observer = self.round_fixture(round_id="creation-round-" + reason.replace(" ", "-"),
                                              **changes)
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.lineage_creation(observer, created_target(), BASE)

    def test_creation_outside_the_supervised_origin_refuses(self):
        observer = self.round_fixture()
        for reason, args in (
                ("other repository", (created_target(repo="owner/other"), BASE)),
                ("other base", (created_target(), sha(3))),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.lineage_creation(observer, *args)

    def test_ambiguous_absent_or_mismatched_pull_request_fails_closed(self):
        for reason, io, head in (
                ("no pull request", FakeGitHub(pulls=[]), CREATED),
                ("two pull requests", FakeGitHub(pulls=[pull_payload(), pull_payload(number=78)]), CREATED),
                ("closed pull request", FakeGitHub(pulls=[pull_payload(state="closed")]), CREATED),
                ("head moved", FakeGitHub(), sha(12)),
                ("fork base", FakeGitHub(pulls=[pull_payload(base={"repo": {"full_name": "fork/repo"}})]), CREATED),
                ("other branch", FakeGitHub(pulls=[pull_payload(head={"ref": "claude/other", "sha": CREATED})]), CREATED),
                ("malformed entry", FakeGitHub(pulls=[{"state": "open"}]), CREATED),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(ContractError):
                    lane_delivery.discover_created_pull(io, origin(), BRANCH, head)

    def test_observed_branch_requires_descent_from_the_immutable_base(self):
        import subprocess
        outputs = {("branch", "--show-current"): BRANCH, ("rev-parse", "HEAD"): CREATED}

        def check_output(argv, **kwargs):
            return outputs[tuple(argv[3:])] + "\n"

        with patch("code_mower.lane_delivery._creation_repository",
                   side_effect=lambda checkout: Path(checkout)), \
                patch("code_mower.lane_delivery.subprocess.check_output", side_effect=check_output), \
                patch("code_mower.lane_delivery.subprocess.run") as run:
            self.assertEqual(lane_delivery.observed_creation_branch("/creation-checkout", origin()),
                             (BRANCH, CREATED))
            run.side_effect = subprocess.CalledProcessError(1, "git")
            with self.assertRaises(ProducerRefusal):
                lane_delivery.observed_creation_branch("/creation-checkout", origin())
            run.side_effect = None
            outputs[("rev-parse", "HEAD")] = BASE
            with self.assertRaises(ProducerRefusal):
                lane_delivery.observed_creation_branch("/creation-checkout", origin())
            outputs[("rev-parse", "HEAD")] = CREATED
            outputs[("branch", "--show-current")] = ""
            with self.assertRaises(ProducerRefusal):
                lane_delivery.observed_creation_branch("/creation-checkout", origin())

    def test_creation_origin_refuses_incomplete_issue_bindings(self):
        for changes in (dict(repo="owner"), dict(repo=42), dict(issue_number=0),
                        dict(issue_number=True), dict(issue_number="1020"),
                        dict(base_sha="deadbeef"), dict(base_sha=None)):
            with self.subTest(changes=changes):
                with self.assertRaises(ProducerRefusal):
                    origin(**changes)
        self.assertEqual(origin(repo="Owner/Repo").repo, REPO)

    def test_round_requires_an_exact_binding_and_named_identifiers(self):
        with self.assertRaises(ProducerRefusal):
            lane_delivery.LineageCreationRound(Path("/rounds"), "r", "w", {"repo": REPO}, CLAUDE,
                Path("/creation-checkout"), config={}, runtime_observation=lambda: "ready")
        with self.assertRaises(ProducerRefusal):
            lane_delivery.LineageCreationRound(Path("/rounds"), "bad round", "w", origin(), CLAUDE,
                Path("/creation-checkout"), config={}, runtime_observation=lambda: "ready")

    def test_symmetric_codex_creation_round_records_and_admits_claude(self):
        observer = self.round_fixture(transport=CODEX, round_id="codex-round")
        io = FakeGitHub(pulls=[pull_payload(head={"ref": "codex/1020-creation", "sha": CREATED})],
                        target=created_target(branch="codex/1020-creation"), labels=("builder:codex",))
        created = lane_delivery.discover_created_pull(io, origin(), "codex/1020-creation", CREATED)
        delivery = lane_delivery.lineage_creation(observer, created, BASE)
        self.assertEqual(delivery.episode.destination_lane, "codex")
        self.assertTrue(self.record(delivery, created, labels=["builder:codex"]))
        decision = resolve(Chain.from_arrivals(created, [delivery.episode]), POLICY,
                           "owner-login", ["builder:codex"])
        self.assertTrue(admit(decision, "claude"))

    def test_shared_owner_login_still_needs_the_stage_two_reviewer_attestation(self):
        """The reviewer floor maps the shared owner login onto the reviewer lane.

        Stage 1 records the builder; only stage 2's workflow-attested audit
        publication can make that reviewer independent, so this conflict is the
        documented remaining gap rather than an accepted admission.
        """
        chain = Chain.from_arrivals(created_target(), [creation_episode()])
        floored = POLICY.with_reviewer_floor("codex", ["owner-login"])
        decision = resolve(chain, floored, "owner-login", ["builder:claude"])
        self.assertEqual(decision.status, "conflict")
        self.assertEqual(decision.reason, "unrecorded_contributor")
        self.assertFalse(admit(decision, "codex"))


class CreationLauncherTests(unittest.TestCase):
    """The supervise entrypoint wiring, from registration to publication."""

    def setUp(self):
        MemoryStore.records, MemoryStore.effects = {}, []
        self.addCleanup(patch.stopall)
        patch("code_mower.builder_lineage_producer.ContextStore", MemoryStore).start()
        patch("code_mower.lane_handoff.ContextStore", MemoryStore).start()
        patch("code_mower.lane_delivery._creation_checkout",
              side_effect=lambda checkout, binding: Path(checkout)).start()
        patch("code_mower.provider_runners.lineage.require_capabilities").start()
        patch("code_mower.provider_runners.lineage.trusted_policy",
              return_value=({}, POLICY, AUTHORITY)).start()
        patch("code_mower.lane_delivery.observed_creation_branch",
              return_value=(BRANCH, CREATED)).start()
        self.attribution = patch("code_mower.builder_runs.record_lineage_builder").start()

    def args(self, **changes):
        return argparse.Namespace(**(dict(
            cwd=Path("/creation-checkout"), writer="creation-round-1", writer_state_dir=Path("/rounds"),
            writer_repo=REPO, writer_lane="claude", lineage_writer="claude--writer",
            lineage_issue=1020, lineage_base=BASE, lineage_before=None, lineage_handoff=None,
            lineage_store=Path("/producer-state"), lineage_output=Path("/out/event.json"),
        ) | changes))

    def result(self, reason="completed", exit_code=0):
        return argparse.Namespace(reason=reason, exit_code=exit_code)

    def test_launcher_publishes_after_an_independently_observed_exit(self):
        io = FakeGitHub()
        observer, finish = lane_delivery._start_creation_round(
            self.args(), io=io, runtime_observation=lambda: "ready")
        observer.started(31, 31)
        observer.finish(quiescent=True)
        finish(self.result())
        self.assertIn("post", io.effects)
        observation = self.attribution.call_args.args[0]
        self.assertEqual(observation.decision.current_writer, "claude")
        self.assertEqual(observation.chain.episodes[0].kind, "creation")
        self.assertEqual(self.attribution.call_args.args[1].lane, "claude")

    def test_launcher_refuses_an_unfinished_or_failed_round_without_publishing(self):
        io = FakeGitHub()
        observer, finish = lane_delivery._start_creation_round(
            self.args(), io=io, runtime_observation=lambda: "ready")
        observer.started(32, 32)
        with self.assertRaises(ProducerRefusal):
            finish(self.result())
        observer.finish(quiescent=True)
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            finish(self.result(reason="timeout", exit_code=1))
        self.assertNotIn("post", io.effects)
        self.attribution.assert_not_called()

    def test_launcher_refuses_a_branch_outside_this_lanes_prefixes(self):
        io = FakeGitHub()
        with patch("code_mower.lane_delivery.observed_creation_branch",
                   return_value=("codex/1020-creation", CREATED)):
            observer, finish = lane_delivery._start_creation_round(
                self.args(), io=io, runtime_observation=lambda: "ready")
            observer.started(33, 33)
            observer.finish(quiescent=True)
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                finish(self.result())
        self.assertNotIn("post", io.effects)

    def test_launcher_refuses_incomplete_or_contradictory_selections(self):
        for reason, changes in (
                ("no private store", dict(lineage_store=None)),
                ("no attribution output", dict(lineage_output=None)),
                ("no supervised writer", dict(lineage_writer=None)),
                ("existing pull request target", dict(lineage_before=Path("/before.json"))),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(lane_delivery.LaneDeliveryError):
                    lane_delivery._start_creation_round(self.args(**changes), io=FakeGitHub(),
                                                        runtime_observation=lambda: "ready")


if __name__ == "__main__":
    unittest.main()
