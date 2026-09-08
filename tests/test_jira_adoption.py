#!/usr/bin/env python3
"""Focused tests for Jira adoption, doctor readiness, and rehearsal (issue #803).

These tests run completely offline and perform zero live network calls.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping
import unittest
from unittest import mock

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init
from code_mower import jira_cloud
from code_mower import jira_mutations
from code_mower.doctor_checks import jira as jira_doctor

FIXTURES = Path(__file__).parent / "fixtures" / "jira"
CONFIG_TEMPLATE = (
    Path(code_mower_init.__file__).parent / "templates" / "code-mower.example.yml"
)

CLOUD_ID = "11111111-2222-3333-4444-555555555555"
SITE_URL = "https://example.atlassian.net"
PROJECT_ID = "10001"
EMAIL = "user@example.com"
TOKEN = "tok-1"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def http_response(payload: Any, status: int = 200, headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), json.dumps(payload).encode("utf-8"))


def http_error(status: int, body: bytes = b"{}", headers: Mapping[str, str] | None = None):
    return (status, dict(headers or {}), body)


class FakeHttp:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        if not self.script:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client_factory(runner: FakeHttp):
    def factory(**kwargs: Any) -> jira_cloud.JiraReadClient:
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

    return factory


def sample_jira_config(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "site_url": SITE_URL,
        "cloud_id": CLOUD_ID,
        "project_id": PROJECT_ID,
        "project_key": "ABC",
    }
    block.update(overrides)
    return {"tracker": {"kind": "jira_cloud", "jira_cloud": block}}


def base_probe_script() -> list[Any]:
    return [
        http_response(load_fixture("server_info.json")),
        http_response(load_fixture("project.json")),
        http_response(load_fixture("statuses.json")),
        http_response(load_fixture("status_categories.json")),
        http_response(load_fixture("permissions.json")),
        http_response(load_fixture("search_page2.json")),
    ]


class JiraInitTests(unittest.TestCase):
    def test_default_init_is_github_only(self) -> None:
        base_cfg = code_mower_config.load_config(CONFIG_TEMPLATE)
        plan = code_mower_init.render_init_plan(base_cfg)
        self.assertEqual(plan.data.get("tracker"), "github")
        self.assertNotIn("Work tracker:", plan.text)
        config_entry = next(f for f in plan.data["generated_files"] if f["path"] == "code-mower.yml")
        self.assertIsNone(config_entry.get("config_data"))

    def test_init_with_jira_tracker(self) -> None:
        base_cfg = code_mower_config.load_config(CONFIG_TEMPLATE)
        plan = code_mower_init.render_init_plan(base_cfg, tracker="jira_cloud")
        self.assertEqual(plan.data.get("tracker"), "jira_cloud")
        self.assertIn("Work tracker:", plan.text)
        self.assertIn("kind: jira_cloud", plan.text)
        config_entry = next(f for f in plan.data["generated_files"] if f["path"] == "code-mower.yml")
        parsed = config_entry["config_data"]
        self.assertIn("tracker", parsed)
        tracker = parsed["tracker"]
        self.assertEqual(tracker.get("kind"), "jira_cloud")
        jc = tracker.get("jira_cloud", {})
        self.assertEqual(jc.get("site_url"), "https://example.atlassian.net")
        self.assertEqual(jc.get("cloud_id"), "11111111-2222-3333-4444-555555555555")
        self.assertEqual(jc.get("project_id"), "10001")
        self.assertEqual(jc.get("project_key"), "ABC")
        mutations = jc.get("mutations", {})
        self.assertFalse(mutations.get("writes_enabled"))
        self.assertEqual(
            mutations.get("allowed_operations"),
            ["assign", "transition", "comment", "link"],
        )

    def test_init_cli_main_dry_run_with_jira(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            orig = os.getcwd()
            os.chdir(tmp_dir)
            try:
                stdout = io.StringIO()
                with mock.patch("sys.stdout", stdout):
                    rc = code_mower_init.main([str(CONFIG_TEMPLATE), "--dry-run", "--jira"])
                self.assertEqual(rc, 0)
                out = stdout.getvalue()
                self.assertIn("Work tracker:", out)
                self.assertIn("kind: jira_cloud", out)

                stdout_gh = io.StringIO()
                with mock.patch("sys.stdout", stdout_gh):
                    rc_gh = code_mower_init.main([str(CONFIG_TEMPLATE), "--dry-run"])
                self.assertEqual(rc_gh, 0)
                out_gh = stdout_gh.getvalue()
                self.assertNotIn("Work tracker:", out_gh)
            finally:
                os.chdir(orig)

    def test_init_apply_writes_valid_config_and_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            orig = os.getcwd()
            os.chdir(tmp_dir)
            try:
                stdout = io.StringIO()
                with mock.patch("sys.stdout", stdout):
                    rc = code_mower_init.main([str(CONFIG_TEMPLATE), "--apply", "--jira"])
                self.assertEqual(rc, 0)
                out = stdout.getvalue()
                self.assertIn("Jira Cloud next steps:", out)
                self.assertIn("review tracker.jira_cloud in code-mower.yml", out)
                config_file = Path(tmp_dir) / ".code-mower.generated" / "code-mower.yml"
                self.assertTrue(config_file.exists())
                loaded = code_mower_config.load_config(config_file)
                self.assertEqual(loaded.get("tracker", {}).get("kind"), "jira_cloud")
                issues = code_mower_config.validate_config(loaded)
                self.assertEqual(issues, [])
            finally:
                os.chdir(orig)

    def test_packaged_example_yml_contains_jira_block(self) -> None:
        content = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("tracker:", content)
        self.assertIn("kind: jira_cloud", content)
        self.assertIn("site_url: \"https://example.atlassian.net\"", content)
        self.assertIn("writes_enabled: false", content)


class JiraDoctorAdoptionTests(unittest.TestCase):
    def test_doctor_adoption_four_jira_checks_pass(self) -> None:
        cfg = sample_jira_config()
        runner = FakeHttp(base_probe_script())
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env=env,
            client_factory=factory,
        )
        by_name = {c.name: c for c in checks}
        self.assertIn(jira_doctor.JIRA_CONFIG_CHECK, by_name)
        self.assertIn(jira_doctor.JIRA_CREDENTIALS_CHECK, by_name)
        self.assertIn(jira_doctor.JIRA_READ_CHECK, by_name)
        self.assertIn(jira_doctor.JIRA_MUTATIONS_CHECK, by_name)
        self.assertEqual(by_name[jira_doctor.JIRA_CONFIG_CHECK].status, "pass")
        self.assertEqual(by_name[jira_doctor.JIRA_CREDENTIALS_CHECK].status, "pass")
        self.assertEqual(by_name[jira_doctor.JIRA_READ_CHECK].status, "pass")
        self.assertEqual(by_name[jira_doctor.JIRA_MUTATIONS_CHECK].status, "pass")
        self.assertFalse(by_name[jira_doctor.JIRA_MUTATIONS_CHECK].detail.get("writes_enabled"))

    def test_doctor_mutations_configured_with_valid_transitions(self) -> None:
        cfg = sample_jira_config(
            status_category_map={"in_progress": ["10001"], "done": ["10002"]},
            mutations={
                "writes_enabled": False,
                "allowed_operations": ["assign", "transition"],
                "transitions": {"in_progress": "21"},
            },
        )
        perms_with_writes = {
            "globalPermissions": [],
            "projectPermissions": [
                {"issues": [], "permission": "BROWSE_PROJECTS", "projects": [10001]},
                {"issues": [], "permission": "EDIT_ISSUES", "projects": [10001]},
                {"issues": [], "permission": "TRANSITION_ISSUES", "projects": [10001]},
            ],
        }
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(perms_with_writes),
            http_response(load_fixture("search_page2.json")),
            http_response(load_fixture("transitions.json")),
        ]
        runner = FakeHttp(script)
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env=env,
            client_factory=factory,
        )
        by_name = {c.name: c for c in checks}
        mut = by_name[jira_doctor.JIRA_MUTATIONS_CHECK]
        self.assertEqual(mut.status, "pass")
        self.assertIn("writes disabled by config guard", mut.message)

    def test_doctor_mutations_fails_on_missing_permission(self) -> None:
        cfg = sample_jira_config(
            status_category_map={"in_progress": ["10001"]},
            mutations={
                "writes_enabled": False,
                "allowed_operations": ["transition"],
                "transitions": {"in_progress": "21"},
            },
        )
        # Permissions response has only BROWSE_PROJECTS, lacks TRANSITION_ISSUES
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(load_fixture("permissions.json")),
            http_response(load_fixture("search_page2.json")),
            http_response(load_fixture("transitions.json")),
        ]
        runner = FakeHttp(script)
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env=env,
            client_factory=factory,
        )
        by_name = {c.name: c for c in checks}
        mut = by_name[jira_doctor.JIRA_MUTATIONS_CHECK]
        self.assertEqual(mut.status, "fail")
        self.assertEqual(mut.detail.get("reason"), "permission_denied")
        self.assertIn("TRANSITION_ISSUES", mut.detail.get("missing_permissions", []))

    def test_doctor_mutations_fails_on_target_status_not_found_in_jira(self) -> None:
        # Status ID 99999 is not in statuses.json. Search returns empty issues list,
        # so live transition probing is skipped and known_status_ids check fires.
        cfg = sample_jira_config(
            status_category_map={"in_progress": ["99999"]},
            mutations={
                "writes_enabled": False,
                "allowed_operations": ["transition"],
                "transitions": {"in_progress": "21"},
            },
        )
        perms_with_writes = {
            "globalPermissions": [],
            "projectPermissions": [
                {"issues": [], "permission": "BROWSE_PROJECTS", "projects": [10001]},
                {"issues": [], "permission": "TRANSITION_ISSUES", "projects": [10001]},
            ],
        }
        empty_search = {"issues": [], "truncated": False}
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(perms_with_writes),
            http_response(empty_search),
        ]
        runner = FakeHttp(script)
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env=env,
            client_factory=factory,
        )
        by_name = {c.name: c for c in checks}
        mut = by_name[jira_doctor.JIRA_MUTATIONS_CHECK]
        self.assertEqual(mut.status, "fail")
        self.assertEqual(mut.detail.get("reason"), "status_not_found")
        self.assertEqual(mut.detail.get("status_id"), "99999")

    def test_doctor_mutations_fails_on_transition_target_mismatch(self) -> None:
        # Transition 21 in transitions.json leads to status 10001, but config expects 10002
        cfg = sample_jira_config(
            status_category_map={"in_progress": ["10002"]},
            mutations={
                "writes_enabled": False,
                "allowed_operations": ["transition"],
                "transitions": {"in_progress": "21"},
            },
        )
        perms_with_writes = {
            "globalPermissions": [],
            "projectPermissions": [
                {"issues": [], "permission": "BROWSE_PROJECTS", "projects": [10001]},
                {"issues": [], "permission": "TRANSITION_ISSUES", "projects": [10001]},
            ],
        }
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(perms_with_writes),
            http_response(load_fixture("search_page2.json")),
            http_response(load_fixture("transitions.json")),
        ]
        runner = FakeHttp(script)
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env=env,
            client_factory=factory,
        )
        by_name = {c.name: c for c in checks}
        mut = by_name[jira_doctor.JIRA_MUTATIONS_CHECK]
        self.assertEqual(mut.status, "fail")
        self.assertEqual(mut.detail.get("reason"), "transition_target_mismatch")

    def test_doctor_rate_limited_warns_and_skips_mutations(self) -> None:
        cfg = sample_jira_config()
        script = [http_error(429, b'{"rate":"limited"}', {"Retry-After": "0"})] * 4
        runner = FakeHttp(script)
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env=env,
            client_factory=factory,
        )
        by_name = {c.name: c for c in checks}
        self.assertEqual(by_name[jira_doctor.JIRA_READ_CHECK].status, "warn")
        self.assertEqual(by_name[jira_doctor.JIRA_MUTATIONS_CHECK].status, "skip")
        self.assertEqual(
            by_name[jira_doctor.JIRA_MUTATIONS_CHECK].detail.get("reason"),
            "rate-limited",
        )

    def test_doctor_credentials_fail_skips_read_and_mutations(self) -> None:
        cfg = sample_jira_config()
        checks = jira_doctor.check_jira_tracker_readiness(
            config=cfg,
            env={},
        )
        by_name = {c.name: c for c in checks}
        self.assertEqual(by_name[jira_doctor.JIRA_CONFIG_CHECK].status, "pass")
        self.assertEqual(by_name[jira_doctor.JIRA_CREDENTIALS_CHECK].status, "fail")
        self.assertEqual(by_name[jira_doctor.JIRA_READ_CHECK].status, "skip")
        self.assertEqual(by_name[jira_doctor.JIRA_MUTATIONS_CHECK].status, "skip")


class JiraAdoptionRehearsalFlowTests(unittest.TestCase):
    def test_full_offline_rehearsal_flow(self) -> None:
        # Step 1: Render init plan with Jira tracker
        base_cfg = code_mower_config.load_config(CONFIG_TEMPLATE)
        plan = code_mower_init.render_init_plan(base_cfg, tracker="jira_cloud")
        config_entry = next(f for f in plan.data["generated_files"] if f["path"] == "code-mower.yml")
        config_dict = config_entry["config_data"]

        # Step 2: Validate config
        issues = code_mower_config.validate_config(config_dict)
        self.assertEqual(issues, [])

        # Operator configures transition to match their workflow (transition 21 leads to status 10001)
        config_dict["tracker"]["jira_cloud"]["mutations"]["transitions"]["in_progress"] = "21"

        # Step 3: Run doctor readiness checks
        perms_with_writes = {
            "globalPermissions": [],
            "projectPermissions": [
                {"issues": [], "permission": "BROWSE_PROJECTS", "projects": [10001]},
                {"issues": [], "permission": "EDIT_ISSUES", "projects": [10001]},
                {"issues": [], "permission": "TRANSITION_ISSUES", "projects": [10001]},
                {"issues": [], "permission": "ADD_COMMENTS", "projects": [10001]},
            ],
        }
        script = [
            http_response(load_fixture("server_info.json")),
            http_response(load_fixture("project.json")),
            http_response(load_fixture("statuses.json")),
            http_response(load_fixture("status_categories.json")),
            http_response(load_fixture("issue_types.json")),
            http_response(load_fixture("createmeta_task.json")),
            http_response(perms_with_writes),
            http_response(load_fixture("search_page2.json")),
            http_response(load_fixture("transitions.json")),
        ]
        runner = FakeHttp(script)
        factory = make_client_factory(runner)
        env = {jira_cloud.JIRA_EMAIL_ENV: EMAIL, jira_cloud.JIRA_TOKEN_ENV: TOKEN}
        checks = jira_doctor.check_jira_tracker_readiness(
            config=config_dict,
            env=env,
            client_factory=factory,
        )
        for check in checks:
            self.assertEqual(check.status, "pass", f"check {check.name} failed: {check.message}")

        # Step 4: Dry-run mutation plan (no writes)
        req = jira_mutations.MutationRequest(
            issue_ref="ABC-1",
            claim=True,
            comment_template="claimed",
        )
        plan_res = jira_mutations.build_mutation_plan(config_dict, req, apply_requested=False)
        self.assertEqual(plan_res["mode"], "plan")
        self.assertEqual(plan_res["status"], "planned")
        self.assertFalse(plan_res["guards"]["writes_authorized"])

        # Step 5: Verify apply is refused when writes_enabled is false
        apply_res = jira_mutations.build_mutation_plan(config_dict, req, apply_requested=True)
        self.assertEqual(apply_res["mode"], "plan")
        self.assertEqual(apply_res["status"], "refused")
        self.assertEqual(apply_res["guards"]["writes_enabled"], False)
        self.assertFalse(apply_res["guards"]["writes_authorized"])


if __name__ == "__main__":
    unittest.main()
