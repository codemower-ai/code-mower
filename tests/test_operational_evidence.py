"""Independent acceptance invariants for local release observations."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_mower import doctor, operational_evidence as evidence, release_qualify
from code_mower.doctor_checks.models import DoctorReport
from code_mower.remote_session import FakeProvider, RemoteError, RemoteSessions

NOW = datetime.now(timezone.utc).replace(microsecond=0)
BINDING = "a" * 64
HEAD = "b" * 40
COMMIT = "c" * 40
MANIFEST = "d" * 64


def record():
    result = {
        "schema": evidence.SCHEMA,
        "binding": BINDING,
        "head": HEAD,
        "release_commit": COMMIT,
        "observations": {},
    }
    values = {
        "implementation": {"state": "complete", "head": HEAD},
        "provider": {"state": "exited", "cancellation": "accepted"},
        "review": {"head": HEAD, "verdict": "pass", "eligible": True, "ci": "pass", "gate": "pass"},
        "package": {
            "head": HEAD,
            "release_commit": COMMIT,
            "artifact_sha256": "e" * 64,
            "contains_head": True,
            "published": True,
        },
        "usage": {
            "authorized_acu_cap": 6,
            "observed_acu": 0.0,
            "settled_acu": None,
            "settled_usd": None,
        },
        "ingestion": {
            "manifest_sha256": MANIFEST,
            "stored": True,
            "accepted_events": 3,
            "reports": 0,
        },
        "aggregate": {"manifest_sha256": MANIFEST, "state": "fresh", "visible": True},
    }
    for name, fields in values.items():
        source = sorted(evidence.SOURCES[name])[0]
        if name == "usage":
            source = "provider_usage"
        result["observations"][name] = dict(
            fields, source=source, binding=BINDING, observed_at=NOW.isoformat(), coverage="complete"
        )
    return result


def checks(value):
    return {row["id"]: row for row in evidence.acceptance_report(value, now=NOW)["checks"]}


class AcceptanceTests(unittest.TestCase):
    def test_independent_complete_evidence_and_unsettled_zero(self):
        result = evidence.acceptance_report(record(), now=NOW)
        rows = {row["id"]: row for row in result["checks"]}
        self.assertEqual(rows["usage_settlement"]["status"], "warn")
        self.assertEqual(rows["usage_settlement"]["reason"], "usage_unsettled")
        self.assertTrue(
            all(row["status"] == "pass" for name, row in rows.items() if name != "usage_settlement")
        )
        self.assertEqual(
            result["usage"],
            {
                "authorized_acu_cap": 6,
                "observed_acu": 0.0,
                "settled_acu": None,
                "settled_usd": None,
            },
        )
        self.assertEqual(result["authority"], "none")
        self.assertNotIn(BINDING, json.dumps(result))
        self.assertNotIn(HEAD, json.dumps(result))

    def test_completion_and_cancel_ack_do_not_imply_exit(self):
        for state in ["active", "unknown"]:
            with self.subTest(state=state):
                value = record()
                value["observations"]["provider"]["state"] = state
                rows = checks(value)
                self.assertEqual(rows["implementation"]["status"], "pass")
                self.assertNotEqual(rows["provider_quiescence"]["status"], "pass")

    def test_suspension_is_quiescence_not_an_exit_or_success_claim(self):
        value = record()
        value["observations"]["provider"]["state"] = "suspended"
        value["observations"]["implementation"]["state"] = "failed"
        rows = checks(value)
        self.assertEqual(rows["provider_quiescence"]["status"], "pass")
        self.assertNotEqual(rows["implementation"]["status"], "pass")
        self.assertIn("suspended", rows["provider_quiescence"]["message"])

    def test_user_cancellation_is_not_a_completed_failed_repair(self):
        value = record()
        value["observations"]["implementation"]["state"] = "user_cancelled_before_delivery"
        row = checks(value)["implementation"]
        self.assertEqual(row["status"], "skip")
        self.assertEqual(row["reason"], "user_cancelled_before_delivery")

    def test_source_freshness_and_coverage_are_independent(self):
        for section, check in [
            ("provider", "provider_quiescence"),
            ("aggregate", "aggregate_visibility"),
        ]:
            value = record()
            value["observations"][section]["observed_at"] = (
                NOW - timedelta(seconds=301)
            ).isoformat()
            self.assertEqual(checks(value)[check]["reason"], "observation_stale")
            self.assertEqual(checks(value)["ingestion_storage"]["status"], "pass")
        for coverage in ["partial", "unavailable"]:
            value = record()
            value["observations"]["provider"]["coverage"] = coverage
            self.assertEqual(checks(value)["provider_quiescence"]["reason"], "partial_coverage")

    def test_passing_publisher_or_ineligible_review_cannot_pass(self):
        for field, value in [
            ("eligible", False),
            ("gate", "unknown"),
            ("ci", "blocked"),
            ("verdict", "blocked"),
        ]:
            payload = record()
            payload["observations"]["review"][field] = value
            self.assertNotEqual(checks(payload)["reviewed_head"]["status"], "pass")

    def test_head_release_and_manifest_mismatch_rejected(self):
        for section, field, value in [
            ("review", "head", "f" * 40),
            ("package", "release_commit", "f" * 40),
            ("aggregate", "manifest_sha256", "f" * 64),
            ("provider", "binding", "f" * 64),
        ]:
            payload = record()
            payload["observations"][section][field] = value
            with self.assertRaisesRegex(evidence.EvidenceError, "^operational_evidence_invalid$"):
                checks(payload)

    def test_merged_head_is_not_published_package_evidence(self):
        value = record()
        del value["observations"]["package"]
        self.assertEqual(checks(value)["reviewed_head"]["status"], "pass")
        self.assertNotEqual(checks(value)["published_package"]["status"], "pass")
        for field in ["published", "contains_head"]:
            value = record()
            value["observations"]["package"][field] = False
            self.assertNotEqual(checks(value)["published_package"]["status"], "pass")

    def test_settled_usage_requires_billing_and_complete_coverage(self):
        value = record()
        usage = value["observations"]["usage"]
        usage["settled_acu"] = 0
        with self.assertRaises(evidence.EvidenceError):
            checks(value)
        usage["source"] = "provider_billing"
        self.assertEqual(checks(value)["usage_settlement"]["status"], "pass")
        usage["coverage"] = "partial"
        with self.assertRaises(evidence.EvidenceError):
            checks(value)

    def test_storage_does_not_prove_view_visibility(self):
        for state in ["stale", "failed", "unknown"]:
            value = record()
            value["observations"]["aggregate"]["state"] = state
            self.assertEqual(checks(value)["ingestion_storage"]["status"], "pass")
            self.assertNotEqual(checks(value)["aggregate_visibility"]["status"], "pass")
        value = record()
        del value["observations"]["ingestion"]
        self.assertNotEqual(checks(value)["aggregate_visibility"]["status"], "pass")
        value = record()
        value["observations"]["ingestion"]["reports"] = 1
        self.assertNotEqual(checks(value)["ingestion_storage"]["status"], "pass")

    def test_empty_observations_are_unavailable_not_success(self):
        value = record()
        value["observations"] = {}
        result = evidence.acceptance_report(value, now=NOW)
        self.assertTrue(all(row["status"] == "warn" for row in result["checks"]))
        self.assertTrue(all(amount is None for amount in result["usage"].values()))

    def test_closed_types_ranges_timestamps_and_unknown_fields(self):
        invalid = [
            ("provider", "source", []),
            ("provider", "coverage", {}),
            ("provider", "state", 1),
            ("review", "eligible", 1),
            ("usage", "observed_acu", True),
            ("usage", "observed_acu", float("nan")),
            ("usage", "settled_usd", -1),
            ("usage", "observed_acu", 10**500),
            ("ingestion", "accepted_events", True),
            ("provider", "observed_at", "2026-09-14"),
            ("provider", "observed_at", (NOW + timedelta(seconds=1)).isoformat()),
            ("aggregate", "state", "private arbitrary prose"),
        ]
        for section, field, value in invalid:
            with self.subTest(section=section, field=field, value_type=type(value).__name__):
                payload = record()
                payload["observations"][section][field] = value
                with self.assertRaises(evidence.EvidenceError):
                    checks(payload)
        payload = record()
        payload["observations"]["provider"]["raw_output"] = "private content"
        with self.assertRaises(evidence.EvidenceError):
            checks(payload)

    def test_evaluation_does_not_modify_input(self):
        value = record()
        original = deepcopy(value)
        evidence.acceptance_report(value, now=NOW)
        self.assertEqual(value, original)


class InputAndCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / "evidence.json"
        self.path.write_text(json.dumps(record()))

    def test_invalid_input_is_bounded_and_does_not_echo(self):
        for content in [
            '{"private-data":"never echo this"}',
            "[" * 2000,
            '{"schema":1,"schema":2}',
            "x" * (evidence.MAX_BYTES + 1),
        ]:
            self.path.write_text(content)
            output = io.StringIO()
            with patch("sys.stdout", output):
                status = evidence.report_file(self.path, json_output=True)
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue())["error"], "operational_evidence_invalid")
            self.assertNotIn(str(self.path), output.getvalue())
            self.assertNotIn("never echo", output.getvalue())

    def test_regular_file_only(self):
        with self.assertRaises(evidence.EvidenceError):
            evidence.read_record(self.root)
        symlink = self.root / "link"
        symlink.symlink_to(self.path)
        with self.assertRaises(evidence.EvidenceError):
            evidence.read_record(symlink)
        if hasattr(os, "mkfifo"):
            fifo = self.root / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(evidence.EvidenceError):
                evidence.read_record(fifo)

    def test_release_reporting_and_explicit_required_evidence(self):
        original = self.path.read_bytes()
        for required, status in [("ingestion_storage", 0), ("usage_settlement", 1)]:
            output = io.StringIO()
            with patch("sys.stdout", output):
                actual = release_qualify.main(
                    ["evidence", "--input", str(self.path), "--require", required, "--json"]
                )
            self.assertEqual(actual, status)
            self.assertEqual(json.loads(output.getvalue())["mode"], "read_only")
        self.assertEqual(self.path.read_bytes(), original)

    def test_doctor_reuses_report_without_reading_provider_state(self):
        output = io.StringIO()
        initial = DoctorReport("test-config", "test-templates", None, ())
        with patch.object(doctor, "run_doctor", return_value=initial), patch("sys.stdout", output):
            status = doctor.main(["--operational-evidence", str(self.path), "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(len(result["checks"]), 7)
        self.assertNotIn(str(self.path), output.getvalue())

    def test_invalid_doctor_record_has_closed_error(self):
        self.path.write_text('{"secret":"private text"}')
        output = io.StringIO()
        initial = DoctorReport("test-config", "test-templates", None, ())
        with patch.object(doctor, "run_doctor", return_value=initial), patch("sys.stdout", output):
            status = doctor.main(["--operational-evidence", str(self.path), "--json"])
        self.assertEqual(status, 1)
        self.assertNotIn("private text", output.getvalue())
        self.assertNotIn(str(self.path), output.getvalue())

    def test_request_key_validation_precedes_state_or_provider_mutation(self):
        store = self.root / "sessions"
        provider = FakeProvider(self.root / "provider")
        remote = RemoteSessions(store, provider)
        for command in ["message", "cancel"]:
            for key in ["", " ", "approved fix " * 20]:
                with self.subTest(command=command, key=key), self.assertRaises(RemoteError):
                    remote.run(command, "existing", request=key, prose="approved fix", apply=True)
        self.assertFalse(store.exists())
        preview = remote.run("message", "existing", request="fix-1", prose="approved fix")
        self.assertEqual(preview["mode"], "dry_run")
        self.assertFalse(store.exists())


if __name__ == "__main__":
    unittest.main()
