"""Offline v3 contract fixtures; no credentials or paid API calls."""

import json
import pickle
import traceback
import urllib.error
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from code_mower import devin_sessions as d


class Runner:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body))
        assert headers["Authorization"] == "Bearer test-secret"
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return result


def client(*responses):
    runner = Runner(*responses)
    return d.DevinClient("org-test", "test-secret", api_runner=runner), runner


def session(**kw):
    return dict(session_id="devin-1", status="running", tags=[], **kw)


def page(items=(), end=None):
    return dict(items=list(items), end_cursor=end, has_next_page=end is not None)


@pytest.mark.parametrize(
    ("status", "detail", "state"),
    [
        ("new", None, "pending"),
        ("claimed", None, "pending"),
        ("running", "working", "running"),
        ("resuming", None, "running"),
        ("running", "finished", "complete"),
        ("running", "waiting_for_user", "owner_action"),
        ("running", "waiting_for_approval", "owner_action"),
        ("suspended", "inactivity", "suspended"),
        ("suspended", "user_request", "suspended"),
        ("error", None, "failed"),
        ("exit", None, "terminated"),
    ],
)
def test_lifecycle(status, detail, state):
    data = dict(session_id="devin-1", status=status, status_detail=detail)
    assert d.normalize_session(data).state == state
    assert d.normalize_session({**data, "is_archived": True}).state == "archived"


@pytest.mark.parametrize(
    "detail",
    [
        "error",
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "total_session_limit_exceeded",
    ],
)
def test_failure_reasons(detail):
    result = d.normalize_session(session(status_detail=detail))
    assert (result.state, result.reason) == ("failed", "session_failed")


@pytest.mark.parametrize(
    "changes",
    [
        {"status": None},
        {"status": []},
        {"status": "provider-private-text"},
        {"session_id": "../escape"},
        {"status_detail": []},
        {"is_archived": "false"},
        {"structured_output": "private"},
        {"session_id": "other"},
    ],
)
def test_malformed_snapshot(changes):
    c, _ = client({**session(), **changes})
    with pytest.raises(d.DevinApiError, match="invalid_response"):
        c.get("devin-1")


def test_all_endpoints_and_checkpoint_order():
    message = dict(event_id="event-1", source="devin", message="local prose", created_at=1)
    c, runner = client(
        {"session_id": "devin-1"},
        session(),
        page([message]),
        session(),
        {**session(), "status": "exit"},
        session(is_archived=True),
    )
    saved = []

    def checkpoint(attempt):
        assert runner.calls == []
        saved.append(asdict(attempt))

    payload = d.create_payload(
        "local task",
        max_acu_limit=5,
        mode="lite",
        playbook_id="play-1",
        knowledge_ids=("note-1",),
        repositories=("owner/repo",),
        tags=("test",),
        resumable=False,
    )
    assert c.create(payload, checkpoint=checkpoint) == "devin-1"
    assert c.get("devin-1").state == "running"
    assert c.list_messages("devin-1") == ([message], None)
    assert c.send_message("devin-1", "local reply").state == "running"
    assert c.terminate("devin-1").state == "terminated"
    assert c.archive("devin-1").state == "archived"
    base = d.API_BASE + "/v3/organizations/org-test/sessions"
    assert [(m, u) for m, u, _ in runner.calls] == [
        ("POST", base),
        ("GET", base + "/devin-1"),
        ("GET", base + "/devin-1/messages?first=100"),
        ("POST", base + "/devin-1/messages"),
        ("DELETE", base + "/devin-1"),
        ("POST", base + "/devin-1/archive"),
    ]
    assert saved[0]["tag"] in runner.calls[0][2]["tags"]
    assert set(saved[0]) == {"org_id", "tag"}
    assert "local task" not in json.dumps(saved)
    assert "test-secret" not in repr(c)
    with pytest.raises(TypeError):
        pickle.dumps(c)


@pytest.mark.parametrize("response", [TimeoutError("test-secret"), {}, {"session_id": "../bad"}])
def test_uncertain_create_never_retries(response):
    c, r = client(response)
    saved = []
    with pytest.raises(d.DevinApiError):
        c.create(d.create_payload("task"), checkpoint=saved.append)
    assert len(r.calls) == len(saved) == 1
    assert "test-secret" not in repr(saved)


def test_failed_checkpoint_sends_nothing():
    c, r = client()

    def fail(attempt):
        raise OSError("disk full")

    with pytest.raises(OSError):
        c.create(d.create_payload("task"), checkpoint=fail)
    assert r.calls == []


@pytest.mark.parametrize("count,state", [(0, "none"), (1, "matched"), (2, "multiple")])
def test_reconciliation_cardinality(count, state):
    attempt = d.CreateCheckpoint("org-test")
    items = [{**session(), "session_id": f"devin-{i}", "tags": [attempt.tag]} for i in range(count)]
    c, r = client(page(items))
    result = c.reconcile(attempt)
    assert result.state == state
    assert result.session_id == ("devin-0" if count == 1 else "")
    assert [m for m, _, _ in r.calls] == ["GET"]


def test_reconcile_pagination_bound_and_duplicate_ids():
    attempt = d.CreateCheckpoint("org-test")
    item = {**session(), "tags": [attempt.tag]}
    c, r = client(page([item], "a&b"), page([item]))
    assert c.reconcile(attempt).state == "matched"
    assert r.calls[1][1].endswith("first=100&after=a%26b")
    c, r = client(page([item], "a"), page([item], "a"))
    assert c.reconcile(attempt).state == "incomplete"
    c, r = client(page([item], "a"))
    assert c.reconcile(attempt, max_pages=1).state == "incomplete"
    assert len(r.calls) == 1
    with pytest.raises(d.DevinApiError):
        c.reconcile(d.CreateCheckpoint("org-other"))


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"items": [], "has_next_page": True},
        page([{}]),
        page([{"event_id": 3}]),
        page([None]),
        page([{}] * 101),
    ],
)
def test_bad_pages(response):
    c, _ = client(response)
    with pytest.raises(d.DevinApiError, match="invalid_response"):
        c.list_messages("devin-1")


@pytest.mark.parametrize(
    "method",
    ["get", "send_message", "terminate", "archive", "list_messages", "reconcile", "create"],
)
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication_required"),
        (403, "permission_denied"),
        (429, "devin_api_rejected"),
        (500, "devin_api_unavailable"),
    ],
)
def test_auth_and_permission_errors_every_endpoint(method, status, code):
    c, r = client(urllib.error.HTTPError("private", status, "test-secret", {}, None))
    args = {
        "create": (d.create_payload("task"),),
        "reconcile": (d.CreateCheckpoint("org-test"),),
        "send_message": ("devin-1", "task"),
    }.get(method, ("devin-1",))
    with pytest.raises(d.DevinApiError, match=code):
        try:
            getattr(c, method)(
                *args, **({"checkpoint": lambda a: None} if method == "create" else {})
            )
        except d.DevinApiError:
            assert "test-secret" not in traceback.format_exc()
            raise
    assert len(r.calls) == 1


@pytest.mark.parametrize(
    "response,code",
    [
        ([], "invalid_response"),
        ({"raw": "a" * d.MAX_RESPONSE_BYTES}, "response_too_large"),
        (TimeoutError("private"), "request_timeout"),
        (OSError("private"), "devin_api_unavailable"),
    ],
)
def test_transport_failures(response, code):
    with pytest.raises(d.DevinApiError, match=code):
        d.make_api_request(
            "GET", "/v3/organizations/org-test/sessions", "test-secret", api_runner=Runner(response)
        )


def test_real_transport_size_json_timeout_redirect_bounds():
    response = MagicMock()
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response
    with patch.object(d.urllib.request, "build_opener", return_value=opener):
        for chunks, code in [
            ([b"x" * (d.MAX_RESPONSE_BYTES + 1)], "response_too_large"),
            ([b"not json", b""], "invalid_response"),
            ([b"\xff", b""], "invalid_response"),
        ]:
            response.read1.side_effect = chunks
            with pytest.raises(d.DevinApiError, match=code):
                d.make_api_request("GET", "/v3/organizations/org-test/sessions", "key")
        assert opener.open.call_args.kwargs["timeout"] == 30
        response.read1.side_effect = [b"{}", b""]
        with patch.object(d.time, "monotonic", side_effect=[0, 31]):
            with pytest.raises(d.DevinApiError, match="request_timeout"):
                d.make_api_request("GET", "/v3/organizations/org-test/sessions", "key")
    assert d._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf"), True])
def test_timeout_configuration_bounded(timeout):
    with pytest.raises(d.DevinApiError, match="invalid_request"):
        d.make_api_request(
            "GET", "/v3/organizations/org-test/sessions", "key", request_timeout=timeout
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(max_acu_limit=0),
        dict(max_acu_limit=101),
        dict(max_acu_limit=float("nan")),
        dict(max_acu_limit=True),
        dict(mode="invented"),
        dict(resumable="yes"),
        dict(repositories=["../bad"]),
        dict(tags=["private prose"]),
        dict(knowledge_ids=["../escape"]),
        dict(playbook_id="unsafe/id"),
    ],
)
def test_bounded_create_options(kwargs):
    with pytest.raises(d.DevinApiError, match="invalid_request"):
        d.create_payload("task", **kwargs)


@pytest.mark.parametrize(
    "method",
    ["get", "send_message", "terminate", "archive", "list_messages", "reconcile", "create"],
)
def test_malformed_response_every_endpoint(method):
    c, r = client({})
    args = {
        "create": (d.create_payload("task"),),
        "reconcile": (d.CreateCheckpoint("org-test"),),
        "send_message": ("devin-1", "task"),
    }.get(method, ("devin-1",))
    with pytest.raises(d.DevinApiError, match="invalid_response"):
        getattr(c, method)(*args, **({"checkpoint": lambda a: None} if method == "create" else {}))
    assert len(r.calls) == 1


def test_fresh_attempt_tags_are_unique():
    c, _ = client({"session_id": "devin-1"}, {"session_id": "devin-2"})
    saved = []
    c.create(d.create_payload("task"), checkpoint=saved.append)
    c.create(d.create_payload("task"), checkpoint=saved.append)
    assert saved[0].tag != saved[1].tag


def test_reconcile_validates_every_page_before_unique_match():
    attempt = d.CreateCheckpoint("org-test")
    c, _ = client(page([{**session(), "tags": [attempt.tag]}], "next"), {})
    with pytest.raises(d.DevinApiError, match="invalid_response"):
        c.reconcile(attempt)


def test_http_content_length_completion_does_not_read_closed_socket():
    import http.client
    import io

    class Socket:
        def makefile(self, *args):
            raw = io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            raw.raw = MagicMock()
            return raw

    response = http.client.HTTPResponse(Socket())
    response.begin()
    opener = MagicMock()
    opener.open.return_value = response
    with patch.object(d.urllib.request, "build_opener", return_value=opener):
        assert d.make_api_request("GET", "/v3/organizations/org-test/sessions", "key") == {}
    assert response.isclosed()


@pytest.mark.parametrize("bad", [None, [], 42, "invalid"])
def test_corrupt_checkpoint_is_closed_error(bad):
    with pytest.raises(d.DevinApiError, match="invalid_request"):
        d.CreateCheckpoint("org-test", bad)


def test_finished_exit_is_complete():
    assert (
        d.normalize_session({**session(), "status": "exit", "status_detail": "finished"}).state
        == "complete"
    )
