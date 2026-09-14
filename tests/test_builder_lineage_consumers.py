"""Consumer-level regressions for exact-head builder lineage.

These exercise the production seams the resolver's own unit tests cannot: the
generated product gate's dependency set, the runner's publish-then-reconcile
ordering, the trailer/SaaS labeler callers, continuation recording after a
takeover, and the configured handoff directory. Each one is written from the
#959 shape -- a Devin-opened PR taken over by Codex, audited by an independent
Claude -- because that is the case every single-signal answer got wrong.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import builder_lineage, init, lane_delivery, lane_handoff  # noqa: E402
from code_mower.audit_labeler_lib import (  # noqa: E402
    NO_LINEAGE,
    author_exclusion_reason,
    builder_identity_matches,
    lineage_context,
    lineage_marker_author_trust,
)
from code_mower.provider_runners import lineage as reviewer_lineage  # noqa: E402

REPO = "codemower-ai/code-mower"
PR = 959
BRANCH = "devin/959-thing"
OPENED = "a" * 40
TAKEN = "b" * 40
FIXED = "c" * 40

IDENTITY = {
    "enabled": True,
    "labels": {"builder:devin": "devin", "builder:codex": "codex", "builder:claude": "claude"},
    "authors": {"devin-ai-integration[bot]": "devin", "codex[bot]": "codex"},
}


def git_free_tempdir(case: unittest.TestCase, prefix: str = "code-mower-lineage-") -> Path:
    """A private store root outside any checkout, as the context store demands."""

    base = Path(tempfile.gettempdir()).resolve()
    if any((parent / ".git").exists() for parent in (base, *base.parents)):
        base = Path("/tmp").resolve()
    if any((parent / ".git").exists() for parent in (base, *base.parents)):
        case.skipTest("no Git-free temporary directory is available here")
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base))).resolve()
    case.addCleanup(__import__("shutil").rmtree, path, True)
    return path


def takeover_episode(sequence: int = 1, resulting: str = TAKEN) -> builder_lineage.ContributionEpisode:
    """The verified Devin -> Codex takeover that produced the current head."""

    return builder_lineage.ContributionEpisode(
        sequence=sequence,
        kind=builder_lineage.HANDOFF_KIND,
        repo=REPO,
        pr_number=PR,
        branch=BRANCH,
        source_lane="devin",
        destination_lane="codex",
        expected_head=OPENED,
        resulting_head=resulting,
        writer_state="terminated",
    )


class GeneratedProductSupportFiles(unittest.TestCase):
    """codex:5f53e76584997c80c318 -- the gate helper's dependency must travel."""

    def test_builder_lineage_is_a_generated_product_support_file(self):
        targets = {target for target, _, _, _ in init.PRODUCT_SUPPORT_FILES}
        self.assertIn("tools/audit_labeler_lib.py", targets)
        self.assertIn("tools/builder_lineage.py", targets)

    def test_every_import_of_the_helper_is_copied_alongside_it(self):
        """Whatever audit_labeler_lib imports has to be in the same list."""

        sources = {
            target: package_copy_from
            for target, package_copy_from, _, _ in init.PRODUCT_SUPPORT_FILES
            if target.startswith("tools/") and target.endswith(".py")
        }
        helper = Path("src/code_mower") / sources["tools/audit_labeler_lib.py"]
        text = helper.read_text(encoding="utf-8")
        for module in ("builder_lineage", "decisions", "context_review"):
            self.assertTrue(
                f"from {module} import" in text or f"import {module} as" in text,
                f"{module} is not imported by the helper",
            )
            self.assertIn(f"tools/{module}.py", sources)

    def test_generated_gate_imports_its_helper_without_the_package(self):
        """A product gate runner has no code_mower installed. Prove it works."""

        if True:
            tmp = git_free_tempdir(self)
            root = Path(tmp)
            tools = root / "tools"
            tools.mkdir()
            (tools / "__init__.py").write_text("", encoding="utf-8")
            for target, package_copy_from, _, _ in init.PRODUCT_SUPPORT_FILES:
                if not (target.startswith("tools/") and target.endswith(".py")):
                    continue
                source = Path("src/code_mower") / package_copy_from
                if not source.exists():  # templated wrappers, not package modules
                    continue
                (root / target).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            # Exactly what a generated gate step does: run a script from the
            # product repository root, with no Code Mower package anywhere on
            # the path, and import the helper plus its dependency.
            (root / "probe.py").write_text(
                "import tools.audit_labeler_lib as lib\n"
                "print(lib.builder_identity_matches("
                "labels=['builder:codex'], author='codex[bot]', text='',"
                " config={'enabled': True, 'labels': {'builder:codex': 'codex'},"
                " 'authors': {'codex[bot]': 'codex'}}))\n",
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONHOME"}
            }
            result = subprocess.run(
                [sys.executable, "probe.py"],
                cwd=root, capture_output=True, text=True, timeout=120, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("codex", result.stdout)


class ConfiguredHandoffDirectory(unittest.TestCase):
    """codex:69acb7733dd8b7ba1b31 -- one store for recording and admission."""

    def test_reviewer_resolves_the_configured_directory(self):
        if True:
            tmp = git_free_tempdir(self)
            configured = tmp / "configured"
            builder_lineage.record_episode(
                lane_handoff.lineage_root(configured), takeover_episode()
            )
            with mock.patch.dict(
                os.environ, {lane_handoff.STATE_DIR_ENV: str(configured)}, clear=False
            ):
                episodes = reviewer_lineage.recorded_episodes(REPO, PR)
            self.assertEqual(len(episodes), 1)
            self.assertEqual(episodes[0].destination_lane, "codex")

    def test_unconfigured_reviewer_still_reads_the_default_root(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lane_handoff.STATE_DIR_ENV, None)
            self.assertEqual(lane_handoff.configured_root(), lane_handoff.default_root())

    def test_a_relative_configured_directory_fails_closed(self):
        with mock.patch.dict(
            os.environ, {lane_handoff.STATE_DIR_ENV: "relative/store"}, clear=False
        ):
            with self.assertRaises(builder_lineage.LineageError):
                reviewer_lineage.recorded_episodes(REPO, PR)

    def test_recording_and_admission_agree_on_the_configured_store(self):
        """The bug: the reviewer read an empty default and admitted a contributor."""

        if True:
            tmp = git_free_tempdir(self)
            configured = tmp / "configured"
            builder_lineage.record_episode(
                lane_handoff.lineage_root(configured), takeover_episode()
            )
            pr_meta = {
                "labels": [{"name": "builder:codex"}],
                "user": {"login": "devin-ai-integration[bot]"},
                "head": {"ref": BRANCH},
            }
            with mock.patch.dict(
                os.environ, {lane_handoff.STATE_DIR_ENV: str(configured)}, clear=False
            ):
                episodes = reviewer_lineage.recorded_episodes(REPO, PR)
            for lane, admitted in (("codex", False), ("devin", False), ("claude", True)):
                decision = reviewer_lineage.reviewer_admission(
                    lane, repo=REPO, pr_number=PR, pr_meta=pr_meta, head_sha=TAKEN,
                    episodes=episodes, identity=IDENTITY,
                )
                self.assertEqual(decision["admitted"], admitted, lane)


class ContinuationDeliveries(unittest.TestCase):
    """codex:7b1f8c5e122de3ff7727 -- an ordinary fix round after a takeover."""

    def setUp(self):
        self.root = git_free_tempdir(self) / "handoffs"
        builder_lineage.record_episode(lane_handoff.lineage_root(self.root), takeover_episode())

    def record(self, **overrides):
        payload = dict(
            repo=REPO, pr_number=PR, branch=BRANCH, lane="codex",
            expected_head=TAKEN, resulting_head=FIXED, delivered=True,
        )
        payload.update(overrides)
        return lane_handoff.record_continuation(self.root, **payload)

    def test_a_new_head_is_recorded_and_resolves_at_that_head(self):
        outcome = self.record()
        self.assertTrue(outcome["recorded"])
        self.assertEqual(outcome["sequence"], 2)
        episodes = builder_lineage.load_episodes(
            lane_handoff.lineage_root(self.root), REPO, PR
        )
        lineage = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=FIXED,
            episodes=episodes, opener_lane="devin", label_lanes=("codex",),
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.contributors, ("devin", "codex"))

    def test_without_a_continuation_the_lineage_stays_behind_the_head(self):
        """This is the defect: the same round with nothing recorded."""

        episodes = builder_lineage.load_episodes(
            lane_handoff.lineage_root(self.root), REPO, PR
        )
        lineage = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=FIXED, episodes=episodes,
        )
        self.assertEqual(lineage.status, "waiting")
        self.assertEqual(lineage.reason, "lineage_behind_head")

    def test_replay_is_idempotent(self):
        self.assertTrue(self.record()["recorded"])
        replay = self.record()
        self.assertFalse(replay["recorded"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["sequence"], 2)
        self.assertEqual(
            len(builder_lineage.load_episodes(lane_handoff.lineage_root(self.root), REPO, PR)),
            2,
        )

    def test_stale_evidence_records_nothing(self):
        outcome = self.record(expected_head="d" * 40)
        self.assertFalse(outcome["recorded"])
        self.assertEqual(outcome["reason"], "continuation_unchained")

    def test_a_lane_that_is_not_the_current_writer_records_nothing(self):
        outcome = self.record(lane="devin")
        self.assertFalse(outcome["recorded"])
        self.assertEqual(outcome["reason"], "not_current_writer")

    def test_an_undelivered_or_unmoved_round_records_nothing(self):
        self.assertEqual(self.record(delivered=False)["reason"], "delivery_unvalidated")
        self.assertEqual(self.record(resulting_head=TAKEN)["reason"], "head_unchanged")

    def test_an_ordinary_single_builder_pr_records_nothing(self):
        if True:
            tmp = git_free_tempdir(self)
            outcome = lane_handoff.record_continuation(
                Path(tmp), repo=REPO, pr_number=777, branch=BRANCH, lane="claude",
                expected_head=OPENED, resulting_head=TAKEN, delivered=True,
            )
        self.assertFalse(outcome["recorded"])
        self.assertEqual(outcome["reason"], "no_recorded_lineage")

    def test_a_continuation_is_not_a_handoff(self):
        """It cannot be forged into one, or manufacture a new contributor."""

        self.record()
        episodes = builder_lineage.load_episodes(
            lane_handoff.lineage_root(self.root), REPO, PR
        )
        self.assertEqual(episodes[1].kind, builder_lineage.CONTINUATION_KIND)
        self.assertEqual(episodes[1].source_lane, episodes[1].destination_lane)
        with self.assertRaises(builder_lineage.LineageError):
            builder_lineage.ContributionEpisode(
                sequence=2, kind=builder_lineage.HANDOFF_KIND, repo=REPO, pr_number=PR,
                branch=BRANCH, source_lane="codex", destination_lane="codex",
                expected_head=TAKEN, resulting_head=FIXED, writer_state="terminated",
            )

    def test_lineage_that_starts_with_a_continuation_fails_closed(self):
        lone = builder_lineage.ContributionEpisode(
            sequence=1, kind=builder_lineage.CONTINUATION_KIND, repo=REPO, pr_number=PR,
            branch=BRANCH, source_lane="codex", destination_lane="codex",
            expected_head=OPENED, resulting_head=TAKEN,
            writer_state=builder_lineage.CONTINUATION_WRITER_STATE,
        )
        lineage = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN, episodes=(lone,),
        )
        self.assertEqual(lineage.status, "conflict")


class ClassifyRecordsContinuations(unittest.TestCase):
    """The producer path the runner actually calls, not a helper in isolation."""

    def test_classify_records_a_continuation_without_a_handoff(self):
        if True:
            tmp = git_free_tempdir(self)
            root = tmp / "handoffs"
            builder_lineage.record_episode(lane_handoff.lineage_root(root), takeover_episode())
            recorded = {}

            def fake(state_dir, **kwargs):
                recorded.update(kwargs)
                recorded["state_dir"] = state_dir
                return {"recorded": True, "reason": "recorded", "sequence": 2}

            args = SimpleNamespace(
                before=None, after=None, provider_exit=0, declared_outcome="delivered",
                supervision="none", handoff=None, handoff_state_dir=root, lane="codex",
                repo=REPO, signal=[], elapsed_seconds=1, user_interventions=0,
                output=None, force=True, json=True,
            )
            before = SimpleNamespace(head_sha=TAKEN, kind="pr", number=PR, branch=BRANCH)
            after = SimpleNamespace(head_sha=FIXED, kind="pr", number=PR, branch=BRANCH)
            with mock.patch.object(lane_delivery, "_load_state", side_effect=[before, after]), \
                    mock.patch.object(
                        lane_delivery, "classify_delivery",
                        return_value=SimpleNamespace(
                            delivered=True, transition="head_moved", reason="ok",
                            as_dict=lambda: {"delivered": True},
                        ),
                    ), \
                    mock.patch.object(lane_delivery, "build_delivery_outcome_event",
                                      return_value={"event_id": "e"}), \
                    mock.patch.object(lane_delivery, "write_delivery_outcome_event"), \
                    mock.patch.object(lane_handoff, "record_continuation", fake):
                lane_delivery._classify_main(args)
        self.assertEqual(recorded["lane"], "codex")
        self.assertEqual(recorded["expected_head"], TAKEN)
        self.assertEqual(recorded["resulting_head"], FIXED)
        self.assertTrue(recorded["delivered"])


class PublishBeforeReconcile(unittest.TestCase):
    """codex:4725ce39cb69b7ac68e7 -- the gate reads comments, not the store."""

    def setUp(self):
        self.root = git_free_tempdir(self) / "handoffs"
        builder_lineage.record_episode(lane_handoff.lineage_root(self.root), takeover_episode())
        self.published: list[str] = []
        self.labelled: list[tuple] = []

    def run_lineage(self, *, head=TAKEN, labels=("builder:devin",), bodies=None):
        args = SimpleNamespace(
            repo=REPO, pr=str(PR), branch=BRANCH, head=head, labels=list(labels),
            author="devin-ai-integration[bot]", state_dir=self.root,
            identity_json=json.dumps(IDENTITY), publish=True, reconcile_labels=True,
            json=True,
        )
        return lane_delivery._lineage_main(
            args,
            head=lambda repo, number: head,
            labels=lambda repo, number, add, remove: self.labelled.append((add, remove)),
            comment_bodies=lambda repo, number: tuple(bodies or self.published),
            publish_comment=lambda repo, number, body: self.published.append(body),
        )

    def test_evidence_is_published_and_then_the_label_moves(self):
        self.assertEqual(self.run_lineage(), 0)
        self.assertEqual(len(self.published), 1)
        marker = self.published[0]
        self.assertIn(builder_lineage.LINEAGE_MARKER, marker)
        self.assertEqual(self.labelled, [(("builder:codex",), ("builder:devin",))])

    def test_the_published_marker_is_what_the_gate_reads(self):
        self.run_lineage()
        episodes = builder_lineage.episodes_from_comment_body(self.published[0])
        self.assertEqual(len(episodes), 1)
        lineage = builder_lineage.resolve_lineage(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=TAKEN, episodes=episodes,
            opener_lane="devin", label_lanes=("codex",),
        )
        self.assertEqual(lineage.status, "resolved")
        self.assertEqual(lineage.current_writer, "codex")
        self.assertEqual(lineage.contributors, ("devin", "codex"))

    def test_publication_carries_only_bounded_metadata(self):
        self.run_lineage()
        payload = json.loads(
            self.published[0].split(builder_lineage.LINEAGE_MARKER, 1)[1].rsplit("-->", 1)[0]
        )
        for episode in payload["episodes"]:
            self.assertEqual(
                set(episode), set(builder_lineage.EPISODE_FIELDS),
                "no field beyond the bounded episode contract may be published",
            )
        body = self.published[0].lower()
        # Built rather than written out: the privacy scanner reads this file
        # too, and a literal host path here is the thing it exists to reject.
        for leaked in ("/users/", "/private/" + "tmp", "session", "prompt", "token"):
            self.assertNotIn(leaked, body)

    def test_publication_is_idempotent(self):
        self.run_lineage()
        already = list(self.published)
        self.assertEqual(self.run_lineage(bodies=already), 0)
        self.assertEqual(self.published, already)

    def test_unresolved_lineage_publishes_nothing_and_moves_no_label(self):
        self.assertEqual(self.run_lineage(head=FIXED), 3)
        self.assertEqual(self.published, [])
        self.assertEqual(self.labelled, [])

    def test_a_failed_publication_leaves_the_label_alone(self):
        def explode(repo, number, body):
            raise subprocess.CalledProcessError(1, "gh")

        args = SimpleNamespace(
            repo=REPO, pr=str(PR), branch=BRANCH, head=TAKEN, labels=["builder:devin"],
            author="devin-ai-integration[bot]", state_dir=self.root,
            identity_json=json.dumps(IDENTITY), publish=True, reconcile_labels=True,
            json=True,
        )
        with self.assertRaises(subprocess.CalledProcessError):
            lane_delivery._lineage_main(
                args,
                head=lambda repo, number: TAKEN,
                labels=lambda repo, number, add, remove: self.labelled.append((add, remove)),
                comment_bodies=lambda repo, number: (),
                publish_comment=explode,
            )
        self.assertEqual(self.labelled, [])


class LabelerCallersCarryLineage(unittest.TestCase):
    """codex:9bac84962f544d9fa4bb -- trusted evidence reaches the labelers."""

    def setUp(self):
        marker = builder_lineage.lineage_comment_marker((takeover_episode(),))
        self.comments = [
            {"user": {"login": "codemower-ai"}, "body": "lineage\n" + marker},
            {"user": {"login": "random-person"}, "body": "hello"},
        ]

    def context(self, comments=None, head=TAKEN):
        return lineage_context(
            repo=REPO, pr_number=PR, branch=BRANCH, head_sha=head,
            comments=self.comments if comments is None else comments,
            trusted_author=lineage_marker_author_trust(authorities=("codemower-ai",)),
        )

    def test_an_independent_claude_reviewer_may_update_its_done_label(self):
        """Devin author + reconciled Codex label used to look like a conflict."""

        self.assertIsNone(
            author_exclusion_reason(
                lane_name="claude", labels=["builder:codex"],
                author="devin-ai-integration[bot]", text="", config=IDENTITY,
                lineage=self.context(),
            )
        )

    def test_identity_only_resolution_would_have_skipped_it(self):
        """The defect the finding describes, kept as an explicit regression."""

        self.assertEqual(
            author_exclusion_reason(
                lane_name="claude", labels=["builder:codex"],
                author="devin-ai-integration[bot]", text="", config=IDENTITY,
                lineage=NO_LINEAGE,
            ),
            "conflicting builder identity; skipping author-excluded label update",
        )

    def test_every_contributor_stays_excluded(self):
        for lane in ("devin", "codex"):
            self.assertEqual(
                author_exclusion_reason(
                    lane_name=lane, labels=["builder:codex"],
                    author="devin-ai-integration[bot]", text="", config=IDENTITY,
                    lineage=self.context(),
                ),
                f"{lane} lane excluded for builder-authored PR",
            )

    def test_matches_name_both_contributors_in_order(self):
        self.assertEqual(
            builder_identity_matches(
                labels=["builder:codex"], author="devin-ai-integration[bot]", text="",
                config=IDENTITY, lineage=self.context(),
            ),
            ("devin", "codex"),
        )

    def test_a_marker_from_an_untrusted_author_is_not_evidence(self):
        untrusted = [{"user": {"login": "random-person"}, "body": self.comments[0]["body"]}]
        self.assertEqual(self.context(comments=untrusted).episodes, ())

    def test_lineage_behind_the_head_skips_rather_than_guesses(self):
        self.assertEqual(
            author_exclusion_reason(
                lane_name="claude", labels=["builder:codex"],
                author="devin-ai-integration[bot]", text="", config=IDENTITY,
                lineage=self.context(head=FIXED),
            ),
            "builder contribution lineage is behind the current head; "
            "skipping author-excluded label update",
        )

    def test_malformed_published_evidence_fails_closed(self):
        broken = [{
            "user": {"login": "codemower-ai"},
            "body": f"<!-- {builder_lineage.LINEAGE_MARKER} {{\"schema\": \"nope\"}} -->",
        }]
        with self.assertRaises(builder_lineage.LineageError):
            self.context(comments=broken)

    def test_an_unverified_head_carries_no_evidence(self):
        self.assertEqual(self.context(head="").episodes, ())
        self.assertEqual(self.context(head="").repo, "")

    def test_an_ordinary_single_builder_pr_is_unaffected(self):
        context = lineage_context(
            repo=REPO, pr_number=777, branch=BRANCH, head_sha=OPENED,
            comments=[], trusted_author=lineage_marker_author_trust(authorities=()),
        )
        self.assertIsNone(
            author_exclusion_reason(
                lane_name="codex", labels=["builder:claude"], author="claude[bot]",
                text="", config=IDENTITY, lineage=context,
            )
        )
        self.assertEqual(
            author_exclusion_reason(
                lane_name="claude", labels=["builder:claude"], author="claude[bot]",
                text="", config=IDENTITY, lineage=context,
            ),
            "claude lane excluded for builder-authored PR",
        )


class TrailerLabelerEntryPath(unittest.TestCase):
    """The real trailer labeler decision, not the helper it calls."""

    def test_resolve_label_decision_threads_trusted_lineage(self):
        from code_mower import trailer_comment_labeler as labeler

        marker = builder_lineage.lineage_comment_marker((takeover_episode(),))
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return None

        event = {
            "action": "created",
            "issue": {
                "number": PR,
                "pull_request": {},
                "labels": [{"name": "builder:codex"}],
                "user": {"login": "devin-ai-integration[bot]"},
                "body": "",
            },
            "comment": {"id": 1, "user": {"login": "codemower-ai"}, "body": "x"},
        }
        config = SimpleNamespace(
            name="claude",
            comment_authors=lambda: {"codemower-ai"},
            is_configured_comment_author=lambda author: False,
            is_default_comment_author=lambda author: True,
            trailer_prefix="Claude-Audit",
            display_name="Claude",
        )
        with mock.patch.object(labeler, "author_exclusion_reason", capture), \
                mock.patch.object(labeler, "classify_audit_comment", return_value="done"), \
                mock.patch.object(
                    labeler.code_mower_decisions,
                    "collect_decision_records_from_comments", return_value=(),
                ):
            labeler.resolve_label_decision(
                event,
                current_head_sha=TAKEN,
                config=config,
                repo=REPO,
                head_branch=BRANCH,
                issue_comments=[{"user": {"login": "codemower-ai"}, "body": marker}],
                decision_authorities=("codemower-ai",),
            )
        self.assertEqual(seen["lineage"].repo, REPO)
        self.assertEqual(seen["lineage"].head_sha, TAKEN)
        self.assertEqual(seen["lineage"].branch, BRANCH)
        self.assertEqual(len(seen["lineage"].episodes), 1)


class SaaSLabelerEntryPath(unittest.TestCase):
    """Every SaaS reviewer entry path receives the same resolved lineage."""

    def test_each_event_shape_receives_the_lineage(self):
        from code_mower import saas_reviewer_labeler as labeler

        marker = builder_lineage.lineage_comment_marker((takeover_episode(),))
        comments = [{"user": {"login": "codemower-ai"}, "body": marker}]
        adapter = SimpleNamespace(
            name="greptile", event_type="issue_comment", opt_in_required=False,
            label_prefix="greptile", needs_label="n", done_label="d", blocked_label="b",
            is_opted_in=lambda labels: True, is_review_author=lambda author: True,
            is_check_run_author=lambda check_run: True,
            is_check_run_name=lambda check_run: True,
        )
        for event_type, event in (
            ("pull_request_review", {"action": "submitted", "pull_request": {"number": PR}}),
            (
                "issue_comment",
                {
                    "action": "created",
                    "issue": {"number": PR, "pull_request": {}},
                    "comment": {"user": {"login": "bot"}, "body": "x"},
                },
            ),
            (
                "check_run",
                {"action": "completed", "check_run": {"status": "completed"}},
            ),
        ):
            with self.subTest(event_type=event_type):
                seen = {}

                def capture(*, _seen=seen, **kwargs):
                    _seen.update(kwargs)
                    return "stop"

                with mock.patch.object(labeler, "author_exclusion_reason", capture):
                    labeler.resolve_label_decision(
                        event,
                        adapter=adapter,
                        event_type=event_type,
                        pr_number=PR,
                        pr_labels=["builder:codex"],
                        pr_author="devin-ai-integration[bot]",
                        pr_body="",
                        current_head_sha=TAKEN,
                        repo=REPO,
                        head_branch=BRANCH,
                        issue_comments=comments,
                        decision_authorities=("codemower-ai",),
                    )
                self.assertEqual(seen["lineage"].head_sha, TAKEN)
                self.assertEqual(len(seen["lineage"].episodes), 1)

    def test_unreadable_published_lineage_skips_the_update(self):
        from code_mower import saas_reviewer_labeler as labeler

        adapter = SimpleNamespace(
            name="greptile", event_type="issue_comment", opt_in_required=False,
            label_prefix="greptile", needs_label="n", done_label="d", blocked_label="b",
            is_opted_in=lambda labels: True, is_review_author=lambda author: True,
            is_check_run_author=lambda check_run: True,
            is_check_run_name=lambda check_run: True,
        )
        decision, reason = labeler.resolve_label_decision(
            {"action": "created", "issue": {"number": PR, "pull_request": {}}},
            adapter=adapter,
            event_type="issue_comment",
            pr_number=PR,
            current_head_sha=TAKEN,
            repo=REPO,
            issue_comments=[{
                "user": {"login": "codemower-ai"},
                "body": f"<!-- {builder_lineage.LINEAGE_MARKER} {{\"schema\": \"nope\"}} -->",
            }],
            decision_authorities=("codemower-ai",),
        )
        self.assertIsNone(decision)
        self.assertIn("unreadable", reason)


class RoleEligibilityIsSeparate(unittest.TestCase):
    """Contribution independence and role qualification are different questions."""

    def test_admission_never_consults_role_eligibility(self):
        source = Path("src/code_mower/provider_runners/lineage.py").read_text(encoding="utf-8")
        self.assertNotIn("role_eligibility", source.replace(
            "code_mower.role_eligibility", ""
        ).replace("mod:``", ""))

    def test_an_independent_lane_is_admitted_on_contribution_grounds_alone(self):
        pr_meta = {
            "labels": [{"name": "builder:codex"}],
            "user": {"login": "devin-ai-integration[bot]"},
            "head": {"ref": BRANCH},
        }
        decision = reviewer_lineage.reviewer_admission(
            "claude", repo=REPO, pr_number=PR, pr_meta=pr_meta, head_sha=TAKEN,
            episodes=(takeover_episode(),), identity=IDENTITY,
        )
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["contributors"], ["devin", "codex"])
        self.assertNotIn("role", json.dumps(decision))


VENDORED_MIRRORS = ("audit_labeler_lib.py", "builder_lineage.py", "decisions.py")

#: Exactly what a generated gate runner executes: the repository's own
#: ``tools/`` copies, imported as the ``tools`` package, with no Code Mower
#: package and nothing from ``src/`` reachable. Asserting against the package
#: source would pass while the vendored copy raises ``NameError``.
VENDORED_PROBE = '''
import json

import tools.audit_labeler_lib as labeler
import tools.builder_lineage as lineage

REPO = "codemower-ai/code-mower"
PR = 959
BRANCH = "devin/959-thing"
OPENED = "a" * 40
TAKEN = "b" * 40
FIXED = "c" * 40

handoff = lineage.ContributionEpisode(
    sequence=1, kind=lineage.HANDOFF_KIND, repo=REPO, pr_number=PR, branch=BRANCH,
    source_lane="devin", destination_lane="codex", expected_head=OPENED,
    resulting_head=TAKEN, writer_state="terminated",
)
continued = lineage.continuation_episode(handoff, lane="codex", resulting_head=FIXED)
marker = lineage.lineage_comment_marker([handoff, continued])

# The vendored decisions copy decides who may publish lineage at all.
authorities = labeler.lineage_decision_authorities()
trusted = labeler.lineage_marker_author_trust(authorities=authorities)

# One authorised republication per round, exactly as the producer posts it.
published = labeler.published_lineage_episodes(
    [{"user": {"login": "codemower-ai"}, "body": marker}] * 20
    + [{"user": {"login": "codex[bot]"}, "body": marker}],
    trusted_author=trusted,
)
resolved = lineage.resolve_lineage(
    repo=REPO, pr_number=PR, branch=BRANCH, head_sha=FIXED, episodes=published,
    opener_lane="devin", label_lanes=["codex"],
)
print(json.dumps({
    "authorities": list(authorities),
    "arrivals": len(published),
    "status": resolved.status,
    "reason": resolved.reason,
    "writer": resolved.current_writer,
    "contributors": list(resolved.contributors),
    "episodes": resolved.episodes,
}))
'''


class VendoredToolMirrors(unittest.TestCase):
    """The shipped ``tools/`` copies must be the canonical implementations.

    CI lints and a generated product gate imports the vendored files, not the
    package ones. Drift here is invisible to every assertion that reaches into
    ``src/code_mower``, and it has already produced an F821 plus a runtime
    ``NameError`` in published-lineage parsing.
    """

    def test_vendored_copies_match_their_canonical_sources(self):
        for name in VENDORED_MIRRORS:
            with self.subTest(module=name):
                canonical = Path("src/code_mower") / name
                vendored = Path("tools") / name
                self.assertEqual(
                    vendored.read_text(encoding="utf-8"),
                    canonical.read_text(encoding="utf-8"),
                    f"tools/{name} has drifted from src/code_mower/{name}",
                )

    def test_vendored_labeler_imports_every_name_it_uses(self):
        """The F821: LINEAGE_MARKER was used but never imported here."""

        source = Path("tools/audit_labeler_lib.py").read_text(encoding="utf-8")
        self.assertIn("LINEAGE_MARKER not in body", source)
        # One import per branch: packaged relative, copied-tools fallback and
        # direct helper execution. Any of the three missing is an F821.
        self.assertEqual(3, source.count("LINEAGE_MARKER,"))

    def _run_vendored_probe(self, environment_extra):
        root = git_free_tempdir(self)
        tools = root / "tools"
        tools.mkdir()
        (tools / "__init__.py").write_text("", encoding="utf-8")
        for target, _package_copy_from, _, _ in init.PRODUCT_SUPPORT_FILES:
            if not (target.startswith("tools/") and target.endswith(".py")):
                continue
            vendored = Path(target)
            if not vendored.exists():  # templated wrappers, not vendored modules
                continue
            (root / target).write_bytes(vendored.read_bytes())
        (root / "probe.py").write_text(VENDORED_PROBE, encoding="utf-8")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
            and not key.startswith("CODE_MOWER_DECISION_AUTHORITIES")
        }
        environment.update(environment_extra)
        result = subprocess.run(
            [sys.executable, "probe.py"],
            cwd=root, capture_output=True, text=True, timeout=120, env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_vendored_modules_parse_markers_and_replay_at_the_exact_head(self):
        """Marker parsing, authority override and idempotent replay, vendored."""

        payload = self._run_vendored_probe({
            # The override the canonical decisions module honours. The stale
            # vendored copy read only the base variable, so it would trust the
            # wrong account and read no published episodes at all.
            "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "codemower-ai",
            "CODE_MOWER_DECISION_AUTHORITIES": "someone-else",
        })
        self.assertEqual(payload["authorities"], ["codemower-ai"])
        # Twenty authorised republications of a two-episode chain; the audit
        # bot's byte-identical marker is not an authority and is not read.
        self.assertEqual(payload["arrivals"], 40)
        self.assertEqual(payload["status"], "resolved", payload["reason"])
        self.assertEqual(payload["episodes"], 2)
        self.assertEqual(payload["writer"], "codex")
        self.assertEqual(payload["contributors"], ["devin", "codex"])

    def test_vendored_authority_override_is_the_only_marker_trust(self):
        """No override configured, no authority: nothing is read or resolved."""

        payload = self._run_vendored_probe({"CODE_MOWER_DECISION_AUTHORITIES": ""})
        self.assertEqual(payload["authorities"], [])
        self.assertEqual(payload["arrivals"], 0)
        # No episodes at all is the ordinary single-builder answer, not a guess.
        self.assertEqual(payload["episodes"], 0)


if __name__ == "__main__":
    unittest.main()
