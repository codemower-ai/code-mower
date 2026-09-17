"""Opt-in checkpoint contract. The v1 validator and operation enum stay frozen."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .context_store import strict_json
from .supervisor_contract import MAX_BYTES, SupervisorError, _check

SCHEMA = "code_mower.supervisor.v2"


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(Path(__file__).with_name("supervisor_contract_v2.schema.json").read_text())


def validate(kind: str, value) -> dict:
    try:
        raw = json.dumps(value, allow_nan=False).encode()
        if len(raw) > MAX_BYTES or kind not in schema()["$defs"]:
            raise SupervisorError("invalid_contract")
        _check(value, schema()["$defs"][kind], document=schema())
        if kind == "status":
            if value["state"] == "complete" and any(value[k] != expected for k, expected in (
                ("implementation", "verified"), ("writer", "terminated"), ("review", "passed"),
                ("gate", "passed"), ("review_writer", "terminated"),
            )):
                raise SupervisorError("invalid_contract")
            if value["state"] == "cancelled" and any(
                value[k] not in {"not_started", "terminated"} for k in ("writer", "review_writer")
            ):
                raise SupervisorError("invalid_contract")
            if value["state"] in {"waiting_for_user", "waiting_for_approval"}:
                expected = {"waiting_for_user": ("user_input_required", "clarify"),
                            "waiting_for_approval": ("approval_required", "owner_action")}
                if (value["lifecycle"]["state"] != value["state"]
                        or (value["reason"], value["next_action"]) != expected[value["state"]]):
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
    return validate("result", result)["status"]
