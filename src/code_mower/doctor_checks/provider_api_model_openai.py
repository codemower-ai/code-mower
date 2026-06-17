"""OpenAI-compatible provider API-model probes."""

from __future__ import annotations

import json
import urllib.request
from typing import Mapping


def fetch_openai_compatible_models(
    api_base: str,
    api_key: str,
    timeout: int,
) -> list[str]:
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", []) if isinstance(payload, Mapping) else []
    return [
        str(entry.get("id"))
        for entry in data
        if isinstance(entry, Mapping) and entry.get("id")
    ]
