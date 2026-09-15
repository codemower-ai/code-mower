"""End-to-end regressions for builder lineage across real production seams.

The consumer regressions in ``test_builder_lineage_consumers`` prove each seam
carries lineage when it is handed some. These prove the seams *obtain* it: the
producer publishes, the publication survives an untrusted duplicate, and a
reviewer host that recorded nothing still resolves the takeover from the pull
request itself before its provider is launched.

Every case is written from the #959 shape -- Devin opens, Codex takes over,
independent Claude reviews -- on a reviewer host with an empty private store,
because that is the arrangement in which every single-signal answer, and every
private-store-only answer, gets the wrong reviewer.
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import builder_lineage, lane_delivery, lane_handoff  # noqa: E402
from code_mower.audit_labeler_lib import (  # noqa: E402
    lineage_context,
    lineage_marker_author_trust,
    published_lineage_episodes,
    resolve_builder_lineage,
)
from code_mower.provider_runners import lineage as reviewer_lineage  # noqa: E402

from test_builder_lineage_consumers import (  # noqa: E402
    BRANCH,
    FIXED,
    IDENTITY,
    PR,
    REPO,
    TAKEN,
    git_free_tempdir,
    takeover_episode,
)
from test_controller import (  # noqa: E402
    _only_codex_merge_reviewer,
    _options,
    _pr,
    _status,
)

AUTHORITY = "codemower-ai"
OUTSIDER = "passer-by"


def continuation_episode(
    sequence: int = 2, expected: str = TAKEN, resulting: str = FIXED
) -> builder_lineage.ContributionEpisode:
    """Codex advancing the pull request it already holds."""

    return builder_lineage.ContributionEpisode(
        sequence=sequence,
        kind=builder_lineage.CONTINUATION_KIND,
        repo=REPO,
        pr_number=PR,
        branch=BRANCH,
        source_lane="codex",
        destination_lane="codex",
        expected_head=expected,
        resulting_head=resulting,
        writer_state=builder_lineage.CONTINUATION_WRITER_STATE,
    )


def _variant(
    episode: builder_lineage.ContributionEpisode, **changes
) -> builder_lineage.ContributionEpisode:
    """The same episode with one field altered -- forged, not recorded."""

    payload = {
        field: getattr(episode, field)
        for field in builder_lineage.EPISODE_FIELDS
        if field != "schema"
    }
    payload.update(changes)
    return builder_lineage.ContributionEpisode(**payload)


def published(episodes, *, author: str = AUTHORITY) -> dict:
    """A pull request comment carrying the bounded lineage marker."""

    return {
        "user": {"login": author},
        "body": "Builder contribution lineage for this head.\n\n"
        + builder_lineage.lineage_comment_marker(tuple(episodes)),
    }


def pr_meta(*, author: str = "devin-ai-integration[bot]", labels=("builder:codex",),
            branch: str = BRANCH, head: str = TAKEN) -> dict:
    return {
        "user": {"login": author},
        "head": {"ref": branch, "sha": head},
        "labels": [{"name": name} for name in labels],
    }


class EmptyStoreReviewerHost(unittest.TestCase):
    """A reviewer host recorded nothing, so the pull request is the evidence.

    Reading only the private store answers "no takeover happened" on precisely
    the independent hosts where a takeover is the question being asked. Each of
    these drives the *real* wrapper entry point, not the shared resolver.
    """

    def setUp(self):
        empty = git_free_tempdir(self, "code-mower-empty-store-")
        self.patches = mock.patch.dict(
            "os.environ",
            {
                lane_handoff.STATE_DIR_ENV: str(empty),
                reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(IDENTITY),
            },
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.comments = [published([takeover_episode()])]

    def test_claude_wrapper_is_admitted_and_both_contributors_are_not(self):
        from code_mower import claude_audit_pr

        decision = claude_audit_pr._require_independent_review(
            "claude", REPO, PR, pr_meta(), TAKEN,
            authorities=(AUTHORITY,),
            fetch_comments=lambda: self.comments,
        )

        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["current_writer"], "codex")
        self.assertEqual(sorted(decision["contributors"]), ["codex", "devin"])

        for lane in ("codex", "devin"):
            with self.subTest(lane=lane):
                with self.assertRaises(RuntimeError) as raised:
                    claude_audit_pr._require_independent_review(
                        lane, REPO, PR, pr_meta(), TAKEN,
                        authorities=(AUTHORITY,),
                        fetch_comments=lambda: self.comments,
                    )
                self.assertIn("contributor_not_independent", str(raised.exception))

    def test_codex_wrapper_reads_the_same_published_evidence(self):
        from code_mower import codex_audit_pr

        with self.assertRaises(RuntimeError) as raised:
            codex_audit_pr._require_independent_review(
                "codex", REPO, PR, pr_meta(), TAKEN,
                authorities=(AUTHORITY,),
                fetch_comments=lambda: self.comments,
            )
        self.assertIn("contributor_not_independent", str(raised.exception))

        admitted = codex_audit_pr._require_independent_review(
            "claude", REPO, PR, pr_meta(), TAKEN,
            authorities=(AUTHORITY,),
            fetch_comments=lambda: self.comments,
        )
        self.assertTrue(admitted["admitted"])

    def test_devin_cli_wrapper_refuses_its_own_contribution_from_a_comment(self):
        from code_mower import devin_cli_audit_pr

        config = SimpleNamespace(repo=REPO, pr_number=PR, github_token="unused")
        with mock.patch.dict(
            "os.environ", {"CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY}
        ):
            with self.assertRaises(devin_cli_audit_pr.AuthorExcludedError) as raised:
                devin_cli_audit_pr._require_independent_devin_review(
                    config, pr_meta(author="someone-else"), TAKEN, "someone-else",
                    fetch_comments=lambda: self.comments,
                )
        self.assertIn("contributor_not_independent", str(raised.exception))

    def test_an_unreadable_publication_fetch_refuses_rather_than_admitting(self):
        from code_mower import claude_audit_pr

        def explode():
            raise OSError("transport")

        with self.assertRaises(RuntimeError) as raised:
            claude_audit_pr._require_independent_review(
                "claude", REPO, PR, pr_meta(), TAKEN,
                authorities=(AUTHORITY,),
                fetch_comments=explode,
            )
        self.assertIn("lineage_unreadable", str(raised.exception))

    def test_an_untrusted_publisher_is_not_evidence(self):
        """An audit bot can post a comment; that is not a takeover it can assert."""

        from code_mower import claude_audit_pr

        outsider = [published([takeover_episode()], author=OUTSIDER)]
        decision = claude_audit_pr._require_independent_review(
            "codex", REPO, PR, pr_meta(labels=()), TAKEN,
            authorities=(AUTHORITY,),
            fetch_comments=lambda: outsider,
        )
        # Falls back to the ordinary identity-only answer, which names the
        # opener -- not the takeover the untrusted marker claimed.
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["contributors"], ["devin"])

    def test_no_configured_authority_reads_no_comments_at_all(self):
        fetched = []

        def fetch():
            fetched.append(1)
            return self.comments

        episodes = reviewer_lineage.reviewer_evidence(
            REPO, PR, authorities=(), fetch_comments=fetch
        )
        self.assertEqual(episodes, ())
        self.assertEqual(fetched, [])


class ReviewAdapterBinding(unittest.TestCase):
    """The embedding adapter carries the metadata the binding is made of."""

    def setUp(self):
        empty = git_free_tempdir(self, "code-mower-adapter-store-")
        patched = mock.patch.dict(
            "os.environ",
            {
                lane_handoff.STATE_DIR_ENV: str(empty),
                reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(IDENTITY),
            },
        )
        patched.start()
        self.addCleanup(patched.stop)

    def _review(self, **overrides):
        from code_mower.devin_review import ReviewInput

        marker = published([takeover_episode()])["body"]
        fields = dict(
            repository=REPO,
            pr=PR,
            head=TAKEN,
            author="someone-else",
            context={},
            changed_files=("handler.py",),
            branch=BRANCH,
            labels=("builder:codex",),
            lineage_markers=(marker,),
        )
        fields.update(overrides)
        return ReviewInput(**fields)

    def test_devin_is_refused_on_a_takeover_it_contributed_to(self):
        self.assertFalse(self._review().lineage_admits())

    def test_a_branch_the_episodes_do_not_name_is_not_admission(self):
        """Strict binding, not a looser one to accommodate the new field."""

        review = self._review(branch="codex/other-branch")
        self.assertFalse(review.lineage_admits())
        self.assertFalse(
            review.pr_metadata()["head"]["ref"] == BRANCH,
            "the carried branch must reach the resolver unchanged",
        )

    def test_labels_and_branch_reach_the_resolver(self):
        meta = self._review().pr_metadata()
        self.assertEqual(meta["head"], {"ref": BRANCH, "sha": TAKEN})
        self.assertEqual(meta["labels"], [{"name": "builder:codex"}])

    def test_an_unbounded_marker_list_is_refused_not_parsed(self):
        marker = published([takeover_episode()])["body"]
        review = self._review(lineage_markers=(marker,) * 40)
        with self.assertRaises(ValueError):
            review.published_lineage()
        self.assertFalse(review.lineage_admits())

    def test_an_independent_lane_is_still_refused_for_the_devin_lane_only(self):
        """The adapter decides one lane. Claude's admission is the wrappers'."""

        review = self._review(head=TAKEN)
        self.assertFalse(review.lineage_admits())
        admission = reviewer_lineage.reviewer_admission(
            "claude",
            repo=REPO,
            pr_number=PR,
            pr_meta=review.pr_metadata(),
            head_sha=TAKEN,
            episodes=review.published_lineage(),
            identity=IDENTITY,
        )
        self.assertTrue(admission["admitted"])


class PublicationTrustContract(unittest.TestCase):
    """Publication and consumption have to mean the same thing by "trusted"."""

    def _publish(self, existing, *, trusted_author=None, episodes=None):
        posted = []
        result = lane_delivery.publish_lineage_evidence(
            repo=REPO,
            pr_number=str(PR),
            branch=BRANCH,
            head_sha=TAKEN,
            episodes=tuple(episodes or (takeover_episode(),)),
            opener_lane="devin",
            label_lanes=("codex",),
            existing_bodies=lambda: list(existing),
            publish=lambda body: (
                posted.append(body),
                existing.append({"user": {"login": AUTHORITY}, "body": body}),
            ),
            trusted_author=trusted_author,
        )
        return result, posted

    def test_an_untrusted_identical_body_does_not_suppress_the_publication(self):
        marker = published([takeover_episode()], author=OUTSIDER)
        existing = [marker]
        result, posted = self._publish(
            existing,
            trusted_author=reviewer_lineage.marker_author_trust((AUTHORITY,)),
        )
        self.assertTrue(result["published"])
        self.assertEqual(len(posted), 1)

    def test_a_trusted_publication_is_idempotent(self):
        existing = [published([takeover_episode()])]
        result, posted = self._publish(
            existing,
            trusted_author=reviewer_lineage.marker_author_trust((AUTHORITY,)),
        )
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["published"])
        self.assertEqual(posted, [])

    def test_a_comment_no_consumer_can_read_is_not_a_publication(self):
        """A successful POST under an untrusted account is not evidence."""

        existing = []
        posted = []
        result = lane_delivery.publish_lineage_evidence(
            repo=REPO,
            pr_number=str(PR),
            branch=BRANCH,
            head_sha=TAKEN,
            episodes=(takeover_episode(),),
            opener_lane="devin",
            label_lanes=("codex",),
            existing_bodies=lambda: list(existing),
            publish=lambda body: (
                posted.append(body),
                existing.append({"user": {"login": OUTSIDER}, "body": body}),
            ),
            trusted_author=reviewer_lineage.marker_author_trust((AUTHORITY,)),
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "publication_author_untrusted")
        self.assertTrue(result["owner_action"])
        self.assertEqual(len(posted), 1, "the attempt happened; it did not count")

    def test_lineage_that_does_not_resolve_at_the_head_is_never_published(self):
        existing = []
        result, posted = self._publish(
            existing,
            trusted_author=reviewer_lineage.marker_author_trust((AUTHORITY,)),
            episodes=(takeover_episode(resulting=FIXED),),
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "lineage_waiting")
        self.assertEqual(posted, [])

    def test_the_published_payload_carries_no_field_beyond_the_contract(self):
        marker = builder_lineage.lineage_comment_marker((takeover_episode(),))
        payload = json.loads(marker.split(None, 2)[2].rsplit("-->", 1)[0].strip())
        for episode in payload["episodes"]:
            self.assertEqual(
                sorted(episode), sorted(builder_lineage.EPISODE_FIELDS)
            )


class PublishThenReconcileCli(unittest.TestCase):
    """The real ``lane-delivery lineage`` path, publication before the label."""

    def setUp(self):
        self.root = git_free_tempdir(self, "code-mower-producer-")
        builder_lineage.record_episode(
            lane_handoff.lineage_root(self.root), takeover_episode()
        )
        patched = mock.patch.dict(
            "os.environ", {"CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY}
        )
        patched.start()
        self.addCleanup(patched.stop)

    def _args(self, **overrides):
        args = SimpleNamespace(
            repo=REPO, pr=str(PR), branch=BRANCH, head=TAKEN,
            labels=["builder:devin"], author="devin-ai-integration[bot]",
            identity_json=json.dumps(IDENTITY), state_dir=self.root,
            publish=True, reconcile_labels=True, json=True,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_an_untrusted_duplicate_does_not_block_the_required_publication(self):
        existing = [published([takeover_episode()], author=OUTSIDER)]
        posted, applied = [], []
        code = lane_delivery._lineage_main(
            self._args(),
            head=lambda repo, number: TAKEN,
            labels=lambda repo, number, add, remove: applied.append((add, remove)),
            comment_bodies=lambda repo, number: list(existing),
            publish_comment=lambda repo, number, body: (
                posted.append(body),
                existing.append({"user": {"login": AUTHORITY}, "body": body}),
            ),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(posted), 1)
        self.assertEqual(applied, [(("builder:codex",), ("builder:devin",))])

    def test_publication_failure_abandons_the_label_move(self):
        applied = []

        def refuse(repo, number, body):
            raise RuntimeError("comment rejected")

        with self.assertRaises(RuntimeError):
            lane_delivery._lineage_main(
                self._args(),
                head=lambda repo, number: TAKEN,
                labels=lambda repo, number, add, remove: applied.append((add, remove)),
                comment_bodies=lambda repo, number: [],
                publish_comment=refuse,
            )
        self.assertEqual(applied, [])

    def test_a_publication_no_consumer_trusts_blocks_the_label_move(self):
        existing, applied = [], []
        code = lane_delivery._lineage_main(
            self._args(),
            head=lambda repo, number: TAKEN,
            labels=lambda repo, number, add, remove: applied.append((add, remove)),
            comment_bodies=lambda repo, number: list(existing),
            publish_comment=lambda repo, number, body: existing.append(
                {"user": {"login": OUTSIDER}, "body": body}
            ),
        )
        self.assertEqual(code, 3)
        self.assertEqual(applied, [])

    def test_a_head_that_moved_under_the_record_publishes_nothing(self):
        existing, applied = [], []
        code = lane_delivery._lineage_main(
            self._args(head=FIXED),
            head=lambda repo, number: FIXED,
            labels=lambda repo, number, add, remove: applied.append((add, remove)),
            comment_bodies=lambda repo, number: list(existing),
            publish_comment=lambda repo, number, body: existing.append(
                {"user": {"login": AUTHORITY}, "body": body}
            ),
        )
        self.assertEqual(code, 3)
        self.assertEqual(existing, [])
        self.assertEqual(applied, [])


class GoldenTakeoverChain(unittest.TestCase):
    """Producer -> published comment -> empty-store reviewer -> labeler -> gate."""

    def setUp(self):
        self.producer_root = git_free_tempdir(self, "code-mower-golden-producer-")
        self.reviewer_root = git_free_tempdir(self, "code-mower-golden-reviewer-")
        builder_lineage.record_episode(
            lane_handoff.lineage_root(self.producer_root), takeover_episode()
        )
        patched = mock.patch.dict(
            "os.environ",
            {
                "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
                reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(IDENTITY),
            },
        )
        patched.start()
        self.addCleanup(patched.stop)

    def _publish_from_the_producer(self):
        comments = []
        code = lane_delivery._lineage_main(
            SimpleNamespace(
                repo=REPO, pr=str(PR), branch=BRANCH, head=TAKEN,
                labels=["builder:devin"], author="devin-ai-integration[bot]",
                identity_json=json.dumps(IDENTITY), state_dir=self.producer_root,
                publish=True, reconcile_labels=True, json=True,
            ),
            head=lambda repo, number: TAKEN,
            labels=lambda repo, number, add, remove: None,
            comment_bodies=lambda repo, number: list(comments),
            publish_comment=lambda repo, number, body: comments.append(
                {"user": {"login": AUTHORITY}, "body": body}
            ),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(comments), 1)
        return comments

    def test_the_whole_chain_admits_claude_and_lets_it_satisfy_the_gate(self):
        from code_mower import claude_audit_pr
        from code_mower.audit_labeler_lib import (
            author_exclusion_reason,
            builder_identity_matches,
            lineage_context,
            lineage_marker_author_trust,
        )

        comments = self._publish_from_the_producer()
        meta = pr_meta()

        with mock.patch.dict(
            "os.environ", {lane_handoff.STATE_DIR_ENV: str(self.reviewer_root)}
        ):
            admission = claude_audit_pr._require_independent_review(
                "claude", REPO, PR, meta, TAKEN,
                authorities=(AUTHORITY,),
                fetch_comments=lambda: comments,
            )
        self.assertTrue(admission["admitted"])

        context = lineage_context(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            comments=comments,
            trusted_author=lineage_marker_author_trust(authorities=(AUTHORITY,)),
        )
        self.assertEqual(len(context.episodes), 1)

        # The labeler updates Claude's own done label rather than skipping it.
        self.assertIsNone(
            author_exclusion_reason(
                lane_name="claude",
                labels=["builder:codex"],
                author="devin-ai-integration[bot]",
                text="",
                config=IDENTITY,
                lineage=context,
            )
        )
        # And the gate reads the same evidence, naming both contributors.
        self.assertEqual(
            sorted(
                builder_identity_matches(
                    labels=["builder:codex"],
                    author="devin-ai-integration[bot]",
                    text="",
                    config=IDENTITY,
                    lineage=context,
                )
            ),
            ["codex", "devin"],
        )

    def test_a_same_writer_continuation_keeps_the_chain_at_the_new_head(self):
        comments = self._publish_from_the_producer()
        comments.append(published([takeover_episode(), continuation_episode()]))

        with mock.patch.dict(
            "os.environ", {lane_handoff.STATE_DIR_ENV: str(self.reviewer_root)}
        ):
            episodes = reviewer_lineage.reviewer_evidence(
                REPO, PR, authorities=(AUTHORITY,), fetch_comments=lambda: comments
            )
        resolved = reviewer_lineage.pr_lineage(
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(head=FIXED),
            head_sha=FIXED,
            episodes=episodes,
            identity=IDENTITY,
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "codex")
        self.assertEqual(sorted(resolved.contributors), ["codex", "devin"])

    def test_conflicting_published_evidence_fails_closed(self):
        comments = self._publish_from_the_producer()
        comments.append(
            published([_variant(takeover_episode(), destination_lane="claude")])
        )
        with mock.patch.dict(
            "os.environ", {lane_handoff.STATE_DIR_ENV: str(self.reviewer_root)}
        ):
            episodes = reviewer_lineage.reviewer_evidence(
                REPO, PR, authorities=(AUTHORITY,), fetch_comments=lambda: comments
            )
        decision = reviewer_lineage.reviewer_admission(
            "claude",
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(),
            head_sha=TAKEN,
            episodes=episodes,
            identity=IDENTITY,
        )
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "lineage_conflict")

    def test_stale_published_evidence_waits_rather_than_guessing(self):
        comments = self._publish_from_the_permanent_past()
        decision = reviewer_lineage.reviewer_admission(
            "claude",
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(head=FIXED),
            head_sha=FIXED,
            episodes=comments,
            identity=IDENTITY,
        )
        self.assertFalse(decision["admitted"])
        self.assertEqual(decision["reason"], "lineage_waiting")

    def _publish_from_the_permanent_past(self):
        return (takeover_episode(),)


class BoundedIdenticalReplay(unittest.TestCase):
    """Republishing the same chain must not look like a malformed lineage.

    The producer publishes the whole chain on every round, and a reviewer merges
    its private record with every trusted marker it finds. Eight snapshots of an
    eight-episode lineage is thirty-six arrivals of thirty-two-or-fewer distinct
    episodes; counting arrivals against the lineage bound calls an authorised
    replay malformed.
    """

    def _chain(self, length: int):
        episodes = [takeover_episode(resulting="0" * 39 + "1")]
        for index in range(2, length + 1):
            episodes.append(
                continuation_episode(
                    sequence=index,
                    expected=episodes[-1].resulting_head,
                    resulting=f"{index:040x}",
                )
            )
        return tuple(episodes)

    def test_eight_snapshots_of_an_eight_episode_chain_still_resolve(self):
        chain = self._chain(8)
        arrivals = tuple(episode for _ in range(8) for episode in chain)
        self.assertGreater(len(arrivals), builder_lineage.MAX_EPISODES)

        resolved = builder_lineage.resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=chain[-1].resulting_head,
            episodes=arrivals,
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.episodes, 8)

    def test_repeated_markers_collapse_before_they_reach_the_resolver(self):
        root = git_free_tempdir(self, "code-mower-replay-")
        chain = self._chain(8)
        comments = [published(chain) for _ in range(8)]
        with mock.patch.dict(
            "os.environ", {lane_handoff.STATE_DIR_ENV: str(root)}
        ):
            episodes = reviewer_lineage.reviewer_evidence(
                REPO, PR, authorities=(AUTHORITY,), fetch_comments=lambda: comments
            )
        self.assertEqual(len(episodes), 8)

    def test_a_disagreeing_duplicate_still_fails_closed(self):
        chain = self._chain(3)
        forged = _variant(chain[1], destination_lane="claude", source_lane="claude")
        resolved = builder_lineage.resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=chain[-1].resulting_head,
            episodes=chain + chain + (forged,),
        )
        self.assertEqual(resolved.status, "conflict")
        self.assertEqual(resolved.reason, "episode_duplicated")

    def test_an_unbounded_arrival_is_refused_without_being_walked(self):
        chain = self._chain(2)
        arrivals = chain * (builder_lineage.MAX_EPISODE_ENTRIES // 2 + 1)
        resolved = builder_lineage.resolve_lineage(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=chain[-1].resulting_head,
            episodes=arrivals,
        )
        self.assertEqual(resolved.status, "conflict")
        self.assertEqual(resolved.reason, "episode_malformed")


class AReviewerMustBeAbleToNameItsOwnLane(unittest.TestCase):
    """A floor, not a default.

    Reviewer independence is decided by naming lanes. A deployment whose
    contract maps the reviewer's own label or account to a blank string names
    no lane for it, so it cannot be recognised as the contributor it is -- and
    the seam admits exactly the reviewer it exists to exclude. `setdefault`
    left such an entry in place, because the key was present.
    """

    MINIMAL = {
        "enabled": True,
        "labels": {"builder:codex": ""},
        "authors": {"codex[bot]": ""},
    }

    def _admit(self, lane, identity, *, author="codex[bot]",
               labels=("builder:codex",)):
        from code_mower import codex_audit_pr

        with mock.patch.dict(
            "os.environ",
            {reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(identity)},
        ):
            return codex_audit_pr._require_independent_review(
                lane, REPO, PR, pr_meta(author=author, labels=labels), TAKEN,
                authorities=(), fetch_comments=lambda: [],
            )

    def test_a_blank_own_label_and_account_still_exclude_codex(self):
        with self.assertRaises(RuntimeError) as raised:
            self._admit("codex", self.MINIMAL)
        self.assertIn("contributor_not_independent", str(raised.exception))

    def test_an_unrelated_reviewer_is_unaffected_by_its_own_blank_entry(self):
        """Only the reviewer's own lane is floored; Claude still reviews."""

        decision = self._admit("claude", self.MINIMAL)
        self.assertTrue(decision["admitted"])

    def test_a_missing_contract_still_excludes_the_reviewer(self):
        with self.assertRaises(RuntimeError) as raised:
            self._admit("codex", {"enabled": False, "labels": {}, "authors": {}})
        self.assertIn("contributor_not_independent", str(raised.exception))

    def test_an_invalid_own_mapping_is_overwritten_not_preserved(self):
        for invalid in (None, 0, [], {}, "   "):
            with self.subTest(invalid=invalid):
                identity = {
                    "enabled": True,
                    "labels": {"builder:codex": invalid},
                    "authors": {"codex[bot]": invalid},
                }
                with self.assertRaises(RuntimeError) as raised:
                    self._admit("codex", identity)
                self.assertIn("contributor_not_independent", str(raised.exception))

    def test_a_conflicting_own_label_refuses_before_the_provider_runs(self):
        identity = {
            "enabled": True,
            "labels": {"builder:codex": "claude"},
            "authors": {"codex[bot]": "codex"},
        }
        with self.assertRaises(reviewer_lineage.ReviewerIdentityInvalid) as raised:
            self._admit("codex", identity)
        self.assertIn("reviewer_identity_invalid", str(raised.exception))

    def test_a_conflicting_own_account_refuses_before_the_provider_runs(self):
        identity = {
            "enabled": True,
            "labels": {"builder:codex": "codex"},
            "authors": {"codex[bot]": "devin"},
        }
        with self.assertRaises(reviewer_lineage.ReviewerIdentityInvalid):
            self._admit("codex", identity)

    def test_a_disabled_contract_with_a_conflict_still_refuses(self):
        """Disabling the contract does not make a misnamed own lane safe."""

        identity = {
            "enabled": False,
            "labels": {"builder:codex": "claude"},
            "authors": {},
        }
        with self.assertRaises(reviewer_lineage.ReviewerIdentityInvalid):
            self._admit("codex", identity)


class ConfiguredBranchIdentityIsCounted(unittest.TestCase):
    """`branch_prefixes` was rendered into the contract and then ignored.

    A `codex/` branch carrying a `builder:claude` label is two configured
    signals disagreeing about who wrote the diff. Resolving it to a sole
    Claude writer admits Codex to review its own work.
    """

    CONFIG = {
        "enabled": True,
        "labels": {"builder:claude": "claude", "builder:codex": "codex"},
        "authors": {"claude[bot]": "claude", "codex[bot]": "codex"},
        "branch_prefixes": {"claude/": "claude", "codex/": "codex"},
        "require_verified_lineage": True,
    }
    UNCONFIGURED = {
        "enabled": True,
        "labels": {"builder:claude": "claude", "builder:codex": "codex"},
        "authors": {},
    }

    def _gate(self, *, branch, labels, config=None, episodes=()):
        return resolve_builder_lineage(
            labels=list(labels),
            author="a-human",
            config=config or self.CONFIG,
            repo=REPO,
            pr_number=PR,
            branch=branch,
            head_sha=TAKEN,
            episodes=episodes,
        )

    def test_a_branch_and_label_disagreement_requires_verified_lineage(self):
        resolved = self._gate(branch="codex/topic", labels=["builder:claude"])
        self.assertEqual(resolved.status, "conflict")
        self.assertEqual(resolved.reason, "conflicting_builder_identity")

    def test_a_matching_branch_and_label_stay_ordinary(self):
        resolved = self._gate(branch="claude/topic", labels=["builder:claude"])
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "claude")

    def test_an_unconfigured_deployment_keeps_its_old_answer(self):
        resolved = self._gate(
            branch="codex/topic", labels=["builder:claude"], config=self.UNCONFIGURED
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "claude")

    def test_a_recorded_takeover_is_still_accepted_over_the_branch(self):
        """Verified episodes decide; the branch never invents a takeover."""

        resolved = self._gate(
            branch=BRANCH, labels=["builder:codex"], episodes=(takeover_episode(),)
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "codex")

    def test_the_wrapper_refuses_the_disagreement_rather_than_admitting(self):
        decision = reviewer_lineage.reviewer_admission(
            "codex",
            repo=REPO,
            pr_number=PR,
            pr_meta=pr_meta(author="a-human", labels=("builder:claude",),
                            branch="codex/topic"),
            head_sha=TAKEN,
            episodes=(),
            identity=self.CONFIG,
        )
        self.assertFalse(decision["admitted"])


class TheWrapperCompositionKeepsTheConfiguredBranchContract(unittest.TestCase):
    """The floor must raise three fields, not rebuild the contract.

    Every real wrapper resolves through ``identity_with_lane_floor``. It was
    reconstructing the mapping from ``enabled``/``labels``/``authors`` alone,
    so ``branch_prefixes`` and ``require_verified_lineage`` -- rendered into
    the contract for exactly this decision -- never reached the resolver. The
    lower-helper branch tests pass an already-resolved identity and so walk
    straight past the composition that drops it. These load the configured
    contract from the environment and go through the real admission boundary.
    """

    CONFIG = {
        "enabled": True,
        "labels": {"builder:claude": "claude", "builder:codex": "codex",
                   "builder:devin": "devin"},
        "authors": {"claude[bot]": "claude", "codex[bot]": "codex",
                    "devin-ai-integration[bot]": "devin"},
        "branch_prefixes": {"claude/": "claude", "codex/": "codex",
                            "feature/cx-": "codex"},
        "require_verified_lineage": True,
    }

    @contextmanager
    def _configured(self):
        with mock.patch.dict(
            "os.environ",
            {
                reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(self.CONFIG),
                "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
            },
        ):
            yield

    def _codex(self, lane, meta, *, comments=()):
        from code_mower import codex_audit_pr

        with self._configured():
            return codex_audit_pr._require_independent_review(
                lane, REPO, PR, meta, TAKEN,
                authorities=(AUTHORITY,), fetch_comments=lambda: list(comments),
            )

    def _claude(self, lane, meta, *, comments=()):
        from code_mower import claude_audit_pr

        with self._configured():
            return claude_audit_pr._require_independent_review(
                lane, REPO, PR, meta, TAKEN,
                authorities=(AUTHORITY,), fetch_comments=lambda: list(comments),
            )

    def _devin(self, meta, author, *, comments=()):
        from code_mower import devin_cli_audit_pr

        config = SimpleNamespace(repo=REPO, pr_number=PR, github_token="unused")
        with self._configured():
            return devin_cli_audit_pr._require_independent_devin_review(
                config, meta, TAKEN, author, fetch_comments=lambda: list(comments),
            )

    DISAGREEING = dict(author="a-human", labels=("builder:claude",),
                       branch="codex/topic")

    def test_the_codex_wrapper_stops_before_the_provider_runs(self):
        with self.assertRaises(RuntimeError) as raised:
            self._codex("codex", pr_meta(**self.DISAGREEING))
        self.assertIn("lineage", str(raised.exception).lower())

    def test_the_claude_wrapper_stops_on_the_same_disagreement(self):
        # Asked about `codex`, which the unfixed composition admits outright:
        # a sole-Claude answer makes Codex look independent of its own branch.
        with self.assertRaises(RuntimeError) as raised:
            self._claude("codex", pr_meta(**self.DISAGREEING))
        self.assertNotIn("contributor_not_independent", str(raised.exception))

    def test_the_devin_wrapper_stops_on_the_same_disagreement(self):
        from code_mower import devin_cli_audit_pr

        with self.assertRaises(
            (devin_cli_audit_pr.AuthorExcludedError, RuntimeError)
        ):
            self._devin(pr_meta(**self.DISAGREEING), "a-human")

    def test_a_custom_configured_branch_prefix_is_honoured(self):
        with self.assertRaises(RuntimeError):
            self._codex(
                "codex",
                pr_meta(author="a-human", labels=("builder:claude",),
                        branch="feature/cx-topic"),
            )

    def test_a_matched_branch_and_label_keep_their_intended_behaviour(self):
        matched = pr_meta(author="a-human", labels=("builder:claude",),
                          branch="claude/topic")
        decision = self._codex("codex", matched)
        self.assertTrue(decision["admitted"])
        with self.assertRaises(RuntimeError) as raised:
            self._claude("claude", matched)
        self.assertIn("contributor_not_independent", str(raised.exception))

    def test_a_recorded_cross_lane_takeover_is_accepted_and_excludes_both(self):
        comments = [published([takeover_episode()])]
        taken = pr_meta(author="devin-ai-integration[bot]",
                        labels=("builder:codex",), branch=BRANCH)
        decision = self._claude("claude", taken, comments=comments)
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["current_writer"], "codex")
        for lane in ("codex", "devin"):
            with self.subTest(lane=lane):
                with self.assertRaises(RuntimeError):
                    self._claude(lane, taken, comments=comments)

    def test_the_floor_still_refuses_a_conflicting_own_remap(self):
        conflicting = dict(self.CONFIG)
        conflicting["labels"] = dict(self.CONFIG["labels"])
        conflicting["labels"]["builder:codex"] = "claude"
        with mock.patch.dict(
            "os.environ",
            {reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(conflicting)},
        ):
            with self.assertRaises(reviewer_lineage.ReviewerIdentityInvalid):
                reviewer_lineage.identity_with_lane_floor(
                    reviewer_lineage.load_identity(), "codex"
                )

    def _aliased(self, *pairs):
        contract = dict(self.CONFIG)
        contract["authors"] = dict(pairs)
        return contract

    def test_a_conflicting_account_alias_refuses_in_either_order(self):
        """Account names match case-insensitively, so these are one account.

        Composing the floor over the raw key left the alias untouched beside a
        new canonical entry, and which one survived normalisation came down to
        insertion order -- an alias could outrank the canonical account and let
        a lane review its own diff.
        """

        orders = (
            (("Codex[Bot]", "claude"), ("codex[bot]", "codex")),
            (("codex[bot]", "codex"), ("Codex[Bot]", "claude")),
            ((" codex[bot] ", "claude"), ("codex[bot]", "codex")),
            (("CODEX[BOT]", "devin"), ("codex[bot]", "codex")),
        )
        for pairs in orders:
            with self.subTest(order=pairs):
                with mock.patch.dict(
                    "os.environ",
                    {reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(
                        self._aliased(*pairs)
                    )},
                ):
                    with self.assertRaises(
                        reviewer_lineage.ReviewerIdentityInvalid
                    ):
                        reviewer_lineage.identity_with_lane_floor(
                            reviewer_lineage.load_identity(), "codex"
                        )

    def test_no_provider_is_invoked_on_a_conflicting_alias(self):
        """The wrappers refuse during composition, before any provider runs."""

        contract = self._aliased(("Codex[Bot]", "claude"), ("codex[bot]", "codex"))
        with mock.patch.dict(
            "os.environ",
            {
                reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(contract),
                "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
            },
        ):
            for wrapper in ("codex_audit_pr", "claude_audit_pr"):
                with self.subTest(wrapper=wrapper):
                    module = __import__(
                        f"code_mower.{wrapper}", fromlist=["_require_independent_review"]
                    )
                    with self.assertRaises(
                        reviewer_lineage.ReviewerIdentityInvalid
                    ):
                        module._require_independent_review(
                            "codex", REPO, PR, pr_meta(), TAKEN,
                            authorities=(AUTHORITY,), fetch_comments=lambda: [],
                        )
            from code_mower import devin_cli_audit_pr

            config = SimpleNamespace(repo=REPO, pr_number=PR, github_token="unused")
            with self.assertRaises(reviewer_lineage.ReviewerIdentityInvalid):
                devin_cli_audit_pr._require_independent_devin_review(
                    config, pr_meta(), TAKEN, "a-human", fetch_comments=lambda: [],
                )

    def test_a_compatible_account_alias_is_accepted(self):
        """Two spellings that name the same lane are one account, not a clash."""

        for pairs in (
            (("Codex[Bot]", "codex"), ("codex[bot]", "codex")),
            (("codex[bot]", "codex"), ("CODEX[BOT]", "Codex")),
            ((" codex[bot] ", "codex"),),
        ):
            with self.subTest(order=pairs):
                floored = reviewer_lineage.identity_with_lane_floor(
                    self._aliased(*pairs), "codex"
                )
                self.assertEqual(floored["authors"]["codex[bot]"], "codex")
                self.assertEqual(
                    floored["branch_prefixes"], self.CONFIG["branch_prefixes"]
                )
                self.assertTrue(floored["require_verified_lineage"])

    def test_an_alias_cannot_outrank_the_canonical_account(self):
        """Whatever the alias said, the canonical account names its own lane."""

        floored = reviewer_lineage.identity_with_lane_floor(
            self._aliased(("Codex[Bot]", "codex"), ("devin-ai-integration[bot]", "devin")),
            "codex",
        )
        self.assertEqual(floored["authors"]["codex[bot]"], "codex")
        self.assertEqual(floored["labels"]["builder:codex"], "codex")

    def test_the_floor_carries_the_branch_contract_through(self):
        floored = reviewer_lineage.identity_with_lane_floor(self.CONFIG, "codex")
        self.assertEqual(floored["branch_prefixes"], self.CONFIG["branch_prefixes"])
        self.assertTrue(floored["require_verified_lineage"])
        self.assertEqual(floored["labels"]["builder:codex"], "codex")


class ATrustedMarkerMustParseOrSaySo(unittest.TestCase):
    """A broken marker is unreadable evidence, never absent evidence.

    The payload regex matches only a complete, object-shaped, terminated
    marker. Looking for evidence with it alone meant an unterminated or
    non-object marker was not read as broken -- it was not seen at all, and a
    trusted comment announcing lineage reported none. Absence and
    unreadability are opposite answers: absence admits an independent reviewer
    on the ordinary single-builder story, unreadability has to stop.
    """

    TRUST = ("codemower-ai",)

    def _trusted(self, body, author=AUTHORITY):
        return [{"user": {"login": author}, "body": body}]

    def _gate(self, comments):
        """What the gate and the labelers actually run over the comments."""

        return published_lineage_episodes(
            comments,
            trusted_author=lineage_marker_author_trust(authorities=self.TRUST),
        )

    def _labeler(self, comments):
        """The labeler entrypoint, which fails closed on LineageError."""

        return lineage_context(
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            comments=comments,
            trusted_author=lineage_marker_author_trust(authorities=self.TRUST),
        )

    def _valid(self):
        return builder_lineage.lineage_comment_marker((takeover_episode(),))

    def _assert_unreadable(self, body):
        comments = self._trusted(body)
        for consumer in (self._gate, self._labeler):
            with self.subTest(consumer=consumer.__name__):
                with self.assertRaises(builder_lineage.LineageError):
                    consumer(comments)

    def test_a_well_formed_marker_still_reads(self):
        episodes = self._gate(self._trusted(self._valid()))
        self.assertEqual(len(episodes), 1)
        self.assertEqual(self._labeler(self._trusted(self._valid())).episodes,
                         episodes)

    def test_an_unterminated_marker_is_unreadable_not_absent(self):
        self._assert_unreadable(self._valid().replace("-->", ""))

    def test_a_non_object_payload_is_unreadable(self):
        self._assert_unreadable(
            f"<!-- {builder_lineage.LINEAGE_MARKER} [1, 2, 3] -->"
        )

    def test_malformed_json_is_unreadable(self):
        self._assert_unreadable(
            f'<!-- {builder_lineage.LINEAGE_MARKER} {{"schema": -->'
        )

    def test_an_empty_payload_is_unreadable(self):
        self._assert_unreadable(f"<!-- {builder_lineage.LINEAGE_MARKER} -->")

    def test_two_markers_on_one_comment_are_ambiguous(self):
        self._assert_unreadable(f"{self._valid()}\n\n{self._valid()}")

    def test_a_valid_marker_beside_a_broken_one_is_still_ambiguous(self):
        broken = f"<!-- {builder_lineage.LINEAGE_MARKER} [] -->"
        self._assert_unreadable(f"{self._valid()}\n\n{broken}")

    def test_a_marker_past_the_body_bound_is_unreadable_not_absent(self):
        filler = "x" * builder_lineage.MAX_MARKER_BODY_CHARS
        self._assert_unreadable(filler + "\n" + self._valid())

    def test_an_untrusted_broken_marker_is_not_authoritative(self):
        """Trust is decided before parsing, so an outsider cannot force a stop."""

        body = self._valid().replace("-->", "")
        comments = self._trusted(body, author=OUTSIDER)
        self.assertEqual(self._gate(comments), ())
        self.assertEqual(self._labeler(comments).episodes, ())

    def test_unrelated_comments_are_ignored(self):
        comments = self._trusted("Looks good to me. Shipping after CI.")
        self.assertEqual(self._gate(comments), ())

    def test_a_mixed_history_stops_on_the_broken_comment(self):
        comments = self._trusted(self._valid()) + self._trusted(
            self._valid().replace("-->", "")
        )
        with self.assertRaises(builder_lineage.LineageError):
            self._gate(comments)

    def _duplicated(self, key: str, extra: str) -> str:
        """The valid marker with ``key`` named a second time."""

        marker = self._valid()
        head, _, tail = marker.partition("{")
        return f'{head}{{{json.dumps(key)}:{extra},{tail}'

    def test_a_duplicate_top_level_key_is_unreadable(self):
        for key, extra in (
            ("schema", '"code_mower.builderLineage.v1"'),
            ("schema", '"something.else"'),
            ("episodes", "[]"),
        ):
            with self.subTest(key=key, extra=extra):
                self._assert_unreadable(self._duplicated(key, extra))

    def test_a_duplicate_key_inside_an_episode_is_unreadable(self):
        marker = self._valid()
        # Name the episode's own binding twice: two answers to "which head".
        forged = marker.replace(
            '"resulting_head"', '"resulting_head":"' + "c" * 40 + '","resulting_head"', 1
        )
        self.assertNotEqual(forged, marker)
        self._assert_unreadable(forged)

    def test_a_duplicate_nested_identity_key_is_unreadable(self):
        marker = self._valid()
        forged = marker.replace(
            '"destination_lane"', '"destination_lane":"claude","destination_lane"', 1
        )
        self.assertNotEqual(forged, marker)
        self._assert_unreadable(forged)

    def test_a_unique_key_payload_still_reads(self):
        self.assertEqual(len(self._gate(self._trusted(self._valid()))), 1)

    def test_an_untrusted_duplicate_key_marker_is_not_authoritative(self):
        comments = self._trusted(self._duplicated("episodes", "[]"), author=OUTSIDER)
        self.assertEqual(self._gate(comments), ())
        self.assertEqual(self._labeler(comments).episodes, ())

    def test_a_duplicate_key_marker_admits_no_reviewer(self):
        """The wrapper boundary: unreadable evidence stops, never admits."""

        comments = self._trusted(self._duplicated("episodes", "[]"))
        with self.assertRaises(RuntimeError) as raised:
            from code_mower import claude_audit_pr

            claude_audit_pr._require_independent_review(
                "claude", REPO, PR, pr_meta(), TAKEN,
                authorities=(AUTHORITY,), fetch_comments=lambda: comments,
            )
        self.assertIn("lineage_unreadable", str(raised.exception))

    def test_a_valid_history_beside_unrelated_comments_still_resolves(self):
        comments = (
            self._trusted("first pass looks reasonable")
            + self._trusted(self._valid())
            + self._trusted("thanks!", author=OUTSIDER)
        )
        resolved = resolve_builder_lineage(
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
            config=IDENTITY,
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            episodes=self._gate(comments),
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.current_writer, "codex")


class CumulativePublicationAtFullLength(unittest.TestCase):
    """The longest supported lineage, published the way the producer publishes.

    Evidence goes out as a cumulative snapshot after every round, so a lineage
    that runs to ``MAX_EPISODES`` is delivered as ``1 + 2 + ... + 32`` raw
    episodes. A bound below that total refuses a lineage the system is
    documented to support -- and refuses it before deduplication, the only step
    that could have shown those arrivals to be one chain.
    """

    def _chain(self, length: int):
        episodes = [takeover_episode(resulting="0" * 39 + "1")]
        for index in range(2, length + 1):
            episodes.append(
                continuation_episode(
                    sequence=index,
                    expected=episodes[-1].resulting_head,
                    resulting=f"{index:040x}",
                )
            )
        return tuple(episodes)

    def _cumulative_comments(self, chain):
        """One published comment per round, each carrying the whole chain."""

        return [published(chain[:length]) for length in range(1, len(chain) + 1)]

    def _gate_episodes(self, comments):
        """What the gate and the labelers actually read off the comments."""

        return published_lineage_episodes(
            comments,
            trusted_author=lineage_marker_author_trust(authorities=(AUTHORITY,)),
        )

    def test_the_full_cumulative_history_is_the_documented_arrival_maximum(self):
        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain))
        expected = builder_lineage.MAX_EPISODES * (builder_lineage.MAX_EPISODES + 1) // 2
        self.assertEqual(len(arrivals), expected, "1 + 2 + ... + 32")
        self.assertEqual(expected, 528)
        self.assertLessEqual(expected, builder_lineage.MAX_EPISODE_ARRIVALS)

    def test_the_gate_resolves_the_current_head_from_the_full_history(self):
        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain))
        lineage = resolve_builder_lineage(
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
            config=IDENTITY,
            repo=REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=chain[-1].resulting_head,
            episodes=arrivals,
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.episodes, builder_lineage.MAX_EPISODES)
        self.assertEqual(lineage.current_writer, "codex")

    def test_a_reviewer_overlapping_its_private_record_still_resolves(self):
        """The completed chain arrives once more from the local store."""

        root = git_free_tempdir(self, "code-mower-cumulative-")
        chain = self._chain(builder_lineage.MAX_EPISODES)
        for episode in chain:
            builder_lineage.record_episode(lane_handoff.lineage_root(root), episode)
        comments = self._cumulative_comments(chain)
        with mock.patch.dict("os.environ", {lane_handoff.STATE_DIR_ENV: str(root)}):
            arrivals = reviewer_lineage.reviewer_evidence(
                REPO, PR, authorities=(AUTHORITY,), fetch_comments=lambda: comments
            )
        # The reviewer path merges the two stores and collapses them itself.
        self.assertEqual(len(arrivals), builder_lineage.MAX_EPISODES)
        resolved = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH,
            head_sha=chain[-1].resulting_head, episodes=arrivals,
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.episodes, builder_lineage.MAX_EPISODES)

    def test_the_raw_public_and_private_union_resolves_at_the_bound(self):
        """A consumer that collapses nothing hands over the exact maximum."""

        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain)) + chain
        self.assertEqual(len(arrivals), builder_lineage.MAX_EPISODE_ARRIVALS)
        self.assertEqual(len(arrivals), 560)
        resolved = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH,
            head_sha=chain[-1].resulting_head, episodes=arrivals,
        )
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.episodes, builder_lineage.MAX_EPISODES)
        self.assertEqual(resolved.current_writer, "codex")

    def test_one_arrival_past_the_contract_is_still_refused(self):
        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain)) + chain
        arrivals = arrivals + (chain[-1],)
        self.assertEqual(len(arrivals), builder_lineage.MAX_EPISODE_ARRIVALS + 1)
        resolved = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH,
            head_sha=chain[-1].resulting_head, episodes=arrivals,
        )
        self.assertEqual(resolved.status, "conflict")
        self.assertEqual(resolved.reason, "episode_malformed")

    def test_a_disagreeing_duplicate_inside_the_full_history_fails_closed(self):
        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain))
        forged = _variant(chain[4], destination_lane="claude", source_lane="claude")
        resolved = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH,
            head_sha=chain[-1].resulting_head, episodes=arrivals + (forged,),
        )
        self.assertEqual(resolved.status, "conflict")
        self.assertEqual(resolved.reason, "episode_duplicated")

    def test_a_stale_full_history_waits_rather_than_resolving(self):
        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain))
        resolved = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=FIXED, episodes=arrivals,
        )
        self.assertEqual(resolved.status, "waiting")
        self.assertEqual(resolved.reason, "lineage_behind_head")

    def test_a_full_history_bound_to_another_branch_fails_closed(self):
        chain = self._chain(builder_lineage.MAX_EPISODES)
        arrivals = self._gate_episodes(self._cumulative_comments(chain))
        resolved = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch="codex/959-other",
            head_sha=chain[-1].resulting_head, episodes=arrivals,
        )
        self.assertEqual(resolved.status, "conflict")
        self.assertEqual(resolved.reason, "episode_unbound")

    def test_a_sequence_past_the_lineage_bound_never_constructs(self):
        """The distinct-episode bound is enforced per entry, as it arrives."""

        chain = self._chain(builder_lineage.MAX_EPISODES)
        with self.assertRaises(builder_lineage.LineageError):
            _variant(chain[-1], sequence=builder_lineage.MAX_EPISODES + 1)


class LaneStatusProjection(unittest.TestCase):
    """The Board/controller projection reads the same evidence as the gate."""

    def setUp(self):
        self.root = git_free_tempdir(self, "code-mower-status-")
        patched = mock.patch.dict(
            "os.environ",
            {
                "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
                reviewer_lineage.AUTHOR_EXCLUSION_ENV: json.dumps(IDENTITY),
                lane_handoff.STATE_DIR_ENV: str(self.root),
            },
        )
        patched.start()
        self.addCleanup(patched.stop)

    def test_the_configured_store_is_read_not_the_packaged_default(self):
        from code_mower import lane_status

        builder_lineage.record_episode(
            lane_handoff.lineage_root(self.root), takeover_episode()
        )
        projection = lane_status.builder_lineage_for(
            REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
        )
        self.assertEqual(projection["status"], "resolved")
        self.assertEqual(projection["current_writer"], "codex")

    def test_a_cross_host_projection_resolves_from_published_comments(self):
        """No local record at all -- the Board still sees the takeover."""

        from code_mower import lane_status

        projection = lane_status.builder_lineage_for(
            REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            labels=["builder:codex"],
            author="devin-ai-integration[bot]",
            comments=[published([takeover_episode()])],
        )
        self.assertEqual(projection["status"], "resolved")
        self.assertEqual(sorted(projection["contributors"]), ["codex", "devin"])

    def test_an_untrusted_marker_is_not_read_into_the_projection(self):
        from code_mower import lane_status

        projection = lane_status.builder_lineage_for(
            REPO,
            pr_number=PR,
            branch=BRANCH,
            head_sha=TAKEN,
            labels=[],
            author="devin-ai-integration[bot]",
            comments=[published([takeover_episode()], author=OUTSIDER)],
        )
        self.assertEqual(projection["contributors"], ["devin"])

    def test_the_status_command_asks_github_for_the_comments(self):
        from code_mower import lane_status

        requested = []

        def runner(args):
            requested.append(args)
            return []

        lane_status._remote(REPO, runner, __import__("datetime").datetime.now(
            __import__("datetime").UTC
        ), 5, 5, 30)
        self.assertTrue(
            any("comments" in str(arg) for args in requested for arg in args),
            "the pull request query must request published lineage comments",
        )


class ControllerDiagnostics(unittest.TestCase):
    """Unreadable evidence and an empty reviewer set are different problems.

    Both stop the controller, and both are fail-closed, but they send the owner
    to different repairs: one to re-record a broken lineage, one to configure a
    reviewer lane that did not write this diff. Collapsing the second into the
    first asks the owner to fix a record that is already correct.
    """

    def _report(self, config, pr):
        from code_mower import controller

        return controller.evaluate_controller_report(
            status_report=_status([pr]),
            ready_issues={"available": True, "errors": [], "issues": []},
            config=config,
            options=_options("manual"),
        )["decision"]

    def test_a_resolved_lineage_with_no_independent_reviewer_names_the_lanes(self):
        pr = _pr(builder="builder:codex", needs=["needs-codex-audit"])
        pr["builder_lineage"] = {
            "status": "resolved",
            "contributors": ["devin", "codex"],
            "current_writer": "codex",
            "owner_action": "",
        }
        decision = self._report(_only_codex_merge_reviewer(), pr)

        self.assertEqual(decision["decision_state"], "owner_action")
        self.assertEqual(decision["owner_action_kind"], "reviewer_lanes_missing")
        self.assertEqual(decision["stop_condition"], "reviewer_lanes_missing")
        self.assertEqual(decision["builder_lineage_status"], "resolved")
        self.assertEqual(sorted(decision["builder_contributors"]), ["codex", "devin"])

    def test_unresolved_lineage_still_reports_the_lineage_repair(self):
        pr = _pr(builder="builder:codex", needs=["needs-codex-audit"])
        pr["builder_lineage"] = {
            "status": "conflict",
            "contributors": [],
            "current_writer": "",
            "owner_action": "re-record the lineage from the verified handoff",
        }
        decision = self._report(_only_codex_merge_reviewer(), pr)

        self.assertEqual(decision["owner_action_kind"], "builder_lineage")
        self.assertEqual(decision["stop_condition"], "builder_lineage_unresolved")
        self.assertEqual(
            decision["next_detail"], "re-record the lineage from the verified handoff"
        )


class AutoRecordCli(unittest.TestCase):
    """The real ``code-mower builder auto-record`` path after a takeover."""

    def setUp(self):
        self.dir = git_free_tempdir(self, "code-mower-auto-record-")
        patched = mock.patch.dict(
            "os.environ", {"CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY}
        )
        patched.start()
        self.addCleanup(patched.stop)

    def _write(self, name: str, payload) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _run(self, *, comments) -> dict:
        from code_mower import builder_runs

        event = self._write(
            "event.json",
            {
                "pull_request": {
                    "number": PR,
                    "html_url": f"https://github.com/{REPO}/pull/{PR}",
                    "user": {"login": "devin-ai-integration[bot]"},
                    "head": {"ref": BRANCH, "sha": TAKEN},
                    "labels": [{"name": "builder:codex"}],
                    "body": "",
                },
                "repository": {"full_name": REPO},
            },
        )
        argv = [
            "auto-record",
            "--pr-json", str(event),
            "--repo", REPO,
            "--output", str(self.dir / "run.json"),
            "--force",
            "--json",
        ]
        if comments is not None:
            argv += ["--comments-json", str(self._write("comments.json", comments))]
        self.assertEqual(builder_runs.main(argv), 0)
        return json.loads((self.dir / "run.json").read_text(encoding="utf-8"))

    def test_a_taken_over_pull_request_is_attributed_to_the_current_writer(self):
        event = self._run(comments=[published([takeover_episode()])])

        dimensions = event["dimensions"]
        self.assertEqual(event["provider"], "codex")
        self.assertEqual(dimensions["builder_executor"], "chatgpt-codex-connector")
        self.assertEqual(dimensions["pr_author"], "devin-ai-integration[bot]")
        self.assertEqual(dimensions["builder_lineage_status"], "resolved")
        self.assertEqual(dimensions["builder_current_writer"], "codex")
        self.assertEqual(
            sorted(dimensions["builder_contributors"]), ["codex", "devin"]
        )
        self.assertIn("builder_lineage:codex", dimensions["builder_inference_signals"])

    def test_without_published_lineage_it_records_the_unresolved_answer(self):
        """The defect this replaces, kept as an explicit regression.

        Opener and label disagree and nothing explains why, so the resolver
        names no writer. Auto-record still attributes to the opener, because
        that is all it knows -- but the recorded lineage says so rather than
        presenting the guess as settled.
        """

        event = self._run(comments=[])

        self.assertEqual(event["provider"], "devin")
        self.assertEqual(event["dimensions"]["builder_current_writer"], "")
        self.assertEqual(event["dimensions"]["builder_lineage_status"], "conflict")

    def test_an_untrusted_marker_does_not_move_the_attribution(self):
        event = self._run(
            comments=[published([takeover_episode()], author=OUTSIDER)]
        )

        self.assertEqual(event["provider"], "devin")
        self.assertNotIn(
            "builder_lineage:codex", event["dimensions"]["builder_inference_signals"]
        )


class SaasReviewPath(unittest.TestCase):
    """``pull_request_review`` resolves the same evidence as every other path."""

    def _adapter(self):
        return SimpleNamespace(
            name="greptile", event_type="pull_request_review", opt_in_required=False,
            label_prefix="greptile", needs_label="n", done_label="d", blocked_label="b",
            supported_event_types=("pull_request_review",),
            requires_review_comments=False, review_comments_page_cap=5,
            check_run_done_requires_absent_same_head_review=False,
            is_opted_in=lambda labels: True, is_review_author=lambda author: True,
            is_check_run_author=lambda check_run: True,
            is_check_run_name=lambda check_run: True,
            token_env_vars=("GITHUB_TOKEN",),
        )

    def _main(self, event_path: Path, *, comments, fail: bool = False):
        from code_mower import saas_reviewer_labeler as labeler

        seen = {}

        def capture(*args, **kwargs):
            seen.update(kwargs)
            return None, "captured"

        def fetch_comments(repo, number, *, tokens, page_cap):
            if fail:
                raise labeler.GitHubRequestError("GET", "/comments", 503, "")
            return comments

        with mock.patch.object(labeler, "load_adapter", lambda name: self._adapter()), \
                mock.patch.object(labeler, "github_tokens_from_env", lambda *a: ("t",)), \
                mock.patch.object(
                    labeler, "fetch_pull_request",
                    lambda repo, number, **kw: {
                        "labels": [{"name": "builder:codex"}],
                        "user": {"login": "devin-ai-integration[bot]"},
                        "body": "",
                        "head": {"sha": TAKEN, "ref": BRANCH},
                    },
                ), \
                mock.patch.object(labeler, "fetch_issue_comments", fetch_comments), \
                mock.patch.object(labeler, "resolve_label_decision", capture):
            code = labeler.main(["--adapter", "greptile"])
        return code, seen

    def test_the_review_path_carries_published_episodes(self):
        directory = git_free_tempdir(self, "code-mower-saas-")
        event_path = directory / "event.json"
        event_path.write_text(
            json.dumps({
                "action": "submitted",
                "pull_request": {"number": PR},
                "review": {"id": 1},
            }),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_EVENT_NAME": "pull_request_review",
            "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
        }):
            code, seen = self._main(
                event_path, comments=[published([takeover_episode()])]
            )

        self.assertEqual(code, 0)
        self.assertEqual(seen["issue_comments"], [published([takeover_episode()])])
        self.assertEqual(seen["current_head_sha"], TAKEN)
        self.assertEqual(seen["head_branch"], BRANCH)
        self.assertEqual(seen["decision_authorities"], (AUTHORITY,))

    def test_an_unreadable_fetch_stops_rather_than_labelling_on_identity(self):
        directory = git_free_tempdir(self, "code-mower-saas-fail-")
        event_path = directory / "event.json"
        event_path.write_text(
            json.dumps({
                "action": "submitted",
                "pull_request": {"number": PR},
                "review": {"id": 1},
            }),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_EVENT_NAME": "pull_request_review",
            "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
        }):
            code, seen = self._main(event_path, comments=[], fail=True)

        self.assertEqual(code, 0)
        self.assertEqual(seen, {}, "no decision is resolved on unreadable evidence")


class PinnedBaseSeam(unittest.TestCase):
    """#963 consumes #955's already-pinned base; it never re-resolves a name."""

    def test_the_claude_wrapper_admits_after_the_pin_and_from_config_base_ref(self):
        from code_mower import claude_audit_pr

        source = Path(claude_audit_pr.__file__).read_text(encoding="utf-8")
        pin = source.index("config, base_ref=diff_context.fetched_base_ref or config.base_ref")
        authorities = source.index("trusted_ref=config.base_ref,", pin)
        admission = source.index('_require_independent_review(\n        "claude"', pin)
        self.assertLess(pin, authorities)
        self.assertLess(authorities, admission)

    def test_the_codex_wrapper_admits_after_its_own_pin(self):
        from code_mower import codex_audit_pr

        source = Path(codex_audit_pr.__file__).read_text(encoding="utf-8")
        pin = source.index("config, base_ref=_pinned_base_revision(local_repo, config.base_ref)")
        authorities = source.index("trusted_ref=config.base_ref,", pin)
        admission = source.index('_require_independent_review(\n        "codex"', pin)
        self.assertLess(pin, authorities)
        self.assertLess(authorities, admission)

    def test_neither_wrapper_fetches_the_base_a_second_time_for_lineage(self):
        for module_name, fetcher in (
            ("claude_audit_pr", "_fetch_base_sha_for_diff"),
            ("codex_audit_pr", "_fetch_base_ref"),
        ):
            with self.subTest(module=module_name):
                module = __import__(f"code_mower.{module_name}", fromlist=[module_name])
                source = Path(module.__file__).read_text(encoding="utf-8")
                admission = source.index("authorities=decision_authorities,")
                self.assertNotIn(fetcher + "(", source[admission:])

    def test_a_ref_that_moves_after_the_fetch_does_not_change_the_authorities(self):
        """The admission reads the pinned SHA, so a later push cannot rewrite it.

        Decision authorities are what makes published lineage readable, so
        re-resolving a mutable name here would let a commit landing mid-audit
        change which markers this reviewer trusts.
        """

        import subprocess

        from code_mower import claude_audit_pr

        repo = git_free_tempdir(self, "code-mower-pinned-base-")

        def git(*args):
            subprocess.run(
                ["git", *args], cwd=repo, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "lane@example.invalid")
        git("config", "user.name", "Lane")
        (repo / "code-mower.yml").write_text(
            "decisions:\n  authorities:\n    - codemower-ai\n", encoding="utf-8"
        )
        git("add", "code-mower.yml")
        git("commit", "-qm", "pinned base")
        pinned = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

        before = claude_audit_pr._decision_authorities_for_repo(
            repo, (), trusted_ref=pinned
        )
        self.assertIn("codemower-ai", before)

        # The name moves under the audit; the pinned revision does not.
        (repo / "code-mower.yml").write_text(
            "decisions:\n  authorities:\n    - somebody-else\n", encoding="utf-8"
        )
        git("add", "code-mower.yml")
        git("commit", "-qm", "moved")

        self.assertEqual(
            claude_audit_pr._decision_authorities_for_repo(repo, (), trusted_ref=pinned),
            before,
        )
        self.assertIn(
            "somebody-else",
            claude_audit_pr._decision_authorities_for_repo(
                repo, (), trusted_ref="main"
            ),
            "the mutable name really did move, so the pin is what held",
        )


if __name__ == "__main__":
    unittest.main()
