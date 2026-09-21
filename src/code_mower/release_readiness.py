"""Static release-readiness checks for Code Mower package promotion."""

from __future__ import annotations

import json
import posixpath
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from . import docs_lifecycle
from . import package as package_module
from . import release_metadata as release_metadata_module
from .release_identity import check_release_identity
from . import versioning as code_mower_versioning


RELEASE_DOC_PATHS = (
    "README.md",
    "docs/install.md",
    "docs/quickstart.md",
    "docs/try-in-10-minutes.md",
    "docs/first-user-install-rehearsal.md",
    "docs/pypi-release.md",
    "docs/public-release-checklist.md",
    "docs/release-qualification.md",
)
REQUIRED_PUBLIC_PACKAGE_SPEC_DOC_PATHS = (
    "README.md",
    "docs/install.md",
    "docs/quickstart.md",
    "docs/first-user-install-rehearsal.md",
    "docs/public-release-checklist.md",
)
# Current public release and install guidance that must never present a
# TestPyPI candidate install that also names production PyPI as an extra index.
CURRENT_PACKAGE_INDEX_GUIDANCE_DOC_PATHS = (
    "README.md",
    "docs/quickstart.md",
    "docs/try-in-10-minutes.md",
    "docs/first-user-install-rehearsal.md",
    "docs/pypi-release.md",
    "docs/public-release-checklist.md",
    "docs/release-qualification.md",
)
UNSAFE_MULTI_INDEX_MARKER = "--pip-extra-index-url https://pypi.org/simple/"
# Combined-index guidance regresses as a command flag or as prose promising
# production PyPI as an extra index alongside the TestPyPI candidate index.
UNSAFE_MULTI_INDEX_MARKERS = (
    UNSAFE_MULTI_INDEX_MARKER,
    "--extra-index-url https://pypi.org/simple/",
    "dependency-only extra index",
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
REPOSITORY_SLUG = "codemower-ai/code-mower"
# The absolute spellings that address a file inside this repository's tree:
# ``blob`` renders it, ``raw`` serves it, and the segment between the view and
# the path is the git ref. Any other host, owner, or repository addresses a
# different tree, however its URL happens to end.
_REPOSITORY_FILE_URL_PATTERN = re.compile(
    r"^https://github\.com/"
    + re.escape(REPOSITORY_SLUG)
    + r"/(?:blob|raw)/[^/]+/(?P<path>.+)$"
)
_URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
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


def _repository_destination_path(destination: str) -> str:
    """The repository-relative path ``destination`` addresses, else ``""``.

    A destination is resolved, not pattern-matched, so the answer is the one
    file a reader lands on. Two spellings resolve:

    * a relative path, normalized against the repository root, which is where
      README.md sits;
    * an absolute GitHub URL for *this* repository, reduced to the path under
      its ref.

    Everything else -- another repository, another host, a ``mailto:``, a
    scheme-relative ``//host/...``, or a site-root ``/SUPPORT.md`` that GitHub
    does not resolve against the repository -- addresses nothing in this tree
    and returns ``""``. A query string or fragment only decorates a
    destination, so both are dropped before resolving.
    """

    trimmed = destination.strip().partition("#")[0].partition("?")[0]
    if not trimmed:
        return ""

    repository_url = _REPOSITORY_FILE_URL_PATTERN.match(trimmed)
    if repository_url:
        candidate = repository_url.group("path")
    elif (
        _URI_SCHEME_PATTERN.match(trimmed)
        or trimmed.startswith("//")
        or trimmed.startswith("/")
    ):
        return ""
    else:
        candidate = trimmed

    normalized = posixpath.normpath(candidate)
    if normalized in {".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def _links_to_repository_doc(markdown: str, label: str, relative_path: str) -> bool:
    """Whether ``markdown`` links ``label`` at ``relative_path``.

    README.md is also the built package's long description, where a relative
    destination resolves against the package index rather than the repository,
    so repository links there are absolute GitHub URLs for this repository.
    Both spellings satisfy this check, and each has to resolve to
    ``relative_path`` itself -- a neighbouring or nested file such as
    ``docs/SUPPORT.md`` is a different document, and an unrelated URL that
    merely ends in ``/SUPPORT.md`` is a different repository.
    """

    pattern = re.compile(
        r"\[" + re.escape(label) + r"\]\(\s*<?([^)\s>]+)>?[^)]*\)"
    )
    required = posixpath.normpath(relative_path)
    return any(
        _repository_destination_path(destination) == required
        for destination in pattern.findall(markdown)
    )


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object, refusing repeated keys instead of keeping the last.

    ``json.loads`` keeps only the final value for a repeated key, so a committed
    manifest carrying two values for one key -- at the top level or inside any
    row -- would compare as exact while publishing something else.
    """

    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _committed_manifest(repo_path: Path) -> dict[str, Any] | None:
    text = _read_text_if_exists(
        repo_path / package_module.COMMITTED_PACKAGE_MANIFEST
    )
    try:
        manifest = (
            json.loads(text, object_pairs_hook=_reject_duplicate_keys)
            if text.strip()
            else None
        )
    except (json.JSONDecodeError, ValueError):
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
            "error": (
                "committed package manifest is missing, is not a JSON object, "
                "or repeats an object key"
            ),
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
    malformed.extend(
        problem
        for problem in package_module.package_manifest_problems(committed)
        if problem not in malformed
    )
    duplicates = _duplicate_manifest_targets(committed)
    committed_rows = _manifest_rows(committed)
    if malformed:
        return {
            **empty,
            "malformed_rows": malformed[:20],
            "duplicate_targets": duplicates[:20],
            "committed_row_count": len(committed_rows),
            "generated_row_count": len(_manifest_rows(generated)),
        }
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
        "python3.12 -m pip --isolated download",
        "sha256",
        "CODE_MOWER_PYPI_PUBLISH",
        f"gh release create {release_tag}",
        "--verify-tag",
        "code-mower release campaign create",
        "--required-providers claude,codex",
        "code-mower board stop --repo",
        'board_wait.py" serving \\',
        "code-mower board doctor",
        "code-mower release campaign upload --release-tag",
        "code-mower cloud board-snapshot",
        "code-mower cloud upload",
        "--dry-run --json",
    )


def _post_merge_runbook_assertions(version: str, release_tag: str) -> tuple[str, ...]:
    """Assertions the post-merge runbook must contain, not merely describe.

    Ordered presence of commands cannot show that an irreversible step is gated:
    each entry here is the assertion whose removal would let the release proceed
    on an unverified merge commit, workflow run, publish-job posture, artifact
    source, Release asset, authorized campaign, publish variable, or Board.
    """

    assertions = (
        # The release commit is the merged pull request's own merge commit.
        "--json mergeCommit --jq '.mergeCommit.oid'",
        'test "$(git cat-file -t "$RELEASE_SHA")" = "commit"',
        # Readiness runs from a fresh, machine-asserted clean clone of that commit.
        'git clone --no-checkout "https://github.com/$REPO.git" "$RELEASE_CHECKOUT"',
        'test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"',
        'test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"',
        f'test "$("$RELEASE_CLI" --version)" = "code-mower {version}"',
        "committed-package-manifest-matches-generated",
        "post-merge-release-runbook-asserted",
        "raise SystemExit(f\"release readiness is not ready: {missing or failing}\")",
        # The tag dereferences to that commit locally and on the remote.
        f'test "$(git rev-list -n 1 {release_tag})" = "$RELEASE_SHA"',
        f"test \"$(git ls-remote origin 'refs/tags/{release_tag}^{{}}' | awk '{{print $1}}')\""
        ' = "$RELEASE_SHA"',
        # Every dispatch names the exact commit it may build, and the
        # workflow's own identity job fails fast unless it matches.
        '-f expected_sha="$RELEASE_SHA"',
        # Every workflow run is asserted, including both publish-job postures.
        'BUILD_JOBS = ("release-identity", "build-distributions", "verify-distributions")',
        'if str(run.get("databaseId")) != run_id:',
        'if run.get("workflowName") != EXPECTED_WORKFLOW:',
        'if run.get("event") != event:',
        'if run.get("headSha") != head_sha:',
        # A commit can carry more than one tag, so each run is also bound to the
        # release tag it was dispatched for.
        'if run.get("headBranch") != head_branch:',
        'if run.get("status") != "completed" or run.get("conclusion") != "success":',
        'problems.append(f"{job_name} is {actual}, expected skipped")',
        'problems.append(f"{job_name} is {actual}, expected success")',
        f'"$NO_PUBLISH_RUN_ID" workflow_dispatch "$RELEASE_SHA" {release_tag} skipped skipped',
        f'"$TESTPYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" {release_tag} success skipped',
        f'"$PYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" {release_tag} skipped success',
        f'"$RELEASE_EVENT_RUN_ID" release "$RELEASE_SHA" {release_tag} skipped skipped',
        # TestPyPI is the exclusive source of the candidate artifacts. The
        # runtime candidate is wheel-only; the sdist is verified separately
        # with its declared build backend installed from canonical PyPI, so
        # pip's PEP 517 metadata preparation never resolves against TestPyPI.
        f"python3.12 -m pip --isolated download code-mower=={version}",
        "--no-cache-dir --no-deps --only-binary :all:",
        '--no-cache-dir --index-url https://pypi.org/simple/ "setuptools>=77"',
        f'"$RELEASE_PYTHON" -m pip --isolated download code-mower=={version}',
        "--no-cache-dir --no-deps --no-binary :all:",
        "--no-build-isolation --check-build-dependencies",
        "--index-url https://test.pypi.org/simple/ --dest \"$TESTPYPI_DIST_DIR\"",
        '--package-spec "$TESTPYPI_WHEEL"',
        # Production commands reach canonical PyPI explicitly, without caches.
        "--index-url https://pypi.org/simple/ --dest \"$PYPI_DOWNLOAD_DIR\"",
        "--pip-index-url https://pypi.org/simple/",
        "--pip-no-cache",
        # Republishing is impossible before the irreversible release creation.
        # An absent repository variable can inherit an organization value, so
        # each one must exist at repository scope and read false.
        '["gh", "api", f"repos/{repo}/actions/variables/{name}"]',
        'if value is None or value.strip().lower() != "false"',
        '"repository-scope publish variables must exist and equal false: "',
        # The production artifact map proven against canonical PyPI is saved
        # once and then treated as immutable release evidence, so a local file
        # replaced afterwards cannot become a Release asset.
        'PYPI_VERIFIED_MAP="$RELEASE_ENV/pypi-verified-artifacts.json"',
        'test ! -e "$PYPI_VERIFIED_MAP"',
        'Path(os.environ["PYPI_VERIFIED_MAP"]).write_text(',
        'test -s "$PYPI_VERIFIED_MAP"',
        'problems.append("local artifacts differ from the PyPI-verified map")',
        'problems.append("release asset SHA-256 values differ from the PyPI-verified map")',
        # The remote peeled tag is re-resolved on every invocation, including
        # the one immediately before the irreversible release creation.
        'if remote_peeled_tag_sha(repo) != release_sha:',
        'problems.append("remote {release_tag} tag does not peel to the exact release commit")',
        'assert_release_assets.py" pre-create',
        # The Release's own assets are downloaded and compared digest by digest.
        'raise SystemExit(f"{mode} release assets are not acceptable: {problems}")',
        'assert_release_assets.py" existing',
        # The post-create verification is unconditional: it runs for a release
        # this runbook created as well as one it found already present.
        'assert_release_assets.py" created',
        # Notes come from the clean checkout of the exact release commit, and
        # the published body and title are compared with that file.
        '--notes-file "$RELEASE_CHECKOUT/{release_notes}"',
        "notes_path = checkout / RELEASE_NOTES_RELPATH",
        'problems.append("release notes in the exact checkout are empty")',
        # The late gate binds the accepted or created release to the exact
        # clean checkout immediately before the existing/pre-create branch.
        'test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"',
        'test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"',
        'test -s "$RELEASE_CHECKOUT/{release_notes}"',
        'problems.append("release checkout is not the exact release commit")',
        'problems.append("release checkout has uncommitted or untracked changes")',
        'problems.append("release body does not match the exact checkout release notes")',
        'problems.append("release title is not the expected {release_tag} title")',
        # The exact-release source rehearsal cannot reach ambient packages.
        "env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS",
        "-u PIP_NO_INDEX",
        "--pip-args='--isolated --no-cache-dir'",
        # Boards stop, are waited for, and only then restart from the release.
        'test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"',
        # The release checkout is cloned before the tag exists, so the tag is
        # fetched into it before its target is compared with the release commit.
        'git -C "$RELEASE_CHECKOUT" fetch --no-tags origin "+refs/tags/{release_tag}:refs/tags/{release_tag}"',
        'test "$(git -C "$RELEASE_CHECKOUT" rev-list -n 1 {release_tag})" = "$RELEASE_SHA"',
        'board_wait.py" gone 5332',
        # Serving is only satisfied by the expected repository on each port.
        'and row.get("repo") == expected_repo',
        'raise SystemExit("serving mode requires PORT=REPO for every port")',
        '"5332=codemower-ai/code-mower" "5333=$BOARD_5333_REPO"',
        'raise SystemExit(f"ports still not {mode} within {DEADLINE_SECONDS}s: {pending}")',
        # Every restarted Board's own doctor verdict is parsed; the CLI exits
        # zero on warn, so exit status is not the gate.
        '--json >"$BOARD_DOCTOR_DIR/5332.json"',
        '--json >"$BOARD_DOCTOR_DIR/5333.json"',
        'BOARD_DOCTOR_SCHEMA = "code_mower.boardDoctor.v1"',
        'if report.get("schema") != BOARD_DOCTOR_SCHEMA:',
        'if report.get("repo") != expected_repo:',
        # Doctor rows are validated before they are indexed, so a repeated
        # check id cannot replace a failing row with a passing one.
        'if check_id in indexed:',
        "missing = sorted(EXPECTED_CHECK_IDS - set(indexed))",
        'failing = sorted(name for name in REQUIRED_PASS_CHECK_IDS if checks[name] != "pass")',
        # Only a queued owner surface may warn, and the top-level verdict must
        # be exactly that check's status.
        "owner_queue = checks[OWNER_QUEUE_CHECK_ID]",
        "if owner_queue not in OWNER_QUEUE_STATUSES:",
        'if report.get("status") != owner_queue:',
        'raise SystemExit(f"restarted Board doctors are not release-ready: {problems}")',
        # The required campaign is a parsed gate, not printed output.
        '>"$CAMPAIGN_DIR/watch.json"',
        '>"$CAMPAIGN_DIR/status.json"',
        'WATCH_SCHEMA = "code_mower.releaseCampaignWatch.v1"',
        'if watch.get("schema") != WATCH_SCHEMA or watch.get("mode") != "release-campaign-watch":',
        'if watch.get("status") != "complete" or watch.get("stop_reason") != "complete":',
        'problems.append(f"watch {key} is {watch.get(key)!r}, expected {expected!r}")',
        # Provider rows are validated before they are indexed, so a duplicate
        # row cannot hide a failing lane behind a later passing lane.
        'if name in indexed:',
        "missing = sorted(REQUIRED_PROVIDERS - set(indexed))",
        'watch_lanes, watch_row_problems = exact_provider_rows(watch.get("providers"), "watch")',
        'if watch_lanes is not None and set(watch_lanes) != REQUIRED_PROVIDERS:',
        'CAMPAIGN_SCHEMA = "code_mower.releaseCampaign.v1"',
        'if status.get("schema") != CAMPAIGN_SCHEMA:',
        'if status.get("status") != "complete":',
        'if status.get("dry_run") is not False:',
        'lanes, lane_row_problems = exact_provider_rows(status.get("providers"), "campaign")',
        'if set(lanes) != REQUIRED_PROVIDERS:',
        'if required != REQUIRED_PROVIDERS:',
        'if lane.get("state") != "complete":',
        'ADOPTION_RESULT_SCHEMA = "code_mower.adoptionResult.v1"',
        'if result.get("schema") != ADOPTION_RESULT_SCHEMA:',
        'problems.append(f"{name} lane result is not bound to {RELEASE_TAG}")',
        # Each adoption result belongs to its own lane and cold-install context.
        'if result.get("provider") != name:',
        'if result.get("qualification_context") != "cold_install":',
        'if outcome not in PASSING_OUTCOMES:',
        'raise SystemExit(f"release qualification campaign is not a pass: {problems}")',
        # Account-specific cloud identifiers stay private: they are supplied as
        # variables, required to be nonempty, and never printed.
        'test -n "$CODE_MOWER_CLOUD_TEAM_ID"',
        'test -n "$CODE_MOWER_INSTALL_ID"',
        # A forgotten placeholder, and an identity the selected install profile
        # does not hold, both fail before the service is probed.
        ': "${CODE_MOWER_CLOUD_TEAM_ID:?private cloud team id is required}"',
        ': "${CODE_MOWER_INSTALL_ID:?private cloud install id is required}"',
        'case "$CODE_MOWER_CLOUD_TEAM_ID" in REPLACE_WITH_*) exit 1 ;; esac',
        'case "$CODE_MOWER_INSTALL_ID" in REPLACE_WITH_*) exit 1 ;; esac',
        'resolution = resolve_cloud_token(token_env=DEFAULT_TOKEN_ENV, install_id=install_id)',
        # The ambient cloud token and endpoint are excluded, so the gate and
        # every later command resolve the selected stored install profile
        # instead of reflecting the values being asserted.
        "env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT",
        'if resolution.source != "install_id":',
        'problems.append("the cloud token was not resolved from the selected install profile")',
        'grep -q \'"source": "install_id"\' "$CLOUD_DIR/identity.json"',
        'problems.append("the selected install profile stores a different install identity")',
        'problems.append("the selected install profile stores a different team identity")',
        'raise SystemExit(f"cloud identity is not bound to the selected profile: {problems}")',
        'grep -q \'"cloud_identity": "bound"\' "$CLOUD_DIR/identity.json"',
        '--install-id "$CODE_MOWER_INSTALL_ID"',
        '--team-id "$CODE_MOWER_CLOUD_TEAM_ID"',
        # The cloud service itself is probed and parsed before either upload,
        # against a newly created empty bundle directory so the expected check
        # inventory is deterministic.
        'CLOUD_DOCTOR_BUNDLE_DIR="$(mktemp -d',
        'code-mower cloud doctor "$CLOUD_DOCTOR_BUNDLE_DIR"',
        '--probe-service --json >"$CLOUD_DIR/doctor.json"',
        'PASSING_CLOUD_CHECKS = ("endpoint", "service", "token")',
        'EXPECTED_CLOUD_CHECKS = frozenset(PASSING_CLOUD_CHECKS) | {"bundle"}',
        'if report.get("mode") != "cloud-doctor":',
        'if report.get("failures") != 0:',
        'raise SystemExit("cloud doctor check list is not a list")',
        'if name in statuses:',
        'problems.append(f"cloud doctor {name} check is {statuses.get(name)!r}")',
        # Health comes from the raw rows, so falsified aggregate fields cannot
        # hide a degraded, missing, extra, or duplicated check.
        'problems.append(f"cloud doctor reported unexpected checks {unexpected}")',
        'problems.append(f"cloud doctor is missing checks {missing}")',
        'if statuses.get("bundle") != "warn":',
        'if raw_failures or raw_failures != report.get("failures"):',
        'raise SystemExit(f"cloud service readiness is not a pass: {problems}")',
        # Both metadata-only uploads are previewed, accepted, applied, and
        # correlated. The preview verdict is a required file, so the applied
        # mutation cannot run on an unvalidated payload.
        '--team-id "$CODE_MOWER_CLOUD_TEAM_ID" --yes --json',
        '>"$CLOUD_DIR/campaign-preflight.json"',
        'raise SystemExit(f"campaign upload preview is not an acceptable payload: {problems}")',
        '"campaign_preview": "accepted",',
        'grep -q \'"campaign_preview": "accepted"\' "$CLOUD_DIR/campaign-preflight.json"',
        '>"$CLOUD_DIR/board-preflight.json"',
        'raise SystemExit(f"board snapshot preview is not an acceptable payload: {problems}")',
        '"board_preview": "accepted",',
        'grep -q \'"board_preview": "accepted"\' "$CLOUD_DIR/board-preflight.json"',
        # Every preview and applied producer must name the endpoint the probe
        # actually reached, so a re-resolved install profile cannot be accepted.
        'probed_endpoint = str(load("doctor.json").get("endpoint") or "")',
        'problems.append("the probed cloud endpoint was not recorded")',
        'problems.append("campaign upload preview does not target the probed service")',
        'problems.append("campaign applied upload does not target the probed service")',
        'problems.append("board snapshot preview does not target the probed service")',
        'problems.append("board upload preview does not target the probed service")',
        'problems.append("board applied upload does not target the probed service")',
        'CAMPAIGN_UPLOAD_SCHEMA = "code_mower.releaseCampaignUpload.v1"',
        'if payload.get("schema") != CAMPAIGN_UPLOAD_SCHEMA:',
        'if payload.get("mode") != "release-campaign-upload":',
        'problems.append(f"{name} campaign identity is not the {release_tag} campaign")',
        'if payload.get("provider_postures") != EXPECTED_POSTURES:',
        'if payload.get("counts") != EXPECTED_COUNTS:',
        "if len(ids) != 2 or len(set(ids)) != 2 or not all(ids):",
        'if preview_upload.get("event_types") != {"adoption_run": 2}:',
        'if preview.get("status") != "dry_run" or preview.get("would_upload") is not False:',
        'if preview.get("requires_yes") is not True:',
        'if preview_upload.get("report_count") != 0:',
        'if applied.get("requires_yes") is not False:',
        'if not 200 <= int(applied_upload.get("status") or 0) < 300:',
        'if [str(value) for value in applied.get("event_ids") or []] != preview_events:',
        'if applied.get("counts") != preview.get("counts"):',
        'raise SystemExit(f"campaign metadata upload is not a verified gate: {problems}")',
        '--install-id "$CODE_MOWER_INSTALL_ID" --yes --json',
        'BUNDLE_SCHEMA = "code_mower.cloudBenchmarkBundle.v1"',
        'EXPECTED_REPO_SLUG = "codemower-ai/code-mower"',
        'if snapshot.get("mode") != "cloud-board-snapshot" or snapshot.get("status") != "dry_run":',
        'if snapshot.get("repo_slug") != EXPECTED_REPO_SLUG:',
        'if snapshot.get("event_count") != 1:',
        'if export.get("event_types") != EXPECTED_EVENT_TYPES or export.get("included_reports"):',
        'if manifest.get("schema") != BUNDLE_SCHEMA:',
        # The bundle and its single event name the release repository too, so a
        # truthful summary cannot cover evidence from another repository.
        'if manifest.get("repo_slug") != EXPECTED_REPO_SLUG:',
        'if event.get("repo_slug") != EXPECTED_REPO_SLUG:',
        'if event_types != ["board_snapshot"] or len(events) != 1:',
        # Malformed rows are reported, never filtered, so one valid event plus
        # anything else cannot look like a single-event bundle.
        'problems.append("board bundle does not carry exactly one structured event")',
        'if manifest.get("included_reports"):',
        'if event.get("schema") != EVENT_SCHEMA or not str(event.get("event_id") or ""):',
        'if dimensions.get("snapshot_schema") != SNAPSHOT_SCHEMA:',
        'if preview.get("event_count") != len(events) or preview.get("event_count") != 1:',
        # Generic `cloud upload --dry-run` emits no event-type map, so exact
        # event types come from the digest-bound manifest instead.
        'if event_type_counts != EXPECTED_EVENT_TYPES:',
        'if not 200 <= int(applied.get("status") or 0) < 300:',
        # The Board snapshot reads a checkout, and its event carries no commit
        # or dirty-state field, so the checkout is re-bound immediately before.
        'test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"',
        # The Board snapshot's own nested cloud doctor must be healthy on its
        # raw rows; only the producer's skipped service probe is tolerated.
        'snapshot_doctor = snapshot.get("doctor")',
        'if snapshot_doctor.get("mode") != "cloud-doctor":',
        'if snapshot_doctor.get("status") != "pass":',
        'SNAPSHOT_DOCTOR_PASSING = ("endpoint", "token", "bundle", "model-provenance")',
        'SNAPSHOT_DOCTOR_CHECKS = frozenset(SNAPSHOT_DOCTOR_PASSING) | {"service"}',
        'problems.append("board snapshot doctor does not target the probed service")',
        'problems.append(f"board snapshot doctor check {name!r} appears more than once")',
        'problems.append(f"board snapshot doctor reported unexpected checks {unexpected_doctor}")',
        'problems.append(f"board snapshot doctor is missing checks {missing_doctor}")',
        'f"board snapshot doctor {name} check is {doctor_statuses.get(name)!r}"',
        'if doctor_statuses.get("service") != "skip":',
        'if doctor_failures or doctor_failures != snapshot_doctor.get("failures"):',
        # Generic cloud upload returns no event identifiers, so the applied
        # upload is bound to the previewed bundle by digest, and the bundle is
        # hashed on both sides of the preview command too.
        '>"$CLOUD_DIR/board-bundle-before-preview.sha256"',
        '>"$CLOUD_DIR/board-bundle-after-preview.sha256"',
        'before_preview = digest_of(cloud_dir / "board-bundle-before-preview.sha256")',
        'after_preview = digest_of(cloud_dir / "board-bundle-after-preview.sha256")',
        'previewed_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()',
        'if before_preview != after_preview or previewed_digest != before_preview:',
        'problems.append("board bundle changed while the preview was generated")',
        '"previewed_digest": previewed_digest,',
        '>"$CLOUD_DIR/board-bundle-before-apply.sha256"',
        '>"$CLOUD_DIR/board-bundle-after-apply.sha256"',
        'before_digest = digest_of(cloud_dir / "board-bundle-before-apply.sha256")',
        'after_digest = digest_of(cloud_dir / "board-bundle-after-apply.sha256")',
        'current_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()',
        'problems.append("the previewed board bundle identity was not retained")',
        "or current_digest != previewed_digest",
        'problems.append("board bundle changed between the preview and the applied upload")',
        # External hashes cannot see an A-B-A substitution inside a producer, so
        # the snapshot and both upload phases report the exact manifest bytes
        # and events they acted on, and those reports must be identical.
        'UPLOAD_IDENTITY_SCHEMA = "code_mower.cloudUploadIdentity.v1"',
        '"manifest_sha256": previewed_digest,',
        '"event_ids": [str(event.get("event_id") or "")],',
        'problems.append("board snapshot does not report the inspected manifest identity")',
        'problems.append("board upload preview does not report the inspected manifest identity")',
        'problems.append("board applied upload does not report the previewed manifest identity")',
        '"previewed_identity": expected_identity,',
        # The snapshot producer is required to collect from the exact clean
        # release checkout and to report the commit it read.
        '--require-head-sha "$RELEASE_SHA" --require-clean',
        'problems.append("board snapshot was not collected from the exact clean release checkout")',
        'problems.append("board event does not carry the release checkout provenance")',
        'raise SystemExit(f"board snapshot upload is not a verified gate: {problems}")',
        # Every Board repository path is the checkout of its paired slug, so a
        # path cannot serve another repository's history under the wrong name.
        'raise SystemExit("a Board repository path does not match its paired slug")',
        'assert_board_repo_paths.py" \\',
    )
    return tuple(
        marker.replace("{release_tag}", release_tag).replace(
            "{release_notes}", _release_notes_path(release_tag)
        )
        for marker in assertions
    )


def _post_merge_runbook_gate_orders() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Ordered command sequences the post-merge runbook must publish in order.

    Presence alone cannot show that a network mutation is gated: a validator
    that runs after its own `--yes` command has already uploaded whatever the
    producer built. Each sequence below fails if a preflight validator is
    deleted or moved behind the mutation it guards.
    """

    return (
        (
            "testpypi-sdist-build-backend-before-download",
            (
                '--no-cache-dir --index-url https://pypi.org/simple/ "setuptools>=77"',
                "--no-build-isolation --check-build-dependencies",
                '--package-spec "$TESTPYPI_WHEEL"',
            ),
        ),
        (
            "campaign-preflight-before-apply",
            (
                'grep -q \'"cloud_identity": "bound"\' "$CLOUD_DIR/identity.json"',
                '>"$CLOUD_DIR/campaign-preview.json"',
                'raise SystemExit(f"campaign upload preview is not an acceptable payload: {problems}")',
                'grep -q \'"campaign_preview": "accepted"\' "$CLOUD_DIR/campaign-preflight.json"',
                '--team-id "$CODE_MOWER_CLOUD_TEAM_ID" --yes --json',
                'raise SystemExit(f"campaign metadata upload is not a verified gate: {problems}")',
            ),
        ),
        (
            "board-preflight-before-apply",
            (
                'test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"',
                "code-mower cloud board-snapshot",
                '>"$CLOUD_DIR/board-bundle-before-preview.sha256"',
                '--dry-run --json \\\n  >"$CLOUD_DIR/board-preview.json"',
                '>"$CLOUD_DIR/board-bundle-after-preview.sha256"',
                'raise SystemExit(f"board snapshot preview is not an acceptable payload: {problems}")',
                'grep -q \'"board_preview": "accepted"\' "$CLOUD_DIR/board-preflight.json"',
                '>"$CLOUD_DIR/board-bundle-before-apply.sha256"',
                '--install-id "$CODE_MOWER_INSTALL_ID" --yes --json',
                '>"$CLOUD_DIR/board-bundle-after-apply.sha256"',
                'raise SystemExit(f"board snapshot upload is not a verified gate: {problems}")',
            ),
        ),
    )


BOARD_SNAPSHOT_COMMAND = "code-mower cloud board-snapshot"
BOARD_SNAPSHOT_BINDING_ASSERTIONS = (
    'test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"',
    'test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"',
)
# The snapshot command is invoked with the ambient cloud token and endpoint
# excluded; that prefix is part of the same command, not an intervening step.
CLOUD_ENV_ISOLATION_PREFIX = (
    "env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \\"
)


def _board_snapshot_binding_problems(runbook_doc: str) -> list[str]:
    """Require the checkout re-binding immediately before the Board snapshot.

    The snapshot event carries no commit or dirty-state field, so an assertion
    made earlier in the runbook cannot speak for a checkout that moved since.
    Both assertions must be the last commands before the snapshot runs.
    """

    start = runbook_doc.find(BOARD_SNAPSHOT_COMMAND)
    if start < 0:
        return [f"{BOARD_SNAPSHOT_COMMAND} is missing"]
    preceding = [
        line.strip()
        for line in runbook_doc[:start].splitlines()
        if line.strip()
        and not line.strip().startswith("#")
        and line.strip() != CLOUD_ENV_ISOLATION_PREFIX
    ]
    if preceding[-2:] != list(BOARD_SNAPSHOT_BINDING_ASSERTIONS):
        return [
            "the release checkout is not re-bound immediately before "
            f"{BOARD_SNAPSHOT_COMMAND}"
        ]
    return []


def _release_create_binding_problems(runbook_doc: str, release_tag: str | None = None) -> list[str]:
    """Require the exact clean checkout immediately before the Release branch.

    Accepting an existing release or creating one publishes notes read from the
    checkout, so a checkout that moved or became dirty after the earlier
    assertions must stop the runbook before either branch runs.
    """

    release_tag = release_tag or _release_tag_for_version(__version__)
    release_create_branch = f'if gh release view {release_tag} --repo "$REPO" >/dev/null 2>&1; then'
    binding_assertions = (
        'test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"',
        'test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"',
        f'test -s "$RELEASE_CHECKOUT/{_release_notes_path(release_tag)}"',
        'test -s "$PYPI_VERIFIED_MAP"',
    )

    start = runbook_doc.find(release_create_branch)
    if start < 0:
        return ["the GitHub Release creation branch is missing"]
    preceding = [
        line.strip()
        for line in runbook_doc[:start].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if preceding[-4:] != list(binding_assertions):
        return [
            "the exact clean release checkout is not re-bound immediately "
            "before the GitHub Release branch"
        ]
    return []


def _post_merge_gate_order_problems(runbook_doc: str) -> list[str]:
    problems: list[str] = []
    for name, sequence in _post_merge_runbook_gate_orders():
        for marker in _unordered_markers(runbook_doc, sequence):
            problems.append(f"{name}: {marker}")
    return problems


def _forbidden_runbook_markers() -> tuple[str, ...]:
    """Commands the post-merge runbook must not publish."""

    return (
        'RELEASE_SHA="$(git rev-parse origin/main)"',
        "gh release upload",
        "--pip-extra-index-url https://pypi.org/simple/",
        # Account-specific cloud identifiers belong in private variables.
        "jeff-internal",
        "--install-id codex-code-mower",
        'echo "$CODE_MOWER_CLOUD_TEAM_ID"',
        'echo "$CODE_MOWER_INSTALL_ID"',
    )


def _forbidden_release_document_markers() -> tuple[str, ...]:
    """Commands the release document must not publish anywhere.

    pip gives its primary index no priority over an extra index, so a TestPyPI
    candidate rehearsal that also names production PyPI cannot show which index
    supplied the package.
    """

    return (
        "--pip-extra-index-url https://pypi.org/simple/",
        "--refresh-package code-mower",
    )


UV_ISOLATION_SITE_COUNT = 2
UV_ISOLATION_ENVIRONMENT = (
    "-u UV_INDEX",
    "-u UV_DEFAULT_INDEX",
    "-u UV_INDEX_URL",
    "-u UV_EXTRA_INDEX_URL",
    "-u UV_FIND_LINKS",
    "-u UV_NO_INDEX",
    "-u UV_OFFLINE",
)
UV_ISOLATION_ARGUMENTS = (
    "uv --no-config --no-cache tool install",
    "--default-index https://pypi.org/simple/",
)


def _uv_isolation_problems(release_doc: str) -> list[str]:
    """Report documented `uv tool install` commands that could resolve elsewhere.

    An ambient `UV_INDEX`, find-links value, offline flag, or project
    configuration redirects uv the same way a `pip.conf` redirects pip, and only
    `--no-cache` bypasses the cache.
    """

    sites = [
        command for command in _shell_commands(release_doc) if "tool install" in command
    ]
    problems: list[str] = []
    if len(sites) != UV_ISOLATION_SITE_COUNT:
        problems.append(
            f"expected {UV_ISOLATION_SITE_COUNT} uv tool installs, found {len(sites)}"
        )
    for command in sites:
        label = command.split("uv ", 1)[-1].strip()[:72]
        problems.extend(
            f"{label} does not clear {fragment}"
            for fragment in UV_ISOLATION_ENVIRONMENT
            if fragment not in command
        )
        problems.extend(
            f"{label} does not use {fragment}"
            for fragment in UV_ISOLATION_ARGUMENTS
            if fragment not in command
        )
    return problems


PIP_ISOLATION_SITE_COUNT = 9
PIP_ISOLATION_ENVIRONMENT = (
    "-u PIP_INDEX_URL",
    "-u PIP_EXTRA_INDEX_URL",
    "-u PIP_FIND_LINKS",
    "-u PIP_NO_INDEX",
    "PIP_CONFIG_FILE=/dev/null",
)


def _shell_commands(text: str) -> list[str]:
    """Return one string per shell command in fenced bash blocks.

    Continuation lines are joined so a command's options can be inspected
    together with the environment its outer `env` invocation established.
    """

    commands: list[str] = []
    pending: list[str] = []
    in_shell_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_shell_block = stripped == "```bash"
            pending = []
            continue
        if not in_shell_block:
            continue
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        pending.append(stripped)
        commands.append(" ".join(part for part in pending if part))
        pending = []
    return commands


def _pip_command_kind(command: str) -> str:
    """Classify a runbook command by which package-source contract it must meet."""

    if " -m pip " in command and (" install" in command or " download" in command):
        return "pip"
    if "package-install-rehearsal" in command:
        return "rehearsal"
    if "pipx install" in command:
        return "pipx"
    return ""


TESTPYPI_INDEX_ARGUMENT = "--index-url https://test.pypi.org/simple/"
SDIST_BUILD_BOUNDARY_ARGUMENTS = ("--no-build-isolation", "--check-build-dependencies")


def _testpypi_download_boundary_problems(command: str) -> list[str]:
    """Report a TestPyPI download that could resolve a build backend from TestPyPI.

    A wheel-only download never prepares PEP 517 metadata. An sdist download
    does, even with `--no-deps`, and would fetch the declared build backend
    from the only configured index -- which TestPyPI does not carry -- unless
    the backend is already installed and pip is told to use it and to fail
    when it is missing.
    """

    if " download" not in command or TESTPYPI_INDEX_ARGUMENT not in command:
        return []
    if "--no-deps" not in command:
        return ["resolves dependencies from TestPyPI"]
    if "--no-binary :all:" in command:
        return [
            f"downloads the TestPyPI sdist without {argument}"
            for argument in SDIST_BUILD_BOUNDARY_ARGUMENTS
            if argument not in command
        ]
    if "--only-binary :all:" not in command:
        return ["does not download the TestPyPI candidate wheel-only"]
    return []


def _post_merge_pip_isolation_problems(runbook_doc: str) -> list[str]:
    """Report post-merge pip-backed commands that could resolve another source.

    An explicit index proves nothing while an ambient `PIP_INDEX_URL`,
    find-links directory, offline flag, or `pip.conf` is still readable, so
    every site is required to establish the same isolated environment.
    """

    sites = [
        (kind, command)
        for command in _shell_commands(runbook_doc)
        if (kind := _pip_command_kind(command))
    ]
    problems: list[str] = []
    if len(sites) != PIP_ISOLATION_SITE_COUNT:
        problems.append(
            f"expected {PIP_ISOLATION_SITE_COUNT} pip-backed commands,"
            f" found {len(sites)}"
        )
    for kind, command in sites:
        label = command.split("PIP_CONFIG_FILE=/dev/null", 1)[-1].strip()[:72]
        problems.extend(
            f"{label} does not set {fragment}"
            for fragment in PIP_ISOLATION_ENVIRONMENT
            if fragment not in command
        )
        if kind == "pip":
            if "-m pip --isolated" not in command:
                problems.append(f"{label} does not run an isolated pip")
            if "--no-cache-dir" not in command:
                problems.append(f"{label} does not bypass the pip cache")
            problems.extend(
                f"{label} {problem}"
                for problem in _testpypi_download_boundary_problems(command)
            )
        if kind == "rehearsal":
            if "--pip-index-url https://pypi.org/simple/" not in command:
                problems.append(f"{label} does not name the canonical index")
            if "--pip-no-cache" not in command:
                problems.append(f"{label} does not bypass the pip cache")
        if kind == "pipx":
            if "--backend pip" not in command:
                problems.append(f"{label} does not use the pip backend")
            if "--pip-args='--isolated --no-cache-dir'" not in command:
                problems.append(f"{label} does not isolate its pip arguments")
        if kind != "rehearsal" and "--index-url http" not in command:
            problems.append(f"{label} does not name an explicit index")
    return problems


# Variables the operator or the shell supplies, which the ordered runbook is
# not expected to assign. The private cloud identifiers are deliberately never
# written into the document; every other name must be established by an earlier
# runbook command before it is dereferenced.
RUNBOOK_EXTERNAL_VARIABLES = frozenset(
    {
        "CODE_MOWER_CLOUD_TEAM_ID",
        "CODE_MOWER_INSTALL_ID",
        "CODE_MOWER_CLOUD_TOKEN",
        "CODE_MOWER_CLOUD_ENDPOINT",
        "HOME",
        "PATH",
        "PWD",
        "TMPDIR",
        "VIRTUAL_ENV",
    }
)
_VARIABLE_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
_VARIABLE_USE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_LOOP_VARIABLE = re.compile(r"^for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s")


def _post_merge_variable_flow_problems(runbook_doc: str) -> list[str]:
    """Report ordered runbook variables dereferenced before they are assigned.

    Every ordered block runs under ``set -u``, so a name that no earlier block
    assigned either aborts the step or -- worse, when an unrelated ambient value
    happens to exist -- silently binds the release to something the runbook
    never established.
    """

    problems: list[str] = []
    assigned: set[str] = set(RUNBOOK_EXTERNAL_VARIABLES)
    reported: set[str] = set()
    for command in _shell_commands(runbook_doc):
        loop = _LOOP_VARIABLE.match(command.strip())
        if loop:
            assigned.add(loop.group(1))
        for name in _VARIABLE_USE.findall(command):
            if name in assigned or name in reported:
                continue
            reported.add(name)
            problems.append(
                f"${name} is used before the ordered runbook assigns it: "
                f"{command[:72]}"
            )
        for line in command.split(";"):
            match = _VARIABLE_ASSIGNMENT.match(line.strip())
            if match:
                assigned.add(match.group(1))
        for marker in (": \"${", "read -r "):
            if marker in command:
                for name in _VARIABLE_USE.findall(command):
                    assigned.add(name)
    return problems


FAIL_FAST_CONTRACT = "set -euo pipefail"


def _post_merge_fail_fast_problems(runbook_doc: str) -> list[str]:
    """Report ordered runbook Bash blocks that keep running after a failure.

    A block without the fail-fast contract reports only its last command's exit
    status, so a failed assertion in the middle of a block can be masked by a
    later command that happens to succeed.
    """

    problems: list[str] = []
    in_block = False
    is_bash = False
    block: list[str] = []
    index = 0
    for line in runbook_doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_block:
                in_block = True
                is_bash = stripped == "```bash"
                block = []
                continue
            in_block = False
            if is_bash:
                index += 1
                if block and block[0] != FAIL_FAST_CONTRACT:
                    problems.append(f"bash block {index} does not {FAIL_FAST_CONTRACT}")
            continue
        if in_block and is_bash and stripped:
            block.append(stripped)
    if not index:
        problems.append("the post-merge runbook publishes no bash block")
    return problems


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


def _release_notes_path(release_tag: str) -> str:
    return "docs/" + release_tag.replace(".", "") + "-release-notes.md"


def _release_tag_for_version(version: str) -> str:
    return code_mower_versioning.release_tag_for_version(version)


def _release_docs(repo_path: Path) -> dict[str, str]:
    paths = list(RELEASE_DOC_PATHS)
    try:
        metadata = release_metadata_module.load_release_metadata(repo_path)
    except release_metadata_module.ReleaseMetadataError:
        metadata = None
    if metadata is not None:
        paths.extend(metadata.documents.values())
    return {
        relative_path: _read_text_if_exists(repo_path / relative_path)
        for relative_path in dict.fromkeys(paths)
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


def _workflow_dispatch_inputs(workflow: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(workflow) if workflow.strip() else {}
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    # ``on`` is parsed as the boolean ``True`` by YAML 1.1 loaders.
    triggers = parsed.get("on", parsed.get(True))
    if not isinstance(triggers, dict):
        return {}
    dispatch = triggers.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return {}
    inputs = dispatch.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _dispatch_sha_gate_holds(
    workflow: str, workflow_jobs: dict[str, Any],
) -> bool:
    """Require every dispatched build and publish to name the exact commit.

    A dispatch that only names a ref can build whatever that ref points at when
    the job starts, so the expected commit is a required input and the first
    job refuses to let anything else run.
    """

    expected = _workflow_dispatch_inputs(workflow).get("expected_sha")
    if not isinstance(expected, dict):
        return False
    if expected.get("required") is not True or expected.get("type") != "string":
        return False
    if not _public_identity_gate_holds(workflow_jobs):
        return False
    guards = [
        step for step in workflow_jobs["release-identity"].get("steps", [])
        if isinstance(step, dict)
        and step.get("if") == "${{ github.event_name == 'workflow_dispatch' }}"
        and step.get("env") == {
            "EXPECTED_SHA": "${{ inputs.expected_sha }}",
            "ACTUAL_REF": "${{ github.ref }}",
        }
    ]
    if len(guards) != 1 or guards[0].get("continue-on-error"):
        return False
    return all(fragment in guards[0].get("run", "") for fragment in (
        "set -euo pipefail",
        "grep -Eq '^[0-9a-f]{40}$'",
        '[[ "$ACTUAL_REF" == refs/tags/v* ]] || exit 1',
    ))


RELEASE_WORKFLOW_DISPATCH_COMMAND = "gh workflow run release.yml"


def _public_identity_gate_holds(workflow_jobs: dict[str, Any]) -> bool:
    identity = workflow_jobs.get("release-identity", {})
    if not isinstance(identity, dict) or identity.get("if") or identity.get("continue-on-error"):
        return False
    steps = identity.get("steps", [])
    validators = [
        step for step in steps if isinstance(step, dict)
        and 'python src/code_mower/release_identity.py --tag "$RELEASE_TAG"' in step.get("run", "")
    ]
    if len(validators) != 1:
        return False
    validator = validators[0]
    if validator.get("if") or validator.get("continue-on-error"):
        return False
    if validator.get("id") != "identity" or identity.get("outputs") != {
        "resolved-sha": "${{ steps.identity.outputs.resolved-sha }}",
    }:
        return False
    if validator.get("env") != {
        "RELEASE_TAG": "${{ github.event.release.tag_name || github.ref_name }}",
        "ACTUAL_REF": "${{ github.ref }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "EXPECTED_SHA": "${{ inputs.expected_sha }}",
    }:
        return False
    if any(fragment not in validator.get("run", "") for fragment in (
        "set -euo pipefail",
        'test "$ACTUAL_REF" = "refs/tags/$RELEASE_TAG"',
        'RESOLVED_SHA="$(git rev-parse --verify "refs/tags/$RELEASE_TAG^{commit}")"',
        "printf '%s\\n' \"$RESOLVED_SHA\" | grep -Eq '^[0-9a-f]{40}$'",
        'test "$(git rev-parse HEAD)" = "$RESOLVED_SHA"',
        'if [ "$EVENT_NAME" = workflow_dispatch ]; then\n'
        '  test "$RESOLVED_SHA" = "$EXPECTED_SHA"\nfi',
        'python src/code_mower/release_identity.py --tag "$RELEASE_TAG"\n'
        'printf \'resolved-sha=%s\\n\' "$RESOLVED_SHA" >> "$GITHUB_OUTPUT"',
    )):
        return False
    for job_name, ref in (
        ("release-identity", "refs/tags/${{ github.event.release.tag_name || github.ref_name }}"),
        ("build-distributions", "${{ needs.release-identity.outputs.resolved-sha }}"),
    ):
        job = workflow_jobs.get(job_name, {})
        if not isinstance(job, dict):
            return False
        checkouts = [step for step in job.get("steps", []) if isinstance(step, dict)
                     and str(step.get("uses", "")).startswith("actions/checkout@")]
        if len(checkouts) != 1:
            return False
        checkout = checkouts[0]
        if checkout.get("if") or checkout.get("continue-on-error"):
            return False
        if checkout.get("with", {}).get("ref") != ref:
            return False
        if job_name == "release-identity" and checkout["with"].get("fetch-depth") != 0:
            return False
    return all(
        _needs_job(workflow_jobs.get(job_name), "release-identity")
        for job_name in ("build-distributions", "publish-testpypi", "publish-pypi")
    )


def _incomplete_dispatch_actions(
    workflow: str, next_actions: list[dict[str, Any]]
) -> list[str]:
    """Report advertised workflow dispatches missing a required input.

    A readiness report that passes while every advertised dispatch is rejected
    at submission is worse than no next action at all, so each generated
    command must supply every input the workflow marks required.
    """

    required_inputs = _required_dispatch_inputs(workflow)
    problems: list[str] = []
    for action in next_actions:
        command = str(action.get("command") or "")
        if RELEASE_WORKFLOW_DISPATCH_COMMAND not in command:
            continue
        problems.extend(
            f"{action.get('id')} omits -f {name}="
            for name in required_inputs
            if f"-f {name}=" not in command
        )
    return problems


def _required_dispatch_inputs(workflow: str) -> list[str]:
    return sorted(
        name
        for name, spec in _workflow_dispatch_inputs(workflow).items()
        if isinstance(spec, dict) and spec.get("required") is True
    )


def _documented_dispatch_commands(doc: str) -> list[str]:
    """Return every documented dispatch command, joining continuation lines."""

    commands: list[str] = []
    lines = doc.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if RELEASE_WORKFLOW_DISPATCH_COMMAND not in line:
            index += 1
            continue
        command = line.strip()
        while command.endswith("\\") and index + 1 < len(lines):
            index += 1
            command = f"{command[:-1].strip()} {lines[index].strip()}"
        commands.append(command)
        index += 1
    return commands


def _incomplete_documented_dispatches(
    workflow: str, docs: dict[str, str]
) -> list[str]:
    """Report documented workflow dispatches missing a required input.

    Public release and install guidance is followed verbatim, so a documented
    command that the workflow rejects at submission blocks the release exactly
    like a broken generated next action.
    """

    required_inputs = _required_dispatch_inputs(workflow)
    problems: list[str] = []
    for relative_path, doc in sorted(docs.items()):
        for position, command in enumerate(_documented_dispatch_commands(doc), start=1):
            problems.extend(
                f"{relative_path} dispatch {position} omits -f {name}="
                for name in required_inputs
                if f"-f {name}=" not in command
            )
    return problems


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


def _candidate_runbook_checks(repo_path: Path) -> tuple[list[str], list[str]]:
    """The current sequence qualifies merge-SHA artifacts before tagging.

    Versioned runbooks stay historical. These are static documentation checks,
    not private acceptance evidence.
    """
    try:
        metadata = release_metadata_module.load_release_metadata(repo_path)
    except release_metadata_module.ReleaseMetadataError as exc:
        return [f"valid {release_metadata_module.MANIFEST_PATH}: {exc}"], []
    text = _read_text_if_exists(repo_path / metadata.documents["runbook"])
    order = (
        "## 1. Review and merge", "## 2. Build and retain",
        "gh workflow run release-candidate.yml", "## 3. Qualify the exact candidate",
        "## 4. Observe the bounded hosted Board canary", "## 5. Owner decision",
        f'git tag -a {metadata.tag} "$RELEASE_SHA"',
        "-f publish_testpypi=false -f publish_pypi=false",
        "-f publish_testpypi=false -f publish_pypi=true",
        "## 6. Independent canonical reinstall",
    )
    assertions = (
        "--json state --jq '.state')\" = MERGED",
        "--json mergeCommit --jq '.mergeCommit.oid'",
        "--require-candidate", "candidate.json", "rehearsal.json",
        '-f candidate_run_id="$CANDIDATE_RUN_ID"',
        f'test "$(git rev-list -n 1 {metadata.tag})" = "$RELEASE_SHA"',
        "fresh install without uv or pipx", f"upgrade from v{metadata.previous_version}",
        "remote observer", "safe init", "Graphify", "basic Slack lifecycle",
        "single-lane", "multi-lane", "aggregate campaign ACU",
        "provider exit", "authorized usage", "settled usage",
        "metadata-only", "fresh dashboard", "does not rebuild",
        "Slack telemetry remains deferred to v1.6.0",
        "independent exact-head audit", "authoritative gate",
    )
    return _unordered_markers(text, order), [item for item in assertions if item not in text]


def render_release_readiness(repo_path: Path) -> dict[str, Any]:
    """Inspect whether the standalone package is ready for package-index promotion."""

    repo_path = repo_path.expanduser().resolve()
    workflow_path = repo_path / ".github" / "workflows" / "release.yml"
    workflow = _read_text_if_exists(workflow_path)
    candidate_workflow = _read_text_if_exists(repo_path / ".github/workflows/release-candidate.yml")
    candidate_workflow_used = (
        (repo_path / ".github/workflows/release-candidate.yml").exists()
        or "scripts/release_candidate.py" in workflow
    )
    ci_workflow_path = repo_path / ".github" / "workflows" / "ci.yml"
    ci_workflow = _read_text_if_exists(ci_workflow_path)
    workflow_jobs = _workflow_jobs(workflow)
    testpypi_job = workflow_jobs.get("publish-testpypi")
    pypi_job = workflow_jobs.get("publish-pypi")
    testpypi_job_text = _job_text(testpypi_job)
    pypi_job_text = _job_text(pypi_job)
    docs = _release_docs(repo_path)
    try:
        current_release = release_metadata_module.load_release_metadata(repo_path)
        release_manifest_error = ""
    except release_metadata_module.ReleaseMetadataError as exc:
        current_release = None
        release_manifest_error = str(exc)
    public_hygiene_docs = {
        relative_path: _read_text_if_exists(repo_path / relative_path)
        for relative_path in PUBLIC_HYGIENE_DOC_PATHS
    }
    docs_lifecycle_report = (
        docs_lifecycle.validate_manifest(repo_path)
        if (repo_path / docs_lifecycle.MANIFEST_PATH).is_file()
        else None
    )
    init_version = _python_package_version(repo_path)
    pyproject_version = _pyproject_version(repo_path)
    manifest_version = _committed_manifest_version(repo_path)
    manifest_drift = _committed_manifest_drift(repo_path)
    version = init_version or pyproject_version
    materialized_versions = _materialized_package_versions(repo_path)
    release_tag = _release_tag_for_version(version) if version else ""
    identity_problems = check_release_identity(repo_path, release_tag)
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
    release_doc = docs.get("docs/pypi-release.md", "")
    forbidden_runbook_markers = [
        marker for marker in _forbidden_runbook_markers() if marker in runbook_doc
    ] + [
        marker
        for marker in _forbidden_release_document_markers()
        if marker in release_doc
    ]
    uv_isolation_problems = _uv_isolation_problems(release_doc) if release_doc else []
    pip_isolation_problems = (
        _post_merge_pip_isolation_problems(runbook_doc) if runbook_doc else []
    )
    gate_order_problems = (
        _post_merge_gate_order_problems(runbook_doc)
        + _board_snapshot_binding_problems(runbook_doc)
        + _release_create_binding_problems(runbook_doc, release_tag)
        + _post_merge_fail_fast_problems(runbook_doc)
        + _post_merge_variable_flow_problems(runbook_doc)
        if runbook_doc
        else ["unknown release version"]
    )
    if candidate_workflow_used:
        missing_runbook_markers, missing_runbook_assertions = _candidate_runbook_checks(repo_path)
        current_runbook = (
            current_release.documents["runbook"]
            if current_release is not None
            else release_metadata_module.MANIFEST_PATH
        )
        runbook_markers = (
            f"{current_runbook}: candidate, private acceptance, canaries, tag, publish",
        )
        runbook_assertions = ("merge SHA and retained artifact binding; explicit owner gates",)
        # Legacy checks above describe the preserved v1.4 publication procedure.
        # The new procedure has its own ordered gates and artifact assertions.
        forbidden_runbook_markers = []
        pip_isolation_problems = []
        gate_order_problems = []
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
    # pip gives its primary index no priority, so a candidate rehearsal that
    # also names production PyPI cannot show which index supplied the package.
    unsafe_package_index_docs = [
        relative_path
        for relative_path in CURRENT_PACKAGE_INDEX_GUIDANCE_DOC_PATHS
        if any(
            marker in docs.get(relative_path, "")
            for marker in UNSAFE_MULTI_INDEX_MARKERS
        )
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

    release_manifest_problems = []
    if release_manifest_error:
        release_manifest_problems.append(release_manifest_error)
    elif current_release is not None:
        if current_release.version != version:
            release_manifest_problems.append(
                f"release.yml version {current_release.version} != package version {version}"
            )
        if current_release.tag != release_tag:
            release_manifest_problems.append(
                f"release.yml tag {current_release.tag} != package tag {release_tag}"
            )
        if current_release.package_spec != package_index_spec:
            release_manifest_problems.append(
                "release.yml package_spec does not match the package version"
            )

    documentation_checks = [
        _release_check(
            check_id="release-metadata",
            title="Current release metadata is valid and matches the package identity",
            status="pass" if not release_manifest_problems else "fail",
            evidence=release_metadata_module.MANIFEST_PATH,
            detail={"problems": release_manifest_problems},
        )
    ]
    if docs_lifecycle_report is not None:
        documentation_checks.append(
            _release_check(
                check_id="documentation-lifecycle",
                title="Documentation inventory, ownership, and immutable history are valid",
                status=docs_lifecycle_report["status"],
                evidence=docs_lifecycle.MANIFEST_PATH,
                detail=docs_lifecycle_report,
            )
        )

    checks = [
        *documentation_checks,
        _release_check(
            check_id="release-public-identity",
            title="Selected release identity and immutable public text agree",
            status="fail" if identity_problems else "pass",
            evidence=f"{release_tag}: pyproject.toml, src/code_mower/__init__.py, README.md, CHANGELOG.md",
            detail={"problems": identity_problems},
        ),
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
            title="Release workflows build once and verify distributions before publish",
            status=(
                "pass"
                if (
                    "  build-distributions:\n" in workflow
                    and "  verify-distributions:\n" in workflow
                    and "    needs: build-distributions\n" in workflow
                    and ("python -m build" in workflow if not candidate_workflow_used else (
                        "python scripts/release_candidate.py verify" in workflow
                        and "--require-candidate" in workflow
                        and "--name code-mower-candidate" in workflow
                        and "python -m build" not in workflow
                        and "python scripts/release_candidate.py build" in candidate_workflow
                        and '[[ "$GITHUB_SHA" == "$SOURCE_SHA" ]]' in candidate_workflow
                        and '[[ "$GITHUB_RUN_ATTEMPT" == 1 ]]' in candidate_workflow
                        and "assert run['head_sha'] == os.environ['SOURCE_SHA']" in workflow
                        and "assert run['run_attempt'] == 1" in workflow
                        and "verify_rehearsal(Path('candidate'), candidate)" in workflow
                    ))
                    and "python -m twine check dist/*" in workflow
                )
                else "fail"
            ),
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="release-dispatch-sha-gate",
            title="Dispatched release builds are bound to the expected commit",
            status=(
                "pass"
                if _dispatch_sha_gate_holds(workflow, workflow_jobs)
                else "fail"
            ),
            evidence=str(workflow_path),
        ),
        _release_check(
            check_id="release-public-identity-gate",
            title="Both release events validate immutable public text before building or publishing",
            status="pass" if _public_identity_gate_holds(workflow_jobs) else "fail",
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
            title="Package-index rehearsal is documented with source-exclusive TestPyPI",
            status=(
                "pass"
                if (
                    package_index_spec
                    and package_index_spec in doc_blob
                    and "--allow-package-index" in doc_blob
                    and "--package-source testpypi" in doc_blob
                    and "package-install-rehearsal" in doc_blob
                    and not unsafe_package_index_docs
                )
                else "fail"
            ),
            evidence=package_index_spec or "missing version",
            detail={
                "docs": package_index_docs,
                "unsafe_multi_index_docs": unsafe_package_index_docs,
            },
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
                if (
                    not missing_runbook_assertions
                    and not forbidden_runbook_markers
                    and not pip_isolation_problems
                    and not uv_isolation_problems
                    and not gate_order_problems
                )
                else "fail"
            ),
            evidence="docs/pypi-release.md",
            detail={
                "release_tag": release_tag,
                "required_assertions": list(runbook_assertions),
                "missing_assertions": missing_runbook_assertions,
                "forbidden_commands": forbidden_runbook_markers,
                "pip_isolation_problems": pip_isolation_problems[:20],
                "uv_isolation_problems": uv_isolation_problems[:20],
                "gate_order_problems": gate_order_problems[:20],
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
                if all(
                    _links_to_repository_doc(docs.get("README.md", ""), label, relative_path)
                    for label, relative_path in (
                        ("Support", "SUPPORT.md"),
                        ("Security Policy", "SECURITY.md"),
                        ("Code of Conduct", "CODE_OF_CONDUCT.md"),
                    )
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
    release_workflow_ref = release_tag or "main"
    next_actions = [
        {
            "id": "dry-run-release-workflow",
            "title": "Run the release workflow without publishing",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                f"--ref {release_workflow_ref} "
                "-f publish_testpypi=false -f publish_pypi=false "
                '-f expected_sha="$RELEASE_SHA"'
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "publish-testpypi-candidate",
            "title": "Publish the verified distribution to TestPyPI",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                f"--ref {release_workflow_ref} "
                "-f publish_testpypi=true -f publish_pypi=false "
                '-f expected_sha="$RELEASE_SHA"'
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["release_workflow"],
        },
        {
            "id": "testpypi-source-exclusive-qualification",
            "title": "Qualify the TestPyPI candidate from TestPyPI alone",
            "command": (
                "code-mower release qualify "
                f"--release-tag {release_workflow_ref} "
                f"--package-spec {package_index_spec} "
                "--output result.json "
                "--package-source testpypi "
                "--execute"
            ),
            "url": PACKAGE_INDEX_SETUP_URLS["testpypi_project"],
        },
        {
            "id": "publish-pypi-release",
            "title": "Publish the verified distribution to production PyPI",
            "command": (
                "gh workflow run release.yml --repo codemower-ai/code-mower "
                f"--ref {release_workflow_ref} "
                "-f publish_testpypi=false -f publish_pypi=true "
                '-f expected_sha="$RELEASE_SHA"'
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
    if candidate_workflow_used:
        # v1.5 promotes the immutable candidate directly to production. TestPyPI
        # remains an owner-selected diagnostic path, so retain its executable
        # commands for that case but mark them optional rather than presenting
        # them as release-critical next actions.
        for action in next_actions:
            if action["id"] in {
                "publish-testpypi-candidate",
                "testpypi-source-exclusive-qualification",
            }:
                action["optional"] = True
                action["title"] = "Optional: " + action["title"]
        for action in next_actions:
            if "gh workflow run release.yml" in action["command"]:
                action["command"] += ' -f candidate_run_id="$CANDIDATE_RUN_ID"'
            if action["id"] == "create-github-release":
                action["required_env"] = [
                    "CANDIDATE_DIR",
                    "CANDIDATE_RUN_ID",
                    "GITHUB_RELEASE_NOTES",
                ]
                action["command"] = (
                    'test -n "$CANDIDATE_DIR" && test -n "$CANDIDATE_RUN_ID" && '
                    'test -n "$GITHUB_RELEASE_NOTES" && '
                    'gh variable set CODE_MOWER_TESTPYPI_PUBLISH --repo codemower-ai/code-mower --body false && '
                    'gh variable set CODE_MOWER_PYPI_PUBLISH --repo codemower-ai/code-mower --body false && '
                    'gh variable set CODE_MOWER_CANDIDATE_RUN_ID --repo codemower-ai/code-mower '
                    '--body "$CANDIDATE_RUN_ID" && '
                    'test "$(gh variable get CODE_MOWER_TESTPYPI_PUBLISH --repo codemower-ai/code-mower '
                    '--json value --jq .value)" = false && '
                    'test "$(gh variable get CODE_MOWER_PYPI_PUBLISH --repo codemower-ai/code-mower '
                    '--json value --jq .value)" = false && '
                    'test "$(gh variable get CODE_MOWER_CANDIDATE_RUN_ID --repo codemower-ai/code-mower '
                    '--json value --jq .value)" = "$CANDIDATE_RUN_ID" && '
                    f'! gh release view {release_tag} --repo codemower-ai/code-mower '
                    '>/dev/null 2>&1 && '
                    f'gh release create {release_tag} '
                    f'"$CANDIDATE_DIR/code_mower-{version}-py3-none-any.whl" '
                    f'"$CANDIDATE_DIR/code_mower-{version}.tar.gz" '
                    '--repo codemower-ai/code-mower --verify-tag --latest '
                    f'--title "Code Mower {release_tag}" '
                    '--notes-file "$GITHUB_RELEASE_NOTES"'
                )
        next_actions.insert(0, {
            "id": "immutable-candidate-first",
            "title": "After merge: build once, then #918 and explicitly authorized #920 before tagging/publication",
            "command": 'gh workflow run release-candidate.yml --repo codemower-ai/code-mower --ref main '
                       '-f expected_sha="$RELEASE_SHA" -f release_pr="$RELEASE_PR"',
            "url": (
                "https://github.com/codemower-ai/code-mower/blob/main/"
                + (
                    current_release.documents["runbook"]
                    if current_release is not None
                    else release_metadata_module.MANIFEST_PATH
                )
            ),
        })
    incomplete_dispatch_actions = _incomplete_dispatch_actions(workflow, next_actions)
    incomplete_documented_dispatches = _incomplete_documented_dispatches(workflow, docs)
    checks.append(
        _release_check(
            check_id="release-workflow-next-actions-dispatchable",
            title="Advertised release workflow dispatches supply every required input",
            status="pass"
            if not incomplete_dispatch_actions and not incomplete_documented_dispatches
            else "fail",
            evidence=(
                "release-readiness next actions, "
                f"{', '.join(RELEASE_DOC_PATHS)}, .github/workflows/release.yml"
            ),
            detail={
                "incomplete_dispatch_actions": incomplete_dispatch_actions,
                "incomplete_documented_dispatches": incomplete_documented_dispatches,
                "required_dispatch_inputs": _required_dispatch_inputs(workflow),
            },
        )
    )
    failed = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    passed = sum(1 for check in checks if check["status"] == "pass")
    status = "pass" if failed == 0 else "fail"
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
