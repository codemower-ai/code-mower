"""Behavior tests for immutable inputs, exact chains, and reviewer admission."""
from dataclasses import FrozenInstanceError
import json
import unittest

from code_mower.builder_lineage import (
    Chain, ContractError, Episode, Identity, Lineage, Target, admit,
    resolve, resolve_identity_only,
)
from minimal_lineage_fixtures import (
    BRANCH, OPENED, PR, REPO, TAKEN, BoundedArrivals, episode, episodes, head,
    identity_mapping, target,
)


class ValueContractTests(unittest.TestCase):
    def test_target_constructor_and_mapping_store_canonical_values(self):
        values = dict(repo=" OWNER/Repo ", pr_number=PR, branch=BRANCH, head_sha=f" {TAKEN.upper()} ")
        direct = Target(**values)
        self.assertEqual(direct, Target.from_mapping(values))
        self.assertEqual(direct, target())
        self.assertEqual(direct.branch, BRANCH)
        self.assertNotEqual(direct, target(branch=BRANCH.lower()))
        with self.assertRaises(FrozenInstanceError):
            direct.branch = "other"

    def test_target_missing_and_malformed_fields_reject(self):
        baseline = dict(repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN)
        cases = {"repo": [None, "", "repo", {}, False],
                 "pr_number": [None, False, True, 0, -1, 1.0, "42"],
                 "branch": [None, "", " feature/x", "a..b", "a//b", "a/", "a/.b", "a.lock"],
                 "head_sha": [None, "", "abc1234", "z" * 40, False]}
        for field, invalids in cases.items():
            for value in invalids:
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    Target(**(baseline | {field: value}))
            with self.subTest(missing=field), self.assertRaises(ContractError):
                Target.from_mapping({k: v for k, v in baseline.items() if k != field})
        for value in (None, False, [], {}, {"unknown": True}):
            with self.subTest(value=value), self.assertRaises(ContractError):
                Target.from_mapping(value)
        with self.assertRaises(ContractError):
            Target()

    def test_episode_constructor_mapping_equality_and_canonical_storage(self):
        fields = episode().to_mapping() | dict(repo=" OWNER/REPO ", source_lane=" DEVIN ",
                                               destination_lane=" CODEX ", expected_head=f" {OPENED.upper()} ",
                                               resulting_head=TAKEN.upper(), kind=" HANDOFF ",
                                               writer_state=" TERMINATED ")
        parsed = Episode.from_mapping(fields)
        self.assertEqual(parsed, Episode(**fields))
        self.assertEqual(parsed, episode())
        self.assertEqual(parsed.to_mapping()["source_lane"], "devin")
        fields["source_lane"] = "claude"
        self.assertEqual(parsed.source_lane, "devin")
        with self.assertRaises(FrozenInstanceError):
            parsed.source_lane = "claude"

    def test_invalid_episode_ingestion_has_one_error(self):
        invalid = [dict(pr_number=True), dict(pr_number=0), dict(sequence=False), dict(sequence=0),
                   dict(branch=""), dict(source_lane=""), dict(destination_lane="bad lane"),
                   dict(resulting_head="x"), dict(expected_head=None), dict(kind="invented"),
                   dict(writer_state="running"), dict(writer_state="unknown"),
                   dict(destination_lane="devin"),
                   dict(kind="continuation", writer_state="same_writer"),
                   dict(kind="continuation", source_lane="codex", writer_state="terminated")]
        for changes in invalid:
            fields = episode().to_mapping() | changes
            for factory in (lambda fields=fields: Episode(**fields), lambda fields=fields: Episode.from_mapping(fields)):
                with self.subTest(changes=changes), self.assertRaises(ContractError):
                    factory()
        for field in set(episode().to_mapping()) - {"kind"}:
            with self.subTest(missing=field), self.assertRaises(ContractError):
                Episode.from_mapping({k: v for k, v in episode().to_mapping().items() if k != field})


class IdentityTests(unittest.TestCase):
    def test_explicit_mapping_text_and_immutable_snapshot(self):
        raw = identity_mapping()
        identity = Identity(raw)
        self.assertEqual(identity, Identity.from_mapping(raw))
        self.assertEqual(identity, Identity.from_text(json.dumps(raw)))
        self.assertEqual(identity.to_mapping(), raw)
        raw["authors"]["outsider"] = "codex"
        self.assertNotIn("outsider", dict(identity.authors))
        with self.assertRaises(FrozenInstanceError):
            identity.enabled = False
        for value in (None, "", [], {"labels": None}, {"enabled": 1}, {"opaque": {}},
                      {"require_verified_lineage": "yes"}):
            with self.subTest(value=value), self.assertRaises(ContractError):
                Identity(value)
        with self.assertRaises(ContractError):
            Identity.from_text('{"enabled":true,"enabled":false}')

    def test_aliases_collapse_or_reject_in_both_insertion_orders(self):
        for section, first, second in (("authors", " BOT[bot] ", "bot[bot]"),
                                       ("branch_prefixes", " Feature/CX- ", "feature/cx-"),
                                       ("labels", " BUILDER:CODEX ", "builder:codex")):
            for reverse in (False, True):
                pairs = [(first, " CODEX "), (second, "codex")]
                if reverse:
                    pairs.reverse()
                parsed = Identity(identity_mapping(**{section: dict(pairs)}))
                self.assertEqual(getattr(parsed, section), ((second, "codex"),))
                pairs = [(first, "codex"), (second, "claude")]
                if reverse:
                    pairs.reverse()
                with self.subTest(section=section, reverse=reverse), self.assertRaises(ContractError):
                    Identity(identity_mapping(**{section: dict(pairs)}))

    def test_longest_prefix_and_exact_branch_case_are_independent(self):
        identity = Identity(identity_mapping(branch_prefixes={" feature/ ": "devin", "FEATURE/CX-": "codex"}))
        decision = resolve(Chain.from_arrivals(target(branch="Feature/CX-42"), []), identity,
                           "codex-bot[bot]", ["builder:codex"])
        self.assertEqual(decision.current_writer, "codex")
        self.assertEqual(decision.target.branch, "Feature/CX-42")
        self.assertTrue(admit(decision, "claude"))

    def test_own_reviewer_floor_uses_canonical_policy_and_preserves_fields(self):
        identity = Identity(identity_mapping(enabled=False, authors={" OWN-BOT ": " CODEX "}))
        floor = identity.with_reviewer_floor(" CODEX ", ["own-bot", " SECOND-BOT "])
        self.assertTrue(floor.enabled)
        self.assertEqual(floor.labels, identity.labels)
        self.assertEqual(floor.branch_prefixes, identity.branch_prefixes)
        self.assertEqual(floor.require_verified_lineage, identity.require_verified_lineage)
        self.assertEqual(dict(floor.authors), {"own-bot": "codex", "second-bot": "codex"})
        decision = resolve_identity_only(floor, " SECOND-BOT ", [], "Feature/CX-42")
        self.assertFalse(admit(decision, " CODEX "))
        self.assertTrue(admit(decision, " Claude "))
        for section, alias in (("authors", " OWN-BOT "), ("labels", " BUILDER:CODEX ")):
            with self.subTest(section=section), self.assertRaises(ContractError):
                Identity(identity_mapping(**{section: {alias: "claude"}})).with_reviewer_floor("codex", ["own-bot"])


class ChainResolutionTests(unittest.TestCase):
    def setUp(self):
        self.identity = Identity(identity_mapping())

    def test_no_raw_overload_no_target_fallback_and_explicit_control(self):
        for invalid in (None, [], {}, episode(), target()):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                resolve(invalid, self.identity, "devin-bot[bot]", [])
        for invalid in (None, {}, False):
            with self.assertRaises(ContractError):
                Chain.from_arrivals(invalid, [])
        with self.assertRaises(ContractError):
            Chain(target(), [])
        with self.assertRaises(ContractError):
            Lineage()
        control = resolve_identity_only(self.identity, "codex-bot[bot]", [], "codex/42-work")
        self.assertIsNone(control.target)
        self.assertEqual(control.contributors, ("codex",))
        self.assertFalse(admit(control, "codex"))
        with self.assertRaises(TypeError):
            resolve_identity_only(self.identity, "", [], BRANCH, [])
        with self.assertRaises(TypeError):
            resolve(Chain.from_arrivals(target(), []), self.identity, "", [], target())

    def test_zero_episodes_considers_branch_conflicts_and_custom_no_contract(self):
        empty = Chain.from_arrivals(target(), [])
        conflict = resolve(empty, self.identity, "codex-bot[bot]", ["builder:codex"])
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(conflict.contributors, ("codex", "devin"))
        self.assertTrue(conflict.owner_action)
        self.assertFalse(admit(conflict, "claude"))
        matched = resolve(empty, self.identity, "devin-bot[bot]", [])
        self.assertEqual(matched.reason, "identity_matched")
        self.assertTrue(admit(matched, "claude"))
        custom = resolve(Chain.from_arrivals(target(branch="feature/cx-42"), []), self.identity,
                         "codex-bot[bot]", [])
        self.assertEqual(custom.current_writer, "codex")
        no_contract = Identity(identity_mapping(require_verified_lineage=False))
        self.assertEqual(resolve(empty, no_contract, "codex-bot[bot]", []).status, "ready")
        self.assertEqual(resolve(empty, Identity({}), "", []).reason, "no_identity")

    def test_recorded_takeover_excludes_all_contributors_and_waits_on_stale_head(self):
        chain = Chain.from_arrivals(target(), [episode()])
        decision = resolve(chain, self.identity, "devin-bot[bot]", ["builder:codex"])
        self.assertEqual((decision.status, decision.reason, decision.current_writer),
                         ("ready", "verified_lineage", "codex"))
        self.assertEqual(decision.contributors, ("codex", "devin"))
        for lane in (" DEVIN ", " CODEX "):
            self.assertFalse(admit(decision, lane))
        self.assertTrue(admit(decision, " CLAUDE "))
        stale = resolve(Chain.from_arrivals(target(head_sha=OPENED), [episode()]), self.identity,
                        "devin-bot[bot]", ["builder:codex"])
        self.assertEqual((stale.status, stale.reason), ("waiting", "lineage_head_pending"))
        self.assertTrue(stale.owner_action)
        self.assertFalse(admit(stale, "claude"))
        unrecorded = resolve(chain, self.identity, "claude-bot[bot]", ["builder:codex"])
        self.assertEqual(unrecorded.status, "conflict")
        self.assertFalse(admit(unrecorded, "unrelated"))

    def test_multiple_handoffs_and_continuations_keep_every_contributor(self):
        items = [episode(), episode(sequence=2, source_lane="codex", destination_lane="claude",
                                    expected_head=TAKEN, resulting_head=head(2)),
                 episode(sequence=3, kind="continuation", source_lane="claude", destination_lane="claude",
                         writer_state="same_writer", expected_head=head(2), resulting_head=head(3))]
        decision = resolve(Chain.from_arrivals(target(head_sha=head(3)), items), self.identity,
                           "devin-bot[bot]", ["builder:claude"])
        self.assertEqual(decision.current_writer, "claude")
        self.assertEqual(decision.contributors, ("claude", "codex", "devin"))
        self.assertTrue(all(not admit(decision, lane) for lane in decision.contributors))
        self.assertTrue(admit(decision, "independent"))

    def test_common_target_contiguity_and_predecessor_are_required(self):
        valid = episodes(2)
        cases = [[episode(repo="owner/other-repo")], [episode(pr_number=PR + 1)],
                 [episode(branch=BRANCH.lower())], [valid[1]], [valid[0], episode(sequence=3)],
                 [valid[0], Episode.from_mapping(valid[1].to_mapping() | {"expected_head": OPENED})],
                 [valid[0], episode(sequence=2, source_lane="claude", expected_head=head(1))]]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(ContractError):
                Chain.from_arrivals(target(), items)
        for invalid in (None, False, 0, "", {}, target()):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                Chain.from_arrivals(target(), invalid)

    def test_arrival_budget_precedes_dedup_and_never_overconsumes(self):
        stream = BoundedArrivals(episode())
        with self.assertRaisesRegex(ContractError, "arrival budget"):
            Chain.from_arrivals(target(), stream)
        self.assertEqual(stream.reads, 561)
        accepted = Chain.from_arrivals(target(), [episode()] * 560)
        self.assertEqual(accepted.episodes, (episode(),))
        self.assertEqual(accepted.raw_arrival_count, 560)
        conflicting = episode(resulting_head=head(3))
        with self.assertRaisesRegex(ContractError, "conflicting duplicate"):
            Chain.from_arrivals(target(), [episode()] * 559 + [conflicting])
        with self.assertRaisesRegex(ContractError, "distinct episode budget"):
            Chain.from_arrivals(target(), episodes(33))

    def test_canonical_duplicates_collapse_and_replay_order_is_irrelevant(self):
        raw = episode().to_mapping() | {"source_lane": " DEVIN ", "repo": " OWNER/REPO "}
        chain = Chain.from_arrivals(target(), [raw, episode()])
        self.assertEqual(chain.episodes, (episode(),))
        self.assertEqual(chain.raw_arrival_count, 2)
        values = episodes(32)
        ordered = Chain.from_arrivals(target(head_sha=head(32)), reversed(values))
        self.assertEqual(ordered.episodes, tuple(values))
        with self.assertRaises(FrozenInstanceError):
            ordered.target = target()


if __name__ == "__main__":
    unittest.main()
