"""Organization-scoped Devin v3 sessions. No persistence, logging, or retries.

Message prose and structured output are local, transient data, never event data.
The checkpoint callback must durably save its metadata before returning.
"""

from __future__ import annotations

import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

API_BASE = "https://api.devin.ai"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 512 * 1024
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
ORG = re.compile(r"^org-[A-Za-z0-9_-]{1,120}$")
REPO = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")
SAFE_ERROR_CODES = frozenset(
    {
        "devin_api_rejected",
        "devin_api_unavailable",
        "authentication_required",
        "permission_denied",
        "invalid_response",
        "response_too_large",
        "request_timeout",
        "invalid_request",
        "approval_required",
        "session_failed",
        "waiting_for_owner",
        "session_suspended",
    }
)
ApiRunner = Callable[[str, str, Mapping[str, Any] | None, Mapping[str, str]], Any]


class DevinApiError(Exception):
    """Only a closed reason code may leave the transport boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if code in SAFE_ERROR_CODES else "devin_api_unavailable"
        super().__init__(self.code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirects can disclose Authorization or repeat paid POSTs.
        return None


def make_api_request(
    method: str,
    path: str,
    api_key: str,
    body: Mapping[str, Any] | None = None,
    *,
    api_runner: ApiRunner | None = None,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if (
        not isinstance(request_timeout, (int, float))
        or isinstance(request_timeout, bool)
        or not math.isfinite(request_timeout)
        or not 0 < request_timeout <= 30
    ):
        raise DevinApiError("invalid_request")
    if not isinstance(api_key, str) or not api_key or any(ord(c) < 33 for c in api_key):
        raise DevinApiError("authentication_required")
    if not path.startswith("/v3/organizations/") or method not in {"GET", "POST", "DELETE"}:
        raise DevinApiError("invalid_request")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    started = time.monotonic()
    try:
        encoded = json.dumps(body, allow_nan=False).encode() if body is not None else None
        if encoded is not None:
            if len(encoded) > MAX_REQUEST_BYTES:
                raise DevinApiError("invalid_request")
            headers["Content-Type"] = "application/json"
        if api_runner is not None:
            value = api_runner(method, API_BASE + path, body, headers)
        else:
            request = urllib.request.Request(
                API_BASE + path, data=encoded, headers=headers, method=method
            )
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=request_timeout
            ) as response:
                chunks = bytearray()
                while True:
                    remaining = request_timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise DevinApiError("request_timeout")
                    # Limit the next socket read to the remaining request budget.
                    response.fp.raw._sock.settimeout(remaining)
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise DevinApiError("response_too_large")
                    if not chunk or response.isclosed():
                        break
                value = json.loads(chunks.decode("utf-8"))
        if time.monotonic() - started > request_timeout:
            raise DevinApiError("request_timeout")
        if not isinstance(value, dict):
            raise DevinApiError("invalid_response")
        if len(json.dumps(value, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise DevinApiError("response_too_large")
        return value
    except DevinApiError:
        raise
    except urllib.error.HTTPError as exc:
        code = {401: "authentication_required", 403: "permission_denied"}.get(
            exc.code, "devin_api_rejected" if 400 <= exc.code < 500 else "devin_api_unavailable"
        )
        exc.close()
        raise DevinApiError(code) from None
    except (socket.timeout, TimeoutError):
        raise DevinApiError("request_timeout") from None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise DevinApiError("invalid_response") from None
    except Exception:
        # Injected runners may also raise exceptions containing credentials.
        raise DevinApiError("devin_api_unavailable") from None


@dataclass(frozen=True)
class Session:
    session_id: str
    state: str
    reason: str = ""
    structured_output: dict[str, Any] | None = field(default=None, repr=False)


def normalize_session(data: Mapping[str, Any], session_id: str = "") -> Session:
    sid = data.get("session_id", session_id)
    status = data.get("status")
    detail = data.get("status_detail")
    if (
        not isinstance(sid, str)
        or not TOKEN.fullmatch(sid)
        or status not in ("new", "claimed", "running", "resuming", "suspended", "exit", "error")
        or (detail is not None and not isinstance(detail, str))
        or type(data.get("is_archived", False)) is not bool
        or (
            data.get("structured_output") is not None
            and not isinstance(data["structured_output"], dict)
        )
    ):
        raise DevinApiError("invalid_response")
    if session_id and sid != session_id:
        raise DevinApiError("invalid_response")
    reason = ""
    if data.get("is_archived"):
        state = "archived"
    elif status == "error" or detail in {
        "error",
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "total_session_limit_exceeded",
    }:
        state, reason = "failed", "session_failed"
    elif status == "exit":
        state = "complete" if detail == "finished" else "terminated"
    elif status == "suspended":
        state, reason = "suspended", "session_suspended"
    elif detail in {"waiting_for_user", "waiting_for_approval"}:
        state, reason = (
            "owner_action",
            ("approval_required" if detail == "waiting_for_approval" else "waiting_for_owner"),
        )
    elif detail == "finished":
        state = "complete"
    else:
        state = {"new": "pending", "claimed": "pending", "resuming": "running"}.get(status, status)
    return Session(sid, state, reason, data.get("structured_output"))


@dataclass(frozen=True)
class CreateCheckpoint:
    org_id: str
    tag: str = field(default_factory=lambda: "cm-" + uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.org_id, str)
            or not ORG.fullmatch(self.org_id)
            or not isinstance(self.tag, str)
            or not re.fullmatch(r"cm-[0-9a-f]{32}", self.tag)
        ):
            raise DevinApiError("invalid_request")


@dataclass(frozen=True)
class Reconciliation:
    state: str  # none, matched, multiple, incomplete; none never authorizes create
    session_id: str = ""


def create_payload(
    prompt: str,
    *,
    max_acu_limit: int = 10,
    mode: str | None = None,
    playbook_id: str | None = None,
    knowledge_ids: tuple[str, ...] = (),
    resumable: bool = True,
    tags: tuple[str, ...] = (),
    repositories: tuple[str, ...] = (),
) -> dict:
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or type(max_acu_limit) is not int
        or not 0 < max_acu_limit <= 100
        or type(resumable) is not bool
        or mode not in (None, "normal", "fast", "lite", "ultra", "fusion")
    ):
        raise DevinApiError("invalid_request")
    for values, pattern in ((knowledge_ids, TOKEN), (tags, TOKEN), (repositories, REPO)):
        if (
            not isinstance(values, (tuple, list))
            or len(values) > 32
            or any(
                not isinstance(v, str) or len(v) > 256 or not pattern.fullmatch(v) for v in values
            )
        ):
            raise DevinApiError("invalid_request")
    if playbook_id is not None and (
        not isinstance(playbook_id, str) or not TOKEN.fullmatch(playbook_id)
    ):
        raise DevinApiError("invalid_request")
    result = dict(
        prompt=prompt,
        max_acu_limit=max_acu_limit,
        resumable=resumable,
        knowledge_ids=list(knowledge_ids),
        tags=list(tags),
        repos=list(repositories),
    )
    if mode is not None:
        result["devin_mode"] = mode
    if playbook_id is not None:
        result["playbook_id"] = playbook_id
    return result


class DevinClient:
    def __init__(self, org_id: str, api_key: str, *, api_runner: ApiRunner | None = None):
        if not isinstance(org_id, str) or not ORG.fullmatch(org_id):
            raise DevinApiError("invalid_request")
        self.org_id = org_id
        self._api_key = api_key
        self._runner = api_runner

    def __getstate__(self):
        raise TypeError("Devin clients cannot be persisted")

    def _request(
        self, method: str, suffix: str = "", body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return make_api_request(
            method,
            f"/v3/organizations/{self.org_id}/sessions{suffix}",
            self._api_key,
            body,
            api_runner=self._runner,
        )

    def _path(self, session_id: str) -> str:
        if not isinstance(session_id, str) or not TOKEN.fullmatch(session_id):
            raise DevinApiError("invalid_request")
        return "/" + session_id

    def create(
        self, payload: Mapping[str, Any], *, checkpoint: Callable[[CreateCheckpoint], None]
    ) -> str:
        # A fresh attempt always gets a fresh tag. Restored checkpoints go only to reconcile.
        attempt = CreateCheckpoint(self.org_id)
        body = dict(payload)
        validated = create_payload(
            body.get("prompt"),
            max_acu_limit=body.get("max_acu_limit", 10),
            mode=body.get("devin_mode"),
            playbook_id=body.get("playbook_id"),
            knowledge_ids=body.get("knowledge_ids", ()),
            resumable=body.get("resumable", True),
            tags=body.get("tags", ()),
            repositories=body.get("repos", ()),
        )
        allowed = set(validated) | {
            "devin_mode",
            "playbook_id",
            "title",
            "structured_output_required",
            "structured_output_schema",
        }
        if set(body) - allowed:
            raise DevinApiError("invalid_request")
        body = {**body, **validated}
        body["tags"].append(attempt.tag)
        if (
            "title" in body
            and (not isinstance(body["title"], str) or len(body["title"]) > 128)
            or (
                "structured_output_required" in body
                and type(body["structured_output_required"]) is not bool
            )
            or (
                "structured_output_schema" in body
                and not isinstance(body["structured_output_schema"], dict)
            )
        ):
            raise DevinApiError("invalid_request")
        try:
            encoded = json.dumps(body, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            raise DevinApiError("invalid_request") from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise DevinApiError("invalid_request")
        checkpoint(attempt)
        data = self._request("POST", body=body)
        sid = data.get("session_id")
        if not isinstance(sid, str) or not TOKEN.fullmatch(sid):
            raise DevinApiError("invalid_response")
        return sid

    def get(self, session_id: str) -> Session:
        return normalize_session(self._request("GET", self._path(session_id)), session_id)

    def send_message(self, session_id: str, message: str) -> Session:
        if not isinstance(message, str) or not message.strip():
            raise DevinApiError("invalid_request")
        return normalize_session(
            self._request("POST", self._path(session_id) + "/messages", {"message": message}),
            session_id,
        )

    def terminate(self, session_id: str) -> Session:
        return normalize_session(self._request("DELETE", self._path(session_id)), session_id)

    def archive(self, session_id: str) -> Session:
        return normalize_session(
            self._request("POST", self._path(session_id) + "/archive"), session_id
        )

    def _page(self, suffix: str, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        if cursor is not None and (not isinstance(cursor, str) or not 0 < len(cursor) <= 1024):
            raise DevinApiError("invalid_request")
        query = urllib.parse.urlencode({"first": 100, **({"after": cursor} if cursor else {})})
        data = self._request("GET", suffix + "?" + query)
        items, more, end = data.get("items"), data.get("has_next_page"), data.get("end_cursor")
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(not isinstance(item, dict) for item in items)
            or type(more) is not bool
            or (end is not None and (not isinstance(end, str) or not 0 < len(end) <= 1024))
            or (more and not end)
        ):
            raise DevinApiError("invalid_response")
        return items, end if more else None

    def list_messages(
        self, session_id: str, *, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One bounded page of transient messages; caller owns further pagination."""
        items, end = self._page(self._path(session_id) + "/messages", cursor)
        result = []
        for item in items:
            if (
                not isinstance(item.get("event_id"), str)
                or not isinstance(item.get("message"), str)
                or item.get("source") not in ("devin", "user")
                or type(item.get("created_at")) is not int
            ):
                raise DevinApiError("invalid_response")
            result.append(
                {key: item[key] for key in ("event_id", "message", "source", "created_at")}
            )
        return result, end

    def reconcile(self, checkpoint: CreateCheckpoint, *, max_pages: int = 5) -> Reconciliation:
        if (
            checkpoint.org_id != self.org_id
            or type(max_pages) is not int
            or not 1 <= max_pages <= 10
        ):
            raise DevinApiError("invalid_request")
        cursor = None
        seen = set()
        matches = set()
        for _ in range(max_pages):
            items, end = self._page("", cursor)
            for item in items:
                session = normalize_session(item)
                tags = item.get("tags")
                if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                    raise DevinApiError("invalid_response")
                if checkpoint.tag in tags:
                    matches.add(session.session_id)
            if len(matches) > 1:
                return Reconciliation("multiple")
            if end is None:
                return (
                    Reconciliation("matched", next(iter(matches)))
                    if matches
                    else Reconciliation("none")
                )
            if end in seen:
                return Reconciliation("incomplete")
            seen.add(end)
            cursor = end
        return Reconciliation("incomplete")
