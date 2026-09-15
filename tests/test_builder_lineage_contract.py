"""Exact-head builder contribution lineage: the owning contract regressions.

The primary fixture is the shape that produced the original defect: a pull
request opened by Devin on a Devin branch, an explicit verified Codex takeover,
and a Codex final head. Every signal that used to be consulted on its own --
the opener, the active label, the branch prefix -- names a different lane here.

These cases run on explicit plain inputs with no environment, store, network or
adapter involvement. That is not an accident of how they are written; it is the
property the contract is being accepted for.
"""

from __future__ import annotations

import dataclasses
import inspect
import unittest

from code_mower.builder_lineage import (
    MAX_EPISODE_ARRIVALS,
    MAX_EPISODES,
    ContributionEpisode,
    IdentityConflictError,
    LineageError,
    branch_lane_from_identity,
    builder_label_for,
    builder_label_plan,
    canonical_identity,
    continuation_episode,
    episode_from_handoff,
    episode_from_mapping,
    lanes_from_identity,
    pr_key,
    require_exact_target,
    resolve_builder_lineage,
    resolve_configured_identity,
    resolve_identity_only,
    resolve_lineage,
)

from lineage_contract_fixtures import (
    BRANCH,
    IDENTITY,
    MOVED,
    OPENED,
    PR,
    REPO,
    TAKEN,
    UNCONFIGURED,
    chain,
    continuation,
    head,
    takeover,
    variant,
)


def resolve(**overrides):
    kwargs = dict(
        repo=REPO,
        pr_number=PR,
        branch=BRANCH,
        head_sha=TAKEN,
        episodes=(takeover(),),
        opener_lane="devin",
        label_lanes=("codex",),
    )
    kwargs.update(overrides)
    return resolve_lineage(**kwargs)


class ExactHeadBindingTests(unittest.TestCase):
    """Repository, pull request, branch and head bind an episode immutably."""

    def test_a_verified_takeover_names_both_contributors_and_one_writer(self):
        lineage = resolve()
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.contributors, ("devin", "codex"))
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.builder_label, "builder:codex")
        self.assertEqual(lineage.stale_builder_labels, ())
        self.assertFalse(lineage.independent("devin"))
        self.assertFalse(lineage.independent("codex"))
        self.assertTrue(lineage.independent("claude"))
        self.assertEqual(
            lineage.independent_lanes(("devin", "codex", "claude")), ("claude",)
        )

    def test_a_stale_label_is_reported_rather_than_treated_as_a_conflict(self):
        lineage = resolve(label_lanes=("devin",))
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.stale_builder_labels, ("devin",))

    def test_a_label_for_an_uninvolved_lane_fails_closed(self):
        lineage = resolve(label_lanes=("claude",))
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "label_outside_lineage")
        self.assertTrue(lineage.owner_action)
        self.assertFalse(lineage.independent("claude"))

    def test_an_opener_outside_the_lineage_fails_closed(self):
        self.assertEqual(
            resolve(opener_lane="claude", label_lanes=()).reason,
            "opener_outside_lineage",
        )

    def test_evidence_bound_elsewhere_is_not_evidence_about_this_pull_request(self):
        for overrides in (
            {"repo": "other/repo"},
            {"pr_number": 960},
            {"branch": "codex/959-other"},
        ):
            with self.subTest(**overrides):
                lineage = resolve(episodes=(takeover(**overrides),))
                self.assertEqual(lineage.status, "conflict")
                self.assertEqual(lineage.reason, "episode_unbound")

    def test_lineage_short_of_the_current_head_waits_rather_than_guessing(self):
        lineage = resolve(head_sha=MOVED)
        self.assertEqual(lineage.status, "waiting")
        self.assertEqual(lineage.reason, "lineage_behind_head")
        self.assertEqual(lineage.contributors, ())
        self.assertEqual(lineage.current_writer, "")
        self.assertFalse(lineage.independent("claude"))

    def test_an_abbreviated_or_missing_head_is_never_resolved(self):
        for head_sha in ("", "abc1234", TAKEN[:39], TAKEN.upper() + "0"):
            with self.subTest(head_sha=head_sha):
                lineage = resolve(head_sha=head_sha)
                self.assertEqual(lineage.status, "conflict")
                self.assertEqual(lineage.reason, "target_invalid")

    def test_a_destination_that_never_moved_the_head_is_writer_not_contributor(self):
        lineage = resolve(
            episodes=(takeover(resulting_head=OPENED),),
            head_sha=OPENED,
            label_lanes=("codex",),
        )
        self.assertEqual(lineage.contributors, ("devin",))
        self.assertEqual(lineage.current_writer, "codex")
        self.assertTrue(lineage.independent("claude"))
        self.assertFalse(lineage.independent("devin"))

    def test_a_second_takeover_preserves_the_whole_ordered_history(self):
        lineage = resolve(
            episodes=(
                takeover(),
                takeover(
                    sequence=2,
                    source_lane="codex",
                    destination_lane="claude",
                    expected_head=TAKEN,
                    resulting_head=MOVED,
                ),
            ),
            head_sha=MOVED,
            label_lanes=("claude",),
        )
        self.assertEqual(lineage.contributors, ("devin", "codex", "claude"))
        self.assertEqual(lineage.current_writer, "claude")
        self.assertEqual(lineage.independent_lanes(("devin", "codex", "claude")), ())


class EpisodeShapeTests(unittest.TestCase):
    """A handoff and a continuation cannot be forged into one another."""

    def test_construction_rejects_self_handoffs_and_malformed_fields(self):
        for overrides in (
            {"destination_lane": "devin"},
            {"expected_head": "zz"},
            {"sequence": 0},
            {"sequence": MAX_EPISODES + 1},
            {"sequence": True},
            {"writer_state": "running"},
            {"kind": "something-else"},
            {"repo": "no-slash"},
            {"pr_number": 0},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(LineageError):
                    takeover(**overrides)

    def test_the_constructor_rejects_every_unusable_pull_request_number(self):
        """`0` and `False` equal the normalizer's "unusable" sentinel.

        Checking the field by round-tripping it through that normalizer and
        comparing accepted exactly the two values it uses to say "no", so an
        episode could bind itself to a pull request that cannot exist. The
        constructor asks whether the value *is* a pull request number instead.
        """

        for pr_number in (0, False, True, -1, -959, 2**31, 2**63):
            with self.subTest(pr_number=pr_number):
                with self.assertRaises(LineageError):
                    takeover(pr_number=pr_number)

    def test_the_constructor_takes_an_integer_and_does_not_parse_one(self):
        """Parsing is `episode_from_mapping`'s job, done before construction."""

        for pr_number in ("959", " 959 ", 959.0, None, [959], {"n": 959}):
            with self.subTest(pr_number=pr_number):
                with self.assertRaises(LineageError):
                    takeover(pr_number=pr_number)
        self.assertEqual(episode_from_mapping(takeover().as_dict()).pr_number, PR)

    def test_valid_pull_request_numbers_at_both_bounds_construct(self):
        for pr_number in (1, 2, PR, 2**31 - 1):
            with self.subTest(pr_number=pr_number):
                self.assertEqual(takeover(pr_number=pr_number).pr_number, pr_number)

    def test_a_zero_pull_request_number_is_malformed_wherever_it_arrives(self):
        payload = takeover().as_dict()
        payload["pr_number"] = 0
        with self.assertRaises(LineageError):
            episode_from_mapping(payload)
        self.assertEqual(
            resolve(episodes=(payload,), label_lanes=()).reason, "episode_malformed"
        )

    def test_a_continuation_must_be_same_lane_and_self_quiescent(self):
        good = continuation(sequence=2, expected=TAKEN, resulting=MOVED)
        self.assertEqual(good.source_lane, good.destination_lane)
        for overrides in (
            {"destination_lane": "claude"},
            {"writer_state": "terminated"},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(LineageError):
                    variant(good, **overrides)

    def test_a_handoff_may_not_carry_the_continuation_writer_state(self):
        with self.assertRaises(LineageError):
            takeover(writer_state="self_quiescent")

    def test_only_the_recorded_current_writer_may_continue_the_lineage(self):
        tip = takeover()
        self.assertEqual(
            continuation_episode(tip, lane="codex", resulting_head=MOVED).sequence, 2
        )
        for lane in ("devin", "claude", "", "not a lane"):
            with self.subTest(lane=lane):
                with self.assertRaises(LineageError):
                    continuation_episode(tip, lane=lane, resulting_head=MOVED)

    def test_a_continuation_must_move_the_head_and_stay_on_the_branch(self):
        tip = takeover()
        with self.assertRaises(LineageError):
            continuation_episode(tip, lane="codex", resulting_head=tip.resulting_head)
        with self.assertRaises(LineageError):
            continuation_episode(
                tip, lane="codex", resulting_head=MOVED, branch="codex/elsewhere"
            )

    def test_episode_parsing_is_strict_about_its_field_set(self):
        payload = takeover().as_dict()
        self.assertEqual(episode_from_mapping(payload), takeover())
        for mutate in (
            lambda item: item.pop("kind"),
            lambda item: item.update({"extra": 1}),
            lambda item: item.update({"schema": "something.else"}),
        ):
            with self.subTest(mutate=mutate):
                broken = takeover().as_dict()
                mutate(broken)
                with self.assertRaises(LineageError):
                    episode_from_mapping(broken)

    def test_an_episode_is_built_from_a_plain_handoff_record(self):
        record = {
            "target_pr": f"{REPO}#{PR}",
            "target_branch": BRANCH,
            "source_lane": "devin",
            "destination_lane": "codex",
            "expected_head": OPENED,
        }
        episode = episode_from_handoff(
            record, resulting_head=TAKEN, writer_state="terminated", sequence=1
        )
        self.assertEqual(episode, takeover())
        with self.assertRaises(LineageError):
            episode_from_handoff(
                record,
                resulting_head=TAKEN,
                writer_state="terminated",
                sequence=1,
                repo="other/repo",
            )
        with self.assertRaises(LineageError):
            episode_from_handoff(
                {"target_pr": "no-hash"},
                resulting_head=TAKEN,
                writer_state="terminated",
                sequence=1,
            )


class BoundedReplayTests(unittest.TestCase):
    """Republishing one chain must not look like a malformed lineage.

    The producer publishes the whole chain on every round and a reader merges
    that with whatever private record it holds, so a lineage that runs to its
    documented full length arrives as ``1 + 2 + ... + 32`` entries plus the
    completed chain once more. Counting arrivals against the *lineage* bound
    calls an authorised replay malformed -- and refuses it before deduplication,
    the only step that could have shown those arrivals to be one chain.
    """

    def test_an_identical_replay_collapses_instead_of_duplicating(self):
        lineage = resolve(episodes=(takeover(), takeover(), takeover()))
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.episodes, 1)
        self.assertEqual(lineage.contributors, ("devin", "codex"))

    def test_eight_snapshots_of_an_eight_episode_chain_still_resolve(self):
        links = chain(8)
        arrivals = tuple(episode for _ in range(8) for episode in links)
        self.assertGreater(len(arrivals), MAX_EPISODES)
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=arrivals,
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.episodes, 8)
        self.assertEqual(lineage.current_writer, "codex")

    def test_the_documented_arrival_maximum_is_the_cumulative_total(self):
        self.assertEqual(MAX_EPISODES, 32)
        self.assertEqual(MAX_EPISODES * (MAX_EPISODES + 1) // 2, 528)
        self.assertEqual(MAX_EPISODE_ARRIVALS, 560)

    def test_every_one_of_the_thirty_two_cumulative_snapshots_resolves(self):
        links = chain(MAX_EPISODES)
        cumulative = tuple(
            episode for length in range(1, len(links) + 1) for episode in links[:length]
        )
        self.assertEqual(len(cumulative), 528)
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=cumulative,
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.episodes, MAX_EPISODES)
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.contributors, ("devin", "codex"))

    def test_the_public_and_private_union_resolves_exactly_at_the_bound(self):
        links = chain(MAX_EPISODES)
        cumulative = tuple(
            episode for length in range(1, len(links) + 1) for episode in links[:length]
        )
        arrivals = cumulative + links
        self.assertEqual(len(arrivals), MAX_EPISODE_ARRIVALS)
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=arrivals,
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.episodes, MAX_EPISODES)

    def test_one_arrival_past_the_contract_is_refused_without_being_walked(self):
        links = chain(MAX_EPISODES)
        cumulative = tuple(
            episode for length in range(1, len(links) + 1) for episode in links[:length]
        )
        arrivals = cumulative + links + (links[-1],)
        self.assertEqual(len(arrivals), MAX_EPISODE_ARRIVALS + 1)
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=arrivals,
        )
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "episode_malformed")

    def test_a_sequence_past_the_lineage_bound_never_constructs(self):
        links = chain(MAX_EPISODES)
        with self.assertRaises(LineageError):
            variant(links[-1], sequence=MAX_EPISODES + 1)

    def test_a_disagreeing_duplicate_inside_a_full_history_fails_closed(self):
        links = chain(MAX_EPISODES)
        cumulative = tuple(
            episode for length in range(1, len(links) + 1) for episode in links[:length]
        )
        forged = variant(links[4], destination_lane="claude", source_lane="claude")
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=links[-1].resulting_head,
            episodes=cumulative + (forged,),
        )
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "episode_duplicated")

    def test_a_stale_full_history_waits_and_a_rebound_one_conflicts(self):
        links = chain(MAX_EPISODES)
        waiting = resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=MOVED, episodes=links
        )
        self.assertEqual(waiting.reason, "lineage_behind_head")
        unbound = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch="codex/959-other",
            head_sha=links[-1].resulting_head,
            episodes=links,
        )
        self.assertEqual(unbound.reason, "episode_unbound")


class ConsistentFailureTests(unittest.TestCase):
    """Duplicated, unchained, unbound or unverified evidence fails closed."""

    def test_reordered_gapped_and_unchained_episodes_fail_closed(self):
        broken = resolve(
            episodes=(
                takeover(),
                takeover(
                    sequence=2,
                    source_lane="codex",
                    destination_lane="claude",
                    expected_head=MOVED,
                    resulting_head=head(9),
                ),
            ),
            head_sha=head(9),
            label_lanes=(),
        )
        self.assertEqual(broken.reason, "episode_unchained")

        gap = resolve(episodes=(takeover(sequence=2),), label_lanes=("codex",))
        self.assertEqual(gap.reason, "episode_unchained")

    def test_a_lineage_that_begins_with_a_continuation_is_not_trustworthy(self):
        lone = continuation(sequence=1, expected=OPENED, resulting=TAKEN)
        lineage = resolve(episodes=(lone,), label_lanes=())
        self.assertEqual(lineage.reason, "episode_unchained")

    def test_two_episodes_claiming_one_position_fail_closed(self):
        duplicated = resolve(
            episodes=(takeover(), takeover(destination_lane="claude")), label_lanes=()
        )
        self.assertEqual(duplicated.reason, "episode_duplicated")

    def test_malformed_or_unverified_writer_state_fails_closed(self):
        self.assertEqual(resolve(episodes=({"schema": "nope"},)).reason, "episode_malformed")
        raw = takeover().as_dict()
        raw["writer_state"] = "unknown"
        self.assertIn(
            resolve(episodes=(raw,)).reason,
            {"episode_malformed", "writer_state_unverified"},
        )

    def test_every_unresolved_result_carries_one_owner_action(self):
        for lineage in (
            resolve(head_sha=MOVED),
            resolve(label_lanes=("claude",)),
            resolve(episodes=({"schema": "nope"},)),
            resolve(episodes=(), label_lanes=("codex",)),
        ):
            with self.subTest(reason=lineage.reason):
                self.assertNotEqual(lineage.status, "resolved")
                self.assertTrue(lineage.owner_action)
                self.assertEqual(lineage.current_writer, "")
                self.assertEqual(lineage.contributors, ())


class IdentityOnlyTests(unittest.TestCase):
    """The ordinary no-evidence case, and the disagreements it must refuse."""

    def test_the_ordinary_single_builder_case_still_resolves(self):
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch="claude/963-lineage",
            head_sha=TAKEN,
            opener_lane="claude",
            label_lanes=("claude",),
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.reason, "single_builder")
        self.assertEqual(lineage.contributors, ("claude",))
        self.assertFalse(lineage.independent("claude"))
        self.assertTrue(lineage.independent("codex"))

    def test_a_pull_request_with_no_builder_identity_excludes_nobody(self):
        lineage = resolve_lineage(
            repo=REPO, pr_number=PR, branch="fix/typo", head_sha=TAKEN
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.reason, "no_builder_identity")
        self.assertEqual(lineage.contributors, ())
        self.assertTrue(lineage.independent("codex"))

    def test_a_conflicting_author_and_label_without_a_handoff_fails_closed(self):
        lineage = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            opener_lane="devin",
            label_lanes=("codex",),
        )
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "conflicting_builder_identity")
        self.assertIn("record the handoff", lineage.owner_action)

    def test_identity_only_resolution_needs_no_head(self):
        self.assertEqual(
            resolve_identity_only(opener_lane="claude", label_lanes=("claude",)).current_writer,
            "claude",
        )
        self.assertEqual(
            resolve_identity_only(opener_lane="devin", label_lanes=("codex",)).status,
            "conflict",
        )

    def test_identity_maps_labels_and_authors_onto_lanes(self):
        opener, labels = lanes_from_identity(
            identity=IDENTITY,
            labels=["builder:codex", "tier:R"],
            author="devin-ai-integration[bot]",
        )
        self.assertEqual(opener, "devin")
        self.assertEqual(labels, ("codex",))
        self.assertEqual(
            lanes_from_identity(
                identity={"enabled": False},
                labels=["builder:codex"],
                author="devin-ai-integration[bot]",
            ),
            ("", ()),
        )

    def test_the_branch_prefix_is_read_only_with_verified_lineage_configured(self):
        self.assertEqual(
            branch_lane_from_identity(identity=IDENTITY, branch="codex/topic"), "codex"
        )
        self.assertEqual(
            branch_lane_from_identity(identity=IDENTITY, branch="feature/cx-topic"),
            "codex",
        )
        self.assertEqual(
            branch_lane_from_identity(identity=UNCONFIGURED, branch="codex/topic"), ""
        )
        self.assertEqual(
            branch_lane_from_identity(identity=IDENTITY, branch="fix/typo"), ""
        )

    def test_the_longest_configured_prefix_wins(self):
        identity = dict(IDENTITY)
        identity["branch_prefixes"] = {"codex-": "claude", "codex-review/": "codex"}
        self.assertEqual(
            branch_lane_from_identity(identity=identity, branch="codex-review/topic"),
            "codex",
        )


class CanonicalIdentityTests(unittest.TestCase):
    """One normalization, consumed by every reader of the contract."""

    def aliased(self, prefixes):
        identity = dict(IDENTITY)
        identity["branch_prefixes"] = dict(prefixes)
        return identity

    def test_canonicalizing_is_idempotent(self):
        once = canonical_identity(IDENTITY)
        self.assertEqual(canonical_identity(once), once)

    def test_it_leaves_every_other_configured_field_alone(self):
        contract = canonical_identity(IDENTITY)
        self.assertTrue(contract["require_verified_lineage"])
        self.assertEqual(contract["labels"], IDENTITY["labels"])
        self.assertEqual(canonical_identity(None), {"labels": {}, "authors": {}, "branch_prefixes": {}})

    def test_padded_and_cased_keys_collapse_without_losing_the_value(self):
        """Trimming the key used to strand the lookup on the untrimmed map."""

        for prefixes in (
            {"  Codex/  ": "codex"},
            {"CODEX/": "codex"},
            {"codex/": "codex", " CODEX/ ": "codex"},
        ):
            with self.subTest(prefixes=prefixes):
                self.assertEqual(
                    branch_lane_from_identity(
                        identity=self.aliased(prefixes), branch="codex/topic"
                    ),
                    "codex",
                )

    def test_a_padded_account_alias_still_names_its_lane(self):
        identity = dict(IDENTITY)
        identity["authors"] = {"  ChatGPT-Codex-Connector[Bot] ": "codex"}
        opener, _ = lanes_from_identity(
            identity=identity, labels=[], author="chatgpt-codex-connector[bot]"
        )
        self.assertEqual(opener, "codex")

    def test_conflicting_branch_prefix_aliases_refuse_in_either_order(self):
        for prefixes in (
            {"Codex/": "claude", "codex/": "codex"},
            {"codex/": "codex", "CODEX/": "claude"},
            {" codex/ ": "devin", "codex/": "codex"},
        ):
            with self.subTest(prefixes=prefixes):
                with self.assertRaises(IdentityConflictError):
                    branch_lane_from_identity(
                        identity=self.aliased(prefixes), branch="codex/topic"
                    )

    def test_the_longest_prefix_is_chosen_among_canonical_keys(self):
        identity = self.aliased({"  Codex-  ": "claude", "CODEX-REVIEW/": "codex"})
        self.assertEqual(
            branch_lane_from_identity(identity=identity, branch="codex-review/topic"),
            "codex",
        )

    def test_the_resolver_and_the_reviewer_floor_agree_on_one_representation(self):
        from code_mower.lineage_identity import identity_with_lane_floor

        identity = dict(IDENTITY)
        identity["authors"] = {"Codex[Bot]": "codex"}
        floored = identity_with_lane_floor(identity, "codex")
        self.assertEqual(floored["authors"]["codex[bot]"], "codex")
        self.assertEqual(canonical_identity(floored)["authors"], floored["authors"])
        self.assertEqual(
            branch_lane_from_identity(identity=floored, branch="codex/topic"), "codex"
        )


class OneSharedDecisionTests(unittest.TestCase):
    """The complete identity-plus-branch-plus-evidence decision, in one place.

    Every consumer has to reach the same answer from the same inputs, including
    when there are no episodes at all. Treating the empty-episode case as a
    separate, easier question is how a configured branch/label disagreement came
    back ``resolved`` and let a lane review its own diff.
    """

    def compose(self, **overrides):
        kwargs = dict(
            identity=IDENTITY,
            labels=["builder:claude"],
            author="a-human",
            repo=REPO,
            pr_number=PR,
            branch="codex/topic",
            head_sha=TAKEN,
            episodes=(),
        )
        kwargs.update(overrides)
        return resolve_builder_lineage(**kwargs)

    def test_a_configured_branch_label_disagreement_is_unresolved_with_no_episodes(self):
        lineage = self.compose()
        self.assertEqual(lineage.episodes, 0)
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "conflicting_builder_identity")
        self.assertFalse(lineage.independent("codex"))
        self.assertFalse(lineage.independent("claude"))

    def test_a_custom_configured_prefix_conflicts_the_same_way(self):
        self.assertEqual(self.compose(branch="feature/cx-topic").status, "conflict")

    def test_a_matched_branch_and_label_stay_ordinary(self):
        lineage = self.compose(branch="claude/topic")
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "claude")
        self.assertTrue(lineage.independent("codex"))

    def test_a_deployment_that_configured_no_branch_contract_keeps_its_answer(self):
        lineage = self.compose(identity=UNCONFIGURED)
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "claude")

    def test_a_pull_request_with_no_contract_at_all_excludes_nobody(self):
        lineage = self.compose(identity={"enabled": False}, labels=[], branch="fix/typo")
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.reason, "no_builder_identity")

    def test_the_branch_never_grants_takeover_authority(self):
        """Only a verified episode moves the writer; the branch can only refuse."""

        lineage = self.compose(
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
            branch=BRANCH,
            episodes=(takeover(),),
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.contributors, ("devin", "codex"))

    def test_an_incomplete_target_refuses_instead_of_falling_back(self):
        """A missing field is not an absent pull request.

        Downgrading to the identity-only answer skipped branch binding
        altogether, so evidence recorded against another branch resolved as
        evidence about this one.
        """

        for missing in (
            {"head_sha": ""},
            {"head_sha": TAKEN[:39]},
            {"repo": ""},
            {"repo": "no-slash"},
            {"pr_number": 0},
            {"pr_number": False},
            {"pr_number": "959"},
            {"branch": ""},
            {"branch": "   "},
        ):
            with self.subTest(**missing):
                lineage = self.compose(**missing)
                self.assertEqual(lineage.status, "conflict")
                self.assertEqual(lineage.reason, "target_invalid")
                self.assertTrue(lineage.owner_action)

    def test_an_incomplete_target_refuses_even_with_valid_evidence(self):
        for missing in ({"branch": ""}, {"repo": ""}, {"head_sha": ""}):
            overrides = dict(
                labels=["builder:codex"],
                author="devin-ai-integration[bot]",
                branch=BRANCH,
                episodes=(takeover(),),
            )
            overrides.update(missing)
            with self.subTest(**missing):
                self.assertEqual(self.compose(**overrides).reason, "target_invalid")

    def test_evidence_must_bind_to_the_branch_under_decision(self):
        """Branch binding is unconditional now that the branch is required."""

        lineage = self.compose(
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
            branch="codex/959-other",
            episodes=(takeover(),),
        )
        self.assertEqual(lineage.reason, "episode_unbound")

    def test_the_identity_only_route_is_selected_and_takes_no_evidence(self):
        ordinary = resolve_configured_identity(
            identity=IDENTITY, labels=["builder:claude"], author="a-human"
        )
        self.assertEqual(ordinary.status, "resolved")
        self.assertEqual(ordinary.current_writer, "claude")

        conflicting = resolve_configured_identity(
            identity=IDENTITY,
            labels=["builder:claude"],
            author="a-human",
            branch="codex/topic",
        )
        self.assertEqual(conflicting.reason, "conflicting_builder_identity")

        self.assertNotIn(
            "episodes", inspect.signature(resolve_configured_identity).parameters
        )

    def test_the_shared_target_contract_is_the_only_gate(self):
        target = require_exact_target(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN
        )
        self.assertEqual(
            (target.repo, target.pr_number, target.branch, target.head_sha),
            (REPO, PR, BRANCH, TAKEN),
        )
        for missing in (
            {"repo": ""},
            {"pr_number": 0},
            {"branch": ""},
            {"head_sha": ""},
        ):
            kwargs = dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN)
            kwargs.update(missing)
            with self.subTest(**missing):
                with self.assertRaises(LineageError):
                    require_exact_target(**kwargs)

    def test_the_target_number_must_already_be_a_number(self):
        """A string that happens to parse is data nobody checked.

        Coercing here would make `"959"` a valid exact target. The coercive
        parser belongs at the raw-payload boundary, not at the contract a
        caller reaches holding data it claims to have verified.
        """

        for pr_number in ("959", " 959 ", "0959", 959.0, True, False, "abc", None):
            with self.subTest(pr_number=pr_number):
                with self.assertRaises(LineageError):
                    require_exact_target(
                        repo=REPO, pr_number=pr_number, branch=BRANCH, head_sha=TAKEN
                    )
                self.assertEqual(
                    self.compose(
                        pr_number=pr_number,
                        labels=["builder:codex"],
                        author="devin-ai-integration[bot]",
                        branch=BRANCH,
                        episodes=(takeover(),),
                    ).reason,
                    "target_invalid",
                )
        target = require_exact_target(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN
        )
        self.assertIsInstance(target.pr_number, int)
        self.assertEqual(target.pr_number, PR)

    def test_the_raw_payload_parser_still_coerces_its_own_input(self):
        """Compatibility at the boundary that legitimately reads text."""

        payload = takeover().as_dict()
        payload["pr_number"] = str(PR)
        self.assertEqual(episode_from_mapping(payload).pr_number, PR)


class ProjectionTests(unittest.TestCase):
    """Bounded metadata only, and a label plan that never guesses."""

    def test_the_projection_is_metadata_only(self):
        payload = resolve().as_dict()
        self.assertEqual(
            set(payload),
            {
                "schema",
                "status",
                "reason",
                "head_sha",
                "contributors",
                "current_writer",
                "builder_label",
                "stale_builder_labels",
                "evidence",
                "episodes",
                "owner_action",
            },
        )
        self.assertEqual(payload["contributors"], ["devin", "codex"])

    def test_admission_is_closed_and_names_an_owner_action_on_refusal(self):
        lineage = resolve()
        for lane, admitted in (("devin", False), ("codex", False), ("claude", True)):
            with self.subTest(lane=lane):
                decision = lineage.admission(lane)
                self.assertEqual(decision["admitted"], admitted)
                self.assertEqual(decision["current_writer"], "codex")
                if not admitted:
                    self.assertEqual(decision["reason"], "contributor_not_independent")
                    self.assertTrue(decision["owner_action"])
        self.assertEqual(lineage.admission("")["reason"], "reviewer_lane_invalid")
        self.assertEqual(
            resolve(head_sha=MOVED).admission("claude")["reason"], "lineage_waiting"
        )

    def test_a_resolved_takeover_plans_exactly_one_active_label(self):
        plan = builder_label_plan(
            resolve(), current_labels=["builder:devin", "tier:R"], identity=IDENTITY
        )
        self.assertEqual(plan["status"], "reconcile")
        self.assertEqual(plan["add"], ["builder:codex"])
        self.assertEqual(plan["remove"], ["builder:devin"])

    def test_an_already_correct_label_set_plans_no_mutation(self):
        plan = builder_label_plan(
            resolve(), current_labels=["builder:codex"], identity=IDENTITY
        )
        self.assertEqual(plan["status"], "current")
        self.assertEqual((plan["add"], plan["remove"]), ([], []))

    def test_unresolved_lineage_plans_no_mutation_at_all(self):
        for lineage in (resolve(head_sha=MOVED), resolve(label_lanes=("claude",))):
            with self.subTest(reason=lineage.reason):
                plan = builder_label_plan(
                    lineage, current_labels=["builder:devin"], identity=IDENTITY
                )
                self.assertEqual(plan["status"], "blocked")
                self.assertEqual((plan["add"], plan["remove"]), ([], []))
                self.assertTrue(plan["owner_action"])

    def test_the_active_label_follows_the_configured_mapping(self):
        self.assertEqual(builder_label_for("codex", IDENTITY), "builder:codex")
        self.assertEqual(builder_label_for("codex", None), "builder:codex")
        self.assertEqual(builder_label_for("", IDENTITY), "")

    def test_the_record_key_is_stable_and_opaque(self):
        key = pr_key(REPO, PR)
        self.assertEqual(key, pr_key(REPO.upper(), str(PR)))
        self.assertNotEqual(key, pr_key(REPO, PR + 1))
        self.assertEqual(len(key), 63)
        self.assertNotIn("/", key)


class PurityTests(unittest.TestCase):
    """The contract is a function of its arguments and nothing else."""

    def test_resolution_is_deterministic_and_mutates_no_input(self):
        episodes = [takeover().as_dict()]
        labels = ["codex"]
        first = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            episodes=episodes,
            opener_lane="devin",
            label_lanes=labels,
        )
        second = resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            episodes=episodes,
            opener_lane="devin",
            label_lanes=labels,
        )
        self.assertEqual(first, second)
        self.assertEqual(episodes, [takeover().as_dict()])
        self.assertEqual(labels, ["codex"])

    def test_the_episode_and_lineage_records_are_immutable(self):
        episode = takeover()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            episode.resulting_head = MOVED  # type: ignore[misc]
        lineage = resolve()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            lineage.current_writer = "claude"  # type: ignore[misc]
        self.assertIsInstance(episode, ContributionEpisode)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
