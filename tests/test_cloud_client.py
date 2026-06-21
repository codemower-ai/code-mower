from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import code_mower.cloud_client.operations as cloud_operations
from code_mower.cloud_client import (
    BUNDLE_MANIFEST_FILENAME,
    CloudBundleError,
    DEFAULT_SETUP_INSTALL_ID,
    EVENT_SCHEMA,
    build_provenance_summary,
    build_provider_catalog_snapshot_events,
    build_cloud_bundle,
    build_upload_payload,
    default_setup_path,
    dogfood_upload,
    normalize_event,
    parse_event_args,
    parse_repo_sync_spec,
    repo_slug_from_remote,
    repo_sync_output_name,
    render_cloud_doctor_text,
    run_cloud_doctor,
    run_cloud_setup,
    safe_config_stem,
    token_prefix,
)
from code_mower import cloud as cloud_cli


def test_cloud_catch_up_summary_separates_history_from_calibration() -> None:
    runs = [
        {
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "createdAt": "2026-06-15T00:00:00Z",
            "updatedAt": "2026-06-15T00:01:00Z",
        },
        {
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "createdAt": "2026-06-16T00:00:00Z",
            "updatedAt": "2026-06-16T00:02:00Z",
        },
        {
            "name": "Dogfood",
            "status": "in_progress",
            "conclusion": "",
            "createdAt": "2026-06-17T00:00:00Z",
            "updatedAt": "2026-06-17T00:03:00Z",
        },
    ]

    summary = cloud_operations.build_catch_up_summary(
        repo_slug="owner/repo",
        runs=runs,
        events=[{"event_type": "workflow_run"} for _ in runs],
        requested_limit=50,
        include_git_ref=False,
    )

    assert summary["repo_slug"] == "owner/repo"
    assert summary["requested_limit"] == 50
    assert summary["run_count"] == 3
    assert summary["event_count"] == 3
    assert summary["provenance"] == "imported_history"
    assert summary["source_category"] == "history"
    assert summary["history_only"] is True
    assert summary["calibration_evidence"] is False
    assert summary["trust_guidance"] == {
        "use_for": "historical activity context and dashboard coverage backfill",
        "do_not_use_for": "reviewer or lens accuracy, lane promotion, or merge-gate policy",
        "next_step": (
            "run current dogfood uploads plus reviewer-runs or calibration evidence "
            "before making provider/lens decisions"
        ),
    }
    assert summary["git_ref_included"] is False
    assert summary["workflow_counts"] == {"CI": 2, "Dogfood": 1}
    assert summary["status_counts"] == {"completed": 2, "in_progress": 1}
    assert summary["conclusion_counts"] == {
        "failure": 1,
        "success": 1,
        "unknown": 1,
    }
    assert summary["oldest_run_at"] == "2026-06-15T00:00:00Z"
    assert summary["newest_run_at"] == "2026-06-17T00:00:00Z"
    assert summary["last_updated_at"] == "2026-06-17T00:03:00Z"


def test_cloud_setup_round_trip_writes_private_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "tokens" / "install.env"

        result = run_cloud_setup(
            token="cmw_live_test_secret_token",
            token_file=None,
            token_stdin=False,
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            endpoint="https://codemower.com/api/ingest",
            team_id="team",
            install_id="install",
            out=target,
            force=False,
            dry_run=False,
        )

        assert result["status"] == "written"
        assert target.stat().st_mode & 0o777 == 0o600
        text = target.read_text(encoding="utf-8")
        assert "CODE_MOWER_CLOUD_TOKEN" in text
        assert "cmw_live_test_secret_token" in text


def test_cloud_setup_helpers_keep_safe_defaults() -> None:
    assert safe_config_stem("  weird path/name  ") == "weird-path-name"
    assert safe_config_stem("") == DEFAULT_SETUP_INSTALL_ID
    rendered = str(default_setup_path("agent@local"))
    assert rendered.endswith("/.config/code-mower/tokens/agent-local.env")
    assert "sixteen-char-tok" not in token_prefix("sixteen-char-tok")


def test_cloud_event_args_accept_jsonl_and_normalize_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_file = root / "events.jsonl"
        event_file.write_text(
            "\n".join(
                [
                    json.dumps({"repo_slug": "owner/repo", "status": "observed"}),
                    json.dumps(
                        {
                            "event_type": "workflow_run",
                            "repo_slug": "owner/repo",
                            "status": "success",
                            "dimensions": {"workflow_name": "CI"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        parsed = parse_event_args([f"reviewer_run={event_file}"])

        assert len(parsed) == 2
        assert parsed[0]["schema"] == EVENT_SCHEMA
        assert parsed[0]["event_type"] == "reviewer_run"
        assert parsed[1]["event_type"] == "workflow_run"


def test_cloud_event_normalization_rejects_unsafe_metadata() -> None:
    try:
        normalize_event(
            {
                "repo_slug": "owner/repo",
                "status": "observed",
                "dimensions": {"output_preview": "secret-ish"},
            },
            "reviewer_run",
        )
    except CloudBundleError as exc:
        assert "unsafe field" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsafe metadata rejection")


def test_cloud_repo_slug_from_remote_supports_common_github_forms() -> None:
    assert repo_slug_from_remote("git@github.com:codemower-ai/code-mower.git") == "codemower-ai/code-mower"
    assert repo_slug_from_remote("https://github.com/codemower-ai/code-mower.git") == "codemower-ai/code-mower"
    assert repo_slug_from_remote("ssh://example.com/nope") == ""


def test_cloud_export_builds_metadata_only_bundle_from_client_module() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "reviewer-value-report.md"
        report.write_text("# Value report\n", encoding="utf-8")

        result = build_cloud_bundle(
            reports=[(report, "value-report")],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "codemower-ai/code-mower",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "tool_version": "0.139.0",
                        "provider": "openai",
                        "model": "gpt-5",
                        "integration": "cli",
                        "lens": "base",
                    },
                    "metrics": {"latency_ms": 42},
                }
            ],
            output_dir=root / "bundle",
            repo_slug="codemower-ai/code-mower",
        )

        assert result["mode"] == "cloud-export"
        assert (root / "bundle" / BUNDLE_MANIFEST_FILENAME).is_file()
        upload = build_upload_payload(bundle_dir=root / "bundle")
        assert upload["upload_mode"] == "metadata_only"
        assert upload["repo_slug"] == "codemower-ai/code-mower"
        assert upload["events"][0]["provider"] == "codex"
        assert upload["events"][0]["tool"]["tool_name"] == "codex"
        assert upload["events"][0]["tool"]["model"] == "gpt-5"
        assert upload["provenance"]["events_with_tool_provenance"] == 1
        assert upload["provenance"]["events_with_model_provenance"] == 1
        assert upload["provenance"]["events_with_tool_version_provenance"] == 1
        assert upload["provenance"]["tools"][0]["tool_name"] == "codex"


def test_provider_catalog_snapshot_events_are_metadata_only(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    events = build_provider_catalog_snapshot_events(
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
    )

    assert events
    codex = next(event for event in events if event["dimensions"]["lane_id"] == "codex")
    assert codex["event_type"] == "provider_catalog_snapshot"
    assert codex["repo_slug"] == "owner/repo"
    assert codex["source"] == "unit-test"
    assert codex["tool"]["tool_name"] == "codex"
    assert codex["tool"]["provider"] == "codex"
    assert codex["tool"]["model_source"] == "missing"
    assert codex["tool"]["version_source"] == "missing"
    assert codex["dimensions"]["catalog_snapshot"] is True
    assert codex["dimensions"]["merge_authority"] is True
    assert "token_env" not in codex["dimensions"]
    assert "auth" not in codex["dimensions"]


def test_provenance_summary_treats_vendor_hidden_model_as_known_source() -> None:
    summary = build_provenance_summary(
        [
            normalize_event(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "evt-vendor-hidden",
                    "event_type": "provider_catalog_snapshot",
                    "created_at": "2026-01-01T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "team_id": "team",
                    "install_id": "install",
                    "source": "unit-test",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gitar",
                        "provider": "gitar",
                        "model": "",
                        "model_source": "vendor_hidden",
                        "version_source": "not_probed",
                    },
                },
                "provider_catalog_snapshot",
            )
        ]
    )

    assert summary["events_with_model_provenance"] == 1
    assert summary["events_missing_model_provenance"] == 0
    assert summary["events_with_tool_version_provenance"] == 1
    assert summary["events_missing_tool_version_provenance"] == 0
    assert summary["inventory_event_count"] == 1
    assert summary["benchmark_event_count"] == 0
    assert summary["benchmark_events_with_model_provenance"] == 0
    assert summary["benchmark_events_missing_model_provenance"] == 0
    assert summary["benchmark_model_missing_providers"] == []
    assert summary["tools"][0]["model_sources"] == ["vendor_hidden"]
    assert summary["tools"][0]["version_sources"] == ["not_probed"]


def test_provenance_summary_preserves_source_quality_fields() -> None:
    summary = build_provenance_summary(
        [
            normalize_event(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "evt-code-mower",
                    "event_type": "dogfood_upload",
                    "created_at": "2026-01-01T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "team_id": "team",
                    "install_id": "install",
                    "source": "unit-test",
                    "tool": {
                        "role": "reporter",
                        "tool_name": "code-mower",
                        "tool_version": "0.5.0b28",
                        "provider": "code-mower",
                        "model": "",
                        "model_source": "not_applicable",
                        "version_source": "package_version",
                    },
                },
                "dogfood_upload",
            ),
            normalize_event(
                {
                    "schema": EVENT_SCHEMA,
                    "event_id": "evt-gemini",
                    "event_type": "provider_catalog_snapshot",
                    "created_at": "2026-01-01T00:00:00Z",
                    "repo_slug": "owner/repo",
                    "team_id": "team",
                    "install_id": "install",
                    "source": "unit-test",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gemini",
                        "tool_version": "0.45.2",
                        "provider": "gemini",
                        "model": "",
                        "model_source": "missing",
                        "version_source": "cli_version_probe",
                    },
                },
                "provider_catalog_snapshot",
            ),
        ]
    )

    assert summary["events_with_model_provenance"] == 1
    assert summary["events_missing_model_provenance"] == 1
    assert summary["events_with_tool_version_provenance"] == 2
    assert summary["events_missing_tool_version_provenance"] == 0
    assert summary["inventory_event_count"] == 1
    assert summary["benchmark_event_count"] == 1
    assert summary["benchmark_events_with_model_provenance"] == 1
    assert summary["benchmark_events_missing_model_provenance"] == 0
    assert summary["benchmark_events_with_tool_version_provenance"] == 1
    assert summary["benchmark_events_missing_tool_version_provenance"] == 0
    assert summary["benchmark_model_missing_providers"] == []
    rows = {row["tool_name"]: row for row in summary["tools"]}
    assert rows["code-mower"]["model_sources"] == ["not_applicable"]
    assert rows["code-mower"]["version_sources"] == ["package_version"]
    assert rows["gemini"]["model_sources"] == ["missing"]
    assert rows["gemini"]["version_sources"] == ["cli_version_probe"]


def test_dogfood_dry_run_preserves_version_probe(monkeypatch, tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    calls: list[bool] = []

    def fake_catalog_events(**kwargs):
        calls.append(bool(kwargs["include_version_probe"]))
        return []

    monkeypatch.setattr(
        "code_mower.cloud_client.operations.build_provider_catalog_snapshot_events",
        fake_catalog_events,
    )

    result = dogfood_upload(
        repo_path=tmp_path,
        output_dir=tmp_path / ".code-mower/cloud-dogfood-bundle",
        reports=[],
        events=[],
        repo_slug="owner/repo",
        team_id="team",
        install_id="install",
        source="unit-test",
        endpoint="https://codemower.com/api/ingest",
        token_env="CODE_MOWER_TEST_EMPTY_TOKEN",
        include_reports=False,
        yes=False,
        timeout=0.1,
    )

    assert result["status"] == "dry_run"
    assert calls == [True]


def test_cloud_doctor_runs_from_client_module() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = run_cloud_doctor(
            bundle_dir=Path(tmp) / "missing-bundle",
            endpoint="http://localhost:3000/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        assert report["mode"] == "cloud-doctor"
        assert report["status"] == "pass"
        assert report["warnings"] == 2
        rendered = render_cloud_doctor_text(report)
        assert "Code Mower cloud doctor" in rendered
        assert "http://localhost:3000" in rendered


def test_cloud_doctor_warns_when_model_provenance_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "provider": "codex",
                        "tool_version": "0.142.0",
                        "model_source": "missing",
                    },
                }
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert report["status"] == "pass"
        assert check["status"] == "warn"
        assert check["detail"]["providers"] == ["codex"]
        assert check["detail"]["model_env_by_provider"]["codex"] == [
            "CODEX_MODEL",
            "CODE_MOWER_CODEX_MODEL",
            "OPENAI_MODEL",
        ]
        assert check["detail"]["model_env_commands"] == [
            "code-mower providers provenance-env --provider codex --shell"
        ]
        assert "code-mower providers provenance-env --provider codex --shell" in check[
            "remediation"
        ]
        assert "CODE_MOWER_CODEX_MODEL" in check["remediation"]


def test_cloud_doctor_does_not_warn_for_inventory_only_model_gaps() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "provider_catalog_snapshot",
                    "repo_slug": "owner/repo",
                    "provider": "gemini",
                    "lens": "base",
                    "status": "observed",
                    "dimensions": {"catalog_snapshot": True},
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gemini",
                        "provider": "gemini",
                        "tool_version": "0.45.2",
                        "model_source": "missing",
                    },
                }
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert report["status"] == "pass"
        assert check["status"] == "pass"
        assert "benchmark evidence events" in check["message"]
        assert check["detail"]["raw_missing_model_events"] == 1
        assert check["detail"]["benchmark_missing_model_events"] == 0


def test_cloud_doctor_fans_out_multiple_model_provenance_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "provider": "codex",
                        "tool_version": "0.142.0",
                        "model_source": "missing",
                    },
                },
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "gemini",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "gemini",
                        "provider": "gemini",
                        "tool_version": "0.45.2",
                        "model_source": "missing",
                    },
                },
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert check["status"] == "warn"
        assert check["detail"]["model_env_commands"] == [
            "code-mower providers provenance-env --provider codex --shell",
            "code-mower providers provenance-env --provider gemini --shell",
        ]
        assert check["detail"]["model_env_command_all"] == (
            "code-mower providers provenance-env --provider codex "
            "--provider gemini --shell"
        )
        assert "detail.model_env_commands" in check["remediation"]
        assert "detail.model_env_command_all" in check["remediation"]


def test_cloud_doctor_passes_when_model_provenance_is_complete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_cloud_bundle(
            reports=[],
            events=[
                {
                    "event_type": "reviewer_run",
                    "repo_slug": "owner/repo",
                    "provider": "codex",
                    "lens": "base",
                    "status": "pass",
                    "tool": {
                        "role": "reviewer",
                        "tool_name": "codex",
                        "provider": "codex",
                        "tool_version": "0.142.0",
                        "model": "gpt-5",
                        "model_source": "env",
                    },
                }
            ],
            output_dir=root / "bundle",
            repo_slug="owner/repo",
        )

        report = run_cloud_doctor(
            bundle_dir=root / "bundle",
            endpoint="https://codemower.com/api/ingest",
            token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
            require_token=False,
        )

        check = next(
            item for item in report["checks"] if item["name"] == "model-provenance"
        )
        assert check["status"] == "pass"


def test_cloud_repo_sync_helpers_live_in_client_module() -> None:
    assert parse_repo_sync_spec("/tmp/repo") == ("", Path("/tmp/repo"))
    assert parse_repo_sync_spec("owner/repo=/tmp/repo") == ("owner/repo", Path("/tmp/repo"))
    assert repo_sync_output_name("Owner/Repo", Path("/tmp/repo"), 2) == "owner--repo-3"


def test_cloud_py_keeps_legacy_operation_aliases() -> None:
    assert cloud_cli._dogfood_upload is dogfood_upload
    assert cloud_cli._repo_sync_output_name("owner/repo", Path("/tmp/repo"), 0) == "owner--repo-1"


def test_cloud_repo_sync_yes_reports_no_events_when_no_steps_upload(monkeypatch, tmp_path) -> None:
    def no_events_step(**kwargs):
        return {
            "mode": "cloud-reviewer-runs",
            "status": "no_events",
            "repo_slug": kwargs["repo_slug"],
        }

    monkeypatch.setattr(cloud_operations, "reviewer_runs_upload", no_events_step)

    result = cloud_operations.repo_sync_upload(
        repo_specs=["owner/repo=/tmp/repo"],
        output_dir=tmp_path / "repo-sync",
        modes=["reviewer-runs"],
        team_id="team",
        install_id="install",
        source_prefix="test",
        limit=1,
        endpoint="http://localhost:3000/api/ingest",
        token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
        include_reports=False,
        include_git_ref=False,
        yes=True,
        timeout=1.0,
    )

    assert result["status"] == "no_events"
    assert result["repos"][0]["steps"][0]["status"] == "no_events"
