"""Shared GitHub comment formatting helpers for provider wrappers."""

from __future__ import annotations


MAX_GITHUB_COMMENT_CHARS = 64_000


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
