"""Shared CodeMower.com token/profile resolution.

Token values stay in memory only. Callers should serialize only the safe
diagnostic fields returned by CloudTokenResolution.safe_detail().
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import CloudBundleError
from .setup import (
    DEFAULT_INSTALL_ID_ENV,
    DEFAULT_TEAM_ID_ENV,
    DEFAULT_TOKEN_ENV,
    DEFAULT_UPLOAD_ENDPOINT,
    safe_config_stem,
)


CURRENT_PROFILE_FILENAME = ".current-profile"
DEFAULT_TOKEN_DIR = Path("~/.config/code-mower/tokens")
_SAFE_CONFIG_DIR = "~/.config/code-mower/tokens"


@dataclass(frozen=True)
class CloudTokenProfile:
    token: str
    endpoint: str = ""
    team_id: str = ""
    install_id: str = ""


@dataclass(frozen=True)
class CloudTokenResolution:
    status: str
    token_env: str
    source: str
    token: str = ""
    token_file: Path | None = None
    token_dir: Path | None = None
    token_files: tuple[str, ...] = ()
    endpoint: str = ""
    team_id: str = ""
    install_id: str = ""
    message: str = ""
    remediation: str = ""

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    def safe_detail(self) -> dict[str, object]:
        detail: dict[str, object] = {
            "token_env": self.token_env,
            "source": self.source,
        }
        if self.token_file is not None:
            detail["token_file"] = display_token_path(self.token_file)
            detail["shell"] = source_token_command(self.token_file)
        if self.token_files:
            source_dir = (self.token_dir or default_token_dir()).expanduser()
            detail["token_files"] = list(self.token_files)
            detail["source_commands"] = [
                source_token_command(source_dir / name)
                for name in self.token_files
            ]
        return detail


def default_token_dir() -> Path:
    return DEFAULT_TOKEN_DIR.expanduser()


def display_token_path(path: Path) -> str:
    expanded = path.expanduser()
    try:
        rel = expanded.resolve(strict=False).relative_to(
            default_token_dir().resolve(strict=False)
        )
    except ValueError:
        return str(expanded)
    return f"{_SAFE_CONFIG_DIR}/{rel.as_posix()}"


def source_token_command(path: Path) -> str:
    return f"source {shlex.quote(str(path.expanduser()))}"


def current_profile_path(token_dir: Path | None = None) -> Path:
    return (token_dir or default_token_dir()).expanduser() / CURRENT_PROFILE_FILENAME


def write_current_token_profile(path: Path) -> Path:
    target = path.expanduser()
    pointer = current_profile_path(target.parent)
    try:
        pointer.write_text(target.name + "\n", encoding="utf-8")
        pointer.chmod(0o600)
    except OSError as exc:
        raise CloudBundleError(f"unable to write current token profile: {exc}") from exc
    return pointer


def _parse_assignment_value(value: str) -> str:
    value = value.strip()
    try:
        parsed = shlex.split(value, posix=True)
    except ValueError as exc:
        raise CloudBundleError("token file contains an invalid quoted value") from exc
    if len(parsed) == 1:
        return parsed[0]
    return value.strip("'\"")


def _profile_from_text(
    text: str,
    *,
    token_env: str,
    allow_raw: bool,
) -> CloudTokenProfile:
    assignments: dict[str, str] = {}
    saw_assignment = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name.replace("_", "").isalnum() or name[:1].isdigit():
            continue
        saw_assignment = True
        assignments[name] = _parse_assignment_value(value)

    token = assignments.get(token_env, "").strip()
    if not token and allow_raw and not saw_assignment:
        token = text.strip()
    if not token:
        raise CloudBundleError(f"token file does not define {token_env}")
    return CloudTokenProfile(
        token=token,
        endpoint=assignments.get("CODE_MOWER_CLOUD_ENDPOINT", "").strip(),
        team_id=assignments.get(DEFAULT_TEAM_ID_ENV, "").strip(),
        install_id=assignments.get(DEFAULT_INSTALL_ID_ENV, "").strip(),
    )


def read_token_profile(
    path: Path,
    *,
    token_env: str = DEFAULT_TOKEN_ENV,
    allow_raw: bool = False,
) -> CloudTokenProfile:
    source = path.expanduser()
    if not source.is_file():
        raise CloudBundleError(
            f"token file does not exist or is not a file: {display_token_path(source)}"
        )
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CloudBundleError(
            f"token file is not UTF-8 text: {display_token_path(source)}"
        ) from exc
    except OSError as exc:
        raise CloudBundleError(
            f"unable to read token file {display_token_path(source)}: {exc}"
        ) from exc
    try:
        return _profile_from_text(text, token_env=token_env, allow_raw=allow_raw)
    except CloudBundleError as exc:
        raise CloudBundleError(f"{display_token_path(source)}: {exc}") from exc


def _resolution_from_profile(
    *,
    profile: CloudTokenProfile,
    token_env: str,
    source: str,
    token_file: Path | None,
) -> CloudTokenResolution:
    return CloudTokenResolution(
        status="ok",
        token_env=token_env,
        source=source,
        token=profile.token,
        token_file=token_file,
        token_dir=token_file.expanduser().parent if token_file else None,
        endpoint=profile.endpoint,
        team_id=profile.team_id,
        install_id=profile.install_id,
        message=f"Code Mower Cloud token resolved from {source}",
    )


def _candidate_for_install_id(token_dir: Path, install_id: str) -> Path:
    return token_dir / f"{safe_config_stem(install_id)}.env"


def _resolve_current_profile(token_dir: Path) -> Path | None:
    pointer = current_profile_path(token_dir)
    if not pointer.exists():
        return None
    try:
        raw = pointer.read_text(encoding="utf-8").strip().splitlines()[0]
    except (IndexError, OSError, UnicodeDecodeError) as exc:
        raise CloudBundleError(
            f"{display_token_path(pointer)} is not a readable current profile"
        ) from exc
    name = raw.strip()
    if not name or "/" in name or "\\" in name or not name.endswith(".env"):
        raise CloudBundleError(
            f"{display_token_path(pointer)} does not name a token profile"
        )
    return token_dir / name


def _safe_file_names(paths: list[Path]) -> tuple[str, ...]:
    return tuple(path.name for path in sorted(paths))


def resolve_cloud_token(
    *,
    token_env: str = DEFAULT_TOKEN_ENV,
    token_file: Path | None = None,
    token_dir: Path | None = None,
    install_id: str = "",
    env: Mapping[str, str] | None = None,
) -> CloudTokenResolution:
    current_env = os.environ if env is None else env
    env_token = current_env.get(token_env, "").strip()
    if env_token:
        return _resolution_from_profile(
            profile=CloudTokenProfile(
                token=env_token,
                endpoint=current_env.get("CODE_MOWER_CLOUD_ENDPOINT", "").strip(),
                team_id=current_env.get(DEFAULT_TEAM_ID_ENV, "").strip(),
                install_id=current_env.get(DEFAULT_INSTALL_ID_ENV, "").strip(),
            ),
            token_env=token_env,
            source="env",
            token_file=None,
        )

    directory = (token_dir or default_token_dir()).expanduser()
    selected_path: Path | None = None
    try:
        if token_file is not None:
            selected_path = token_file
            profile = read_token_profile(
                token_file,
                token_env=token_env,
                allow_raw=True,
            )
            return _resolution_from_profile(
                profile=profile,
                token_env=token_env,
                source="token_file",
                token_file=token_file,
            )

        selected_install_id = install_id.strip()
        if selected_install_id:
            selected = _candidate_for_install_id(directory, selected_install_id)
            selected_path = selected
            profile = read_token_profile(
                selected,
                token_env=token_env,
                allow_raw=False,
            )
            return _resolution_from_profile(
                profile=profile,
                token_env=token_env,
                source="install_id",
                token_file=selected,
            )

        current = _resolve_current_profile(directory)
        if current is not None:
            selected_path = current
            profile = read_token_profile(
                current,
                token_env=token_env,
                allow_raw=False,
            )
            return _resolution_from_profile(
                profile=profile,
                token_env=token_env,
                source="current_profile",
                token_file=current,
            )
    except CloudBundleError as exc:
        return CloudTokenResolution(
            status="malformed",
            token_env=token_env,
            source="file",
            token_file=selected_path,
            token_dir=directory,
            message=str(exc),
            remediation=(
                "Run `code-mower cloud setup --token-stdin` again, or pass "
                "--token-file with a sourceable token env file."
            ),
        )

    valid: list[Path] = []
    malformed: list[Path] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.env")):
            try:
                read_token_profile(path, token_env=token_env, allow_raw=False)
            except CloudBundleError:
                malformed.append(path)
            else:
                valid.append(path)

    if len(valid) == 1:
        profile = read_token_profile(valid[0], token_env=token_env, allow_raw=False)
        return _resolution_from_profile(
            profile=profile,
            token_env=token_env,
            source="single_profile",
            token_file=valid[0],
        )
    if len(valid) > 1:
        names = _safe_file_names(valid)
        return CloudTokenResolution(
            status="ambiguous",
            token_env=token_env,
            source="token_dir",
            token_dir=directory,
            token_files=names,
            message="multiple Code Mower Cloud token files found; no profile selected",
            remediation=(
                "Pass --install-id, pass --token-file, or source exactly one "
                "token file before rerunning."
            ),
        )
    if malformed:
        return CloudTokenResolution(
            status="malformed",
            token_env=token_env,
            source="token_dir",
            token_dir=directory,
            token_files=_safe_file_names(malformed),
            message="stored token files were found but none define the requested token env",
            remediation=(
                "Run `code-mower cloud setup --token-stdin` again, or pass "
                "--token-file with a sourceable token env file."
            ),
        )
    return CloudTokenResolution(
        status="missing",
        token_env=token_env,
        source="missing",
        token_dir=directory,
        message=f"{token_env} is not set and no stored Code Mower Cloud token was found",
        remediation=(
            "Run `code-mower cloud setup --token-stdin`, pass --token-file, "
            f"or export {token_env} before using --yes."
        ),
    )


def resolve_cloud_endpoint(endpoint: str, resolution: CloudTokenResolution) -> str:
    if endpoint == DEFAULT_UPLOAD_ENDPOINT and resolution.endpoint:
        return resolution.endpoint
    return endpoint


def resolve_cloud_identity(
    *,
    team_id: str,
    install_id: str,
    resolution: CloudTokenResolution,
) -> tuple[str, str]:
    return (
        team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "") or resolution.team_id,
        install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "") or resolution.install_id,
    )


def require_upload_token(
    *,
    endpoint: str,
    resolution: CloudTokenResolution,
    local_endpoint: bool,
) -> str:
    if resolution.token:
        return resolution.token
    if local_endpoint:
        return ""
    detail = resolution.message or f"{resolution.token_env} is not set"
    remediation = resolution.remediation
    if resolution.token_files:
        detail = f"{detail}; candidates: {', '.join(resolution.token_files)}"
    if remediation:
        detail = f"{detail}. {remediation}"
    raise CloudBundleError(detail)
