#!/usr/bin/env python3
"""Focused offline tests for the read-only Jira Cloud client (issue #800).

Every test is deterministic and performs no live network call: HTTP goes
through an injected fake runner and Keychain access through an injected
fake runner. Fixtures under tests/fixtures/jira use synthetic ids and
example.atlassian.net only.
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

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

    def test_incomplete_http_response_retries_then_succeeds(self) -> None:
        runner = FakeHttp(
            [
                http.client.IncompleteRead(b"partial", 10),
                http_response(load_fixture("server_info.json")),
            ]
        )
        client = make_client(runner, max_attempts=2)
        info = client.get_server_info()
        self.assertEqual(info["base_url"], SITE_URL)
        self.assertEqual(len(runner.calls), 2)

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
        for call in runner.calls[1:]:
            self.assertIn("/rest/api/3/issue/createmeta/", call["url"])
            self.assertIn("startAt=0", call["url"])
            self.assertIn("maxResults=", call["url"])

    def test_create_fields_follow_bounded_pagination(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "fields": [
                            {"fieldId": "customfield_10001", "required": True},
                            {"fieldId": "labels", "required": False},
                        ],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": 3,
                    }
                ),
                http_response(
                    {
                        "fields": [{"fieldId": "priority", "required": True}],
                        "startAt": 2,
                        "maxResults": 50,
                        "total": 3,
                    }
                ),
            ]
        )
        client = make_client(runner)
        required = client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(required, ["customfield_10001", "priority"])
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("startAt=0", runner.calls[0]["url"])
        self.assertIn("startAt=2", runner.calls[1]["url"])

    def test_create_fields_malformed_pagination_fails_closed(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "fields": [{"fieldId": "priority", "required": True}],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": "not-a-number",
                    }
                )
            ]
        )
        client = make_client(runner)
        # A partial required-field set must never be returned: doctor
        # would otherwise treat it as the complete set.
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(ctx.exception.endpoint, "createMeta")
        self.assertEqual(len(runner.calls), 1)

    def test_create_fields_repeated_pagination_state_fails_closed(self) -> None:
        page = {
            "fields": [{"fieldId": "priority", "required": True}],
            "startAt": 0,
            "maxResults": 50,
            "total": 999,
        }
        runner = FakeHttp([http_response(page), http_response(page)])
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 2)

    def test_create_fields_page_bound_fails_closed(self) -> None:
        script = [
            http_response(
                {
                    "fields": [{"fieldId": f"customfield_{index:05d}", "required": True}],
                    "startAt": index,
                    "maxResults": 50,
                    "total": 10000,
                }
            )
            for index in range(jira_cloud.CREATEMETA_MAX_PAGES)
        ]
        runner = FakeHttp(script)
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), jira_cloud.CREATEMETA_MAX_PAGES)

    def test_create_fields_result_cap_fails_closed(self) -> None:
        fields = [
            {"fieldId": f"customfield_{index:05d}", "required": True}
            for index in range(jira_cloud.MAX_REQUIRED_CREATE_FIELDS + 1)
        ]
        runner = FakeHttp(
            [
                http_response(
                    {
                        "fields": fields[:50],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": len(fields),
                    }
                ),
                http_response(
                    {
                        "fields": fields[50:],
                        "startAt": 50,
                        "maxResults": 50,
                        "total": len(fields),
                    }
                ),
            ]
        )
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 2)

    def test_create_fields_startat_mismatch_fails_closed(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "fields": [
                            {"fieldId": "customfield_10001", "required": True},
                            {"fieldId": "labels", "required": False},
                        ],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": 3,
                    }
                ),
                http_response(
                    {
                        "fields": [{"fieldId": "priority", "required": True}],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": 3,
                    }
                ),
            ]
        )
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 2)

    def test_create_fields_mapping_shaped_payload_fails_closed(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "fields": {"priority": {"required": True}},
                        "startAt": 0,
                        "maxResults": 50,
                        "total": 1,
                    }
                )
            ]
        )
        client = make_client(runner)
        # A mapping-shaped page is malformed: returning [] would let
        # doctor report "no required fields" as a complete answer.
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_required_create_fields(PROJECT_ID, "10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 1)

    def test_create_fields_ignore_malformed_records(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "fields": [
                            "not-a-mapping",
                            {"required": True},
                            {"fieldId": "bad id!", "required": True},
                            {"fieldId": "priority", "required": "yes"},
                            {"fieldId": "priority", "required": True},
                        ],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": 5,
                    }
                )
            ]
        )
        client = make_client(runner)
        self.assertEqual(
            client.get_required_create_fields(PROJECT_ID, "10001"), ["priority"]
        )

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
        entry = bodies[0]["projectPermissions"][0]
        self.assertEqual(
            entry["permissions"],
            [
                "BROWSE_PROJECTS",
                "CREATE_ISSUES",
                "EDIT_ISSUES",
                "TRANSITION_ISSUES",
                "ADD_COMMENTS",
            ],
        )
        self.assertEqual(entry["projects"], [int(PROJECT_ID)])
        self.assertNotIn("projectId", entry)
        self.assertTrue(runner.calls[0]["url"].endswith("/rest/api/3/permissions/check"))
        self.assertEqual(runner.calls[0]["method"], "POST")

    def test_permission_names_are_bounded_tokens(self) -> None:
        runner = FakeHttp([])
        client = make_client(runner)
        with self.assertRaises(ValueError):
            client.check_permissions(PROJECT_ID, ["browse_projects"])
        self.assertEqual(runner.calls, [])

    def test_permission_denied_and_missing_are_false(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "globalPermissions": [],
                        "projectPermissions": [
                            {
                                "permission": "BROWSE_PROJECTS",
                                "projects": [int(PROJECT_ID)],
                                "issues": [],
                            }
                        ],
                    }
                )
            ]
        )
        client = make_client(runner)
        permissions = client.check_permissions(PROJECT_ID)
        self.assertTrue(permissions["BROWSE_PROJECTS"])
        self.assertFalse(permissions["CREATE_ISSUES"])
        self.assertFalse(permissions["EDIT_ISSUES"])

    def test_permission_out_of_project_is_denied(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "globalPermissions": [],
                        "projectPermissions": [
                            {
                                "permission": "BROWSE_PROJECTS",
                                "projects": [99999],
                                "issues": [],
                            },
                            {
                                "permission": "CREATE_ISSUES",
                                "projects": [int(PROJECT_ID)],
                                "issues": [],
                            },
                        ],
                    }
                )
            ]
        )
        client = make_client(runner)
        permissions = client.check_permissions(PROJECT_ID)
        self.assertFalse(permissions["BROWSE_PROJECTS"])
        self.assertTrue(permissions["CREATE_ISSUES"])

    def test_permission_malformed_records_are_ignored(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "globalPermissions": [],
                        "projectPermissions": [
                            "not-a-mapping",
                            {"permission": "BROWSE_PROJECTS"},
                            {"permission": "BROWSE_PROJECTS", "projects": "10001"},
                            {
                                "permission": "CREATE_ISSUES",
                                "projects": [True, None, "abc", int(PROJECT_ID)],
                                "issues": "not-a-list",
                            },
                            {
                                "permission": "UNKNOWN_PERMISSION",
                                "projects": [int(PROJECT_ID)],
                                "issues": [],
                            },
                        ],
                    }
                )
            ]
        )
        client = make_client(runner)
        permissions = client.check_permissions(PROJECT_ID)
        self.assertFalse(permissions["BROWSE_PROJECTS"])
        self.assertTrue(permissions["CREATE_ISSUES"])
        self.assertFalse(permissions["EDIT_ISSUES"])

    def test_permission_numeric_string_project_id_is_tolerated(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "globalPermissions": [],
                        "projectPermissions": [
                            {
                                "permission": "EDIT_ISSUES",
                                "projects": [PROJECT_ID],
                                "issues": [],
                            }
                        ],
                    }
                )
            ]
        )
        client = make_client(runner)
        permissions = client.check_permissions(PROJECT_ID)
        self.assertTrue(permissions["EDIT_ISSUES"])


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

    def test_default_factory_constructs_client_without_injection(self) -> None:
        """Exercise the real default factory path with bounded fake network.

        No ``client_factory`` is injected, so ``_probe_jira_read`` builds
        the production ``JiraReadClient``. Only the network boundary is
        faked by patching ``default_http_runner`` with a scripted,
        offline runner; a duplicate-keyword TypeError here would surface
        as a misleading ``malformed`` read failure.
        """
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        scripted = FakeHttp(ready_script())
        timeouts: list[float] = []

        def fake_default(
            method: str,
            url: str,
            headers: Mapping[str, str],
            body: bytes | None,
            *,
            timeout_seconds: float = 5,
            max_response_bytes: int = jira_cloud.MAX_RESPONSE_BYTES,
        ) -> tuple[int, Mapping[str, str], bytes]:
            timeouts.append(float(timeout_seconds))
            return scripted(method, url, headers, body)

        with mock.patch.object(
            jira_cloud, "default_http_runner", fake_default
        ):
            checks = jira_doctor.check_jira_tracker_readiness(
                config=jira_config(),
                env=dict(env),
                client_factory=None,
            )
        by_id = checks_by_id(checks)
        self.assertEqual(by_id[jira_doctor.JIRA_CONFIG_CHECK].status, "pass")
        self.assertEqual(by_id[jira_doctor.JIRA_CREDENTIALS_CHECK].status, "pass")
        read = by_id[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "pass")
        self.assertEqual(read.detail.get("reason"), "ready")
        # All six probe requests flowed through the bounded fake network.
        self.assertEqual(len(scripted.calls), 6)
        self.assertTrue(
            scripted.calls[0]["url"].startswith(
                f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/"
            )
        )
        # Production construction passes timeout_seconds through exactly once.
        self.assertTrue(timeouts)
        for value in timeouts:
            self.assertEqual(value, 5.0)
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

    def test_wrong_cloud_fails_on_mismatched_returned_site(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(),
            [http_response({"baseUrl": "https://other.atlassian.net"})],
            env=env,
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "wrong-cloud")

    def test_server_identity_allows_trailing_slash(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(site_url=f"{SITE_URL}/"), ready_script(), env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "pass")

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

    def test_browse_projects_denial_fails_readiness(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        script = ready_script()
        script[4] = http_response(
            {"globalPermissions": [], "projectPermissions": []}
        )
        checks, _ = run_doctor_checks(jira_config(), script, env=env)
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "forbidden")

    def test_rejected_request_has_non_transient_remediation(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks, _ = run_doctor_checks(
            jira_config(), [http_error(400, b"{}")], env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "rejected")
        self.assertNotIn("transient", str(read.remediation).lower())

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


class FakeRedirectResponse:
    """Minimal context-manager response for opener wiring tests."""

    def __init__(self, payload: bytes) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self._payload = payload

    def read(self, limit: int = -1) -> bytes:
        return self._payload

    def __enter__(self) -> FakeRedirectResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def redirect_handler_for(location: str) -> type[BaseHTTPRequestHandler]:
    """Build a localhost handler that 302-redirects every GET to location."""

    class RedirectOnceHandler(BaseHTTPRequestHandler):
        hits: list[str] = []

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            type(self).hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    return RedirectOnceHandler


def start_local_server(
    handler_cls: type[BaseHTTPRequestHandler],
) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    return server, thread


def closed_local_port() -> int:
    import socket as socket_module

    sock = socket_module.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class RedirectRejectionTests(unittest.TestCase):
    CODES = (301, 302, 303, 307, 308)

    def test_handler_rejects_same_host_cross_host_and_downgrade(self) -> None:
        handler = jira_cloud._RejectRedirectHandler()
        origin = "https://api.atlassian.com/ex/jira/x"
        targets = {
            "same-host": "https://api.atlassian.com/ex/jira/other",
            "cross-host": "https://collector.example/x",
            "downgrade": "http://api.atlassian.com/ex/jira/other",
        }
        for label, target in targets.items():
            for code in self.CODES:
                with self.subTest(target=label, code=code):
                    req = urllib.request.Request(
                        origin, headers={"Authorization": "Basic REDACTED"}
                    )
                    with self.assertRaises(jira_cloud.JiraRedirectRejected):
                        handler.redirect_request(req, None, code, "Moved", {}, target)

    def test_rejection_carries_no_urls_or_secrets(self) -> None:
        handler = jira_cloud._RejectRedirectHandler()
        req = urllib.request.Request("https://api.atlassian.com/ex/jira/x")
        try:
            handler.redirect_request(
                req, None, 302, "Moved", {}, "https://collector.example/x"
            )
            self.fail("redirect was not rejected")
        except jira_cloud.JiraRedirectRejected as exc:
            self.assertNotIn("collector.example", str(exc))
            self.assertNotIn("api.atlassian.com", str(exc))

    def test_default_runner_uses_redirect_rejecting_opener(self) -> None:
        captured: dict[str, Any] = {}

        def fake_build_opener(*handlers: Any) -> Any:
            captured["handlers"] = handlers

            class FakeOpener:
                def open(self, request: Any, timeout: Any = None) -> Any:
                    captured["request"] = request
                    return FakeRedirectResponse(b'{"baseUrl": "https://example.atlassian.net"}')

            return FakeOpener()

        with mock.patch.object(
            urllib.request, "build_opener", fake_build_opener
        ):
            with mock.patch.object(
                urllib.request,
                "urlopen",
                side_effect=AssertionError("must use the rejecting opener"),
            ):
                status, _, raw = jira_cloud.default_http_runner(
                    "GET",
                    "https://api.atlassian.com/ex/jira/x",
                    {"Accept": "application/json"},
                    None,
                )
        self.assertEqual(status, 200)
        self.assertIn(b"example.atlassian.net", raw)
        self.assertTrue(
            any(
                handler is jira_cloud._RejectRedirectHandler
                for handler in captured["handlers"]
            )
        )

    def test_same_host_redirect_is_not_followed(self) -> None:
        handler_cls = redirect_handler_for("/target")
        handler_cls.hits = []
        server, thread = start_local_server(handler_cls)
        try:
            port = int(server.server_address[1])
            with self.assertRaises(jira_cloud.JiraRedirectRejected):
                jira_cloud.default_http_runner(
                    "GET",
                    f"http://127.0.0.1:{port}/start",
                    {"Authorization": "Basic REDACTED"},
                    None,
                )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        # Only the first request was made; the redirect target never saw
        # the request, so no Authorization header could be forwarded.
        self.assertEqual(handler_cls.hits, ["/start"])

    def test_cross_host_redirect_never_connects(self) -> None:
        # The redirect target is a closed port: following the redirect
        # would raise ConnectionRefusedError, so JiraRedirectRejected
        # proves the credentialed second request was never attempted.
        target = f"http://127.0.0.1:{closed_local_port()}/collect"
        handler_cls = redirect_handler_for(target)
        handler_cls.hits = []
        server, thread = start_local_server(handler_cls)
        try:
            port = int(server.server_address[1])
            with self.assertRaises(jira_cloud.JiraRedirectRejected):
                jira_cloud.default_http_runner(
                    "GET",
                    f"http://127.0.0.1:{port}/start",
                    {"Authorization": "Basic REDACTED"},
                    None,
                )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(handler_cls.hits, ["/start"])

    def test_client_maps_rejected_redirect_without_retry(self) -> None:
        sleeps: list[float] = []
        runner = FakeHttp(
            [jira_cloud.JiraRedirectRejected("refusing HTTP redirect (302)")]
        )
        client = make_client(runner, sleeps=sleeps, max_attempts=4)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_server_info()
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(ctx.exception.endpoint, "serverInfo")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(sleeps, [])


class IssueTypePaginationTests(unittest.TestCase):
    @staticmethod
    def page(entries: list[tuple[str, str]], start: int, total: int) -> Any:
        return http_response(
            {
                "issueTypes": [{"id": type_id, "name": name} for type_id, name in entries],
                "startAt": start,
                "maxResults": 50,
                "total": total,
            }
        )

    def test_multi_page_success(self) -> None:
        runner = FakeHttp(
            [
                self.page([("10001", "Task"), ("10002", "Bug")], 0, 3),
                self.page([("10003", "Story")], 2, 3),
            ]
        )
        client = make_client(runner)
        types = client.get_issue_types(PROJECT_ID)
        self.assertEqual([item["id"] for item in types], ["10001", "10002", "10003"])
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("startAt=0", runner.calls[0]["url"])
        self.assertIn("startAt=2", runner.calls[1]["url"])

    def test_page_bound_fails_closed(self) -> None:
        script = [
            self.page([(f"1000{index}", f"Type{index}")], index, 10000)
            for index in range(jira_cloud.CREATEMETA_MAX_PAGES)
        ]
        runner = FakeHttp(script)
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_issue_types(PROJECT_ID)
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), jira_cloud.CREATEMETA_MAX_PAGES)

    def test_repeated_state_fails_closed(self) -> None:
        page = {
            "issueTypes": [{"id": "10001", "name": "Task"}],
            "startAt": 0,
            "maxResults": 50,
            "total": 999,
        }
        runner = FakeHttp([http_response(page), http_response(page)])
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_issue_types(PROJECT_ID)
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 2)

    def test_malformed_total_fails_closed(self) -> None:
        runner = FakeHttp(
            [
                http_response(
                    {
                        "issueTypes": [{"id": "10001", "name": "Task"}],
                        "startAt": 0,
                        "maxResults": 50,
                        "total": "not-a-number",
                    }
                )
            ]
        )
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_issue_types(PROJECT_ID)
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 1)

    def test_startat_mismatch_fails_closed(self) -> None:
        runner = FakeHttp(
            [
                self.page([("10001", "Task"), ("10002", "Bug")], 0, 3),
                self.page([("10003", "Story")], 0, 3),
            ]
        )
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.get_issue_types(PROJECT_ID)
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(len(runner.calls), 2)


class SearchPaginationTests(unittest.TestCase):
    @staticmethod
    def issue_page(
        keys: list[str], token: Any = None, is_last: bool = True
    ) -> Any:
        issues = [
            {
                "id": f"2000{index}",
                "key": key,
                "fields": {"status": {"id": "10000", "name": "To Do"}},
            }
            for index, key in enumerate(keys)
        ]
        payload: dict[str, Any] = {"issues": issues, "isLast": is_last}
        if token is not None:
            payload["nextPageToken"] = token
        return http_response(payload)

    def test_empty_pages_with_changing_tokens_terminate(self) -> None:
        runner = FakeHttp(
            [
                self.issue_page([], token="token-1", is_last=False),
                self.issue_page([], token="token-2", is_last=False),
                self.issue_page(["ABC-1"]),
            ]
        )
        client = make_client(runner)
        result = client.search_issues("project = 10001")
        self.assertFalse(result["truncated"])
        self.assertEqual([issue["key"] for issue in result["issues"]], ["ABC-1"])
        self.assertEqual(len(runner.calls), 3)

    def test_repeated_token_returns_truncated_result(self) -> None:
        runner = FakeHttp(
            [
                self.issue_page(["ABC-1"], token="token-1", is_last=False),
                self.issue_page(["ABC-2"], token="token-1", is_last=False),
            ]
        )
        client = make_client(runner)
        result = client.search_issues("project = 10001")
        self.assertTrue(result["truncated"])
        self.assertEqual(
            [issue["key"] for issue in result["issues"]], ["ABC-1", "ABC-2"]
        )
        self.assertEqual(len(runner.calls), 2)

    def test_malformed_token_fails_closed(self) -> None:
        runner = FakeHttp([self.issue_page(["ABC-1"], token=123, is_last=False)])
        client = make_client(runner)
        with self.assertRaises(jira_cloud.JiraApiError) as ctx:
            client.search_issues("project = 10001")
        self.assertEqual(ctx.exception.code, "jira_unavailable")
        self.assertEqual(ctx.exception.endpoint, "search")
        self.assertEqual(len(runner.calls), 1)

    def test_page_cap_returns_truncated_result(self) -> None:
        script = [
            self.issue_page([], token=f"token-{index}", is_last=False)
            for index in range(jira_cloud.SEARCH_MAX_PAGES + 5)
        ]
        runner = FakeHttp(script)
        client = make_client(runner)
        result = client.search_issues("project = 10001")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(runner.calls), jira_cloud.SEARCH_MAX_PAGES)

    def test_is_last_false_without_token_is_truncated(self) -> None:
        runner = FakeHttp([self.issue_page(["ABC-1"], is_last=False)])
        client = make_client(runner)
        result = client.search_issues("project = 10001")
        self.assertTrue(result["truncated"])
        self.assertEqual([issue["key"] for issue in result["issues"]], ["ABC-1"])
        self.assertEqual(len(runner.calls), 1)


class KeychainProfileEmailTests(unittest.TestCase):
    def test_env_service_with_selected_profile_email(self) -> None:
        import tempfile

        seen: list[Any] = []

        def fake_keychain(argv: Any, env: Any) -> str:
            seen.append(tuple(argv))
            return TOKEN

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "team.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={SERVICE}\n",
            )
            env = {jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: "env-service"}
            resolution = jira_cloud.resolve_jira_credentials(
                profile="team",
                config_dir=config_dir,
                env=env,
                keychain_runner=fake_keychain,
            )
        self.assertTrue(resolution.has_credentials)
        self.assertTrue(resolution.keychain_used)
        self.assertEqual(resolution.email, EMAIL)
        # The email comes from the selected profile while the service
        # comes from the environment; neither value leaks into argv
        # beyond the Keychain account lookup itself.
        self.assertEqual(len(seen), 1)
        self.assertIn("env-service", seen[0])
        self.assertIn(EMAIL, seen[0])

    def test_explicit_credential_file_email_with_env_service(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(
                Path(tmp),
                "custom.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={SERVICE}\n",
            )
            env = {jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE}
            resolution = jira_cloud.resolve_jira_credentials(
                credential_file=path,
                env=env,
                keychain_runner=lambda argv, env: TOKEN,
            )
        self.assertTrue(resolution.has_credentials)
        self.assertEqual(resolution.email, EMAIL)
        self.assertTrue(resolution.keychain_used)

    def test_ambient_email_wins_over_profile_email(self) -> None:
        import tempfile

        seen: list[Any] = []

        def fake_keychain(argv: Any, env: Any) -> str:
            seen.append(tuple(argv))
            return TOKEN

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "team.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}=stored@example.com\n"
                f"{jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV}={SERVICE}\n",
            )
            env = {
                jira_cloud.JIRA_EMAIL_ENV: EMAIL,
                jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE,
            }
            resolution = jira_cloud.resolve_jira_credentials(
                profile="team",
                config_dir=config_dir,
                env=env,
                keychain_runner=fake_keychain,
            )
        self.assertTrue(resolution.has_credentials)
        self.assertEqual(resolution.email, EMAIL)
        self.assertIn(EMAIL, seen[0])
        self.assertNotIn("stored@example.com", "".join(seen[0]))


class EmailAliasTests(unittest.TestCase):
    ALIAS = jira_cloud.JIRA_ACCOUNT_EMAIL_ENV

    def test_ambient_alias_email_resolves(self) -> None:
        env = {self.ALIAS: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        resolution = jira_cloud.resolve_jira_credentials(env=env)
        self.assertEqual(resolution.status, "ok")
        self.assertEqual(resolution.source, "env")
        self.assertEqual(resolution.email, EMAIL)
        self.assertEqual(resolution.token, TOKEN)

    def test_primary_email_wins_over_alias(self) -> None:
        env = {
            jira_cloud.JIRA_EMAIL_ENV: EMAIL,
            self.ALIAS: "other@example.com",
            jira_cloud.JIRA_TOKEN_ENV: TOKEN,
        }
        resolution = jira_cloud.resolve_jira_credentials(env=env)
        self.assertEqual(resolution.status, "ok")
        self.assertEqual(resolution.email, EMAIL)

    def test_profile_file_alias_email_resolves(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{self.ALIAS}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={TOKEN}\n",
            )
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env={}
            )
        self.assertEqual(resolution.status, "ok")
        self.assertEqual(resolution.source, "single_profile")
        self.assertEqual(resolution.email, EMAIL)

    def test_alias_email_completes_keychain_token(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                self.ALIAS: EMAIL,
                jira_cloud.JIRA_KEYCHAIN_SERVICE_ENV: SERVICE,
            }
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=Path(tmp),
                env=env,
                keychain_runner=lambda argv, env: TOKEN,
            )
        self.assertTrue(resolution.has_credentials)
        self.assertEqual(resolution.email, EMAIL)
        self.assertTrue(resolution.keychain_used)

    def test_partial_ambient_alias_fails_closed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{jira_cloud.JIRA_EMAIL_ENV}={EMAIL}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={TOKEN}\n",
            )
            # An ambient alias email is authoritative ambient presence, so
            # the missing token fails closed instead of falling back to
            # the stored profile.
            resolution = jira_cloud.resolve_jira_credentials(
                config_dir=config_dir, env={self.ALIAS: EMAIL}
            )
        self.assertEqual(resolution.status, "missing")
        self.assertFalse(resolution.has_credentials)

    def test_alias_values_never_appear_in_diagnostics(self) -> None:
        import tempfile

        secret_email = "alias-secret@example.com"
        secret_token = "tok-alias-secret-7"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            write_profile(
                config_dir,
                "jira.env",
                f"{self.ALIAS}={secret_email}\n"
                f"{jira_cloud.JIRA_TOKEN_ENV}={secret_token}\n",
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
        self.assertNotIn(secret_email, blob)
        self.assertNotIn(secret_token, blob)
        self.assertNotIn(str(config_dir), blob)


class DoctorFailClosedTests(unittest.TestCase):
    def test_malformed_create_meta_pagination_fails_read(self) -> None:
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(load_fixture("issue_types.json")),
            http_response(
                {
                    "fields": [{"fieldId": "priority", "required": True}],
                    "startAt": 0,
                    "maxResults": 50,
                    "total": "not-a-number",
                }
            ),
        ]
        checks, _ = run_doctor_checks(
            jira_config(issue_type_id="10002"), script, env=env
        )
        read = checks_by_id(checks)[jira_doctor.JIRA_READ_CHECK]
        # Doctor must fail the probe rather than report the partial
        # ["priority"] field set as complete.
        self.assertEqual(read.status, "fail")
        self.assertEqual(read.detail.get("reason"), "unavailable")
        self.assertNotIn("required_create_fields", read.detail)
        blob = doctor_blob(checks)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(EMAIL, blob)


if __name__ == "__main__":
    unittest.main()
