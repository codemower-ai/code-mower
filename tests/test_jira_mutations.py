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

ISSUE_PATH = f"/rest/api/3/issue/{ISSUE}"
LEDGER_PATH = f"{ISSUE_PATH}/properties/{jira_mutations.LEDGER_PROPERTY_KEY}"
MYSELF_PATH = "/rest/api/3/myself"


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

    def test_mutation_client_allows_only_the_four_operations_and_its_ledger(self) -> None:
        client = make_client(ExplodingHttp())
        for method, path in (
            ("GET", ISSUE_PATH),
            ("PUT", f"{ISSUE_PATH}/assignee"),
            ("POST", f"{ISSUE_PATH}/transitions"),
            ("POST", f"{ISSUE_PATH}/comment"),
            ("POST", f"{ISSUE_PATH}/remotelink"),
            ("PUT", LEDGER_PATH),
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

    def test_the_retry_policy_seam_names_only_the_two_unsafe_writes(self) -> None:
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
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("PUT", LEDGER_PATH): empty(),
                ("POST", f"{ISSUE_PATH}/comment"): ok({"id": "20001", "body": PROSE}, status=201),
            }
        )
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
        post = next(call for call in runner.calls if call["path"].endswith("/comment"))
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


class ReplayTests(unittest.TestCase):
    def _ledger(self, template: str, state: str) -> dict[str, Any]:
        fingerprint = jira_mutations.mutation_fingerprint(
            "comment",
            {
                "issue": ISSUE,
                "template": template,
                "text": jira_mutations.render_comment(template),
            },
        )
        return {
            "value": {
                "schema": jira_mutations.LEDGER_SCHEMA,
                "entries": {
                    fingerprint: {
                        "operation": "comment",
                        "state": state,
                        "at": "2026-09-08T00:00:00+00:00",
                    }
                },
            }
        }

    def test_applied_comment_is_never_reposted(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): ok(self._ledger("claimed", "applied")),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=runner
        )
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "comment")["reason"], "already_commented")
        self.assertNotIn(f"{ISSUE_PATH}/comment", runner.paths("POST"))

    def test_interrupted_comment_is_unverified_rather_than_claimed_applied(self) -> None:
        """A pending entry means "unknown", and the report must say so.

        The comment may have committed, may have been lost in flight, or may
        never have left. Reposting risks a duplicate and claiming success
        would be a guess, so the run reports ``unverified`` and hands the one
        comment back to an owner.
        """
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): ok(self._ledger("claimed", "pending")),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=runner
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(operation(report, "comment")["status"], "unverified")
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertNotIn(f"{ISSUE_PATH}/comment", runner.paths("POST"))
        self.assertIn("by hand", report["next_action"])
        final_ledger = runner.write_calls()[-1]["body"]
        self.assertEqual(
            [entry["state"] for entry in final_ledger["entries"].values()], ["unverified"]
        )

    def test_an_unverified_comment_stays_unverified_and_is_never_reposted(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): ok(self._ledger("claimed", "unverified")),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(
            ["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=runner
        )
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "comment")["reason"], "comment_unverified")
        self.assertNotIn(f"{ISSUE_PATH}/comment", runner.paths("POST"))

    def test_a_failed_earlier_operation_leaves_no_pending_comment_behind(self) -> None:
        """A never-attempted comment must not be suppressed on the retry.

        The intent is recorded immediately before the post, so a transition
        that fails first cannot leave a pending ledger entry that a later run
        would read as an interrupted post.
        """
        first = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "to": {"id": "3"}}]}
                ),
                ("POST", f"{ISSUE_PATH}/transitions"): error(500),
            }
        )
        args = [
            "mutate",
            "--issue",
            ISSUE,
            "--transition",
            "in_progress",
            "--comment",
            "claimed",
            "--apply",
        ]
        code, report, _ = run_cli(args, runner=first)
        self.assertEqual(code, 1)
        self.assertEqual(operation(report, "transition")["reason"], "unavailable")
        self.assertEqual(operation(report, "comment")["status"], "skipped")
        # The only write attempted is the transition itself: no ledger entry
        # was opened for a comment that never ran.
        self.assertEqual(
            [call["path"] for call in first.write_calls()],
            [f"{ISSUE_PATH}/transitions"],
        )

        second = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "to": {"id": "3"}}]}
                ),
                ("POST", f"{ISSUE_PATH}/transitions"): empty(),
                ("POST", f"{ISSUE_PATH}/comment"): ok({"id": "20001"}, status=201),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli(args, runner=second)
        self.assertEqual(code, 0)
        self.assertEqual(operation(report, "transition")["status"], "applied")
        self.assertEqual(operation(report, "comment")["status"], "applied")
        self.assertEqual(
            [call["path"] for call in second.write_calls()],
            [
                f"{ISSUE_PATH}/transitions",
                LEDGER_PATH,
                f"{ISSUE_PATH}/comment",
                LEDGER_PATH,
            ],
        )

    def test_comment_intent_is_recorded_before_the_post(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("PUT", LEDGER_PATH): empty(),
                ("POST", f"{ISSUE_PATH}/comment"): ok({"id": "20001"}, status=201),
            }
        )
        run_cli(["mutate", "--issue", ISSUE, "--comment", "claimed", "--apply"], runner=runner)
        write_paths = [call["path"] for call in runner.write_calls()]
        self.assertEqual(
            write_paths, [LEDGER_PATH, f"{ISSUE_PATH}/comment", LEDGER_PATH]
        )
        first_ledger = runner.write_calls()[0]["body"]
        states = [entry["state"] for entry in first_ledger["entries"].values()]
        self.assertEqual(states, ["pending"])

    def test_fingerprints_are_stable_across_runs(self) -> None:
        payload = {"issue": ISSUE, "template": "claimed", "text": "x"}
        self.assertEqual(
            jira_mutations.mutation_fingerprint("comment", payload),
            jira_mutations.mutation_fingerprint("comment", dict(reversed(list(payload.items())))),
        )

    def test_ledger_drops_unknown_and_oversized_entries(self) -> None:
        entries = {
            f"{index:032x}": {
                "operation": "comment",
                "state": "applied",
                "at": f"2026-09-08T00:00:{index:02d}+00:00",
            }
            for index in range(jira_mutations.MAX_LEDGER_ENTRIES + 5)
        }
        entries["not-a-fingerprint"] = {"operation": "comment", "state": "applied", "at": "x"}
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

    def test_first_apply_performs_each_operation_once(self) -> None:
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response(status_id="1")),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("PUT", f"{ISSUE_PATH}/assignee"): empty(),
                ("GET", f"{ISSUE_PATH}/transitions"): ok(
                    {"transitions": [{"id": "31", "to": {"id": "3"}}]}
                ),
                ("POST", f"{ISSUE_PATH}/transitions"): empty(),
                ("GET", f"{ISSUE_PATH}/remotelink"): ok([]),
                ("POST", f"{ISSUE_PATH}/remotelink"): ok({"id": 9}, status=201),
                ("POST", f"{ISSUE_PATH}/comment"): ok({"id": "20001"}, status=201),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli([*self.ARGS, "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "applied")
        self.assertEqual(
            [item["status"] for item in report["operations"]], ["applied"] * 4
        )
        effect_paths = [
            call["path"] for call in runner.write_calls() if call["path"] != LEDGER_PATH
        ]
        self.assertEqual(
            effect_paths,
            [
                f"{ISSUE_PATH}/assignee",
                f"{ISSUE_PATH}/transitions",
                f"{ISSUE_PATH}/remotelink",
                f"{ISSUE_PATH}/comment",
            ],
        )
        self.final_ledger = runner.write_calls()[-1]["body"]

    def test_replay_after_a_completed_run_writes_nothing(self) -> None:
        self.test_first_apply_performs_each_operation_once()
        global_id = jira_mutations.remote_link_global_id("owner", "repo", "12")
        runner = RouteHttp(
            {
                # Live state now reflects the first run.
                ("GET", ISSUE_PATH): ok(
                    issue_response(status_id="3", assignee=ACCOUNT_ID)
                ),
                ("GET", LEDGER_PATH): ok({"value": self.final_ledger}),
                ("GET", MYSELF_PATH): ok({"accountId": ACCOUNT_ID}),
                ("GET", f"{ISSUE_PATH}/remotelink"): ok(
                    {"id": 9, "globalId": global_id}
                ),
                ("PUT", LEDGER_PATH): empty(),
            }
        )
        code, report, _ = run_cli([*self.ARGS, "--apply"], runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "already_applied")
        self.assertEqual(
            [item["status"] for item in report["operations"]], ["already_applied"] * 4
        )
        effect_paths = [
            call["path"] for call in runner.write_calls() if call["path"] != LEDGER_PATH
        ]
        self.assertEqual(effect_paths, [])

    def test_human_output_states_both_guards_and_the_gate_owner(self) -> None:
        report = jira_mutations.build_mutation_plan(
            load_config(),
            jira_mutations.MutationRequest(issue_ref=ISSUE, claim=True),
        )
        text = jira_mutations._render_text(report)
        self.assertIn("Writes enabled (config): true", text)
        self.assertIn("Apply requested (runtime): false", text)
        self.assertIn("Jira write requests: 0", text)
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
        runner = RouteHttp(
            {
                ("GET", ISSUE_PATH): ok(issue_response()),
                ("GET", LEDGER_PATH): error(404),
                ("GET", MYSELF_PATH): ok(
                    {"accountId": ACCOUNT_ID, "emailAddress": EMAIL, "displayName": "QA Bot"}
                ),
                ("PUT", f"{ISSUE_PATH}/assignee"): empty(),
                ("PUT", LEDGER_PATH): empty(),
                ("POST", f"{ISSUE_PATH}/comment"): ok(
                    {"id": "1", "body": PROSE, "author": {"emailAddress": EMAIL}}, status=201
                ),
            }
        )
        code, report, stderr = run_cli(
            ["mutate", "--issue", ISSUE, "--claim", "--comment", "claimed", "--apply"],
            runner=runner,
        )
        self.assertEqual(code, 0)
        serialized = json.dumps(report)
        for forbidden in (PROSE, EMAIL, TOKEN, ACCOUNT_ID, "QA Bot"):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, stderr)

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
