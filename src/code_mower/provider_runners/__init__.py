"""Shared provider-runner primitives.

Provider-specific modules remain responsible for prompts and verdict parsing.
This package holds common process/auth helpers used by Codex, Claude,
Gemini/Antigravity, Hermes, and local reviewer lanes.
"""

from .comments import (
    AUDIT_RUN_TRAILER_PREFIX,
    MAX_GITHUB_COMMENT_CHARS,
    STALE_REQUEUE_MARKER,
    UNKNOWN_REQUEUE_MARKER,
    audit_comment_posture,
    bind_actions_run_comment_id,
    format_audit_comment_header,
    limit_comment_body,
    normalize_calibration_badge,
)
from .exit_codes import audit_exit_code
from .git import fetch_local_checkout_diff, local_head_sha, run_git
from .github_auth import (
    pop_github_token_env,
    resolve_github_token_from_env_or_gh,
    resolve_github_token_from_stdin_or_env,
)
from .github_pr import (
    edit_pr_comment,
    fetch_issue_comments,
    fetch_pull_request,
    fetch_pull_request_diff,
    fetch_pull_request_files,
    post_pr_comment,
)
from .process import DEFAULT_HOME_ENV_KEYS, build_allowlisted_child_env
from .pr_worktree import (
    FetchedHeadMismatch,
    create_temp_worktree,
    fetch_base_ref,
    fetch_base_ref_sha,
    fetch_pr_head,
    fetch_pr_head_sha,
    fetch_pr_head_sha_or_raise,
    fetch_pr_head_sha_unless_local_matches,
    fetch_pr_head_unless_local_matches,
    local_checkout_matches_head,
    remove_worktree,
    run_git_text,
)
from .repo_paths import (
    LOCAL_AUDIT_RUNNER_DOC,
    parse_repo_paths,
    validate_repo_path_for_wrapper,
)
from .text_schema import clip_text, one_line, require_exact_keys
from .verdict_artifacts import (
    audit_runtime_quarantine_reason,
    fixture_verdict_comment_reason,
    is_fixture_structured_verdict,
    is_fixture_verdict_artifact,
    is_fixture_verdict_comment,
    load_audit_verdict_artifact,
    repost_audit_verdict_artifact,
    validate_audit_verdict_artifact_payload,
    write_audit_verdict_artifact,
)
from .workspace import (
    ProviderWorkspaceError,
    verify_checkout_at_head,
    working_tree_status,
)

__all__ = [
    "fetch_pull_request",
    "fetch_pull_request_diff",
    "fetch_pull_request_files",
    "fetch_issue_comments",
    "edit_pr_comment",
    "AUDIT_RUN_TRAILER_PREFIX",
    "audit_comment_posture",
    "audit_exit_code",
    "FetchedHeadMismatch",
    "fetch_base_ref",
    "fetch_base_ref_sha",
    "fetch_local_checkout_diff",
    "fetch_pr_head",
    "fetch_pr_head_sha",
    "fetch_pr_head_sha_or_raise",
    "fetch_pr_head_sha_unless_local_matches",
    "fetch_pr_head_unless_local_matches",
    "fixture_verdict_comment_reason",
    "format_audit_comment_header",
    "is_fixture_verdict_artifact",
    "is_fixture_verdict_comment",
    "is_fixture_structured_verdict",
    "local_checkout_matches_head",
    "load_audit_verdict_artifact",
    "local_head_sha",
    "limit_comment_body",
    "MAX_GITHUB_COMMENT_CHARS",
    "audit_runtime_quarantine_reason",
    "bind_actions_run_comment_id",
    "clip_text",
    "DEFAULT_HOME_ENV_KEYS",
    "one_line",
    "parse_repo_paths",
    "LOCAL_AUDIT_RUNNER_DOC",
    "pop_github_token_env",
    "post_pr_comment",
    "ProviderWorkspaceError",
    "repost_audit_verdict_artifact",
    "require_exact_keys",
    "resolve_github_token_from_env_or_gh",
    "resolve_github_token_from_stdin_or_env",
    "run_git",
    "run_git_text",
    "validate_audit_verdict_artifact_payload",
    "STALE_REQUEUE_MARKER",
    "UNKNOWN_REQUEUE_MARKER",
    "normalize_calibration_badge",
    "validate_repo_path_for_wrapper",
    "create_temp_worktree",
    "remove_worktree",
    "verify_checkout_at_head",
    "working_tree_status",
    "build_allowlisted_child_env",
    "write_audit_verdict_artifact",
]
