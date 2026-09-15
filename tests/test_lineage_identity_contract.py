"""Canonical reviewer identity, alias normalization and pure admission.

Reviewer independence is decided by naming lanes. A deployment whose contract
cannot name the reviewer's own lane names no contributor for it either, and the
seam then admits exactly the reviewer it exists to exclude. These cases pin the
floor that prevents that, the alias rules that keep it order-independent, and
the admission decision built on top -- all from explicit inputs, with no
environment read anywhere.
"""

from __future__ import annotations

import json
import unittest

from code_mower.builder_lineage import LineageError
from code_mower.lineage_identity import (
    AUTHOR_EXCLUSION_ENV,
    LANE_ACCOUNT_FLOOR,
    ReviewerIdentityInvalid,
    ReviewerNotIndependent,
    account_key,
    combine_evidence,
    identity_from_json,
    identity_with_lane_floor,
    load_identity,
    normalized_account_map,
    pr_lineage,
    require_independent_reviewer,
    require_reviewer_lane,
    reviewer_admission,
    trusted_published_episodes,
)

from lineage_contract_fixtures import (
    AUTHORITY,
    IDENTITY,
    MOVED,
    OUTSIDER,
    PR,
    REPO,
    TAKEN,
    UNCONFIGURED,
    chain,
    comment,
    pr_meta,
    published,
    takeover,
)


class IdentityLoadingTests(unittest.TestCase):
    """Loading is a pure parse of text the caller supplies."""

    def test_a_valid_contract_parses(self):
        self.assertEqual(identity_from_json(json.dumps(IDENTITY)), IDENTITY)

    def test_a_missing_or_unusable_contract_disables_lane_naming(self):
        for raw in (None, "", "   not json", "[1, 2]", '"text"', "7"):
            with self.subTest(raw=raw):
                self.assertEqual(identity_from_json(raw), {"enabled": False})

    def test_the_compatibility_spelling_is_the_same_function(self):
        self.assertIs(load_identity, identity_from_json)

    def test_the_contract_variable_is_named_but_never_read_here(self):
        self.assertEqual(AUTHOR_EXCLUSION_ENV, "CODE_MOWER_AUTHOR_EXCLUSION_JSON")


class OwnLaneFloorTests(unittest.TestCase):
    """A floor, not a default: the reviewer's own lane is always nameable."""

    MINIMAL = {
        "enabled": True,
        "labels": {"builder:codex": ""},
        "authors": {"codex[bot]": ""},
    }

    def test_a_blank_own_label_and_account_are_overwritten(self):
        floored = identity_with_lane_floor(self.MINIMAL, "codex")
        self.assertEqual(floored["labels"]["builder:codex"], "codex")
        self.assertEqual(floored["authors"]["codex[bot]"], "codex")

    def test_an_invalid_own_mapping_is_overwritten_not_preserved(self):
        for invalid in (None, 0, [], {}, "   "):
            with self.subTest(invalid=invalid):
                floored = identity_with_lane_floor(
                    {
                        "enabled": True,
                        "labels": {"builder:codex": invalid},
                        "authors": {"codex[bot]": invalid},
                    },
                    "codex",
                )
                self.assertEqual(floored["labels"]["builder:codex"], "codex")
                self.assertEqual(floored["authors"]["codex[bot]"], "codex")

    def test_a_missing_or_disabled_contract_still_names_the_reviewer(self):
        for identity in (None, {}, {"enabled": False, "labels": {}, "authors": {}}):
            with self.subTest(identity=identity):
                floored = identity_with_lane_floor(identity, "codex")
                self.assertTrue(floored["enabled"])
                self.assertEqual(floored["labels"]["builder:codex"], "codex")
                for login in LANE_ACCOUNT_FLOOR["codex"]:
                    self.assertEqual(floored["authors"][login], "codex")

    def test_only_the_reviewers_own_lane_is_synthesized(self):
        floored = identity_with_lane_floor(self.MINIMAL, "claude")
        self.assertEqual(floored["labels"]["builder:claude"], "claude")
        self.assertNotIn("builder:devin", floored["labels"])
        # The other lane's useless entry is left exactly as configured.
        self.assertEqual(floored["labels"]["builder:codex"], "")

    def test_a_conflicting_own_label_or_account_refuses(self):
        for identity in (
            {"enabled": True, "labels": {"builder:codex": "claude"}, "authors": {}},
            {
                "enabled": True,
                "labels": {"builder:codex": "codex"},
                "authors": {"codex[bot]": "devin"},
            },
            # Disabling the contract does not make a misnamed own lane safe.
            {"enabled": False, "labels": {"builder:codex": "claude"}, "authors": {}},
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(ReviewerIdentityInvalid) as raised:
                    identity_with_lane_floor(identity, "codex")
                self.assertIn("reviewer_identity_invalid", str(raised.exception))

    def test_the_rest_of_the_configured_contract_survives_the_floor(self):
        floored = identity_with_lane_floor(IDENTITY, "codex")
        self.assertEqual(floored["branch_prefixes"], IDENTITY["branch_prefixes"])
        self.assertTrue(floored["require_verified_lineage"])
        self.assertEqual(floored["labels"]["builder:claude"], "claude")

    def test_flooring_leaves_the_supplied_contract_untouched(self):
        original = json.loads(json.dumps(self.MINIMAL))
        identity_with_lane_floor(original, "codex")
        self.assertEqual(original, self.MINIMAL)


class AccountAliasTests(unittest.TestCase):
    """Account names match case-insensitively, so aliases are one account."""

    def _aliased(self, *pairs):
        contract = dict(IDENTITY)
        contract["authors"] = dict(pairs)
        return contract

    def test_the_key_is_normalized_the_way_resolution_reads_it(self):
        self.assertEqual(account_key("  Codex[Bot] "), "codex[bot]")
        self.assertEqual(account_key(None), "")
        self.assertEqual(
            normalized_account_map({" Codex[Bot] ": "codex"}), {"codex[bot]": "codex"}
        )
        self.assertEqual(normalized_account_map({"   ": "codex"}), {})
        self.assertEqual(normalized_account_map("not a mapping"), {})

    def test_a_conflicting_alias_refuses_in_either_insertion_order(self):
        for pairs in (
            (("Codex[Bot]", "claude"), ("codex[bot]", "codex")),
            (("codex[bot]", "codex"), ("Codex[Bot]", "claude")),
            ((" codex[bot] ", "claude"), ("codex[bot]", "codex")),
            (("CODEX[BOT]", "devin"), ("codex[bot]", "codex")),
        ):
            with self.subTest(order=pairs):
                with self.assertRaises(ReviewerIdentityInvalid):
                    identity_with_lane_floor(self._aliased(*pairs), "codex")

    def test_a_compatible_alias_is_accepted(self):
        for pairs in (
            (("Codex[Bot]", "codex"), ("codex[bot]", "codex")),
            (("codex[bot]", "codex"), ("CODEX[BOT]", "Codex")),
            ((" codex[bot] ", "codex"),),
        ):
            with self.subTest(order=pairs):
                floored = identity_with_lane_floor(self._aliased(*pairs), "codex")
                self.assertEqual(floored["authors"]["codex[bot]"], "codex")
                self.assertEqual(
                    floored["branch_prefixes"], IDENTITY["branch_prefixes"]
                )

    def test_an_alias_cannot_outrank_the_canonical_account(self):
        floored = identity_with_lane_floor(
            self._aliased(
                ("Codex[Bot]", "codex"), ("devin-ai-integration[bot]", "devin")
            ),
            "codex",
        )
        self.assertEqual(floored["authors"]["codex[bot]"], "codex")
        self.assertEqual(floored["labels"]["builder:codex"], "codex")
        self.assertEqual(floored["authors"]["devin-ai-integration[bot]"], "devin")


class PublishedEvidenceTests(unittest.TestCase):
    """Raw validation first, then trust, then parsing."""

    def test_an_unreadable_read_is_refused_before_trust_is_considered(self):
        for response in (None, False, {}, [comment(body="hi"), "text"]):
            with self.subTest(response=response):
                with self.assertRaises(LineageError):
                    trusted_published_episodes(response, authorities=(AUTHORITY,))
                with self.assertRaises(LineageError):
                    trusted_published_episodes(response, authorities=())

    def test_with_no_authorities_configured_nothing_is_read(self):
        history = [published((takeover(),))]
        self.assertEqual(trusted_published_episodes(history, authorities=()), ())

    def test_a_trusted_marker_is_read_and_an_untrusted_one_is_not(self):
        self.assertEqual(
            trusted_published_episodes(
                [published((takeover(),))], authorities=(AUTHORITY,)
            ),
            (takeover(),),
        )
        self.assertEqual(
            trusted_published_episodes(
                [published((takeover(),), author=OUTSIDER)], authorities=(AUTHORITY,)
            ),
            (),
        )

    def test_the_composer_preserves_raw_arrivals_for_the_owning_resolver(self):
        """Collapsing here would let two collapsed inputs reset the raw cap."""

        links = chain(6)
        self.assertEqual(len(combine_evidence(links, links)), 12)
        self.assertEqual(len(combine_evidence((), links)), 6)
        self.assertEqual(len(combine_evidence(links, ())), 6)
        with self.assertRaises(LineageError):
            combine_evidence((), ("not an episode",))


class ReviewerAdmissionTests(unittest.TestCase):
    """Contributors are refused; an uninvolved lane is admitted. Fails closed."""

    def admit(self, lane, **overrides):
        kwargs = dict(
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(),
            head_sha=TAKEN,
            identity=IDENTITY,
            episodes=(takeover(),),
        )
        kwargs.update(overrides)
        return reviewer_admission(lane, **kwargs)

    def test_contributors_are_refused_and_an_independent_lane_is_admitted(self):
        for lane, admitted in (("devin", False), ("codex", False), ("claude", True)):
            with self.subTest(lane=lane):
                decision = self.admit(lane)
                self.assertEqual(decision["admitted"], admitted)
                self.assertEqual(decision["current_writer"], "codex")
                self.assertEqual(decision["contributors"], ["devin", "codex"])
                if not admitted:
                    self.assertEqual(decision["reason"], "contributor_not_independent")
                    self.assertTrue(decision["owner_action"])

    def test_admission_uses_the_head_the_caller_pinned(self):
        decision = self.admit("claude", head_sha=MOVED)
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "lineage_waiting")

    def test_contradictory_signals_without_evidence_refuse_every_lane(self):
        for lane in ("devin", "codex", "claude"):
            with self.subTest(lane=lane):
                decision = self.admit(lane, episodes=())
                self.assertFalse(decision["admitted"])
                self.assertEqual(decision["reason"], "lineage_conflict")

    def test_a_configured_branch_label_disagreement_admits_nobody(self):
        disagreeing = pr_meta(
            author="a-human", labels=("builder:claude",), branch="codex/topic"
        )
        for lane in ("codex", "claude"):
            with self.subTest(lane=lane):
                decision = self.admit(lane, pr_meta=disagreeing, episodes=())
                self.assertFalse(decision["admitted"])
                self.assertEqual(decision["reason"], "lineage_conflict")

    def test_an_unconfigured_deployment_keeps_its_old_answer(self):
        disagreeing = pr_meta(
            author="a-human", labels=("builder:claude",), branch="codex/topic"
        )
        decision = self.admit(
            "codex", pr_meta=disagreeing, episodes=(), identity=UNCONFIGURED
        )
        self.assertTrue(decision["admitted"])

    def test_a_matched_branch_and_label_keep_their_intended_behaviour(self):
        matched = pr_meta(
            author="a-human", labels=("builder:claude",), branch="claude/topic"
        )
        self.assertTrue(self.admit("codex", pr_meta=matched, episodes=())["admitted"])
        refused = self.admit("claude", pr_meta=matched, episodes=())
        self.assertFalse(refused["admitted"])
        self.assertEqual(refused["reason"], "contributor_not_independent")

    def test_a_reviewer_with_no_contract_at_all_still_excludes_itself(self):
        """The floor, at the admission boundary rather than in isolation."""

        decision = reviewer_admission(
            "codex",
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(
                author="chatgpt-codex-connector[bot]",
                labels=(),
                branch="codex/topic",
            ),
            head_sha=TAKEN,
            identity={"enabled": False},
            episodes=(),
        )
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "contributor_not_independent")

    def test_unreadable_evidence_refuses_rather_than_admitting(self):
        decision = self.admit("claude", episodes=({"schema": "nope"},))
        self.assertFalse(decision["admitted"])
        self.assertIn(decision["reason"], {"lineage_conflict", "lineage_unreadable"})

    def test_an_invalid_lane_name_is_refused(self):
        decision = self.admit("")
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "reviewer_lane_invalid")

    def test_a_misnamed_own_lane_refuses_before_any_resolution(self):
        conflicting = dict(IDENTITY)
        conflicting["labels"] = dict(IDENTITY["labels"])
        conflicting["labels"]["builder:codex"] = "claude"
        with self.assertRaises(ReviewerIdentityInvalid):
            self.admit("codex", identity=conflicting)

    def test_requiring_independence_raises_bounded_metadata_only(self):
        with self.assertRaises(ReviewerNotIndependent) as raised:
            require_independent_reviewer(
                "codex",
                repo=REPO,
                pr_number=PR,
                pr_meta=pr_meta(),
                head_sha=TAKEN,
                identity=IDENTITY,
                episodes=(takeover(),),
            )
        message = str(raised.exception)
        self.assertIn("contributor_not_independent", message)
        self.assertNotIn("/Users", message)
        self.assertNotIn("session", message)

    def test_the_wrapper_facing_form_reports_a_plain_runtime_error(self):
        with self.assertRaises(RuntimeError) as raised:
            require_reviewer_lane(
                "codex",
                REPO,
                PR,
                pr_meta(),
                TAKEN,
                identity=IDENTITY,
                episodes=(takeover(),),
            )
        message = str(raised.exception)
        self.assertIn("contributor_not_independent", message)
        self.assertIn(TAKEN[:12], message)
        self.assertNotIn("/Users", message)
        self.assertNotIn("\n", message)

    def test_an_admitted_lane_is_returned_rather_than_raised(self):
        decision = require_reviewer_lane(
            "claude",
            REPO,
            PR,
            pr_meta(),
            TAKEN,
            identity=IDENTITY,
            episodes=(takeover(),),
        )
        self.assertTrue(decision["admitted"])


class PrLineageTests(unittest.TestCase):
    """Metadata shapes the caller may actually be handed."""

    def test_metadata_that_names_no_branch_refuses_the_exact_target(self):
        """Admission is an exact-target claim, so an unbound one cannot answer."""

        for meta in (
            {},
            {"user": "a-string", "head": None, "labels": None},
            {"labels": ["not-an-object"], "head": {"ref": "   "}},
        ):
            with self.subTest(meta=meta):
                lineage = pr_lineage(
                    repo=REPO,
                    pr_number=PR,
                    pr_meta=meta,
                    head_sha=TAKEN,
                    identity=IDENTITY,
                    episodes=(),
                )
                self.assertEqual(lineage.status, "conflict")
                self.assertEqual(lineage.reason, "target_invalid")

    def test_a_complete_target_with_no_named_lane_excludes_nobody(self):
        lineage = pr_lineage(
            repo=REPO,
            pr_number=PR,
            pr_meta={"labels": ["not-an-object"], "head": {"ref": "fix/typo"}},
            head_sha=TAKEN,
            identity=IDENTITY,
            episodes=(),
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.reason, "no_builder_identity")

    def test_an_unbound_reviewer_admission_admits_nobody(self):
        decision = reviewer_admission(
            "claude",
            repo=REPO,
            pr_number=PR,
            pr_meta={"user": {"login": "a-human"}, "labels": []},
            head_sha=TAKEN,
            identity=IDENTITY,
            episodes=(),
        )
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "lineage_conflict")

    def test_the_branch_comes_from_the_metadata_the_caller_fetched(self):
        lineage = pr_lineage(
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(author="a-human", labels=(), branch="codex/topic"),
            head_sha=TAKEN,
            identity=IDENTITY,
            episodes=(),
        )
        self.assertEqual(lineage.current_writer, "codex")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
