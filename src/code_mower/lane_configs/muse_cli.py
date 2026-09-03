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
    name="muse_cli",
    display_name="Muse CLI",
    needs_label="needs-muse-audit",
    done_label="muse-audit-done",
    blocked_label="muse-audit-blocked",
    trailer_prefix="MUSE_AUDIT_STATE",
    default_authors=("muse-cli-audit-bot", "muse-cli-audit-bot[bot]"),
    authors_env_var="MUSE_CLI_BOT_AUTHORS",
    pass_patterns=(
        re.compile(
            r"Muse(?: CLI)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*PASS\b",
            flags=re.IGNORECASE,
        ),
    ),
    blocked_patterns=(
        re.compile(
            r"Muse(?: CLI)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("MUSE_CLI_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
