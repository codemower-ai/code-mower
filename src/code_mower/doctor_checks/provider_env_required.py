"""Provider required environment status helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from .common import TRUTHY_ENV_VALUES, as_sequence


@dataclass(frozen=True)
class ProviderRequiredEnvStatus:
    required: tuple[str, ...]
    required_truthy: tuple[str, ...]
    missing: tuple[str, ...]
    missing_truthy: tuple[str, ...]

    @property
    def declares_required_env(self) -> bool:
        return bool(self.required or self.required_truthy)

    @property
    def all_present(self) -> bool:
        return not self.missing and not self.missing_truthy


def provider_required_env_status(lane: Mapping[str, Any]) -> ProviderRequiredEnvStatus:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return ProviderRequiredEnvStatus((), (), (), ())
    required = tuple(
        str(name)
        for name in as_sequence(provider_config.get("required_env", []))
        if str(name).strip()
    )
    required_truthy = tuple(
        str(name)
        for name in as_sequence(provider_config.get("required_env_truthy", []))
        if str(name).strip()
    )
    missing = tuple(name for name in required if not os.environ.get(name))
    missing_truthy = tuple(
        name
        for name in required_truthy
        if os.environ.get(name, "").strip().lower() not in TRUTHY_ENV_VALUES
    )
    return ProviderRequiredEnvStatus(
        required=required,
        required_truthy=required_truthy,
        missing=missing,
        missing_truthy=missing_truthy,
    )


__all__ = ["ProviderRequiredEnvStatus", "provider_required_env_status"]
