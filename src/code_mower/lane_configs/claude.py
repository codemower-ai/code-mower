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
    name="claude",
    display_name="Claude",
    needs_label="needs-claude-audit",
    done_label="claude-audit-done",
    blocked_label="claude-audit-blocked",
    trailer_prefix="CLAUDE_AUDIT_STATE",
    default_authors=("claude-audit-bot", "claude-audit-bot[bot]"),
    authors_env_var="CLAUDE_AUDIT_BOT_AUTHORS",
    pass_patterns=(
        re.compile(
            r"<!--\s*CLAUDE_AUDIT_STATE:\s*claude-audit-done\s*-->",
            flags=re.IGNORECASE,
        ),
    ),
    blocked_patterns=(
        re.compile(
            r"<!--\s*CLAUDE_AUDIT_STATE:\s*claude-audit-blocked\s*-->",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("CLAUDE_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
