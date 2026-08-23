"""Version and public package-spec helpers."""

from __future__ import annotations

import re


PUBLIC_REPO_URL = "https://github.com/codemower-ai/code-mower"
TRY_IN_10_MINUTES_DOC = "docs/try-in-10-minutes.md"
STAGE_TAG_NAMES = {
    "a": "alpha",
    "b": "beta",
    "rc": "rc",
}


def release_tag_for_version(version: str) -> str:
    match = re.fullmatch(
        r"(?P<base>\d+\.\d+\.\d+)(?:(?P<stage>a|b|rc)(?P<num>\d+))?",
        version,
    )
    if not match:
        return f"v{version}"
    base = match.group("base")
    stage = match.group("stage")
    number = match.group("num")
    if not stage or not number:
        return f"v{base}"
    return f"v{base}-{STAGE_TAG_NAMES[stage]}.{number}"


def public_package_spec(version: str, repo_url: str = PUBLIC_REPO_URL) -> str:
    """Return the package-index install spec for public prerelease users."""

    return f"code-mower=={version}"


def github_package_spec(version: str, repo_url: str = PUBLIC_REPO_URL) -> str:
    """Return the GitHub-tag install spec used for release debugging."""

    return f"git+{repo_url}.git@{release_tag_for_version(version)}"


def public_baseline_sentence(version: str) -> str:
    """Return the shared current-baseline sentence for public docs."""

    return (
        f"The current verified public beta baseline is `{release_tag_for_version(version)}`, "
        f"published on PyPI as `{public_package_spec(version)}`."
    )


def tagged_doc_url(
    version: str,
    doc_path: str = TRY_IN_10_MINUTES_DOC,
    repo_url: str = PUBLIC_REPO_URL,
) -> str:
    """Return a GitHub URL for a document pinned to the release tag."""

    return f"{repo_url}/blob/{release_tag_for_version(version)}/{doc_path.lstrip('/')}"
