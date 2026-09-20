"""Candidate identity, artifact tampering and pre-tag qualification regressions."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import yaml

from code_mower import __version__, release_readiness

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_candidate", ROOT / "scripts/release_candidate.py")
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
spec = importlib.util.spec_from_file_location("rehearse_v151", ROOT / "scripts/rehearse_v151.py")
rehearsal = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"release_candidate": candidate}):
    spec.loader.exec_module(rehearsal)
SHA = "a" * 40


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        with zipfile.ZipFile(self.dist / candidate.NAMES[0], "w") as archive:
            archive.writestr("code_mower-1.5.1.dist-info/METADATA",
                             "Name: code-mower\nVersion: 1.5.1\nRequires-Dist: PyYAML>=6.0\nRequires-Dist: packaging>=23.2\n")
            for module in candidate.MODULES:
                archive.writestr("code_mower/" + module, b"synthetic")
            for doc in candidate.DOCS:
                archive.writestr("code_mower-1.5.1.data/data/share/code-mower/docs/" + doc, b"synthetic")
        with tarfile.open(self.dist / candidate.NAMES[1], "w:gz") as archive:
            for path in (["src/code_mower/" + m for m in candidate.MODULES] +
                         ["docs/" + d for d in candidate.DOCS]):
                info = tarfile.TarInfo("code_mower-1.5.1/" + path)
                info.size = 9
                archive.addfile(info, io.BytesIO(b"synthetic"))
        self.manifest = {"schema": candidate.SCHEMA, "version": "1.5.1", "source_sha": SHA,
                         "kind": "candidate", "release_pr": 42,
                         "artifacts": {name: candidate.digest(self.dist / name) for name in candidate.NAMES},
                         "inventory": candidate.inspect(self.dist)}
        self.write_manifest()

    def write_manifest(self):
        (self.dist / "candidate.json").write_text(json.dumps(self.manifest))

    def test_exact_pair_passes_but_wrong_source_and_rehearsal_cannot_publish(self):
        candidate.verify(self.dist, SHA, candidate=True)
        with self.assertRaisesRegex(ValueError, "SHA mismatch"):
            candidate.verify(self.dist, "b" * 40, candidate=True)
        self.manifest["kind"] = "rehearsal"
        self.write_manifest()
        candidate.verify(self.dist, SHA)
        with self.assertRaisesRegex(ValueError, "pre-merge"):
            candidate.verify(self.dist, SHA, candidate=True)

    def test_digest_change_or_extra_distribution_refused(self):
        with (self.dist / candidate.NAMES[0]).open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(ValueError, "digest"):
            candidate.verify(self.dist, SHA)
        (self.dist / "unexpected.whl").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "unexpected distribution"):
            candidate.verify(self.dist, SHA)

    def test_inventories_must_match_even_if_digests_are_updated(self):
        with zipfile.ZipFile(self.dist / candidate.NAMES[0], "a") as archive:
            archive.writestr("code_mower/extra.py", b"extra")
        self.manifest["artifacts"][candidate.NAMES[0]] = candidate.digest(self.dist / candidate.NAMES[0])
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "inventory"):
            candidate.verify(self.dist, SHA)

    def test_private_inventory_refused(self):
        with zipfile.ZipFile(self.dist / candidate.NAMES[0], "a") as archive:
            archive.writestr("code_mower/.code-mower/private.json", b"synthetic")
        with self.assertRaisesRegex(ValueError, "private archive"):
            candidate.inspect(self.dist)

    def test_mixed_extra_marker_cannot_hide_a_default_dependency(self):
        wheel = self.dist / candidate.NAMES[0]
        with zipfile.ZipFile(wheel) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files["code_mower-1.5.1.dist-info/METADATA"] += (
            b'Requires-Dist: slack-sdk; python_version >= "3.12" or extra == "coworker"\n'
        )
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        with self.assertRaisesRegex(ValueError, "base dependencies"):
            candidate.inspect(self.dist)

    def test_dirty_or_wrong_checkout_does_not_build(self):
        for outputs in (("b" * 40,), (SHA, " M README.md")):
            with self.subTest(outputs=outputs), patch.object(candidate, "run", side_effect=outputs), \
                    patch.object(candidate.subprocess, "run") as build:
                with self.assertRaises(ValueError):
                    candidate.build(ROOT, self.dist / "new", SHA, None)
                build.assert_not_called()


class RehearsalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        # A future version uses the manifest's wheel identity, not a v1.5.1 key.
        self.wheel = "code_mower-1.5.1-py3-none-any.whl"
        self.manifest = {"source_sha": SHA, "artifacts": {self.wheel: "b" * 64}}
        self.evidence = {"schema": candidate.REHEARSAL_SCHEMA, "status": "pass",
                         "source_sha": SHA, "artifact_sha256": "b" * 64,
                         "checks": list(candidate.REHEARSAL_CHECKS)}

    def verify(self):
        (self.dist / "rehearsal.json").write_text(json.dumps(self.evidence))
        return candidate.verify_rehearsal(self.dist, self.manifest)

    def test_wheel_identity_is_derived_from_manifest_for_later_versions(self):
        self.assertEqual(self.verify(), self.evidence)

    def test_missing_or_ambiguous_wheel_has_clear_identity_error(self):
        for artifacts in ({}, {self.wheel: "b" * 64, "another.whl": "c" * 64}):
            with self.subTest(artifacts=artifacts):
                self.manifest["artifacts"] = artifacts
                with self.assertRaisesRegex(ValueError, "identity requires exactly one wheel"):
                    self.verify()

    def test_every_named_check_is_required_even_with_overall_pass(self):
        self.assertEqual(len(candidate.GRAPHIFY_CHECKS), 3)
        for name in candidate.REHEARSAL_CHECKS:
            with self.subTest(name=name):
                self.evidence["checks"] = [c for c in candidate.REHEARSAL_CHECKS if c != name]
                with self.assertRaisesRegex(ValueError, "missing required rehearsal checks"):
                    self.verify()

    def test_wrong_identity_or_failure_cannot_qualify(self):
        for field, value, error in (
            ("schema", "unknown", "identity"), ("status", "fail", "status"),
            ("source_sha", "c" * 40, "source SHA"), ("artifact_sha256", "c" * 64, "digest"),
            ("checks", None, "checks"), ("checks", [{"not": "a check"}], "checks"),
        ):
            with self.subTest(field=field), patch.dict(self.evidence, {field: value}):
                with self.assertRaisesRegex(ValueError, error):
                    self.verify()


class CanaryCandidateEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prior = self.root / "prior"
        self.final = self.root / "final"
        self.prior_sha = "a" * 40
        self.final_sha = "b" * 40
        self._write_candidate(self.prior, self.prior_sha, 100)
        self._write_candidate(
            self.final, self.final_sha, 101,
            changes={
                "code_mower/audit_publication.py": b"final audit publication",
                "code_mower-1.5.1.data/data/share/code-mower/docs/v151-qualification.md":
                    b"final qualification",
                "code_mower-1.5.1.dist-info/METADATA": self._metadata(b"final description"),
                "code_mower-1.5.1.dist-info/RECORD": b"final record",
            },
        )

    @staticmethod
    def _metadata(description=b"prior description"):
        return (b"Name: code-mower\nVersion: 1.5.1\nSummary: stable\n"
                b"Requires-Dist: PyYAML>=6.0\nRequires-Dist: packaging>=23.2\n\n" + description)

    def _wheel_files(self):
        files = {
            "code_mower-1.5.1.dist-info/METADATA": self._metadata(),
            "code_mower-1.5.1.dist-info/RECORD": b"prior record",
            "code_mower-1.5.1.dist-info/entry_points.txt":
                b"[console_scripts]\ncode-mower=code_mower.cli:main\n",
            "code_mower/audit_publication.py": b"prior audit publication",
            "code_mower/release_readiness.py": b"prior release readiness",
            "code_mower/campaign_adapters.py": b"paid canary surface",
            "code_mower/templates/workflows/local-audit-publication.yml.j2": b"prior template",
            "code_mower/templates/workflows/trailer-comment-labeler.yml.j2": b"prior template",
        }
        files.update({"code_mower/" + module: b"stable required module"
                      for module in candidate.MODULES})
        files.update({"code_mower-1.5.1.data/data/share/code-mower/docs/" + doc:
                      b"stable documentation" for doc in candidate.DOCS})
        return files

    def _write_candidate(self, path, sha, release_pr, changes=None):
        path.mkdir()
        files = self._wheel_files()
        files.update(changes or {})
        with zipfile.ZipFile(path / candidate.NAMES[0], "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        with tarfile.open(path / candidate.NAMES[1], "w:gz") as archive:
            for member in (["src/code_mower/" + name for name in candidate.MODULES] +
                           ["docs/" + name for name in candidate.DOCS]):
                info = tarfile.TarInfo("code_mower-1.5.1/" + member)
                info.size = len(b"synthetic")
                archive.addfile(info, io.BytesIO(b"synthetic"))
        manifest = {
            "schema": candidate.SCHEMA,
            "version": candidate.VERSION,
            "source_sha": sha,
            "kind": "candidate",
            "release_pr": release_pr,
            "artifacts": {name: candidate.digest(path / name) for name in candidate.NAMES},
            "inventory": candidate.inspect(path),
        }
        (path / "candidate.json").write_text(json.dumps(manifest))

    def compare(self):
        with patch.object(candidate, "run", return_value="") as ancestry:
            result = candidate.compare_canary_surface(
                self.prior, self.final, self.prior_sha, self.final_sha, ROOT)
        ancestry.assert_called_once_with(
            "git", "merge-base", "--is-ancestor", self.prior_sha, self.final_sha, cwd=ROOT)
        return result

    def test_closed_non_canary_delta_passes_with_explicit_attestation(self):
        result = self.compare()
        self.assertEqual(result["schema"], candidate.CANARY_EQUIVALENCE_SCHEMA)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["prior_release_pr"], 100)
        self.assertEqual(result["final_release_pr"], 101)
        self.assertTrue(result["metadata_headers_unchanged"])
        self.assertTrue(result["required_canary_members_unchanged"])
        self.assertEqual(result["changed_wheel_members"], sorted({
            "code_mower/audit_publication.py",
            "code_mower-1.5.1.data/data/share/code-mower/docs/v151-qualification.md",
            "code_mower-1.5.1.dist-info/METADATA",
            "code_mower-1.5.1.dist-info/RECORD",
        }))
        self.assertEqual(set(result["changed_wheel_member_sha256"]),
                         set(result["changed_wheel_members"]))
        for digests in result["changed_wheel_member_sha256"].values():
            self.assertRegex(digests["prior"], r"^[0-9a-f]{64}$")
            self.assertRegex(digests["final"], r"^[0-9a-f]{64}$")
            self.assertNotEqual(digests["prior"], digests["final"])

    def test_operational_member_or_inventory_change_fails_closed(self):
        for name, content, error in (
            ("code_mower/campaign_adapters.py", b"changed provider path", "paid-canary surface"),
            ("code_mower/new_dynamic_loader.py", b"new member", "member inventory"),
        ):
            with self.subTest(name=name):
                final = self.root / ("bad-" + name.rsplit("/", 1)[-1])
                self._write_candidate(final, self.final_sha, 101, changes={name: content})
                with patch.object(candidate, "run", return_value=""), \
                        self.assertRaisesRegex(ValueError, error):
                    candidate.compare_canary_surface(
                        self.prior, final, self.prior_sha, self.final_sha, ROOT)

    def test_metadata_header_change_fails_closed(self):
        final = self.root / "bad-metadata"
        metadata = self._metadata(b"final description").replace(b"Summary: stable", b"Summary: changed")
        self._write_candidate(final, self.final_sha, 101, changes={
            "code_mower-1.5.1.dist-info/METADATA": metadata,
            "code_mower-1.5.1.dist-info/RECORD": b"final record",
        })
        with patch.object(candidate, "run", return_value=""), \
                self.assertRaisesRegex(ValueError, "metadata headers"):
            candidate.compare_canary_surface(
                self.prior, final, self.prior_sha, self.final_sha, ROOT)


class WorkflowBindingTests(unittest.TestCase):
    def setUp(self):
        self.candidate_steps = yaml.safe_load((ROOT / ".github/workflows/release-candidate.yml").read_text())["jobs"]["candidate"]["steps"]
        steps = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]["build-distributions"]["steps"]
        self.publish = next(step["run"] for step in steps if "gh run download" in step.get("run", ""))

    def test_candidate_exact_workflow_sha_and_first_attempt_before_checkout(self):
        validation, *following = self.candidate_steps
        self.assertIn("actions/checkout@", following[0]["uses"])
        self.assertNotIn("uses", validation)
        environment = {"GITHUB_REF": "refs/heads/main", "SOURCE_SHA": SHA,
                       "GITHUB_SHA": SHA, "GITHUB_RUN_ATTEMPT": "1", "RELEASE_PR": "1033"}
        for overrides, passed in (
            ({}, True), ({"GITHUB_SHA": "b" * 40}, False),
            ({"GITHUB_RUN_ATTEMPT": "2"}, False), ({"GITHUB_RUN_ATTEMPT": ""}, False),
            ({"GITHUB_REF": "refs/heads/other"}, False), ({"SOURCE_SHA": "main"}, False),
            ({"RELEASE_PR": "0"}, False),
        ):
            with self.subTest(overrides=overrides):
                result = subprocess.run(["/bin/bash", "-c", validation["run"]],
                                        env={**environment, **overrides}, capture_output=True)
                self.assertEqual(result.returncode == 0, passed)

    def test_publication_rejects_wrong_run_identity_before_downloading(self):
        before_download = self.publish.split("gh run download", 1)[0]
        code, = re.findall(r"<<'PY'\n(.*?)\nPY", before_download, re.S)
        run = {"path": ".github/workflows/release-candidate.yml", "event": "workflow_dispatch",
               "head_branch": "main", "head_sha": SHA, "run_attempt": 1,
               "status": "completed", "conclusion": "success",
               "repository": {"full_name": "codemower-ai/code-mower"}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run.json"
            def validate():
                path.write_text(json.dumps(run))
                with patch.object(sys, "argv", ["-", str(path)]), patch.dict(os.environ, SOURCE_SHA=SHA):
                    exec(compile(code, "release.yml run identity", "exec"), {})
            validate()
            for field, bad in (("path", "other.yml"), ("event", "push"), ("head_branch", "other"),
                               ("head_sha", "b" * 40), ("run_attempt", 2), ("run_attempt", "1"),
                               ("status", "in_progress"), ("conclusion", "failure"),
                               ("repository", {"full_name": "someone/fork"})):
                with self.subTest(field=field, bad=bad), patch.dict(run, {field: bad}):
                    with self.assertRaises(AssertionError):
                        validate()
            for field in ("head_sha", "run_attempt"):
                value = run.pop(field)
                with self.subTest(missing=field), self.assertRaises(KeyError):
                    validate()
                run[field] = value

    def test_publication_requires_verified_artifacts_and_named_rehearsal_before_copy(self):
        self.assertNotIn("code_mower-1.5.1-py3-none-any.whl", self.publish)
        self.assertNotIn("python -m build", self.publish)
        verification = self.publish.index("python scripts/release_candidate.py verify")
        self.assertIn("--require-candidate", self.publish[verification:])
        evidence = self.publish.index("verify_rehearsal(Path('candidate'), candidate)")
        self.assertLess(verification, evidence)
        self.assertLess(evidence, self.publish.index("cp candidate/*.whl"))

    def test_ci_exercises_the_installed_wheel_without_claiming_a_candidate(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
        job = jobs["release_rehearsal"]
        self.assertEqual(job["steps"][0]["with"]["ref"], "${{ env.SOURCE_SHA }}")
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("python scripts/release_candidate.py build", commands)
        self.assertIn('python scripts/rehearse_v151.py --dist "$RUNNER_TEMP/rehearsal-dist"', commands)
        self.assertNotIn("--release-pr", commands)
        self.assertIn("release_rehearsal", jobs["package"]["needs"])
        self.assertIn('test "${{ needs.release_rehearsal.result }}" = "success"', jobs["package"]["steps"][0]["run"])


class OfflineGuardTests(unittest.TestCase):
    def test_graph_exception_only_allows_transport_disabled_fixture_git_reads(self):
        repository = "/synthetic/repository"
        environment = {"GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        prefix = ["git", "-C", repository, "--no-optional-locks"]
        for event, args, allowed in (
            ("subprocess.Popen", ["git", prefix + ["rev-parse", "HEAD"], None, environment], True),
            ("subprocess.Popen", ["git", prefix + ["fetch"], None, environment], False),
            ("subprocess.Popen", ["git", prefix + ["config", "x", "y"], None, environment], False),
            ("subprocess.Popen", ["git", prefix + ["cat-file", "--batch"], None, {}], False),
            ("subprocess.Popen", ["git", ["git", "-C", "/other", "rev-parse"], None, environment], False),
            ("subprocess.Popen", ["sh", ["sh", "-c", "true"], None, environment], False),
            ("socket.__new__", [], False), ("os.system", ["true"], False),
            ("os.posix_spawn", ["/bin/sh", [], {}], False),
        ):
            with self.subTest(event=event, args=args):
                # Audit events alone exercise the guard without launching any child.
                code = rehearsal.installed_code(f"sys.audit({event!r}, *{args!r})", repository)
                result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True)
                self.assertEqual(result.returncode == 0, allowed)


class ReleaseContractTests(unittest.TestCase):
    def test_identity_and_readiness(self):
        self.assertEqual(__version__, "1.5.1")
        self.assertEqual(release_readiness.render_release_readiness(ROOT)["status"], "pass")

    def test_removing_exact_candidate_qualification_or_moving_tag_first_blocks(self):
        text = (ROOT / "docs/v151-release-runbook.md").read_text()
        for bad in (text.replace("## 3. Qualify the exact candidate", "## Removed qualification"),
                    text.replace('git tag -a v1.5.1 "$RELEASE_SHA"', "tag removed"),
                    text.replace("aggregate campaign ACU", "unspecified budget")):
            with self.subTest(text=bad[:10]), patch.object(release_readiness, "_read_text_if_exists", return_value=bad):
                order, assertions = release_readiness._candidate_runbook_checks(ROOT)
                self.assertTrue(order or assertions)

    def test_patch_release_contract_keeps_scope_and_observations_explicit(self):
        runbook = (ROOT / "docs/v151-release-runbook.md").read_text()
        qualification = (ROOT / "docs/v151-qualification.md").read_text()
        for marker in (
            "fresh install without uv or pipx",
            "upgrade from v1.5.0",
            "remote observer",
            "safe init",
            "basic Slack lifecycle",
            "Slack telemetry remains deferred to v1.6.0",
            "metadata-only",
            "fresh dashboard",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, runbook)
        self.assertIn("one bounded hosted Board canary", qualification)
        self.assertIn("authorized usage and settled usage are separate facts", qualification)

    def test_later_versions_do_not_revert_to_building_at_publication(self):
        with patch.object(release_readiness, "_python_package_version", return_value="1.5.1"):
            payload = release_readiness.render_release_readiness(ROOT)
        checks = {c["id"]: c for c in payload["checks"]}
        for name in ("distribution-build-and-verify", "post-merge-release-runbook-ordered",
                     "post-merge-release-runbook-asserted", "release-workflow-next-actions-dispatchable"):
            with self.subTest(check=name):
                self.assertEqual(checks[name]["status"], "pass")
        ordered = checks["post-merge-release-runbook-ordered"]["detail"]
        asserted = checks["post-merge-release-runbook-asserted"]["detail"]
        self.assertEqual(ordered["release_tag"], "v1.5.1")
        self.assertIn("docs/v151-release-runbook.md", ordered["required_commands"][0])
        self.assertNotIn("gh release create v1.5.1", ordered["required_commands"])
        self.assertEqual(asserted["required_assertions"],
                         ["merge SHA and retained artifact binding; explicit owner gates"])
        self.assertEqual(payload["next_actions"][0]["id"], "immutable-candidate-first")
        self.assertIn("gh workflow run release-candidate.yml", payload["next_actions"][0]["command"])
        dispatches = [a for a in payload["next_actions"] if "gh workflow run release.yml" in a["command"]]
        self.assertEqual(len(dispatches), 3)
        for action in dispatches:
            self.assertIn("--ref v1.5.1", action["command"])
            self.assertIn('-f candidate_run_id="$CANDIDATE_RUN_ID"', action["command"])

    def test_later_versions_still_reject_missing_candidate_runbook_gates(self):
        original = release_readiness._read_text_if_exists
        runbook_path = ROOT / "docs/v151-release-runbook.md"
        for marker, check_id, detail in (
            ("## 3. Qualify the exact candidate", "post-merge-release-runbook-ordered", "missing_or_out_of_order"),
            ("aggregate campaign ACU", "post-merge-release-runbook-asserted", "missing_assertions"),
        ):
            def read(path, marker=marker):
                text = original(path)
                return text.replace(marker, "") if path == runbook_path else text
            with self.subTest(marker=marker), \
                    patch.object(release_readiness, "_python_package_version", return_value="1.5.1"), \
                    patch.object(release_readiness, "_read_text_if_exists", side_effect=read):
                checks = release_readiness.render_release_readiness(ROOT)["checks"]
            check = next(c for c in checks if c["id"] == check_id)
            self.assertEqual(check["status"], "fail")
            self.assertIn(marker, check["detail"][detail])

    def test_readiness_rejects_missing_candidate_workflow_or_integrity_gates(self):
        original = release_readiness._read_text_if_exists
        candidate_path = ROOT / ".github/workflows/release-candidate.yml"
        publication_path = ROOT / ".github/workflows/release.yml"
        for path, marker in (
            (candidate_path, None),
            (candidate_path, '[[ "$GITHUB_SHA" == "$SOURCE_SHA" ]]'),
            (candidate_path, '[[ "$GITHUB_RUN_ATTEMPT" == 1 ]]'),
            (publication_path, "assert run['head_sha'] == os.environ['SOURCE_SHA']"),
            (publication_path, "assert run['run_attempt'] == 1"),
            (publication_path, "verify_rehearsal(Path('candidate'), candidate)"),
        ):
            def read(selected, path=path, marker=marker):
                text = original(selected)
                return (text.replace(marker, "") if marker else "") if selected == path else text
            with self.subTest(marker=marker), patch.object(release_readiness, "_read_text_if_exists", side_effect=read):
                checks = release_readiness.render_release_readiness(ROOT)["checks"]
            check = next(c for c in checks if c["id"] == "distribution-build-and-verify")
            self.assertEqual(check["status"], "fail")

    def test_historical_v14_records_are_unchanged(self):
        # Recorded from the parent release-prep baseline. No Git history needed in sdist tests.
        expected = json.loads((ROOT / "tests/fixtures/release_identity/v14-evidence-sha256.json").read_text())
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)
