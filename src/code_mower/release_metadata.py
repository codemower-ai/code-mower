"""Validated, version-neutral metadata for the current Code Mower release."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from packaging.version import Version
import yaml


MANIFEST_PATH = "release.yml"
SCHEMA = "code_mower.release.v1"
_TOP_LEVEL_KEYS = {
    "schema",
    "version",
    "previous_version",
    "previous_wheel_sha256",
    "tag",
    "package_spec",
    "stage",
    "python",
    "documents",
    "required_modules",
    "required_docs",
    "rehearsal_schema",
}
_DOCUMENT_KEYS = {"notes", "qualification", "runbook", "installation", "publication"}


class ReleaseMetadataError(ValueError):
    """The release manifest is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    previous_version: str
    previous_wheel_sha256: str
    tag: str
    package_spec: str
    stage: str
    python_minimum: str
    python_tested: tuple[str, ...]
    documents: dict[str, str]
    required_modules: tuple[str, ...]
    required_docs: tuple[str, ...]
    rehearsal_schema: str

    @property
    def distribution_names(self) -> tuple[str, str]:
        return (
            f"code_mower-{self.version}-py3-none-any.whl",
            f"code_mower-{self.version}.tar.gz",
        )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReleaseMetadataError(f"{label} must be a mapping with string keys")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseMetadataError(f"{label} must be non-empty text")
    return value


def _text_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ReleaseMetadataError(f"{label} must be a non-empty list of strings")
    if len(set(value)) != len(value):
        raise ReleaseMetadataError(f"{label} contains duplicates")
    return tuple(value)


def load_release_metadata(repo_root: Path) -> ReleaseMetadata:
    """Load and validate the repository's single current-release manifest."""

    path = repo_root.expanduser().resolve() / MANIFEST_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ReleaseMetadataError(f"cannot read {MANIFEST_PATH}: {exc}") from exc
    data = _mapping(raw, MANIFEST_PATH)
    missing = sorted(_TOP_LEVEL_KEYS - set(data))
    unexpected = sorted(set(data) - _TOP_LEVEL_KEYS)
    if missing or unexpected:
        raise ReleaseMetadataError(
            f"{MANIFEST_PATH} keys differ: missing={missing}, unexpected={unexpected}"
        )
    if data["schema"] != SCHEMA:
        raise ReleaseMetadataError(f"unsupported release schema {data['schema']!r}")

    version = _text(data["version"], "version")
    previous = _text(data["previous_version"], "previous_version")
    previous_wheel_sha256 = _text(
        data["previous_wheel_sha256"], "previous_wheel_sha256"
    )
    try:
        parsed_version = Version(version)
        parsed_previous = Version(previous)
    except ValueError as exc:
        raise ReleaseMetadataError(f"invalid release version: {exc}") from exc
    if parsed_previous >= parsed_version:
        raise ReleaseMetadataError("previous_version must be older than version")
    if not re.fullmatch(r"[0-9a-f]{64}", previous_wheel_sha256):
        raise ReleaseMetadataError("previous_wheel_sha256 must be a lowercase SHA-256 digest")
    tag = _text(data["tag"], "tag")
    package_spec = _text(data["package_spec"], "package_spec")
    if tag != f"v{version}" or package_spec != f"code-mower=={version}":
        raise ReleaseMetadataError("tag and package_spec must match version")
    stage = _text(data["stage"], "stage")
    if stage not in {"development", "candidate", "stable"}:
        raise ReleaseMetadataError("stage must be development, candidate, or stable")

    python = _mapping(data["python"], "python")
    if set(python) != {"minimum", "tested"}:
        raise ReleaseMetadataError("python must contain exactly minimum and tested")
    python_minimum = _text(python["minimum"], "python.minimum")
    python_tested = _text_list(python["tested"], "python.tested")
    if python_minimum not in python_tested:
        raise ReleaseMetadataError("python.minimum must be present in python.tested")
    if any(not re.fullmatch(r"3\.\d+", item) for item in python_tested):
        raise ReleaseMetadataError("python.tested entries must be major.minor versions")

    documents = _mapping(data["documents"], "documents")
    if set(documents) != _DOCUMENT_KEYS:
        raise ReleaseMetadataError(
            "documents must name notes, qualification, runbook, installation, and publication"
        )
    normalized_documents = {key: _text(value, f"documents.{key}") for key, value in documents.items()}
    required_modules = _text_list(data["required_modules"], "required_modules")
    required_docs = _text_list(data["required_docs"], "required_docs")
    for relative in (*normalized_documents.values(), *required_docs):
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ReleaseMetadataError(f"release path is not repository-relative: {relative}")
        if not (repo_root / candidate).is_file():
            raise ReleaseMetadataError(f"release path does not exist: {relative}")

    rehearsal_schema = _text(data["rehearsal_schema"], "rehearsal_schema")
    return ReleaseMetadata(
        version=version,
        previous_version=previous,
        previous_wheel_sha256=previous_wheel_sha256,
        tag=tag,
        package_spec=package_spec,
        stage=stage,
        python_minimum=python_minimum,
        python_tested=python_tested,
        documents=normalized_documents,
        required_modules=required_modules,
        required_docs=required_docs,
        rehearsal_schema=rehearsal_schema,
    )
