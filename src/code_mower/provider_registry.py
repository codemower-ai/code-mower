"""Reference Code Mower provider registry.

This module is intentionally declarative. It documents the provider/lane shape
that a future `code-mower.yml` parser should produce without making the shared
labelers know provider-specific semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

try:
    from . import local_llm_profiles
except ImportError:  # pragma: no cover - direct repo execution fallback.
    from tools import local_llm_profiles


@dataclass(frozen=True)
class LaneLabels:
    needs: str
    done: str
    blocked: str


@dataclass(frozen=True)
class ProviderLane:
    lane_id: str
    lane_type: str
    driver: str
    provider: str
    labels: LaneLabels
    adapter: str | None = None
    events: tuple[str, ...] = ()
    token_env: tuple[str, ...] = ()
    token_env_any: tuple[tuple[str, ...], ...] = ()
    result_sources: tuple[str, ...] = ()
    informational: bool = False
    merge_authority: bool = False
    enabled_by_default: bool = True
    trigger_policy: str = "label"
    spend_policy: str = "none"
    provider_config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_config", _freeze_mapping(self.provider_config))

    @property
    def is_paid_optional(self) -> bool:
        return (
            self.spend_policy == "paid"
            and not self.enabled_by_default
            and self.trigger_policy == "manual"
        )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


REFERENCE_PROVIDERS: dict[str, ProviderLane] = {
    "codex": ProviderLane(
        lane_id="codex",
        lane_type="audit",
        driver="local_cli",
        provider="codex",
        labels=LaneLabels(
            needs="needs-codex-audit",
            done="codex-audit-done",
            blocked="codex-audit-blocked",
        ),
        token_env=("CODEX_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("trailer_comment",),
        merge_authority=True,
    ),
    "claude_review": ProviderLane(
        lane_id="claude_review",
        lane_type="review",
        driver="manual",
        provider="claude",
        labels=LaneLabels(
            needs="needs-claude-review",
            done="claude-review-done",
            blocked="claude-review-blocked",
        ),
        result_sources=("post_review",),
        informational=True,
        merge_authority=False,
        trigger_policy="manual",
    ),
    "claude_audit": ProviderLane(
        lane_id="claude_audit",
        lane_type="audit",
        driver="local_cli",
        provider="claude",
        labels=LaneLabels(
            needs="needs-claude-audit",
            done="claude-audit-done",
            blocked="claude-audit-blocked",
        ),
        token_env=("CLAUDE_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("trailer_comment",),
        merge_authority=True,
    ),
    "devin": ProviderLane(
        lane_id="devin",
        lane_type="audit",
        driver="hosted_bridge",
        provider="devin",
        labels=LaneLabels(
            needs="needs-devin-audit",
            done="devin-audit-done",
            blocked="devin-audit-blocked",
        ),
        token_env=("DEVIN_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("trailer_comment",),
        merge_authority=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="paid",
    ),
    "greptile": ProviderLane(
        lane_id="greptile",
        lane_type="audit",
        driver="saas_event",
        provider="greptile",
        adapter="greptile",
        events=("pull_request_review", "check_run"),
        labels=LaneLabels(
            needs="needs-greptile-audit",
            done="greptile-audit-done",
            blocked="greptile-audit-blocked",
        ),
        token_env=("GREPTILE_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("pull_request_review", "check_run"),
        informational=True,
        provider_config={
            "bot_authors": ("greptile-apps[bot]", "greptile-apps"),
            "check_runs": ({"app_slug": "greptile-apps", "name": "Greptile Review"},),
        },
    ),
    "gitar": ProviderLane(
        lane_id="gitar",
        lane_type="audit",
        driver="saas_event",
        provider="gitar",
        adapter="gitar",
        events=("issue_comment",),
        labels=LaneLabels(
            needs="needs-gitar-audit",
            done="gitar-audit-done",
            blocked="gitar-audit-blocked",
        ),
        token_env=("GITAR_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("issue_comment",),
        informational=True,
        provider_config={
            "bot_authors": ("gitar-ai[bot]", "gitar-bot", "gitar-bot[bot]"),
        },
    ),
    "qodo": ProviderLane(
        lane_id="qodo",
        lane_type="audit",
        driver="saas_event",
        provider="qodo",
        adapter="qodo",
        events=("issue_comment",),
        labels=LaneLabels(
            needs="needs-qodo-audit",
            done="qodo-audit-done",
            blocked="qodo-audit-blocked",
        ),
        token_env=("QODO_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("issue_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="paid",
        provider_config={
            "bot_authors": ("qodo-code-review[bot]", "qodo-code-review"),
            "spend_policy": "never trigger automatically from the reference workflows",
        },
    ),
    "cursor_bugbot": ProviderLane(
        lane_id="cursor_bugbot",
        lane_type="audit",
        driver="saas_event",
        provider="cursor_bugbot",
        adapter="cursor_bugbot",
        events=("issue_comment",),
        labels=LaneLabels(
            needs="needs-cursor-bugbot-audit",
            done="cursor-bugbot-audit-done",
            blocked="cursor-bugbot-audit-blocked",
        ),
        token_env=("CURSOR_BUGBOT_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("issue_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="paid",
        provider_config={
            "bot_authors": ("cursor[bot]", "cursor"),
            "trigger_comments": ("bugbot run", "@cursor review"),
            "rules_file": ".cursor/BUGBOT.md",
            "status": (
                "manual informational lane; keep calibration-only until enabled "
                "BugBot output shape is captured and adjudicated"
            ),
        },
    ),
    "local_llm": ProviderLane(
        lane_id="local_llm",
        lane_type="audit",
        driver="api_model",
        provider="openai_compatible",
        labels=LaneLabels(
            needs="needs-local-llm-audit",
            done="local-llm-audit-done",
            blocked="local-llm-audit-blocked",
        ),
        token_env=("LOCAL_LLM_AUDIT_LABEL_TOKEN", "LOCAL_LLM_API_KEY", "GITHUB_TOKEN"),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="local",
        provider_config={
            "api_base_env": "LOCAL_LLM_API_BASE",
            "model_env": "LOCAL_LLM_MODEL",
            "bot_authors_env": "LOCAL_LLM_BOT_AUTHORS",
            "default_api_base": "http://localhost:1234/v1",
            "profile_env": "LOCAL_LLM_PROFILE",
            # Keep provider metadata synced with the canonical profile registry.
            "profiles": local_llm_profiles.profile_ids(),
            "status": "informational calibration lane, not merge authority",
        },
    ),
    "aider": ProviderLane(
        lane_id="aider",
        lane_type="audit",
        driver="local_cli",
        provider="aider",
        labels=LaneLabels(
            needs="needs-aider-audit",
            done="aider-audit-done",
            blocked="aider-audit-blocked",
        ),
        token_env=("AIDER_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="local",
        provider_config={
            "command": "aider",
            "mode": "review-only",
            "model_env": "AIDER_MODEL",
            "api_base_env": "AIDER_API_BASE",
            "status": "optional tool lane; provider-specific invocation belongs in an adapter",
        },
    ),
    "gemini_cli": ProviderLane(
        lane_id="gemini_cli",
        lane_type="audit",
        driver="local_cli",
        provider="gemini",
        labels=LaneLabels(
            needs="needs-gemini-cli-audit",
            done="gemini-cli-audit-done",
            blocked="gemini-cli-audit-blocked",
        ),
        token_env=("GITHUB_TOKEN",),
        token_env_any=(("GEMINI_API_KEY", "GOOGLE_API_KEY"),),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="included",
        provider_config={
            "command": "gemini",
            "command_env": "GEMINI_CLI_COMMAND",
            "model_env": "GEMINI_MODEL",
            "prompt_lenses": ("base-audit",),
            "status": "optional third-peer CLI lane; not merge authority until calibrated",
        },
    ),
    "antigravity_cli": ProviderLane(
        lane_id="antigravity_cli",
        lane_type="audit",
        driver="local_cli",
        provider="antigravity",
        labels=LaneLabels(
            needs="needs-antigravity-cli-audit",
            done="antigravity-cli-audit-done",
            blocked="antigravity-cli-audit-blocked",
        ),
        token_env=("GITHUB_TOKEN",),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="included",
        provider_config={
            "command": "agy",
            "alternate_commands": ("antigravity",),
            "command_env": "ANTIGRAVITY_CLI_COMMAND",
            "model_env": "ANTIGRAVITY_MODEL",
            "prompt_lenses": ("base-audit",),
            "required_env_truthy": ("ANTIGRAVITY_CLI_USE_AMBIENT_HOME",),
            "auth": (
                "run `agy` login/install locally; set "
                "ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1 to opt into local OAuth "
                "state in trusted environments"
            ),
            "status": (
                "forward Google CLI research lane; not merge authority until "
                "calibrated against Gemini compatibility records"
            ),
        },
    ),
    "hermes_cli": ProviderLane(
        lane_id="hermes_cli",
        lane_type="audit",
        driver="local_cli",
        provider="hermes",
        labels=LaneLabels(
            needs="needs-hermes-cli-audit",
            done="hermes-cli-audit-done",
            blocked="hermes-cli-audit-blocked",
        ),
        token_env=("GITHUB_TOKEN",),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="included",
        provider_config={
            "command": "hermes",
            "command_env": "HERMES_CLI_COMMAND",
            "model_env": "HERMES_INFERENCE_MODEL",
            "provider_env": "HERMES_PROVIDER",
            "prompt_lenses": ("base-audit",),
            "required_env_truthy": ("HERMES_CLI_USE_AMBIENT_HOME",),
            "doctor_probe_args": ("--version",),
            "auth": (
                "run `hermes setup` or provider login locally; set "
                "HERMES_CLI_USE_AMBIENT_HOME=1 to opt into local Hermes "
                "auth/session state in trusted environments"
            ),
            "status": (
                "informational Hermes Agent one-shot lane; not merge authority "
                "until calibrated and kept stateless enough for blind review"
            ),
        },
    ),
    "coderabbit_cli": ProviderLane(
        lane_id="coderabbit_cli",
        lane_type="audit",
        driver="local_cli",
        provider="coderabbit",
        labels=LaneLabels(
            needs="needs-coderabbit-cli-audit",
            done="coderabbit-cli-audit-done",
            blocked="coderabbit-cli-audit-blocked",
        ),
        token_env=("GITHUB_TOKEN",),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="included",
        provider_config={
            "command": "coderabbit",
            "alternate_commands": ("cr",),
            "status": "optional CLI lane for local/on-demand review capture",
        },
    ),
    "acp_bridge": ProviderLane(
        lane_id="acp_bridge",
        lane_type="audit",
        driver="local_cli",
        provider="acp",
        labels=LaneLabels(
            needs="needs-acp-audit",
            done="acp-audit-done",
            blocked="acp-audit-blocked",
        ),
        token_env=("GITHUB_TOKEN",),
        result_sources=("trailer_comment",),
        informational=True,
        enabled_by_default=False,
        trigger_policy="manual",
        spend_policy="none",
        provider_config={
            "command_env": "CODE_MOWER_ACP_COMMAND",
            "protocol": "agent-client-protocol",
            "prompt_lenses": ("base-audit",),
            "status": "protocol research lane; no automatic merge authority",
        },
    ),
}


def get_provider(lane_id: str) -> ProviderLane:
    return REFERENCE_PROVIDERS[lane_id]


def audit_queue_labels() -> tuple[str, ...]:
    return tuple(
        lane.labels.needs
        for lane in REFERENCE_PROVIDERS.values()
        if lane.lane_type in {"audit", "review"}
    )
