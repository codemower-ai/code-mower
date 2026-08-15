from __future__ import annotations

import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from code_mower import builder_runs
from code_mower.cloud_client import build_cloud_bundle, build_upload_payload, parse_event_args


def test_builder_record_writes_source_free_grok_cursor_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work_order = root / "bridge-pro-bidding.md"
        work_order.write_text("# Work Order: Bridge bidding\n", encoding="utf-8")
        work_order.with_suffix(".json").write_text(
            json.dumps(
                {
                    "schema": "code_mower.workOrder.v1",
                    "repo": "jeffhuber/bridge-pro",
                    "source": {
                        "type": "github_issue",
                        "repo": "jeffhuber/bridge-pro",
                        "issue_number": "12",
                        "issue_url": "https://github.com/jeffhuber/bridge-pro/issues/12",
                    },
                }
            ),
            encoding="utf-8",
        )
        output = root / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "grok_bot",
                    "--executor",
                    "cursor_cloud_agent",
                    "--work-order",
                    str(work_order),
                    "--pr",
                    "jeffhuber/bridge-pro#13",
                    "--branch",
                    "cursor/bridge-bidding",
                    "--model",
                    "grok-4",
                    "--tool-version",
                    "cursor-cloud-agent",
                    "--elapsed-seconds",
                    "42",
                    "--cost-usd",
                    "0.25",
                    "--user-interventions",
                    "1",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["mode"] == "builder-record"
        event = json.loads(output.read_text(encoding="utf-8"))
        assert event["schema"] == "code_mower.benchmarkEvent.v1"
        assert event["event_type"] == "builder_run"
        assert event["provider"] == "grok_bot"
        assert event["repo_slug"] == "jeffhuber/bridge-pro"
        assert event["tool"]["role"] == "builder"
        assert event["tool"]["tool_name"] == "grok_bot"
        assert event["tool"]["executor"] == "cursor_cloud_agent"
        assert event["tool"]["model"] == "grok-4"
        assert event["dimensions"]["builder_executor"] == "cursor_cloud_agent"
        assert event["dimensions"]["issue_number"] == "12"
        assert event["dimensions"]["pr_number"] == "13"
        assert event["dimensions"]["work_order_file"] == "bridge-pro-bidding.md"
        assert event["metrics"]["elapsed_seconds"] == 42
        assert event["metrics"]["cost_usd"] == 0.25
        assert event["metrics"]["user_interventions"] == 1
        assert "source_code" not in json.dumps(event)
        assert "raw_diffs" not in json.dumps(event)
        assert "transcript" not in json.dumps(event)

        bundle_result = build_cloud_bundle(
            reports=[],
            events=parse_event_args([f"builder_run={output}"]),
            output_dir=root / "bundle",
            repo_slug="jeffhuber/bridge-pro",
        )
        assert bundle_result["event_types"] == {"builder_run": 1}
        upload = build_upload_payload(bundle_dir=root / "bundle")
        assert upload["events"][0]["event_type"] == "builder_run"
        assert upload["events"][0]["tool"]["role"] == "builder"


def test_builder_record_requires_provenance_anchor() -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = builder_runs.main(["record", "--provider", "grok_bot", "--json"])

    assert code == 1


def test_builder_record_rejects_non_finite_float_metrics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "grok_bot",
                    "--issue",
                    "codemower-ai/code-mower#1",
                    "--elapsed-seconds",
                    "nan",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_uses_work_order_repo_for_numeric_pr_refs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work_order = root / "repo-anchored.md"
        work_order.write_text("# Work Order\n", encoding="utf-8")
        work_order.with_suffix(".json").write_text(
            json.dumps(
                {
                    "schema": "code_mower.workOrder.v1",
                    "repo": "codemower-ai/code-mower",
                }
            ),
            encoding="utf-8",
        )
        output = root / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "cursor_cloud_agent",
                    "--work-order",
                    str(work_order),
                    "--pr",
                    "289",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 0
        event = json.loads(output.read_text(encoding="utf-8"))
        assert event["repo_slug"] == "codemower-ai/code-mower"
        assert event["dimensions"]["pr_repo"] == "codemower-ai/code-mower"
        assert event["dimensions"]["pr_number"] == "289"
        assert event["dimensions"]["pr_url"] == "https://github.com/codemower-ai/code-mower/pull/289"
