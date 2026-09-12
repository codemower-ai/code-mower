"""Product identity and supported Code Mower transport behavior.

These are integration capabilities, not claims about everything a vendor can
do. They confer neither runtime readiness nor review/merge authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .yaml_subset import ConfigError


CAPABILITY_SCHEMA = "code_mower.providerCapabilities.v1"


@dataclass(frozen=True)
class Capabilities:
    coordinate: str
    build: str
    review: str
    message: str
    cancel: str
    context: str
    structured_results: str


@dataclass(frozen=True)
class ProviderTransport:
    product: str
    transport: str
    driver: str
    review_lane: str
    capabilities: Capabilities

    def declaration(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "transport": self.transport,
            "capabilities": asdict(self.capabilities),
        }

    def brief(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_SCHEMA,
            **self.declaration(),
            "readiness": "unchecked",
            "capability_gaps": [
                name for name, mode in asdict(self.capabilities).items()
                if mode == "unavailable"
            ],
        }


TRANSPORTS = MappingProxyType({
    "devin_cli": ProviderTransport(
        "devin", "devin_cli", "local_cli", "devin_cli",
        Capabilities(
            coordinate="agent_handoff", build="local_runner", review="local_runner",
            message="unavailable", cancel="unavailable", context="unavailable",
            structured_results="local_runner",
        ),
    ),
    "devin_api_v3": ProviderTransport(
        "devin", "devin_api_v3", "hosted_bridge", "devin",
        Capabilities(
            coordinate="unavailable", build="agent_handoff", review="evidence_only",
            message="remote_session", cancel="remote_session", context="agent_handoff",
            structured_results="remote_session",
        ),
    ),
})

# Earlier maintained declarations, still accepted and migrated in memory to the current ones.
_HOSTED_PRE_REMOTE_SESSION = {
    **asdict(TRANSPORTS["devin_api_v3"].capabilities),
    "message": "unavailable",
    "cancel": "unavailable",
    "structured_results": "campaign_only",
}
LEGACY_CAPABILITIES = MappingProxyType({
    "devin_api_v3": (
        _HOSTED_PRE_REMOTE_SESSION,
        {**_HOSTED_PRE_REMOTE_SESSION, "context": "unavailable"},
    ),
})

TRANSPORT_ALIASES = MappingProxyType({
    "devin": "devin_api_v3",  # legacy hosted lane/provider, not the product default
    "devin_cloud": "devin_api_v3",
    "devin_api_v3": "devin_api_v3",
    "devin_cli": "devin_cli",
})


def resolve_transport(value: Any) -> ProviderTransport:
    if isinstance(value, str) and len(value) <= 64:
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        canonical = TRANSPORT_ALIASES.get(key)
        if canonical:
            return TRANSPORTS[canonical]
    raise ConfigError("Devin transport must be devin_cli or devin_api_v3 (legacy devin/devin_cloud means hosted)")


def lane_transport(lane_id: str, lane: Mapping[str, Any]) -> ProviderTransport | None:
    """Infer legacy lane identities, rejecting contradictory declarations."""
    provider = lane.get("provider")
    product = lane.get("product")
    raw_transport = lane.get("transport")
    is_devin = product == "devin" or any(
        isinstance(value, str) and value in TRANSPORT_ALIASES
        for value in (lane_id, provider, raw_transport)
    )
    if not is_devin:
        return None
    if product is not None and product != "devin":
        raise ConfigError("Devin lanes require product: devin; keep provider as the execution identity")
    if not isinstance(provider, str) or provider not in TRANSPORT_ALIASES:
        raise ConfigError("Devin lanes require provider: devin_cli for local execution or provider: devin for hosted execution")
    identity = provider if isinstance(provider, str) and provider in TRANSPORT_ALIASES else lane_id
    inferred = resolve_transport(identity) if identity in TRANSPORT_ALIASES else None
    transport = resolve_transport(raw_transport) if raw_transport is not None else inferred
    if transport is None:
        raise ConfigError("Devin lanes require transport: devin_cli or devin_api_v3")
    if inferred and transport != inferred:
        raise ConfigError("Devin provider and transport disagree; use devin_cli/local_cli or devin/hosted_bridge with devin_api_v3")
    if lane_id in TRANSPORT_ALIASES and resolve_transport(lane_id) != transport:
        raise ConfigError("Keep existing Devin lane identities: devin_cli is local; devin is hosted. Select a transport without repurposing its lane.")
    if lane.get("driver") != transport.driver:
        raise ConfigError("Devin transport and driver disagree; devin_cli requires local_cli; devin_api_v3 requires hosted_bridge")
    if "capabilities" in lane and lane["capabilities"] != asdict(transport.capabilities) \
            and lane["capabilities"] not in LEGACY_CAPABILITIES.get(transport.transport, ()):
        raise ConfigError("Devin capabilities must match the declared transport; remove capabilities to use maintained defaults")
    provider_config = lane.get("provider_config", {})
    if isinstance(provider_config, Mapping) and "campaign_transport" in provider_config:
        if resolve_transport(provider_config["campaign_transport"]) != transport:
            raise ConfigError("Devin campaign_transport must match the lane transport; do not substitute hosted execution for a local lane")
    for key in ("merge_authority", "informational"):
        if key in lane and not isinstance(lane[key], bool):
            raise ConfigError("Devin merge_authority and informational must be true or false")
    if lane.get("merge_authority") and (product is None or raw_transport is None):
        raise ConfigError(
            "Legacy Devin review authority is uncalibrated: set merge_authority: false and "
            "informational: true. Only retain promotion after independent calibration and "
            "explicit product: devin plus transport: devin_cli or devin_api_v3."
        )
    return transport


def normalize_lane(lane_id: str, lane: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(lane)
    transport = lane_transport(lane_id, lane)
    if transport:
        result.update(transport.declaration())
        result.setdefault("merge_authority", False)
        result.setdefault("informational", not result["merge_authority"])
    return result


def normalize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate unambiguous legacy declarations in memory, without file writes."""
    result = dict(config)
    lanes = config.get("lanes")
    if isinstance(lanes, Mapping):
        result["lanes"] = {
            lane_id: normalize_lane(lane_id, lane) if isinstance(lane, Mapping) else lane
            for lane_id, lane in lanes.items()
        }
    return result
