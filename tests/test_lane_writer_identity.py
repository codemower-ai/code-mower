"""Canonical lineage writer identity for every legal repository slug.

A repository name may legally contain ``.``, while ``LineageRound`` accepts only
``[A-Za-z0-9_-]{1,100}`` for the stable writer identity and the supervised round
ID. The runner used to paste ``<lane>-<owner>__<name>`` into both, so a dotted or
very long slug was refused before the provider ever launched. Every identity the
round already accepted stays exactly as it is, because persisted private lineage
records hold it and a continuation refuses on an inexact writer. These rows own
the one canonical derivation, that preservation, the runner wiring that uses it,
and the supervised round that a dotted slug must now reach.
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
from lineage_producer_fixtures import (
    AUTHORITY, BRANCH, POLICY, TRANSPORT, MemoryStore, episode, sha,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = (ROOT / "tools/lanes/run_mac_lane.sh",
           ROOT / "src/code_mower/templates/lanes/run_mac_lane.sh")
DOTTED = "owner/repo.example"
#: A legal slug whose pasted identity LineageRound already accepted, so private
#: lineage records for it hold that exact stable writer.
DASHED = "owner/a--b"
HISTORICAL_DASHED = "claude-owner__a--b"
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
            # A name may legally contain '--'; LineageRound already accepted
            # this identity, so private records hold it and it is never rewritten.
            ("claude", DASHED, HISTORICAL_DASHED),
            ("claude", "owner/a--b--c", "claude-owner__a--b--c"),
            ("codex", "own-er/le--ading", "codex-own-er__le--ading"),
        ):
            with self.subTest(repo=repo):
                self.assertEqual(delivery.lineage_writer_id(lane, repo), expected)
                self.assertAccepted(expected)

    def test_every_accepted_historical_identity_is_preserved_verbatim(self):
        # The migration hazard: an identity LineageRound already accepted is
        # stored in private lineage records, so re-encoding it strands the
        # repository at lineage_continuation()'s exact writer equality check.
        # Only a genuinely ambiguous owner/name boundary is exempt, which the
        # encoded rows below cover. 86 and 87 straddle the 100-character cap for
        # a six-character lane.
        slugs = ["owner/repo", "codemower-ai/code-mower", "owner/a--b", "owner/a--", "o/n",
                 "owner/" + "n" * 86, "owner/" + "n" * 87, "Owner_9/Repo-9",
                 "own/er__repo", "o/__b", "owner/repo_example-7d4b3c99832ebb9513776661"]
        for lane in LANES:
            for repo in slugs:
                for run in (None, "20260917T091500Z-4242"):
                    historical = f"{lane}-{repo.replace('/', '__')}"
                    historical += "" if run is None else f"-{run}"
                    if not re.fullmatch(delivery.LINEAGE_ID_PATTERN, historical):
                        continue
                    with self.subTest(lane=lane, repo=repo, run=run):
                        self.assertEqual(delivery.lineage_writer_id(lane, repo, run=run),
                                         historical)
                        # For one lane the preserved and encoded namespaces are
                        # disjoint: only an encoded identity begins '<lane>--'.
                        self.assertFalse(historical.startswith(lane + "--"), historical)

    def test_dotted_long_and_hostile_slugs_become_accepted_identifiers(self):
        slugs = [DOTTED, "owner/repo.js", "owner/.hidden.name.", "owner/" + "n" * 100,
                 "o" * 39 + "/" + "n" * 100, "owner/repo..name", "owner/éé",
                 "owner/repo name", "owner/repo;rm -rf", "o_/_b", "own__er/repo",
                 # Not a legal GitHub owner, and never a historical identity:
                 # an owner starting with '-' would otherwise land the preserved
                 # identity inside the encoded '<lane>--' namespace.
                 "-owner/repo"]
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
                 # Preserved identities carrying the encoding marker, next to
                 # the encoded identities of slugs that sanitize to the same
                 # readable text.
                 DASHED, "owner/a--b--c", "owner/a__b", "owner/a..b", "owner/a-_b",
                 # A slug shaped like an encoded identity must not claim one:
                 # the preserved form keeps a single '-' after the lane, and an
                 # owner starting with '-' is encoded rather than preserved.
                 "owner/repo_example-7d4b3c99832ebb9513776661", "-owner/repo",
                 "-owner__repo/x"]
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

    def test_authored_template_is_rendered_for_the_runtime(self):
        runtime = RUNNERS[0].read_text(encoding="utf-8")
        authored = RUNNERS[1].read_text(encoding="utf-8")
        self.assertIn("__LANE_MAC_RUNNER_ALLOWED_CASE__", authored)
        self.assertNotIn("__LANE_MAC_RUNNER_ALLOWED_CASE__", runtime)
        self.assertIn('case "$LANE" in codex|claude)', runtime)

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


class HistoricalRecordContinuation(unittest.TestCase):
    """A persisted historical writer must still be the derived writer.

    ``owner/a--b`` is a legal slug whose pasted ``claude-owner__a--b`` identity
    ``LineageRound`` already accepted, so a private lineage record holds it.
    Deriving anything else launches the provider and only then refuses at
    ``lineage_continuation()``'s exact writer equality check, leaving the round
    unrecordable and unpublishable.
    """

    ROUNDS = "/dashed-rounds"
    STORE = "/dashed-producer-state"

    def setUp(self):
        from code_mower.builder_lineage_producer import ProducerStore
        MemoryStore.records, MemoryStore.effects = {}, []
        self.addCleanup(patch.stopall)
        patch("code_mower.builder_lineage_producer.ContextStore", MemoryStore).start()
        patch("code_mower.lane_handoff.ContextStore", MemoryStore).start()
        patch.object(delivery, "_lineage_checkout").start()
        self.memory = MemoryStore
        self.store = ProducerStore(Path(self.STORE))

    def target(self, n):
        # The approved producer fixtures, moved onto the dashed slug.
        return Target(DASHED, 42, BRANCH, sha(n))

    def persist_historical_record(self):
        """The record an earlier accepted round already wrote for this slug."""
        record = dict(schema="code_mower.lineageProducer.v1", repo=DASHED, pr_number=42,
            branch=BRANCH, episodes=[episode(1, repo=DASHED).to_mapping()],
            writer=HISTORICAL_DASHED, round_id=HISTORICAL_DASHED + "-20260916T101500Z-11",
            transport=TRANSPORT.__dict__)
        self.memory.records[(self.STORE, self.store._key(self.target(1)))] = record
        previous = self.store.read(self.target(1))
        self.assertEqual(previous["writer"], HISTORICAL_DASHED)
        return previous

    def stopped_round(self, writer, round_id):
        observer = delivery.LineageRound(Path(self.ROUNDS), round_id, writer, self.target(1),
            TRANSPORT, Path(self.ROUNDS + "/checkout"), config={},
            runtime_observation=lambda: "ready")
        observer.started(11, 11)
        observer.finish(quiescent=True)
        return observer

    def test_the_derived_writer_continues_the_persisted_historical_record(self):
        previous = self.persist_historical_record()
        writer = delivery.lineage_writer_id("claude", DASHED)
        round_id = delivery.lineage_writer_id("claude", DASHED, run="20260917T091500Z-77")
        self.assertEqual(writer, HISTORICAL_DASHED)
        self.assertNotEqual(round_id, previous["round_id"])
        observer = self.stopped_round(writer, round_id)
        continuation = delivery.lineage_continuation(observer, self.target(2), previous)
        self.assertEqual(continuation.writer, HISTORICAL_DASHED)
        self.assertEqual(continuation.episode.writer_state, "same_writer")
        self.assertEqual(continuation.episode.sequence, 2)
        # Recordable, so the round still reaches publication as it did before.
        self.assertTrue(self.store.record(continuation, self.target(2), POLICY, AUTHORITY,
            History([]), author="source-bot", labels=["builder:codex"], config={},
            runtime_observation=lambda: "ready"))
        self.assertEqual(len(self.store.read(self.target(2))["episodes"]), 2)

    def test_a_re_encoded_writer_would_refuse_the_same_persisted_record(self):
        # The exact failure the preserved identity avoids, shown against the
        # same record: the round launches and the continuation is then refused.
        from code_mower.builder_lineage_producer import ProducerRefusal
        previous = self.persist_historical_record()
        encoded = delivery.lineage_writer_id("claude", DOTTED)
        self.assertNotEqual(encoded, HISTORICAL_DASHED)
        observer = self.stopped_round(encoded, encoded + "-20260917T091500Z-77")
        with self.assertRaises(ProducerRefusal):
            delivery.lineage_continuation(observer, self.target(2), previous)


class PreChangeInstallationGate(unittest.TestCase):
    """An installed CLI without the derivation must refuse before every effect.

    ``lineage-capabilities`` is answered by the installed CLI's own capability
    list, so a pre-change installation reports success without knowing that
    ``writer-id`` is now required. The runner needs both identities only after
    target selection and handoff processing, so discovering the unknown
    subcommand there would stop the source writer, reserve and accept the
    handoff and post its acceptance comment first. The refusal therefore comes
    from an explicit, side-effect-free probe at the initial capability gate.
    """

    #: The probe, and the later derivation it deliberately does not replace.
    PROBE = '--repo "owner/writer-id.probe" --run probe'
    DERIVATION = '"${lane_delivery[@]}" writer-id --lane "$LANE" --repo "$REPO"'
    #: Runner text performing an effect that the refusal has to precede.
    EFFECTS = (
        'gh pr list -R "$REPO" --state open --limit',
        '"${lane_delivery[@]}" handoff "${handoff_args[@]}" --json',
        'gh pr comment "$num" -R "$REPO" --body-file "$handoff_body_file"',
        'writer_source="${log%.log}.source.json"',
        "--reserve-launch",
        "run_provider()",
    )

    def test_every_runner_copy_probes_the_derivation_at_the_capability_gate(self):
        for path in RUNNERS:
            with self.subTest(runner=str(path)):
                text = path.read_text(encoding="utf-8")
                probe = text.index(self.PROBE)
                self.assertLess(text.index('"${lane_delivery[@]}" lineage-capabilities'), probe)
                # Before argument-only handoff validation, and before every effect.
                self.assertLess(probe, text.index('if [ -n "$HANDOFF_SOURCE_LANE" ]'))
                for marker in self.EFFECTS:
                    self.assertLess(probe, text.index(marker), marker)
                # The real derivation and its fail-closed check stay where they are.
                self.assertLess(probe, text.index(self.DERIVATION))
                self.assertIn("lane-delivery writer-id is required", text)

    def stub(self, directory, name, body):
        path = directory/name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def test_a_pre_change_installation_refuses_before_any_modeled_effect(self):
        import os
        import shlex
        import subprocess
        import sys
        from lineage_consumer_fixtures import fixture_shell_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bin_dir = root/"bin"
            bin_dir.mkdir()
            effects, calls = root/"effects", root/"calls"
            # Every modeled effect the runner could reach records itself and fails.
            for command in ("gh", "git", "claude", "codex"):
                self.stub(bin_dir, command,
                          'printf "%s\\n" "$0 $*" >> ' + shlex.quote(str(effects)) + "\nexit 81\n")
            # A pre-change CLI: it answers the legacy capability command and every
            # subcommand it shipped, and knows nothing about writer-id.
            legacy = ('printf "%s\\n" "$*" >> ' + shlex.quote(str(calls)) + "\n"
                      'case "${1:-}" in\n'
                      '  writer-id) echo "invalid choice: \'writer-id\'" >&2; exit 2 ;;\n'
                      "  *) exit 0 ;;\n"
                      "esac\n")
            pinned = self.stub(bin_dir, "lane-delivery-pre-change", legacy)
            self.stub(bin_dir, "code-mower", "shift\n" + legacy)
            source_file = root/"handoff-source.json"
            source_file.write_text(json.dumps({"transport": "local_process",
                "writer": "devin-owner__repo", "state_dir": str(root/"writers")}))
            base = os.environ | fixture_shell_env(root) | {
                "HOME": str(root), "LANE_PYTHON": sys.executable,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
            base.pop("CODE_MOWER_LANE_DELIVERY_CMD", None)
            for mode in ("installed", "pin"):
                env = dict(base)
                if mode == "pin":
                    env["CODE_MOWER_LANE_DELIVERY_CMD"] = str(pinned)
                with self.subTest(mode=mode):
                    result = subprocess.run(["bash", str(RUNNERS[0]),
                        "--lane", "claude", "--repo", DOTTED, "--max-minutes", "1",
                        "--target", "pr:42", "--handoff-source-lane", "devin",
                        "--handoff-expected-head", "a"*40,
                        "--handoff-source-file", str(source_file)],
                        env=env, text=True, capture_output=True, check=False)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("lane-delivery writer-id is required", result.stderr)
                    # No selection, source stop, reservation, acceptance comment,
                    # writer registration or provider launch happened first.
                    self.assertFalse(effects.exists(), result.stdout + result.stderr)
                    recorded = calls.read_text(encoding="utf-8").splitlines()
                    self.assertIn("lineage-capabilities", recorded)
                    self.assertTrue(any(line.startswith("writer-id ") for line in recorded), recorded)
                    self.assertFalse([line for line in recorded if line.startswith(
                        ("handoff", "supervise", "classify", "transition", "lineage-record"))], recorded)
                    calls.unlink()


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
