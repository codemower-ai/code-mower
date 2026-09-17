"""Versioned private supervisor boundary and closed public status projection.

This is not authentication. Only an authenticated embedding may resolve these
opaque bindings against its private authorization store. Never publish a claim,
admission, decision, result, or exception from a dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from .context_store import strict_json

SCHEMA = "code_mower.supervisor.v1"
MAX_BYTES = 65536


class SupervisorError(ValueError):
    """A closed reason code; never includes private input or dependency output."""


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(Path(__file__).with_name("supervisor_contract.schema.json").read_text())


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _check(value, rule, depth=0, *, document=None):
    if depth > 16:
        raise SupervisorError("invalid_contract")
    if "$ref" in rule:
        return _check(value, (document or schema())["$defs"][rule["$ref"].rsplit("/", 1)[1]],
                      depth + 1, document=document)
    if "anyOf" in rule:
        for child in rule["anyOf"]:
            try:
                _check(value, child, depth + 1, document=document)
                return
            except SupervisorError:
                pass
        raise SupervisorError("invalid_contract")
    if "const" in rule and (type(value) is not type(rule["const"]) or value != rule["const"]):
        raise SupervisorError("invalid_contract")
    if "enum" in rule and (type(value) is not str or value not in rule["enum"]):
        raise SupervisorError("invalid_contract")
    kind = rule.get("type")
    if kind and type(value) is not {"object": dict, "string": str, "integer": int,
                                   "boolean": bool, "null": type(None)}[kind]:
        raise SupervisorError("invalid_contract")
    if kind == "object":
        if set(value) != set(rule["required"]):
            raise SupervisorError("invalid_contract")
        for key, child in value.items():
            _check(child, rule["properties"][key], depth + 1, document=document)
    elif kind == "string":
        if (not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 256)
                or ("pattern" in rule and re.fullmatch(rule["pattern"], value) is None)):
            raise SupervisorError("invalid_contract")
    elif kind == "integer" and not rule["minimum"] <= value <= rule["maximum"]:
        raise SupervisorError("invalid_contract")


def validate(kind: str, value) -> dict:
    """Validate the closed packaged subset, then detach from the caller."""
    try:
        raw = json.dumps(value, allow_nan=False).encode()
        if len(raw) > MAX_BYTES or kind not in schema()["$defs"]:
            raise SupervisorError("invalid_contract")
        _check(value, schema()["$defs"][kind])
        if kind == "status":
            if value["state"] == "complete" and (
                value["implementation"] != "verified" or value["writer"] != "terminated"
                or value["review"] != "passed" or value["gate"] != "passed"
                or value["review_writer"] != "terminated"
            ):
                raise SupervisorError("invalid_contract")
            if value["state"] == "cancelled" and any(
                value[key] not in {"not_started", "terminated"} for key in ("writer", "review_writer")
            ):
                raise SupervisorError("invalid_contract")
        if kind == "result":
            validate("status", value["status"])
            if (value["target"] is not None) != (value["status"]["implementation"] == "verified"):
                raise SupervisorError("invalid_contract")
        return json.loads(raw)
    except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
        raise SupervisorError("invalid_contract") from None


def decode(kind: str, raw: bytes) -> dict:
    try:
        if type(raw) is not bytes or len(raw) > MAX_BYTES:
            raise SupervisorError("invalid_contract")
        return validate(kind, strict_json(raw))
    except Exception:
        raise SupervisorError("invalid_contract") from None


def public_status(result: dict) -> dict:
    """The only public/telemetry output: every leaf is a closed enum or bound count."""
    return validate("result", result)["status"]
