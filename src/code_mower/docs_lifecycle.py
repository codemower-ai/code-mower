"""Classify and validate the repository documentation lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml

from . import __version__


MANIFEST_PATH = "docs/docs-manifest.yml"
INDEX_PATH = "docs/README.md"
SCHEMA = "code_mower.docsManifest.v1"
STATUSES = frozenset({"canonical", "supporting", "frozen", "archived"})
IMMUTABLE_STATUSES = frozenset({"frozen", "archived"})
JOURNEY_SUBJECTS = (
    "installation",
    "quickstart",
    "upgrade",
    "board-operations",
    "troubleshooting",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _markdown_paths(repo_path: Path) -> set[str]:
    docs = repo_path / "docs"
    if not docs.is_dir():
        return set()
    return {path.relative_to(repo_path).as_posix() for path in docs.rglob("*.md") if path.is_file()}


def _load_manifest(repo_path: Path) -> tuple[dict[str, Any], list[str]]:
    path = repo_path / MANIFEST_PATH
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, [f"{MANIFEST_PATH}: {exc}"]
    except yaml.YAMLError as exc:
        return {}, [f"{MANIFEST_PATH}: invalid YAML: {exc}"]
    if not isinstance(payload, dict):
        return {}, [f"{MANIFEST_PATH}: top level must be an object"]
    return payload, []


def validate_manifest(repo_path: Path) -> dict[str, Any]:
    """Validate complete classification, canonical ownership, and frozen bytes."""

    repo_path = repo_path.expanduser().resolve()
    payload, problems = _load_manifest(repo_path)
    rows = payload.get("documents") if isinstance(payload, dict) else None
    if payload.get("schema") != SCHEMA:
        problems.append(f"{MANIFEST_PATH}: schema must be {SCHEMA}")
    if not isinstance(rows, list):
        problems.append(f"{MANIFEST_PATH}: documents must be a list")
        rows = []

    inventory: dict[str, dict[str, Any]] = {}
    canonical_subjects: dict[str, str] = {}
    status_counts = {status: 0 for status in sorted(STATUSES)}
    for index, raw in enumerate(rows):
        label = f"{MANIFEST_PATH}: documents[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label} must be an object")
            continue
        allowed = {"path", "status", "subject", "sha256"}
        unexpected = sorted(set(raw) - allowed)
        if unexpected:
            problems.append(f"{label} has unexpected keys: {', '.join(unexpected)}")
        path = raw.get("path")
        status = raw.get("status")
        if not isinstance(path, str) or not path:
            problems.append(f"{label}.path must be non-empty text")
            continue
        normalized = PurePosixPath(path).as_posix()
        if (
            normalized != path
            or ".." in PurePosixPath(path).parts
            or not path.startswith("docs/")
            or not path.endswith(".md")
        ):
            problems.append(f"{label}.path must be a normalized docs/*.md path")
            continue
        if path in inventory:
            problems.append(f"{label}.path duplicates {path}")
            continue
        if status not in STATUSES:
            problems.append(f"{label}.status must be one of {', '.join(sorted(STATUSES))}")
            continue
        inventory[path] = raw
        status_counts[status] += 1

        subject = raw.get("subject")
        if status == "canonical":
            if not isinstance(subject, str) or not subject.strip():
                problems.append(f"{label}.subject is required for canonical documents")
            elif subject in canonical_subjects:
                problems.append(
                    f"{label}.subject duplicates {subject!r} owned by {canonical_subjects[subject]}"
                )
            else:
                canonical_subjects[subject] = path
        elif subject is not None:
            problems.append(f"{label}.subject is allowed only for canonical documents")

        digest = raw.get("sha256")
        if status in IMMUTABLE_STATUSES:
            if not isinstance(digest, str) or len(digest) != 64:
                problems.append(f"{label}.sha256 is required for {status} documents")
            else:
                document = repo_path / path
                if document.is_file() and _sha256(document) != digest:
                    problems.append(f"{path}: immutable content differs from docs manifest")
        elif digest is not None:
            problems.append(f"{label}.sha256 is allowed only for immutable documents")

        if status == "supporting":
            document = repo_path / path
            try:
                text = document.read_text(encoding="utf-8")
            except OSError:
                text = ""
            package_spec = f"code-mower=={__version__}"
            if package_spec in text:
                problems.append(
                    f"{path}: supporting documents must link to canonical install "
                    f"guidance instead of repeating the current pin {package_spec}"
                )

    actual = _markdown_paths(repo_path)
    declared = set(inventory)
    missing = sorted(actual - declared)
    stale = sorted(declared - actual)
    if missing:
        problems.append("unclassified Markdown documents: " + ", ".join(missing))
    if stale:
        problems.append("manifest paths that do not exist: " + ", ".join(stale))
    missing_journey = [subject for subject in JOURNEY_SUBJECTS if subject not in canonical_subjects]
    if missing_journey:
        problems.append(
            "maintained journey is missing canonical subjects: " + ", ".join(missing_journey)
        )

    expected_index = render_index(payload, repo_path=repo_path)
    index_path = repo_path / INDEX_PATH
    actual_index = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    if expected_index != actual_index:
        problems.append(f"{INDEX_PATH}: generated index is stale; run docs lifecycle --write-index")

    return {
        "schema": "code_mower.docsLifecycleReport.v1",
        "status": "pass" if not problems else "fail",
        "manifest": MANIFEST_PATH,
        "index": INDEX_PATH,
        "document_count": len(actual),
        "declared_count": len(declared),
        "status_counts": status_counts,
        "canonical_subjects": dict(sorted(canonical_subjects.items())),
        "problems": problems,
    }


def _title(repo_path: Path, relative_path: str) -> str:
    try:
        lines = (repo_path / relative_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return relative_path
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return Path(relative_path).stem.replace("-", " ").title()


def render_index(payload: dict[str, Any], *, repo_path: Path | None = None) -> str:
    """Render the stable documentation entry point from canonical ownership."""

    root = (repo_path or Path.cwd()).expanduser().resolve()
    rows = payload.get("documents")
    documents = rows if isinstance(rows, list) else []
    canonical = sorted(
        (
            (str(row.get("subject", "")), str(row.get("path", "")))
            for row in documents
            if isinstance(row, dict)
            and row.get("status") == "canonical"
            and row.get("path") != INDEX_PATH
        ),
        key=lambda item: item[0],
    )
    supporting_count = sum(
        1 for row in documents if isinstance(row, dict) and row.get("status") == "supporting"
    )
    frozen_count = sum(
        1 for row in documents if isinstance(row, dict) and row.get("status") == "frozen"
    )
    archived_count = sum(
        1 for row in documents if isinstance(row, dict) and row.get("status") == "archived"
    )
    lines = [
        "# Code Mower documentation",
        "",
        "<!-- Generated from docs/docs-manifest.yml. Run `python -m "
        "code_mower.docs_lifecycle --write-index` after changing canonical ownership. -->",
        "",
        "Start with the canonical guide for the task at hand. Supporting documents add",
        "detail without redefining these contracts. Frozen and archived documents preserve",
        "historical release evidence and are not current operating guidance.",
        "",
        "## Maintained user journey",
        "",
    ]
    canonical_by_subject = dict(canonical)
    for index, subject in enumerate(JOURNEY_SUBJECTS, start=1):
        path = canonical_by_subject.get(subject)
        if not path:
            continue
        relative = Path(path).relative_to("docs").as_posix()
        lines.append(f"{index}. [{_title(root, path)}]({relative})")
    lines.extend(
        [
            "",
            "## Canonical guides",
            "",
            "| Subject | Guide |",
            "| --- | --- |",
        ]
    )
    for subject, path in canonical:
        relative = Path(path).relative_to("docs").as_posix()
        lines.append(
            f"| {subject.replace('-', ' ').title()} | [{_title(root, path)}]({relative}) |"
        )
    lines.extend(
        [
            "",
            "## Lifecycle",
            "",
            f"The manifest currently classifies {supporting_count} supporting, "
            f"{frozen_count} frozen, and {archived_count} archived document(s).",
            "See [Documentation lifecycle](documentation-lifecycle.md) before adding, moving,",
            "or changing release-sensitive documentation.",
            "",
        ]
    )
    return "\n".join(lines)


def write_index(repo_path: Path) -> None:
    payload, problems = _load_manifest(repo_path)
    if problems:
        raise ValueError("; ".join(problems))
    (repo_path / INDEX_PATH).write_text(
        render_index(payload, repo_path=repo_path), encoding="utf-8"
    )


def _render_text(report: dict[str, Any]) -> str:
    lines = [
        "Code Mower documentation lifecycle",
        "",
        f"status: {report['status']}",
        f"documents: {report['document_count']}",
    ]
    for status, count in report["status_counts"].items():
        lines.append(f"{status}: {count}")
    if report["problems"]:
        lines.extend(["", "Problems:"])
        lines.extend(f"- {problem}" for problem in report["problems"])
    return "\n".join(lines) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--write-index", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo_path = Path(args.repo_path).expanduser().resolve()
    if args.write_index:
        write_index(repo_path)
    report = validate_manifest(repo_path)
    print(
        json.dumps(report, indent=2, sort_keys=True) if args.json else _render_text(report), end=""
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
