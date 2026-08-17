from __future__ import annotations

import json
import tempfile
from contextlib import chdir, redirect_stdout
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


def test_builder_record_defaults_issue_only_status_to_observed() -> None:
    event = builder_runs.build_builder_run_event(
        provider="grok_bot",
        issue="codemower-ai/code-mower#1",
    )

    assert event["status"] == "observed"
    assert event["dimensions"]["issue_url"] == "https://github.com/codemower-ai/code-mower/issues/1"
    assert event["dimensions"]["pr_url"] == ""


def test_builder_record_rejects_pr_opened_status_without_pr() -> None:
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
                    "--status",
                    "pr-opened",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


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


def test_builder_record_rejects_missing_work_order_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing_work_order = root / "missing.md"
        output = root / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "grok_bot",
                    "--work-order",
                    str(missing_work_order),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_rejects_work_order_without_repo_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work_order = root / "unowned.md"
        work_order.write_text("# Work Order\n", encoding="utf-8")
        output = root / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "grok_bot",
                    "--work-order",
                    str(work_order),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_renders_host_prefixed_refs_without_github_dot_com_prefix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "cursor_cloud_agent",
                    "--repo",
                    "ghe.example.com/acme/widgets",
                    "--issue",
                    "12",
                    "--pr",
                    "13",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 0
        event = json.loads(output.read_text(encoding="utf-8"))
        assert event["repo_slug"] == "ghe.example.com/acme/widgets"
        assert event["dimensions"]["issue_url"] == "https://ghe.example.com/acme/widgets/issues/12"
        assert event["dimensions"]["pr_url"] == "https://ghe.example.com/acme/widgets/pull/13"


def test_builder_record_rejects_explicit_repo_that_conflicts_with_full_pr_ref() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "grok_bot",
                    "--repo",
                    "codemower-ai/code-mower",
                    "--pr",
                    "jeffhuber/bridge-pro#13",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_rejects_explicit_repo_that_conflicts_with_full_issue_ref() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "builder-run.cloud-event.json"

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "record",
                    "--provider",
                    "grok_bot",
                    "--repo",
                    "codemower-ai/code-mower",
                    "--issue",
                    "jeffhuber/bridge-pro#12",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_rejects_mismatched_issue_and_pr_repositories() -> None:
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
                    "codemower-ai/code-mower#12",
                    "--pr",
                    "jeffhuber/bridge-pro#13",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_rejects_mismatched_work_order_and_pr_repositories() -> None:
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
                    "grok_bot",
                    "--work-order",
                    str(work_order),
                    "--pr",
                    "jeffhuber/bridge-pro#13",
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_rejects_mismatched_explicit_repo_and_work_order() -> None:
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
                    "grok_bot",
                    "--repo",
                    "jeffhuber/bridge-pro",
                    "--work-order",
                    str(work_order),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        assert code == 1
        assert not output.exists()


def test_builder_record_event_id_includes_work_order_repo_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = root / "first" / "same-name.md"
        second = root / "second" / "same-name.md"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("# Work Order\n", encoding="utf-8")
        second.write_text("# Work Order\n", encoding="utf-8")
        for work_order, repo in (
            (first, "codemower-ai/code-mower"),
            (second, "jeffhuber/bridge-pro"),
        ):
            work_order.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "schema": "code_mower.workOrder.v1",
                        "repo": repo,
                        "source": {
                            "type": "github_issue",
                            "repo": repo,
                            "issue_number": "12",
                        },
                    }
                ),
                encoding="utf-8",
            )

        first_event = builder_runs.build_builder_run_event(
            provider="grok_bot",
            executor="cursor_cloud_agent",
            work_order=first,
            branch="cursor/same-branch",
        )
        second_event = builder_runs.build_builder_run_event(
            provider="grok_bot",
            executor="cursor_cloud_agent",
            work_order=second,
            branch="cursor/same-branch",
        )

        assert first_event["repo_slug"] == "codemower-ai/code-mower"
        assert second_event["repo_slug"] == "jeffhuber/bridge-pro"
        assert first_event["event_id"] != second_event["event_id"]


def test_builder_record_event_id_includes_builder_run_identity() -> None:
    first = builder_runs.build_builder_run_event(
        provider="grok_bot",
        executor="cursor_cloud_agent",
        pr="codemower-ai/code-mower#289",
        builder_id="attempt-1",
    )
    second = builder_runs.build_builder_run_event(
        provider="grok_bot",
        executor="cursor_cloud_agent",
        pr="codemower-ai/code-mower#289",
        builder_id="attempt-2",
    )

    assert first["event_id"] != second["event_id"]
    assert first["dimensions"]["builder_id"] == "attempt-1"
    assert second["dimensions"]["builder_id"] == "attempt-2"


def test_builder_record_default_output_path_includes_builder_run_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        with chdir(root):
            for builder_id in ("attempt-1", "attempt-2"):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = builder_runs.main(
                        [
                            "record",
                            "--provider",
                            "grok_bot",
                            "--executor",
                            "cursor_cloud_agent",
                            "--pr",
                            "codemower-ai/code-mower#289",
                            "--builder-id",
                            builder_id,
                            "--json",
                        ]
                    )
                assert code == 0

        outputs = sorted((root / ".code-mower" / "builder-runs").glob("*.json"))
        assert [path.name for path in outputs] == [
            "grok_bot-cursor_cloud_agent-pr-289-attempt-1.cloud-event.json",
            "grok_bot-cursor_cloud_agent-pr-289-attempt-2.cloud-event.json",
        ]


def _write_pr_event(
    path: Path,
    *,
    author: str = "human-builder",
    branch: str = "feature/manual",
    body: str = "",
    number: int = 52,
) -> None:
    path.write_text(
        json.dumps(
            {
                "repository": {"full_name": "jeffhuber/bridge-pro"},
                "pull_request": {
                    "number": number,
                    "html_url": f"https://github.com/jeffhuber/bridge-pro/pull/{number}",
                    "user": {"login": author},
                    "head": {"ref": branch},
                    "body": body,
                },
            }
        ),
        encoding="utf-8",
    )


def test_builder_auto_record_infers_codex_connector_author() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(pr_json, author="chatgpt-codex-connector", branch="codex/cm-1")

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "auto-record",
                    "--pr-json",
                    str(pr_json),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        event = json.loads(output.read_text(encoding="utf-8"))
        assert code == 0
        assert payload["status"] == "recorded"
        assert event["provider"] == "codex"
        assert event["dimensions"]["builder_executor"] == "chatgpt-codex-connector"
        assert event["dimensions"]["pr_number"] == "52"
        assert event["dimensions"]["branch"] == "codex/cm-1"
        assert event["dimensions"]["auto_inferred"] is True
        assert "author:chatgpt-codex-connector" in event["dimensions"]["builder_inference_signals"]


def test_builder_auto_record_infers_cursor_agent_link_without_storing_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(
            pr_json,
            branch="cursor/bidding-panel",
            body=(
                "Implemented the task.\n\n"
                "View agent run: https://cursor.com/agents/run_abc123\n"
                "Do not store this body text."
            ),
        )

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "auto-record",
                    "--pr-json",
                    str(pr_json),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        event_text = output.read_text(encoding="utf-8")
        event = json.loads(event_text)
        assert code == 0
        assert event["provider"] == "cursor_cloud_agent"
        assert event["dimensions"]["builder_run_url"] == "https://cursor.com/agents/run_abc123"
        assert event["dimensions"]["builder_id"] == "run_abc123"
        assert event["dimensions"]["builder_inference_confidence"] == "high"
        assert "cursor_agent_url" in event["dimensions"]["builder_inference_signals"]
        assert "Do not store this body text" not in event_text


def test_builder_auto_record_skips_generic_cursor_links() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(
            pr_json,
            body="See https://cursor.com/pricing for developer tooling notes.",
        )

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "auto-record",
                    "--pr-json",
                    str(pr_json),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        assert code == 0
        assert payload["status"] == "skipped"
        assert not output.exists()


def test_builder_auto_record_skips_incidental_cursor_agent_words() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(
            pr_json,
            body="This human PR compares Cursor setup with an unrelated release agent.",
        )

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "auto-record",
                    "--pr-json",
                    str(pr_json),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        assert code == 0
        assert payload["status"] == "skipped"
        assert not output.exists()


def test_builder_auto_record_accepts_cursor_agent_footer_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(pr_json, body="Cursor agent completed this change.")

        code = builder_runs.main(
            [
                "auto-record",
                "--pr-json",
                str(pr_json),
                "--output",
                str(output),
                "--json",
            ]
        )

        event = json.loads(output.read_text(encoding="utf-8"))
        assert code == 0
        assert event["provider"] == "cursor_cloud_agent"
        assert event["dimensions"]["builder_inference_confidence"] == "medium"
        assert "cursor_agent_footer" in event["dimensions"]["builder_inference_signals"]


def test_builder_auto_record_infers_claude_bot_author() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(pr_json, author="claude[bot]", branch="claude/bridge-copy")

        code = builder_runs.main(
            [
                "auto-record",
                "--pr-json",
                str(pr_json),
                "--output",
                str(output),
                "--json",
            ]
        )

        event = json.loads(output.read_text(encoding="utf-8"))
        assert code == 0
        assert event["provider"] == "claude"
        assert event["dimensions"]["builder_executor"] == "claude_code_action"


def test_builder_auto_record_skips_unrecognized_pr_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pr_json = root / "event.json"
        output = root / "builder-run.cloud-event.json"
        _write_pr_event(pr_json)

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = builder_runs.main(
                [
                    "auto-record",
                    "--pr-json",
                    str(pr_json),
                    "--output",
                    str(output),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        assert code == 0
        assert payload["status"] == "skipped"
        assert not output.exists()
