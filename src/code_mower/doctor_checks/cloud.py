"""Code Mower Cloud doctor checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .models import STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck

DEFAULT_CLOUD_TOKEN_ENV = "CODE_MOWER_CLOUD_TOKEN"
DEFAULT_CLOUD_TOKEN_DIR = Path("~/.config/code-mower/tokens")


def token_file_mentions_cloud_token(path: Path, token_env: str) -> bool | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if stripped.startswith(f"{token_env}="):
            return True
    return False


def check_cloud_token_surface(
    *,
    token_env: str = DEFAULT_CLOUD_TOKEN_ENV,
    token_dir: Path | None = None,
) -> DoctorCheck:
    """Check optional Code Mower Cloud token setup without exposing secrets."""

    if os.environ.get(token_env):
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_PASS,
            message="Code Mower Cloud token env is set",
            detail={"token_env": token_env, "source": "env"},
        )

    directory = (token_dir or DEFAULT_CLOUD_TOKEN_DIR).expanduser()
    redacted_dir = "~/.config/code-mower/tokens"
    if not directory.exists():
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_SKIP,
            message="optional Code Mower Cloud token is not configured",
            detail={"token_env": token_env, "token_dir": redacted_dir},
            remediation=(
                "Cloud upload is optional. To enable it, create or receive a "
                "developer/team token, then run `code-mower cloud setup "
                "--token-stdin` and source the generated env file."
            ),
        )

    token_files: list[str] = []
    unreadable_files: list[str] = []
    insecure_files: list[str] = []
    for path in sorted(directory.glob("*.env")):
        has_token = token_file_mentions_cloud_token(path, token_env)
        if has_token is None:
            unreadable_files.append(path.name)
            continue
        if not has_token:
            continue
        token_files.append(path.name)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            unreadable_files.append(path.name)
            continue
        if mode & 0o077:
            insecure_files.append(path.name)

    if unreadable_files:
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_WARN,
            message="could not inspect one or more Code Mower Cloud token files",
            detail={
                "token_env": token_env,
                "token_dir": redacted_dir,
                "unreadable_file_count": len(unreadable_files),
                "unreadable_files": unreadable_files,
            },
            remediation=(
                "Verify token files under ~/.config/code-mower/tokens are UTF-8 "
                "env files owned by the current user."
            ),
        )

    if not token_files:
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_SKIP,
            message="optional Code Mower Cloud token file was not found",
            detail={"token_env": token_env, "token_dir": redacted_dir},
            remediation=(
                "Cloud upload is optional. To enable it, run `code-mower cloud "
                "setup --token-stdin` and source the generated env file."
            ),
        )

    detail = {
        "token_env": token_env,
        "token_dir": redacted_dir,
        "token_file_count": len(token_files),
        "token_files": token_files,
    }
    if insecure_files:
        return DoctorCheck(
            name="cloud.token",
            status=STATUS_WARN,
            message="Code Mower Cloud token file permissions are too broad",
            detail={**detail, "insecure_files": insecure_files},
            remediation="Run `chmod 600 ~/.config/code-mower/tokens/*.env`.",
        )

    return DoctorCheck(
        name="cloud.token",
        status=STATUS_PASS,
        message="Code Mower Cloud token file is configured",
        detail=detail,
    )
