"""Stage 1 initial issue-to-PR creation lineage: contract and supervised delivery.

Every row here is adversarial about the same thing: a creation episode may only
be minted when one supervised lane is independently observed to have stopped and
exactly one readable pull request is bound to the exact branch head that lane
left behind — on a clean checkout, and on the one branch the round reserved
while nothing else held it. The accepted #963 handoff/continuation contracts are
asserted unchanged, including the rendered public marker bytes.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from code_mower import branch_policy, lane_delivery
from code_mower.builder_lineage import (
    Authorities, Chain, ContractError, Episode, History, Identity, Target, admit,
    parse_markers, render, resolve,
)
from code_mower.builder_lineage_producer import (
    GitHub, ProducerRefusal, ProducerStore, Snapshot, Transport, decode_transport,
)
from code_mower.context_contract import ContextError
from code_mower.context_store import MAX_STATE_BYTES
from lineage_producer_fixtures import MemoryStore, episode as handoff_episode, sha, target as pr_target

REPO = "owner/repo"
ISSUE = 1020
# The frontier is the highest pull request number that existed before launch, so
# the created pull request is the first number the repository allocated after it.
FRONTIER = 1020
PR = 1021
BRANCH = "claude/1020-creation"
CODEX_BRANCH = "codex/1020-creation"
BASE = sha(9)
CREATED = sha(10)
AUTHORITY = Authorities(["lineage-publisher[bot]"])
# The real checkout observations, captured before any test patches them away, so
# one row each can still exercise them against a genuine directory.
REAL_CHECKOUT = lane_delivery._lineage_checkout
REAL_CREATION_CHECKOUT = lane_delivery._creation_checkout
STATUS = ("status", "--porcelain", "-z", "--untracked-files=all")
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
    return lane_delivery.CreationOrigin(**(dict(repo=REPO, issue_number=ISSUE, base_sha=BASE,
                                                branch=BRANCH, pull_frontier=FRONTIER) | changes))


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

    def __init__(self, *, pulls=None, refs=None, target=None, labels=("builder:claude",),
                 author="owner-login", frontier=FRONTIER):
        self.target = target if target is not None else created_target()
        self.pulls = [pull_payload()] if pulls is None else pulls
        self.refs = self.pushed_refs() if refs is None else dict(refs)
        self.current_labels = tuple(labels)
        self.author = author
        self.frontier = frontier
        self.public = []
        self.effects = []

    def pushed_refs(self):
        """Every branch the readable pull requests sit on actually exists."""
        return {entry["head"].get("ref"): entry["head"].get("sha") for entry in self.pulls
                if isinstance(entry.get("head"), dict)}

    def writer_created(self, *pulls):
        """Apply exactly what only the supervised writer can leave behind.

        Before launch the reserved branch has no ref and no pull request; this
        is the push and the pull request the writer itself performs during the
        round, so a launcher row observes the same two states the real one does.
        """
        self.pulls = list(pulls) if pulls else [pull_payload()]
        self.refs = self.pushed_refs()
        return self

    def pull_frontier(self, repo):
        self.effects.append(("frontier", repo))
        return self.frontier

    def pulls_for_branch(self, repo, branch):
        self.effects.append(("pulls", repo, branch))
        return self.pulls

    def branch_ref(self, repo, branch):
        self.effects.append(("ref", repo, branch))
        return self.refs.get(branch)

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
        patch("code_mower.lane_delivery._lineage_checkout",
              side_effect=lambda checkout, target: None).start()
        self.store = ProducerStore(Path("/producer-state"))

    def round_fixture(self, *, transport=CLAUDE, round_id="creation-round-1", binding=None,
                      quiescent=True, started=True, finished=True,
                      checkout=Path("/creation-checkout")):
        observer = lane_delivery.LineageCreationRound(
            Path("/rounds"), round_id, "claude--writer", binding or origin(), transport,
            checkout, config={}, runtime_observation=lambda: "ready")
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
        created = observer.bind_created(lane_delivery.discover_created_pull(io, origin(), BRANCH, CREATED))
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
        from code_mower.builder_lineage_producer import _delivery
        observer = self.round_fixture()
        created = observer.bind_created(
            lane_delivery.discover_created_pull(FakeGitHub(), origin(), BRANCH, CREATED))
        delivery = lane_delivery.lineage_creation(observer, created, BASE)
        self.assertTrue(self.record(delivery))
        writes = len(MemoryStore.effects)
        self.assertFalse(self.record(delivery, create=False))
        self.assertEqual(writes, len(MemoryStore.effects))
        # The round itself can no longer mint a second, different receipt, so the
        # store's own refusal is asserted against a hand-built conflicting one.
        moved = _delivery(creation_episode(resulting_head=sha(12)), delivery.writer,
                          delivery.round_id, CLAUDE)
        with self.assertRaises(ProducerRefusal):
            self.record(moved, created_target(head_sha=sha(12)), create=False)

    def test_one_finished_round_attributes_exactly_one_created_pull_request(self):
        """A finished observer is not a licence to mint receipts for other work.

        Registration binds an issue and a base, never a pull request, so only
        the persisted discovery separates this round's creation from any other
        pull request numbered above the pre-launch frontier. Without that bind,
        one stopped writer could name unrelated work as its own single-lane
        creation, and the store would accept both because its replay checks are
        scoped per target.
        """
        observer = self.round_fixture(round_id="creation-round-single")
        created = observer.bind_created(created_target())
        self.assertEqual(observer.bind_created(created_target()), created)
        for reason, other in (("another pull request", created_target(pr_number=PR + 1)),
                              ("a moved head", created_target(head_sha=sha(12))),
                              ("another branch", created_target(branch="claude/other"))):
            with self.subTest(reason=reason):
                with self.assertRaises(ProducerRefusal):
                    observer.bind_created(other)
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.lineage_creation(observer, other, BASE)
        self.assertEqual(lane_delivery.lineage_creation(observer, created, BASE).episode.pr_number, PR)

    def test_a_round_that_created_nothing_on_purpose_has_nothing_to_attribute(self):
        """A bounded declaration plus an intact reservation is a no-creation round.

        The provider may answer an issue with "no code change is needed" or
        "this needs the owner" instead of a pull request. Nothing was created
        then, so there is nothing to discover and nothing to publish — and the
        runner brokers that declaration only from a provider that exited zero,
        so refusing would spend the unit's one explanation on a lineage error.

        Both halves are required. The declaration alone is the provider's own
        claim, so the repository has to agree the branch this round reserved is
        still exactly as unused as it was before launch; and the reservation
        alone cannot tell a pull request that was never opened from one a failed
        read cannot see, so an absent declaration still refuses.
        """
        checkout = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, checkout, ignore_errors=True)
        declaration = checkout / lane_delivery.LANE_OUTCOME_FILE
        declaration.parent.mkdir(parents=True)

        def declined(written, io=None):
            declaration.unlink(missing_ok=True)
            if written is not None:
                declaration.write_text(written, encoding="utf-8")
            return lane_delivery.declined_creation(
                io if io is not None else FakeGitHub(pulls=[], refs={}), checkout, origin())

        for reason, written in (
                ("no declaration at all", None),
                ("an unreadable declaration", "{"),
                ("a declaration that is not an object", '"no_change"'),
                ("no outcome at all", '{"summary": "nothing to change"}'),
                ("an outcome this contract never accepts", '{"outcome": "delivered"}'),
                ("a delivery reported as a declaration", '{"outcome": "pr_opened"}')):
            with self.subTest(reason=reason):
                self.assertFalse(declined(written))
        for reason, written in (
                ("no change", '{"outcome": "no_change", "summary": "nothing to change"}'),
                ("owner action", '{"outcome": "owner_action", "summary": "needs a credential"}'),
                # The summary is the runner's to validate: it posts the comment
                # and applies the label, and voids a declaration without one.
                # Re-deciding that here could only disagree with it.
                ("an outcome whose summary the runner will void", '{"outcome": "no_change"}')):
            with self.subTest(reason=reason):
                self.assertTrue(declined(written))
        # Whatever it declared, a round that pushed the reserved branch or
        # opened a pull request from it created something, and goes back through
        # discovery and publication unchanged.
        bounded = '{"outcome": "no_change", "summary": "nothing to change"}'
        for reason, io in (
                ("a pull request on the reserved branch", FakeGitHub(refs={})),
                ("a closed pull request on it", FakeGitHub(pulls=[pull_payload(state="closed")], refs={})),
                ("the reserved branch pushed", FakeGitHub(pulls=[], refs={BRANCH: CREATED}))):
            with self.subTest(reason=reason):
                self.assertFalse(declined(bounded, io))
        self.assertTrue(declined(bounded, FakeGitHub(pulls=[], refs={"claude/other": CREATED})))

    def test_minting_refuses_a_pull_request_this_round_never_discovered(self):
        observer = self.round_fixture(round_id="creation-round-unbound")
        with self.assertRaises(ProducerRefusal):
            lane_delivery.lineage_creation(observer, created_target(), BASE)

    def test_minting_requires_the_checkout_to_still_sit_on_the_created_head(self):
        """The created head must be the one the writer left, as a delivery's is."""
        checkout = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, checkout, ignore_errors=True)
        (checkout / ".git").mkdir()
        left = {("rev-parse", "HEAD"): CREATED, ("branch", "--show-current"): BRANCH}
        observer = self.round_fixture(round_id="creation-round-checkout", checkout=checkout)
        with patch("code_mower.lane_delivery._lineage_checkout", REAL_CHECKOUT), \
                patch("code_mower.lane_delivery.subprocess.check_output",
                      side_effect=lambda argv, **kwargs: left[tuple(argv[3:])] + "\n"):
            created = observer.bind_created(created_target())
            self.assertEqual(lane_delivery.lineage_creation(observer, created, BASE)
                             .episode.resulting_head, CREATED)
            for reason, key, value in (("head moved", ("rev-parse", "HEAD"), sha(12)),
                                       ("branch changed", ("branch", "--show-current"), "claude/other")):
                with self.subTest(reason=reason):
                    restored, left[key] = left[key], value
                    with self.assertRaises(ProducerRefusal):
                        lane_delivery.lineage_creation(observer, created, BASE)
                    left[key] = restored

    def test_creation_cannot_start_a_chain_without_the_explicit_create_selection(self):
        observer = self.round_fixture()
        created = observer.bind_created(
            lane_delivery.discover_created_pull(FakeGitHub(), origin(), BRANCH, CREATED))
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
                    observer.bind_created(created_target())
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.lineage_creation(observer, created_target(), BASE)

    def test_creation_outside_the_supervised_origin_refuses(self):
        observer = self.round_fixture()
        observer.bind_created(created_target())
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

    def test_a_pull_request_that_existed_before_launch_is_never_a_creation(self):
        """A writer may check out an existing PR branch that descends from the base.

        Branch, head and ancestry all look exactly like a creation in that case,
        so only the pre-launch frontier separates the two. Attributing it would
        publish a chain naming this lane alone and silently drop every earlier
        contributor from later reviewer-exclusion checks.
        """
        for reason, binding, number in (
                ("opened before this round", origin(), FRONTIER),
                ("opened before the targeted issue", origin(pull_frontier=3), ISSUE - 1),
                ("frontier moved past the round", origin(pull_frontier=PR), PR),
        ):
            with self.subTest(reason=reason):
                io = FakeGitHub(pulls=[pull_payload(number=number)])
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.discover_created_pull(io, binding, BRANCH, CREATED)
        # An empty repository frontier still floors the round at the issue number.
        self.assertEqual(origin(pull_frontier=0).creation_floor, ISSUE)
        self.assertEqual(origin().creation_floor, FRONTIER)
        io = FakeGitHub()
        self.assertEqual(lane_delivery.discover_created_pull(io, origin(), BRANCH, CREATED),
                         created_target())

    def test_minting_refuses_a_pre_existing_pull_request_handed_in_directly(self):
        observer = self.round_fixture(round_id="creation-round-pre-existing")
        for handed_in in (lambda t: observer.bind_created(t),
                          lambda t: lane_delivery.lineage_creation(observer, t, BASE)):
            with self.assertRaises(ProducerRefusal):
                handed_in(created_target(pr_number=FRONTIER))
        observer.bind_created(created_target())
        self.assertEqual(lane_delivery.lineage_creation(observer, created_target(), BASE)
                         .episode.pr_number, PR)

    def test_for_launch_binds_the_frontier_and_branch_observed_before_registration(self):
        io = FakeGitHub(pulls=[], refs={}, frontier=1000)
        bound = lane_delivery.CreationOrigin.for_launch(io, "Owner/Repo", ISSUE, BASE, BRANCH)
        self.assertEqual((bound.repo, bound.branch, bound.pull_frontier), (REPO, BRANCH, 1000))
        # Every observation is a read of the repository as it stands before the
        # round is registered; nothing is written and nothing is launched yet.
        self.assertEqual(io.effects, [("frontier", REPO), ("pulls", REPO, BRANCH), ("ref", REPO, BRANCH)])
        with self.assertRaises(ProducerRefusal):
            lane_delivery.CreationOrigin.for_launch(io, "owner", ISSUE, BASE, BRANCH)

    def test_a_branch_anything_else_already_holds_can_never_be_reserved(self):
        """The frontier cannot separate this round's creation from a concurrent one.

        A pull request another writer opens after the frontier read is numbered
        above it too, so a supervised writer that checked out that branch would
        pass every remaining check and publish single-lane attribution for work
        it did not do. The branch is what separates them, so it is claimed
        before launch and only while provably nothing else holds it.
        """
        for reason, io in (
                ("an existing pull request", FakeGitHub(refs={})),
                ("a closed pull request on it", FakeGitHub(pulls=[pull_payload(state="closed")], refs={})),
                ("an existing branch ref", FakeGitHub(pulls=[], refs={BRANCH: CREATED})),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.CreationOrigin.for_launch(io, REPO, ISSUE, BASE, BRANCH)
        unused = FakeGitHub(pulls=[], refs={"claude/other": CREATED})
        self.assertEqual(lane_delivery.CreationOrigin.for_launch(unused, REPO, ISSUE, BASE, BRANCH).branch,
                         BRANCH)

    def test_discovery_requires_the_reserved_branch_to_carry_the_created_head(self):
        """The reserved branch is the evidence the writer itself pushed it.

        It had no ref when the round was registered, so a ref that now points at
        the head the stopped writer left in its own checkout can only have been
        pushed during the round — and the one pull request on it opened from it.
        """
        for reason, io, branch in (
                ("the reserved branch was never pushed", FakeGitHub(refs={}), BRANCH),
                ("the pushed branch moved past the writer", FakeGitHub(refs={BRANCH: sha(12)}), BRANCH),
                ("another branch entirely", FakeGitHub(), "claude/other"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(ProducerRefusal):
                    lane_delivery.discover_created_pull(io, origin(), branch, CREATED)
        self.assertEqual(lane_delivery.discover_created_pull(FakeGitHub(), origin(), BRANCH, CREATED),
                         created_target())

    def test_registration_refuses_a_checkout_carrying_work_beyond_the_base(self):
        """Uncommitted work in the checkout is not this round's immutable base.

        Staged, modified or untracked work another lane left is invisible to a
        HEAD comparison. The supervised writer could commit it and open a pull
        request whose creation episode names only this lane, so the omitted
        contributor would be admitted to review its own work.
        """
        outputs = {("rev-parse", "HEAD"): BASE, STATUS: ""}
        with patch("code_mower.lane_delivery._creation_repository",
                   side_effect=lambda checkout: Path(checkout)), \
                patch("code_mower.lane_delivery.subprocess.check_output",
                      side_effect=lambda argv, **kwargs: outputs[tuple(argv[3:])] + "\n"):
            self.assertEqual(REAL_CREATION_CHECKOUT("/creation-checkout", origin()),
                             Path("/creation-checkout"))
            for reason, dirty in (
                    ("staged work", "M  src/code_mower/lane_delivery.py"),
                    ("modified work", " M tools/lanes/run_mac_lane.sh"),
                    ("untracked work", "?? src/code_mower/left_behind.py"),
                    ("renamed work", "R  docs/lanes/new.md\0docs/lanes/old.md"),
                    # A repository that tracks its own private state has
                    # committable content there like anywhere else.
                    ("staged private state", "A  .code-mower/tracked.json"),
                    ("modified private state", " M .code-mower/tracked.json"),
                    # Runner runtime alone is dropped; work beside it is not.
                    ("work beside the runtime",
                     "?? .code-mower/runtime/bin/python3\0?? src/code_mower/left_behind.py")):
                with self.subTest(reason=reason):
                    outputs[STATUS] = dirty + "\0"
                    with self.assertRaises(ProducerRefusal):
                        REAL_CREATION_CHECKOUT("/creation-checkout", origin())
            # The runner writes its own runtime into the checkout before the
            # writer exists and installs no git exclusion for it, so a
            # repository that does not ignore `.code-mower/` must still register
            # a clean round rather than fail every creation before launch.
            outputs[STATUS] = ("?? .code-mower/runtime/bin/python\0"
                               "?? .code-mower/runtime/bin/python3\0"
                               "?? .code-mower/lane-delivery/outcome.json\0")
            self.assertEqual(REAL_CREATION_CHECKOUT("/creation-checkout", origin()),
                             Path("/creation-checkout"))
            outputs[STATUS] = ""
            outputs[("rev-parse", "HEAD")] = CREATED
            with self.assertRaises(ProducerRefusal):
                REAL_CREATION_CHECKOUT("/creation-checkout", origin())

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
            # A writer that ended on any other branch — including one another
            # writer had already published — never created this round's work.
            outputs[("branch", "--show-current")] = "claude/other"
            with self.assertRaises(ProducerRefusal):
                lane_delivery.observed_creation_branch("/creation-checkout", origin())

    def test_creation_origin_refuses_incomplete_issue_bindings(self):
        for changes in (dict(repo="owner"), dict(repo=42), dict(issue_number=0),
                        dict(issue_number=True), dict(issue_number="1020"),
                        dict(base_sha="deadbeef"), dict(base_sha=None),
                        dict(pull_frontier=-1), dict(pull_frontier="1020"),
                        dict(pull_frontier=None), dict(pull_frontier=True),
                        dict(branch=""), dict(branch=None), dict(branch=BRANCH.encode()),
                        dict(branch=" " + BRANCH), dict(branch=BRANCH + " "),
                        dict(branch="claude/../codex/1020"), dict(branch="claude//1020"),
                        dict(branch="claude/1020.lock"), dict(branch="claude/1020@{1}"),
                        dict(branch="claude/1020/"), dict(branch="claude/1020."),
                        dict(branch="/claude/1020"), dict(branch=".claude/1020"),
                        dict(branch="claude/" + "x" * 200)):
            with self.subTest(changes=changes):
                with self.assertRaises(ProducerRefusal):
                    origin(**changes)
        self.assertEqual(origin(repo="Owner/Repo").repo, REPO)
        # The reserved branch is validated by the repository's one branch
        # contract, not a stricter local spelling: a name `branch_policy` renders
        # and the pre-push guard authorizes must still reach the writer, and
        # lineage `Target` accepts exactly the same set when the episode is
        # minted. A stricter rule here would abort the round after branch
        # resolution and guard setup, before the writer ever launched.
        for accepted in ("claude/1020-", "claude/1020.lockfile", "x",
                         "claude/" + "x" * (branch_policy.MAX_BRANCH_LENGTH - 7)):
            with self.subTest(accepted=accepted):
                self.assertTrue(branch_policy.is_valid_ref(accepted))
                self.assertEqual(origin(branch=accepted).branch, accepted)
                self.assertEqual(Target(REPO, PR, accepted, CREATED).branch, accepted)

    def test_round_requires_an_exact_binding_and_named_identifiers(self):
        with self.assertRaises(ProducerRefusal):
            lane_delivery.LineageCreationRound(Path("/rounds"), "r", "w", {"repo": REPO}, CLAUDE,
                Path("/creation-checkout"), config={}, runtime_observation=lambda: "ready")
        with self.assertRaises(ProducerRefusal):
            lane_delivery.LineageCreationRound(Path("/rounds"), "bad round", "w", origin(), CLAUDE,
                Path("/creation-checkout"), config={}, runtime_observation=lambda: "ready")

    def test_symmetric_codex_creation_round_records_and_admits_claude(self):
        binding = origin(branch=CODEX_BRANCH)
        observer = self.round_fixture(transport=CODEX, round_id="codex-round", binding=binding)
        io = FakeGitHub(pulls=[pull_payload(number=PR, head={"ref": CODEX_BRANCH, "sha": CREATED})],
                        target=created_target(branch=CODEX_BRANCH), labels=("builder:codex",))
        created = observer.bind_created(
            lane_delivery.discover_created_pull(io, binding, CODEX_BRANCH, CREATED))
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
        patch("code_mower.lane_delivery._lineage_checkout",
              side_effect=lambda checkout, target: None).start()
        patch("code_mower.provider_runners.lineage.require_capabilities").start()
        patch("code_mower.provider_runners.lineage.trusted_policy",
              return_value=({}, POLICY, AUTHORITY)).start()
        patch("code_mower.lane_delivery.observed_creation_branch",
              return_value=(BRANCH, CREATED)).start()
        self.attribution = patch("code_mower.builder_runs.record_lineage_builder").start()
        # The discovered pull request is named on disk for real, because the
        # runner reads exactly those bytes to file the private record.
        self.artifacts = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.artifacts, ignore_errors=True)
        self.created_output = self.artifacts / "run.created.json"

    def args(self, **changes):
        return argparse.Namespace(**(dict(
            cwd=Path("/creation-checkout"), writer="creation-round-1", writer_state_dir=Path("/rounds"),
            writer_repo=REPO, writer_lane="claude", lineage_writer="claude--writer",
            lineage_issue=1020, lineage_base=BASE, lineage_before=None, lineage_handoff=None,
            lineage_branch=BRANCH, lineage_store=Path("/producer-state"),
            lineage_output=Path("/out/event.json"), lineage_created=self.created_output,
        ) | changes))

    def named_creation(self):
        """What the round wrote down about the pull request it discovered."""
        return json.loads(self.created_output.read_text(encoding="utf-8"))

    def unused(self, **changes):
        """The repository as it stands before launch: nothing holds the branch."""
        return FakeGitHub(pulls=[], refs={}, **changes)

    def result(self, reason="completed", exit_code=0):
        return argparse.Namespace(reason=reason, exit_code=exit_code)

    def test_launcher_publishes_after_an_independently_observed_exit(self):
        io = self.unused()
        observer, finish = lane_delivery._start_creation_round(
            self.args(), io=io, runtime_observation=lambda: "ready")
        # The frontier and the branch reservation are both observed before the
        # round is registered, never after the writer can open or push anything.
        self.assertEqual(io.effects, [("frontier", REPO), ("pulls", REPO, BRANCH), ("ref", REPO, BRANCH)])
        self.assertEqual((observer.origin.pull_frontier, observer.origin.branch), (FRONTIER, BRANCH))
        observer.started(31, 31)
        observer.finish(quiescent=True)
        io.writer_created()
        finish(self.result())
        self.assertIn("post", io.effects)
        # The discovered pull request is bound into the stopped writer's own
        # record before anything is minted, so the receipt names only it.
        written = MemoryStore.records[(observer.control.store.root, observer.control.key)]
        self.assertEqual(written["lineage_created"],
                         dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=CREATED))
        observation = self.attribution.call_args.args[0]
        self.assertEqual(observation.decision.current_writer, "claude")
        self.assertEqual(observation.chain.episodes[0].kind, "creation")
        self.assertEqual(self.attribution.call_args.args[1].lane, "claude")
        # The same pull request is named for the runner, which is the only way
        # the private record reaches the number a later round reads it under.
        self.assertEqual(self.named_creation(),
                         dict(schema="code_mower.lineageCreated.v1", repo=REPO, pr_number=PR))

    def test_a_publication_that_posted_and_then_failed_still_names_the_creation(self):
        """The strandable case: the chain is public, the round's own output is not.

        ``publish`` posts the public marker before it reads the marker back and
        reconciles labels, so a failure in either leaves the chain published on
        the created head while the successful-attribution output is never
        written. The private record is filed under the issue, and only the
        delivered number is ever looked at again -- by a fix round on the created
        pull request and by a rerun of the same issue -- so nothing could extend
        the published chain and every later round would answer
        ``lineage_head_pending``. Naming the pull request before publication
        starts is what the runner files the record on.
        """
        io = self.unused()
        # The launcher binds ``publish`` when the round starts, so the failing
        # publication has to be in place before that, not only before ``finish``.
        with patch("code_mower.builder_lineage_producer.publish",
                   side_effect=ProducerRefusal("published marker unreadable")) as publication:
            observer, finish = lane_delivery._start_creation_round(
                self.args(), io=io, runtime_observation=lambda: "ready")
            observer.started(38, 38)
            observer.finish(quiescent=True)
            io.writer_created()
            with self.assertRaises(ProducerRefusal):
                finish(self.result())
        publication.assert_called_once()
        self.attribution.assert_not_called()
        self.assertEqual(self.named_creation(),
                         dict(schema="code_mower.lineageCreated.v1", repo=REPO, pr_number=PR))

    def test_launcher_refuses_an_unfinished_or_failed_round_without_publishing(self):
        io = self.unused()
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
        io = self.unused()
        with patch("code_mower.lane_delivery.observed_creation_branch",
                   return_value=(CODEX_BRANCH, CREATED)):
            observer, finish = lane_delivery._start_creation_round(
                self.args(), io=io, runtime_observation=lambda: "ready")
            observer.started(33, 33)
            observer.finish(quiescent=True)
            io.writer_created()
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                finish(self.result())
        self.assertNotIn("post", io.effects)
        # The branch is refused at launch too, before any round is registered.
        with self.assertRaises(lane_delivery.LaneDeliveryError):
            lane_delivery._start_creation_round(self.args(lineage_branch=CODEX_BRANCH),
                                                io=self.unused(), runtime_observation=lambda: "ready")

    def test_launcher_refuses_a_pull_request_that_existed_before_launch(self):
        """End to end: an existing PR on the checked-out branch is not a creation."""
        io = self.unused()
        observer, finish = lane_delivery._start_creation_round(
            self.args(), io=io, runtime_observation=lambda: "ready")
        observer.started(34, 34)
        observer.finish(quiescent=True)
        io.writer_created(pull_payload(number=FRONTIER))
        io.target = created_target(pr_number=FRONTIER)
        with self.assertRaises(ProducerRefusal):
            finish(self.result())
        self.assertNotIn("post", io.effects)
        self.attribution.assert_not_called()

    def test_launcher_refuses_a_branch_another_writer_already_holds(self):
        """The reservation is what a concurrent writer's pull request fails.

        A pull request opened after the frontier read is numbered above it, so
        the launcher would otherwise accept a writer that checked it out and
        exited cleanly. Both the pre-launch claim and the post-exit branch are
        refused instead.
        """
        for reason, io in (("an existing pull request", FakeGitHub(refs={})),
                           ("an existing branch ref", FakeGitHub(pulls=[], refs={BRANCH: CREATED}))):
            with self.subTest(reason=reason):
                with self.assertRaises(ProducerRefusal):
                    lane_delivery._start_creation_round(self.args(), io=io,
                                                        runtime_observation=lambda: "ready")
                self.assertNotIn("post", io.effects)
        io = self.unused()
        with patch("code_mower.lane_delivery.observed_creation_branch",
                   return_value=("claude/other", CREATED)):
            observer, finish = lane_delivery._start_creation_round(
                self.args(), io=io, runtime_observation=lambda: "ready")
            observer.started(35, 35)
            observer.finish(quiescent=True)
            # Another writer's concurrent pull request, numbered above the
            # frontier and inside this lane's prefixes, on its own branch.
            io.writer_created(pull_payload(number=PR + 1, head={"ref": "claude/other", "sha": CREATED}))
            with self.assertRaises(ProducerRefusal):
                finish(self.result())
        self.assertNotIn("post", io.effects)
        self.attribution.assert_not_called()

    def test_launcher_keeps_a_bounded_no_creation_outcome_deliverable(self):
        """A clean exit that created nothing must stay a clean exit.

        The runner brokers a bounded declaration only from a provider that
        exited zero and was not killed, and applies ``needs-owner`` on the same
        evidence. Raising here because no pull request exists would take that
        exit code with it, and the unit would be reported as undelivered rather
        than carrying the explanation the provider actually wrote.
        """
        checkout = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, checkout, ignore_errors=True)
        declaration = checkout / lane_delivery.LANE_OUTCOME_FILE
        declaration.parent.mkdir(parents=True)
        declaration.write_text('{"outcome": "owner_action", "summary": "needs a credential"}',
                               encoding="utf-8")
        io = self.unused()
        observer, finish = lane_delivery._start_creation_round(
            self.args(cwd=checkout), io=io, runtime_observation=lambda: "ready")
        observer.started(36, 36)
        observer.finish(quiescent=True)
        # Nothing was created, so the reservation this round took before launch
        # still holds, and the repository is what confirms it.
        self.assertIsNone(finish(self.result()))
        self.assertEqual(io.effects[-2:], [("pulls", REPO, BRANCH), ("ref", REPO, BRANCH)])
        self.assertNotIn("post", io.effects)
        self.attribution.assert_not_called()
        self.assertIsNone(MemoryStore.records[
            (observer.control.store.root, observer.control.key)].get("lineage_created"))
        # Nothing was created, so nothing is named, and the runner has no record
        # to file: the issue keeps whatever this round left under it.
        self.assertFalse(self.created_output.exists())

    def test_launcher_still_attributes_a_creation_that_declared_otherwise(self):
        """A declaration cannot excuse a round out of attributing what it created.

        The declaration is only ever believed while the repository agrees the
        reserved branch is untouched. A writer that pushed it and opened a pull
        request delivered, whatever it wrote about itself, and the creation is
        published exactly as it is without one.
        """
        checkout = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, checkout, ignore_errors=True)
        declaration = checkout / lane_delivery.LANE_OUTCOME_FILE
        declaration.parent.mkdir(parents=True)
        declaration.write_text('{"outcome": "no_change", "summary": "nothing to change"}',
                               encoding="utf-8")
        io = self.unused()
        observer, finish = lane_delivery._start_creation_round(
            self.args(cwd=checkout), io=io, runtime_observation=lambda: "ready")
        observer.started(37, 37)
        observer.finish(quiescent=True)
        io.writer_created()
        finish(self.result())
        self.assertIn("post", io.effects)
        self.assertEqual(MemoryStore.records[(observer.control.store.root, observer.control.key)]
                         ["lineage_created"],
                         dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=CREATED))
        self.assertEqual(self.attribution.call_args.args[0].chain.episodes[0].kind, "creation")
        self.assertEqual(self.named_creation()["pr_number"], PR)

    def test_launcher_refuses_incomplete_or_contradictory_selections(self):
        for reason, changes in (
                ("no private store", dict(lineage_store=None)),
                ("no attribution output", dict(lineage_output=None)),
                ("no discovered pull request output", dict(lineage_created=None)),
                ("no supervised writer", dict(lineage_writer=None)),
                ("no reserved branch", dict(lineage_branch=None)),
                ("existing pull request target", dict(lineage_before=Path("/before.json"))),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(lane_delivery.LaneDeliveryError):
                    lane_delivery._start_creation_round(self.args(**changes), io=self.unused(),
                                                        runtime_observation=lambda: "ready")
        # The mirror image: a delivery to an existing pull request reserves no
        # branch and creates none to name, and either selection is refused
        # before that round reads anything at all.
        for reason, changes in (("a reserved branch", dict(lineage_branch=BRANCH)),
                                ("a created pull request to name",
                                 dict(lineage_created=self.created_output))):
            with self.subTest(existing_pull_request=reason):
                with self.assertRaises(lane_delivery.LaneDeliveryError):
                    lane_delivery._start_lineage_round(
                        self.args(**(dict(lineage_branch=None, lineage_created=None) | changes)),
                        io=self.unused(), runtime_observation=lambda: "ready")


class CreationFrontierTransportTests(unittest.TestCase):
    """The real pre-launch frontier read, against realistic pull request payloads.

    Every creation round begins with this request, so its cost is the cost of
    launching at all: the transport decodes the whole response under a fixed
    byte budget, and a repository large enough to fill a page of complete pull
    request objects would otherwise refuse every round before the writer starts.
    """

    def fake_gh(self, sizes):
        """A ``gh api`` stand-in returning ``per_page`` complete pull requests."""
        def run(argv, **kwargs):
            endpoint = argv[2]
            requested = int(endpoint.split("per_page=")[1].split("&")[0])
            sizes.append((endpoint, requested))
            # Numbered newest-created-first, exactly as the sorted endpoint reports.
            payload = [dict(pull_payload(number=PR - offset),
                            body="x" * 4096, title="t" * 256, labels=[])
                       for offset in range(requested)]
            return argparse.Namespace(stdout=json.dumps(payload))
        return run

    def responding(self, body):
        """A ``gh api`` stand-in returning one fixed response text."""
        return lambda *args, **kwargs: argparse.Namespace(stdout=body)

    def test_the_frontier_read_stays_inside_the_transport_byte_budget(self):
        sizes = []
        with patch("code_mower.builder_lineage_producer.subprocess.run", self.fake_gh(sizes)):
            self.assertEqual(GitHub().pull_frontier(REPO), PR)
        (endpoint, requested), = sizes
        self.assertEqual(requested, 1, f"frontier read must request one pull request: {endpoint}")
        # The same payload at a full page is what the decoder refuses, so the
        # bound above is load-bearing rather than merely tidier.
        oversized = json.dumps([dict(pull_payload(number=PR - offset), body="x" * 4096,
                                     title="t" * 256, labels=[]) for offset in range(100)])
        self.assertGreater(len(oversized.encode("utf-8")), MAX_STATE_BYTES)
        with self.assertRaises(ContextError):
            decode_transport(oversized)

    def test_the_frontier_refuses_an_unreadable_or_overlong_response(self):
        for reason, payload in (
                ("not a list", json.dumps(pull_payload())),
                ("unnumbered entry", json.dumps([{"state": "open"}])),
                ("non-integer number", json.dumps([{"number": "1021"}])),
                ("more than the one requested", json.dumps([{"number": PR}, {"number": PR - 1}])),
        ):
            with self.subTest(reason=reason):
                with patch("code_mower.builder_lineage_producer.subprocess.run",
                           self.responding(payload)):
                    with self.assertRaises(ProducerRefusal):
                        GitHub().pull_frontier(REPO)

    def test_a_repository_with_no_pull_requests_has_a_zero_frontier(self):
        with patch("code_mower.builder_lineage_producer.subprocess.run", self.responding("[]")):
            self.assertEqual(GitHub().pull_frontier(REPO), 0)


if __name__ == "__main__":
    unittest.main()
