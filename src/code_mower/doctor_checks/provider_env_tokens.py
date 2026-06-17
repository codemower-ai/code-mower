"""Provider token environment status helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import SUPPORTED_TOKEN_FILE_ENV_NAMES, as_sequence, code_mower_secrets


@dataclass(frozen=True)
class ProviderTokenStatus:
    token_env: tuple[str, ...]
    token_env_any: tuple[tuple[str, ...], ...]
    token_file_env: tuple[str, ...]
    missing: tuple[str, ...]
    missing_any: tuple[tuple[str, ...], ...]

    @property
    def declares_tokens(self) -> bool:
        return bool(self.token_env or self.token_env_any)

    @property
    def all_present(self) -> bool:
        return not self.missing and not self.missing_any


def _token_file_value(name: str, path_text: str) -> str:
    if name not in SUPPORTED_TOKEN_FILE_ENV_NAMES:
        return ""
    try:
        result = code_mower_secrets.read_secret_file(
            Path(path_text),
            supported_env_names=SUPPORTED_TOKEN_FILE_ENV_NAMES,
        )
    except OSError:
        return ""
    return result.value


def _token_is_present(name: str) -> bool:
    if os.environ.get(name):
        return True
    path_text = os.environ.get(f"{name}_FILE", "").strip()
    if not path_text:
        return False
    return bool(_token_file_value(name, path_text))


def _token_file_env_names(
    token_env: tuple[str, ...],
    token_env_any: tuple[tuple[str, ...], ...],
) -> tuple[str, ...]:
    names = [
        f"{name}_FILE"
        for name in token_env
        if name in SUPPORTED_TOKEN_FILE_ENV_NAMES and os.environ.get(f"{name}_FILE")
    ]
    names.extend(
        f"{name}_FILE"
        for group in token_env_any
        for name in group
        if name in SUPPORTED_TOKEN_FILE_ENV_NAMES and os.environ.get(f"{name}_FILE")
    )
    return tuple(sorted(set(names)))


def provider_token_status(lane: Mapping[str, Any]) -> ProviderTokenStatus:
    token_env = tuple(str(item) for item in as_sequence(lane.get("token_env", [])))
    token_env_any = tuple(
        tuple(str(item) for item in as_sequence(group))
        for group in as_sequence(lane.get("token_env_any", []))
    )
    review_hygiene = lane.get("review_hygiene", {})
    if (
        not token_env
        and isinstance(review_hygiene, Mapping)
        and review_hygiene.get("token_env")
    ):
        token_env = (str(review_hygiene["token_env"]),)

    token_file_env = _token_file_env_names(token_env, token_env_any)
    missing = tuple(name for name in token_env if not _token_is_present(name))
    missing_any = tuple(
        group
        for group in token_env_any
        if group and not any(_token_is_present(name) for name in group)
    )
    return ProviderTokenStatus(
        token_env=token_env,
        token_env_any=token_env_any,
        token_file_env=token_file_env,
        missing=missing,
        missing_any=missing_any,
    )


__all__ = ["ProviderTokenStatus", "provider_token_status"]
