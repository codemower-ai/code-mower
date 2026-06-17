"""GitHub Actions annotation parsing helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .common import ACTIONS_BILLING_BLOCK_PATTERNS


def _annotation_mentions_actions_billing_block(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in ACTIONS_BILLING_BLOCK_PATTERNS)


def _check_run_id_from_actions_job(job: Mapping[str, Any]) -> Any | None:
    check_run_url = str(job.get("check_run_url") or "")
    match = re.search(r"/check-runs/([0-9]+)$", check_run_url)
    if match:
        return match.group(1)
    return None


__all__ = (
    "_annotation_mentions_actions_billing_block",
    "_check_run_id_from_actions_job",
)
