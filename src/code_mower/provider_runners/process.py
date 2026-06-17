"""Shared subprocess environment helpers for provider-runner wrappers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping


DEFAULT_HOME_ENV_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


def _stringify_env_value(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def build_allowlisted_child_env(
    allowlist: Iterable[str],
    *,
    exclude_env: Iterable[str] = (),
    extra_env: Mapping[str, object | None] | None = None,
    home_env: Mapping[str, object] | None = None,
    preserve_ambient_home: bool = False,
    ambient_home_keys: Iterable[str] = DEFAULT_HOME_ENV_KEYS,
) -> dict[str, str]:
    """Build a minimal child environment for provider subprocesses.

    ``allowlist`` controls which ambient variables are copied. ``exclude_env``
    lets a caller suppress allowlisted values for negative-path tests. Provider
    wrappers can pass ``home_env`` for deterministic isolated HOME/XDG paths, or
    set ``preserve_ambient_home`` to copy selected home/config variables from
    the ambient process instead. ``extra_env`` is applied last and ignores
    ``None`` values, so explicit provider keys can override ambient values.
    """

    excluded = set(exclude_env)
    child_env = {
        key: value
        for key in allowlist
        if key not in excluded and (value := os.environ.get(key))
    }
    if preserve_ambient_home:
        for key in ambient_home_keys:
            if key not in excluded and (value := os.environ.get(key)):
                child_env[key] = value
    elif home_env:
        for key, value in home_env.items():
            if key not in excluded:
                child_env[key] = _stringify_env_value(value)

    if extra_env:
        for key, value in extra_env.items():
            if value is not None:
                child_env[key] = _stringify_env_value(value)

    return child_env
