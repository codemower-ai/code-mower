"""Shared provider-runner primitives.

Provider-specific modules remain responsible for prompts and verdict parsing.
This package holds common process/auth helpers used by Codex, Claude,
Gemini/Antigravity, Hermes, and local reviewer lanes.
"""

from .github_auth import (
    pop_github_token_env,
    resolve_github_token_from_env_or_gh,
    resolve_github_token_from_stdin_or_env,
)
from .github_pr import fetch_pull_request, post_pr_comment

__all__ = [
    "fetch_pull_request",
    "pop_github_token_env",
    "post_pr_comment",
    "resolve_github_token_from_env_or_gh",
    "resolve_github_token_from_stdin_or_env",
]
