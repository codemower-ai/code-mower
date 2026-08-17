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
    name="grok_build",
    display_name="Grok Build",
    needs_label="needs-grok-audit",
    done_label="grok-audit-done",
    blocked_label="grok-audit-blocked",
    trailer_prefix="GROK_AUDIT_STATE",
    default_authors=("grok-build-audit-bot", "grok-build-audit-bot[bot]"),
    authors_env_var="GROK_BUILD_BOT_AUTHORS",
    pass_patterns=(
        re.compile(
            r"Grok(?: Build)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*PASS\b",
            flags=re.IGNORECASE,
        ),
    ),
    blocked_patterns=(
        re.compile(
            r"Grok(?: Build)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("GROK_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
