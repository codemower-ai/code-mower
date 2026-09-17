"""Canonical lineage writer identity for every legal repository slug.

A repository name may legally contain ``.``, while ``LineageRound`` accepts only
``[A-Za-z0-9_-]{1,100}`` for the stable writer identity and the supervised round
ID. The runner used to paste ``<lane>-<owner>__<name>`` into both, so a dotted or
very long slug was refused before the provider ever launched. These rows own the
one canonical derivation, the runner wiring that uses it, and the supervised
round that a dotted slug must now reach.
"""
import contextlib
import io as io_module
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from code_mower import lane_delivery as delivery
from code_mower.builder_lineage import History, Target
from code_mower.builder_lineage_producer import Snapshot
from lineage_consumer_fixtures import AUTHORS, complete_pr, git, pinned_repo, policy

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = (ROOT/"tools/lanes/run_mac_lane.sh", ROOT/"templates/lanes/run_mac_lane.sh",
           ROOT/"src/code_mower/templates/lanes/run_mac_lane.sh")
DOTTED = "owner/repo.example"
LANES = ("claude", "codex", "devin")


class CanonicalWriterIdentity(unittest.TestCase):
    def assertAccepted(self, value):
        self.assertIsInstance(value, str)
        self.assertRegex(value, r"\A" + delivery.LINEAGE_ID_PATTERN + r"\Z")
        self.assertLessEqual(len(value), 100)

    def test_ordinary_slugs_keep_the_identifier_they_already_have(self):
        # Preserving these keeps existing private writer state addressable.
        for lane, repo, expected in (
            ("claude", "codemower-ai/code-mower", "claude-codemower-ai__code-mower"),
            ("codex", "owner/repo", "codex-owner__repo"),
            ("devin", "Owner-9/some_repo", "devin-Owner-9__some_repo"),
        ):
            with self.subTest(repo=repo):
                self.assertEqual(delivery.lineage_writer_id(lane, repo), expected)
                self.assertAccepted(expected)

    def test_dotted_long_and_hostile_slugs_become_accepted_identifiers(self):
        slugs = [DOTTED, "owner/repo.js", "owner/.hidden.name.", "owner/" + "n" * 100,
                 "o" * 39 + "/" + "n" * 100, "owner/repo..name", "owner/a--b",
                 "owner/éé", "owner/repo name", "owner/repo;rm -rf", "o_/_b"]
        for lane in LANES:
            for repo in slugs:
                with self.subTest(lane=lane, repo=repo):
                    derived = delivery.lineage_writer_id(lane, repo)
                    self.assertAccepted(derived)
                    # Deterministic: the same slug always addresses the same writer.
                    self.assertEqual(derived, delivery.lineage_writer_id(lane, repo))
                    self.assertTrue(derived.startswith(lane + "--"), derived)

    def test_similar_and_truncating_slugs_never_share_one_identity(self):
        # Sanitizing '.' to '_' and truncating a long slug both merge distinct
        # repositories unless the derivation carries a digest of the exact slug.
        slugs = [DOTTED, "owner/repo_example", "owner/repo-example", "owner/repo.example.",
                 "owner/repo..example", "owner/" + "n" * 100, "owner/" + "n" * 99,
                 "owner/" + "n" * 100 + ".git", "own/er__repo", "own__er/repo",
                 "owner/repo", "owner/repo.", "o_/_b", "o/__b",
                 # A slug shaped like an encoded identity must not claim one.
                 "owner/repo_example-7d4b3c99832ebb9513776661"]
        identities = {}
        for lane in LANES:
            for repo in slugs:
                for run in (None, "20260917T091500Z-4242"):
                    derived = delivery.lineage_writer_id(lane, repo, run=run)
                    self.assertAccepted(derived)
                    key = (lane, repo, run)
                    self.assertNotIn(derived, identities,
                                     f"{key} collides with {identities.get(derived)}")
                    identities[derived] = key
        self.assertEqual(len(identities), len(LANES) * len(slugs) * 2)

    def test_a_run_suffix_yields_a_fresh_round_id_for_the_same_writer(self):
        writer = delivery.lineage_writer_id("claude", DOTTED)
        first = delivery.lineage_writer_id("claude", DOTTED, run="20260917T091500Z-11")
        second = delivery.lineage_writer_id("claude", DOTTED, run="20260917T091501Z-11")
        for value in (writer, first, second):
            self.assertAccepted(value)
        self.assertNotEqual(writer, first)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(writer), (writer, first))

    def test_inexact_inputs_are_refused_rather_than_repaired(self):
        cases = [("claude", "owner"), ("claude", "owner/"), ("claude", "/repo"),
                 ("claude", "owner/repo/extra"), ("claude", None), ("", "owner/repo"),
                 ("claude lane", "owner/repo"), ("claude/", "owner/repo"), (None, "owner/repo")]
        for lane, repo in cases:
            with self.subTest(lane=lane, repo=repo):
                with self.assertRaises(delivery.LaneDeliveryError):
                    delivery.lineage_writer_id(lane, repo)
        for run in ("", "not a run", "x" * 41, 7):
            with self.subTest(run=run):
                with self.assertRaises(delivery.LaneDeliveryError):
                    delivery.lineage_writer_id("claude", "owner/repo", run=run)


class WriterIdentityCli(unittest.TestCase):
    def derive(self, *argv):
        stream = io_module.StringIO()
        with contextlib.redirect_stdout(stream):
            code = delivery.main(["writer-id", *argv])
        self.assertEqual(code, 0)
        return json.loads(stream.getvalue())

    def test_cli_reports_both_identities_for_a_dotted_slug(self):
        payload = self.derive("--lane", "claude", "--repo", DOTTED, "--run", "20260917T091500Z-9")
        self.assertEqual(payload["writer"], delivery.lineage_writer_id("claude", DOTTED))
        self.assertEqual(payload["round_id"],
                         delivery.lineage_writer_id("claude", DOTTED, run="20260917T091500Z-9"))
        self.assertEqual(payload["repo"], DOTTED)
        self.assertEqual(payload["lane"], "claude")

    def test_cli_omits_a_round_id_when_no_run_is_selected(self):
        payload = self.derive("--lane", "codex", "--repo", "owner/repo")
        self.assertEqual(payload, {"lane": "codex", "repo": "owner/repo",
                                   "writer": "codex-owner__repo"})

    def test_cli_refuses_an_inexact_slug_without_printing_an_identity(self):
        stream, errors = io_module.StringIO(), io_module.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(errors):
            code = delivery.main(["writer-id", "--lane", "claude", "--repo", "owner"])
        self.assertEqual(code, 2)
        self.assertEqual(stream.getvalue(), "")
        self.assertIn("OWNER/REPO", errors.getvalue())


class RunnerWiring(unittest.TestCase):
    def test_every_runner_copy_derives_both_identities_from_lane_delivery(self):
        for path in RUNNERS:
            with self.subTest(runner=str(path)):
                text = path.read_text(encoding="utf-8")
                derive = text.index('"${lane_delivery[@]}" writer-id --lane "$LANE" --repo "$REPO"')
                alias = text.index('writer_alias="$(printf')
                writer = text.index('lineage_writer="$(printf')
                used = text.index('--lineage-writer "$lineage_writer"')
                self.assertLess(derive, alias)
                self.assertLess(alias, used)
                self.assertLess(writer, used)
                # The pasted slug is what LineageRound refused for dotted names.
                self.assertNotIn('writer_alias="${LANE}-${repo_key}', text)
                self.assertNotIn('--lineage-writer "${LANE}-${repo_key}"', text)
                # An unusable identity stops the unit instead of launching a
                # provider whose supervised round would be refused anyway.
                self.assertIn("no canonical lineage writer identity for ${REPO}", text)

    def test_template_copies_stay_identical(self):
        self.assertEqual(RUNNERS[1].read_bytes(), RUNNERS[2].read_bytes())

    def test_an_installed_cli_without_the_derivation_is_refused_as_uncapable(self):
        from code_mower.builder_lineage import ContractError
        from code_mower.provider_runners.lineage import require_capabilities
        require_capabilities()
        with patch.object(delivery, "lineage_writer_id", None):
            with self.assertRaises(ContractError):
                require_capabilities()


class DottedSlugDeliveryIO:
    """Only the GitHub boundary is simulated; every decision below is real."""

    def __init__(self, repo, checkout, base, branch):
        self.repo, self.checkout, self.base, self.branch = repo, checkout, base, branch
        self.labels_active = ["builder:claude"]

    def target(self):
        return Target(self.repo, 42, self.branch, git(self.checkout, "rev-parse", "HEAD"))

    def snapshot(self, target):
        return Snapshot(self.target(), "human", tuple(self.labels_active))

    def _json(self, endpoint):
        assert endpoint == f"repos/{self.repo}/pulls/42"
        return complete_pr({"base": {"repo": {"full_name": self.repo}, "sha": self.base}},
                           branch=self.branch, head=self.target().head_sha, author="human",
                           labels=self.labels_active)

    def history(self, target):
        return History([])

    def post(self, target, body):
        raise AssertionError("no publication is expected without a private record")

    def labels(self, target, desired, remove, add):
        raise AssertionError("no label effect is expected without a private record")


class SupervisedDottedSlugRound(unittest.TestCase):
    """A dotted slug must reach provider launch and a recorded attribution."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.checkout = self.root/"checkout"
        self.config = policy()
        self.config["repositories"][0]["slug"] = DOTTED
        self.config["decisions"]["authorities"] = list(AUTHORS)
        self.base = pinned_repo(self.checkout, self.config)
        git(self.checkout, "checkout", "-qb", "claude/topic")
        self.rounds = self.root/"round-state"
        self.io = DottedSlugDeliveryIO(DOTTED, self.checkout, self.base, "claude/topic")

    def round_args(self, writer, round_id, output):
        before = self.io.target()
        before_file = self.root/(round_id+"-before.json")
        before_file.write_text(json.dumps({"snapshot_complete": True, "kind": "pr",
            "number": "42", "pr_number": "42", "pr_state": "OPEN", "branch": before.branch,
            "head_sha": before.head_sha, "author": "human", "labels": self.io.labels_active}))
        return ["supervise", "--cwd", str(self.checkout), "--log", str(self.root/(round_id+".log")),
                "--timeout-seconds", "60", "--writer", round_id,
                "--writer-state-dir", str(self.rounds), "--writer-repo", DOTTED,
                "--writer-lane", "claude", "--lineage-before", str(before_file),
                "--lineage-base", self.base, "--lineage-writer", writer,
                "--lineage-output", str(output), "--",
                "git", "-C", str(self.checkout), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                "commit", "--allow-empty", "-qm", round_id]

    def run_round(self, writer, round_id):
        output = self.root/(round_id+"-event.json")
        errors = io_module.StringIO()
        with patch("code_mower.builder_lineage_producer.GitHub", return_value=self.io), \
                contextlib.redirect_stderr(errors):
            code = delivery.main(self.round_args(writer, round_id, output))
        return code, output, errors.getvalue()

    def test_a_dotted_slug_reaches_provider_launch_and_records_attribution(self):
        head = self.io.target().head_sha
        writer = delivery.lineage_writer_id("claude", DOTTED)
        round_id = delivery.lineage_writer_id("claude", DOTTED, run="20260917T091500Z-77")
        code, output, errors = self.run_round(writer, round_id)
        self.assertEqual(code, 0, errors)
        self.assertNotEqual(self.io.target().head_sha, head, "the provider never ran")
        event = json.loads(output.read_text())
        self.assertEqual(event["dimensions"]["lineage"]["current_writer"], "claude")
        self.assertEqual(event["tool"]["executor"], "claude_cli")

    def test_the_pasted_slug_identity_is_refused_before_any_provider_launch(self):
        # The exact regression: '<lane>-<owner>__<name>' for a dotted slug.
        legacy = "claude-" + DOTTED.replace("/", "__")
        self.assertFalse(re.fullmatch(delivery.LINEAGE_ID_PATTERN, legacy))
        head = self.io.target().head_sha
        code, output, errors = self.run_round(legacy, "claude-legacy-round")
        self.assertEqual(code, 2)
        self.assertIn("ProducerRefusal", errors)
        self.assertEqual(self.io.target().head_sha, head)
        self.assertFalse(output.exists())


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
