from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from code_mower.cloud_client import CloudBundleError, included_report_payloads


def _manifest_entry(target: str = "reports/value-report.md") -> dict[str, object]:
    return {
        "kind": "value-report",
        "target": target,
        "bytes": 12,
        "source_basename": "reviewer-value-report.md",
    }


def _raise_on_missing_error(func, expected: str) -> None:
    try:
        func()
    except CloudBundleError as exc:
        assert expected in str(exc)
    else:  # pragma: no cover
        raise AssertionError(f"expected CloudBundleError containing {expected!r}")


def test_included_report_payloads_metadata_only_does_not_read_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = {"included_reports": [_manifest_entry("reports/missing.md")]}

        payloads = included_report_payloads(manifest, root, include_reports=False)

        assert payloads == [
            {
                "kind": "value-report",
                "target": "reports/missing.md",
                "bytes": 12,
                "source_basename": "reviewer-value-report.md",
            }
        ]
        assert "text" not in payloads[0]


def test_included_report_payloads_reads_report_text_when_enabled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reports" / "value-report.md"
        report.parent.mkdir()
        report.write_text("# Value report\n", encoding="utf-8")
        manifest = {"included_reports": [_manifest_entry()]}

        payloads = included_report_payloads(manifest, root, include_reports=True)

        assert payloads[0]["text"] == "# Value report\n"
        assert payloads[0]["target"] == "reports/value-report.md"


def test_included_report_payloads_rejects_non_object_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = {"included_reports": ["reports/value-report.md"]}

        _raise_on_missing_error(
            lambda: included_report_payloads(manifest, root, include_reports=False),
            "non-object included_reports entry",
        )


def test_included_report_payloads_rejects_non_utf8_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reports" / "value-report.md"
        report.parent.mkdir()
        report.write_bytes(b"\xff\xfe")
        manifest = {"included_reports": [_manifest_entry()]}

        _raise_on_missing_error(
            lambda: included_report_payloads(manifest, root, include_reports=True),
            "report is not UTF-8 text",
        )


def test_included_report_payloads_rejects_oversized_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reports" / "value-report.md"
        report.parent.mkdir()
        report.write_text("too large\n", encoding="utf-8")
        manifest = {"included_reports": [_manifest_entry()]}

        with mock.patch("code_mower.cloud_client.reports.MAX_REPORT_UPLOAD_BYTES", 4):
            _raise_on_missing_error(
                lambda: included_report_payloads(manifest, root, include_reports=True),
                "exceeds 4 byte limit",
            )
