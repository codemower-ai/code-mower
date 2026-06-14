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
    name="aider",
    display_name="Aider",
    needs_label="needs-aider-audit",
    done_label="aider-audit-done",
    blocked_label="aider-audit-blocked",
    trailer_prefix="AIDER_AUDIT_STATE",
    default_authors=("aider-audit-bot", "aider-audit-bot[bot]"),
    authors_env_var="AIDER_BOT_AUTHORS",
    pass_patterns=(
        re.compile(r"Aider Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*PASS\b", flags=re.IGNORECASE),
    ),
    blocked_patterns=(
        re.compile(
            r"Aider Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("AIDER_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
