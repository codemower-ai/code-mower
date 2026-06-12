#!/usr/bin/env python3
"""Scan tracked files for public-release privacy and secret hygiene issues."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SUBSTRINGS = (
    "j" + "huber",
    "Jeff" + " Huber",
    "j" + "huber@gmail",
    "/" + "Users/",
    "/private" + "/tmp",
    "cube" + "-snap",
    "cube" + "-two-view-debugger",
    "Cube" + "Snap",
    "CT" + "VD",
)

FORBIDDEN_REGEXES = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(
        r"\b(?:"
        r"GITHUB_TOKEN|GH_TOKEN|GEMINI_API_KEY|GOOGLE_API_KEY|"
        r"ANTHROPIC_API_KEY|OPENAI_API_KEY"
        r")\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}",
    ),
    re.compile(
        r"\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
    ),
)

DEFAULT_EXCLUDES = frozenset(
    {
        "scripts/privacy_scan.py",
    }
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    pattern: str


def _tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [root / line for line in completed.stdout.splitlines() if line.strip()]


def _scan_text(path: Path, rel_path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern in FORBIDDEN_SUBSTRINGS:
            if pattern in line:
                findings.append(
                    Finding(
                        path=rel_path,
                        line=line_no,
                        kind="forbidden-substring",
                        pattern=pattern,
                    )
                )
        for regex in FORBIDDEN_REGEXES:
            if regex.search(line):
                findings.append(
                    Finding(
                        path=rel_path,
                        line=line_no,
                        kind="forbidden-regex",
                        pattern=regex.pattern,
                    )
                )
    return findings


def scan(root: Path = ROOT, *, excludes: set[str] | None = None) -> list[Finding]:
    excludes = DEFAULT_EXCLUDES | set(excludes or set())
    findings: list[Finding] = []
    for path in _tracked_files(root):
        rel_path = path.relative_to(root).as_posix()
        if rel_path in excludes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(_scan_text(path, rel_path, text))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    findings = scan(ROOT)
    if args.json:
        print(
            json.dumps(
                {
                    "mode": "privacy-scan",
                    "status": "fail" if findings else "pass",
                    "finding_count": len(findings),
                    "findings": [asdict(finding) for finding in findings],
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif findings:
        print("privacy scan failed:", file=sys.stderr)
        for finding in findings:
            print(
                f"- {finding.path}:{finding.line}: {finding.kind}: {finding.pattern}",
                file=sys.stderr,
            )
    else:
        print("privacy scan passed")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
