"""Package materializer path and config loading helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools.code_mower_config import ConfigError, load_config
else:  # pragma: no cover - exercised after package extraction.
    from .config import ConfigError, load_config


DEFAULT_PROVIDER_TEMPLATES = "code-mower.provider-templates.yml"


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ConfigError(f"{name} must be a mapping")


def load_provider_templates(path: Path) -> Mapping[str, Any]:
    templates = load_config(path)
    if templates.get("version") not in {1, "1"}:
        raise ConfigError("provider templates version must be 1")
    _as_mapping(templates.get("provider_templates"), "provider_templates")
    profiles = _as_mapping(templates.get("profiles"), "profiles")
    for profile_id, profile in profiles.items():
        if not isinstance(profile, Mapping):
            raise ConfigError(f"provider template profile {profile_id!r} must be a mapping")
    return templates


def resolve_provider_templates_path(path_text: str) -> Path:
    path = Path(path_text)
    if path_text != DEFAULT_PROVIDER_TEMPLATES or path.is_absolute():
        return path

    module_dir = Path(__file__).resolve().parent
    candidates = (
        module_dir.parent / DEFAULT_PROVIDER_TEMPLATES,
        module_dir / "templates" / "providers.yml",
        Path.cwd() / DEFAULT_PROVIDER_TEMPLATES,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
