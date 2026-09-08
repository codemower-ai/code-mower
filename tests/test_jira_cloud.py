#!/usr/bin/env python3
"""Focused offline tests for the read-only Jira Cloud client (issue #800).

Every test is deterministic and performs no live network call: HTTP goes
through an injected fake runner and Keychain access through an injected
fake runner. Fixtures under tests/fixtures/jira use synthetic ids and
example.atlassian.net only.
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from typing import Any, Mapping

from code_mower import jira_cloud
from code_mower.doctor_checks import jira as jira_doctor
from code_mower.provider_credentials import display_profile_path

FIXTURES = Path(__file__).parent / "fixtures" / "jira"

CLOUD_ID = "11111111-2222-3333-4444-555555555555"
SITE_URL = "https://example.atlassian.net"
PROJECT_ID = "10001"
EMAIL = "qa-bot@example.com"
TOKEN = "tok-1"
SERVICE = "svc-a"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def http_response(payload: Any, status: int = 200, headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), json.dumps(payload).encode("utf-8"))


def http_error(status: int, body: bytes = b"{}", headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), body)


class FakeHttp:
    """Scripted HTTP runner; records calls and never touches the network."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        if not self.script:
            raise AssertionError("unexpected HTTP call: no scripted response left")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def request_bodies(self) -> list[Any]:
        out = []
        for call in self.calls:
            raw = call["body"]
            out.append(json.loads(raw.decode("utf-8")) if raw else None)
        return out


def make_client(
    runner: FakeHttp,
    *,
    sleeps: list[float] | None = None,
    cancelled: Any = None,
    max_attempts: int = 4,
    max_response_bytes: int = jira_cloud.MAX_RESPONSE_BYTES,
) -> jira_cloud.JiraReadClient:
    sleep_log: list[float] = sleeps if sleeps is not None else []
    return jira_cloud.JiraReadClient(
        cloud_id=CLOUD_ID,
        email=EMAIL,
        token=TOKEN,
        site_url=SITE_URL,
        http_runner=runner,
        sleep_fn=sleep_log.append,
        random_fn=lambda: 0.0,
        cancelled_fn=(cancelled or (lambda: False)),
        max_attempts=max_attempts,
        max_response_bytes=max_response_bytes,
    )


def write_profile(config_dir: Path, name: str, text: str, mode: int = 0o600) -> Path:
    path = config_dir / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


class GatewayTests(unittest.TestCase):
    def test_gateway_base_uses_cloud_id_not_site_url(self) -> None:
        runner = FakeHttp([http_response(load_fixture("server_info.json"))])
        client = make_client(runner)
        client.get_server_info()
        self.assertEqual(len(runner.calls), 1)
        url = runner.calls[0]["url"]
        self.assertTrue(
            url.startswith(f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/"),
            url,
        )
        self.assertNotIn("example.atlassian.net", url)

    def test_site_url_is_display_identity_only(self) -> None:
        runner = FakeHttp([])
        client = make_client(runner)
        self.assertEqual(
            client.browse_url("browse/ABC-1"), f"{SITE_URL}/browse/ABC-1"
        )
        self.assertEqual(client.base_url, f"https://api.atlassian.com/ex/jira/{CLOUD_ID}")

    def test_invalid_cloud_id_rejected_before_network(self) -> None:
        runner = FakeHttp([])
        with self.assertRaises(ValueError):
            jira_cloud.gateway_base("not a cloud id!")
        self.assertEqual(runner.calls, [])

    def test_non_https_site_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            jira_cloud.display_site_url("http://example.atlassian.net")


class CredentialPrecedenceTests(unittest.TestCase):
    def test_environment_wins_over_stored_profile(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}=stored@example.com\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}=stored-tok\n",
            )
            env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env=env
            )
        self.assertEqual(resolution.status, "ok")
        self.assertEqual(resolution.source, "env")
        self.assertEqual(resolution.email, EMAIL)
        self.assertEqual(resolution.token, TOKEN)

    def test_partial_ambient_credentials_fail_closed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}=stored-tok\n",
            )
            env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL}
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env=env
            )
        self.assertEqual(resolution.status, "missing")
        self.assertFalse(resolution.has_credentials)

    def test_malformed_email_rejected(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: "not-an-email", jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        resolution = jira_cloud.resolve_jira_credentials(env=env)
        self.assertEqual(resolution.status, "malformed")
        self.assertFalse(resolution.has_credentials)

    def test_explicit_credential_file_success(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(
                Path(tmp),
                "custom.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={TOKEN}\n",
            )
            resolution = jira_cloud.resolve_jira_credentials(
                credential_file=path, env={}
            )
        self.assertEqual(resolution.status, "ok")
        self.assertEqual(resolution.source, "credential_file")
        self.assertEqual(resolution.candidate_files, ("custom.env",))

    def test_secure_restart_single_profile_discovery(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={TOKEN}\n",
            )
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env={}
            )
        self.assertEqual(resolution.status, "ok")
        self.assertEqual(resolution.source, "single_profile")

    def test_ambiguous_discovery_lists_filenames_only(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir, "jira.env", f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
            )
            write_profile(
                config_dir, "jira-backup.env", f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
            )
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env={}
            )
        self.assertEqual(resolution.status, "ambiguous")
        self.assertEqual(
            resolution.candidate_files, ("jira-backup.env", "jira.env")
        )
        blob = json.dumps(
            {
                "message": resolution.message,
                "remediation": resolution.remediation,
                "detail": resolution.safe_detail(),
            }
        )
        self.assertIn("jira.env", blob)
        self.assertNotIn(str(config_dir), blob)
        self.assertNotIn(EMAIL, blob)

    def test_insecure_permissions_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={TOKEN}\n",
                mode=0o644,
            )
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env={}
            )
        self.assertEqual(resolution.status, "insecure_permissions")
        blob = json.dumps(
            {
                "message": resolution.message,
                "remediation": resolution.remediation,
                "detail": resolution.safe_detail(),
            }
        )
        self.assertIn("jira.env", blob)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(EMAIL, blob)
        self.assertNotIn(str(config_dir), blob)

    def test_missing_credentials_name_env_vars(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=Path(tmp), env={}
            )
        self.assertEqual(resolution.status, "missing")
        self.assertIn(jira_cloud.JIRA_TOKEN_ENV, resolution.missing_variables)
        self.assertIn(jira_cloud.JIRA_EMAIL_ENV, resolution.missing_variables)

    def test_display_profile_path_never_exposes_absolute_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp), "custom.env", "x=1\n")
            label = display_profile_path(path)
        self.assertNotIn(str(Path(tmp)), label)
        self.assertIn("custom.env", label)


class KeychainTests(unittest.TestCase):
    def test_env_service_completes_token_from_keychain(self) -> None:
        import tempfile

        seen: list[Any] = []

        def fake_keychain(argv: Any, env: Any) -> str:
            seen.append(tuple(argv))
            return TOKEN

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                jira_cloud.JIRA_EMAIL_ENV: EMAIL,
                jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE,
            }
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=Path(tmp), env=env, keychain_runner=fake_keychain
            )
        self.assertTrue(resolution.has_credentials)
        self.assertTrue(resolution.keychain_used)
        self.assertEqual(len(seen), 1)
        argv = seen[0]
        self.assertEqual(argv[:3], ("security", "find-generic-password", "-s"))
        self.assertIn(SERVICE, argv)
        self.assertIn(EMAIL, argv)
        # The token value travels in stdout only, never in argv.
        for part in argv:
            self.assertNotEqual(part, TOKEN)
        self.assertTrue(resolution.safe_detail().get("keychain"))

    def test_profile_file_service_completes_token_from_keychain(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={SERVICE}\n",
            )
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir,
                env={},
                keychain_runner=lambda argv, env: TOKEN,
            )
        self.assertTrue(resolution.has_credentials)
        self.assertTrue(resolution.keychain_used)

    def test_keychain_unavailable_stays_portable_and_missing(self) -> None:
        import tempfile

        def missing_tool(argv: Any, env: Any) -> str:
            raise FileNotFoundError("no such file: security")

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                jira_cloud.JIRA_EMAIL_ENV: EMAIL,
                jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE,
            }
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=Path(tmp), env=env, keychain_runner=missing_tool
            )
        self.assertEqual(resolution.status, "missing")
        self.assertFalse(resolution.has_credentials)
        blob = json.dumps(
            {
                "message": resolution.message,
                "remediation": resolution.remediation,
                "detail": resolution.safe_detail(),
            }
        )
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(SERVICE, blob)
        self.assertNotIn(EMAIL, blob)

    def test_unreadable_keychain_entry_is_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                jira_cloud.JIRA_EMAIL_ENV: EMAIL,
                jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE,
            }
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=Path(tmp),
                env=env,
                keychain_runner=lambda argv, env: "",
            )
        self.assertEqual(resolution.status, "missing")

    def test_ambiguous_profiles_win_over_keychain(self) -> None:
        import tempfile

        calls: list[Any] = []
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={SERVICE}\n",
            )
            write_profile(
                config_dir,
                "jira-second.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={SERVICE}\n",
            )
            env = {jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE}
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir,
                env=env,
                keychain_runner=lambda argv, env: calls.append(argv) or TOKEN,
            )
        self.assertEqual(resolution.status, "ambiguous")
        self.assertEqual(calls, [])

    def test_read_keychain_token_rejects_bad_inputs_without_values(self) -> None:
        with self.assertRaises(jira_cloud.KeychainError):
            jira_cloud.read_keychain_token("", EMAIL)
        with self.assertRaises(jira_cloud.KeychainError):
            jira_cloud.read_keychain_token(SERVICE, "not-an-email")
        for exc in (
            jira_cloud.KeychainUnavailable(),
            jira_cloud.KeychainMissing(),
        ):
            self.assertNotIn(SERVICE, str(exc))
            self.assertNotIn(EMAIL, str(exc))


class TransportErrorTests(unittest.TestCase):
    def test_error_codes_map_closed(self) -> None:
        cases = [
            (400, "jira_rejected"),
            (401, "jira_unauthorized"),
            (403, "jira_forbidden"),
            (404, "jira_not_found"),
            (418, "jira_rejected"),
        ]
        for status, code in cases:
            with self.subTest(status=status):
                runner = FakeHttp([http_error(status, b'{"secret":"body-marker-x"}')])
                client = make_client(runner, max_attempts=1)
                with self.assertRaises(jira_cloud.JiraApiError) as ctx:
                    client.get_server_info()
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(str(ctx.exception), code)
                # Raw response bodies never enter diagnostics.
                self.assertNotIn("body-marker-x", str(ctx.exception))
                self.assertNotIn("body-marker-x", repr(ctx.exception))
                self.assertEqual(len(runner.calls), 1)

    def test_rate_limited_after_bounded_retries(self) -> None:
        sleeps: list[float] = []
        runner = FakeHttp(
            [http_error(429, b"{}", {"Retry-After": "1"})] * 4,
        )
        client = make_client(runner, sleeps=sleeps, max_attempts=4)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_server_info()
        self.assertEqual(ctx.exception.code, "jira_rate_limited")
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(len(sleeps), 3)

    def test_retry_after_header_is_capped(self) -> None:
        sleeps: list[float] = []
        runner = FakeHttp(
            [
                http_error(429, b"{}", {"Retry-After": "3600"}),
                http_response(load_fixture("server_info.json")),
            ]
        )
        client = make_client(runner, sleeps=sleeps, max_attempts=2)
        info = client.get_server_info()
        self.assertEqual(info["base_url"], SITE_URL)
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], jira_cloud.RETRY_AFTER_CAP_SECONDS)

    def test_retry_after_header_is_honored(self) -> None:
        sleeps: list[float] = []
        runner = FakeHttp(
            [
                http_error(429, b"{}", {"Retry-After": "2"}),
                http_response(load_fixture("server_info.json")),
            ]
        )
        client = make_client(runner, sleeps=sleeps, max_attempts=2)
        client.get_server_info()
        self.assertEqual(sleeps, [2.0])

    def test_transient_5xx_retries_with_backoff_then_succeeds(self) -> None:
        sleeps: list[float] = []
        runner = FakeHttp(
            [
                http_error(503, b"{}"),
                http_error(500, b"{}"),
                http_response(load_fixture("server_info.json")),
            ]
        )
        client = make_client(runner, sleeps=sleeps, max_attempts=4)
        info = client.get_server_info()
        self.assertEqual(info["base_url"], SITE_URL)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        for delay in sleeps:
            self.assertLessEqual(delay, jira_cloud.BACKOFF_CAP_SECONDS)

    def test_persistent_5xx_becomes_unavailable(self) -> None:
        runner = FakeHttp([http_error(502, b"{}")] * 3)
        client = make_client(runner, max_attempts=3)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_server_info()
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 3)

    def test_network_failures_retry_then_succeed(self) -> None:
        import socket as socket_module

        runner = FakeHttp(
            [socket_module.timeout("timed out"), OSError("boom")]
            + [http_response(load_fixture("server_info.json"))]
        )
        client = make_client(runner, max_attempts=3)
        info = client.get_server_info()
        self.assertEqual(info["base_url"], SITE_URL)
        self.assertEqual(len(runner.calls), 3)

    def test_oversize_response_is_rejected(self) -> None:
        big = b'{"baseUrl": "' + b"x" * 4096 + b'"}'
        runner = FakeHttp([(200, {}, big)])
        client = make_client(runner, max_attempts=1, max_response_bytes=16)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_server_info()
        self.assertEqual(ctx.exception.code, "jira_unavailable")

    def test_non_object_response_is_rejected(self) -> None:
        runner = FakeHttp([(200, {}, b"[1,2,3]")])
        client = make_client(runner, max_attempts=1)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_server_info()
        self.assertEqual(ctx.exception.code, "jira_unavailable")

    def test_cancellation_aborts_before_retry(self) -> None:
        sleeps: list[float] = []
        calls = {"count": 0}

        def cancel_after_first() -> bool:
            return calls["count"] >= 1

        runner = FakeHttp([http_error(503, b"{}"), http_error(503, b"{}")])
        client = make_client(runner, sleeps=sleeps, cancelled=cancel_after_first)

        original = runner.__call__

        def counting(method: str, url: str, headers: Any, body: Any):
            calls["count"] += 1
            return original(method, url, headers, body)

        client.http_runner = counting  # type: ignore[method-assign]
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_server_info()
        self.assertEqual(ctx.exception.code, "jira_cancelled")
        self.assertEqual(calls["count"], 1)

    def test_client_is_read_only(self) -> None:
        runner = FakeHttp([])
        client = make_client(runner)
        with self.assertRaises(ValueError):
            client.request_json("DELETE", "/rest/api/3/issue/20001")
        with self.assertRaises(ValueError):
            client.request_json("POST", "/rest/api/3/issue")
        with self.assertRaises(ValueError):
            client.request_json("GET", "/rest/api/2/search")
        self.assertEqual(runner.calls, [])

    def test_search_rejects_non_metadata_fields(self) -> None:
        runner = FakeHttp([])
        client = make_client(runner)
        for bad_field in ("summary", "description", "comment", "*all"):
            with self.subTest(field=bad_field):
                with self.assertRaises(ValueError):
                    client.search_issues("project = 10001", fields=["status", bad_field])
        self.assertEqual(runner.calls, [])


class ReadPrimitiveTests(unittest.TestCase):
    def test_project_discovery_by_immutable_id(self) -> None:
        runner = FakeHttp([http_response(load_fixture("project.json"))])
        client = make_client(runner)
        project = client.get_project(PROJECT_ID)
        self.assertEqual(
            project, {"id": "10001", "key": "ABC", "name": "Example Project"}
        )
        self.assertIn(f"/rest/api/3/project/{PROJECT_ID}", runner.calls[0]["url"])

    def test_project_id_must_be_numeric(self) -> None:
        runner = FakeHttp([])
        client = make_client(runner)
        with self.assertRaises(ValueError):
            client.get_project("ABC")
        self.assertEqual(runner.calls, [])

    def test_reserved_word_project_keys_are_quoted(self) -> None:
        for key in ("AND", "OR", "IF", "ORDER", "ELSE"):
            with self.subTest(key=key):
                jql = jira_cloud.build_project_jql(project_key=key)
                self.assertEqual(jql, f'project = "{key}"')
        # The immutable id stays the preferred, unquoted primitive.
        self.assertEqual(
            jira_cloud.build_project_jql(project_id="10001", project_key="AND"),
            "project = 10001",
        )
        quoted = jira_cloud.quote_jql_string('A"B\\C')
        self.assertEqual(quoted, '"A\\"B\\\\C"')
        with self.assertRaises(ValueError):
            jira_cloud.build_project_jql()

    def test_issue_types_and_workflow_variation(self) -> None:
        runner = FakeHttp(
            [
                http_response(load_fixture("issue_types.json")),
                http_response(load_fixture("createmeta_task.json")),
                http_response(load_fixture("createmeta_bug.json")),
            ]
        )
        client = make_client(runner)
        types = client.get_issue_types(PROJECT_ID)
        self.assertEqual(
            types, [{"id": "10001", "name": "Task"}, {"id": "10002", "name": "Bug"}]
        )
        task_fields = client.get_required_create_fields(PROJECT_ID, "10001")
        bug_fields = client.get_required_create_fields(PROJECT_ID, "10002")
        self.assertEqual(task_fields, ["customfield_10001", "priority"])
        self.assertEqual(bug_fields, ["customfield_10002", "priority"])
        self.assertNotEqual(task_fields, bug_fields)

    def test_status_metadata_and_categories(self) -> None:
        runner = FakeHttp(
            [
                http_response(load_fixture("statuses.json")),
                http_response(load_fixture("status_categories.json")),
            ]
        )
        client = make_client(runner)
        statuses = client.get_statuses()
        categories = client.get_status_categories()
        self.assertEqual([item["id"] for item in statuses], ["10000", "10001", "10002"])
        self.assertEqual(
            [item["key"] for item in categories], ["new", "indeterminate", "done"]
        )
        category_map = {"new": ["10000"], "in_progress": ["10001"], "done": ["10002"]}
        self.assertEqual(
            jira_cloud.map_status_to_lifecycle("10000", statuses, category_map), "new"
        )
        self.assertEqual(
            jira_cloud.map_status_to_lifecycle("10001", statuses, category_map),
            "in_progress",
        )
        self.assertEqual(
            jira_cloud.map_status_to_lifecycle("10002", statuses, category_map), "done"
        )
        self.assertIsNone(
            jira_cloud.map_status_to_lifecycle("99999", statuses, category_map)
        )
        self.assertIsNone(jira_cloud.map_status_to_lifecycle("10000", statuses, {}))

    def test_search_pagination_with_next_page_token(self) -> None:
        runner = FakeHttp(
            [
                http_response(load_fixture("search_page1.json")),
                http_response(load_fixture("search_page2.json")),
            ]
        )
        client = make_client(runner)
        result = client.search_issues("project = 10001")
        self.assertFalse(result["truncated"])
        self.assertEqual(
            [issue["key"] for issue in result["issues"]], ["ABC-1", "ABC-2", "ABC-3"]
        )
        bodies = runner.request_bodies()
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("nextPageToken", bodies[0])
        self.assertEqual(bodies[1]["nextPageToken"], "synthetic-token-1")
        # Only bounded metadata fields are requested and returned.
        for body in bodies:
            self.assertLessEqual(set(body["fields"]), jira_cloud.SAFE_SEARCH_FIELDS)
        first = result["issues"][0]
        self.assertEqual(first["id"], "20001")
        self.assertEqual(first["fields"]["status_id"], "10000")
        self.assertEqual(first["fields"]["labels"], ["lane-a"])
        self.assertTrue(first["fields"]["assigned"])
        self.assertFalse(result["issues"][1]["fields"]["assigned"])
        blob = json.dumps(result)
        self.assertNotIn("decoy_prose", blob)
        self.assertNotIn("fixture prose", blob)

    def test_search_truncation_is_explicit(self) -> None:
        runner = FakeHttp([http_response(load_fixture("search_page1.json"))])
        client = make_client(runner)
        result = client.search_issues("project = 10001", max_issues=2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["issues"]), 2)
        self.assertEqual(len(runner.calls), 1)

    def test_transitions_are_bounded_metadata(self) -> None:
        runner = FakeHttp([http_response(load_fixture("transitions.json"))])
        client = make_client(runner)
        transitions = client.get_transitions("20001")
        self.assertEqual(
            [item["id"] for item in transitions], ["11", "21", "31"]
        )
        self.assertEqual(transitions[1]["to_status_id"], "10001")

    def test_effective_permission_probe_returns_booleans(self) -> None:
        runner = FakeHttp([http_response(load_fixture("permissions.json"))])
        client = make_client(runner)
        permissions = client.check_permissions(PROJECT_ID)
        self.assertEqual(
            permissions,
            {
                "BROWSE_PROJECTS": True,
                "CREATE_ISSUES": True,
                "EDIT_ISSUES": False,
                "TRANSITION_ISSUES": False,
                "ADD_COMMENTS": False,
            },
        )
        bodies = runner.request_bodies()
        self.assertEqual(
            bodies[0]["projectPermissions"][0]["projectId"], PROJECT_ID
        )

    def test_permission_names_are_bounded_tokens(self) -> None:
        runner = FakeHttp([])
        client = make_client(runner)
        with self.assertRaises(ValueError):
            client.check_permissions(PROJECT_ID, ["browse_projects"])
        self.assertEqual(runner.calls, [])


def jira_config(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "site_url": SITE_URL,
        "cloud_id": CLOUD_ID,
        "project_id": PROJECT_ID,
    }
    block.update(overrides)
    return {"tracker": {"kind": "jira_cloud", "jira_cloud": block}}


def ready_script() -> list[Any]:
    return [
        http_response(load_fixture("server_info.json")),
        http_response(load_fixture("project.json")),
        http_response(load_fixture("statuses.json")),
        http_response(load_fixture("status_categories.json")),
        http_response(load_fixture("permissions.json")),
        http_response(load_fixture("search_page2.json")),
    ]


def run_doctor_checks(
    config: Mapping[str, Any] | None,
    script: list[Any] | None,
    *,
    env: Mapping[str, str] | None = None,
    config_dir: Path | None = None,
) -> tuple[Any, FakeHttp | None]:
    runner = FakeHttp(script) if script is not None else None

    def factory(**kwargs: Any) -> jira_cloud.JiraReadClient:
        assert runner is not None
        return jira_cloud.JiraReadClient(
            cloud_id=kwargs["cloud_id"],
            email=kwargs["email"],
            token=kwargs["token"],
            site_url=kwargs.get("site_url", ""),
            http_runner=runner,
            sleep_fn=lambda delay: None,
            random_fn=lambda: 0.0,
            timeout_seconds=float(kwargs.get("timeout_seconds", 5)),
        )

    checks = jira_doctor.check_jira_tracker_readiness(
        config=config,
        env=dict(env) if env is not None else {},
        config_dir=config_dir,
        client_factory=factory if runner is not None else None,
    )
    return checks, runner


def checks_by_id(checks: Any) -> dict[str, Any]:
    return {check.name: check for check in checks}


def doctor_blob(checks: Any) -> str:
    return json.dumps([check.as_dict() for check in checks], sort_keys=True)


class DoctorCheckTests(unittest.TestCase):
    def test_github_configs_produce_no_jira_checks(self) -> None:
        for config in (None, {}, {"tracker": {"kind": "github"}}):
            with self.subTest(config=config):
                checks, _ = run_doctor_checks(config, None, env={})
                self.assertEqual(checks, ())

    def test_missing_tracker_block_fails_config(self) -> None:
        checks, _ = run_doctor_checks(
            {"tracker": {"kind": "jira_cloud"}}, None, env={}
        )
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_CONFIG_CHECK].status, "fail")

    def test_malformed_site_url_fails_config(self) -> None:
        checks, _ = run_doctor_checks(
            jira_config(site_url="http://example.atlassian.net"), None, env={}
        )
        by_id = checks_by_id(checks)
        check = by_id[jira_doctor.JIRA_CONFIG_CHECK]
        self.assertEqual(check.status, "fail")
        self.assertIn("remediation", check.as_dict())

    def test_missing_credentials_fail_with_safe_remediation(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            checks, _ = run_doctor_checks(
                jira_config(), None, env={}, config_dir=Path(tmp)
            )
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_CONFIG_CHECK].status, "pass")
        creds = by_id[jira_doctor.JIRA_CREDENTIALS_CHECK]
        self.assertEqual(creds.status, "fail")
        read = by_id[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "skip")
        blob = doctor_blob(checks)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(EMAIL, blob)
        self.assertNotIn(str(tmp), blob)

    def test_malformed_credentials_fail(self) -> None:
        checks, _ = run_doctor_checks(
            jira_config(),
            None,
            env={jira_cloud.JIRA_EMAIL_ENV: "bad", jira_cloud.JIRA_TOKEN_ENV: TOKEN},
        )
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_CREDENTIALS_CHECK].status, "fail")
        self.assertEqual(by_id[jira_doctor.JIRA_READ_CHECK].status, "skip")

    def test_insecure_profile_fails_credentials(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={TOKEN}\n",
                mode=0o644,
            )
            checks, _ = run_doctor_checks(
                jira_config(), None, env={}, config_dir=config_dir
            )
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_CREDENTIALS_CHECK].status, "fail")
        blob = doctor_blob(checks)
        self.assertIn("jira.env", blob)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(EMAIL, blob)

    def test_ready_probe_passes_with_metadata_only_detail(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, runner = run_doctor_checks(jira_config(), ready_script(), env=env)
        assert runner is not None
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_CONFIG_CHECK].status, "pass")
        self.assertEqual(by_id[jira_doctor.JIRA_CREDENTIALS_CHECK].status, "pass")
        read = by_id[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "pass")
        detail = dict(read.detail or {})
        self.assertEqual(detail["reason"], "ready")
        self.assertEqual(detail["project_key"], "ABC")
        self.assertEqual(detail["status_count"], 3)
        self.assertEqual(detail["permission_probe"]["BROWSE_PROJECTS"], True)
        # The default queue query uses the immutable project id.
        bodies = runner.request_bodies()
        search_body = bodies[-1]
        self.assertEqual(search_body["jql"], "project = 10001")
        blob = doctor_blob(checks)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(EMAIL, blob)

    def test_configured_jql_is_used_for_queue_probe(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        custom = "project = 10001 AND statusCategory != Done ORDER BY updated DESC"
        checks, runner = run_doctor_checks(
            jira_config(jql=custom), ready_script(), env=env
        )
        assert runner is not None
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_READ_CHECK].status, "pass")
        self.assertEqual(runner.request_bodies()[-1]["jql"], custom)

    def test_expired_token_fails_read(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(), [http_error(401, b'{"marker":"x"}')], env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "expired/unauthorized")
        self.assertIn("expired", read.message)

    def test_forbidden_fails_read(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(), [http_error(403, b"{}")], env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "forbidden")

    def test_wrong_cloud_fails_on_server_identity(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(), [http_error(404, b"{}")], env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "wrong-cloud")

    def test_wrong_project_fails_on_project_lookup(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(),
            [
                http_response(load_fixture("server_info.json")),
                http_error(404, b"{}"),
            ],
            env=env,
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "wrong-project")

    def test_project_key_mismatch_fails_read(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(project_key="WRONG"),
            [
                http_response(load_fixture("server_info.json")),
                http_response(load_fixture("project.json")),
            ],
            env=env,
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "wrong-project")

    def test_rate_limited_warns_without_writes(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        script = [http_error(429, b'{"rate":"limited-marker"}', {"Retry-After": "0"})] * 4
        checks, runner = run_doctor_checks(jira_config(), script, env=env)
        assert runner is not None
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "warn")
        self.assertEqual(read.detail.get("reason"), "rate-limited")
        self.assertEqual(len(runner.calls), 4)
        for call in runner.calls:
            self.assertEqual(call["method"], "GET")
        blob = doctor_blob(checks)
        self.assertNotIn("limited-marker", blob)

    def test_issue_type_probe_reports_required_fields(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(load_fixture("issue_types.json")),
            http_response(load_fixture("createmeta_bug.json")),
            http_response(load_fixture("permissions.json")),
            http_response(load_fixture("search_page2.json")),
        ]
        checks, _ = run_doctor_checks(
            jira_config(issue_type_id="10002"), script, env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "pass")
        self.assertEqual(
            read.detail.get("required_create_fields"),
            ["customfield_10002", "priority"],
        )

    def test_unknown_issue_type_probe_target_fails(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(load_fixture("issue_types.json")),
        ]
        checks, _ = run_doctor_checks(
            jira_config(issue_type_id="99999"), script, env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "wrong-project")

    def test_check_ids_are_stable(self) -> None:
        self.assertEqual(jira_doctor.JIRA_CONFIG_CHECK, "tracker.jira.config")
        self.assertEqual(jira_doctor.JIRA_CREDENTIALS_CHECK, "tracker.jira.credentials")
        self.assertEqual(jira_doctor.JIRA_READ_CHECK, "tracker.jira.read")


class NoSecretDiagnosticsTests(unittest.TestCase):
    def test_no_secret_values_in_any_failure_surface(self) -> None:
        import tempfile

        secret_token = "tok-secret-9"
        secret_email = "secreto@example.com"
        secret_service = "svc-secret-9"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={secret_email}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={secret_token}\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={secret_service}\n",
                mode=0o644,
            )
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env={}
            )
            self.assertEqual(resolution.status, "insecure_permissions")
            blob = json.dumps(
                {
                    "message": resolution.message,
                    "remediation": resolution.remediation,
                    "detail": resolution.safe_detail(),
                }
            )
            # Mode broader than 0600 is rejected on POSIX.
            mode = stat.S_IMODE((config_dir / "jira.env").stat().st_mode)
            self.assertNotEqual(mode & 0o077, 0)
        for secret in (secret_token, secret_email, secret_service):
            self.assertNotIn(secret, blob)
        self.assertNotIn(str(config_dir), blob)

    def test_safe_detail_never_carries_values(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                jira_cloud.JIRA_EMAIL_ENV: EMAIL,
                jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE,
            }
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=Path(tmp),
                env=env,
                keychain_runner=lambda argv, env: TOKEN,
            )
        detail = resolution.safe_detail()
        blob = json.dumps(detail)
        self.assertNotIn(EMAIL, blob)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(SERVICE, blob)
        self.assertTrue(detail.get("keychain"))


if __name__ == "__main__":
    unittest.main()

