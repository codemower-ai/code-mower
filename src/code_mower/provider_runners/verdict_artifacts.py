"""Verdict artifact helpers shared by provider audit runners."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .github_pr import post_pr_comment

VERDICT_ARTIFACT_SCHEMA = "code_mower.auditVerdictArtifact.v1"
VERDICT_ARTIFACT_DIR_ENV = "CODE_MOWER_VERDICT_ARTIFACT_DIR"


def _safe_artifact_slug(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    safe = safe.strip("._-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "item"


def _verdict_artifact_root() -> Path:
    configured = os.environ.get(VERDICT_ARTIFACT_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "code-mower-audits" / "verdicts"


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
) -> Path | None:
    """Persist the rendered audit comment before posting to GitHub."""

    root = _verdict_artifact_root()
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
    return post_pr_comment(
        str(artifact["repo"]),
        int(artifact["pr_number"]),
        str(artifact["comment_body"]),
        token=token,
    )
