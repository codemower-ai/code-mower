"""Offline contract tests for local-repository graph evidence (issue #876).

These prove the extension point and the adopt gate without installing a graph
package or running an indexer. A passing suite is not Graphify compatibility
evidence; see docs/graphify-evaluation.md for the decision and its boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from code_mower import context_contract as contract, context_graph as graph
from code_mower.context_contract import ContextError


FIXTURE = Path(__file__).parent / "fixtures" / "local_graph_contract.json"
NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def packet() -> dict:
    """The fixture's ``_comment`` documents provenance; the schema forbids it."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.pop("_comment")
    return data


class GraphCitationScopeTests(unittest.TestCase):
    def test_parses_paths_and_line_spans(self) -> None:
        for source, expected in (
            ("example_pkg/config.py", (None, None)),
            ("example_pkg/config.py#L12", (12, 12)),
            ("example_pkg/config.py#L12-L30", (12, 30)),
        ):
            with self.subTest(source=source):
                citation = graph.parse_graph_citation(source)
                self.assertEqual(citation.path, "example_pkg/config.py")
                self.assertEqual((citation.start_line, citation.end_line), expected)

    def test_rejects_citations_outside_the_indexed_checkout(self) -> None:
        """Cache and worktree isolation: a graph may only cite what it indexed."""
        for source in (
            "/etc/passwd",
            "/example/other-worktree/config.py",
            "../sibling-worktree/config.py",
            "example_pkg/../../escape.py",
            "example_pkg\\config.py",
            ".git/config",
            ".graph/index.db",
            ".graphify/cache/nodes.bin",
            ".code-mower/lane-outcome.json",
            "",
            "   ",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ContextError):
                    graph.parse_graph_citation(source)

    def test_excluded_roots_are_matched_case_insensitively(self) -> None:
        """APFS and NTFS default to case-insensitive: ``.GIT`` is ``.git``."""
        for source in (
            ".GIT/config",
            ".Graphify/cache/nodes.bin",
            ".Graph/index.db",
            ".Code-Mower/lane-outcome.json",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ContextError):
                    graph.parse_graph_citation(source)

    def test_rejects_malformed_line_spans(self) -> None:
        for source in (
            "example_pkg/config.py#L0",
            "example_pkg/config.py#L30-L12",
            "example_pkg/config.py#L12-L",
            "example_pkg/config.py#Lnope",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ContextError):
                    graph.parse_graph_citation(source)


class GraphEvidenceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.write("example_pkg/config.py", 40)
        self.write("example_pkg/loader.py", 50)
        self.write("tests/test_config.py", 30)

    def write(self, relative: str, lines: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"line {n}" for n in range(1, lines + 1)), encoding="utf-8")
        return path

    def evaluate(self, data: dict, *, revision_state: str = "matching"):
        return graph.evaluate_graph_evidence(
            data, repository_root=self.root, revision_state=revision_state
        )

    def test_fixture_citations_resolve_against_the_checkout(self) -> None:
        report = self.evaluate(packet())
        self.assertEqual(report.total_citations, 4)
        self.assertEqual(report.line_citations, 3)
        self.assertEqual(report.resolved_line_citations, 3)
        self.assertEqual(report.resolution_rate, 1.0)
        self.assertTrue(report.meets_gate())

    def test_truncation_and_completeness_stay_explicit(self) -> None:
        report = self.evaluate(packet())
        self.assertTrue(report.truncated)
        self.assertEqual(report.completeness, "partial")
        self.assertEqual(
            report.shareable_summary(),
            {
                "schema": "code_mower.contextGraphQuality.v1",
                "revision_state": "matching",
                "citations": 4,
                "line_citations": 3,
                "resolved_line_citations": 3,
                "completeness": "partial",
                "truncated": True,
            },
        )

    def test_line_claim_past_end_of_file_is_unresolved(self) -> None:
        """A stale index cites lines that no longer exist; that must show up."""
        data = packet()
        data["documents"][0]["citations"][1]["source"] = "example_pkg/loader.py#L4000"
        report = self.evaluate(data)
        self.assertEqual(report.resolved_line_citations, 2)
        self.assertAlmostEqual(report.resolution_rate, 2 / 3)
        self.assertFalse(report.meets_gate())

    def test_line_claim_ending_on_the_last_line_resolves(self) -> None:
        """The resolver stops at the claimed line; the boundary must be exact.

        ``example_pkg/config.py`` has 40 lines, so a span ending on line 40
        resolves and one reaching line 41 does not.
        """
        for span, resolved in (("#L39-L40", 3), ("#L40-L41", 2)):
            with self.subTest(span=span):
                data = packet()
                data["documents"][0]["citations"][1]["source"] = f"example_pkg/config.py{span}"
                self.assertEqual(self.evaluate(data).resolved_line_citations, resolved)

    def test_missing_file_is_unresolved_rather_than_an_error(self) -> None:
        data = packet()
        data["documents"][0]["citations"][0]["source"] = "example_pkg/removed.py#L3"
        self.assertEqual(self.evaluate(data).resolved_line_citations, 2)

    def test_symlink_out_of_the_checkout_does_not_resolve(self) -> None:
        enclosing = tempfile.TemporaryDirectory()
        self.addCleanup(enclosing.cleanup)
        outside = Path(enclosing.name)
        (outside / "secret.py").write_text("line 1\nline 2\n", encoding="utf-8")
        link = self.root / "example_pkg" / "linked.py"
        try:
            link.symlink_to(outside / "secret.py")
        except OSError:  # pragma: no cover - platforms without symlink support
            self.skipTest("symlinks are unavailable on this platform")
        data = packet()
        data["documents"][0]["citations"][0]["source"] = "example_pkg/linked.py#L1"
        self.assertEqual(self.evaluate(data).resolved_line_citations, 2)

    def test_stale_and_unknown_revision_fail_the_gate(self) -> None:
        for state in ("stale", "unknown"):
            with self.subTest(state=state):
                report = self.evaluate(packet(), revision_state=state)
                self.assertEqual(report.resolution_rate, 1.0)
                self.assertFalse(report.meets_gate())
                self.assertEqual(report.shareable_summary()["revision_state"], state)

    def test_out_of_scope_citation_rejects_the_whole_packet(self) -> None:
        data = packet()
        data["documents"][1]["citations"][0]["source"] = "../other-worktree/test_config.py#L8"
        with self.assertRaises(ContextError):
            self.evaluate(data)

    def test_rejects_unsupported_inputs(self) -> None:
        organization = packet()
        organization["kind"] = "organization"
        with self.assertRaises(ContextError):
            self.evaluate(organization)
        with self.assertRaises(ContextError):
            self.evaluate(packet(), revision_state="fresh")
        with self.assertRaises(ContextError):
            graph.evaluate_graph_evidence(packet(), revision_state="matching")
        with self.assertRaises(ContextError):
            graph.evaluate_graph_evidence(
                packet(), repository_root=Path("relative"), revision_state="matching"
            )

    def test_rejects_inconsistent_completeness(self) -> None:
        data = packet()
        data["completeness"] = "complete"
        with self.assertRaises(ContextError):
            self.evaluate(data)

    def test_citation_budget_is_bounded(self) -> None:
        data = packet()
        document = copy.deepcopy(data["documents"][0])
        document["citations"] = [
            {"source": "example_pkg/config.py#L1", "title": "generated citation"}
        ] * 10
        data["documents"] = [document] * 30
        with self.assertRaises(ContextError):
            self.evaluate(data)

    def test_resolver_injection_keeps_scoring_offline(self) -> None:
        """A supplied resolver scores without a filesystem; scope still applies."""
        report = graph.evaluate_graph_evidence(
            packet(), revision_state="matching", resolve=lambda citation: False
        )
        self.assertEqual(report.resolved_line_citations, 0)
        self.assertEqual(report.resolution_rate, 0.0)
        self.assertFalse(report.meets_gate())

    def test_evidence_without_line_claims_scores_as_resolved(self) -> None:
        data = packet()
        data["documents"] = [data["documents"][2]]
        report = self.evaluate(data)
        self.assertEqual(report.line_citations, 0)
        self.assertEqual(report.resolution_rate, 1.0)
        self.assertTrue(report.meets_gate())


class GraphPacketDeliveryCompatibilityTests(unittest.TestCase):
    """The fixture must survive the shared validator, not just the graph checks.

    This is the compatibility evidence issue #876 asks for: a local graph packet
    reaches a recipient through the existing delivery path with no OAuth
    principal, no workspace, and no provider SDK.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = packet()

    def load(self, revision: str | None):
        authorized = {
            "schema": contract.CONNECTION_SCHEMA,
            "capability_version": 1,
            "connection": "example-context",
            "provider": "synthetic-graph",
            "kind": "repository",
            "generation": "generation-one",
            "state": "verified",
            "identity": {"repository_root": "/example/repository"},
            "repositories": ["owner/repo"],
            "recipients": ["claude", "codex"],
            "expires_at": "2026-01-01T13:00:00Z",
            "capabilities": {"search": True, "memory": False, "revision_binding": True},
        }
        encoded = json.dumps(self.data).encode()
        target = self.root / "packet.json"
        target.write_bytes(encoded)
        target.chmod(0o600)
        return contract.load_packet(
            private_root=self.root,
            reference={
                "path": "packet.json",
                "sha256": hashlib.sha256(encoded).hexdigest(),
            },
            policy={
                "schema": contract.POLICY_SCHEMA,
                "connection": "example-context",
                "policy_version": "v1",
                "required": False,
            },
            request=contract.ContextRequest(
                "owner/repo", "work-item-one", "claude", revision
            ),
            authorize=lambda: copy.deepcopy(authorized),
            now=NOW,
        )

    def test_fixture_loads_through_the_shared_delivery_contract(self) -> None:
        validated = self.load("revision-one")
        self.assertEqual(validated.revision_state, "matching")
        summary = validated.shareable_summary()
        self.assertEqual(summary["kind"], "repository")
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["completeness"], "partial")

    def test_stale_graph_is_detected_at_delivery_and_fails_the_gate(self) -> None:
        validated = self.load("revision-two")
        self.assertEqual(validated.revision_state, "stale")
        report = graph.evaluate_graph_evidence(
            validated.private_payload(),
            revision_state=validated.revision_state,
            resolve=lambda citation: True,
        )
        self.assertFalse(report.meets_gate())

    def test_unknown_revision_binding_is_not_reported_as_fresh(self) -> None:
        self.assertEqual(self.load(None).revision_state, "unknown")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
