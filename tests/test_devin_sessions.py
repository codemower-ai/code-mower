"""Offline v3 contract fixtures; no credentials or paid API calls."""

import json
import pickle
import sys
import traceback
import unittest
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import devin_sessions as d


class Runner:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body))
        if headers["Authorization"] != "Bearer test-secret":
            raise AssertionError("Unexpected authorization header")
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


class DevinSessionsTests(unittest.TestCase):
    def test_lifecycle(self):
        for status, detail, state in [
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
        ]:
            with self.subTest(status=status, detail=detail, state=state):
                data = dict(session_id="devin-1", status=status, status_detail=detail)
                self.assertEqual(d.normalize_session(data).state, state)
                self.assertEqual(
                    d.normalize_session({**data, "is_archived": True}).state, "archived"
                )

    def test_failure_reasons(self):
        for detail in [
            "error",
            "usage_limit_exceeded",
            "out_of_credits",
            "out_of_quota",
            "no_quota_allocation",
            "payment_declined",
            "org_usage_limit_exceeded",
            "user_usage_limit_exceeded",
            "total_session_limit_exceeded",
        ]:
            with self.subTest(detail=detail):
                result = d.normalize_session(session(status_detail=detail))
                self.assertEqual((result.state, result.reason), ("failed", "session_failed"))

    def test_malformed_snapshot(self):
        for changes in [
            {"status": None},
            {"status": []},
            {"status": "provider-private-text"},
            {"session_id": "../escape"},
            {"status_detail": []},
            {"is_archived": "false"},
            {"structured_output": "private"},
            {"session_id": "other"},
        ]:
            with self.subTest(changes=changes):
                c, _ = client({**session(), **changes})
                with self.assertRaisesRegex(d.DevinApiError, "invalid_response"):
                    c.get("devin-1")

    def test_all_endpoints_and_checkpoint_order(self):
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
            self.assertEqual(runner.calls, [])
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
        self.assertEqual(c.create(payload, checkpoint=checkpoint), "devin-1")
        self.assertEqual(c.get("devin-1").state, "running")
        self.assertEqual(c.list_messages("devin-1"), ([message], None))
        self.assertEqual(c.send_message("devin-1", "local reply").state, "running")
        self.assertEqual(c.terminate("devin-1").state, "terminated")
        self.assertEqual(c.archive("devin-1").state, "archived")
        base = d.API_BASE + "/v3/organizations/org-test/sessions"
        self.assertEqual(
            [(m, u) for m, u, _ in runner.calls],
            [
                ("POST", base),
                ("GET", base + "/devin-1"),
                ("GET", base + "/devin-1/messages?first=100"),
                ("POST", base + "/devin-1/messages"),
                ("DELETE", base + "/devin-1"),
                ("POST", base + "/devin-1/archive"),
            ],
        )
        self.assertIn(saved[0]["tag"], runner.calls[0][2]["tags"])
        self.assertEqual(set(saved[0]), {"org_id", "tag"})
        self.assertNotIn("local task", json.dumps(saved))
        self.assertNotIn("test-secret", repr(c))
        with self.assertRaises(TypeError):
            pickle.dumps(c)

    def test_uncertain_create_never_retries(self):
        for response in [TimeoutError("test-secret"), {}, {"session_id": "../bad"}]:
            with self.subTest(response=response):
                c, r = client(response)
                saved = []
                with self.assertRaises(d.DevinApiError):
                    c.create(d.create_payload("task"), checkpoint=saved.append)
                self.assertEqual(len(r.calls), 1)
                self.assertEqual(len(saved), 1)
                self.assertNotIn("test-secret", repr(saved))

    def test_failed_checkpoint_sends_nothing(self):
        c, r = client()

        def fail(attempt):
            raise OSError("disk full")

        with self.assertRaises(OSError):
            c.create(d.create_payload("task"), checkpoint=fail)
        self.assertEqual(r.calls, [])

    def test_reconciliation_cardinality(self):
        for count, state in [(0, "none"), (1, "matched"), (2, "multiple")]:
            with self.subTest(count=count, state=state):
                attempt = d.CreateCheckpoint("org-test")
                items = [
                    {**session(), "session_id": f"devin-{i}", "tags": [attempt.tag]}
                    for i in range(count)
                ]
                c, r = client(page(items))
                result = c.reconcile(attempt)
                self.assertEqual(result.state, state)
                self.assertEqual(result.session_id, "devin-0" if count == 1 else "")
                self.assertEqual([m for m, _, _ in r.calls], ["GET"])

    def test_reconcile_pagination_bound_and_duplicate_ids(self):
        attempt = d.CreateCheckpoint("org-test")
        item = {**session(), "tags": [attempt.tag]}
        c, r = client(page([item], "a&b"), page([item]))
        self.assertEqual(c.reconcile(attempt).state, "matched")
        self.assertTrue(r.calls[1][1].endswith("first=100&after=a%26b"))
        c, r = client(page([item], "a"), page([item], "a"))
        self.assertEqual(c.reconcile(attempt).state, "incomplete")
        c, r = client(page([item], "a"))
        self.assertEqual(c.reconcile(attempt, max_pages=1).state, "incomplete")
        self.assertEqual(len(r.calls), 1)
        with self.assertRaises(d.DevinApiError):
            c.reconcile(d.CreateCheckpoint("org-other"))

    def test_bad_pages(self):
        for response in [
            {},
            {"items": [], "has_next_page": True},
            page([{}]),
            page([{"event_id": 3}]),
            page([None]),
            page([{}] * 101),
        ]:
            with self.subTest(response=response):
                c, _ = client(response)
                with self.assertRaisesRegex(d.DevinApiError, "invalid_response"):
                    c.list_messages("devin-1")

    def test_auth_and_permission_errors_every_endpoint(self):
        for method in [
            "get",
            "send_message",
            "terminate",
            "archive",
            "list_messages",
            "reconcile",
            "create",
        ]:
            with self.subTest(method=method):
                for status, code in [
                    (401, "authentication_required"),
                    (403, "permission_denied"),
                    (429, "devin_api_rejected"),
                    (500, "devin_api_unavailable"),
                ]:
                    with self.subTest(status=status, code=code):
                        c, r = client(
                            urllib.error.HTTPError("private", status, "test-secret", {}, None)
                        )
                        args = {
                            "create": (d.create_payload("task"),),
                            "reconcile": (d.CreateCheckpoint("org-test"),),
                            "send_message": ("devin-1", "task"),
                        }.get(method, ("devin-1",))
                        with self.assertRaisesRegex(d.DevinApiError, code):
                            try:
                                getattr(c, method)(
                                    *args,
                                    **(
                                        {"checkpoint": lambda a: None} if method == "create" else {}
                                    ),
                                )
                            except d.DevinApiError:
                                self.assertNotIn("test-secret", traceback.format_exc())
                                raise
                        self.assertEqual(len(r.calls), 1)

    def test_transport_failures(self):
        for response, code in [
            ([], "invalid_response"),
            ({"raw": "a" * d.MAX_RESPONSE_BYTES}, "response_too_large"),
            (TimeoutError("private"), "request_timeout"),
            (OSError("private"), "devin_api_unavailable"),
        ]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(d.DevinApiError, code):
                    d.make_api_request(
                        "GET",
                        "/v3/organizations/org-test/sessions",
                        "test-secret",
                        api_runner=Runner(response),
                    )

    def test_real_transport_size_json_timeout_redirect_bounds(self):
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
                with self.assertRaisesRegex(d.DevinApiError, code):
                    d.make_api_request("GET", "/v3/organizations/org-test/sessions", "key")
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
            response.read1.side_effect = [b"{}", b""]
            with patch.object(d.time, "monotonic", side_effect=[0, 31]):
                with self.assertRaisesRegex(d.DevinApiError, "request_timeout"):
                    d.make_api_request("GET", "/v3/organizations/org-test/sessions", "key")
        self.assertIs(
            d._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil"), None
        )

    def test_timeout_configuration_bounded(self):
        for timeout in [0, -1, 31, float("nan"), float("inf"), True]:
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(d.DevinApiError, "invalid_request"):
                    d.make_api_request(
                        "GET", "/v3/organizations/org-test/sessions", "key", request_timeout=timeout
                    )

    def test_bounded_create_options(self):
        for kwargs in [
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
        ]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(d.DevinApiError, "invalid_request"):
                    d.create_payload("task", **kwargs)

    def test_malformed_response_every_endpoint(self):
        for method in [
            "get",
            "send_message",
            "terminate",
            "archive",
            "list_messages",
            "reconcile",
            "create",
        ]:
            with self.subTest(method=method):
                c, r = client({})
                args = {
                    "create": (d.create_payload("task"),),
                    "reconcile": (d.CreateCheckpoint("org-test"),),
                    "send_message": ("devin-1", "task"),
                }.get(method, ("devin-1",))
                with self.assertRaisesRegex(d.DevinApiError, "invalid_response"):
                    getattr(c, method)(
                        *args, **({"checkpoint": lambda a: None} if method == "create" else {})
                    )
                self.assertEqual(len(r.calls), 1)

    def test_fresh_attempt_tags_are_unique(self):
        c, _ = client({"session_id": "devin-1"}, {"session_id": "devin-2"})
        saved = []
        c.create(d.create_payload("task"), checkpoint=saved.append)
        c.create(d.create_payload("task"), checkpoint=saved.append)
        self.assertNotEqual(saved[0].tag, saved[1].tag)

    def test_reconcile_validates_every_page_before_unique_match(self):
        attempt = d.CreateCheckpoint("org-test")
        c, _ = client(page([{**session(), "tags": [attempt.tag]}], "next"), {})
        with self.assertRaisesRegex(d.DevinApiError, "invalid_response"):
            c.reconcile(attempt)

    def test_http_content_length_completion_does_not_read_closed_socket(self):
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
            self.assertEqual(
                d.make_api_request("GET", "/v3/organizations/org-test/sessions", "key"), {}
            )
        self.assertTrue(response.isclosed())

    def test_corrupt_checkpoint_is_closed_error(self):
        for bad in [None, [], 42, "invalid"]:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(d.DevinApiError, "invalid_request"):
                    d.CreateCheckpoint("org-test", bad)

    def test_finished_exit_is_complete(self):
        self.assertEqual(
            d.normalize_session({**session(), "status": "exit", "status_detail": "finished"}).state,
            "complete",
        )


if __name__ == "__main__":
    unittest.main()
