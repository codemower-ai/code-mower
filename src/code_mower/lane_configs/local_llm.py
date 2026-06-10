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
    name="local_llm",
    display_name="Local LLM",
    needs_label="needs-local-llm-audit",
    done_label="local-llm-audit-done",
    blocked_label="local-llm-audit-blocked",
    trailer_prefix="LOCAL_LLM_AUDIT_STATE",
    default_authors=("local-llm-audit-bot", "local-llm-audit-bot[bot]"),
    authors_env_var="LOCAL_LLM_BOT_AUTHORS",
    pass_patterns=(
        re.compile(r"Local LLM Audit(?:\s+Result)?\s*[—–:-]\s*PASS\b", flags=re.IGNORECASE),
    ),
    blocked_patterns=(
        re.compile(
            r"Local LLM Audit(?:\s+Result)?\s*[—–:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
)
