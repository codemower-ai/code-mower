#!/usr/bin/env python3
"""Shared Git identity for Code Mower-created scratch repositories."""

from __future__ import annotations

SCRATCH_GIT_USER_NAME = "Code Mower Scratch"
SCRATCH_GIT_USER_EMAIL = "code-mower-scratch@example.com"


def scratch_git_config_commands(git: str) -> tuple[tuple[str, ...], ...]:
    return (
        (git, "config", "user.name", SCRATCH_GIT_USER_NAME),
        (git, "config", "user.email", SCRATCH_GIT_USER_EMAIL),
        (git, "config", "commit.gpgSign", "false"),
    )
