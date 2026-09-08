from __future__ import annotations

import copy
import io
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from code_mower import board, board_store, controller, jira_cloud, lane_status, tracker_queue
from code_mower import init as code_mower_init
from code_mower.cloud_client.events import build_board_snapshot_event
from code_mower.tracker_contract import validate_tracker_work_item
from test_board import _write_board_config
from test_controller import _config, _options, _pr, _status


NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
PROSE = "private issue prose must stay local"


def config():
    result = _config()
    result["tracker"] = {"kind": "jira_cloud", "jira_cloud": {
        "site_url": "https://example.atlassian.net", "cloud_id": "cloud-example",
        "project_id": "10001", "project_key": "ORDER",
        "jql": 'labels = "ready" OR project = 999 ORDER BY updated DESC',
    }}
    return result


def issue(number=1):
    return {"id": str(number), "key": f"ABC-{number}", "summary": PROSE,
            "fields": {"project": {"id": "10001"}, "summary": PROSE,
                       "description": PROSE, "comment": {"comments": [PROSE]},
                       "attachment": [PROSE], "customfield_999": PROSE,
                       "status": {"id": "10", "name": "Open", "statusCategory": {"key": "new"}},
                       "issuetype": {"name": "Task"}, "labels": ["builder:codex", "ready"],
                       "assignee": None, "created": "2026-09-01T10:00:00Z",
                       "updated": "2026-09-08T10:00:00Z"}}


class Reader:
    def __init__(self, *pages):
        self.pages = iter(pages or ({"issues": [issue()], "isLast": True},))
        self.calls = []

    def search_page(self, **kwargs):
        self.calls.append(kwargs)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page


def queue(reader=None, **kwargs):
    return tracker_queue.collect_queue(config(), reader=reader or Reader(), now=NOW, **kwargs)


def view(payload, *, prs=(), **kwargs):
    return tracker_queue.queue_view(payload, config=config(), remote={
        "available": True, "pull_requests": list(prs)}, now=NOW, **kwargs)


class TrackerQueueTests(unittest.TestCase):
    def test_queue_reader_resolves_credentials_without_exposing_them(self):
        captured = {}
        reader = Reader()

        def factory(**kwargs):
            captured.update(kwargs)
            return reader

        resolved = tracker_queue.resolve_jira_queue_reader(
            config(),
            env={
                jira_cloud.JIRA_EMAIL_ENV: "operator@example.com",
                jira_cloud.JIRA_TOKEN_ENV: "token-value",
            },
            client_factory=factory,
        )
        self.assertIs(resolved, reader)
        self.assertEqual(captured["cloud_id"], "cloud-example")
        self.assertEqual(captured["site_url"], "https://example.atlassian.net")
        self.assertIsNone(
            tracker_queue.resolve_jira_queue_reader(config(), env={})
        )

    def test_default_jira_client_reads_a_sanitized_queue_page(self):
        raw = issue()
        runner = Mock(
            side_effect=[
                (
                    200,
                    {},
                    json.dumps(
                        {"baseUrl": "https://example.atlassian.net"}
                    ).encode("utf-8"),
                ),
                (
                    200,
                    {},
                    json.dumps({"id": "10001", "key": "ORDER"}).encode("utf-8"),
                ),
                (
                    200,
                    {},
                    json.dumps({"issues": [raw], "isLast": True}).encode("utf-8"),
                ),
            ]
        )
        reader = tracker_queue.resolve_jira_queue_reader(
            config(),
            env={
                jira_cloud.JIRA_EMAIL_ENV: "operator@example.com",
                jira_cloud.JIRA_TOKEN_ENV: "token-value",
            },
            client_factory=lambda **kwargs: jira_cloud.JiraReadClient(
                **kwargs, http_runner=runner
            ),
        )

        result = queue(reader)

        self.assertTrue(result["complete"])
        self.assertEqual(result["freshness"], "live")
        self.assertEqual(result["items"][0]["identity"]["issue_key"], "ABC-1")
        request_body = json.loads(runner.call_args.args[3])
        self.assertNotIn("summary", request_body["fields"])
        self.assertNotIn(PROSE, json.dumps(result))

    def test_default_jira_client_rejects_wrong_tenant_before_queue_read(self):
        runner = Mock(
            return_value=(
                200,
                {},
                json.dumps({"baseUrl": "https://other.atlassian.net"}).encode(
                    "utf-8"
                ),
            )
        )

        reader = tracker_queue.resolve_jira_queue_reader(
            config(),
            env={
                jira_cloud.JIRA_EMAIL_ENV: "operator@example.com",
                jira_cloud.JIRA_TOKEN_ENV: "token-value",
            },
            client_factory=lambda **kwargs: jira_cloud.JiraReadClient(
                **kwargs, http_runner=runner
            ),
        )

        self.assertIsNone(reader)
        self.assertEqual(runner.call_count, 1)

    def test_queue_page_rejects_invalid_or_oversized_issue_sets(self):
        for raw_issues in ([{}], [issue(1), issue(2)]):
            runner = Mock(
                return_value=(
                    200,
                    {},
                    json.dumps({"issues": raw_issues, "isLast": True}).encode(
                        "utf-8"
                    ),
                )
            )
            client = jira_cloud.JiraReadClient(
                cloud_id="cloud-example",
                email="operator@example.com",
                token="token-value",
                site_url="https://example.atlassian.net",
                http_runner=runner,
            )
            with self.assertRaises(jira_cloud.JiraApiError):
                client.search_page(
                    jql="project = 10001",
                    fields=["project", "status"],
                    max_results=1,
                    next_page_token=None,
                )

    def test_queue_page_rejects_truncated_labels(self):
        raw_issue = issue()
        raw_issue["fields"]["labels"] = [
            f"label-{index}" for index in range(jira_cloud.MAX_LABELS)
        ] + ["needs-owner"]
        runner = Mock(
            return_value=(
                200,
                {},
                json.dumps({"issues": [raw_issue], "isLast": True}).encode("utf-8"),
            )
        )
        client = jira_cloud.JiraReadClient(
            cloud_id="cloud-example",
            email="operator@example.com",
            token="token-value",
            site_url="https://example.atlassian.net",
            http_runner=runner,
        )
        with self.assertRaises(jira_cloud.JiraApiError):
            client.search_page(
                jql="project = 10001",
                fields=["labels", "project", "status"],
                max_results=1,
                next_page_token=None,
            )

    def test_queue_page_rejects_truncated_custom_labels(self):
        raw_issue = issue()
        raw_issue["fields"]["customfield_123"] = [
            f"label-{index}" for index in range(jira_cloud.MAX_LABELS)
        ] + ["needs-owner"]
        runner = Mock(
            return_value=(
                200,
                {},
                json.dumps({"issues": [raw_issue], "isLast": True}).encode("utf-8"),
            )
        )
        client = jira_cloud.JiraReadClient(
            cloud_id="cloud-example",
            email="operator@example.com",
            token="token-value",
            site_url="https://example.atlassian.net",
            http_runner=runner,
        )
        with self.assertRaises(jira_cloud.JiraApiError):
            client.search_page(
                jql="project = 10001",
                fields=["customfield_123", "project", "status"],
                max_results=1,
                next_page_token=None,
            )

    def test_queue_page_accepts_missing_native_labels(self):
        raw_issue = issue()
        raw_issue["fields"].pop("labels")
        runner = Mock(
            return_value=(
                200,
                {},
                json.dumps({"issues": [raw_issue], "isLast": True}).encode("utf-8"),
            )
        )
        client = jira_cloud.JiraReadClient(
            cloud_id="cloud-example",
            email="operator@example.com",
            token="token-value",
            site_url="https://example.atlassian.net",
            http_runner=runner,
        )
        page = client.search_page(
            jql="project = 10001",
            fields=["labels", "project", "status"],
            max_results=1,
            next_page_token=None,
        )
        self.assertEqual(page["issues"][0]["fields"]["labels"], [])

    def test_queue_page_rejects_nested_custom_labels(self):
        raw_issue = issue()
        raw_issue["fields"]["customfield_123"] = [[PROSE]]
        runner = Mock(
            return_value=(
                200,
                {},
                json.dumps({"issues": [raw_issue], "isLast": True}).encode("utf-8"),
            )
        )
        client = jira_cloud.JiraReadClient(
            cloud_id="cloud-example",
            email="operator@example.com",
            token="token-value",
            site_url="https://example.atlassian.net",
            http_runner=runner,
        )
        with self.assertRaises(jira_cloud.JiraApiError):
            client.search_page(
                jql="project = 10001",
                fields=["customfield_123", "project", "status"],
                max_results=1,
                next_page_token=None,
            )

    def test_controller_cli_wires_profile_to_live_jira_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "code-mower.yml"
            rendered_config = config()
            rendered_config["tracker"]["jira_cloud"]["jql"] = "labels = ready"
            config_path.write_text(
                code_mower_init._render_provider_catalog(rendered_config),
                encoding="utf-8",
            )

            def gh_json(args):
                if args[:2] in (["pr", "list"], ["run", "list"]):
                    return []
                self.fail(f"unexpected GitHub call: {args}")

            def local(args):
                return subprocess.CompletedProcess(args, 1, "", "")

            with patch.object(
                tracker_queue,
                "resolve_jira_queue_reader",
                return_value=Reader(),
            ) as resolver:
                with patch("sys.stdout", new_callable=io.StringIO) as out:
                    code = controller.main(
                        [
                            "run",
                            "--repo",
                            "owner/repo",
                            "--config",
                            str(config_path),
                            "--provider-profile",
                            "jira.env",
                            "--mode",
                            "dry_run",
                            "--json",
                        ],
                        gh_json_runner=gh_json,
                        command_runner=local,
                    )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["decision"]["decision_state"], "dispatch_builder")
        self.assertEqual(resolver.call_args.kwargs["profile"], "jira.env")

    def test_lanes_cli_wires_profile_to_live_jira_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "code-mower.yml"
            rendered_config = config()
            rendered_config["tracker"]["jira_cloud"]["jql"] = "labels = ready"
            config_path.write_text(
                code_mower_init._render_provider_catalog(rendered_config),
                encoding="utf-8",
            )

            def gh_json(args):
                if args[:2] in (["pr", "list"], ["run", "list"]):
                    return []
                self.fail(f"unexpected GitHub call: {args}")

            def local(args):
                return subprocess.CompletedProcess(args, 1, "", "")

            with patch.object(
                tracker_queue,
                "resolve_jira_queue_reader",
                return_value=Reader(),
            ) as resolver:
                with patch("sys.stdout", new_callable=io.StringIO) as out:
                    code = lane_status.main(
                        [
                            "status",
                            "--repo",
                            "owner/repo",
                            "--config",
                            str(config_path),
                            "--provider-profile",
                            "jira.env",
                            "--json",
                        ],
                        gh_json_runner=gh_json,
                        command_runner=local,
                    )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["tracker"]["freshness"], "live")
        self.assertEqual(resolver.call_args.kwargs["profile"], "jira.env")

    def test_empty_and_paginated_stable_identity_and_scope(self):
        self.assertEqual(queue(Reader({"issues": [], "isLast": True}))["items"], [])
        reader = Reader({"issues": [issue(2)], "nextPageToken": "page-two"},
                        {"issues": [issue(1), issue(2)], "isLast": True})
        result = queue(reader)
        self.assertTrue(result["complete"])
        self.assertEqual([item["identity"]["issue_id"] for item in result["items"]], ["1", "2"])
        self.assertEqual(reader.calls[0]["jql"], 'project = 10001 AND (labels = "ready" OR project = 999) ORDER BY created ASC, key ASC')
        self.assertEqual(reader.calls[1]["next_page_token"], "page-two")
        self.assertNotIn("summary", reader.calls[0]["fields"])
        for item in result["items"]:
            self.assertEqual(validate_tracker_work_item(item), ())
        self.assertNotIn(PROSE, json.dumps(result))
        self.assertNotIn("jql", json.dumps(result))

    def test_quoted_order_by_and_invalid_scope(self):
        cfg = config()
        cfg["tracker"]["jira_cloud"]["jql"] = 'labels = "ORDER BY" ORDER BY updated DESC'
        reader = Reader()
        tracker_queue.collect_queue(cfg, reader=reader)
        self.assertIn('(labels = "ORDER BY")', reader.calls[0]["jql"])
        for project in ("ORDER", "10001 OR project = 999"):
            cfg["tracker"]["jira_cloud"]["project_id"] = project
            reader = Reader()
            self.assertFalse(tracker_queue.collect_queue(cfg, reader=reader)["available"])
            self.assertEqual(reader.calls, [])
        raw = issue()
        raw["fields"]["project"]["id"] = "999"
        self.assertFalse(queue(Reader({"issues": [raw]}))["available"])

    def test_jql_parenthesis_attacks_fail_before_reading(self):
        for predicate in (
            ') OR project != 10001 OR (',
            'labels = "ready") OR project = 999 OR (labels = "ready"',
            '(labels = "ready"', 'labels = "ready")',
            '(labels = "ready" ORDER BY created ASC)',
            r'labels = ready\) OR project = 999 OR (',
            'labels = "unterminated',
        ):
            with self.subTest(predicate=predicate):
                cfg = config()
                cfg["tracker"]["jira_cloud"]["jql"] = predicate
                reader = Reader()
                result = tracker_queue.collect_queue(cfg, reader=reader)
                self.assertFalse(result["available"])
                self.assertEqual(result["errors"], ["jira_read_unavailable"])
                self.assertEqual(reader.calls, [])

    def test_jql_balanced_and_quoted_parenthesis_boundaries(self):
        for predicate in (
            '', '(labels = "ready" OR (status in ("Open", "New")))',
            'labels = ")"', "labels = '('", r'labels = "escaped \" )"',
            'labels = "ORDER BY )"',
        ):
            with self.subTest(predicate=predicate):
                cfg = config()["tracker"]["jira_cloud"]
                cfg["jql"] = predicate + " ORDER BY updated DESC"
                expected = "project = 10001" + (f" AND ({predicate})" if predicate else "")
                self.assertEqual(tracker_queue._query(cfg), expected + " ORDER BY created ASC, key ASC")

    def test_exact_page_limit_with_fresh_cursors_is_partial_and_ineligible(self):
        for max_pages in (1, 3, 10):
            with self.subTest(max_pages=max_pages):
                reader = Reader(*(
                    {"issues": [issue(index + 1)], "isLast": False, "nextPageToken": f"page-{index + 1}"}
                    for index in range(max_pages + 1)
                ))
                result = queue(reader, max_pages=max_pages)
                self.assertEqual(len(reader.calls), max_pages)
                self.assertEqual([call["next_page_token"] for call in reader.calls],
                                 [None] + [f"page-{index}" for index in range(1, max_pages)])
                self.assertTrue(result["available"])
                self.assertFalse(result["complete"])
                self.assertEqual(result["freshness"], "partial")
                self.assertEqual(result["errors"], ["jira_page_limit"])
                rows = view(result)["items"]
                self.assertEqual(len(rows), max_pages)
                self.assertTrue(all(not row["eligible"] for row in rows))

    def test_bounded_repeated_missing_and_failing_pages(self):
        first = {"issues": [issue()], "nextPageToken": "repeat", "isLast": False}
        partial = queue(Reader(first), max_pages=1)
        self.assertEqual(partial["freshness"], "partial")
        self.assertFalse(view(partial)["items"][0]["eligible"])
        for second in (first, {"issues": [], "isLast": False}, RuntimeError(PROSE)):
            result = queue(Reader(first, second))
            self.assertFalse(result["available"])
            self.assertEqual(result["items"], [])
            self.assertNotIn(PROSE, json.dumps(result))
        self.assertFalse(tracker_queue.collect_queue(config())["available"])
        reader = Reader({"issues": [issue()] * 101})
        self.assertFalse(queue(reader, page_size=500)["available"])
        self.assertEqual(reader.calls[0]["max_results"], 100)

    def test_custom_fields_status_mapping_and_bounds(self):
        cfg = config()["tracker"]["jira_cloud"]
        cfg["status_category_map"] = {"blocked": ["10"]}
        cfg["field_mappings"] = {"labels": "customfield_100", "assigned": "customfield_101"}
        raw = issue()
        raw["fields"].update({"customfield_100": ["z", "a", "z", "x" * 129, {"description": PROSE}], "customfield_101": True})
        item = tracker_queue.normalize_jira_work_item(raw, cfg)
        self.assertEqual(item["lifecycle_category"], "blocked")
        self.assertEqual(item["labels"], ["a", "z"])
        self.assertTrue(item["assigned"])
        raw["fields"]["customfield_101"] = PROSE
        with self.assertRaises(ValueError):
            tracker_queue.normalize_jira_work_item(raw, cfg)
        cfg = config()
        cfg["tracker"]["jira_cloud"]["field_mappings"] = {"labels": "description"}
        reader = Reader()
        self.assertFalse(tracker_queue.collect_queue(cfg, reader=reader)["available"])
        self.assertEqual(reader.calls, [])

    def test_native_assignee_mapping_and_portable_custom_category(self):
        cfg = config()["tracker"]["jira_cloud"]
        cfg["field_mappings"] = {"assigned": "assignee", "lifecycle_category": "customfield_100"}
        raw = issue()
        raw["fields"].update({"assignee": {"displayName": PROSE}, "customfield_100": "blocked"})
        item = tracker_queue.normalize_jira_work_item(raw, cfg)
        self.assertTrue(item["assigned"])
        self.assertEqual(item["lifecycle_category"], "blocked")
        self.assertNotIn(PROSE, json.dumps(item))

    def test_malformed_completion_markers_and_unknown_categories_fail_closed(self):
        for metadata in ({"isLast": "false"}, {"nextPageToken": 0},
                         {"nextPageToken": ""}, {"isLast": True, "nextPageToken": "more"}):
            with self.subTest(metadata=metadata):
                result = queue(Reader({"issues": [issue()], **metadata}))
                self.assertFalse(result["available"])
                self.assertEqual(result["items"], [])
        raw = issue()
        raw["fields"]["status"]["statusCategory"]["key"] = "unknown"
        self.assertFalse(queue(Reader({"issues": [raw]}))["available"])

    def test_newest_duplicate_wins_and_page_count_is_bounded(self):
        older, newer = issue(), issue()
        newer["fields"].update({"updated": "2026-09-08T11:00:00Z", "assignee": {"displayName": PROSE}})
        result = queue(Reader({"issues": [newer], "nextPageToken": "more"}, {"issues": [older]}))
        self.assertTrue(result["items"][0]["assigned"])
        self.assertFalse(view(result)["items"][0]["eligible"])
        reader = Reader(*({"issues": [], "nextPageToken": str(index)} for index in range(20)))
        self.assertEqual(queue(reader, max_pages=100)["freshness"], "partial")
        self.assertEqual(len(reader.calls), 10)

    def test_stale_current_vs_historical_pr_and_gate(self):
        payload = queue()
        payload["observed_at"] = (NOW - timedelta(hours=2)).isoformat()
        payload["gate_status"] = "success"
        payload["pull_requests"] = [_pr()]
        links = {("cloud-example", "10001", "1"): 42}
        live_pr = _pr(checks=[{"name": "code-mower/gate", "state": "failure"}], next_action="fix failing check")
        result = view(payload, prs=[live_pr], links=links)
        row = result["items"][0]
        self.assertEqual(row["freshness"], "historical")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["gate_status"], "failure")
        self.assertEqual(row["next_action"], "fix failing check")
        self.assertEqual(row["lane_id"], "cursor")
        missing = view(payload, links=links)["items"][0]
        self.assertEqual(missing["gate_status"], "unknown")
        self.assertEqual(missing["pr_freshness"], "unavailable")

    def test_old_issue_update_is_not_old_observation(self):
        raw = issue()
        raw["fields"]["updated"] = "2020-01-01T00:00:00Z"
        row = view(queue(Reader({"issues": [raw]})))["items"][0]
        self.assertEqual(row["freshness"], "live")
        self.assertTrue(row["eligible"])

    def test_controller_jira_decision_and_github_priority(self):
        cfg = config()
        ready = controller._collect_ready_issues(repo="owner/repo", config=cfg,
            gh_json_runner=lambda args: self.fail("must not list GitHub issues"),
            issue_limit=50, tracker_view=view(queue()))
        report = controller.evaluate_controller_report(status_report=_status(), ready_issues=ready, config=cfg, options=_options())
        self.assertEqual(report["decision"]["decision_state"], "dispatch_builder")
        self.assertEqual(report["decision"]["work_item"]["identity"]["issue_key"], "ABC-1")
        self.assertIn("ABC-1", controller.render_text(report))
        event = controller.build_controller_event(report=report)
        self.assertNotIn(PROSE, json.dumps(event))
        unavailable = controller._collect_ready_issues(repo="owner/repo", config=cfg,
            gh_json_runner=lambda args: [], issue_limit=50)
        for issues in (ready, unavailable):
            mixed = controller.evaluate_controller_report(status_report=_status([_pr()]), ready_issues=issues, config=cfg, options=_options())
            baseline = controller.evaluate_controller_report(status_report=_status([_pr()]), ready_issues=None, config=_config(), options=_options())
            self.assertEqual(mixed["decision"], baseline["decision"])
        degraded = controller.evaluate_controller_report(status_report=_status(), ready_issues=unavailable, config=cfg, options=_options())
        self.assertEqual(degraded["decision"]["stop_condition"], "jira_unavailable")

    def test_status_board_and_history_are_metadata_only(self):
        def runner(args):
            return subprocess.CompletedProcess(args, 1, "", "")
        pr = {**_pr(), "title": "Public fixture", "gate_rerun_command": ""}
        with patch.object(lane_status, "_remote", return_value=_status([pr])["remote"]):
            baseline = lane_status.collect_status(repo="owner/repo", command_runner=runner, now=NOW)
            github = lane_status.collect_status(repo="owner/repo", command_runner=runner, now=NOW, tracker_config=_config())
            self.assertEqual(json.dumps(baseline, sort_keys=True), json.dumps(github, sort_keys=True))
            status = lane_status.collect_status(repo="owner/repo", command_runner=runner, now=NOW, tracker_config=config(), jira_reader=Reader())
        self.assertEqual(status["remote"], baseline["remote"])
        self.assertIn("ABC-1", lane_status.render_text(status))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path = path / "code-mower.yml"
            _write_board_config(config_path)
            with config_path.open("a") as stream:
                stream.write(yaml.safe_dump({"tracker": config()["tracker"]}, width=2000))
            with patch.object(lane_status, "collect_status", return_value=copy.deepcopy(baseline)):
                payload = board.status_payload(board.BoardConfig(repo="owner/repo", repo_path=path), jira_reader=Reader())
            self.assertEqual(payload["tracker"]["items"][0]["work_item"]["identity"]["issue_key"], "ABC-1")
            history = board_store.snapshot_event(payload)
            cloud = build_board_snapshot_event(repo_slug="owner/repo", snapshot=payload, team_id="", install_id="", source="test")
            for artifact in (payload, history, cloud):
                encoded = json.dumps(artifact)
                self.assertNotIn(PROSE, encoded)
                self.assertNotIn(config()["tracker"]["jira_cloud"]["jql"], encoded)
            with patch.object(lane_status, "collect_status", return_value=copy.deepcopy(baseline)):
                degraded = board.status_payload(board.BoardConfig(repo="owner/repo", repo_path=path))
            self.assertEqual(degraded["tracker"]["freshness"], "unavailable")
            self.assertEqual(degraded["remote"], baseline["remote"])

    def test_board_live_jira_survives_github_outage_without_mutating_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path = path / "code-mower.yml"
            _write_board_config(config_path)
            with config_path.open("a") as stream:
                stream.write(yaml.safe_dump({"tracker": config()["tracker"]}, width=2000))
            cfg = board.BoardConfig(repo="owner/repo", repo_path=path)
            status = _status()
            status["remote"].update(available=False, errors=["GitHub unavailable"])
            status["tracker"] = {"sentinel": "caller-owned"}
            before = copy.deepcopy(status)
            reader = Reader()
            pilot = board.supervised_pilot_payload(
                cfg, status, jira_reader=reader,
                gh_json_runner=lambda args: self.fail("must not read GitHub issues"),
            )
            self.assertEqual(status, before)
            self.assertEqual(len(reader.calls), 1)
            self.assertEqual(pilot["active_issues"][0]["work_item"]["identity"]["issue_key"], "ABC-1")
            self.assertEqual(pilot["tracker"]["freshness"], "live")
            self.assertEqual(pilot["tracker"]["items"][0]["gate_status"], "unknown")
            self.assertEqual(pilot["tracker"]["items"][0]["pr_freshness"], "unavailable")
            self.assertEqual(pilot["decision"]["stop_condition"], "github_unavailable")
            self.assertFalse(pilot["decision"]["would_mutate"])
            with patch.object(lane_status, "collect_status", return_value=copy.deepcopy(before)):
                payload = board.status_payload(cfg, jira_reader=Reader())
            self.assertEqual(payload["tracker"], payload["supervised_pilot"]["tracker"])
            self.assertEqual(payload["remote"], before["remote"])
            self.assertEqual(len(payload["supervised_pilot"]["active_issues"]), 1)

    def test_board_cached_queue_is_historical_without_changing_snapshot(self):
        snapshot = {"board": {}, "tracker": view(queue(), prs=[_pr()],
                    links={("cloud-example", "10001", "1"): 42})}
        cache = Mock()
        cache.get.return_value = (snapshot, {"state": "stale"})
        server = board.ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(
            board.BoardConfig(repo="owner/repo"), status_cache=cache))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/status", timeout=2) as response:
                payload = json.load(response)
            row = payload["tracker"]["items"][0]
            self.assertEqual(row["freshness"], "historical")
            self.assertEqual(row["pr_freshness"], "historical")
            self.assertFalse(row["eligible"])
            self.assertEqual(row["next_action"], "refresh current state")
            self.assertEqual(snapshot["tracker"]["items"][0]["freshness"], "live")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
