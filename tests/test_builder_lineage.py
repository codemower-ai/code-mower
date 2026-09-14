"""Exact-head builder contribution lineage regressions.

The primary fixture is the PR #959 shape: a pull request opened by Devin on a
Devin branch, an explicit verified Codex takeover, and a Codex final head.
"""

from __future__ import annotations

import unittest

from code_mower.builder_lineage import (
    ContributionEpisode,
    LineageError,
    episodes_from_comment_body,
    lanes_from_identity,
    lineage_comment_marker,
    record_episode,
    load_episodes,
    resolve_identity_only,
    resolve_lineage,
)
from code_mower.provider_runners.lineage import (
    ReviewerNotIndependent,
    require_independent_reviewer,
    reviewer_admission,
)


REPO = "codemower-ai/code-mower"
BRANCH = "devin/959-release-dogfood"
H1 = "a" * 40
H2 = "b" * 40
H3 = "c" * 40

IDENTITY = {
    "enabled": True,
    "labels": {
        "builder:devin": "devin",
        "builder:codex": "codex",
        "builder:claude": "claude",
    },
    "authors": {
        "devin-ai-integration[bot]": "devin",
        "chatgpt-codex-connector": "codex",
    },
}


def episode(**overrides):
    payload = dict(
        sequence=1,
        repo=REPO,
        pr_number=959,
        branch=BRANCH,
        source_lane="devin",
        destination_lane="codex",
        expected_head=H1,
        resulting_head=H2,
        writer_state="terminated",
    )
    payload.update(overrides)
    return ContributionEpisode(**payload)


def resolve(**overrides):
    kwargs = dict(
        repo=REPO,
        pr_number=959,
        branch=BRANCH,
        head_sha=H2,
        episodes=(episode(),),
        opener_lane="devin",
        label_lanes=("codex",),
    )
    kwargs.update(overrides)
    return resolve_lineage(**kwargs)


class TakeoverLineageTests(unittest.TestCase):
    def test_devin_opener_with_verified_codex_takeover(self):
        lineage = resolve()
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.contributors, ("devin", "codex"))
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.builder_label, "builder:codex")
        self.assertEqual(lineage.stale_builder_labels, ())
        # Both contributors are excluded from gating their own work.
        self.assertFalse(lineage.independent("devin"))
        self.assertFalse(lineage.independent("codex"))
        # An uninvolved qualified lane may review the exact final head.
        self.assertTrue(lineage.independent("claude"))
        self.assertEqual(lineage.independent_lanes(("devin", "codex", "claude")), ("claude",))

    def test_stale_builder_label_is_reported_not_treated_as_conflict(self):
        lineage = resolve(label_lanes=("devin",))
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.builder_label, "builder:codex")
        self.assertEqual(lineage.stale_builder_labels, ("devin",))

    def test_label_for_an_uninvolved_lane_fails_closed(self):
        lineage = resolve(label_lanes=("claude",))
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "label_outside_lineage")
        self.assertTrue(lineage.owner_action)
        self.assertFalse(lineage.independent("claude"))

    def test_head_change_leaves_lineage_waiting_rather_than_guessing(self):
        lineage = resolve(head_sha=H3)
        self.assertEqual(lineage.status, "waiting")
        self.assertEqual(lineage.reason, "lineage_behind_head")
        self.assertEqual(lineage.contributors, ())
        self.assertEqual(lineage.current_writer, "")
        self.assertFalse(lineage.independent("claude"))

    def test_a_destination_that_never_moved_the_head_is_writer_not_contributor(self):
        lineage = resolve(
            episodes=(episode(resulting_head=H1),), head_sha=H1, label_lanes=("codex",)
        )
        self.assertEqual(lineage.contributors, ("devin",))
        self.assertEqual(lineage.current_writer, "codex")
        self.assertTrue(lineage.independent("claude"))
        self.assertFalse(lineage.independent("devin"))

    def test_second_takeover_preserves_the_whole_ordered_history(self):
        lineage = resolve(
            episodes=(
                episode(),
                episode(
                    sequence=2,
                    source_lane="codex",
                    destination_lane="claude",
                    expected_head=H2,
                    resulting_head=H3,
                ),
            ),
            head_sha=H3,
            label_lanes=("claude",),
        )
        self.assertEqual(lineage.contributors, ("devin", "codex", "claude"))
        self.assertEqual(lineage.current_writer, "claude")
        self.assertEqual(lineage.independent_lanes(("devin", "codex", "claude")), ())


class AdversarialEvidenceTests(unittest.TestCase):
    def test_conflicting_author_and_label_without_a_handoff_fails_closed(self):
        lineage = resolve_lineage(
            repo=REPO, pr_number=959, branch=BRANCH, head_sha=H2,
            episodes=(), opener_lane="devin", label_lanes=("codex",),
        )
        self.assertEqual(lineage.status, "conflict")
        self.assertEqual(lineage.reason, "conflicting_builder_identity")
        self.assertIn("record the handoff", lineage.owner_action)

    def test_normal_single_builder_case_still_resolves(self):
        lineage = resolve_lineage(
            repo=REPO, pr_number=959, branch="claude/963-lineage", head_sha=H2,
            episodes=(), opener_lane="claude", label_lanes=("claude",),
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.contributors, ("claude",))
        self.assertEqual(lineage.current_writer, "claude")
        self.assertFalse(lineage.independent("claude"))
        self.assertTrue(lineage.independent("codex"))

    def test_a_pull_request_with_no_builder_identity_excludes_nobody(self):
        lineage = resolve_lineage(
            repo=REPO, pr_number=959, branch="fix/typo", head_sha=H2,
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.contributors, ())
        self.assertTrue(lineage.independent("codex"))

    def test_episode_bound_to_another_repository_pr_or_branch_is_rejected(self):
        for overrides in (
            {"repo": "other/repo"},
            {"pr_number": 960},
            {"branch": "codex/959-other"},
        ):
            with self.subTest(**overrides):
                lineage = resolve(episodes=(episode(**overrides),))
                self.assertEqual(lineage.status, "conflict")
                self.assertEqual(lineage.reason, "episode_unbound")

    def test_unchained_reordered_and_duplicated_episodes_fail_closed(self):
        broken = resolve(
            episodes=(
                episode(),
                episode(sequence=2, source_lane="codex", destination_lane="claude",
                        expected_head=H3, resulting_head=H3[:39] + "d"),
            ),
            head_sha=H3[:39] + "d",
            label_lanes=(),
        )
        self.assertEqual(broken.reason, "episode_unchained")

        gap = resolve(
            episodes=(episode(sequence=2),), head_sha=H2, label_lanes=("codex",)
        )
        self.assertEqual(gap.reason, "episode_unchained")

        duplicated = resolve(
            episodes=(episode(), episode(destination_lane="claude")),
            label_lanes=(),
        )
        self.assertEqual(duplicated.reason, "episode_duplicated")

    def test_an_identical_replayed_episode_is_not_a_duplicate(self):
        lineage = resolve(episodes=(episode(), episode()))
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.episodes, 1)
        self.assertEqual(lineage.contributors, ("devin", "codex"))

    def test_malformed_and_unverified_writer_state_fail_closed(self):
        malformed = resolve(episodes=({"schema": "nope"},))
        self.assertEqual(malformed.reason, "episode_malformed")

        raw = episode().as_dict()
        raw["writer_state"] = "unknown"
        unverified = resolve(episodes=(raw,))
        self.assertIn(unverified.reason, {"episode_malformed", "writer_state_unverified"})

    def test_an_opener_outside_the_lineage_fails_closed(self):
        lineage = resolve(opener_lane="claude", label_lanes=())
        self.assertEqual(lineage.reason, "opener_outside_lineage")

    def test_an_abbreviated_or_missing_head_is_never_resolved(self):
        for head in ("", "abc1234", H2[:39]):
            with self.subTest(head=head):
                lineage = resolve(head_sha=head)
                self.assertEqual(lineage.status, "conflict")
                self.assertEqual(lineage.reason, "target_invalid")

    def test_episode_construction_rejects_self_handoff_and_bad_shas(self):
        for overrides in (
            {"destination_lane": "devin"},
            {"expected_head": "zz"},
            {"sequence": 0},
            {"writer_state": "running"},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(LineageError):
                    episode(**overrides)


class IdentityMappingTests(unittest.TestCase):
    def test_identity_maps_labels_and_author_onto_lane_names(self):
        opener, labels = lanes_from_identity(
            identity=IDENTITY,
            labels=["builder:codex", "tier:R"],
            author="devin-ai-integration[bot]",
        )
        self.assertEqual(opener, "devin")
        self.assertEqual(labels, ("codex",))

    def test_disabled_identity_names_no_lanes(self):
        self.assertEqual(
            lanes_from_identity(identity={"enabled": False}, labels=["builder:codex"],
                                author="devin-ai-integration[bot]"),
            ("", ()),
        )

    def test_identity_only_resolution_needs_no_head(self):
        lineage = resolve_identity_only(opener_lane="claude", label_lanes=("claude",))
        self.assertEqual(lineage.current_writer, "claude")
        self.assertEqual(
            resolve_identity_only(opener_lane="devin", label_lanes=("codex",)).status,
            "conflict",
        )


class RecordTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        # Private lineage state must live outside any Git repository, so the
        # record tests need a temporary root that is not inside this checkout.
        base = Path(tempfile.gettempdir()).resolve()
        if any((parent / ".git").exists() for parent in (base, *base.parents)):
            base = Path("/tmp").resolve()
        if any((parent / ".git").exists() for parent in (base, *base.parents)):
            self.skipTest("no Git-free temporary directory is available here")
        self.root = tempfile.mkdtemp(prefix="code-mower-lineage-", dir=str(base))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_recording_is_idempotent_and_chained(self):
        first = record_episode(self.root, episode())
        self.assertEqual(first, {"recorded": True, "duplicate": False, "episodes": 1})
        replay = record_episode(self.root, episode())
        self.assertEqual(replay, {"recorded": False, "duplicate": True, "episodes": 1})
        self.assertEqual(len(load_episodes(self.root, REPO, 959)), 1)

        with self.assertRaises(LineageError):
            record_episode(self.root, episode(destination_lane="claude"))
        with self.assertRaises(LineageError):
            # Sequence 2 must start from the head sequence 1 produced.
            record_episode(
                self.root,
                episode(sequence=2, source_lane="codex", destination_lane="claude",
                        expected_head=H3, resulting_head=H1),
            )
        record_episode(
            self.root,
            episode(sequence=2, source_lane="codex", destination_lane="claude",
                    expected_head=H2, resulting_head=H3),
        )
        self.assertEqual(len(load_episodes(self.root, REPO, 959)), 2)

    def test_unrecorded_pull_requests_read_as_empty(self):
        self.assertEqual(load_episodes(self.root, REPO, 1), ())


class PublicTransportTests(unittest.TestCase):
    def test_marker_round_trips_metadata_only(self):
        marker = lineage_comment_marker((episode(),))
        self.assertNotIn("/Users", marker)
        self.assertNotIn("session", marker)
        parsed = episodes_from_comment_body("text\n" + marker + "\nmore")
        self.assertEqual(parsed, (episode(),))

    def test_an_unreadable_marker_raises_rather_than_being_ignored(self):
        with self.assertRaises(LineageError):
            episodes_from_comment_body("<!-- CODE_MOWER_BUILDER_LINEAGE {not json} -->")

    def test_a_body_without_a_marker_yields_nothing(self):
        self.assertEqual(episodes_from_comment_body("Codex took this over."), ())


class ReviewerAdmissionTests(unittest.TestCase):
    def pr_meta(self, *, author="devin-ai-integration[bot]", labels=("builder:codex",)):
        return {
            "user": {"login": author},
            "head": {"ref": BRANCH, "sha": H2},
            "labels": [{"name": name} for name in labels],
        }

    def test_contributors_are_refused_and_an_independent_lane_is_admitted(self):
        for lane, admitted in (("devin", False), ("codex", False), ("claude", True)):
            with self.subTest(lane=lane):
                decision = reviewer_admission(
                    lane, repo=REPO, pr_number=959, pr_meta=self.pr_meta(),
                    head_sha=H2, episodes=(episode(),), identity=IDENTITY,
                )
                self.assertEqual(decision["admitted"], admitted)
                self.assertEqual(decision["current_writer"], "codex")
                self.assertEqual(decision["contributors"], ["devin", "codex"])
                if not admitted:
                    self.assertEqual(decision["reason"], "contributor_not_independent")
                    self.assertTrue(decision["owner_action"])

    def test_admission_uses_the_head_the_caller_pinned(self):
        decision = reviewer_admission(
            "claude", repo=REPO, pr_number=959, pr_meta=self.pr_meta(),
            head_sha=H3, episodes=(episode(),), identity=IDENTITY,
        )
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "lineage_waiting")

    def test_require_independent_reviewer_raises_with_bounded_metadata(self):
        with self.assertRaises(ReviewerNotIndependent) as caught:
            require_independent_reviewer(
                "codex", repo=REPO, pr_number=959, pr_meta=self.pr_meta(),
                head_sha=H2, episodes=(episode(),), identity=IDENTITY,
            )
        message = str(caught.exception)
        self.assertIn("contributor_not_independent", message)
        self.assertNotIn("/", message.split("is not admitted")[0])

    def test_no_evidence_plus_contradictory_signals_refuses_every_lane(self):
        for lane in ("devin", "codex", "claude"):
            with self.subTest(lane=lane):
                decision = reviewer_admission(
                    lane, repo=REPO, pr_number=959, pr_meta=self.pr_meta(),
                    head_sha=H2, identity=IDENTITY,
                )
                self.assertFalse(decision["admitted"])
                self.assertEqual(decision["reason"], "lineage_conflict")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
