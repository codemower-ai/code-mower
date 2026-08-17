"""Shared GitHub comment formatting helpers for provider wrappers."""

from __future__ import annotations

import hashlib
import re


MAX_GITHUB_COMMENT_CHARS = 64_000
AUDIT_RUN_TRAILER_RE = re.compile(
    r"<!--\s*CODE_MOWER_AUDIT_RUN:\s*run_id=([0-9]+)"
    r"(?:\s+comment_id=([0-9]+))?"
    r"(?:\s+body_sha256=[0-9a-f]{64})?\s*-->"
)


def limit_comment_body(
    body: str,
    trailer: str,
    *,
    provider_name: str,
    max_chars: int = MAX_GITHUB_COMMENT_CHARS,
) -> str:
    """Keep an audit comment under GitHub's body-size limit without losing trailer state."""

    if len(body) <= max_chars:
        return body

    note = (
        f"\n\n[{provider_name} audit comment truncated to stay under "
        "GitHub's comment-size limit.]\n\n"
    )
    suffix = note + trailer + "\n"
    allowed_prefix_len = max_chars - len(suffix)
    if allowed_prefix_len < 0:
        return suffix[-max_chars:]

    prefix = body.rsplit(trailer, 1)[0] if trailer in body else body
    return prefix[:allowed_prefix_len].rstrip() + suffix


def bind_actions_run_comment_id(body: str, comment_id: object) -> str:
    """Bind a workflow audit-run marker to the GitHub comment id just created."""

    comment_id_text = str(comment_id or "").strip()
    if not comment_id_text:
        raise ValueError("posted GitHub comment response did not include an id")

    def replace_without_digest(match: re.Match[str]) -> str:
        return (
            "<!-- CODE_MOWER_AUDIT_RUN: "
            f"run_id={match.group(1)} comment_id={comment_id_text} -->"
        )

    without_digest, count = AUDIT_RUN_TRAILER_RE.subn(
        replace_without_digest,
        body,
        count=1,
    )
    if count == 0:
        raise ValueError("audit comment does not contain a CODE_MOWER_AUDIT_RUN marker")
    body_digest = hashlib.sha256(without_digest.encode("utf-8")).hexdigest()

    def replace_with_digest(match: re.Match[str]) -> str:
        return (
            "<!-- CODE_MOWER_AUDIT_RUN: "
            f"run_id={match.group(1)} comment_id={comment_id_text} "
            f"body_sha256={body_digest} -->"
        )

    return AUDIT_RUN_TRAILER_RE.sub(replace_with_digest, without_digest, count=1)
