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
from .provider_capabilities import (
    TRANSPORTS,
    devin_lane_transport_name,
    normalize_lane,
    resolve_transport,
)


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
        Participant("devin", "Devin", "devin_cli", "devin", note="transport capabilities and readiness are separate from review authority"),
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
    "devin-cloud": "devin", "devin-api-v3": "devin",
}
# A participant name that also names a transport; rewriting one of these keeps a
# saved selection consistent with an explicit transport choice.
TRANSPORT_ALIAS_NAMES = frozenset({"devin-cli", "devin-cloud", "devin-api-v3"})
TRANSPORT_PARTICIPANT_ALIASES = {
    "devin_cli": "devin-cli",
    "devin_api_v3": "devin-api-v3",
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


def selected_transports(selected: tuple[str, ...]) -> dict[str, str]:
    """Keep explicit transport aliases before normalizing product identities."""
    result: dict[str, str] = {}
    for raw in selected:
        key = raw.strip().lower().replace("_", "-").replace(" ", "-")
        if key in {"devin-cli", "devin-cloud", "devin-api-v3"}:
            transport = resolve_transport(key).transport
            if "devin" in result and result["devin"] != transport:
                raise ConfigError("select one Devin transport per session: devin_cli or devin_api_v3")
            result["devin"] = transport
    return result


def configured_transports(config: Mapping[str, Any], *, profile: str | None = "recommended") -> dict[str, str]:
    """Resolve session transports; None validates defaults without profile inference."""
    defaults = config.get("session_defaults", {})
    if not isinstance(defaults, Mapping):
        raise ConfigError("session_defaults must be a mapping")
    raw = defaults.get("transports", {})
    if not isinstance(raw, Mapping) or set(raw) - {"devin"}:
        raise ConfigError("session_defaults.transports must map devin to devin_cli or devin_api_v3")
    selected = defaults.get("participants", [])
    if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
        raise ConfigError("session_defaults.participants must be a list of names")
    aliases = selected_transports(tuple(selected))
    explicit = {product: resolve_transport(value).transport for product, value in raw.items()}
    if "devin" in aliases and "devin" in explicit and aliases["devin"] != explicit["devin"]:
        raise ConfigError("Devin participant alias conflicts with session_defaults.transports.devin; select one transport")
    inferred = "devin_cli"
    if profile is not None and not aliases and not explicit:
        profiles = config.get("profiles", {})
        active_profile = profiles.get(profile, {}) if isinstance(profiles, Mapping) else {}
        active = active_profile.get("lanes", []) if isinstance(active_profile, Mapping) else []
        if isinstance(active, list) and "devin" in active:
            if "devin_cli" in active:
                raise ConfigError("Both Devin review transports are active; set session_defaults.transports.devin to devin_cli or devin_api_v3")
            inferred = "devin_api_v3"
    return {"devin": inferred, **aliases, **explicit}


def review_lane_for(name: str, transports: Mapping[str, str]) -> str | None:
    if name == "devin":
        return TRANSPORTS[transports[name]].review_lane
    return PARTICIPANTS[name].review_lane


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
    if "devin" in active_lanes:
        selected.add("devin")
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
    if lane.product:
        data["product"] = lane.product
        data["transport"] = lane.transport
    return normalize_lane(lane_id, data)


def config_with_participants(
    config: Mapping[str, Any], selected: tuple[str, ...], *, profile: str = "recommended"
) -> dict[str, Any]:
    result = copy.deepcopy(dict(config))
    profiles = result.get("profiles")
    lanes = result.get("lanes")
    if not isinstance(profiles, dict) or profile not in profiles or not isinstance(lanes, dict):
        raise ConfigError(f"config must contain lanes and profile {profile!r}")
    transports = {**configured_transports(config, profile=profile), **selected_transports(selected)}
    selected = parse_participants(",".join(selected))
    review_lanes = []
    for name in selected:
        lane_id = review_lane_for(name, transports)
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
    if "devin" in selected:
        result["session_defaults"]["transports"] = {"devin": transports["devin"]}
    return result


def _normalized_name(raw: str) -> str:
    return raw.strip().lower().replace("_", "-").replace(" ", "-")


def parse_transport_selection(raw: str) -> tuple[str, str]:
    """Return the `PRODUCT=TRANSPORT` pair a targeted transport switch names."""
    product, _, transport = raw.partition("=")
    product = product.strip()
    transport = transport.strip()
    if not product or not transport:
        raise ConfigError(
            "--set-transport takes PRODUCT=TRANSPORT, for example devin=devin_cli"
        )
    item = resolve_transport(transport)
    if item.product != product:
        raise ConfigError(
            f"transport {transport!r} belongs to product {item.product!r}, not {product!r}"
        )
    return product, item.transport


def config_with_transport(
    config: Mapping[str, Any], product: str, transport: str, *, profile: str = "recommended"
) -> dict[str, Any]:
    """Return the config with only `product`'s transport choice replaced.

    Rewriting the whole participant list to change one transport would drop every
    unrelated participant and profile lane, so this touches nothing but the
    product's own transport, participant alias, and profile lane. A profile whose
    product lanes are custom-named cannot be switched this way without editing
    lanes the repository owns, so it is reported instead of rewritten.
    """
    item = resolve_transport(transport)
    if item.product != product:
        raise ConfigError(
            f"transport {transport!r} belongs to product {item.product!r}, not {product!r}"
        )
    if product != "devin":
        raise ConfigError("only the devin transport can be replaced in place")
    result = copy.deepcopy(dict(config))
    profiles = result.get("profiles")
    lanes = result.get("lanes")
    if not isinstance(profiles, dict) or profile not in profiles or not isinstance(lanes, dict):
        raise ConfigError(f"config must contain lanes and profile {profile!r}")
    active = profiles[profile].get("lanes", [])
    if not isinstance(active, list):
        raise ConfigError(f"profile {profile!r} lanes must be a list")
    canonical = {
        entry.review_lane for entry in TRANSPORTS.values() if entry.product == product
    }
    product_lanes = [
        lane_id
        for lane_id in active
        if devin_lane_transport_name(
            lane_id, lanes[lane_id] if isinstance(lanes.get(lane_id), Mapping) else None
        )
        is not None
    ]
    custom = [lane_id for lane_id in product_lanes if lane_id not in canonical]
    if custom:
        raise ConfigError(
            f"profile {profile!r} selects custom-named {product} lanes ("
            + ", ".join(custom)
            + "); edit them interactively instead of replacing the transport with a "
            "generated command"
        )
    target = TRANSPORTS[transport].review_lane
    if target and target not in lanes:
        lanes[target] = reference_review_config(target)
        builder_lane = PARTICIPANTS[product].builder_lane
        if builder_lane:
            lanes[target]["author_lane"] = builder_lane
    updated: list[str] = []
    for lane_id in active:
        replacement = target if lane_id in product_lanes else lane_id
        if replacement and replacement not in updated:
            updated.append(replacement)
    if target and target not in updated:
        updated.append(target)
    profiles[profile] = {**profiles[profile], "lanes": updated}
    defaults = result.get("session_defaults", {})
    if not isinstance(defaults, Mapping):
        raise ConfigError("session_defaults must be a mapping")
    defaults = dict(defaults)
    alias = TRANSPORT_PARTICIPANT_ALIASES[transport]
    selected = defaults.get("participants")
    names = list(selected) if isinstance(selected, list) else list(DEFAULT_PARTICIPANTS)
    if not all(isinstance(name, str) for name in names):
        raise ConfigError("session_defaults.participants must be a list of names")
    rewritten = [
        alias if _normalized_name(name) in TRANSPORT_ALIAS_NAMES else name
        for name in names
    ]
    if not any(participant_id(name) == product for name in rewritten):
        rewritten.append(alias)
    defaults["participants"] = rewritten
    transports = defaults.get("transports")
    defaults["transports"] = {
        **(transports if isinstance(transports, Mapping) else {}),
        product: transport,
    }
    result["session_defaults"] = defaults
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
