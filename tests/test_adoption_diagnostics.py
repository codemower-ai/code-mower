from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest import TestCase

from code_mower import board, controller, lane_status, productivity_report, reviewer_spend
from code_mower import board_store
from code_mower import config as code_mower_config
from code_mower import init as code_mower_init


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

VALID_CONFIG_YAML = """
version: 1
project:
  name: demo
  state_dir: .code-mower
repositories:
  - slug: owner/repo
    default_branch: main
owner_surface:
  ready_label: tier:R
builder_identity:
  labels:
    builder:codex: codex
lanes:
  codex:
    type: audit
    driver: local_cli
    provider: codex
    merge_authority: true
    labels:
      needs: needs-codex-audit
      done: codex-audit-done
      blocked: codex-audit-blocked
""".lstrip()


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _gh_json_empty(args: list[str]) -> object:
    if args[:2] in (["pr", "list"], ["run", "list"], ["issue", "list"]):
        return []
    raise AssertionError(args)


def _command_runner_quiet(_args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", "")


def _starter_config() -> dict[str, object]:
    path = code_mower_init._resolve_config_path("code-mower.example.yml")
    loaded = code_mower_config.load_config(path)
    return dict(loaded)


class InitConfigSourceTests(TestCase):
    def test_packaged_starter_is_identified_in_text_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = code_mower_init.render_init_plan(
                _starter_config(),
                config_path="code-mower.example.yml",
                package_mode=True,
                package_command="code-mower",
                repo_root=tmp,
            )

        self.assertEqual(plan.data["config_source"]["kind"], "packaged_starter")
        self.assertEqual(plan.data["config_source"]["requested_path"], "code-mower.example.yml")
        self.assertFalse(plan.data["config_source"]["root_config_present"])
        self.assertEqual(plan.data["setup_drift_hint"], "")
        self.assertIn("Config source: packaged starter (code-mower.example.yml)", plan.text)
        self.assertNotIn("Setup drift:", plan.text)

    def test_root_config_with_starter_points_at_explicit_config_and_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "code-mower.yml").write_text("version: 1\n", encoding="utf-8")
            plan = code_mower_init.render_init_plan(
                _starter_config(),
                config_path="code-mower.example.yml",
                package_mode=True,
                package_command="code-mower",
                repo_root=tmp,
            )

        hint = plan.data["setup_drift_hint"]
        self.assertTrue(plan.data["config_source"]["root_config_present"])
        self.assertIn("code-mower init code-mower.yml", hint)
        self.assertIn("setup-drift", hint)
        self.assertIn("docs/upgrade-existing-repo.md", hint)
        self.assertIn("Setup drift:", plan.text)

    def test_explicit_repository_config_never_gets_drift_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "code-mower.yml").write_text("version: 1\n", encoding="utf-8")
            plan = code_mower_init.render_init_plan(
                _starter_config(),
                config_path="code-mower.yml",
                package_mode=True,
                package_command="code-mower",
                repo_root=tmp,
            )

        self.assertEqual(plan.data["config_source"]["kind"], "explicit_repository_config")
        self.assertEqual(plan.data["setup_drift_hint"], "")
        self.assertIn("Config source: explicit repository config (code-mower.yml)", plan.text)

    def test_default_config_selection_behavior_is_unchanged(self) -> None:
        resolved = code_mower_init._resolve_config_path("code-mower.example.yml")
        self.assertEqual(resolved.name, "code-mower.example.yml")
        self.assertTrue(resolved.is_file())

    def test_explicit_same_named_file_is_not_the_packaged_starter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit_path = str(Path(tmp) / "code-mower.example.yml")
            Path(tmp, "code-mower.yml").write_text("version: 1\n", encoding="utf-8")
            plan = code_mower_init.render_init_plan(
                _starter_config(),
                config_path=explicit_path,
                package_mode=True,
                package_command="code-mower",
                repo_root=tmp,
                source_kind="explicit_repository_config",
            )

        self.assertEqual(plan.data["config_source"]["kind"], "explicit_repository_config")
        self.assertEqual(plan.data["config_source"]["requested_path"], explicit_path)
        self.assertEqual(plan.data["setup_drift_hint"], "")
        self.assertIn(
            f"Config source: explicit repository config ({explicit_path})", plan.text
        )
        self.assertNotIn("Setup drift:", plan.text)

    def test_resolution_context_keeps_supplied_same_named_file_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "code-mower.example.yml").write_text(
                VALID_CONFIG_YAML, encoding="utf-8"
            )
            original_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                resolved = code_mower_init._resolve_config_path("code-mower.example.yml")
            finally:
                os.chdir(original_cwd)

        # The user-supplied file resolves to itself, not the packaged
        # fallback, so main must classify it as an explicit repository config.
        self.assertEqual(resolved, Path("code-mower.example.yml"))

    def test_packaged_fallback_shows_stable_starter_name(self) -> None:
        resolved = code_mower_init._resolve_config_path("code-mower.example.yml")
        self.assertTrue(resolved.is_absolute())
        with tempfile.TemporaryDirectory() as tmp:
            plan = code_mower_init.render_init_plan(
                _starter_config(),
                config_path=str(resolved),
                package_mode=True,
                package_command="code-mower",
                repo_root=tmp,
                source_kind="packaged_starter",
            )

        self.assertEqual(plan.data["config_source"]["kind"], "packaged_starter")
        self.assertEqual(
            plan.data["config_source"]["requested_path"], "code-mower.example.yml"
        )
        self.assertIn(
            "Config source: packaged starter (code-mower.example.yml)", plan.text
        )
        source_line = next(
            line for line in plan.text.splitlines() if line.startswith("Config source:")
        )
        self.assertNotIn(str(resolved), source_line)
        self.assertNotIn(
            str(resolved), json.dumps(plan.data["config_source"], sort_keys=True)
        )


class ControllerConfigFailureTests(TestCase):
    def test_missing_config_names_request_and_points_to_init_and_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing-code-mower.yml")
            err = StringIO()
            with redirect_stderr(err):
                code = controller.main(
                    ["run", "--repo", "owner/repo", "--config", missing],
                    gh_json_runner=_gh_json_empty,  # type: ignore[call-arg]
                    command_runner=_command_runner_quiet,  # type: ignore[call-arg]
                )

        self.assertEqual(code, 1)
        output = err.getvalue()
        self.assertIn("missing-code-mower.yml", output)
        self.assertIn("was not found", output)
        self.assertIn("code-mower init --easy", output)
        self.assertIn("docs/upgrade-existing-repo.md", output)
        self.assertNotIn("Traceback", output)

    def test_invalid_config_names_request_and_points_to_init_and_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "code-mower.yml"
            bad.write_text("{not valid", encoding="utf-8")
            err = StringIO()
            with redirect_stderr(err):
                code = controller.main(
                    ["run", "--repo", "owner/repo", "--config", str(bad)],
                    gh_json_runner=_gh_json_empty,  # type: ignore[call-arg]
                    command_runner=_command_runner_quiet,  # type: ignore[call-arg]
                )

        self.assertEqual(code, 1)
        output = err.getvalue()
        self.assertIn("code-mower.yml", output)
        self.assertIn("invalid", output)
        self.assertIn("code-mower init --easy", output)
        self.assertIn("docs/upgrade-existing-repo.md", output)
        self.assertNotIn("Traceback", output)

    def test_uploadable_event_carries_no_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "code-mower.yml"
            config_path.write_text(VALID_CONFIG_YAML, encoding="utf-8")
            event_path = root / "controller-event.json"
            out = StringIO()
            with redirect_stdout(out):
                code = controller.main(
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--config",
                        str(config_path),
                        "--event-file",
                        str(event_path),
                        "--json",
                    ],
                    gh_json_runner=_gh_json_empty,  # type: ignore[call-arg]
                    command_runner=_command_runner_quiet,  # type: ignore[call-arg]
                )

            self.assertEqual(code, 0)
            event = json.loads(event_path.read_text(encoding="utf-8"))
            serialized = json.dumps(event)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("cwd", serialized.lower())


class ProductivityEvidenceTests(TestCase):
    def test_empty_evidence_stays_successful_but_reports_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = productivity_report.build_report(repo="owner/repo", repo_path=tmp, now=NOW)
            out = StringIO()
            with redirect_stdout(out):
                code = productivity_report.main(
                    ["report", "--repo", "owner/repo", "--repo-path", tmp]
                )
            text = productivity_report.render_text(report)

        self.assertEqual(code, 0)
        # Existing consumers keep working: status and next_action are unchanged.
        self.assertEqual(report["status"], "warn")
        self.assertIn("next_action", report)
        evidence = report["evidence"]
        self.assertFalse(evidence["ready"])
        self.assertEqual(evidence["coverage"], "empty")
        self.assertEqual(evidence["missing"], ["board history", "reviewer spend", "cloud events"])
        self.assertIn("insufficient evidence", evidence["detail"])
        self.assertIn("board record", evidence["detail"])
        self.assertIn("Evidence:", text)
        self.assertIn("insufficient evidence", text)

    def test_partial_evidence_names_only_what_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spend_path = root / "reviewer-spend.json"
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                                "lane": "codex-audit",
                                "repo": "owner/repo",
                                "pr_number": 42,
                                "head_sha": "abcdef0123456789",
                                "verdict": "PASS",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = productivity_report.build_report(
                repo="owner/repo", repo_path=root, spend_path=spend_path, now=NOW
            )

        evidence = report["evidence"]
        self.assertFalse(evidence["ready"])
        self.assertEqual(evidence["coverage"], "partial")
        self.assertTrue(evidence["reviewer_spend"])
        self.assertFalse(evidence["board_history"])
        self.assertEqual(evidence["missing"], ["board history", "cloud events"])

    def test_local_board_history_alone_is_ready_without_cloud_or_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "events.jsonl"
            board_store.append_snapshot(
                {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "generated_at": NOW.isoformat().replace("+00:00", "Z"),
                    "remote": {"available": False},
                    "local_boards": {"available": False, "boards": [], "message": ""},
                    "local_processes": {"available": False, "processes": [], "message": ""},
                    "next_action": "inspect",
                    "next_detail": "",
                },
                path=store_path,
                now=NOW,
            )
            report = productivity_report.build_report(
                repo="owner/repo", repo_path=root, store_path=store_path, now=NOW
            )

        evidence = report["evidence"]
        self.assertTrue(evidence["ready"])
        self.assertEqual(evidence["coverage"], "partial")
        self.assertTrue(evidence["board_history"])
        self.assertFalse(evidence["reviewer_spend"])
        self.assertFalse(evidence["cloud_events"])
        self.assertEqual(evidence["missing"], ["reviewer spend", "cloud events"])
        # Optional completeness enhancements keep concrete recording guidance.
        self.assertIn("local board history is present", evidence["detail"])
        self.assertIn("optional enhancements", evidence["detail"])
        self.assertIn("optionally capture reviewer spend rows", evidence["detail"])
        self.assertIn("optionally add productivity_summary event files", evidence["detail"])
        self.assertNotIn("upload", evidence["detail"])
        self.assertNotIn("insufficient evidence", evidence["detail"])

    def test_next_action_frames_spend_and_cloud_as_optional(self) -> None:
        spend_action = productivity_report._next_action(
            repo="owner/repo",
            current_status=None,
            board_metrics={"snapshot_count": 1},
            spend={"run_count": 0},
            cloud={"event_count": 0},
        )
        cloud_action = productivity_report._next_action(
            repo="owner/repo",
            current_status=None,
            board_metrics={"snapshot_count": 1},
            spend={"run_count": 2},
            cloud={"event_count": 0},
        )

        for action in (spend_action, cloud_action):
            self.assertIn("optionally", action)
            self.assertNotIn("upload", action)

    def test_full_evidence_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "events.jsonl"
            spend_path = root / "reviewer-spend.json"
            board_store.append_snapshot(
                {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "generated_at": NOW.isoformat().replace("+00:00", "Z"),
                    "remote": {"available": False},
                    "local_boards": {"available": False, "boards": [], "message": ""},
                    "local_processes": {"available": False, "processes": [], "message": ""},
                    "next_action": "inspect",
                    "next_detail": "",
                },
                path=store_path,
                now=NOW,
            )
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                                "lane": "codex-audit",
                                "repo": "owner/repo",
                                "pr_number": 42,
                                "head_sha": "abcdef0123456789",
                                "verdict": "PASS",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cloud_path = root / "productivity-events.json"
            cloud_path.write_text(
                json.dumps(
                    {
                        "productivity_summary_events": [
                            {
                                "schema": "code_mower.benchmarkEvent.v1",
                                "event_id": "adoption-diagnostics-full",
                                "event_type": "productivity_summary",
                                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                                "repo_slug": "owner/repo",
                                "team_id": "team",
                                "install_id": "install",
                                "source": "fixture",
                                "provider": "code-mower",
                                "lens": "productivity",
                                "status": "observed",
                                "tool": {
                                    "role": "reporter",
                                    "tool_name": "code-mower",
                                    "tool_version": "1.0.1",
                                    "provider": "code-mower",
                                    "model": "",
                                    "model_source": "not_applicable",
                                    "version_source": "package_version",
                                    "integration": "cli",
                                    "runtime_environment": "local",
                                },
                                "metrics": {"reviewer_run_count": 1},
                                "dimensions": {
                                    "productivity_schema": "code_mower.productivityMetrics.v1",
                                    "repo_slug": "owner/repo",
                                    "window_start": "2026-09-03T10:00:00Z",
                                    "window_end": "2026-09-03T12:00:00Z",
                                    "window_granularity": "release",
                                    "aggregation_subject": "repo",
                                    "aggregation_key": "owner/repo",
                                    "release": "v1.0.0",
                                    "pilot_posture": "supervised",
                                    "event_source": "local-report",
                                },
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                store_path=store_path,
                spend_path=spend_path,
                cloud_event_paths=[cloud_path],
                now=NOW,
            )
            board_form = productivity_report.board_payload(
                repo="owner/repo",
                repo_path=root,
                store_path=store_path,
                spend_path=spend_path,
            )

        self.assertTrue(report["evidence"]["ready"])
        self.assertEqual(report["evidence"]["coverage"], "complete")
        self.assertEqual(report["evidence"]["missing"], [])
        # The compact Board form has no cloud-event inputs, so it stays
        # explicit about that gap while keeping local evidence fields.
        # Cloud is optional: local Board history plus reviewer spend is ready.
        self.assertIn("evidence", board_form)
        self.assertTrue(board_form["evidence"]["ready"])
        self.assertEqual(board_form["evidence"]["coverage"], "partial")
        self.assertTrue(board_form["evidence"]["board_history"])
        self.assertTrue(board_form["evidence"]["reviewer_spend"])
        self.assertFalse(board_form["evidence"]["cloud_events"])
        self.assertEqual(board_form["evidence"]["missing"], ["cloud events"])
        self.assertIn(
            "optionally add productivity_summary event files",
            board_form["evidence"]["detail"],
        )
        self.assertNotIn("upload", board_form["evidence"]["detail"])


def _board_command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
        return _completed("p123\ncnode\nn127.0.0.1:5332\n")
    if args == ["ps", "-p", "123", "-o", "command="]:
        return _completed("code-mower board serve --repo owner/repo\n")
    if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
        return _completed("p123\nn/tmp/lane-checkout\n")
    return _completed("", returncode=1)


class BoardStaleLauncherMetadataTests(TestCase):
    def _write_card(self, directory: Path, name: str, pid: int | None) -> None:
        card: dict[str, object] = {"provider": "codex", "role": "builder", "status": "running"}
        if pid is not None:
            card["pid"] = pid
        (directory / name).write_text(json.dumps(card), encoding="utf-8")

    def test_dead_pid_cards_are_marked_stale_and_live_cards_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            self._write_card(adapter_dir, "dead.json", 99999999)
            self._write_card(adapter_dir, "live.json", os.getpid())
            self._write_card(adapter_dir, "nopid.json", None)

            payload = board.agent_adapters_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp)
            )

        by_file = {card["source_file"]: card for card in payload["agents"]}
        self.assertTrue(by_file["dead.json"].get("stale"))
        self.assertNotIn("stale", by_file["live.json"])
        self.assertNotIn("stale", by_file["nopid.json"])
        self.assertEqual(payload["stale_cards"], 1)

    def test_board_html_renders_explicit_stale_pill_for_stale_cards(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))

        # Stale agent cards keep their original metadata status; the browser
        # view must expose the payload's stale flag as its own pill so a
        # dead-pid card never visually looks running.
        self.assertIn('agent.stale ? pill("stale")', html)

    def test_prune_removes_only_all_stale_pid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            self._write_card(adapter_dir, "stale.json", 99999999)
            self._write_card(adapter_dir, "live.json", os.getpid())
            self._write_card(adapter_dir, "nopid.json", None)
            (adapter_dir / "broken.json").write_text("{not json", encoding="utf-8")
            (adapter_dir / "notes.txt").write_text("not an adapter", encoding="utf-8")

            result = board.prune_stale_agent_adapters(
                adapter_dir, pid_alive=lambda pid: pid == os.getpid()
            )

            self.assertEqual(result["pruned"], ["stale.json"])
            self.assertEqual(sorted(result["kept"]), ["broken.json", "live.json", "nopid.json"])
            self.assertEqual(result["errors"], [])
            self.assertFalse((adapter_dir / "stale.json").exists())
            self.assertTrue((adapter_dir / "live.json").exists())
            self.assertTrue((adapter_dir / "nopid.json").exists())
            self.assertTrue((adapter_dir / "notes.txt").exists())

    def test_prune_enumeration_failure_uses_stable_message_without_paths(self) -> None:
        secret_dir = "/tmp/secret-checkout-prune-list"
        exception_payload = f"[Errno 13] Permission denied: '{secret_dir}/.code-mower/board/agents'"
        original_glob = Path.glob

        def _bad_glob(self: Path, _pattern: str):  # type: ignore[no-untyped-def]
            raise OSError(exception_payload)

        with tempfile.TemporaryDirectory() as tmp:
            Path.glob = _bad_glob  # type: ignore[method-assign]
            try:
                result = board.prune_stale_agent_adapters(Path(tmp), pid_alive=lambda _pid: False)
            finally:
                Path.glob = original_glob  # type: ignore[method-assign]

        self.assertEqual(
            result["errors"],
            [{"file": "", "message": "could not list agent adapter files"}],
        )
        dumped = json.dumps(result, sort_keys=True)
        self.assertNotIn(secret_dir, dumped)
        self.assertNotIn("Permission denied", dumped)
        text = board.render_stop_text(
            {
                "status": "pruned",
                "message": "pruned stale launcher metadata without stopping any Board listener",
                "stopped": [],
                "errors": [],
                "pruned_agents": result,
            }
        )
        self.assertNotIn(secret_dir, text)
        self.assertNotIn("Permission denied", text)

    def test_prune_unlink_failure_uses_stable_message_without_paths(self) -> None:
        sentinel = "prune-unlink-secret-payload"
        original_unlink = Path.unlink

        def _bad_unlink(self: Path, missing_ok: bool = False) -> None:  # type: ignore[no-untyped-def]
            raise OSError(f"[Errno 13] Permission denied: '{self}' :: {sentinel}")

        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            self._write_card(adapter_dir, "stale.json", 99999999)
            Path.unlink = _bad_unlink  # type: ignore[method-assign]
            try:
                result = board.prune_stale_agent_adapters(adapter_dir, pid_alive=lambda _pid: False)
            finally:
                Path.unlink = original_unlink  # type: ignore[method-assign]

        self.assertEqual(
            result["errors"],
            [{"file": "stale.json", "message": "could not delete stale agent adapter file"}],
        )
        self.assertEqual(result["pruned"], [])
        dumped = json.dumps(result, sort_keys=True)
        self.assertNotIn(tmp, dumped)
        self.assertNotIn(sentinel, dumped)
        self.assertNotIn("Permission denied", dumped)
        text = board.render_stop_text(
            {
                "status": "pruned",
                "message": "pruned stale launcher metadata without stopping any Board listener",
                "stopped": [],
                "errors": [],
                "pruned_agents": result,
            }
        )
        self.assertNotIn(tmp, text)
        self.assertNotIn(sentinel, text)
        self.assertNotIn("Permission denied", text)

    def test_prune_rejects_symlinked_adapters_directory_without_deleting_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside-target"
            outside.mkdir()
            self._write_card(outside, "stale.json", 99999999)
            link = Path(tmp) / "adapters"
            link.symlink_to(outside, target_is_directory=True)

            result = board.prune_stale_agent_adapters(link, pid_alive=lambda _pid: False)

            self.assertEqual(result["pruned"], [])
            self.assertEqual(result["kept"], [])
            self.assertEqual(
                result["errors"],
                [{"file": "", "message": "refusing to prune agent adapter files through a symlink"}],
            )
            self.assertTrue((outside / "stale.json").exists())
            self.assertTrue(link.is_symlink())
            dumped = json.dumps(result, sort_keys=True)
            self.assertNotIn(tmp, dumped)
            self.assertNotIn("stale.json", dumped)
            self.assertNotIn("Permission denied", dumped)
            text = board.render_stop_text(
                {
                    "status": "pruned",
                    "message": "pruned stale launcher metadata without stopping any Board listener",
                    "stopped": [],
                    "errors": [],
                    "pruned_agents": result,
                }
            )
            self.assertNotIn(tmp, text)
            self.assertNotIn("stale.json", text)

            stop_result = board.stop_board(
                prune_stale_agents=True,
                yes=True,
                agent_adapters_path=link,
                pid_alive=lambda _pid: False,
            )
            self.assertEqual(stop_result["status"], "failed")
            self.assertNotEqual(stop_result["status"], "pruned")

    def test_prune_processes_all_adapter_files_beyond_fifty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            for index in range(60):
                self._write_card(adapter_dir, f"stale-{index:03d}.json", 99999999)

            result = board.prune_stale_agent_adapters(adapter_dir, pid_alive=lambda _pid: False)

            self.assertEqual(len(result["pruned"]), 60)
            self.assertEqual(result["kept"], [])
            self.assertEqual(result["errors"], [])
            self.assertEqual(
                sorted(result["pruned"]), [f"stale-{index:03d}.json" for index in range(60)]
            )
            self.assertEqual(list(adapter_dir.glob("*.json")), [])

    def test_prune_keeps_files_with_invalid_pid_values(self) -> None:
        invalid_values: list[object] = ["nan", "Infinity", 1.5, 0, -1, True]
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            for index, value in enumerate(invalid_values):
                (adapter_dir / f"invalid-{index}.json").write_text(
                    json.dumps({"provider": "codex", "pid": value}), encoding="utf-8"
                )
            (adapter_dir / "mixed.json").write_text(
                json.dumps({"agents": [{"pid": 99999999}, {"pid": "nan"}]}),
                encoding="utf-8",
            )

            result = board.prune_stale_agent_adapters(adapter_dir, pid_alive=lambda _pid: False)

            self.assertEqual(result["pruned"], [])
            self.assertEqual(len(result["kept"]), len(invalid_values) + 1)
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(list(adapter_dir.glob("*.json"))), len(invalid_values) + 1)

    def test_revalidation_refuses_unrelated_board_serve_but_accepts_code_mower_forms(self) -> None:
        self.assertTrue(board._command_looks_like_board("code-mower board serve --repo owner/repo"))
        self.assertTrue(
            board._command_looks_like_board("python -m code_mower.board --repo owner/repo")
        )
        self.assertTrue(
            board._command_looks_like_board("code_mower.cli board serve --repo owner/repo")
        )
        unrelated = "/usr/local/bin/board serve --port 5332"
        self.assertIn("board serve", unrelated.lower())
        self.assertFalse(board._command_looks_like_board(unrelated))
        self.assertFalse(lane_status.command_looks_like_code_mower_board(unrelated))

        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p123\ncnode\nn127.0.0.1:5332\n")
            if args == ["ps", "-p", "123", "-o", "command="]:
                return _completed(unrelated + "\n")
            if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
                return _completed("p123\nn/tmp/lane-checkout\n")
            return _completed("", returncode=1)

        stopped: list[tuple[int, int]] = []
        result = board.stop_board(
            port=5332,
            yes=True,
            command_runner=runner,
            killer=lambda pid, sig: stopped.append((pid, sig)),
        )

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(stopped, [])
        self.assertEqual(result["errors"], [])

    def test_stop_refuses_to_signal_a_reused_pid(self) -> None:
        calls: list[str] = []

        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args == ["ps", "-p", "123", "-o", "command="]:
                calls.append("ps")
                if len(calls) == 1:
                    return _completed("code-mower board serve --repo owner/repo\n")
                return _completed("postgres: checkpointer process\n")
            return _board_command_runner(args)

        stopped: list[tuple[int, int]] = []
        result = board.stop_board(
            port=5332,
            yes=True,
            command_runner=runner,
            killer=lambda pid, sig: stopped.append((pid, sig)),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(stopped, [])
        self.assertIn("refusing to signal", result["errors"][0]["message"])

    def test_stop_still_stops_a_revalidated_board(self) -> None:
        stopped: list[tuple[int, int]] = []
        result = board.stop_board(
            port=5332,
            yes=True,
            command_runner=_board_command_runner,
            killer=lambda pid, sig: stopped.append((pid, sig)),
        )

        import signal

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(stopped, [(123, signal.SIGTERM)])

    def test_stop_signal_failure_does_not_expose_exception_text(self) -> None:
        sentinel = "signal-secret-payload"

        def fail_to_signal(_pid: int, _sig: int) -> None:
            raise OSError(f"private path and auth output: {sentinel}")

        result = board.stop_board(
            port=5332,
            yes=True,
            command_runner=_board_command_runner,
            killer=fail_to_signal,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["message"], "could not signal Board process")
        self.assertNotIn(sentinel, json.dumps(result))

    def test_combined_stop_reports_partial_when_pruning_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            adapters_link = Path(tmp) / "agents"
            adapters_link.symlink_to(outside, target_is_directory=True)
            result = board.stop_board(
                port=5332,
                yes=True,
                command_runner=_board_command_runner,
                killer=lambda *_args: None,
                prune_stale_agents=True,
                agent_adapters_path=adapters_link,
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["stopped"]), 1)
        self.assertEqual(len(result["pruned_agents"]["errors"]), 1)

    def test_stop_prune_requires_yes_and_reports_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            self._write_card(adapter_dir, "stale.json", 99999999)

            dry = board.stop_board(
                port=5332,
                command_runner=_board_command_runner,
                killer=lambda *_args: None,
                prune_stale_agents=True,
                agent_adapters_path=adapter_dir,
                pid_alive=lambda _pid: False,
            )
            self.assertNotIn("pruned_agents", dry)
            self.assertTrue((adapter_dir / "stale.json").exists())

            result = board.stop_board(
                port=5332,
                yes=True,
                command_runner=_board_command_runner,
                killer=lambda *_args: None,
                prune_stale_agents=True,
                agent_adapters_path=adapter_dir,
                pid_alive=lambda _pid: False,
            )

        self.assertEqual(result["pruned_agents"]["pruned"], ["stale.json"])
        self.assertFalse((adapter_dir / "stale.json").exists())

    def test_prune_only_mode_works_without_selector_and_never_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            self._write_card(adapter_dir, "stale.json", 99999999)
            self._write_card(adapter_dir, "live.json", os.getpid())

            killer_calls: list[tuple[int, int]] = []

            def _killer(pid: int, sig: int) -> None:
                killer_calls.append((pid, sig))

            result = board.stop_board(
                prune_stale_agents=True,
                yes=True,
                agent_adapters_path=adapter_dir,
                pid_alive=lambda pid: pid == os.getpid(),
                killer=_killer,
            )

            self.assertEqual(result["status"], "pruned")
            self.assertEqual(result["stopped"], [])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["pruned_agents"]["pruned"], ["stale.json"])
            self.assertEqual(killer_calls, [])
            self.assertFalse((adapter_dir / "stale.json").exists())
            self.assertTrue((adapter_dir / "live.json").exists())

    def test_prune_only_mode_requires_yes_and_never_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp)
            self._write_card(adapter_dir, "stale.json", 99999999)
            killer_calls: list[tuple[int, int]] = []

            dry = board.stop_board(
                prune_stale_agents=True,
                agent_adapters_path=adapter_dir,
                pid_alive=lambda _pid: False,
                killer=lambda pid, sig: killer_calls.append((pid, sig)),
            )

            self.assertEqual(dry["status"], "confirmation_required")
            self.assertNotIn("pruned_agents", dry)
            self.assertEqual(killer_calls, [])
            self.assertTrue((adapter_dir / "stale.json").exists())

    def test_stop_without_selector_or_prune_stays_invalid(self) -> None:
        result = board.stop_board(killer=lambda *_args: None)

        self.assertEqual(result["status"], "invalid_selector")
        self.assertNotIn("pruned_agents", result)

    def test_stop_prune_only_cli_uses_standard_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            self._write_card(adapter_dir, "stale.json", 99999999)
            original_cwd = Path.cwd()
            out = StringIO()
            try:
                os.chdir(tmp)
                with redirect_stdout(out):
                    code = board.main(["stop", "--prune-stale-agents", "--yes", "--json"])
            finally:
                os.chdir(original_cwd)

            payload = json.loads(out.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "pruned")
            self.assertEqual(payload["pruned_agents"]["pruned"], ["stale.json"])
            self.assertFalse((adapter_dir / "stale.json").exists())
