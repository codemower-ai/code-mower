"""Shared participant selection for setup and agent-coordinated sessions.

Product identities belong here; reviewer execution and policy remain in the
provider registry and repository config. Selecting a product does not promote it.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from .config import ConfigError
from .provider_registry import REFERENCE_PROVIDERS


@dataclass(frozen=True)
class Participant:
    id: str
    name: str
    review_lane: str | None = None
    builder_lane: str | None = None
    orchestrator: bool = True
    builder: bool = True
    note: str = ""


DEFAULT_PARTICIPANTS = ("claude", "codex")
PARTICIPANTS = {
    item.id: item
    for item in (
        Participant("claude", "Claude Code", "claude_audit", "claude"),
        Participant("codex", "Codex", "codex", "codex"),
        Participant("devin", "Devin", "devin_cli", "devin", note="local CLI review; cloud execution requires its own setup"),
        Participant("cursor", "Cursor", builder_lane="cursor", note="Bugbot is a separate optional reviewer"),
        Participant("grok-bot", "Grok Bot", note="agent handoff; no dedicated automatic builder/reviewer transport"),
        Participant("antigravity", "Antigravity", "antigravity_cli", note="CLI review; building uses an agent handoff"),
        Participant("muse", "Muse", "muse_cli", note="CLI review; building uses an agent handoff"),
        Participant("gitar", "Gitar", "gitar", orchestrator=False, builder=False, note="optional SaaS reviewer"),
        Participant("cursor-bugbot", "Cursor Bugbot", "cursor_bugbot", orchestrator=False, builder=False, note="optional SaaS reviewer"),
        Participant("qodo", "Qodo", "qodo", orchestrator=False, builder=False, note="optional SaaS reviewer"),
        Participant("greptile", "Greptile", "greptile", orchestrator=False, builder=False, note="optional SaaS reviewer"),
    )
}
ALIASES = {
    "claude-code": "claude", "claude-audit": "claude",
    "devin-cli": "devin", "antigravity-cli": "antigravity", "muse-cli": "muse",
}


def participant_id(raw: str) -> str:
    normalized = raw.strip().lower().replace("_", "-").replace(" ", "-")
    normalized = ALIASES.get(normalized, normalized)
    if normalized not in PARTICIPANTS:
        raise ConfigError(f"unknown participant {raw!r}; choose from: {', '.join(PARTICIPANTS)}")
    return normalized


def parse_participants(raw: str) -> tuple[str, ...]:
    if not raw.strip() or any(not item.strip() for item in raw.split(",")):
        raise ConfigError("--with requires a comma-separated list of participants")
    return tuple(dict.fromkeys(participant_id(item) for item in raw.split(",")))


def configured_participants(config: Mapping[str, Any]) -> tuple[str, ...]:
    defaults = config.get("session_defaults", {})
    if not isinstance(defaults, Mapping):
        raise ConfigError("session_defaults must be a mapping")
    selected = defaults.get("participants")
    if selected is None:
        return DEFAULT_PARTICIPANTS
    if not isinstance(selected, list) or not selected or not all(isinstance(x, str) for x in selected):
        raise ConfigError("session_defaults.participants must be a nonempty list of names")
    return parse_participants(",".join(selected))


def picker_initial_participants(config: Mapping[str, Any], *, profile: str) -> tuple[str, ...]:
    """Include active known reviewers when editing an existing setup."""
    selected = set(configured_participants(config)) if "session_defaults" in config else set()
    profiles = config.get("profiles", {})
    if not isinstance(profiles, Mapping) or not isinstance(profiles.get(profile), Mapping):
        raise ConfigError(f"unknown or invalid profile {profile!r}")
    active_lanes = profiles[profile].get("lanes", [])
    if not isinstance(active_lanes, list):
        raise ConfigError(f"profile {profile!r} lanes must be a list")
    selected.update(name for name, item in PARTICIPANTS.items() if item.review_lane in active_lanes)
    return tuple(name for name in PARTICIPANTS if name in selected)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def reference_review_config(lane_id: str) -> dict[str, Any]:
    lane = REFERENCE_PROVIDERS[lane_id]
    data = {
        "type": lane.lane_type, "driver": lane.driver, "provider": lane.provider,
        "labels": {"needs": lane.labels.needs, "done": lane.labels.done, "blocked": lane.labels.blocked},
        "merge_authority": lane.merge_authority, "informational": lane.informational,
        "enabled_by_default": lane.enabled_by_default, "trigger_policy": lane.trigger_policy,
        "spend_policy": lane.spend_policy, "token_env": list(lane.token_env),
        "provider_config": _plain(lane.provider_config),
    }
    if lane.token_env_any:
        data["token_env_any"] = _plain(lane.token_env_any)
    if lane.adapter:
        data["adapter"] = lane.adapter
    if lane.events:
        data["events"] = list(lane.events)
    return data


def config_with_participants(
    config: Mapping[str, Any], selected: tuple[str, ...], *, profile: str = "recommended"
) -> dict[str, Any]:
    result = copy.deepcopy(dict(config))
    profiles = result.get("profiles")
    lanes = result.get("lanes")
    if not isinstance(profiles, dict) or profile not in profiles or not isinstance(lanes, dict):
        raise ConfigError(f"config must contain lanes and profile {profile!r}")
    selected = parse_participants(",".join(selected))
    review_lanes = []
    for name in selected:
        lane_id = PARTICIPANTS[name].review_lane
        if lane_id:
            if lane_id not in lanes:
                lanes[lane_id] = reference_review_config(lane_id)
                if PARTICIPANTS[name].builder_lane:
                    lanes[lane_id]["author_lane"] = PARTICIPANTS[name].builder_lane
            review_lanes.append(lane_id)
    profiles[profile] = {
        **profiles[profile],
        "description": "Selected participants: " + ", ".join(PARTICIPANTS[x].name for x in selected),
        "lanes": review_lanes,
    }
    defaults = result.get("session_defaults", {})
    if not isinstance(defaults, Mapping):
        raise ConfigError("session_defaults must be a mapping")
    result["session_defaults"] = {**defaults, "participants": list(selected)}
    return result


def pick_participants(initial: tuple[str, ...]) -> tuple[str, ...]:
    """Portable checkbox menu, including terminals without cursor-key support."""
    if not sys.stdin.isatty():
        raise ConfigError("--interactive needs a terminal; use --with claude,codex,devin in agents or scripts")
    choices = list(PARTICIPANTS)
    selected = set(initial)
    while True:
        print("\nChoose Code Mower participants", file=sys.stderr)
        for index, name in enumerate(choices, 1):
            item = PARTICIPANTS[name]
            suffix = f" — {item.note}" if item.note else ""
            print(f"  {index:2}. [{'x' if name in selected else ' '}] {item.name}{suffix}", file=sys.stderr)
        print("Toggle numbers (for example 3,4); Enter accepts; q cancels: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ConfigError("participant selection cancelled") from exc
        if answer.lower() == "q":
            raise ConfigError("participant selection cancelled")
        if not answer:
            if selected:
                return tuple(name for name in choices if name in selected)
            print("Select at least one participant.", file=sys.stderr)
            continue
        try:
            indices = {int(value) for value in answer.replace(",", " ").split()}
            if not indices or not all(1 <= index <= len(choices) for index in indices):
                raise ValueError
        except ValueError:
            print("Enter listed numbers, Enter, or q.", file=sys.stderr)
            continue
        selected.symmetric_difference_update(choices[index - 1] for index in indices)
