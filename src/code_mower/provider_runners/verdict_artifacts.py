"""Verdict artifact helpers shared by provider audit runners."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .github_pr import post_pr_comment

VERDICT_ARTIFACT_SCHEMA = "code_mower.auditVerdictArtifact.v1"
VERDICT_ARTIFACT_DIR_ENV = "CODE_MOWER_VERDICT_ARTIFACT_DIR"
VERDICT_QUARANTINE_DIR_ENV = "CODE_MOWER_VERDICT_QUARANTINE_DIR"
FIXTURE_VERDICT_TEXT = frozenset({"test", "example", "placeholder", "t", "d"})
FIXTURE_VERDICT_PATHS = frozenset(
    {
        "a.py",
        "a.txt",
        "a.yml",
        "f",
        "file",
        "file.py",
        "test.py",
        "example.py",
        "placeholder.py",
    }
)
FINDING_LINE_RE = re.compile(
    r"^\s*-\s*\[P[0-3]\]\s*(?P<title>.*?)\s*(?:--|—)\s*`?"
    r"(?P<file>[^`:\s]+):(?P<line>\d+)`?",
    flags=re.MULTILINE,
)
SUMMARY_RE = re.compile(
    r"^\s*Summary:\s*(?P<body>.*?)(?:^\s*Findings:\s*|\Z)",
    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _safe_artifact_slug(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    safe = safe.strip("._-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "item"


def _cache_home() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache"


def _verdict_artifact_root(*, quarantine: bool = False) -> Path:
    if quarantine:
        configured = os.environ.get(VERDICT_QUARANTINE_DIR_ENV, "").strip()
        if configured:
            return Path(configured).expanduser()
        configured_artifacts = os.environ.get(VERDICT_ARTIFACT_DIR_ENV, "").strip()
        if configured_artifacts:
            artifact_root = Path(configured_artifacts).expanduser()
            return artifact_root.parent / "quarantine" / artifact_root.name
        return _cache_home() / "code-mower-audits" / "quarantine" / "verdicts"
    configured = os.environ.get(VERDICT_ARTIFACT_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return _cache_home() / "code-mower-audits" / "verdicts"


def _normalized_fixture_token(value: Any) -> str:
    text = str(value or "").strip().strip("`'\"").lower()
    text = re.sub(r"\s+", " ", text)
    return text.rstrip(".:;")


def _is_fixture_text(value: Any) -> bool:
    return _normalized_fixture_token(value) in FIXTURE_VERDICT_TEXT


def _is_fixture_path(value: Any) -> bool:
    path = str(value or "").strip().strip("`'\"").replace("\\", "/").lower()
    while path.startswith("./"):
        path = path[2:]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path in FIXTURE_VERDICT_PATHS


def _summary_first_line(comment_body: str) -> str:
    match = SUMMARY_RE.search(comment_body)
    if not match:
        return ""
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def fixture_verdict_comment_reason(comment_body: str) -> str | None:
    """Return a reason when a rendered audit comment is clearly test output."""

    summary = _summary_first_line(comment_body)
    if not _is_fixture_text(summary):
        return None
    findings = list(FINDING_LINE_RE.finditer(comment_body))
    if not findings:
        return None
    fixture_findings = [
        match
        for match in findings
        if _is_fixture_text(match.group("title")) and _is_fixture_path(match.group("file"))
    ]
    if len(fixture_findings) != len(findings):
        return None
    return "fixture-shaped audit verdict comment"


def is_fixture_verdict_comment(comment_body: str) -> bool:
    return fixture_verdict_comment_reason(comment_body) is not None


def is_fixture_verdict_artifact(payload: dict[str, Any]) -> bool:
    if payload.get("schema") != VERDICT_ARTIFACT_SCHEMA:
        return False
    if payload.get("quarantined") is True:
        return True
    comment_body = str(payload.get("comment_body") or "")
    return is_fixture_verdict_comment(comment_body)


def is_fixture_structured_verdict(
    summary: Any,
    findings: Any,
) -> bool:
    if not _is_fixture_text(summary) or not isinstance(findings, list) or not findings:
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        if not _is_fixture_text(finding.get("title")):
            return False
        if not _is_fixture_path(finding.get("file")):
            return False
    return True


def audit_runtime_quarantine_reason(
    *,
    comment_body: str,
    fixture_reason: str | None = None,
) -> str | None:
    reasons: list[str] = []
    if os.environ.get("PYTEST_CURRENT_TEST"):
        reasons.append("PYTEST_CURRENT_TEST is set")
    if fixture_reason:
        reasons.append(fixture_reason)
    return "; ".join(reasons) if reasons else None


def write_audit_verdict_artifact(
    *,
    lane_id: str,
    repo: str,
    pr_number: int,
    head_sha_start: str,
    head_sha_end: str,
    verdict: str,
    trailer: str,
    comment_body: str,
    quarantine_reason: str | None = None,
) -> Path | None:
    """Persist the rendered audit comment before posting to GitHub."""

    root = _verdict_artifact_root(quarantine=bool(quarantine_reason))
    repo_slug = _safe_artifact_slug(repo.replace("/", "__"))
    head_slug = _safe_artifact_slug(head_sha_start[:16] or "unknown-head")
    lane_slug = _safe_artifact_slug(lane_id)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"{timestamp}-{lane_slug}-{_safe_artifact_slug(verdict.lower())}.json"
    path = root / repo_slug / f"pr-{pr_number}" / head_slug / filename
    payload = {
        "schema": VERDICT_ARTIFACT_SCHEMA,
        "lane_id": lane_id,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha_start": head_sha_start,
        "head_sha_end": head_sha_end,
        "verdict": verdict,
        "trailer": trailer,
        "comment_body": comment_body,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "posted_comment_url": None,
    }
    if quarantine_reason:
        payload["quarantined"] = True
        payload["quarantine_reason"] = quarantine_reason
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        print(
            f"warning: failed to write audit verdict artifact {path}: {exc}",
            file=sys.stderr,
        )
        return None


def load_audit_verdict_artifact(path: Path) -> dict[str, Any]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verdict artifact must contain a JSON object")
    if payload.get("schema") != VERDICT_ARTIFACT_SCHEMA:
        raise ValueError(
            f"unsupported verdict artifact schema: {payload.get('schema')!r}"
        )
    for key in ("repo", "pr_number", "comment_body"):
        if key not in payload:
            raise ValueError(f"verdict artifact missing {key}")
    return payload


def repost_audit_verdict_artifact(path: Path, *, token: str) -> dict[str, Any]:
    artifact = load_audit_verdict_artifact(path)
    if is_fixture_verdict_artifact(artifact):
        raise ValueError("refusing to repost quarantined or fixture-shaped verdict artifact")
    return post_pr_comment(
        str(artifact["repo"]),
        int(artifact["pr_number"]),
        str(artifact["comment_body"]),
        token=token,
    )
