"""Shared provider-runner primitives.

Provider-specific modules remain responsible for prompts and verdict parsing.
This package holds common process/auth helpers used by Codex, Claude,
Gemini/Antigravity, Hermes, and local reviewer lanes.
"""

from .comments import MAX_GITHUB_COMMENT_CHARS, limit_comment_body
from .git import fetch_local_checkout_diff, local_head_sha, run_git
from .github_auth import (
    pop_github_token_env,
    resolve_github_token_from_env_or_gh,
    resolve_github_token_from_stdin_or_env,
)
from .github_pr import fetch_pull_request, post_pr_comment
from .process import DEFAULT_HOME_ENV_KEYS, build_allowlisted_child_env
from .repo_paths import parse_repo_paths
from .text_schema import clip_text, one_line, require_exact_keys
from .verdict_artifacts import (
    load_audit_verdict_artifact,
    repost_audit_verdict_artifact,
    write_audit_verdict_artifact,
)

__all__ = [
    "fetch_pull_request",
    "fetch_local_checkout_diff",
    "load_audit_verdict_artifact",
    "local_head_sha",
    "limit_comment_body",
    "MAX_GITHUB_COMMENT_CHARS",
    "clip_text",
    "DEFAULT_HOME_ENV_KEYS",
    "one_line",
    "parse_repo_paths",
    "pop_github_token_env",
    "post_pr_comment",
    "repost_audit_verdict_artifact",
    "require_exact_keys",
    "resolve_github_token_from_env_or_gh",
    "resolve_github_token_from_stdin_or_env",
    "run_git",
    "build_allowlisted_child_env",
    "write_audit_verdict_artifact",
]
