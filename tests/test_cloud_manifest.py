from __future__ import annotations

import tempfile
from pathlib import Path

from code_mower.cloud_client import BUNDLE_MANIFEST_FILENAME, CloudBundleError, build_cloud_bundle
from code_mower.cloud_client.manifest import load_bundle_manifest, report_path_from_manifest


def test_load_bundle_manifest_reads_valid_bundle_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reviewer-value-report.md"
        report.write_text("# Value report\n", encoding="utf-8")
        build_cloud_bundle(
            reports=[(report, "value-report")],
            output_dir=root / "bundle",
            repo_slug="codemower-ai/code-mower",
        )

        manifest = load_bundle_manifest(root / "bundle")

        assert manifest["repo_slug"] == "codemower-ai/code-mower"
        assert manifest["included_reports"][0]["kind"] == "value-report"


def test_load_bundle_manifest_rejects_missing_or_unsupported_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            load_bundle_manifest(root)
        except CloudBundleError as exc:
            assert "bundle manifest not found" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing manifest rejection")

        (root / BUNDLE_MANIFEST_FILENAME).write_text('{"schema": "wrong"}', encoding="utf-8")
        try:
            load_bundle_manifest(root)
        except CloudBundleError as exc:
            assert "unsupported bundle manifest schema" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected unsupported manifest rejection")


def test_report_path_from_manifest_rejects_unsafe_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reports" / "value.md"
        report.parent.mkdir()
        report.write_text("# Value report\n", encoding="utf-8")

        assert report_path_from_manifest(root, "reports/value.md") == report.resolve()

        for target in ("", "/tmp/value.md", "../value.md", "reports/../value.md"):
            try:
                report_path_from_manifest(root, target)
            except CloudBundleError as exc:
                assert "unsafe report target" in str(exc)
            else:  # pragma: no cover
                raise AssertionError(f"expected unsafe target rejection for {target!r}")


def test_report_path_from_manifest_rejects_missing_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            report_path_from_manifest(root, "reports/missing.md")
        except CloudBundleError as exc:
            assert "bundle report file is missing" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing report rejection")
