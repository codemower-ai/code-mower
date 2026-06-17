"""Static release-readiness checks for Code Mower package promotion."""

from __future__ import annotations

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
REQUIRED_ALPHA_TAG_DOC_PATHS = (
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
    version = init_version or pyproject_version
    materialized_versions = _materialized_package_versions(repo_path)
    release_tag = _release_tag_for_version(version) if version else ""
    package_index_spec = f"code-mower=={version}" if version else ""
    doc_blob = "\n".join(docs.values())
    public_hygiene_blobs = {
        relative_path: text.lower()
        for relative_path, text in public_hygiene_docs.items()
    }

    version_docs = [
        relative_path
        for relative_path, text in docs.items()
        if release_tag and release_tag in text
    ]
    missing_release_docs = [
        relative_path
        for relative_path in REQUIRED_ALPHA_TAG_DOC_PATHS
        if release_tag and release_tag not in docs.get(relative_path, "")
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
            check_id="release-tag-docs-current",
            title="Current release tag is present in public install docs",
            status="pass" if release_tag and not missing_release_docs else "fail",
            evidence=release_tag or "missing version",
            detail={
                "docs": version_docs,
                "required_docs": list(REQUIRED_ALPHA_TAG_DOC_PATHS),
                "missing_docs": missing_release_docs,
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
    next_actions = [
        {
            "id": "dry-run-release-workflow",
            "title": "Run the release workflow without publishing",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                "--ref main -f publish_testpypi=false -f publish_pypi=false"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "publish-testpypi-candidate",
            "title": "Publish the verified distribution to TestPyPI",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                "--ref main -f publish_testpypi=true -f publish_pypi=false"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "testpypi-install-rehearsal",
            "title": "Install from TestPyPI in a fresh toy repo",
            "command": (
                "code-mower migration package-install-rehearsal "
                f"--package-spec {package_index_spec} "
                "--pip-index-url https://test.pypi.org/simple/ "
                "--pip-extra-index-url https://pypi.org/simple/ "
                "--json"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["testpypi_project"],
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
