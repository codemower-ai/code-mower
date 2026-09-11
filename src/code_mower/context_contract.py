"""Optional context contracts, with no provider SDK or ambient authentication.

Packet files and connection envelopes are private. Only ``shareable_summary``
is intended for public status. The authorization callback must be supplied by
trusted runtime code; structurally valid provider JSON is never authorization.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol


POLICY_SCHEMA = "code_mower.contextPolicy.v1"
CONNECTION_SCHEMA = "code_mower.contextConnection.v1"
PACKET_SCHEMA = "code_mower.contextPacket.v1"
EXTERNAL_MANIFEST_SCHEMA = "code_mower.externalContextManifest.v1"
CAPABILITY_VERSION = 1
MAX_PACKET_BYTES = 262_144
MAX_REFERENCES = 16
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_LIMITS = {
    "max_packet_bytes": (MAX_PACKET_BYTES, MAX_PACKET_BYTES),
    "max_documents": (5, 50),
    "max_document_bytes": (20_000, 80_000),
    "max_text_bytes": (80_000, 160_000),
    "max_requests": (3, 20),
    "max_pages": (2, 20),
    "timeout_seconds": (30, 120),
    "max_age_seconds": (3600, 86_400),
}


class ContextError(ValueError):
    """Fixed diagnostics intentionally omit private values and paths."""


def _object(
    value: Any, required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise ContextError("context object has missing or unsupported fields")
    return value


def _text(value: Any, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in value)
    ):
        raise ContextError("context identifier must be bounded single-line text")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContextError("context reference must be a generic lowercase identifier")
    return value


def _integer(value: Any, maximum: int) -> int:
    # The repository's small YAML parser represents integer scalars as strings.
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 8:
        value = int(value)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ContextError("context limit must be a positive bounded integer")
    return value


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, maximum=40).replace("Z", "+00:00"))
    except ValueError:
        raise ContextError("context timestamp must be ISO 8601 with timezone") from None
    if parsed.tzinfo is None:
        raise ContextError("context timestamp must include a timezone")
    return parsed


def _strings(value: Any, *, maximum: int = 32) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ContextError("context scope must be a nonempty bounded list")
    result = tuple(_text(item) for item in value)
    if len(set(result)) != len(result):
        raise ContextError("context scope contains duplicates")
    return result


def normalize_policy(value: Any) -> dict[str, Any] | None:
    """Validate shared policy. Identity, endpoint, and credentials have no keys."""
    if value is None:
        return None
    policy = _object(value, {"schema", "connection", "policy_version", "required"}, set(_LIMITS))
    if policy["schema"] != POLICY_SCHEMA or type(policy["required"]) is not bool:
        raise ContextError("unsupported context policy schema or required flag")
    return {
        "schema": POLICY_SCHEMA,
        "connection": _identifier(policy["connection"]),
        "policy_version": _identifier(policy["policy_version"]),
        "required": policy["required"],
        **{
            key: _integer(policy.get(key, default), ceiling)
            for key, (default, ceiling) in _LIMITS.items()
        },
    }


def validate_connection(value: Any, *, now: datetime) -> Mapping[str, Any]:
    """Validate an envelope returned by a trusted live authorization check.

    This checks structure and expiry, not the truth of the provider's identity.
    C3 supplies that verification and rejects revoked or disconnected accounts.
    Local repositories deliberately have no principal, workspace, or OAuth key.
    """
    connection = _object(
        value,
        {
            "schema",
            "capability_version",
            "connection",
            "provider",
            "kind",
            "generation",
            "state",
            "identity",
            "repositories",
            "recipients",
            "expires_at",
            "capabilities",
        },
    )
    if (
        connection["schema"] != CONNECTION_SCHEMA
        or type(connection["capability_version"]) is not int
        or connection["capability_version"] != CAPABILITY_VERSION
    ):
        raise ContextError("unsupported context connection schema or capability version")
    if connection["state"] != "verified":
        raise ContextError("context connection requires verified authorization")
    for key in ("connection", "provider"):
        _identifier(connection[key])
    _text(connection["generation"])
    capabilities = _object(connection["capabilities"], {"search", "memory", "revision_binding"})
    if any(type(value) is not bool for value in capabilities.values()):
        raise ContextError("context capabilities must be explicit booleans")
    if connection["kind"] == "organization":
        identity = _object(connection["identity"], {"principal", "workspace", "endpoint"})
    elif connection["kind"] == "repository":
        identity = _object(connection["identity"], {"repository_root"})
        if not Path(_text(identity["repository_root"], maximum=4096)).is_absolute():
            raise ContextError("local context repository root must be absolute")
    else:
        raise ContextError("unsupported context kind")
    for item in identity.values():
        _text(item, maximum=4096)
    _strings(connection["repositories"])
    _strings(connection["recipients"])
    if _timestamp(connection["expires_at"]) <= now:
        raise ContextError("context authorization has expired")
    return connection


@dataclass(frozen=True)
class ContextRequest:
    repository: str = field(repr=False)
    work_item: str = field(repr=False)
    recipient: str = field(repr=False)
    revision: str | None = field(default=None, repr=False)


class ContextAdapter(Protocol):
    """Trusted adapters implement verification and bounded read-only retrieval.

    Both methods return the versioned mappings validated here. Never pass this
    interface or its credentials into a builder/reviewer process. Capabilities
    and verified tool allowlists are adapter-specific; no write method exists.
    """

    def authorize(self) -> Mapping[str, Any]: ...

    def retrieve(self, request: ContextRequest, policy: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ValidatedPacket:
    """An immutable snapshot: decode a fresh copy only at an approved delivery."""

    _encoded: bytes = field(repr=False)
    sha256: str = field(repr=False)
    revision_state: str

    def private_payload(self) -> dict[str, Any]:
        return json.loads(self._encoded)

    def shareable_summary(self) -> dict[str, Any]:
        packet = self.private_payload()
        return {
            "schema": "code_mower.contextSummary.v1",
            "kind": packet["kind"],
            "documents": len(packet["documents"]),
            "completeness": packet["completeness"],
            "truncated": packet["truncated"],
            "revision_state": self.revision_state,
        }


def _reference(value: Any) -> Mapping[str, Any]:
    ref = _object(value, {"path", "sha256"})
    path = PurePosixPath(_text(ref["path"], maximum=1024))
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in ref["path"].split("/"))
        or "\\" in ref["path"]
    ):
        raise ContextError("context packet path must stay within the private store")
    if not isinstance(ref["sha256"], str) or not _DIGEST.fullmatch(ref["sha256"]):
        raise ContextError("context packet reference requires SHA-256")
    return ref


def manifest_packet_references(manifest: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Additive private refs; old external manifests retain their existing shape."""
    if "provider_packets" not in manifest:
        return ()
    if manifest.get("schema") != EXTERNAL_MANIFEST_SCHEMA:
        raise ContextError("unsupported external context manifest schema")
    refs = manifest["provider_packets"]
    if not isinstance(refs, list) or len(refs) > MAX_REFERENCES:
        raise ContextError("context packet reference count exceeds the supported bound")
    return tuple(dict(_reference(ref)) for ref in refs)


def _read_private_packet(root: Path, relative: str, maximum: int) -> bytes:
    """Descriptor-relative opens reject symlink traversal and check regular files.

    The caller supplies the trusted private store root, never a PR/provider path.
    Unsupported platforms fail closed until a secure store implementation exists.
    """
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ContextError("private context storage is unsupported on this platform")
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        _check_private_mode(descriptor)
        parts = PurePosixPath(relative).parts
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
            _check_private_mode(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ContextError("context packet must be a regular private file")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ContextError("context packet exceeds its byte budget")
        return data
    except OSError:
        raise ContextError("context packet is inaccessible or has an unsafe path") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _check_private_mode(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ContextError("context store and packet must be private to the current operator")


def _decode(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContextError("context JSON contains duplicate fields")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise ContextError("context packet is not supported JSON") from None


def load_packet(
    *,
    private_root: Path,
    reference: Mapping[str, Any],
    policy: Mapping[str, Any],
    request: ContextRequest,
    authorize: Callable[[], Mapping[str, Any]],
    now: datetime | None = None,
) -> ValidatedPacket:
    """Reauthorize every load/replay before reading, then validate all bindings.

    The reference/hash and policy must come from trusted runtime state, never the
    proposed PR. Hashes detect changed bytes; they are not signatures. Do not
    persist a ValidatedPacket as an authorization cache; load again for delivery.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ContextError("context validation time must include a timezone")
    limits = normalize_policy(policy)
    if limits is None:
        raise ContextError("context policy is not configured")
    ref = _reference(reference)
    for item in (request.repository, request.work_item, request.recipient):
        _text(item)
    if request.revision is not None:
        _text(request.revision)
    try:
        authorized = authorize()
    except Exception:
        raise ContextError(
            "context authorization failed; reconnect the selected connection"
        ) from None
    connection = validate_connection(authorized, now=current)
    if (
        connection["connection"] != limits["connection"]
        or request.repository not in connection["repositories"]
        or request.recipient not in connection["recipients"]
    ):
        raise ContextError("context connection does not authorize this destination or scope")
    data = _read_private_packet(private_root, ref["path"], limits["max_packet_bytes"])
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref["sha256"]:
        raise ContextError("context packet integrity check failed")
    packet = _object(
        _decode(data),
        {
            "schema",
            "capability_version",
            "provider",
            "kind",
            "retrieved_at",
            "source_revision",
            "source_built_at",
            "completeness",
            "truncated",
            "documents",
            "binding",
        },
    )
    if (
        packet["schema"] != PACKET_SCHEMA
        or type(packet["capability_version"]) is not int
        or packet["capability_version"] != CAPABILITY_VERSION
    ):
        raise ContextError("unsupported context packet schema or capability version")
    if packet["provider"] != connection["provider"] or packet["kind"] != connection["kind"]:
        raise ContextError("context provider binding does not match")
    binding = _object(
        packet["binding"],
        {
            "connection",
            "generation",
            "identity",
            "repository",
            "work_item",
            "policy_version",
            "recipients",
            "expires_at",
        },
    )
    if (
        any(binding[key] != connection[key] for key in ("connection", "generation", "identity"))
        or binding["repository"] != request.repository
        or binding["work_item"] != request.work_item
        or binding["policy_version"] != limits["policy_version"]
    ):
        raise ContextError("context packet scope or authorization binding does not match")
    recipients = _strings(binding["recipients"])
    if request.recipient not in recipients or not set(recipients) <= set(connection["recipients"]):
        raise ContextError("context packet recipient is not authorized")
    retrieved = _timestamp(packet["retrieved_at"])
    expiry = _timestamp(binding["expires_at"])
    if (
        not retrieved <= current < expiry
        or (current - retrieved).total_seconds() > limits["max_age_seconds"]
        or expiry > _timestamp(connection["expires_at"])
    ):
        raise ContextError("context packet is expired or outside its authorized time window")
    if packet["source_built_at"] is not None and _timestamp(packet["source_built_at"]) > retrieved:
        raise ContextError("context source build time is after retrieval")
    revision = packet["source_revision"]
    if revision is not None:
        _text(revision)
    revision_state = (
        "unknown"
        if revision is None or request.revision is None
        else ("matching" if revision == request.revision else "stale")
    )
    if (
        packet["completeness"] not in ("complete", "partial", "unknown")
        or type(packet["truncated"]) is not bool
        or (packet["truncated"] and packet["completeness"] == "complete")
    ):
        raise ContextError("context completeness and truncation are inconsistent")
    documents = packet["documents"]
    if not isinstance(documents, list) or len(documents) > limits["max_documents"]:
        raise ContextError("context document count exceeds its budget")
    total = 0
    for document in documents:
        doc = _object(document, {"text", "citations", "confidence"})
        if not isinstance(doc["text"], str) or not doc["text"].strip():
            raise ContextError("context evidence must contain text")
        try:
            count = len(doc["text"].encode("utf-8"))
        except UnicodeError:
            raise ContextError("context evidence must be valid UTF-8") from None
        total += count
        if count > limits["max_document_bytes"] or total > limits["max_text_bytes"]:
            raise ContextError("context evidence exceeds its text budget")
        if doc["confidence"] not in ("extracted", "inferred", "unknown"):
            raise ContextError("unsupported context evidence confidence")
        if not isinstance(doc["citations"], list) or not 1 <= len(doc["citations"]) <= 10:
            raise ContextError("context evidence requires bounded citations")
        for citation in doc["citations"]:
            cite = _object(citation, {"source", "title"})
            _text(cite["source"], maximum=2048)
            _text(cite["title"], maximum=512)
    return ValidatedPacket(data, digest, revision_state)
