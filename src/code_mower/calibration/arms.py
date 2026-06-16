"""Built-in calibration arm catalog and command-lane defaults."""

from __future__ import annotations

from typing import Any


DEFAULT_LOCAL_LLM_PROFILES = (
    "qwen3-coder-next-lmstudio",
    "gemma4-ollama",
)
DEFAULT_CLI_LANES = ("gemini_cli", "antigravity_cli", "hermes_cli", "coderabbit_cli")


def default_arms() -> list[dict[str, Any]]:
    return [
        {
            "arm_id": "topology-baseline",
            "kind": "cross-provider",
            "reviewers": [
                {"reviewer_id": "codex-audit", "provider": "codex", "lenses": ["base-audit"]},
                {"reviewer_id": "claude-audit", "provider": "claude", "lenses": ["base-audit"]},
            ],
            "purpose": "Measure decorrelation from different model families.",
        },
        {
            "arm_id": "same-provider-lenses",
            "kind": "lens-shift",
            "reviewers": [
                {"reviewer_id": "claude-base-audit", "provider": "claude", "lenses": ["base-audit"]},
                {
                    "reviewer_id": "claude-package-runtime",
                    "provider": "claude",
                    "lenses": ["base-audit", "package-runtime"],
                },
            ],
            "purpose": "Measure whether prompt lenses create useful disagreement.",
        },
        {
            "arm_id": "same-provider-doctrine-lenses",
            "kind": "lens-shift",
            "reviewers": [
                {"reviewer_id": "claude-base-audit", "provider": "claude", "lenses": ["base-audit"]},
                {
                    "reviewer_id": "claude-generic-programming",
                    "provider": "claude",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "claude-context-driven-quality",
                    "provider": "claude",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Measure whether the same model with materially different review "
                "doctrine creates useful disagreement beyond the same-model noise floor."
            ),
        },
        {
            "arm_id": "same-provider-risk-ops-lenses",
            "kind": "lens-shift",
            "reviewers": [
                {"reviewer_id": "claude-base-audit", "provider": "claude", "lenses": ["base-audit"]},
                {
                    "reviewer_id": "claude-security-threat-model",
                    "provider": "claude",
                    "lenses": ["base-audit", "security-threat-model"],
                },
                {
                    "reviewer_id": "claude-operability",
                    "provider": "claude",
                    "lenses": ["base-audit", "operability"],
                },
            ],
            "purpose": (
                "Measure whether security and operability lenses catch useful "
                "production-risk findings beyond the base audit and same-model noise floor."
            ),
        },
        {
            "arm_id": "gemini-risk-ops-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "gemini-base-audit",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "gemini-security-threat-model",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "security-threat-model"],
                },
                {
                    "reviewer_id": "gemini-operability",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "operability"],
                },
            ],
            "purpose": (
                "Execute Gemini CLI against the same head with base, security, "
                "and operability lenses so lens evidence can be collected without "
                "manual command fan-out."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "gemini-doctrine-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "gemini-base-audit",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "gemini-generic-programming",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "gemini-context-driven-quality",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Execute Gemini CLI against the same head with base, generic "
                "programming, and context-driven quality lenses so same-model "
                "doctrine shifts can be measured with real lane outputs."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "antigravity-risk-ops-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "antigravity-base-audit",
                    "provider": "local-cli",
                    "lane_id": "antigravity_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "antigravity-security-threat-model",
                    "provider": "local-cli",
                    "lane_id": "antigravity_cli",
                    "lenses": ["base-audit", "security-threat-model"],
                },
                {
                    "reviewer_id": "antigravity-operability",
                    "provider": "local-cli",
                    "lane_id": "antigravity_cli",
                    "lenses": ["base-audit", "operability"],
                },
            ],
            "purpose": (
                "Execute Antigravity CLI against the same head with base, "
                "security, and operability lenses so the forward Google CLI "
                "lane can be calibrated separately from Gemini compatibility."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "antigravity-doctrine-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "antigravity-base-audit",
                    "provider": "local-cli",
                    "lane_id": "antigravity_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "antigravity-generic-programming",
                    "provider": "local-cli",
                    "lane_id": "antigravity_cli",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "antigravity-context-driven-quality",
                    "provider": "local-cli",
                    "lane_id": "antigravity_cli",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Execute Antigravity CLI against the same head with base, "
                "generic programming, and context-driven quality lenses so "
                "same-provider doctrine shifts can be measured on the forward "
                "Google CLI lane."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "hermes-doctrine-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "hermes-base-audit",
                    "provider": "local-cli",
                    "lane_id": "hermes_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "hermes-generic-programming",
                    "provider": "local-cli",
                    "lane_id": "hermes_cli",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "hermes-context-driven-quality",
                    "provider": "local-cli",
                    "lane_id": "hermes_cli",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Execute Hermes CLI against the same head with base, generic "
                "programming, and context-driven quality lenses so its one-shot "
                "agent profile can be compared with Gemini/Claude doctrine "
                "evidence without granting merge authority."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "same-provider-control",
            "kind": "noise-floor",
            "reviewers": [
                {"reviewer_id": "claude-base-audit-a", "provider": "claude", "lenses": ["base-audit"]},
                {"reviewer_id": "claude-base-audit-b", "provider": "claude", "lenses": ["base-audit"]},
            ],
            "purpose": "Measure repeated-run variance for the same model and same lens.",
        },
        {
            "arm_id": "local-cli-models",
            "kind": "informational-bakeoff",
            "reviewers": [
                *(
                    {
                        "reviewer_id": profile_id,
                        "provider": "local-llm",
                        "profile_id": profile_id,
                        "lenses": ["base-audit"],
                    }
                    for profile_id in DEFAULT_LOCAL_LLM_PROFILES
                ),
                *(
                    {
                        "reviewer_id": lane_id,
                        "provider": "local-cli",
                        "lane_id": lane_id,
                        "lenses": ["base-audit"],
                    }
                    for lane_id in DEFAULT_CLI_LANES
                ),
            ],
            "purpose": "Compare cheap informational reviewers before promotion.",
        },
    ]
