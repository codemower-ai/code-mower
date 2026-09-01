from __future__ import annotations

import io
import json
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from code_mower import antigravity_sdk_probe
from code_mower import cli as code_mower_cli


class FakeDistribution:
    requires = [
        "absl-py",
        "google-genai>=1.0",
        "pytest>=7.0; extra == 'dev'",
        "pydantic>=2.0",
    ]
    files = [
        "google/antigravity/__init__.py",
        "google/antigravity/bin/localharness",
    ]


class AntigravitySdkProbeTest(unittest.TestCase):
    def test_missing_package_warns_without_import(self) -> None:
        with (
            mock.patch.object(
                antigravity_sdk_probe.metadata,
                "distribution",
                side_effect=antigravity_sdk_probe.metadata.PackageNotFoundError,
            ),
            mock.patch.object(
                antigravity_sdk_probe.importlib.util,
                "find_spec",
                side_effect=ModuleNotFoundError("No module named 'google'"),
            ),
        ):
            report = antigravity_sdk_probe.probe_antigravity_sdk()

        self.assertEqual(report["status"], "warn")
        self.assertFalse(report["installed"])
        self.assertFalse(report["importable"])
        self.assertFalse(report["privacy"]["model_call"])
        self.assertFalse(report["privacy"]["auth_probe"])

    def test_import_probe_reports_expected_exports(self) -> None:
        fake_module = types.SimpleNamespace(
            Agent=object(),
            LocalAgentConfig=object(),
            CapabilitiesConfig=object(),
            UsageMetadata=object(),
            GeminiAPIEndpoint=object(),
            VertexEndpoint=object(),
        )
        with (
            mock.patch.object(
                antigravity_sdk_probe.metadata,
                "distribution",
                return_value=FakeDistribution(),
            ),
            mock.patch.object(
                antigravity_sdk_probe.metadata,
                "version",
                return_value="0.1.15",
            ),
            mock.patch.object(
                antigravity_sdk_probe.importlib.util,
                "find_spec",
                return_value=object(),
            ),
            mock.patch.object(
                antigravity_sdk_probe.importlib,
                "import_module",
                return_value=fake_module,
            ),
        ):
            report = antigravity_sdk_probe.probe_antigravity_sdk(import_api=True)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["package_version"], "0.1.15")
        self.assertTrue(report["has_local_harness_binary"])
        self.assertEqual(
            report["dependencies"],
            ["absl-py", "google-genai", "pydantic"],
        )
        self.assertEqual(report["optional_dependencies"], ["pytest"])
        self.assertTrue(all(report["api_exports"].values()))

    def test_cli_json_surfaces_probe_report(self) -> None:
        probe_report = {
            "mode": "antigravity-sdk-probe",
            "status": "pass",
            "package": "google-antigravity",
            "installed": True,
            "package_version": "0.1.15",
            "importable": True,
            "message": "ok",
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(
                antigravity_sdk_probe,
                "probe_antigravity_sdk",
                return_value=probe_report,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = code_mower_cli.main(
                ["providers", "antigravity-sdk-probe", "--json"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), probe_report)


if __name__ == "__main__":
    unittest.main()
