"""Local token/config handling for CodeMower.com setup flows."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from .endpoints import validate_upload_endpoint
from .errors import CloudBundleError


DEFAULT_UPLOAD_ENDPOINT = "https://codemower.com/api/ingest"
DEFAULT_TOKEN_ENV = "CODE_MOWER_CLOUD_TOKEN"
DEFAULT_TEAM_ID_ENV = "CODE_MOWER_CLOUD_TEAM_ID"
DEFAULT_INSTALL_ID_ENV = "CODE_MOWER_INSTALL_ID"
DEFAULT_SETUP_INSTALL_ID = "code-mower-local"


def token_prefix(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    visible = min(12, max(4, len(token) // 2), max(0, len(token) - 4))
    if visible <= 0:
        return "<redacted>"
    return token[:visible] + "..."


def safe_config_stem(value: str) -> str:
    stem = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in value.strip()
    )
    return stem.strip("-_.") or DEFAULT_SETUP_INSTALL_ID


def default_setup_path(install_id: str) -> Path:
    return (
        Path.home()
        / ".config"
        / "code-mower"
        / "tokens"
        / f"{safe_config_stem(install_id)}.env"
    )


def read_token_file(path: Path) -> str:
    source = path.expanduser()
    if not source.is_file():
        raise CloudBundleError(f"token file does not exist or is not a file: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CloudBundleError(f"token file is not UTF-8 text: {source}") from exc
    except OSError as exc:
        raise CloudBundleError(f"unable to read token file {source}: {exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if stripped.startswith(f"{DEFAULT_TOKEN_ENV}="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return text.strip()


def resolve_setup_token(
    *,
    token: str,
    token_file: Path | None,
    token_stdin: bool,
    token_env: str,
) -> str:
    explicit_sources = sum(
        1 for value in (bool(token), token_file is not None, token_stdin) if value
    )
    if explicit_sources > 1:
        raise CloudBundleError(
            "choose only one token source: --token, --token-file, or --token-stdin"
        )
    if token:
        resolved = token.strip()
    elif token_file is not None:
        resolved = read_token_file(token_file)
    elif token_stdin:
        resolved = sys.stdin.read().strip()
    else:
        resolved = os.environ.get(token_env, "").strip()
    if not resolved:
        raise CloudBundleError(
            "cloud setup needs a token; pass --token-stdin, --token-file, "
            f"or set {token_env}"
        )
    return resolved


def render_setup_env(
    *,
    token: str,
    endpoint: str,
    team_id: str,
    install_id: str,
) -> str:
    try:
        validate_upload_endpoint(endpoint)
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc
    assignments = {
        DEFAULT_TOKEN_ENV: token.strip(),
        "CODE_MOWER_CLOUD_ENDPOINT": endpoint.strip(),
        DEFAULT_TEAM_ID_ENV: team_id.strip(),
        DEFAULT_INSTALL_ID_ENV: install_id.strip(),
    }
    lines = [
        "# Code Mower Cloud local token file",
        "# Keep this file private. It contains a bearer token.",
    ]
    lines.extend(
        f"export {name}={shlex.quote(value)}"
        for name, value in assignments.items()
        if value
    )
    return "\n".join(lines) + "\n"


def write_setup_env_file(
    *,
    path: Path,
    text: str,
    force: bool = False,
) -> None:
    target = path.expanduser()
    if target.exists() and not force:
        raise CloudBundleError(
            f"setup file already exists; pass --force to overwrite: {target}"
        )
    parent_existed = target.parent.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            target.parent.chmod(0o700)
    except OSError as exc:
        raise CloudBundleError(
            f"unable to prepare setup directory {target.parent}: {exc}"
        ) from exc
    flags = os.O_WRONLY | os.O_CREAT
    if not force:
        flags |= os.O_EXCL
    try:
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.truncate(0)
            handle.write(text)
        target.chmod(0o600)
    except FileExistsError as exc:
        raise CloudBundleError(
            f"setup file already exists; pass --force to overwrite: {target}"
        ) from exc
    except OSError as exc:
        raise CloudBundleError(f"unable to write setup file {target}: {exc}") from exc


def run_cloud_setup(
    *,
    token: str,
    token_file: Path | None,
    token_stdin: bool,
    token_env: str,
    endpoint: str,
    team_id: str,
    install_id: str,
    out: Path | None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, str]:
    resolved_install_id = install_id.strip() or DEFAULT_SETUP_INSTALL_ID
    target = out.expanduser() if out else default_setup_path(resolved_install_id)
    resolved_token = resolve_setup_token(
        token=token,
        token_file=token_file,
        token_stdin=token_stdin,
        token_env=token_env,
    )
    env_text = render_setup_env(
        token=resolved_token,
        endpoint=endpoint,
        team_id=team_id,
        install_id=resolved_install_id,
    )
    if not dry_run:
        write_setup_env_file(path=target, text=env_text, force=force)
    return {
        "mode": "cloud-setup",
        "status": "dry_run" if dry_run else "written",
        "path": str(target),
        "endpoint": endpoint,
        "team_id": team_id,
        "install_id": resolved_install_id,
        "token_prefix": token_prefix(resolved_token),
        "shell": f"source {shlex.quote(str(target))}",
    }
