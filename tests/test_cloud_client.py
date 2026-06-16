from __future__ import annotations

import json
import tempfile
from pathlib import Path

from code_mower.cloud_client import (
    BUNDLE_MANIFEST_FILENAME,
    CloudBundleError,
    DEFAULT_SETUP_INSTALL_ID,
    EVENT_SCHEMA,
    build_cloud_bundle,
    build_upload_payload,
    default_setup_path,
    normalize_event,
    parse_event_args,
    repo_slug_from_remote,
    run_cloud_setup,
    safe_config_stem,
    token_prefix,
)


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
