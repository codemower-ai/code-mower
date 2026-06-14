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
    name="codex",
    display_name="Codex",
    needs_label="needs-codex-audit",
    done_label="codex-audit-done",
    blocked_label="codex-audit-blocked",
    trailer_prefix="CODEX_AUDIT_STATE",
    default_authors=("codex-audit-bot", "codex-audit-bot[bot]"),
    authors_env_var="CODEX_BOT_AUTHORS",
    pass_patterns=(
        re.compile(r"Codex Audit(?:\s+Result)?\s*[—–:-]\s*PASS\b", flags=re.IGNORECASE),
    ),
    blocked_patterns=(
        re.compile(
            r"Codex Audit(?:\s+Result)?\s*[—–:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("CODEX_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
