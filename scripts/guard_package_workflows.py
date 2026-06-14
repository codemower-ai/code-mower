#!/usr/bin/env python3
"""Guard generated workflow templates against packaging regressions."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MUTABLE_ACTION_RE = re.compile(r"uses:\s+actions/[^@\s]+@v\d")
HARDCODED_SSH_GITHUB_RE = re.compile(r"git@github\.com:[^{}\s]+")
PRIVATE_SHADOW_TARGETS = (
    "templates/workflows/private-standalone-shadow.yml.j2",
    "src/code_mower/templates/workflows/private-standalone-shadow.yml.j2",
)
WORKFLOW_SOURCES = (
    ".github/workflows/ci.yml",
    "templates/workflows/blind-review-artifacts-dry-run.yml.j2",
    "templates/workflows/private-standalone-shadow.yml.j2",
    "src/code_mower/templates/workflows/private-standalone-shadow.yml.j2",
    "src/code_mower/package.py",
)


def main() -> int:
    errors: list[str] = []

    for rel_path in WORKFLOW_SOURCES:
        path = ROOT / rel_path
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if MUTABLE_ACTION_RE.search(line):
                errors.append(f"{rel_path}:{line_no}: mutable GitHub action tag: {line.strip()}")

    for rel_path in PRIVATE_SHADOW_TARGETS:
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        if "code_mower_standalone_repo_url" not in text:
            errors.append(f"{rel_path}: missing code_mower_standalone_repo_url placeholder")
        if "code_mower_standalone_package_repo_url" not in text:
            errors.append(f"{rel_path}: missing code_mower_standalone_package_repo_url placeholder")
        if "package-install-rehearsal" not in text:
            errors.append(f"{rel_path}: missing package-install rehearsal")
        if 'code_mower_ref="${CODE_MOWER_STANDALONE_REF:-}"' not in text:
            errors.append(f"{rel_path}: package rehearsal does not honor CODE_MOWER_STANDALONE_REF env override")
        if HARDCODED_SSH_GITHUB_RE.search(text):
            errors.append(f"{rel_path}: hard-codes a private SSH GitHub repository URL")
        if "{% raw %}${{ secrets.CODE_MOWER_STANDALONE_DEPLOY_KEY }}{% endraw %}" not in text:
            errors.append(f"{rel_path}: GitHub secret expression is not Jinja-raw protected")

    package_text = (ROOT / "src/code_mower/package.py").read_text(encoding="utf-8")
    if "code_mower_standalone_repo_url" not in package_text:
        errors.append("src/code_mower/package.py: generated private shadow template is not parameterized")
    if "code_mower_standalone_package_repo_url" not in package_text:
        errors.append("src/code_mower/package.py: generated private shadow template lacks package repo URL")
    if "package-install-rehearsal" not in package_text:
        errors.append("src/code_mower/package.py: generated private shadow template lacks package-install rehearsal")
    if 'code_mower_ref="${CODE_MOWER_STANDALONE_REF:-}"' not in package_text:
        errors.append("src/code_mower/package.py: package rehearsal does not honor CODE_MOWER_STANDALONE_REF env override")
    if HARDCODED_SSH_GITHUB_RE.search(package_text):
        errors.append("src/code_mower/package.py: hard-codes a private SSH GitHub repository URL")

    if errors:
        print("package workflow guard failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("package workflow guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
