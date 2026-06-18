"""Version and public package-spec helpers."""

from __future__ import annotations

import re


PUBLIC_REPO_URL = "https://github.com/codemower-ai/code-mower"
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
