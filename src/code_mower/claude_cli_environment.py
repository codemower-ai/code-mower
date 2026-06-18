"""Claude CLI environment cleanup helpers."""

from __future__ import annotations

from collections.abc import Mapping


CLAUDE_AUTH_OVERRIDE_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
    "CLAUDE_CONFIG_DIR",
)

GITHUB_TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")

_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def clean_claude_cli_env(
    base_env: Mapping[str, str],
    *,
    unset_github_tokens: bool = False,
    scrub_auth_overrides: bool = True,
    extra_unset: tuple[str, ...] = (),
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return a Claude CLI child environment with known stale overrides removed."""

    env = dict(base_env)
    names: list[str] = []
    if unset_github_tokens:
        names.extend(GITHUB_TOKEN_ENV)
    if scrub_auth_overrides:
        names.extend(CLAUDE_AUTH_OVERRIDE_ENV)
    names.extend(extra_unset)

    removed: list[str] = []
    for name in dict.fromkeys(name for name in names if name):
        if name in env:
            removed.append(name)
            env.pop(name, None)
    return env, tuple(removed)


def render_claude_env_unset_snippet(
    *,
    include_comments: bool = True,
    names: tuple[str, ...] = CLAUDE_AUTH_OVERRIDE_ENV,
) -> str:
    lines: list[str] = []
    if include_comments:
        lines.extend(
            [
                "# Source this file to remove Claude/Anthropic auth overrides",
                "# from the current shell before running Claude Code.",
                "# It does not delete Claude credentials or modify your keychain.",
            ]
        )
    lines.extend(f"unset {name}" for name in names)
    return "\n".join(lines) + "\n"


__all__ = (
    "CLAUDE_AUTH_OVERRIDE_ENV",
    "GITHUB_TOKEN_ENV",
    "clean_claude_cli_env",
    "env_flag",
    "render_claude_env_unset_snippet",
)
