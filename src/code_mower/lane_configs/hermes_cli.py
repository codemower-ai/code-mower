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
    name="hermes_cli",
    display_name="Hermes CLI",
    needs_label="needs-hermes-cli-audit",
    done_label="hermes-cli-audit-done",
    blocked_label="hermes-cli-audit-blocked",
    trailer_prefix="HERMES_CLI_AUDIT_STATE",
    default_authors=("hermes-cli-audit-bot", "hermes-cli-audit-bot[bot]"),
    authors_env_var="HERMES_CLI_BOT_AUTHORS",
    pass_patterns=(
        re.compile(
            r"Hermes(?: CLI)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*PASS\b",
            flags=re.IGNORECASE,
        ),
    ),
    blocked_patterns=(
        re.compile(
            r"Hermes(?: CLI)? Audit(?:\s+Result)?\s*[\u2014\u2013:-]\s*(BLOCKED|BLOCKER|INCOMPLETE)\b",
            flags=re.IGNORECASE,
        ),
    ),
    token_env_vars=("HERMES_CLI_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
)
