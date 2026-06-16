#!/usr/bin/env python3
"""Prepare opt-in Code Mower cloud benchmark bundles."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    from code_mower.cloud_client import (
        BUNDLE_MANIFEST_FILENAME,
        EVENT_SCHEMA,
        GITHUB_RUN_LIST_FIELDS,
        MAX_EVENT_COUNT,
        SAFE_EVENT_TYPES,
        SAFE_REPORT_KINDS,
        UPLOAD_SCHEMA as UPLOAD_SCHEMA,
        CloudBundleError,
        DEFAULT_INSTALL_ID_ENV,
        DEFAULT_SETUP_INSTALL_ID,
        DEFAULT_TEAM_ID_ENV,
        DEFAULT_TOKEN_ENV,
        DEFAULT_UPLOAD_ENDPOINT,
        build_cloud_bundle,
        catch_up_upload as _catch_up_upload,
        default_setup_path as _default_setup_path,
        build_upload_payload,
        default_dogfood_reports as _default_dogfood_reports,
        dogfood_upload as _dogfood_upload,
        event_id_from_github_run as _event_id_from_github_run,
        is_local_http_endpoint,
        load_bundle_manifest as load_bundle_manifest,
        load_event_file as _load_event_file,
        parse_repo_sync_spec as _parse_repo_sync_spec,
        parse_event_args as _parse_event_args,
        post_upload_payload,
        read_token_file as _read_token_file,
        render_bundle_readme,
        render_cloud_doctor_text,
        render_setup_env,
        repo_slug_from_remote as _repo_slug_from_remote,
        repo_sync_output_name as _repo_sync_output_name,
        repo_sync_upload as _repo_sync_upload,
        resolve_setup_token as _resolve_setup_token,
        reviewer_runs_upload as _reviewer_runs_upload,
        run_cloud_doctor,
        run_cloud_setup,
        run_git as _run_git,
        safe_config_stem as _safe_config_stem,
        safe_event_type as _safe_event_type,
        safe_kind as _safe_kind,
        token_prefix as _token_prefix,
        utc_now as _utc_now,
        validate_metadata_payload,
        write_setup_env_file,
    )
    from code_mower import code_mower_telemetry
else:  # pragma: no cover - exercised after package extraction.
    from .cloud_client import (
        BUNDLE_MANIFEST_FILENAME,
        EVENT_SCHEMA,
        GITHUB_RUN_LIST_FIELDS,
        MAX_EVENT_COUNT,
        SAFE_EVENT_TYPES,
        SAFE_REPORT_KINDS,
        UPLOAD_SCHEMA as UPLOAD_SCHEMA,
        CloudBundleError,
        DEFAULT_INSTALL_ID_ENV,
        DEFAULT_SETUP_INSTALL_ID,
        DEFAULT_TEAM_ID_ENV,
        DEFAULT_TOKEN_ENV,
        DEFAULT_UPLOAD_ENDPOINT,
        build_cloud_bundle,
        catch_up_upload as _catch_up_upload,
        default_setup_path as _default_setup_path,
        build_upload_payload,
        default_dogfood_reports as _default_dogfood_reports,
        dogfood_upload as _dogfood_upload,
        event_id_from_github_run as _event_id_from_github_run,
        is_local_http_endpoint,
        load_bundle_manifest as load_bundle_manifest,
        load_event_file as _load_event_file,
        parse_repo_sync_spec as _parse_repo_sync_spec,
        parse_event_args as _parse_event_args,
        post_upload_payload,
        read_token_file as _read_token_file,
        render_bundle_readme,
        render_cloud_doctor_text,
        render_setup_env,
        repo_slug_from_remote as _repo_slug_from_remote,
        repo_sync_output_name as _repo_sync_output_name,
        repo_sync_upload as _repo_sync_upload,
        resolve_setup_token as _resolve_setup_token,
        reviewer_runs_upload as _reviewer_runs_upload,
        run_cloud_doctor,
        run_cloud_setup,
        run_git as _run_git,
        safe_config_stem as _safe_config_stem,
        safe_event_type as _safe_event_type,
        safe_kind as _safe_kind,
        token_prefix as _token_prefix,
        utc_now as _utc_now,
        validate_metadata_payload,
        write_setup_env_file,
    )
    from . import code_mower_telemetry


DEFAULT_OUTPUT_DIR = ".code-mower/cloud-benchmark-bundle"
DEFAULT_CATCH_UP_OUTPUT_DIR = ".code-mower/cloud-catch-up-bundle"
DEFAULT_REVIEWER_RUNS_OUTPUT_DIR = ".code-mower/reviewer-run-bundle"
DEFAULT_REPO_SYNC_OUTPUT_DIR = ".code-mower/cloud-repo-sync"

# Keep the legacy cloud.py import surface intentional while implementation
# moves into code_mower.cloud_client modules.
__all__ = [
    "EVENT_SCHEMA",
    "GITHUB_RUN_LIST_FIELDS",
    "BUNDLE_MANIFEST_FILENAME",
    "CloudBundleError",
    "DEFAULT_INSTALL_ID_ENV",
    "DEFAULT_SETUP_INSTALL_ID",
    "DEFAULT_TEAM_ID_ENV",
    "DEFAULT_TOKEN_ENV",
    "DEFAULT_UPLOAD_ENDPOINT",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_CATCH_UP_OUTPUT_DIR",
    "DEFAULT_REVIEWER_RUNS_OUTPUT_DIR",
    "DEFAULT_REPO_SYNC_OUTPUT_DIR",
    "build_cloud_bundle",
    "build_upload_payload",
    "post_upload_payload",
    "render_bundle_readme",
    "render_setup_env",
    "run_cloud_doctor",
    "run_cloud_setup",
    "validate_metadata_payload",
    "write_setup_env_file",
    "main",
    "_catch_up_upload",
    "_default_dogfood_reports",
    "_default_setup_path",
    "_dogfood_upload",
    "_event_id_from_github_run",
    "_load_event_file",
    "_parse_event_args",
    "_parse_repo_sync_spec",
    "_read_token_file",
    "_repo_slug_from_remote",
    "_repo_sync_output_name",
    "_repo_sync_upload",
    "_resolve_setup_token",
    "_reviewer_runs_upload",
    "_run_git",
    "_safe_config_stem",
    "_safe_event_type",
    "_token_prefix",
    "_utc_now",
]


def _is_local_http_endpoint(endpoint: str) -> bool:
    return is_local_http_endpoint(endpoint)


def _parse_report_args(values: list[str]) -> list[tuple[Path, str]]:
    reports: list[tuple[Path, str]] = []
    for raw in values:
        if "=" not in raw:
            raise CloudBundleError(
                "--report entries must use KIND=PATH, for example reviewer-metrics=metrics.json"
            )
        kind, path_text = raw.split("=", 1)
        reports.append((Path(path_text), _safe_kind(kind)))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower cloud")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("setup")
    setup.add_argument(
        "--token",
        default="",
        help="team ingest token; prefer --token-stdin to avoid shell history",
    )
    setup.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="file containing either a raw token or CODE_MOWER_CLOUD_TOKEN assignment",
    )
    setup.add_argument(
        "--token-stdin",
        action="store_true",
        help="read the team ingest token from stdin",
    )
    setup.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    setup.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    setup.add_argument("--team-id", default=os.environ.get(DEFAULT_TEAM_ID_ENV, ""))
    setup.add_argument(
        "--install-id",
        default=os.environ.get(DEFAULT_INSTALL_ID_ENV, DEFAULT_SETUP_INSTALL_ID),
    )
    setup.add_argument(
        "--out",
        type=Path,
        default=None,
        help="env file to write; defaults to ~/.config/code-mower/tokens/<install-id>.env",
    )
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--json", action="store_true")
    export = subparsers.add_parser("export")
    export.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help=(
            "copy a shareable report into the bundle; kinds: "
            + ", ".join(sorted(SAFE_REPORT_KINDS))
        ),
    )
    export.add_argument(
        "--event",
        action="append",
        default=[],
        metavar="EVENT_TYPE=PATH",
        help=(
            "include structured benchmark events from JSON/JSONL; event types: "
            + ", ".join(sorted(SAFE_EVENT_TYPES))
        ),
    )
    export.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    export.add_argument("--repo-slug", default="")
    export.add_argument("--team-id", default="")
    export.add_argument("--install-id", default="")
    export.add_argument("--anonymous", action="store_true")
    export.add_argument("--json", action="store_true")
    upload = subparsers.add_parser("upload")
    upload.add_argument(
        "bundle_dir",
        nargs="?",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help="bundle directory created by `code-mower cloud export`",
    )
    upload.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    upload.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    upload.add_argument("--include-reports", action="store_true")
    upload.add_argument("--dry-run", action="store_true")
    upload.add_argument(
        "--yes",
        action="store_true",
        help="perform the network upload; without this, upload prints a dry-run preview",
    )
    upload.add_argument("--timeout", type=float, default=20.0)
    upload.add_argument("--json", action="store_true")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument(
        "bundle_dir",
        nargs="?",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help="bundle directory created by `code-mower cloud export`",
    )
    doctor.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    doctor.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    doctor.add_argument(
        "--probe-service",
        action="store_true",
        help="perform a lightweight GET against the endpoint's /api/health route",
    )
    doctor.add_argument("--timeout", type=float, default=5.0)
    doctor.add_argument("--json", action="store_true")
    dogfood = subparsers.add_parser("dogfood")
    dogfood.add_argument("--repo-path", type=Path, default=Path.cwd())
    dogfood.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    dogfood.add_argument("--repo-slug", default="")
    dogfood.add_argument("--team-id", default="")
    dogfood.add_argument("--install-id", default="")
    dogfood.add_argument("--source", default="local")
    dogfood.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help="override default dogfood reports with explicit KIND=PATH entries",
    )
    dogfood.add_argument(
        "--event",
        action="append",
        default=[],
        metavar="EVENT_TYPE=PATH",
        help="include additional structured benchmark events from JSON/JSONL",
    )
    dogfood.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    dogfood.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    dogfood.add_argument("--include-reports", action="store_true")
    dogfood.add_argument(
        "--yes",
        action="store_true",
        help="perform the network upload; without this, dogfood is a dry run",
    )
    dogfood.add_argument("--timeout", type=float, default=20.0)
    dogfood.add_argument("--json", action="store_true")
    catch_up = subparsers.add_parser("catch-up")
    catch_up.add_argument("--repo-path", type=Path, default=Path.cwd())
    catch_up.add_argument(
        "--output-dir",
        type=Path,
        default=Path(DEFAULT_CATCH_UP_OUTPUT_DIR),
    )
    catch_up.add_argument("--repo-slug", default="")
    catch_up.add_argument("--team-id", default="")
    catch_up.add_argument("--install-id", default="")
    catch_up.add_argument("--source", default="github-actions-catch-up")
    catch_up.add_argument(
        "--limit",
        type=int,
        default=50,
        help=f"number of recent GitHub Actions runs to include; max {MAX_EVENT_COUNT}",
    )
    catch_up.add_argument(
        "--include-git-ref",
        action="store_true",
        help="include workflow head branch and SHA in metadata",
    )
    catch_up.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    catch_up.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    catch_up.add_argument(
        "--yes",
        action="store_true",
        help="perform the network upload; without this, catch-up is a dry run",
    )
    catch_up.add_argument("--timeout", type=float, default=20.0)
    catch_up.add_argument("--json", action="store_true")
    reviewer_runs = subparsers.add_parser("reviewer-runs")
    reviewer_runs.add_argument("--repo-path", type=Path, default=Path.cwd())
    reviewer_runs.add_argument(
        "--verdicts",
        type=Path,
        default=code_mower_telemetry.default_verdict_artifact_dir(),
        help="verdict artifact file or directory",
    )
    reviewer_runs.add_argument(
        "--output-dir",
        type=Path,
        default=Path(DEFAULT_REVIEWER_RUNS_OUTPUT_DIR),
    )
    reviewer_runs.add_argument("--repo-slug", default="")
    reviewer_runs.add_argument("--team-id", default="")
    reviewer_runs.add_argument("--install-id", default="")
    reviewer_runs.add_argument(
        "--limit",
        type=int,
        default=MAX_EVENT_COUNT,
        help=f"number of verdict artifacts to include; max {MAX_EVENT_COUNT}",
    )
    reviewer_runs.add_argument(
        "--include-git-ref",
        action="store_true",
        help="include verdict head SHA metadata",
    )
    reviewer_runs.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    reviewer_runs.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    reviewer_runs.add_argument(
        "--yes",
        action="store_true",
        help="perform the network upload; without this, reviewer-runs is a dry run",
    )
    reviewer_runs.add_argument("--timeout", type=float, default=20.0)
    reviewer_runs.add_argument("--json", action="store_true")
    repo_sync = subparsers.add_parser("repo-sync")
    repo_sync.add_argument(
        "--repo",
        action="append",
        required=True,
        default=[],
        metavar="[OWNER/REPO=]PATH",
        help="repository path to sync; optionally prefix with OWNER/REPO=",
    )
    repo_sync.add_argument(
        "--mode",
        action="append",
        choices=("dogfood", "catch-up", "reviewer-runs"),
        default=[],
        help="sync mode to run; repeatable, defaults to dogfood and reviewer-runs",
    )
    repo_sync.add_argument(
        "--output-dir",
        type=Path,
        default=Path(DEFAULT_REPO_SYNC_OUTPUT_DIR),
    )
    repo_sync.add_argument("--team-id", default="")
    repo_sync.add_argument("--install-id", default="")
    repo_sync.add_argument("--source-prefix", default="local-repo-sync")
    repo_sync.add_argument(
        "--limit",
        type=int,
        default=MAX_EVENT_COUNT,
        help=f"number of catch-up/reviewer-run events per repo; max {MAX_EVENT_COUNT}",
    )
    repo_sync.add_argument(
        "--include-git-ref",
        action="store_true",
        help="include workflow branches/head SHAs and verdict head SHAs",
    )
    repo_sync.add_argument(
        "--endpoint",
        default=os.environ.get("CODE_MOWER_CLOUD_ENDPOINT", DEFAULT_UPLOAD_ENDPOINT),
    )
    repo_sync.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    repo_sync.add_argument("--include-reports", action="store_true")
    repo_sync.add_argument(
        "--yes",
        action="store_true",
        help="perform network uploads; without this, repo-sync is a dry run",
    )
    repo_sync.add_argument("--timeout", type=float, default=20.0)
    repo_sync.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "setup":
            result = run_cloud_setup(
                token=args.token,
                token_file=args.token_file,
                token_stdin=args.token_stdin,
                token_env=args.token_env,
                endpoint=args.endpoint,
                team_id=args.team_id,
                install_id=args.install_id,
                out=args.out,
                force=args.force,
                dry_run=args.dry_run,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud setup")
                print(f"Status: {result['status']}")
                print(f"Path: {result['path']}")
                print(f"Endpoint: {result['endpoint']}")
                if result["team_id"]:
                    print(f"Team: {result['team_id']}")
                print(f"Install: {result['install_id']}")
                print(f"Token: {result['token_prefix']}")
                if result["status"] == "written":
                    print(f"Load it with: {result['shell']}")
            return 0
        if args.command == "export":
            payload = build_cloud_bundle(
                reports=_parse_report_args(args.report),
                events=_parse_event_args(args.event),
                output_dir=args.output_dir,
                repo_slug=args.repo_slug,
                team_id=args.team_id,
                install_id=args.install_id,
                anonymous=args.anonymous,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud benchmark bundle")
                print(f"Output: {payload['output_dir']}")
                print(f"Manifest: {payload['manifest']}")
                print("Upload: local export only")
            return 0
        if args.command == "upload":
            payload = build_upload_payload(
                bundle_dir=args.bundle_dir,
                include_reports=args.include_reports,
            )
            dry_run = args.dry_run or not args.yes
            if dry_run:
                preview = {
                    "mode": "cloud-upload-dry-run",
                    "endpoint": args.endpoint,
                    "would_upload": False,
                    "requires_yes": not args.yes,
                    "upload_mode": payload["upload_mode"],
                    "report_count": len(payload["reports"]),
                    "event_count": len(payload["events"]),
                    "privacy_mode": payload["privacy_mode"],
                    "excluded_content": payload["excluded_content"],
                }
                if args.json:
                    print(json.dumps(preview, indent=2, sort_keys=True))
                else:
                    print("Code Mower cloud upload dry run")
                    print(f"Endpoint: {preview['endpoint']}")
                    print(f"Mode: {preview['upload_mode']}")
                    print(f"Reports: {preview['report_count']}")
                    print(f"Events: {preview['event_count']}")
                    print("Network: skipped (pass --yes to upload)")
                return 0
            token = os.environ.get(args.token_env, "")
            if not token and not _is_local_http_endpoint(args.endpoint):
                raise CloudBundleError(
                    f"{args.token_env} is not set; refusing non-local upload without a token"
                )
            result = post_upload_payload(
                payload=payload,
                endpoint=args.endpoint,
                token=token,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud upload complete")
                print(f"Endpoint: {result['endpoint']}")
                print(f"Status: {result['status']}")
            return 0
        if args.command == "doctor":
            report = run_cloud_doctor(
                bundle_dir=args.bundle_dir,
                endpoint=args.endpoint,
                token_env=args.token_env,
                probe_service=args.probe_service,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(render_cloud_doctor_text(report), end="")
            return 1 if report["failures"] else 0
        if args.command == "dogfood":
            result = _dogfood_upload(
                repo_path=args.repo_path,
                output_dir=args.output_dir,
                reports=_parse_report_args(args.report),
                events=_parse_event_args(args.event),
                repo_slug=args.repo_slug,
                team_id=args.team_id,
                install_id=args.install_id,
                source=args.source,
                endpoint=args.endpoint,
                token_env=args.token_env,
                include_reports=args.include_reports,
                yes=args.yes,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud dogfood")
                print(f"Status: {result['status']}")
                print(f"Bundle: {result['export']['output_dir']}")
                print(f"Reports: {len(result['export']['included_reports'])}")
                print(f"Events: {result['export']['event_count']}")
                if result["status"] == "uploaded":
                    print(f"Upload status: {result['upload']['status']}")
                elif result["status"] == "dry_run":
                    print("Network: skipped (pass --yes to upload)")
            return 1 if result["status"] == "doctor_failed" else 0
        if args.command == "catch-up":
            result = _catch_up_upload(
                repo_path=args.repo_path,
                output_dir=args.output_dir,
                repo_slug=args.repo_slug,
                team_id=args.team_id,
                install_id=args.install_id,
                source=args.source,
                limit=args.limit,
                endpoint=args.endpoint,
                token_env=args.token_env,
                yes=args.yes,
                timeout=args.timeout,
                include_git_ref=args.include_git_ref,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud catch-up")
                print(f"Status: {result['status']}")
                print(f"Repository: {result['repo_slug']}")
                print(f"Runs: {result['run_count']}")
                print(f"Bundle: {result['export']['output_dir']}")
                print(f"Events: {result['export']['event_count']}")
                if result["status"] == "uploaded":
                    print(f"Upload status: {result['upload']['status']}")
                elif result["status"] == "dry_run":
                    print("Network: skipped (pass --yes to upload)")
            return 1 if result["status"] == "doctor_failed" else 0
        if args.command == "reviewer-runs":
            result = _reviewer_runs_upload(
                repo_path=args.repo_path,
                verdicts=args.verdicts,
                output_dir=args.output_dir,
                repo_slug=args.repo_slug,
                team_id=args.team_id,
                install_id=args.install_id,
                limit=args.limit,
                endpoint=args.endpoint,
                token_env=args.token_env,
                yes=args.yes,
                timeout=args.timeout,
                include_git_ref=args.include_git_ref,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud reviewer runs")
                print(f"Status: {result['status']}")
                print(f"Repository: {result['repo_slug']}")
                print(f"Events: {result['event_count']}")
                if result["status"] == "no_events":
                    print(f"Verdicts: {result['verdicts']}")
                else:
                    print(f"Bundle: {result['export']['output_dir']}")
                if result["status"] == "uploaded":
                    print(f"Upload status: {result['upload']['status']}")
                elif result["status"] == "dry_run":
                    print("Network: skipped (pass --yes to upload)")
            return 1 if result["status"] == "doctor_failed" else 0
        if args.command == "repo-sync":
            result = _repo_sync_upload(
                repo_specs=args.repo,
                output_dir=args.output_dir,
                modes=args.mode,
                team_id=args.team_id,
                install_id=args.install_id,
                source_prefix=args.source_prefix,
                limit=args.limit,
                endpoint=args.endpoint,
                token_env=args.token_env,
                include_reports=args.include_reports,
                include_git_ref=args.include_git_ref,
                yes=args.yes,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("Code Mower cloud repo sync")
                print(f"Status: {result['status']}")
                print(f"Repositories: {result['repo_count']}")
                print(f"Steps: {result['step_count']}")
                print(f"Errors: {result['error_count']}")
            return 0 if result["error_count"] == 0 else 1
    except CloudBundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled cloud command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
