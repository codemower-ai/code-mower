"""Optional Antigravity SDK probe helpers."""

from __future__ import annotations

import importlib
import importlib.util
from importlib import metadata
from typing import Any

PACKAGE_NAME = "google-antigravity"
IMPORT_NAME = "google.antigravity"
EXPECTED_EXPORTS = (
    "Agent",
    "LocalAgentConfig",
    "CapabilitiesConfig",
    "UsageMetadata",
    "GeminiAPIEndpoint",
    "VertexEndpoint",
)
LOCAL_HARNESS_PATH = "google/antigravity/bin/localharness"


def _dependency_name(requirement: str) -> str:
    text = requirement.split(";", 1)[0].strip()
    for separator in ("<", ">", "=", "!", "~", " "):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
    return text


def _dependency_names(requirements: list[str] | None) -> tuple[list[str], list[str]]:
    required: set[str] = set()
    optional: set[str] = set()
    for requirement in requirements or []:
        name = _dependency_name(requirement)
        if not name:
            continue
        if "extra ==" in requirement:
            optional.add(name)
        else:
            required.add(name)
    return sorted(required), sorted(optional)


def _distribution_details(package_name: str) -> dict[str, Any]:
    try:
        distribution = metadata.distribution(package_name)
    except metadata.PackageNotFoundError:
        return {
            "installed": False,
            "package_version": "",
            "dependencies": [],
            "optional_dependencies": [],
            "has_local_harness_binary": False,
        }
    files = distribution.files or []
    dependencies, optional_dependencies = _dependency_names(distribution.requires)
    return {
        "installed": True,
        "package_version": metadata.version(package_name),
        "dependencies": dependencies,
        "optional_dependencies": optional_dependencies,
        "has_local_harness_binary": any(
            str(path).replace("\\", "/") == LOCAL_HARNESS_PATH for path in files
        ),
    }


def _is_importable(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def probe_antigravity_sdk(*, import_api: bool = False) -> dict[str, Any]:
    """Return metadata-only facts about the optional Antigravity SDK surface."""

    details = _distribution_details(PACKAGE_NAME)
    importable = _is_importable(IMPORT_NAME)
    report: dict[str, Any] = {
        "mode": "antigravity-sdk-probe",
        "package": PACKAGE_NAME,
        "import_name": IMPORT_NAME,
        "installed": details["installed"],
        "package_version": details["package_version"],
        "dependencies": details["dependencies"],
        "optional_dependencies": details["optional_dependencies"],
        "has_local_harness_binary": details["has_local_harness_binary"],
        "importable": importable,
        "import_api": import_api,
        "api_exports": {},
        "research_posture": "optional_research",
        "privacy": {
            "model_call": False,
            "auth_probe": False,
            "source_or_diff": False,
            "raw_output": False,
        },
    }
    if import_api and importable:
        module = importlib.import_module(IMPORT_NAME)
        report["api_exports"] = {
            name: hasattr(module, name) for name in EXPECTED_EXPORTS
        }

    missing_exports = [
        name for name, present in report["api_exports"].items() if not present
    ]
    if not report["installed"]:
        report["status"] = "warn"
        report["message"] = (
            "optional Antigravity SDK package is not installed; install "
            "google-antigravity in a disposable venv before SDK lane research"
        )
    elif not report["importable"]:
        report["status"] = "warn"
        report["message"] = "optional Antigravity SDK package is installed but not importable"
    elif import_api and missing_exports:
        report["status"] = "warn"
        report["message"] = (
            "optional Antigravity SDK import succeeded but expected API exports "
            "are missing"
        )
        report["missing_exports"] = missing_exports
    else:
        report["status"] = "pass"
        report["message"] = "optional Antigravity SDK probe completed without model calls"
    return report
