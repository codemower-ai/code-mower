from __future__ import annotations

import re

if __package__ and __package__.startswith("code_mower."):
    from ..audit_labeler_lib import LaneConfig
else:
    try:
        from tools.audit_labeler_lib import LaneConfig
    except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
        from audit_labeler_lib import LaneConfig

CONFIG = LaneConfig(
    name="devin",
    display_name="Devin",
    needs_label="needs-devin-audit",
    done_label="devin-audit-done",
    blocked_label="devin-audit-blocked",
    trailer_prefix="DEVIN_AUDIT_STATE",
    default_authors=("devin-ai-integration", "devin-ai-integration[bot]"),
    authors_env_var="DEVIN_BOT_AUTHORS",
    pass_patterns=(
        re.compile(r"Devin Audit(?:\s+Result)?\s*[—–:-]\s*PASS\b", flags=re.IGNORECASE),
    ),
    blocked_patterns=(
        re.compile(
            r"Devin Audit(?:\s+Result)?\s*[—–:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    label_state_fallbacks=True,
    token_env_vars=("DEVIN_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
