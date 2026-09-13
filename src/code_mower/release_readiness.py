"""Static release-readiness checks for Code Mower package promotion."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from . import package as package_module
from . import versioning as code_mower_versioning


RELEASE_DOC_PATHS = (
    "README.md",
    "docs/quickstart.md",
    "docs/try-in-10-minutes.md",
    "docs/first-user-install-rehearsal.md",
    "docs/pypi-release.md",
    "docs/public-release-checklist.md",
)
REQUIRED_PUBLIC_PACKAGE_SPEC_DOC_PATHS = (
    "README.md",
    "docs/quickstart.md",
    "docs/try-in-10-minutes.md",
    "docs/first-user-install-rehearsal.md",
    "docs/public-release-checklist.md",
)
PUBLIC_HYGIENE_DOC_PATHS = (
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/dependabot.yml",
)
PACKAGE_INDEX_SETUP_URLS = {
    "github_environments": (
        "https://github.com/codemower-ai/code-mower/settings/environments"
    ),
    "release_workflow": (
        "https://github.com/codemower-ai/code-mower/actions/workflows/release.yml"
    ),
    "testpypi_project": "https://test.pypi.org/project/code-mower/",
    "testpypi_trusted_publishers": (
        "https://test.pypi.org/manage/project/code-mower/settings/publishing/"
    ),
    "pypi_project": "https://pypi.org/project/code-mower/",
    "pypi_trusted_publishers": (
        "https://pypi.org/manage/project/code-mower/settings/publishing/"
    ),
}


def _release_check(
    *,
    check_id: str,
    title: str,
    status: str,
    evidence: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": check_id,
        "title": title,
        "status": status,
        "evidence": evidence,
    }
    if detail:
        check["detail"] = detail
    return check


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _python_package_version(repo_path: Path) -> str:
    init_text = _read_text_if_exists(repo_path / "src" / "code_mower" / "__init__.py")
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", init_text)
    return match.group(1) if match else ""


def _pyproject_version(repo_path: Path) -> str:
    pyproject_text = _read_text_if_exists(repo_path / "pyproject.toml")
    match = re.search(r"^version\s*=\s*[\"']([^\"']+)[\"']", pyproject_text, re.MULTILINE)
    return match.group(1) if match else ""


def _materialized_package_versions(repo_path: Path) -> dict[str, Any]:
    try:
        plan = package_module.render_package_plan(
            package_module.load_config(
                repo_path / "src" / "code_mower" / "templates" / "code-mower.example.yml"
            ),
            package_module.load_provider_templates(
                repo_path / "src" / "code_mower" / "templates" / "providers.yml"
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            package_module.materialize_package_plan(
                plan,
                output_dir=output_dir,
                repo_root=repo_path,
                force=True,
            )
            return {
                "error": "",
                "init_version": _python_package_version(output_dir),
                "pyproject_version": _pyproject_version(output_dir),
            }
    except Exception as exc:  # pragma: no cover - exercised through status output.
        return {
            "error": str(exc),
            "init_version": "",
            "pyproject_version": "",
        }


def _committed_manifest(repo_path: Path) -> dict[str, Any] | None:
    text = _read_text_if_exists(
        repo_path / package_module.COMMITTED_PACKAGE_MANIFEST
    )
    try:
        manifest = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return None
    return manifest if isinstance(manifest, dict) else None


def _committed_manifest_version(repo_path: Path) -> str:
    manifest = _committed_manifest(repo_path) or {}
    package = manifest.get("package")
    version = package.get("version") if isinstance(package, dict) else None
    return version if isinstance(version, str) else ""


MANIFEST_ENTRY_KEYS = ("target", "source", "kind")

POST_MERGE_RUNBOOK_HEADING = "Post-Merge Release Runbook"


def _manifest_rows(manifest: dict[str, Any]) -> list[Any]:
    rows = manifest.get("files_written")
    return list(rows) if isinstance(rows, list) else []


def _malformed_manifest_rows(manifest: dict[str, Any]) -> list[str]:
    """Describe rows that cannot be compared, instead of dropping them."""

    problems: list[str] = []
    for index, row in enumerate(_manifest_rows(manifest)):
        if not isinstance(row, dict):
            problems.append(f"row {index} is not an object")
            continue
        missing = [key for key in MANIFEST_ENTRY_KEYS if key not in row]
        if missing:
            problems.append(f"row {index} is missing {', '.join(missing)}")
        unexpected = sorted(set(row) - set(MANIFEST_ENTRY_KEYS))
        if unexpected:
            problems.append(f"row {index} has unexpected {', '.join(unexpected)}")
        non_text = sorted(
            key
            for key in MANIFEST_ENTRY_KEYS
            if key in row and not isinstance(row[key], str)
        )
        if non_text:
            problems.append(f"row {index} has non-string {', '.join(non_text)}")
    return problems


def _duplicate_manifest_targets(manifest: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in _manifest_rows(manifest):
        if not isinstance(row, dict):
            continue
        target = row.get("target")
        if not isinstance(target, str):
            continue
        if target in seen:
            duplicates.add(target)
        seen.add(target)
    return sorted(duplicates)


def _manifest_inventory(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    inventory: dict[str, dict[str, str]] = {}
    for entry in _manifest_rows(manifest):
        if isinstance(entry, dict):
            inventory[str(entry.get("target", ""))] = {
                "source": str(entry.get("source", "")),
                "kind": str(entry.get("kind", "")),
            }
    return inventory


def _committed_manifest_drift(repo_path: Path) -> dict[str, Any]:
    """Compare the whole normalized committed manifest with a fresh generation.

    A stale committed artifact is a release defect: it is the published record of
    the standalone package surface. The gate is strict equality of the whole
    normalized manifest, so malformed rows, duplicated targets, and extra rows
    cannot normalize into apparent agreement; the target diagnostics are only
    bounded reporting on top of that equality.
    """

    empty = {
        "matches": False,
        "error": "",
        "malformed_rows": [],
        "duplicate_targets": [],
        "missing_targets": [],
        "unexpected_targets": [],
        "changed_targets": [],
        "committed_row_count": 0,
        "generated_row_count": 0,
        "metadata_matches": False,
    }
    committed = _committed_manifest(repo_path)
    if committed is None:
        return {
            **empty,
            "error": "committed package manifest is missing or not valid JSON",
        }
    try:
        generated = package_module.generate_committed_package_manifest(repo_path)
    except Exception as exc:  # pragma: no cover - exercised through status output.
        return {
            **empty,
            "error": str(exc),
            "committed_row_count": len(_manifest_rows(committed)),
        }
    malformed = _malformed_manifest_rows(committed)
    duplicates = _duplicate_manifest_targets(committed)
    normalized = package_module.normalized_package_manifest(committed)
    committed_files = _manifest_inventory(normalized)
    generated_files = _manifest_inventory(generated)
    changed = sorted(
        target
        for target, entry in generated_files.items()
        if target in committed_files and committed_files[target] != entry
    )
    metadata_matches = all(
        normalized.get(key) == generated.get(key)
        for key in ("mode", "package", "output_dir", "deferred_package_files")
    )
    committed_rows = _manifest_rows(committed)
    return {
        "matches": bool(
            not malformed
            and not duplicates
            and normalized == generated
            and len(committed_rows) == len(_manifest_rows(generated))
        ),
        "error": "",
        "malformed_rows": malformed[:20],
        "duplicate_targets": duplicates[:20],
        "missing_targets": sorted(set(generated_files) - set(committed_files))[:20],
        "unexpected_targets": sorted(set(committed_files) - set(generated_files))[:20],
        "changed_targets": changed[:20],
        "committed_row_count": len(committed_rows),
        "generated_row_count": len(_manifest_rows(generated)),
        "metadata_matches": metadata_matches,
    }


def _manifest_matches_generated(drift: dict[str, Any]) -> bool:
    return bool(drift["matches"] and not drift["error"])


def _post_merge_runbook_markers(release_tag: str, package_index_spec: str) -> tuple[str, ...]:
    """Ordered, exact commands the post-merge runbook must publish in sequence."""

    return (
        "gh pr view \"$RELEASE_PR\" --repo \"$REPO\" --json state --jq '.state'",
        'git fetch origin "$RELEASE_SHA"',
        "migration release-readiness --json",
        f'git tag -a {release_tag} "$RELEASE_SHA"',
        f"git push origin refs/tags/{release_tag}",
        "-f publish_testpypi=false -f publish_pypi=false",
        "-f publish_testpypi=true -f publish_pypi=false",
        "--index-url https://test.pypi.org/simple/",
        "-f publish_testpypi=false -f publish_pypi=true",
        f"--package-spec {package_index_spec}",
        "gh run download",
        "--name code-mower-dist",
        "python3.12 -m pip download",
        "sha256",
        "CODE_MOWER_PYPI_PUBLISH",
        f"gh release create {release_tag}",
        "--verify-tag",
        "provider.devin.repository_scope",
        "code-mower release campaign create",
        "--required-providers claude,codex,devin",
        "code-mower board stop --port",
        'board_wait.py" serving 5332 5342 5344',
        "code-mower board doctor",
        "code-mower cloud upload",
        "--dry-run --json",
    )


def _post_merge_runbook_assertions(version: str, release_tag: str) -> tuple[str, ...]:
    """Assertions the post-merge runbook must contain, not merely describe.

    Ordered presence of commands cannot show that an irreversible step is gated:
    each entry here is the assertion whose removal would let the release proceed
    on an unverified merge commit, workflow run, publish-job posture, artifact
    source, Release asset, Devin posture, publish variable, or Board.
    """

    return (
        # The release commit is the merged pull request's own merge commit.
        "--json mergeCommit --jq '.mergeCommit.oid'",
        'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"',
        # Readiness runs from a fresh environment built at that exact commit.
        f'test "$("$RELEASE_CLI" --version)" = "code-mower {version}"',
        "committed-package-manifest-matches-generated",
        "post-merge-release-runbook-asserted",
        "raise SystemExit(f\"release readiness is not ready: {missing or failing}\")",
        # The tag dereferences to that commit locally and on the remote.
        f'test "$(git rev-list -n 1 {release_tag})" = "$RELEASE_SHA"',
        f"test \"$(git ls-remote origin 'refs/tags/{release_tag}^{{}}' | awk '{{print $1}}')\""
        ' = "$RELEASE_SHA"',
        # Every workflow run is asserted, including both publish-job postures.
        'if run.get("workflowName") != EXPECTED_WORKFLOW:',
        'if run.get("headSha") != head_sha:',
        'problems.append(f"{job_name} is {actual}, expected skipped")',
        'problems.append(f"{job_name} is {actual}, expected success")',
        '"$NO_PUBLISH_RUN_ID" workflow_dispatch "$RELEASE_SHA" skipped skipped',
        '"$TESTPYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" success skipped',
        '"$PYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" skipped success',
        '"$RELEASE_EVENT_RUN_ID" release "$RELEASE_SHA" skipped skipped',
        # TestPyPI is the exclusive source of the candidate artifacts.
        f"PIP_CONFIG_FILE=/dev/null python3.12 -m pip download code-mower=={version}",
        "--index-url https://test.pypi.org/simple/ --dest \"$TESTPYPI_DIST_DIR\"",
        '--package-spec "$TESTPYPI_WHEEL"',
        # Production commands reach canonical PyPI explicitly, without caches.
        "--index-url https://pypi.org/simple/ --dest \"$PYPI_DOWNLOAD_DIR\"",
        "--pip-index-url https://pypi.org/simple/",
        "--pip-no-cache",
        # Republishing is impossible before the irreversible release creation.
        'if values.get(name, "false").strip().lower() == "true"',
        'raise SystemExit(f"release-event republishing is enabled: {enabled}")',
        # The Release's own assets are downloaded and compared digest by digest.
        'raise SystemExit(f"{mode} release assets are not acceptable: {problems}")',
        'problems.append("release asset SHA-256 values differ from PROD_DIST_DIR")',
        'assert_release_assets.py" existing',
        # Hosted Devin readiness is required, not reported.
        "--set-transport devin=devin_api_v3",
        'raise SystemExit(f"hosted Devin readiness is blocked: {blocked}")',
        # Boards stop, are waited for, and only then restart from the release.
        'test "$(git -C "$CODE_MOWER_RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"',
        'board_wait.py" gone "$BOARD_PORT"',
        'raise SystemExit(f"ports still not {mode} within {DEADLINE_SECONDS}s: {pending}")',
    )


def _forbidden_runbook_markers() -> tuple[str, ...]:
    """Commands the post-merge runbook must not publish."""

    return (
        'RELEASE_SHA="$(git rev-parse origin/main)"',
        "gh release upload",
        "--pip-extra-index-url https://pypi.org/simple/",
    )


def _document_section(text: str, heading: str) -> str:
    """Return one Markdown section, so a gate reads the runbook and nothing else."""

    start = text.find(heading) if heading else -1
    if start < 0:
        return ""
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def _unordered_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    """Report markers that are missing or appear before their predecessor."""

    problems: list[str] = []
    position = -1
    for marker in markers:
        found = text.find(marker, position + 1)
        if found < 0:
            problems.append(marker)
            continue
        position = found
    return problems


def _release_tag_for_version(version: str) -> str:
    return code_mower_versioning.release_tag_for_version(version)


def _release_docs(repo_path: Path) -> dict[str, str]:
    return {
        relative_path: _read_text_if_exists(repo_path / relative_path)
        for relative_path in RELEASE_DOC_PATHS
    }


def _workflow_jobs(workflow: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(workflow) if workflow.strip() else {}
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    jobs = parsed.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _needs_job(job: Any, required: str) -> bool:
    if not isinstance(job, dict):
        return False
    needs = job.get("needs")
    if isinstance(needs, str):
        return needs == required
    if isinstance(needs, list):
        return required in needs
    return False


def _permissions_include(job: Any, key: str, value: str) -> bool:
    if not isinstance(job, dict):
        return False
    permissions = job.get("permissions")
    return isinstance(permissions, dict) and permissions.get(key) == value


def _job_uses_environment(job: Any, environment: str) -> bool:
    if not isinstance(job, dict):
        return False
    value = job.get("environment")
    if isinstance(value, str):
        return value == environment
    if isinstance(value, dict):
        return value.get("name") == environment
    return False


def _job_uses_action(job: Any, action_prefix: str) -> bool:
    if not isinstance(job, dict):
        return False
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith(action_prefix):
            return True
    return False


def _job_text(job: Any) -> str:
    return yaml.safe_dump(job, sort_keys=True) if isinstance(job, dict) else ""


def render_release_readiness(repo_path: Path) -> dict[str, Any]:
    """Inspect whether the standalone package is ready for package-index promotion."""

    repo_path = repo_path.expanduser().resolve()
    workflow_path = repo_path / ".github" / "workflows" / "release.yml"
    workflow = _read_text_if_exists(workflow_path)
    ci_workflow_path = repo_path / ".github" / "workflows" / "ci.yml"
    ci_workflow = _read_text_if_exists(ci_workflow_path)
    workflow_jobs = _workflow_jobs(workflow)
    testpypi_job = workflow_jobs.get("publish-testpypi")
    pypi_job = workflow_jobs.get("publish-pypi")
    testpypi_job_text = _job_text(testpypi_job)
    pypi_job_text = _job_text(pypi_job)
    docs = _release_docs(repo_path)
    public_hygiene_docs = {
        relative_path: _read_text_if_exists(repo_path / relative_path)
        for relative_path in PUBLIC_HYGIENE_DOC_PATHS
    }
    init_version = _python_package_version(repo_path)
    pyproject_version = _pyproject_version(repo_path)
    manifest_version = _committed_manifest_version(repo_path)
    manifest_drift = _committed_manifest_drift(repo_path)
    version = init_version or pyproject_version
    materialized_versions = _materialized_package_versions(repo_path)
    release_tag = _release_tag_for_version(version) if version else ""
    package_index_spec = f"code-mower=={version}" if version else ""
    doc_blob = "\n".join(docs.values())
    runbook_doc = _document_section(
        docs.get("docs/pypi-release.md", ""),
        f"## {release_tag} {POST_MERGE_RUNBOOK_HEADING}" if release_tag else "",
    )
    runbook_markers = (
        _post_merge_runbook_markers(release_tag, package_index_spec)
        if release_tag and package_index_spec
        else ()
    )
    missing_runbook_markers = (
        _unordered_markers(runbook_doc, runbook_markers)
        if runbook_markers
        else ["unknown release version"]
    )
    runbook_assertions = (
        _post_merge_runbook_assertions(version, release_tag) if release_tag else ()
    )
    missing_runbook_assertions = (
        [marker for marker in runbook_assertions if marker not in runbook_doc]
        if runbook_assertions
        else ["unknown release version"]
    )
    forbidden_runbook_markers = [
        marker for marker in _forbidden_runbook_markers() if marker in runbook_doc
    ]
    public_hygiene_blobs = {
        relative_path: text.lower()
        for relative_path, text in public_hygiene_docs.items()
    }

    version_docs = [
        relative_path
        for relative_path, text in docs.items()
        if release_tag and release_tag in text
    ]
    package_spec_docs = [
        relative_path
        for relative_path, text in docs.items()
        if package_index_spec and package_index_spec in text
    ]
    missing_package_spec_docs = [
        relative_path
        for relative_path in REQUIRED_PUBLIC_PACKAGE_SPEC_DOC_PATHS
        if package_index_spec and package_index_spec not in docs.get(relative_path, "")
    ]
    package_index_docs = [
        relative_path
        for relative_path, text in docs.items()
        if package_index_spec and package_index_spec in text
    ]
    missing_public_hygiene_docs = [
        relative_path
        for relative_path, text in public_hygiene_docs.items()
        if not text
    ]
    redaction_terms = (
        "tokens",
        "private source",
        "raw diffs",
        "raw model transcripts",
        "auth output",
        "security.md",
    )
    public_redaction_docs = ("SUPPORT.md", "CODE_OF_CONDUCT.md")
    missing_redaction_terms = {
        relative_path: [
            term
            for term in redaction_terms
            if term not in public_hygiene_blobs.get(relative_path, "")
        ]
        for relative_path in public_redaction_docs
    }
    missing_redaction_terms = {
        relative_path: terms
        for relative_path, terms in missing_redaction_terms.items()
        if terms
    }

    checks = [
        _release_check(
            check_id="package-version-consistency",
            title="Package versions agree",
            status=(
                "pass"
                if init_version and pyproject_version and init_version == pyproject_version
                else "fail"
            ),
            evidence=(
                f"src/code_mower/__init__.py={init_version or 'missing'}, "
                f"pyproject.toml={pyproject_version or 'missing'}"
            ),
            detail={"init_version": init_version, "pyproject_version": pyproject_version},
        ),
        _release_check(
            check_id="materialized-package-version-consistency",
            title="Materialized package versions agree with source",
            status=(
                "pass"
                if (
                    version
                    and not materialized_versions["error"]
                    and materialized_versions["init_version"] == version
                    and materialized_versions["pyproject_version"] == version
                )
                else "fail"
            ),
            evidence=(
                f"generated src/code_mower/__init__.py="
                f"{materialized_versions['init_version'] or 'missing'}, "
                f"generated pyproject.toml="
                f"{materialized_versions['pyproject_version'] or 'missing'}"
            ),
            detail={
                "source_version": version,
                "generated_init_version": materialized_versions["init_version"],
                "generated_pyproject_version": materialized_versions[
                    "pyproject_version"
                ],
                "error": materialized_versions["error"],
            },
        ),
        _release_check(
            check_id="committed-package-manifest-version",
            title="Committed package manifest version agrees with source",
            status=(
                "pass"
                if (
                    manifest_version
                    and manifest_version == init_version
                    and manifest_version == pyproject_version
                )
                else "fail"
            ),
            evidence=(
                f"code-mower-package-manifest.json={manifest_version or 'missing'}, "
                f"src/code_mower/__init__.py={init_version or 'missing'}, "
                f"pyproject.toml={pyproject_version or 'missing'}"
            ),
            detail={
                "manifest_version": manifest_version,
                "init_version": init_version,
                "pyproject_version": pyproject_version,
            },
        ),
        _release_check(
            check_id="committed-package-manifest-matches-generated",
            title="Committed package manifest matches the current package inventory",
            status="pass" if _manifest_matches_generated(manifest_drift) else "fail",
            evidence=(
                f"{package_module.COMMITTED_PACKAGE_MANIFEST}="
                f"{manifest_drift['committed_row_count']} row(s), "
                f"generated={manifest_drift['generated_row_count']} row(s)"
            ),
            detail=manifest_drift,
        ),
        _release_check(
            check_id="release-workflow-present",
            title="Release workflow exists",
            status="pass" if workflow_path.is_file() else "fail",
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="distribution-build-and-verify",
            title="Release workflow builds and verifies distributions before publish",
            status=(
                "pass"
                if (
                    "  build-distributions:\n" in workflow
                    and "  verify-distributions:\n" in workflow
                    and "    needs: build-distributions\n" in workflow
                    and "python -m build" in workflow
                    and "python -m twine check dist/*" in workflow
                )
                else "fail"
            ),
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="manual-dispatch-gates",
            title="Manual workflow dispatch has separate TestPyPI and PyPI inputs",
            status=(
                "pass"
                if "workflow_dispatch:" in workflow
                and "publish_testpypi:" in workflow
                and "publish_pypi:" in workflow
                else "fail"
            ),
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="testpypi-gate",
            title="TestPyPI publishing is gated separately",
            status=(
                "pass"
                if (
                    _needs_job(testpypi_job, "verify-distributions")
                    and "inputs.publish_testpypi" in testpypi_job_text
                    and "inputs.publish_testpypi == true" in testpypi_job_text
                    and "github.event_name == 'workflow_dispatch'" in testpypi_job_text
                    and "github.event_name == 'release'" in testpypi_job_text
                    and "CODE_MOWER_TESTPYPI_PUBLISH" in testpypi_job_text
                    and _job_uses_environment(testpypi_job, "testpypi")
                    and _permissions_include(testpypi_job, "id-token", "write")
                    and _job_uses_action(testpypi_job, "pypa/gh-action-pypi-publish@")
                    and "https://test.pypi.org/legacy/" in testpypi_job_text
                )
                else "fail"
            ),
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="pypi-gate",
            title="Production PyPI publishing is gated separately",
            status=(
                "pass"
                if (
                    _needs_job(pypi_job, "verify-distributions")
                    and "inputs.publish_pypi" in pypi_job_text
                    and "inputs.publish_pypi == true" in pypi_job_text
                    and "github.event_name == 'workflow_dispatch'" in pypi_job_text
                    and "github.event_name == 'release'" in pypi_job_text
                    and "CODE_MOWER_PYPI_PUBLISH" in pypi_job_text
                    and _job_uses_environment(pypi_job, "pypi")
                    and _permissions_include(pypi_job, "id-token", "write")
                    and _job_uses_action(pypi_job, "pypa/gh-action-pypi-publish@")
                    and "test.pypi.org" not in pypi_job_text
                )
                else "fail"
            ),
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="public-package-spec-docs-current",
            title="Current package-index spec is present in public install docs",
            status="pass" if package_index_spec and not missing_package_spec_docs else "fail",
            evidence=package_index_spec or "missing version",
            detail={
                "release_tag_docs": version_docs,
                "package_spec_docs": package_spec_docs,
                "required_docs": list(REQUIRED_PUBLIC_PACKAGE_SPEC_DOC_PATHS),
                "missing_docs": missing_package_spec_docs,
            },
        ),
        _release_check(
            check_id="package-index-rehearsal-docs",
            title="Package-index rehearsal is documented",
            status=(
                "pass"
                if (
                    package_index_spec
                    and package_index_spec in doc_blob
                    and "--allow-package-index" in doc_blob
                    and "--pip-index-url https://test.pypi.org/simple/" in doc_blob
                    and "--pip-extra-index-url https://pypi.org/simple/" in doc_blob
                    and "package-install-rehearsal" in doc_blob
                )
                else "fail"
            ),
            evidence=package_index_spec or "missing version",
            detail={"docs": package_index_docs},
        ),
        _release_check(
            check_id="ci-package-install-rehearsal",
            title="CI proves the package-installed first-user path",
            status=(
                "pass"
                if (
                    "Package-install first-user rehearsal" in ci_workflow
                    and "package-install-rehearsal" in ci_workflow
                    and '--package-spec "$GITHUB_WORKSPACE"' in ci_workflow
                    and '--work-dir "$RUNNER_TEMP/code-mower-package-install"' in ci_workflow
                    and "--json" in ci_workflow
                )
                else "fail"
            ),
            evidence=str(ci_workflow_path),
        ),
        _release_check(
            check_id="trusted-publishing-runbook",
            title="Trusted publishing setup is documented",
            status=(
                "pass"
                if (
                    "trusted publishing" in doc_blob.lower()
                    and "environment: `testpypi`" in doc_blob
                    and "environment: `pypi`" in doc_blob
                    and "Workflow Dispatch Matrix" in doc_blob
                )
                else "fail"
            ),
            evidence="docs/pypi-release.md",
        ),
        _release_check(
            check_id="post-merge-release-runbook-ordered",
            title="Post-merge runbook publishes the full ordered release sequence",
            status="pass" if not missing_runbook_markers else "fail",
            evidence="docs/pypi-release.md",
            detail={
                "release_tag": release_tag,
                "required_commands": list(runbook_markers),
                "missing_or_out_of_order": missing_runbook_markers,
            },
        ),
        _release_check(
            check_id="post-merge-release-runbook-asserted",
            title="Post-merge runbook asserts every irreversible release gate",
            status=(
                "pass"
                if not missing_runbook_assertions and not forbidden_runbook_markers
                else "fail"
            ),
            evidence="docs/pypi-release.md",
            detail={
                "release_tag": release_tag,
                "required_assertions": list(runbook_assertions),
                "missing_assertions": missing_runbook_assertions,
                "forbidden_commands": forbidden_runbook_markers,
            },
        ),
        _release_check(
            check_id="public-maintainer-docs",
            title="Public maintainer and community files are present",
            status="pass" if not missing_public_hygiene_docs else "fail",
            evidence=", ".join(PUBLIC_HYGIENE_DOC_PATHS),
            detail={"missing_docs": missing_public_hygiene_docs},
        ),
        _release_check(
            check_id="public-docs-linked-from-readme",
            title="Public support, security, and conduct docs are linked from README",
            status=(
                "pass"
                if (
                    "[Support](SUPPORT.md)" in docs.get("README.md", "")
                    and "[Security Policy](SECURITY.md)" in docs.get("README.md", "")
                    and "[Code of Conduct](CODE_OF_CONDUCT.md)" in docs.get("README.md", "")
                )
                else "fail"
            ),
            evidence="README.md",
        ),
        _release_check(
            check_id="public-support-redaction-guidance",
            title="Public support docs warn against sharing sensitive artifacts",
            status="pass" if not missing_redaction_terms else "fail",
            evidence="SUPPORT.md, CODE_OF_CONDUCT.md, SECURITY.md",
            detail={"missing_terms_by_doc": missing_redaction_terms},
        ),
    ]
    failed = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    passed = sum(1 for check in checks if check["status"] == "pass")
    status = "pass" if failed == 0 else "fail"
    release_workflow_ref = release_tag or "main"
    next_actions = [
        {
            "id": "dry-run-release-workflow",
            "title": "Run the release workflow without publishing",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                f"--ref {release_workflow_ref} "
                "-f publish_testpypi=false -f publish_pypi=false"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "publish-testpypi-candidate",
            "title": "Publish the verified distribution to TestPyPI",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                f"--ref {release_workflow_ref} "
                "-f publish_testpypi=true -f publish_pypi=false"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "testpypi-install-rehearsal",
            "title": "Install from TestPyPI in a fresh toy repo",
            "command": (
                "code-mower migration package-install-rehearsal "
                f"--package-spec {package_index_spec} "
                "--allow-package-index "
                "--upgrade-pip "
                "--pip-index-url https://test.pypi.org/simple/ "
                "--pip-extra-index-url https://pypi.org/simple/ "
                "--json"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["testpypi_project"],
        },
        {
            "id": "publish-pypi-release",
            "title": "Publish the verified distribution to production PyPI",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                f"--ref {release_workflow_ref} "
                "-f publish_testpypi=false -f publish_pypi=true"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "pypi-install-rehearsal",
            "title": "Install the published package from production PyPI",
            "command": (
                "code-mower migration package-install-rehearsal "
                f"--package-spec {package_index_spec} "
                "--allow-package-index "
                "--upgrade-pip "
                "--json"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["pypi_project"],
        },
        {
            "id": "compare-artifact-digests",
            "title": "Compare the workflow artifact digests with PyPI before release assets",
            "command": (
                "gh run download \"$PYPI_RUN_ID\" --repo codemower-ai/code-mower "
                "--name code-mower-dist --dir \"$PROD_DIST_DIR\" "
                "&& sha256sum \"$PROD_DIST_DIR\"/*"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["pypi_project"],
        },
        {
            "id": "create-github-release",
            "title": "Attach the exact verified artifacts to the GitHub Release",
            "command": (
                f"gh release create {release_tag or 'RELEASE_TAG'} "
                "\"$PROD_DIST_DIR\"/* --repo codemower-ai/code-mower --verify-tag "
                "--latest --fail-on-no-commits"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
    ]
    return {
        "mode": "code-mower-release-readiness",
        "status": status,
        "repo_path": str(repo_path),
        "version": version,
        "release_tag": release_tag,
        "alpha_tag": release_tag,
        "package_index_spec": package_index_spec,
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "total": len(checks),
        "checks": checks,
        "next_actions": next_actions,
        "setup_urls": PACKAGE_INDEX_SETUP_URLS,
    }


def render_release_readiness_text(payload: dict[str, Any]) -> str:
    lines = [
        "Code Mower release readiness",
        "",
        f"status: {payload['status']}",
        f"version: {payload.get('version') or 'unknown'}",
        f"release_tag: {payload.get('release_tag') or payload.get('alpha_tag') or 'unknown'}",
        f"checks: {payload['passed']} passed, {payload['failed']} failed, {payload['warnings']} warnings",
        "",
        "Checks:",
    ]
    for check in payload["checks"]:
        lines.append(f"- {check['status'].upper()} {check['id']}: {check['title']}")
        lines.append(f"  evidence: {check['evidence']}")
    lines.extend(["", "Setup URLs:"])
    for label, url in payload.get("setup_urls", {}).items():
        lines.append(f"- {label}: {url}")
    lines.extend(["", "Next actions:"])
    for action in payload["next_actions"]:
        lines.append(f"- {action['title']}")
        lines.append(f"  {action['command']}")
        if action.get("url"):
            lines.append(f"  {action['url']}")
    return "\n".join(lines) + "\n"
