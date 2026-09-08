#!/usr/bin/env python3
"""Offline tests for the guarded Jira mutation plan/apply surface (issue #799).

Every test is deterministic and performs no live network call: HTTP goes
through an injected route-based fake runner. Fixtures use synthetic ids and
example.atlassian.net only. A test that expects zero Jira traffic injects a
runner that raises on any call, so "no write happened" is proven rather than
asserted from a report field alone.
"""

from __future__ import annotations

import json
import re
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from code_mower import jira_cloud, jira_mutations


CLOUD_ID = "11111111-2222-3333-4444-555555555555"
SITE_URL = "https://example.atlassian.net"
PROJECT_ID = "10001"
EMAIL = "qa-bot@example.com"
TOKEN = "tok-1"
ISSUE = "ABC-1"
ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef5"
OTHER_ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef9"
PR_URL = "https://github.com/owner/repo/pull/12"
PROSE = "private issue prose must never leave Jira"

ISSUE_ID = "10101"

ISSUE_PATH = f"/rest/api/3/issue/{ISSUE}"
LEDGER_PATH = f"{ISSUE_PATH}/properties/{jira_mutations.LEDGER_PROPERTY_KEY}"
MYSELF_PATH = "/rest/api/3/myself"


def comment_fingerprint(template: str, pr_url: str = "", issue_id: str = ISSUE_ID) -> str:
    """The replay fingerprint apply computes for one comment intent."""
    detail: dict[str, Any] = {"template": template}
    if pr_url:
        detail["pr_url"] = pr_url
    return jira_mutations._operation_fingerprint(
        "comment", jira_mutations.fingerprint_subject(issue_id), detail
    )


def claim_path(template: str, pr_url: str = "", issue_ref: str = ISSUE) -> str:
    """The dedicated claim property path for one comment intent."""
    key = jira_mutations.comment_claim_property_key(comment_fingerprint(template, pr_url))
    return f"/rest/api/3/issue/{issue_ref}/properties/{key}"


def ok(payload: Any, status: int = 200, headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), json.dumps(payload).encode("utf-8"))


def empty(status: int = 204, headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), b"")


def error(status: int, headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), b"{}")


def issue_response(
    *,
    status_id: str = "3",
    assignee: str | None = None,
    project_id: str = PROJECT_ID,
) -> Any:
    """A realistic issue payload, deliberately carrying prose we must drop."""
    return {
        "id": "10101",
        "key": ISSUE,
        "fields": {
            "summary": PROSE,
            "description": PROSE,
            "project": {"id": project_id, "key": "ABC"},
            "status": {"id": status_id, "name": "In Progress"},
            "issuetype": {"id": "10001", "name": "Task"},
            "assignee": None if assignee is None else {"accountId": assignee},
        },
    }


class RouteHttp:
    """Route-based HTTP runner keyed by (method, exact path)."""

    def __init__(self, routes: Mapping[tuple[str, str], Any]) -> None:
        self.routes = {key: list(value) if isinstance(value, list) else [value]
                       for key, value in routes.items()}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        parts = urllib.parse.urlsplit(url)
        # Requests go to the scoped-token gateway, so routes match the REST
        # path after the /ex/jira/{cloud_id} prefix.
        rest_path = "/rest/api/3/" + parts.path.split("/rest/api/3/", 1)[-1]
        self.calls.append(
            {
                "method": method,
                "path": rest_path,
                "query": dict(urllib.parse.parse_qsl(parts.query)),
                "body": json.loads(body.decode("utf-8")) if body else None,
            }
        )
        queue = self.routes.get((method, rest_path))
        if not queue:
            raise AssertionError(f"unrouted request: {method} {rest_path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def paths(self, method: str = "") -> list[str]:
        return [
            call["path"]
            for call in self.calls
            if not method or call["method"] == method
        ]

    def write_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] in ("PUT", "POST", "DELETE")]


class ExplodingHttp:
    """Runner that fails the test if any Jira request is attempted."""

    def __call__(self, *args: Any, **kwargs: Any):
        raise AssertionError("a Jira request was attempted when none was allowed")


_ISSUE_ROUTE = re.compile(r"^/rest/api/3/issue/(?P<ref>[^/]+)(?P<suffix>/.*)?$")


class FakeJira:
    """A small stateful Jira for the endpoints this surface touches.

    Issue properties follow the documented create-or-update contract: ``PUT``
    answers 201 the first time a key is written and 200 for every write
    afterwards. That distinction is the whole at-most-once primitive behind a
    comment claim, so a fake that always answered 200 -- or always 201 --
    would let a real double-post pass its tests.

    The issue answers to both its key and its immutable id, which is what
    makes a replay across the two spellings testable.
    """

    def __init__(
        self,
        *,
        issue_id: str = ISSUE_ID,
        key: str = ISSUE,
        status_id: str = "3",
        assignee: str | None = None,
        project_id: str = PROJECT_ID,
        transitions: Any = ({"id": "31", "name": "Start", "to": {"id": "3"}},),
        account_id: str = ACCOUNT_ID,
        global_ids: Any = (),
        faults: Mapping[tuple[str, str], Any] | None = None,
        before_request: Any = None,
    ) -> None:
        self.issue_id = issue_id
        self.key = key
        self.status_id = status_id
        self.assignee = assignee
        self.project_id = project_id
        self.transitions = [dict(item) for item in transitions]
        self.account_id = account_id
        self.global_ids = list(global_ids)
        self.faults = {key_: list(value) for key_, value in dict(faults or {}).items()}
        self.before_request = before_request
        self.properties: dict[str, Any] = {}
        self.comments: list[Mapping[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    # -- helpers used by tests -------------------------------------------

    def paths(self, method: str = "") -> list[str]:
        return [
            call["path"] for call in self.calls if not method or call["method"] == method
        ]

    def write_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] in ("PUT", "POST", "DELETE")]

    def comment_posts(self) -> list[dict[str, Any]]:
        return [
            call
            for call in self.calls
            if call["method"] == "POST" and call["path"].endswith("/comment")
        ]

    def claim(self, template: str, pr_url: str = "") -> Any:
        key = jira_mutations.comment_claim_property_key(
            comment_fingerprint(template, pr_url)
        )
        return self.properties.get(key)

    # -- transport --------------------------------------------------------

    def _fault(self, method: str, suffix: str) -> Any:
        queue = self.faults.get((method, suffix))
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        parts = urllib.parse.urlsplit(url)
        path = "/rest/api/3/" + parts.path.split("/rest/api/3/", 1)[-1]
        parsed_body = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": dict(urllib.parse.parse_qsl(parts.query)),
                "body": parsed_body,
            }
        )
        if self.before_request is not None:
            self.before_request(self, method, path)
        return self._handle(method, path, parsed_body)

    def _handle(self, method: str, path: str, body: Any):
        if path == "/rest/api/3/myself":
            fault = self._fault("GET", "myself")
            if fault is not None:
                return self._raise_or_return(fault)
            return ok({"accountId": self.account_id, "emailAddress": EMAIL})

        match = _ISSUE_ROUTE.fullmatch(path)
        if match is None:
            raise AssertionError(f"unrouted request: {method} {path}")
        ref = urllib.parse.unquote(match.group("ref"))
        # Jira resolves an issue key case-insensitively, and by its id too.
        if ref != self.issue_id and ref.upper() != self.key.upper():
            return error(404)
        suffix = match.group("suffix") or ""

        if suffix.startswith("/properties/"):
            key = urllib.parse.unquote(suffix[len("/properties/"):])
            return self._property(method, key, body)

        label = suffix.lstrip("/") or "issue"
        fault = self._fault(method, label)
        if fault is not None:
            return self._raise_or_return(fault)

        if suffix == "" and method == "GET":
            return ok(
                issue_response(
                    status_id=self.status_id,
                    assignee=self.assignee,
                    project_id=self.project_id,
                )
                | {"id": self.issue_id, "key": self.key}
            )
        if suffix == "/transitions" and method == "GET":
            return ok({"transitions": self.transitions})
        if suffix == "/transitions" and method == "POST":
            chosen = next(
                (
                    item
                    for item in self.transitions
                    if item["id"] == body["transition"]["id"]
                ),
                None,
            )
            assert chosen is not None, "transition posted without being offered"
            self.status_id = str(chosen.get("to", {}).get("id") or self.status_id)
            return empty()
        if suffix == "/assignee" and method == "PUT":
            self.assignee = body["accountId"]
            return empty()
        if suffix == "/comment" and method == "POST":
            self.comments.append(body)
            return ok({"id": f"2000{len(self.comments)}"}, status=201)
        if suffix == "/remotelink" and method == "GET":
            return ok([{"id": 9, "globalId": gid} for gid in self.global_ids])
        if suffix == "/remotelink" and method == "POST":
            if body["globalId"] not in self.global_ids:
                self.global_ids.append(body["globalId"])
            return ok({"id": 9}, status=201)
        raise AssertionError(f"unrouted request: {method} {path}")

    def _property(self, method: str, key: str, body: Any):
        fault = self._fault(method, f"properties/{key}")
        if fault is None and key.startswith(jira_mutations.COMMENT_CLAIM_PREFIX):
            fault = self._fault(method, "comment_claim")
        if fault is not None:
            return self._raise_or_return(fault)
        if method == "GET":
            if key not in self.properties:
                return error(404)
            return ok({"key": key, "value": self.properties[key]})
        if method == "PUT":
            created = key not in self.properties
            self.properties[key] = body
            # Jira: 201 Created on first write, 200 OK on replacement.
            return empty(status=201 if created else 200)
        raise AssertionError(f"unrouted property request: {method} {key}")

    @staticmethod
    def _raise_or_return(item: Any):
        if isinstance(item, BaseException):
            raise item
        return item


def make_client(
    runner: Any,
    *,
    sleeps: list[float] | None = None,
    cancelled: Any = None,
    max_attempts: int = 4,
) -> jira_mutations.JiraMutationClient:
    return jira_mutations.JiraMutationClient(
        cloud_id=CLOUD_ID,
        email=EMAIL,
        token=TOKEN,
        site_url=SITE_URL,
        http_runner=runner,
        sleep_fn=(sleeps if sleeps is not None else []).append,
        random_fn=lambda: 0.0,
        cancelled_fn=(cancelled or (lambda: False)),
        max_attempts=max_attempts,
    )


def config_text(
    *,
    writes_enabled: bool = True,
    allowed_operations: tuple[str, ...] = ("assign", "transition", "comment", "link"),
    transitions: Mapping[str, str] | None = None,
    status_category_map: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    lines = [
        "tracker:",
        "  kind: jira_cloud",
        "  jira_cloud:",
        f'    site_url: "{SITE_URL}"',
        f'    cloud_id: "{CLOUD_ID}"',
        f'    project_id: "{PROJECT_ID}"',
        '    project_key: "ABC"',
    ]
    status_map = {"in_progress": ("3",)} if status_category_map is None else status_category_map
    if status_map:
        lines.append("    status_category_map:")
        for category, ids in status_map.items():
            lines.append(f"      {category}:")
            for status_id in ids:
                lines.append(f'        - "{status_id}"')
    lines.append("    mutations:")
    lines.append(f"      writes_enabled: {'true' if writes_enabled else 'false'}")
    lines.append("      allowed_operations:")
    for operation in allowed_operations:
        lines.append(f"        - {operation}")
    resolved_transitions = {"in_progress": "31"} if transitions is None else transitions
    if resolved_transitions:
        lines.append("      transitions:")
        for category, transition_id in resolved_transitions.items():
            lines.append(f'        {category}: "{transition_id}"')
    return "\n".join(lines) + "\n"


def load_config(**kwargs: Any) -> Mapping[str, Any]:
    from code_mower import config as code_mower_config

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "code-mower.yml"
        path.write_text(config_text(**kwargs), encoding="utf-8")
        return code_mower_config.load_config(path)


def run_cli(
    args: list[str],
    *,
    runner: Any = None,
    config_kwargs: Mapping[str, Any] | None = None,
    with_credentials: bool = True,
) -> tuple[int, dict[str, Any], str]:
    """Run `code-mower tracker mutate` with an injected transport."""
    factory_runner = ExplodingHttp() if runner is None else runner

    def factory(**kwargs: Any) -> jira_mutations.JiraMutationClient:
        return make_client(factory_runner)

    env = (
        {"JIRA_API_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN} if with_credentials else {}
    )
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "code-mower.yml"
        path.write_text(config_text(**(config_kwargs or {})), encoding="utf-8")
        # An empty credential directory keeps profile discovery deterministic
        # regardless of what the developer machine happens to hold.
        profiles = Path(tmp) / "profiles"
        profiles.mkdir()
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = jira_mutations.main(
                [
                    args[0],
                    str(path),
                    *args[1:],
                    "--provider-config-dir",
                    str(profiles),
                    "--json",
                ],
                client_factory=factory,
                env=env,
            )
    text = out.getvalue()
    report = json.loads(text) if text.strip().startswith("{") else {}
    return code, report, err.getvalue()


def operation(report: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    for item in report["operations"]:
        if item["operation"] == name:
            return item
    raise AssertionError(f"operation {name} missing from report")


class GuardTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_touches_no_network(self) -> None:
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim"])
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "plan")
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["write_request_count"], 0)
        self.assertFalse(report["guards"]["apply_requested"])
        self.assertFalse(report["guards"]["writes_authorized"])
        self.assertEqual(report["guards"]["refusals"], ["apply_flag_missing"])
        self.assertEqual(operation(report, "assign")["status"], "planned")

    def test_writes_enabled_without_apply_still_only_plans(self) -> None:
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim"])
        self.assertEqual(code, 0)
        self.assertTrue(report["guards"]["writes_enabled"])
        self.assertEqual(report["mode"], "plan")
        self.assertEqual(report["write_request_count"], 0)

    def test_apply_without_configured_writes_enabled_refuses(self) -> None:
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--apply"],
            config_kwargs={"writes_enabled": False},
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["mode"], "plan")
        self.assertEqual(report["status"], "refused")
        self.assertEqual(report["write_request_count"], 0)
        self.assertEqual(report["guards"]["refusals"], ["writes_disabled"])
        self.assertEqual(operation(report, "assign")["reason"], "writes_disabled")
        self.assertIn("writes_enabled", report["next_action"])

    def test_operation_outside_allowed_operations_is_refused(self) -> None:
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--apply"],
            config_kwargs={"allowed_operations": ("comment",)},
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "refused")
        self.assertEqual(operation(report, "assign")["reason"], "operation_not_allowed")
        self.assertEqual(report["write_request_count"], 0)

    def test_capabilities_require_both_guards(self) -> None:
        from code_mower.tracker_contract import tracker_capabilities

        block = {"mutations": {"writes_enabled": True, "allowed_operations": ["assign"]}}
        self.assertFalse(tracker_capabilities("jira_cloud", block).can_apply_mutations)
        self.assertTrue(
            tracker_capabilities("jira_cloud", block, apply_requested=True).can_apply_mutations
        )
        disabled = {"mutations": {"writes_enabled": False, "allowed_operations": ["assign"]}}
        self.assertFalse(
            tracker_capabilities("jira_cloud", disabled, apply_requested=True).can_apply_mutations
        )

    def test_plan_reports_the_operations_that_will_never_exist(self) -> None:
        _, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim"])
        self.assertIn("delete", report["never_supported_operations"])
        self.assertIn("attachment", report["never_supported_operations"])
        self.assertIn("arbitrary_field_update", report["never_supported_operations"])
        self.assertEqual(report["gate_authority"], "github")
        self.assertEqual(report["gate_impact"], "none")


class TransportAllowListTests(unittest.TestCase):
    def test_read_client_still_refuses_every_write(self) -> None:
        client = jira_cloud.JiraReadClient(
            cloud_id=CLOUD_ID, email=EMAIL, token=TOKEN, http_runner=ExplodingHttp()
        )
        for method, path in (
            ("PUT", f"{ISSUE_PATH}/assignee"),
            ("POST", f"{ISSUE_PATH}/transitions"),
            ("POST", f"{ISSUE_PATH}/comment"),
            ("POST", f"{ISSUE_PATH}/remotelink"),
            ("PUT", LEDGER_PATH),
            ("DELETE", ISSUE_PATH),
        ):
            with self.assertRaises(ValueError):
                client._check_request_allowed(method, path)

    def test_mutation_client_allows_only_the_four_operations_and_its_keys(self) -> None:
        client = make_client(ExplodingHttp())
        for method, path in (
            ("GET", ISSUE_PATH),
            ("PUT", f"{ISSUE_PATH}/assignee"),
            ("POST", f"{ISSUE_PATH}/transitions"),
            ("POST", f"{ISSUE_PATH}/comment"),
            ("POST", f"{ISSUE_PATH}/remotelink"),
            ("PUT", LEDGER_PATH),
            ("PUT", claim_path("claimed")),
        ):
            client._check_request_allowed(method, path)

    def test_mutation_client_refuses_delete_and_arbitrary_writes(self) -> None:
        client = make_client(ExplodingHttp())
        forbidden = (
            ("DELETE", ISSUE_PATH),
            ("DELETE", f"{ISSUE_PATH}/remotelink"),
            ("DELETE", LEDGER_PATH),
            ("PUT", ISSUE_PATH),
            ("POST", f"{ISSUE_PATH}/attachments"),
            ("PUT", f"{ISSUE_PATH}/properties/other-key"),
            # A claim key is only writable in this module's exact shape.
            ("PUT", f"{ISSUE_PATH}/properties/code-mower-comment-v1."),
            ("PUT", f"{ISSUE_PATH}/properties/code-mower-comment-v1.nothex"),
            ("PUT", f"{ISSUE_PATH}/properties/code-mower-comment-v2.{'a' * 32}"),
            ("PUT", f"{ISSUE_PATH}/properties/code-mower-comment-v1.{'a' * 33}"),
            ("POST", "/rest/api/3/issue"),
            ("PUT", "/rest/api/3/project/10001"),
            ("POST", "/rest/api/3/issueLink"),
            ("PUT", "/rest/api/2/issue/ABC-1/assignee"),
        )
        for method, path in forbidden:
            with self.assertRaises(ValueError, msg=f"{method} {path}"):
                client._check_request_allowed(method, path)


class TransportRetryPolicyTests(unittest.TestCase):
    """A write Jira may already have committed is never retried blindly.

    Timeout, 429, and 5xx are all ambiguous: the request may have been lost
    on the way out, or Jira may have applied it and lost the response. For a
    comment post or a transition post there is no way to tell and no
    server-side idempotency key, so the transport attempts them exactly once
    and lets apply reconcile on the operator's next run.
    """

    AMBIGUOUS_FAILURES = {
        "timeout": lambda: [TimeoutError("timed out")] * 4,
        "rate_limited": lambda: [error(429, {"Retry-After": "1"})] * 4,
        "server_error": lambda: [error(503)] * 4,
    }

    def _attempt_count(self, route: tuple[str, str], responses: Any, call: Any) -> int:
        sleeps: list[float] = []
        runner = RouteHttp({route: responses})
        client = make_client(runner, sleeps=sleeps, max_attempts=4)
        with self.assertRaises(jira_cloud.JiraApiError):
            call(client)
        self.assertEqual(sleeps, [], "a single-attempt write must not back off")
        return len(runner.calls)

    def test_comment_post_is_attempted_once_after_an_ambiguous_failure(self) -> None:
        for label, responses in self.AMBIGUOUS_FAILURES.items():
            with self.subTest(failure=label):
                attempts = self._attempt_count(
                    ("POST", f"{ISSUE_PATH}/comment"),
                    responses(),
                    lambda client: client.add_templated_comment(
                        ISSUE, "claimed", pr_url="", fingerprint="a" * 32
                    ),
                )
                self.assertEqual(attempts, 1)

    def test_transition_post_is_attempted_once_after_an_ambiguous_failure(self) -> None:
        for label, responses in self.AMBIGUOUS_FAILURES.items():
            with self.subTest(failure=label):
                attempts = self._attempt_count(
                    ("POST", f"{ISSUE_PATH}/transitions"),
                    responses(),
                    lambda client: client.transition_issue(ISSUE, "31"),
                )
                self.assertEqual(attempts, 1)

    def test_reads_and_idempotent_writes_keep_the_retry_budget(self) -> None:
        """The narrowing is surgical: everything else still retries.

        Assignee is a whole-value PUT, the remote link is upserted by its
        deterministic globalId, and the ledger property is a whole-value PUT,
        so repeating any of them cannot produce a second effect.
        """
        cases = (
            (("GET", ISSUE_PATH), lambda client: client.get_issue_state(ISSUE)),
            (
                ("PUT", f"{ISSUE_PATH}/assignee"),
                lambda client: client.assign_issue(ISSUE, ACCOUNT_ID),
            ),
            (
                ("POST", f"{ISSUE_PATH}/remotelink"),
                lambda client: client.link_pull_request(ISSUE, PR_URL),
            ),
            (
                ("PUT", LEDGER_PATH),
                lambda client: client.set_mutation_ledger(ISSUE, {"entries": {}}),
            ),
        )
        for route, call in cases:
            with self.subTest(route=route):
                runner = RouteHttp({route: [error(503)] * 4})
                client = make_client(runner, sleeps=[], max_attempts=4)
                with self.assertRaises(jira_cloud.JiraApiError):
                    call(client)
                self.assertEqual(len(runner.calls), 4)

    def test_the_retry_policy_seam_names_only_the_unsafe_writes(self) -> None:
        client = make_client(ExplodingHttp(), max_attempts=4)
        for method, path in (
            ("POST", f"{ISSUE_PATH}/comment"),
            ("POST", f"{ISSUE_PATH}/transitions"),
        ):
            self.assertEqual(client._attempts_for(method, path), 1)
        for method, path in (
            ("GET", ISSUE_PATH),
            ("PUT", f"{ISSUE_PATH}/assignee"),
            ("POST", f"{ISSUE_PATH}/remotelink"),
            ("PUT", LEDGER_PATH),
        ):
            self.assertEqual(client._attempts_for(method, path), 4)

    def test_acquiring_a_claim_is_single_attempt_but_finalizing_is_not(self) -> None:
        """Only the acquire depends on a 201-versus-200 answer.

        A retried acquire would meet its own 201 as a 200 and conclude that
        some other apply owns the claim, so the comment would never post.
        Rewriting a key this process already holds has no such signal left.
        """
        client = make_client(ExplodingHttp(), max_attempts=4)
        path = claim_path("claimed")
        self.assertEqual(client._attempts_for("PUT", path, "commentClaimAcquire"), 1)
        self.assertEqual(client._attempts_for("PUT", path, "commentClaimFinalize"), 4)

    def test_a_claim_acquire_is_attempted_once_after_an_ambiguous_failure(self) -> None:
        for label, responses in self.AMBIGUOUS_FAILURES.items():
            with self.subTest(failure=label):
                attempts = self._attempt_count(
                    ("PUT", claim_path("claimed")),
                    responses(),
                    lambda client: client.acquire_comment_claim(
                        ISSUE,
                        comment_fingerprint("claimed"),
                        template="claimed",
                        owner="0" * 16,
                    ),
                )
                self.assertEqual(attempts, 1)

    def test_only_a_created_property_acquires_the_claim(self) -> None:
        """201 means this request created the key; 200 means it did not."""
        fingerprint = comment_fingerprint("claimed")
        for status, acquired in ((201, True), (200, False)):
            with self.subTest(status=status):
                runner = RouteHttp(
                    {("PUT", claim_path("claimed")): empty(status=status)}
                )
                client = make_client(runner)
                self.assertEqual(
                    client.acquire_comment_claim(
                        ISSUE, fingerprint, template="claimed", owner="0" * 16
                    ),
                    acquired,
                )

    def test_the_read_client_retries_every_request_it_can_make(self) -> None:
        client = jira_cloud.JiraReadClient(
            cloud_id=CLOUD_ID,
            email=EMAIL,
            token=TOKEN,
            http_runner=ExplodingHttp(),
            max_attempts=4,
        )
        self.assertEqual(client._attempts_for("GET", ISSUE_PATH), 4)
        self.assertEqual(client._attempts_for("POST", "/rest/api/3/search/jql"), 4)


class ApplyTests(unittest.TestCase):
    def test_claim_assigns_the_authenticated_account(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok(
                    {"accountId": ACCOUNT_ID, "emailAddress": EMAIL, "displayName": "QA"}
                ),
                ("PUT", f"{ISSUE_PATH}/assignee"): empty(),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "apply")
        self.assertEqual(report["status"], "applied")
        self.assertEqual(operation(report, "assign")["status"], "applied")
        assign_call = next(
            call for call in runner.calls if call["path"].endswith("/assignee")
        )
        self.assertEqual(assign_call["method"], "PUT")
        self.assertEqual(assign_call["body"], {"accountId": ACCOUNT_ID})

    def test_claim_is_idempotent_when_already_assigned(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(assignee=ACCOUNT_ID)),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "assign")["status"], "already_applied")
        self.assertEqual(operation(report, "assign")["reason"], "already_assigned")
        self.assertNotIn(f"{ISSUE_PATH}/assignee", runner.paths("PUT"))

    def test_reassignment_happens_when_another_account_holds_the_issue(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(assignee=OTHER_ACCOUNT_ID)),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", f"{ISSUE_PATH}/assignee"): empty(),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        _, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(operation(report, "assign")["status"], "applied")

    def test_configured_transition_is_verified_against_the_live_issue(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "name": "Start", "to": {"id": "3"}}]}
                ),
                ("POST", f"{ISSUE_PATH}/transitions"): empty(),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "transition")["status"], "applied")
        post = next(
            call
            for call in runner.calls
            if call["method"] == "POST" and call["path"].endswith("/transitions")
        )
        self.assertEqual(post["body"], {"transition": {"id": "31"}})

    def test_unconfigured_transition_category_is_refused_without_network(self) -> None:
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "done", "--apply"],
        )
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "transition")["reason"], "transition_not_configured")
        self.assertEqual(report["write_request_count"], 0)

    def test_workflow_drift_blocks_instead_of_guessing(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "99", "name": "Other", "to": {"id": "5"}}]}
                ),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(operation(report, "transition")["reason"], "transition_unavailable")
        self.assertEqual(runner.write_calls(), [])

    def test_transition_landing_outside_the_configured_target_is_blocked(self) -> None:
        """A configured transition id is only a workflow edge.

        The workflow can be re-pointed under it, so where the edge actually
        lands is verified against the lifecycle category's configured status
        ids before any write.
        """
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "name": "Start", "to": {"id": "9"}}]}
                ),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            operation(report, "transition")["reason"], "transition_target_mismatch"
        )
        self.assertEqual(
            operation(report, "transition")["detail"]["destination_status_id"], "9"
        )
        self.assertEqual(runner.write_calls(), [])

    def test_a_transition_with_no_configured_target_status_is_blocked(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "to": {"id": "3"}}]}
                ),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
            config_kwargs={"status_category_map": {}},
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            operation(report, "transition")["reason"], "target_status_not_configured"
        )
        self.assertEqual(runner.write_calls(), [])

    def test_a_transition_with_an_unknown_destination_is_blocked(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "name": "Start"}]}
                ),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            operation(report, "transition")["reason"], "transition_target_mismatch"
        )
        self.assertEqual(runner.write_calls(), [])

    def test_a_self_transition_outside_the_target_ids_blocks_without_writing(
        self,
    ) -> None:
        """An edge that lands on the current status is still a destination.

        The issue sits on status 5, which the requested category does not
        name a target, and the configured edge is re-pointed to land back on
        status 5. Destination equals current status, but "where the issue
        already is" was never the target, so this must block on the
        destination check rather than report already-at-target success.
        """
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="5")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "name": "Start", "to": {"id": "5"}}]}
                ),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            operation(report, "transition")["reason"], "transition_target_mismatch"
        )
        self.assertEqual(
            operation(report, "transition")["detail"]["destination_status_id"], "5"
        )
        # Nothing was written, and nothing was recorded as an applied effect.
        self.assertEqual(runner.write_calls(), [])

    def test_transition_is_idempotent_at_the_configured_target_status(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="3")),
                ("GET", LEDGER_PATH): error(404),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "transition")["reason"], "already_at_target_status")
        self.assertNotIn(f"{ISSUE_PATH}/transitions", runner.paths("POST"))

    def test_templated_comment_posts_bounded_adf_with_a_replay_marker(self) -> None:
        runner = FakeJira()
        code, report, _ = run_cli(
            [
                "mutate",
                "--issue",
                ISSUE,
                "--comment",
                "pr_opened",
                "--pr-url",
                PR_URL,
                "--apply",
            ],
            runner=runner,
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "applied")
        post = runner.comment_posts()[0]
        body = post["body"]["body"]
        self.assertEqual(body["type"], "doc")
        self.assertEqual(body["version"], 1)
        rendered = " ".join(
            node["text"] for block in body["content"] for node in block["content"]
        )
        self.assertIn(PR_URL, rendered)
        self.assertIn("idempotency marker", rendered)
        self.assertNotIn(PROSE, rendered)
        self.assertLessEqual(
            len(rendered), jira_mutations.MAX_COMMENT_CHARACTERS + 80
        )

    def test_comment_rejects_free_form_and_unknown_templates(self) -> None:
        with self.assertRaises(jira_mutations.MutationRequestError):
            jira_mutations.render_comment("ship it now")
        with self.assertRaises(jira_mutations.MutationRequestError):
            jira_mutations.render_comment("pr_opened", "https://example.com/evil")

    def test_pull_request_remote_link_uses_a_deterministic_global_id(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/remotelink"): ok([]),
                ("POST", f"{ISSUE_PATH}/remotelink"): ok({"id": 900}, status=201),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--link-pr", "--pr-url", PR_URL, "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "link")["status"], "applied")
        post = next(
            call
            for call in runner.calls
            if call["method"] == "POST" and call["path"].endswith("/remotelink")
        )
        self.assertEqual(
            post["body"]["globalId"], "code-mower:github:owner/repo/pull/12"
        )
        self.assertEqual(post["body"]["object"]["url"], PR_URL)
        self.assertEqual(post["body"]["object"]["title"], "owner/repo#12")
        self.assertLessEqual(len(post["body"]["globalId"]), jira_cloud.MAX_GLOBAL_ID_LENGTH)

    def test_existing_remote_link_is_not_duplicated(self) -> None:
        global_id = jira_mutations.remote_link_global_id("owner", "repo", "12")
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/remotelink"): ok({"id": 900, "globalId": global_id}),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--link-pr", "--pr-url", PR_URL, "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "link")["reason"], "already_linked")
        self.assertNotIn(f"{ISSUE_PATH}/remotelink", runner.paths("POST"))

    def test_issue_outside_the_configured_project_is_blocked(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(project_id="99999")),
                ("GET", LEDGER_PATH): error(404),
            }
        )
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "assign")["reason"], "issue_out_of_scope")
        self.assertEqual(runner.write_calls(), [])


class CommentClaimTests(unittest.TestCase):
    """At-most-once comment delivery, keyed by a dedicated claim property.

    The shared ledger is deliberately not the primitive here. It is bounded
    and evicting, and two applies that both read it absent would both post.
    """

    ARGS = ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"]

    def test_a_first_comment_claims_its_key_then_posts_then_finalizes(self) -> None:
        jira = FakeJira()
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "applied")
        self.assertEqual(len(jira.comment_posts()), 1)
        self.assertEqual(
            [call["path"] for call in jira.write_calls()],
            [claim_path("claimed"), f"{ISSUE_PATH}/comment", claim_path("claimed")],
        )
        claim = jira.claim("claimed")
        self.assertEqual(claim["state"], "posted")
        self.assertEqual(claim["schema"], jira_mutations.COMMENT_CLAIM_SCHEMA)
        self.assertEqual(claim["template"], "claimed")
        self.assertEqual(operation(report, "comment")["detail"]["claim_state"], "posted")

    def test_the_claim_is_written_before_the_post(self) -> None:
        order: list[str] = []
        jira = FakeJira(
            before_request=lambda _fake, method, path: order.append(f"{method} {path}")
            if method in ("PUT", "POST")
            else None
        )
        run_cli(self.ARGS, runner=jira)
        self.assertEqual(order[0], f"PUT {claim_path('claimed')}")
        self.assertEqual(order[1], f"POST {ISSUE_PATH}/comment")

    def test_a_posted_claim_is_never_reposted(self) -> None:
        jira = FakeJira()
        run_cli(self.ARGS, runner=jira)
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "already_applied")
        self.assertEqual(operation(report, "comment")["reason"], "already_commented")
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_an_unfinalized_claim_reports_unverified_and_never_reposts(self) -> None:
        """A claim with no recorded post outcome means "unknown", truthfully.

        The comment may have committed, may have been lost in flight, or may
        never have left. Reposting risks a duplicate and claiming success
        would be a guess, so the run hands that one comment to an owner.
        """
        jira = FakeJira()
        key = jira_mutations.comment_claim_property_key(comment_fingerprint("claimed"))
        jira.properties[key] = {
            "schema": jira_mutations.COMMENT_CLAIM_SCHEMA,
            "operation": "comment",
            "fingerprint": comment_fingerprint("claimed"),
            "template": "claimed",
            "state": "claimed",
            "owner": "0" * 16,
            "at": "2026-09-08T00:00:00+00:00",
        }
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertEqual(jira.comment_posts(), [])
        self.assertIn("by hand", report["next_action"])

    def test_an_unreadable_claim_value_is_treated_as_held_not_absent(self) -> None:
        jira = FakeJira()
        key = jira_mutations.comment_claim_property_key(comment_fingerprint("claimed"))
        jira.properties[key] = {"state": "something-else"}
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["status"], "unverified")
        self.assertEqual(operation(report, "comment")["detail"]["claim_state"], "unknown")
        self.assertEqual(jira.comment_posts(), [])

    def test_an_ambiguous_post_stays_claimed_unverified_and_is_never_retried(self) -> None:
        jira = FakeJira(faults={("POST", "comment"): [TimeoutError("lost")] * 4})
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["status"], "unverified")
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertEqual(operation(report, "comment")["detail"]["post_error"], "unavailable")
        # Exactly one post attempt: an ambiguous comment is never retried.
        self.assertEqual(len(jira.comment_posts()), 1)
        self.assertEqual(jira.claim("claimed")["state"], "unverified")

        # And a later run reads that claim and still refuses to repost.
        jira.faults = {}
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_a_rejected_post_is_unverified_rather_than_silently_dropped(self) -> None:
        jira = FakeJira(faults={("POST", "comment"): [error(403)]})
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["status"], "unverified")
        self.assertEqual(
            operation(report, "comment")["detail"]["post_error"], "permission_denied"
        )
        self.assertEqual(jira.claim("claimed")["state"], "unverified")

    def test_a_failed_finalize_leaves_the_claim_and_never_reposts(self) -> None:
        """Losing the record must never lose the protection."""
        jira = FakeJira(
            faults={("PUT", "comment_claim"): [None, *([error(500)] * 4)]}
        )
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "applied")
        self.assertEqual(operation(report, "comment")["detail"]["claim_state"], "claimed")
        self.assertEqual(jira.claim("claimed")["state"], "claimed")

        jira.faults = {}
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_an_ambiguous_claim_that_landed_still_never_posts(self) -> None:
        """A lost claim response is never recovered into a right to post.

        The claim write is attempted exactly once, so its answer is
        ambiguous rather than retried, and the answer it lost -- 201 created
        versus 200 replaced -- is the only thing that authorizes a post.
        Reading this attempt's own owner token back proves the write landed
        and nothing more, so the run reports the claim as held-unconfirmed
        and hands it to an owner.
        """
        jira = FakeJira()

        def lose_the_response(fake: FakeJira, method: str, path: str) -> None:
            if method == "PUT" and path == claim_path("claimed") and not fake.properties:
                fake._property("PUT", path.split("/properties/")[1], fake.calls[-1]["body"])
                raise TimeoutError("response lost after Jira committed")

        jira.before_request = lose_the_response
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["status"], "unverified")
        self.assertEqual(
            operation(report, "comment")["reason"], "comment_claim_unconfirmed"
        )
        self.assertEqual(
            operation(report, "comment")["detail"]["claim_state"], "claimed_unconfirmed"
        )
        self.assertEqual(jira.comment_posts(), [])
        # The report tells an owner to look at the issue once by hand.
        self.assertIn("by hand", report["next_action"])

        # The claim stays held, so no later run reposts it either.
        jira.before_request = None
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertEqual(jira.comment_posts(), [])

    def test_mixed_case_pull_request_urls_replay_onto_one_claim(self) -> None:
        """Two spellings of one pull request are one comment intent."""
        lower = "https://github.com/owner/repo/pull/12"
        upper = "https://github.com/Owner/Repo/pull/12"
        jira = FakeJira()

        def args(pr_url: str) -> list[str]:
            return [
                "mutate", "--issue", ISSUE,
                "--comment", "pr_opened",
                "--pr-url", pr_url,
                "--apply",
            ]

        code, report, _ = run_cli(args(lower), runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "applied")

        code, report, _ = run_cli(args(upper), runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "already_applied")
        self.assertEqual(operation(report, "comment")["reason"], "already_commented")
        # One intent, one claim key, one comment -- not two of each.
        self.assertEqual(len(jira.comment_posts()), 1)
        self.assertEqual(len(jira.properties), 1)
        self.assertEqual(jira.claim("pr_opened", lower)["state"], "posted")

    def test_an_ambiguous_claim_that_never_landed_posts_nothing(self) -> None:
        jira = FakeJira(faults={("PUT", "comment_claim"): [TimeoutError("lost")]})
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["reason"], "unavailable")
        self.assertEqual(operation(report, "comment")["detail"]["claim_state"], "unknown")
        self.assertEqual(jira.comment_posts(), [])

        # Nothing was claimed, so a clean re-run posts exactly once.
        jira.faults = {}
        code, report, _ = run_cli(self.ARGS, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_a_failed_earlier_operation_leaves_no_claim_behind(self) -> None:
        """A never-attempted comment must not be suppressed on the retry."""
        args = [
            "mutate", "--issue", ISSUE,
            "--transition", "in_progress",
            "--comment", "claimed",
            "--apply",
        ]
        jira = FakeJira(status_id="1", faults={("POST", "transitions"): [error(500)]})
        code, report, _ = run_cli(args, runner=jira)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["status"], "skipped")
        self.assertEqual(jira.properties, {})

        jira.faults = {}
        code, report, _ = run_cli(args, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "applied")
        self.assertEqual(len(jira.comment_posts()), 1)


class CommentConcurrencyTests(unittest.TestCase):
    """Two applies of the same comment intent produce at most one post.

    Each test interleaves a second client inside the first client's request
    stream against one shared Jira, which is the interleaving that made the
    shared ledger unsafe: both runs read it absent, and both posted.
    """

    ARGS = ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"]
    MARKER = claim_path("claimed")

    def _interleave(self, trigger_method: str, trigger_path: str) -> FakeJira:
        jira = FakeJira()
        state = {"reentered": False, "second": None}

        def interleave(fake: FakeJira, method: str, path: str) -> None:
            if state["reentered"] or method != trigger_method or path != trigger_path:
                return
            state["reentered"] = True
            # The competing apply runs to completion against the same Jira
            # before this one continues past the triggering request.
            state["second"] = run_cli(self.ARGS, runner=fake)

        jira.before_request = interleave
        self.first = run_cli(self.ARGS, runner=jira)
        self.second = state["second"]
        self.assertIsNotNone(self.second, "the competing apply never ran")
        return jira

    def test_a_second_apply_between_the_claim_read_and_the_claim_write(self) -> None:
        # The competing apply runs after this one has already read the claim
        # as absent and is about to write it. Under the shared ledger this is
        # the interleaving where both runs posted.
        jira = self._interleave("PUT", self.MARKER)
        # Exactly one comment reached Jira, and exactly one claim was created.
        self.assertEqual(len(jira.comment_posts()), 1)
        self.assertEqual(len(jira.properties), 1)
        # The competing run won the claim and posted; this one lost it and
        # never posted, and says so instead of guessing either way.
        self.assertEqual(self.second[0], 0)
        self.assertEqual(operation(self.second[1], "comment")["status"], "applied")
        self.assertEqual(self.first[0], 1)
        self.assertEqual(operation(self.first[1], "comment")["status"], "unverified")
        self.assertEqual(
            operation(self.first[1], "comment")["reason"], "comment_claim_held"
        )
        self.assertEqual(
            operation(self.first[1], "comment")["detail"]["claim_state"],
            "held_by_another_apply",
        )

    def test_an_overwriting_claim_with_a_lost_response_posts_nothing(self) -> None:
        """The interleaving an owner-token readback cannot survive.

        The second apply reads the claim absent, then the first apply runs
        to completion: it creates the claim, posts, and finalizes to
        ``posted``. The second apply's ``PUT`` then lands as a **200** that
        overwrites that finished claim, and its response is lost. Reading
        the claim back now finds the second apply's own owner token -- which
        proves only that its write landed, not that it created anything. A
        run that treats that as its lost 201 posts a duplicate comment.
        """
        jira = FakeJira()
        state: dict[str, Any] = {"reentered": False, "first": None}

        def overwrite_then_lose_the_response(
            fake: FakeJira, method: str, path: str
        ) -> None:
            if state["reentered"] or method != "PUT" or path != self.MARKER:
                return
            state["reentered"] = True
            # This apply's claim body, captured before the competing run
            # appends its own calls.
            body = fake.calls[-1]["body"]
            state["first"] = run_cli(self.ARGS, runner=fake)
            # Jira commits this apply's PUT as a 200 replacement of the
            # finished claim, then the response never arrives.
            fake._property("PUT", path.split("/properties/")[1], body)
            raise TimeoutError("response lost after Jira committed the overwrite")

        jira.before_request = overwrite_then_lose_the_response
        second = run_cli(self.ARGS, runner=jira)
        first = state["first"]
        self.assertIsNotNone(first, "the competing apply never ran")

        # Exactly one comment reached Jira, across both applies.
        self.assertEqual(len(jira.comment_posts()), 1)
        self.assertEqual(first[0], 0)
        self.assertEqual(operation(first[1], "comment")["status"], "applied")
        # The overwrite really happened: the stored claim is the second
        # apply's, so its owner token matches on readback.
        self.assertEqual(jira.claim("claimed")["state"], "claimed")
        # And it still refused to post, truthfully, for an owner to settle.
        self.assertEqual(second[0], 1)
        self.assertEqual(operation(second[1], "comment")["status"], "unverified")
        self.assertEqual(
            operation(second[1], "comment")["reason"], "comment_claim_unconfirmed"
        )
        self.assertEqual(
            operation(second[1], "comment")["detail"]["claim_state"],
            "claimed_unconfirmed",
        )
        self.assertIn("by hand", second[1]["next_action"])

    def test_a_second_apply_between_the_claim_write_and_the_post(self) -> None:
        jira = self._interleave("POST", f"{ISSUE_PATH}/comment")
        self.assertEqual(len(jira.comment_posts()), 1)
        # The competing run saw a claim it did not own and refused to post.
        self.assertEqual(self.second[0], 1)
        self.assertEqual(operation(self.second[1], "comment")["status"], "unverified")
        self.assertEqual(self.first[0], 0)
        self.assertEqual(operation(self.first[1], "comment")["status"], "applied")


class CommentCapacityTests(unittest.TestCase):
    """Replay protection must not evict, at any number of comments.

    The shared ledger holds 32 entries and drops the oldest. With comment
    protection living there, comment 33 evicted comment 1 and a replay of
    comment 1 posted a duplicate. One property per intent cannot evict.
    """

    COUNT = jira_mutations.MAX_LEDGER_ENTRIES + 8

    @staticmethod
    def _args(index: int) -> list[str]:
        return [
            "mutate", "--issue", ISSUE,
            "--comment", "pr_opened",
            "--pr-url", f"https://github.com/owner/repo/pull/{index}",
            "--apply",
        ]

    def test_replay_is_still_suppressed_far_past_the_ledger_bound(self) -> None:
        jira = FakeJira()
        for index in range(1, self.COUNT + 1):
            code, report, _ = run_cli(self._args(index), runner=jira)
            self.assertEqual(code, 0, f"comment {index} did not apply")
            self.assertEqual(operation(report, "comment")["status"], "applied")
        self.assertEqual(len(jira.comment_posts()), self.COUNT)
        self.assertEqual(len(jira.properties), self.COUNT)

        # The very first intent is far outside anything a 32-entry ledger
        # could still remember, and it is still never reposted.
        for index in (1, 2, self.COUNT):
            code, report, _ = run_cli(self._args(index), runner=jira)
            self.assertEqual(code, 0)
            self.assertEqual(operation(report, "comment")["reason"], "already_commented")
        self.assertEqual(len(jira.comment_posts()), self.COUNT)

    def test_the_advisory_ledger_still_evicts_and_that_is_harmless(self) -> None:
        """Nothing the ledger holds decides whether a write happens."""
        self.assertEqual(
            set(jira_mutations.LEDGER_OPERATIONS), {"assign", "transition", "link"}
        )
        entries = {
            f"{index:032x}": {
                "operation": "assign",
                "state": "applied",
                "at": f"2026-09-08T00:00:{index % 60:02d}+00:00",
            }
            for index in range(jira_mutations.MAX_LEDGER_ENTRIES + 5)
        }
        cleaned = jira_mutations._validated_ledger({"entries": entries})
        self.assertEqual(len(cleaned["entries"]), jira_mutations.MAX_LEDGER_ENTRIES)
        # Ties on the same timestamp evict in a defined order rather than
        # whichever key the mapping happened to yield first.
        again = jira_mutations._validated_ledger(
            {"entries": dict(reversed(list(entries.items())))}
        )
        self.assertEqual(list(cleaned["entries"]), list(again["entries"]))

    def test_a_comment_entry_never_enters_the_shared_ledger(self) -> None:
        jira = FakeJira()
        run_cli(["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=jira)
        self.assertNotIn(jira_mutations.LEDGER_PROPERTY_KEY, jira.properties)
        cleaned = jira_mutations._validated_ledger(
            {
                "entries": {
                    "a" * 32: {"operation": "comment", "state": "applied", "at": "x"}
                }
            }
        )
        self.assertEqual(cleaned["entries"], {})


class IssueIdentityReplayTests(unittest.TestCase):
    """Fingerprints follow the immutable issue id, not the caller's spelling."""

    def _args(self, ref: str) -> list[str]:
        return ["mutate", "--issue", ref, "--comment", "claimed", "--apply"]

    def test_the_same_issue_by_key_then_by_id_posts_once(self) -> None:
        jira = FakeJira()
        code, report, _ = run_cli(self._args(ISSUE), runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["status"], "applied")

        code, report, _ = run_cli(self._args(ISSUE_ID), runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["reason"], "already_commented")
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_a_lowercase_key_resolves_to_the_same_replay_identity(self) -> None:
        jira = FakeJira(key="ABC-1")
        run_cli(self._args("ABC-1"), runner=jira)
        code, report, _ = run_cli(self._args("abc-1"), runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["reason"], "already_commented")
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_a_plan_built_from_a_key_says_its_fingerprints_are_provisional(self) -> None:
        by_key = jira_mutations.build_mutation_plan(
            load_config(),
            jira_mutations.MutationRequest(issue_ref=ISSUE, comment_template="claimed"),
        )
        by_id = jira_mutations.build_mutation_plan(
            load_config(),
            jira_mutations.MutationRequest(issue_ref=ISSUE_ID, comment_template="claimed"),
        )
        self.assertEqual(by_key["tracker"]["fingerprint_basis"], "issue_ref_provisional")
        self.assertEqual(by_id["tracker"]["fingerprint_basis"], "issue_id")
        # Planning performs no Jira call, so it is deterministic per input.
        repeat = jira_mutations.build_mutation_plan(
            load_config(),
            jira_mutations.MutationRequest(issue_ref=ISSUE, comment_template="claimed"),
        )
        self.assertEqual(
            operation(by_key, "comment")["fingerprint"],
            operation(repeat, "comment")["fingerprint"],
        )

    def test_apply_rewrites_the_fingerprint_to_the_live_issue_id(self) -> None:
        jira = FakeJira()
        plan = jira_mutations.build_mutation_plan(
            load_config(),
            jira_mutations.MutationRequest(issue_ref=ISSUE, comment_template="claimed"),
            apply_requested=True,
        )
        planned = operation(plan, "comment")["fingerprint"]
        report = jira_mutations.apply_mutation_plan(plan, make_client(jira))
        applied = operation(report, "comment")["fingerprint"]
        self.assertNotEqual(planned, applied)
        self.assertEqual(applied, comment_fingerprint("claimed"))
        self.assertEqual(report["tracker"]["fingerprint_basis"], "issue_id")
        self.assertEqual(report["tracker"]["issue_id"], ISSUE_ID)

    def test_an_issue_with_no_resolvable_id_blocks_before_any_write(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(
                    {"key": ISSUE, "fields": {"project": {"id": PROJECT_ID}}}
                ),
                ("GET", LEDGER_PATH): error(404),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=runner
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            operation(report, "comment")["reason"], "issue_identity_unresolved"
        )
        self.assertEqual(runner.write_calls(), [])

    def test_comment_fingerprints_are_semantic_not_rendered_prose(self) -> None:
        """The replay key is a template id plus a canonical PR identity.

        Hashing the rendered sentence instead would tie replay protection to
        wording: editing a template in this file would change every key and
        silently unprotect comments an earlier build already posted.
        """
        subject = jira_mutations.fingerprint_subject(ISSUE_ID)
        self.assertTrue(subject.isupper() or subject.isdigit())
        self.assertEqual(
            jira_mutations._operation_fingerprint(
                "comment", subject, {"template": "pr_opened", "pr_url": PR_URL}
            ),
            jira_mutations.mutation_fingerprint(
                "comment",
                {
                    "issue": subject,
                    "template": "pr_opened",
                    "pull_request": jira_mutations.remote_link_global_id(
                        "owner", "repo", "12"
                    ),
                },
            ),
        )

    def test_mixed_case_pull_request_spellings_share_one_claim_key(self) -> None:
        """GitHub owner and repository casing is not part of PR identity."""
        subject = jira_mutations.fingerprint_subject(ISSUE_ID)
        spellings = (
            "https://github.com/owner/repo/pull/12",
            "https://github.com/Owner/Repo/pull/12",
            "https://github.com/OWNER/REPO/pull/12",
        )
        fingerprints = {
            jira_mutations._operation_fingerprint(
                "comment", subject, {"template": "pr_opened", "pr_url": pr_url}
            )
            for pr_url in spellings
        }
        self.assertEqual(len(fingerprints), 1)
        keys = {
            jira_mutations.comment_claim_property_key(value) for value in fingerprints
        }
        self.assertEqual(len(keys), 1)
        # A different pull request on the same repository still differs.
        self.assertNotIn(
            jira_mutations._operation_fingerprint(
                "comment",
                subject,
                {"template": "pr_opened", "pr_url": "https://github.com/owner/repo/pull/13"},
            ),
            fingerprints,
        )


class WriteAttemptAccountingTests(unittest.TestCase):
    """Every write attempt is counted, including the ones that failed.

    An attempt Jira may have committed and then failed to acknowledge changed
    Jira just as much as one that answered 201, so counting only successful
    calls understates what a run may have done.
    """

    def test_a_successful_comment_counts_claim_post_and_finalize(self) -> None:
        jira = FakeJira()
        _, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=jira
        )
        self.assertEqual(report["write_request_count"], 3)
        self.assertEqual(len(jira.write_calls()), 3)

    def test_a_timed_out_write_counts_every_attempt_it_made(self) -> None:
        jira = FakeJira(faults={("PUT", "assignee"): [TimeoutError("lost")] * 4})
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=jira
        )
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "assign")["reason"], "unavailable")
        # Four transport attempts left this process, and all four could have
        # reached Jira. The report says four, not zero.
        self.assertEqual(len(jira.paths("PUT")), 4)
        self.assertEqual(report["write_request_count"], 4)

    def test_a_retried_write_that_finally_succeeds_counts_each_attempt(self) -> None:
        jira = FakeJira(faults={("PUT", "assignee"): [error(503), error(503), None]})
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=jira
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "assign")["status"], "applied")
        # Two failures, one success, then the ledger write.
        self.assertEqual(report["write_request_count"], 4)

    def test_an_ambiguous_comment_post_is_counted_once_and_only_once(self) -> None:
        jira = FakeJira(faults={("POST", "comment"): [TimeoutError("lost")] * 4})
        _, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=jira
        )
        # Claim, the single post attempt, and the unverified finalize.
        self.assertEqual(report["write_request_count"], 3)
        self.assertEqual(len(jira.comment_posts()), 1)

    def test_reads_are_never_counted_as_writes(self) -> None:
        jira = FakeJira(assignee=ACCOUNT_ID, status_id="3")
        args = ["mutate", "--issue", ISSUE, "--claim", "--apply"]
        code, report, _ = run_cli(args, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "assign")["reason"], "already_assigned")
        # The issue was already claimed, so the only write is the advisory
        # ledger noting that verified state for the first time.
        self.assertEqual(report["write_request_count"], 1)

        code, report, _ = run_cli(args, runner=jira)
        self.assertEqual(code, 0)
        self.assertEqual(report["ledger_status"], "unchanged")
        # A second reconciliation reads the same live state and writes
        # nothing at all, however many reads that took.
        self.assertGreater(len(jira.paths("GET")), 0)
        self.assertEqual(report["write_request_count"], 0)

    def test_the_counter_lives_at_the_transport_attempt_boundary(self) -> None:
        client = make_client(FakeJira())
        self.assertEqual(client.write_attempts, 0)
        client._on_request_attempt("GET", ISSUE_PATH)
        client._on_request_attempt("POST", "/rest/api/3/search/jql")
        self.assertEqual(client.write_attempts, 0)
        client._on_request_attempt("POST", f"{ISSUE_PATH}/comment")
        client._on_request_attempt("PUT", LEDGER_PATH)
        self.assertEqual(client.write_attempts, 2)

    def test_a_refusal_reports_zero_and_issues_no_request(self) -> None:
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--apply"],
            config_kwargs={"writes_enabled": False},
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["write_request_count"], 0)


class FingerprintTests(unittest.TestCase):
    def test_fingerprints_are_stable_across_key_order(self) -> None:
        payload = {"issue": ISSUE_ID, "template": "claimed", "text": "x"}
        self.assertEqual(
            jira_mutations.mutation_fingerprint("comment", payload),
            jira_mutations.mutation_fingerprint("comment", dict(reversed(list(payload.items())))),
        )

    def test_distinct_intents_get_distinct_claim_keys(self) -> None:
        keys = {
            jira_mutations.comment_claim_property_key(
                comment_fingerprint("pr_opened", f"https://github.com/owner/repo/pull/{n}")
            )
            for n in range(1, 6)
        }
        self.assertEqual(len(keys), 5)
        for key in keys:
            self.assertEqual(key, jira_cloud.validate_property_key(key))
            self.assertLessEqual(len(key), jira_cloud.MAX_PROPERTY_KEY_LENGTH)

    def test_a_malformed_fingerprint_cannot_become_a_property_key(self) -> None:
        for candidate in ("", "zz", "../admin", "A" * 32):
            with self.assertRaises(jira_mutations.MutationRequestError):
                jira_mutations.comment_claim_property_key(candidate)

    def test_ledger_drops_unknown_and_oversized_entries(self) -> None:
        entries = {
            f"{index:032x}": {
                "operation": "assign",
                "state": "applied",
                "at": f"2026-09-08T00:00:{index:02d}+00:00",
            }
            for index in range(jira_mutations.MAX_LEDGER_ENTRIES + 5)
        }
        entries["not-a-fingerprint"] = {"operation": "assign", "state": "applied", "at": "x"}
        entries["a" * 32] = {"operation": "delete", "state": "applied", "at": "x"}
        cleaned = jira_mutations._validated_ledger({"entries": entries, "extra": PROSE})
        self.assertEqual(set(cleaned), {"schema", "entries"})
        self.assertEqual(len(cleaned["entries"]), jira_mutations.MAX_LEDGER_ENTRIES)
        self.assertNotIn("not-a-fingerprint", cleaned["entries"])
        self.assertNotIn("a" * 32, cleaned["entries"])


class FullRunTests(unittest.TestCase):
    """One command exercising all four operations, then its exact replay."""

    ARGS = [
        "mutate",
        "--issue",
        ISSUE,
        "--claim",
        "--transition",
        "in_progress",
        "--link-pr",
        "--comment",
        "pr_opened",
        "--pr-url",
        PR_URL,
    ]

    def _first_apply(self) -> FakeJira:
        runner = FakeJira(status_id="1")
        code, report, _ = run_cli([*self.ARGS, "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "applied")
        self.assertEqual(
            [item["status"] for item in report["operations"]], ["applied"] * 4
        )
        return runner

    def test_first_apply_performs_each_operation_once(self) -> None:
        runner = self._first_apply()
        claim = claim_path("pr_opened", PR_URL)
        self.assertEqual(
            [call["path"] for call in runner.write_calls()],
            [
                f"{ISSUE_PATH}/assignee",
                f"{ISSUE_PATH}/transitions",
                f"{ISSUE_PATH}/remotelink",
                claim,
                f"{ISSUE_PATH}/comment",
                claim,
                LEDGER_PATH,
            ],
        )
        # The shared ledger records only the three live-reconcilable effects.
        ledger = runner.properties[jira_mutations.LEDGER_PROPERTY_KEY]
        self.assertEqual(
            sorted(entry["operation"] for entry in ledger["entries"].values()),
            ["assign", "link", "transition"],
        )

    def test_replay_after_a_completed_run_writes_nothing(self) -> None:
        # The same Jira, carrying everything the first run left behind.
        runner = self._first_apply()
        before = len(runner.write_calls())
        code, report, _ = run_cli([*self.ARGS, "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "already_applied")
        self.assertEqual(
            [item["status"] for item in report["operations"]], ["already_applied"] * 4
        )
        self.assertEqual(report["write_request_count"], 0)
        self.assertEqual(len(runner.write_calls()), before)
        self.assertEqual(len(runner.comment_posts()), 1)

    def test_human_output_states_both_guards_and_the_gate_owner(self) -> None:
        report = jira_mutations.build_mutation_plan(
            load_config(),
            jira_mutations.MutationRequest(issue_ref=ISSUE, claim=True),
        )
        text = jira_mutations._render_text(report)
        self.assertIn("Writes enabled (config): true", text)
        self.assertIn("Apply requested (runtime): false", text)
        self.assertIn("Jira write attempts: 0", text)
        self.assertIn("Gate authority: github", text)
        self.assertIn("- assign: planned (ok)", text)


class FailureSemanticsTests(unittest.TestCase):
    def _claim_runner(self, assign_response: Any) -> RouteHttp:
        return RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", f"{ISSUE_PATH}/assignee"): assign_response,
                ("PUT", LEDGER_PATH): empty(),
            }
        )

    def test_permission_denied_blocks_and_stops(self) -> None:
        runner = self._claim_runner(error(403))
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(operation(report, "assign")["reason"], "permission_denied")

    def test_conflict_blocks_without_a_blind_retry(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "to": {"id": "3"}}]}
                ),
                ("POST", f"{ISSUE_PATH}/transitions"): error(409),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--transition", "in_progress", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "transition")["reason"], "conflict")
        transition_posts = [
            call
            for call in runner.calls
            if call["method"] == "POST" and call["path"].endswith("/transitions")
        ]
        self.assertEqual(len(transition_posts), 1)

    def test_rate_limit_retries_then_succeeds(self) -> None:
        sleeps: list[float] = []
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", f"{ISSUE_PATH}/assignee"): [
                    error(429, {"Retry-After": "1"}),
                    empty(),
                ],
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        client = make_client(runner, sleeps=sleeps)
        report = jira_mutations.apply_mutation_plan(
            jira_mutations.build_mutation_plan(
                load_config(),
                jira_mutations.MutationRequest(issue_ref=ISSUE, claim=True),
                apply_requested=True,
            ),
            client,
        )
        self.assertEqual(operation(report, "assign")["status"], "applied")
        self.assertEqual(sleeps, [1.0])
        assign_calls = [call for call in runner.calls if call["path"].endswith("/assignee")]
        self.assertEqual(len(assign_calls), 2)

    def test_exhausted_rate_limit_fails_without_applying(self) -> None:
        runner = self._claim_runner([error(429), error(429), error(429), error(429)])
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(operation(report, "assign")["reason"], "rate_limited")

    def test_server_error_retries_then_succeeds(self) -> None:
        runner = self._claim_runner([error(503), empty()])
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "assign")["status"], "applied")

    def test_timeout_fails_closed(self) -> None:
        runner = RouteHttp(
            {("GET", ISSUE_PATH): [TimeoutError("timed out")] * 4}
        )
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "assign")["reason"], "unavailable")
        self.assertEqual(report["write_request_count"], 0)

    def test_cancellation_stops_before_any_write(self) -> None:
        runner = RouteHttp({("GET", ISSUE_PATH): ok(issue_response())})
        client = make_client(runner, cancelled=lambda: True)
        report = jira_mutations.apply_mutation_plan(
            jira_mutations.build_mutation_plan(
                load_config(),
                jira_mutations.MutationRequest(issue_ref=ISSUE, claim=True),
                apply_requested=True,
            ),
            client,
        )
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["write_request_count"], 0)
        self.assertEqual(runner.calls, [])

    def test_later_operations_are_skipped_after_a_failure(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): error(403),
                ("PUT", LEDGER_PATH): empty(),
                ("POST", f"{ISSUE_PATH}/comment"): ok({"id": "1"}, status=201),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--comment", "claimed", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "assign")["reason"], "permission_denied")
        self.assertEqual(operation(report, "comment")["status"], "skipped")
        self.assertEqual(operation(report, "comment")["reason"], "aborted_after_failure")
        self.assertNotIn(f"{ISSUE_PATH}/comment", runner.paths("POST"))

    def test_ledger_write_failure_does_not_invent_a_duplicate(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", f"{ISSUE_PATH}/assignee"): empty(),
                ("PUT", LEDGER_PATH): [error(500)] * 4,
            }
        )
        code, report, _ = run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["ledger_status"], "write_failed")
        self.assertEqual(operation(report, "assign")["status"], "applied")


class PrivacyTests(unittest.TestCase):
    def test_reports_carry_no_issue_prose_or_credentials(self) -> None:
        runner = FakeJira()
        code, report, stderr = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--comment", "claimed", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 0)
        serialized = json.dumps(report)
        for forbidden in (PROSE, EMAIL, TOKEN, ACCOUNT_ID):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, stderr)

    def test_the_claim_property_carries_bounded_metadata_only(self) -> None:
        """The claim is written to Jira, so it is held to the same bar."""
        runner = FakeJira()
        run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "pr_opened", "--pr-url", PR_URL, "--apply"],
            runner=runner,
        )
        claim = runner.claim("pr_opened", PR_URL)
        self.assertEqual(
            set(claim),
            {"schema", "operation", "fingerprint", "template", "state", "owner", "at"},
        )
        serialized = json.dumps(claim)
        for forbidden in (PROSE, EMAIL, TOKEN, ACCOUNT_ID, PR_URL, "Code Mower claimed"):
            self.assertNotIn(forbidden, serialized)

    def test_retained_plan_is_bounded_metadata_only(self) -> None:
        with TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            config_path = Path(tmp) / "code-mower.yml"
            config_path.write_text(config_text(), encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                code = jira_mutations.main(
                    [
                        "mutate",
                        str(config_path),
                        "--issue",
                        ISSUE,
                        "--claim",
                        "--plan-out",
                        str(plan_path),
                    ],
                    client_factory=lambda **kwargs: make_client(ExplodingHttp()),
                    env={},
                )
            self.assertEqual(code, 0)
            retained = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(retained["schema"], jira_mutations.MUTATION_REPORT_SCHEMA)
        self.assertEqual(retained["write_request_count"], 0)
        self.assertNotIn(str(plan_path), out.getvalue())
        self.assertNotIn(TOKEN, json.dumps(retained))

    def test_only_bounded_issue_fields_are_requested(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", f"{ISSUE_PATH}/assignee"): empty(),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        run_cli(["mutate", "--issue", ISSUE, "--claim", "--apply"], runner=runner)
        issue_call = next(call for call in runner.calls if call["path"] == ISSUE_PATH)
        self.assertEqual(
            issue_call["query"], {"fields": "status,assignee,project,issuetype"}
        )


class RequestValidationTests(unittest.TestCase):
    def test_a_request_needs_at_least_one_operation(self) -> None:
        code, _, stderr = run_cli(["mutate", "--issue", ISSUE])
        self.assertEqual(code, 1)
        self.assertIn("--claim", stderr)

    def test_malformed_issue_reference_is_rejected(self) -> None:
        code, _, stderr = run_cli(["mutate", "--issue", "../../admin", "--claim"])
        self.assertEqual(code, 1)
        self.assertIn("bounded id or key token", stderr)

    def test_pull_request_url_must_be_a_github_pr(self) -> None:
        for candidate in (
            "https://example.com/owner/repo/pull/1",
            "https://github.com/owner/repo/issues/1",
            "http://github.com/owner/repo/pull/1",
            "https://github.com/owner/repo/pull/0",
        ):
            with self.assertRaises(jira_mutations.MutationRequestError):
                jira_mutations.parse_pull_request_url(candidate)

    def test_non_jira_tracker_cannot_plan_a_mutation(self) -> None:
        with self.assertRaises(jira_mutations.MutationRequestError):
            jira_mutations.resolve_mutation_settings({"tracker": {"kind": "github"}})

    def test_missing_credentials_refuse_before_any_request(self) -> None:
        code, _, stderr = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--apply"],
            runner=ExplodingHttp(),
            with_credentials=False,
        )
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
