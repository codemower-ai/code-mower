"""Raw ingress, shared bounds, public readback and exact effect ordering."""
from pathlib import Path
import tempfile
import unittest

from code_mower.builder_lineage import Authorities, ContractError, History, Identity
from code_mower.builder_lineage_producer import (
    ProducerRefusal, Snapshot, decode_transport, fetch_history, observe, publish, selected_history,
)
from code_mower.builder_runs import record_lineage_builder
from lineage_producer_fixtures import (
    AUTHORITY, GitHubIO, POLICY, TRANSPORT, comments, episode, observation_args, sha, target,
)


class PublicationTests(unittest.TestCase):
    def publish(self, io, private=None, **changes):
        args = dict(target=target(), identity=POLICY, authorities=AUTHORITY,
                    private=[episode()] if private is None else private)
        args.update(changes)
        return publish(io, **args)

    def test_success_semantic_duplicate_and_idempotent_replay(self):
        io = GitHubIO()
        io.readback = comments([episode()]) * 2
        outcome = self.publish(io)
        self.assertTrue(outcome.comment_posted)
        self.assertEqual(io.effects, ["snapshot", "history", "snapshot", "post", "history", "snapshot", "labels", "snapshot"])
        io.effects.clear()
        outcome = self.publish(io)
        self.assertFalse(outcome.comment_posted)
        self.assertFalse(outcome.labels_attempted)
        self.assertNotIn("post", io.effects)
        self.assertNotIn("labels", io.effects)

    def test_initial_ingress_failure_means_no_post_labels_or_artifact(self):
        malformed = [None, {}, [None], [False], [[{}]], [{"body": None}], [{"user": []}],
                     [{"body": 1}], [{"author": {"login": 4}}]]
        for raw in malformed:
            with self.subTest(raw=raw):
                io = GitHubIO()
                io.public = raw
                with self.assertRaises(ProducerRefusal) as raised:
                    self.publish(io)
                self.assertFalse(raised.exception.comment_posted)
                self.assertEqual(io.effects, ["snapshot", "history"])
        for raw in ([], [{"user": None}], [{"author": None}], [{}]):
            io = GitHubIO()
            io.public = raw
            self.assertTrue(self.publish(io).comment_posted)

    def test_selected_history_never_uses_numeric_rest_count(self):
        for selected in (None, 3, {}, [1]):
            with self.assertRaises(ContractError):
                selected_history({"comments": 3}, selected=selected)
        self.assertEqual(selected_history({"comments": 3}, selected=[]), History([]))
        for raw in (None, {}, [[{}], None], [[{}], {}], [None]):
            with self.assertRaises(ContractError):
                History.from_pages(raw)
        self.assertEqual(History.from_pages([[], [{"user": None}]]), History([{"user": None}]))

    def test_marker_trust_shapes_legacy_and_no_authority(self):
        body = comments([episode()])[0]["body"]
        invalid = [body.replace('"schema":', '"schema":"duplicate","schema":'),
            body.replace('"episodes":[', '"episodes":[],"extra":['),
            '<!-- CODE_MOWER_BUILDER_LINEAGE: {"schema":"code_mower.builderLineage.v1","episodes":[]} -->',
            '<!-- CODE_MOWER_BUILDER_LINEAGE: broken -->', body + body,
            body.replace('CODE_MOWER_BUILDER_LINEAGE:', 'CODE_MOWER_BUILDER_LINEAGE'),
            body.replace('"sequence":1', '"schema":"old","sequence":1')]
        for bad in invalid:
            io = GitHubIO()
            io.public = [{"user": {"login": "lineage-publisher[bot]"}, "body": bad}]
            with self.subTest(body=bad), self.assertRaises(ProducerRefusal):
                self.publish(io)
            self.assertNotIn("post", io.effects)
            self.assertNotIn("labels", io.effects)
        io = GitHubIO()
        io.public = comments([episode()], author="outsider")
        self.assertTrue(self.publish(io).comment_posted)
        io = GitHubIO()
        with self.assertRaises(ProducerRefusal):
            self.publish(io, authorities=Authorities([]))
        self.assertNotIn("post", io.effects)

    def test_post_then_missing_untrusted_conflicting_or_malformed_readback(self):
        bad_readbacks = [[], comments([episode()], author="outsider"),
            comments([episode(expected_head=sha(9))]),
            [{"body": "<!-- CODE_MOWER_BUILDER_LINEAGE: broken -->", "user": {"login": "lineage-publisher[bot]"}}],
            [{"body": False}]]
        for raw in bad_readbacks:
            io = GitHubIO()
            io.readback = raw
            with self.subTest(raw=raw), self.assertRaises(ProducerRefusal) as caught:
                self.publish(io)
            self.assertTrue(caught.exception.comment_posted)
            self.assertFalse(caught.exception.labels_attempted)
            self.assertEqual(io.effects, ["snapshot", "history", "snapshot", "post", "history"])

    def test_public_only_missing_final_control(self):
        io = GitHubIO(2)
        io.public = comments([episode()])
        io.readback = comments([episode()])
        with self.assertRaises(ProducerRefusal) as caught:
            self.publish(io, private=[episode(), episode(2)], target=target(2))
        self.assertTrue(caught.exception.comment_posted)
        self.assertNotIn("labels", io.effects)

    def test_fresh_label_failure_and_head_race_ordering(self):
        for stage, expected in ((1, []), (2, ["history"]), (3, ["post", "history"]), (4, ["labels"])):
            io = GitHubIO()
            io.fail_snapshot = stage
            with self.subTest(stage=stage), self.assertRaises(ProducerRefusal) as caught:
                self.publish(io)
            for effect in expected:
                self.assertIn(effect, io.effects)
            self.assertEqual(caught.exception.comment_posted, stage >= 3)
            self.assertEqual(caught.exception.labels_attempted, stage == 4)
            if stage <= 3:
                self.assertNotIn("labels", io.effects)
        for stage in (1, 2, 3, 4):
            io = GitHubIO()
            original = io.snapshot
            def snapshot(t, original=original, io=io, stage=stage):
                value = original(t)
                return Snapshot(target(9), value.author, value.labels) if io.snapshots == stage else value
            io.snapshot = snapshot
            with self.subTest(race=stage), self.assertRaises(ProducerRefusal):
                self.publish(io)
            if stage <= 2:
                self.assertNotIn("post", io.effects)
            if stage <= 3:
                self.assertNotIn("labels", io.effects)

    def test_exact_target_and_empty_chain_branch_policy(self):
        for changes in ({"repo": "wrong/repo"}, {"pr_number": 43}, {"branch": "codex/topic"}, {"head_sha": sha(9)}):
            io = GitHubIO()
            with self.assertRaises(ProducerRefusal):
                self.publish(io, target=target(**changes))
            self.assertEqual(io.effects, ["snapshot"])
        args = observation_args()
        args.update(private=[], author="unmapped", labels=["builder:claude"])
        with self.assertRaises(ProducerRefusal):
            observe(**args)
        for bad in (None, {}, {"repo": "owner/repo"}):
            with self.assertRaises(ProducerRefusal):
                observe(**(args | {"target": bad}))

    def test_identity_aliases_and_reviewer_floor_are_preserved(self):
        policy = POLICY.with_reviewer_floor("claude", ["reviewer-bot"])
        self.assertEqual(policy.branch_prefixes, POLICY.branch_prefixes)
        self.assertTrue(policy.require_verified_lineage)
        self.assertNotIn("reviewer-bot", AUTHORITY.accounts)
        with self.assertRaises(ContractError):
            POLICY.with_reviewer_floor("claude", ["source-bot"])
        with self.assertRaises(ContractError):
            Identity.from_mapping({"authors": {"BOT": "codex", " bot ": "claude"}})


class BoundsTests(unittest.TestCase):
    def test_single_560_budget_and_561st_probe_without_562nd_read(self):
        episodes = [episode(n) for n in range(1, 33)]
        public = History([row for n in range(1, 33) for row in comments(episodes[:n])])
        args = observation_args(32) | {"history": public, "private": episodes}
        observation = observe(**args)
        self.assertEqual(observation.chain.raw_arrival_count, 560)
        self.assertEqual(len(observation.chain.episodes), 32)
        read = []
        def arrivals():
            for n in range(34):
                read.append(n)
                yield episodes[n] if n < 32 else episodes[-1]
        with self.assertRaisesRegex(ContractError, "arrival budget"):
            observe(**(args | {"private": arrivals()}))
        self.assertEqual(len(read), 33)
        # Intermediate public head is not separately resolved before later private evidence.
        args["history"] = History(comments(episodes[:16]))
        self.assertEqual(observe(**args).decision.status, "ready")

    def test_finite_page_terminal_full_cap_extra_probe_and_failure(self):
        for pages, success, expected in (([[]], True, [1]),
                ([[{}, {}], [{}]], True, [1, 2]),
                ([[{}, {}], [{}, {}], []], True, [1, 2, 3]),
                ([[{}, {}], [{}, {}], [{}]], False, [1, 2, 3]),
                ([[{}, {}], None], False, [1, 2]),
                ([[{}, {}], {}], False, [1, 2])):
            calls = []
            def fetch(page, size, calls=calls, pages=pages):
                calls.append(page)
                return pages[page-1]
            if success:
                fetch_history(fetch, page_size=2, max_pages=2)
            else:
                with self.assertRaises(ValueError):
                    fetch_history(fetch, page_size=2, max_pages=2)
            self.assertEqual(calls, expected)
        def failed(page, size):
            raise OSError("request failed")
        with self.assertRaises(ProducerRefusal):
            fetch_history(failed)
        for raw in ('null', '{"x":1,"x":2}', '[{"body":null}]'):
            with self.assertRaises(ValueError):
                History(decode_transport(raw))


class AttributionTests(unittest.TestCase):
    def test_verified_writer_and_actual_local_hosted_transport_remain_distinct(self):
        from code_mower.builder_lineage_producer import Transport
        for transport in (TRANSPORT, Transport("devin", "devin_cli", "devin_cli", "local_cli"),
                          Transport("devin", "devin", "devin", "hosted_async_builder")):
            args = observation_args()
            args["private"] = [episode(destination_lane=transport.lane)]
            observation = observe(**args)
            with tempfile.TemporaryDirectory() as tmp:
                event = record_lineage_builder(observation, transport, Path(tmp)/"event.json", created_at="2026-01-01T00:00:00+00:00")
            self.assertEqual(event["provider"], transport.provider)
            self.assertEqual(event["tool"]["executor"], transport.executor)
            self.assertEqual(event["tool"]["integration"], transport.integration)

    def test_no_lineage_control_and_malformed_selected_history_no_artifact(self):
        from code_mower.builder_lineage_producer import Transport
        args = observation_args() | {"private": []}
        observation = observe(**args)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/"event.json"
            event = record_lineage_builder(observation, Transport("codex", "codex", "codex_cli", "local_cli"),
                                          output, created_at="2026-01-01T00:00:00+00:00")
            self.assertEqual(event["dimensions"]["lineage"]["episode_count"], 0)
            output.unlink()
            with self.assertRaises(ValueError):
                selected_history({"comments": 100, "body": "takeover by claude"}, selected=None)
            self.assertFalse(output.exists())
