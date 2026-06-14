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
    name="antigravity_cli",
    display_name="Antigravity CLI",
    needs_label="needs-antigravity-cli-audit",
    done_label="antigravity-cli-audit-done",
    blocked_label="antigravity-cli-audit-blocked",
    trailer_prefix="ANTIGRAVITY_CLI_AUDIT_STATE",
    default_authors=("antigravity-cli-audit-bot", "antigravity-cli-audit-bot[bot]"),
    authors_env_var="ANTIGRAVITY_CLI_BOT_AUTHORS",
    pass_patterns=(
        re.compile(
            r"Antigravity(?: CLI)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*PASS\b",
            flags=re.IGNORECASE,
        ),
    ),
    blocked_patterns=(
        re.compile(
            r"Antigravity(?: CLI)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("ANTIGRAVITY_CLI_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
