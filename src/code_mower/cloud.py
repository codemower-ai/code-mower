#!/usr/bin/env python3
"""Prepare opt-in Code Mower cloud benchmark bundles."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    from code_mower.cloud_client import (
        BUNDLE_MANIFEST_FILENAME,
        BUNDLE_SCHEMA,
        EXCLUDED_CONTENT,
        EXPECTED_BUNDLE_ENTRIES,
        MAX_EVENT_COUNT,
        SAFE_EVENT_TYPES,
        SAFE_REPORT_KINDS,
        UPLOAD_SCHEMA as UPLOAD_SCHEMA,
        CloudBundleError,
        dashboard_url_for_endpoint,
        build_upload_payload,
        build_dogfood_dry_run_preview,
        build_dogfood_plan,
        default_dogfood_reports,
        DogfoodPlan,
        health_url_for_endpoint,
        is_local_http_endpoint,
        is_bundle_manifest,
        load_bundle_manifest as load_bundle_manifest,
        post_upload_payload,
        probe_cloud_service,
        validate_metadata_payload,
        validate_upload_endpoint,
    )
    from code_mower import code_mower_telemetry
else:  # pragma: no cover - exercised after package extraction.
    from .cloud_client import (
        BUNDLE_MANIFEST_FILENAME,
        BUNDLE_SCHEMA,
        EXCLUDED_CONTENT,
        EXPECTED_BUNDLE_ENTRIES,
        MAX_EVENT_COUNT,
        SAFE_EVENT_TYPES,
        SAFE_REPORT_KINDS,
        UPLOAD_SCHEMA as UPLOAD_SCHEMA,
        CloudBundleError,
        dashboard_url_for_endpoint,
        build_upload_payload,
        build_dogfood_dry_run_preview,
        build_dogfood_plan,
        default_dogfood_reports,
        DogfoodPlan,
        health_url_for_endpoint,
        is_local_http_endpoint,
        is_bundle_manifest,
        load_bundle_manifest as load_bundle_manifest,
        post_upload_payload,
        probe_cloud_service,
        validate_metadata_payload,
        validate_upload_endpoint,
    )
    from . import code_mower_telemetry


EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
DEFAULT_OUTPUT_DIR = ".code-mower/cloud-benchmark-bundle"
DEFAULT_CATCH_UP_OUTPUT_DIR = ".code-mower/cloud-catch-up-bundle"
DEFAULT_REVIEWER_RUNS_OUTPUT_DIR = ".code-mower/reviewer-run-bundle"
DEFAULT_UPLOAD_ENDPOINT = "https://codemower.com/api/ingest"
DEFAULT_TOKEN_ENV = "CODE_MOWER_CLOUD_TOKEN"
DEFAULT_TEAM_ID_ENV = "CODE_MOWER_CLOUD_TEAM_ID"
DEFAULT_INSTALL_ID_ENV = "CODE_MOWER_INSTALL_ID"
DEFAULT_SETUP_INSTALL_ID = "code-mower-local"
GITHUB_RUN_LIST_FIELDS = (
    "databaseId",
    "name",
    "status",
    "conclusion",
    "event",
    "headBranch",
    "headSha",
    "createdAt",
    "updatedAt",
    "url",
)


def _is_local_http_endpoint(endpoint: str) -> bool:
    return is_local_http_endpoint(endpoint)


def _validate_upload_endpoint(endpoint: str) -> None:
    try:
        validate_upload_endpoint(endpoint)
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc


def _probe_cloud_service(endpoint: str, *, timeout: float) -> dict[str, Any]:
    return probe_cloud_service(endpoint, timeout=timeout)


def _token_prefix(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    visible = min(12, max(4, len(token) // 2), max(0, len(token) - 4))
    if visible <= 0:
        return "<redacted>"
    return token[:visible] + "..."


def _safe_config_stem(value: str) -> str:
    stem = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in value.strip()
    )
    return stem.strip("-_.") or DEFAULT_SETUP_INSTALL_ID


def _default_setup_path(install_id: str) -> Path:
    return (
        Path.home()
        / ".config"
        / "code-mower"
        / "tokens"
        / f"{_safe_config_stem(install_id)}.env"
    )


def _read_token_file(path: Path) -> str:
    source = path.expanduser()
    if not source.is_file():
        raise CloudBundleError(f"token file does not exist or is not a file: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CloudBundleError(f"token file is not UTF-8 text: {source}") from exc
    except OSError as exc:
        raise CloudBundleError(f"unable to read token file {source}: {exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if stripped.startswith(f"{DEFAULT_TOKEN_ENV}="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return text.strip()


def _resolve_setup_token(
    *,
    token: str,
    token_file: Path | None,
    token_stdin: bool,
    token_env: str,
) -> str:
    explicit_sources = sum(
        1 for value in (bool(token), token_file is not None, token_stdin) if value
    )
    if explicit_sources > 1:
        raise CloudBundleError("choose only one token source: --token, --token-file, or --token-stdin")
    if token:
        resolved = token.strip()
    elif token_file is not None:
        resolved = _read_token_file(token_file)
    elif token_stdin:
        resolved = sys.stdin.read().strip()
    else:
        resolved = os.environ.get(token_env, "").strip()
    if not resolved:
        raise CloudBundleError(
            "cloud setup needs a token; pass --token-stdin, --token-file, "
            f"or set {token_env}"
        )
    return resolved


def render_setup_env(
    *,
    token: str,
    endpoint: str,
    team_id: str,
    install_id: str,
) -> str:
    _validate_upload_endpoint(endpoint)
    assignments = {
        DEFAULT_TOKEN_ENV: token.strip(),
        "CODE_MOWER_CLOUD_ENDPOINT": endpoint.strip(),
        DEFAULT_TEAM_ID_ENV: team_id.strip(),
        DEFAULT_INSTALL_ID_ENV: install_id.strip(),
    }
    lines = [
        "# Code Mower Cloud local token file",
        "# Keep this file private. It contains a bearer token.",
    ]
    lines.extend(
        f"export {name}={shlex.quote(value)}"
        for name, value in assignments.items()
        if value
    )
    return "\n".join(lines) + "\n"


def write_setup_env_file(
    *,
    path: Path,
    text: str,
    force: bool = False,
) -> None:
    target = path.expanduser()
    if target.exists() and not force:
        raise CloudBundleError(f"setup file already exists; pass --force to overwrite: {target}")
    parent_existed = target.parent.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            target.parent.chmod(0o700)
    except OSError as exc:
        raise CloudBundleError(f"unable to prepare setup directory {target.parent}: {exc}") from exc
    flags = os.O_WRONLY | os.O_CREAT
    if not force:
        flags |= os.O_EXCL
    try:
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.truncate(0)
            handle.write(text)
        target.chmod(0o600)
    except FileExistsError as exc:
        raise CloudBundleError(
            f"setup file already exists; pass --force to overwrite: {target}"
        ) from exc
    except OSError as exc:
        raise CloudBundleError(f"unable to write setup file {target}: {exc}") from exc


def run_cloud_setup(
    *,
    token: str,
    token_file: Path | None,
    token_stdin: bool,
    token_env: str,
    endpoint: str,
    team_id: str,
    install_id: str,
    out: Path | None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    resolved_install_id = install_id.strip() or DEFAULT_SETUP_INSTALL_ID
    target = out.expanduser() if out else _default_setup_path(resolved_install_id)
    resolved_token = _resolve_setup_token(
        token=token,
        token_file=token_file,
        token_stdin=token_stdin,
        token_env=token_env,
    )
    env_text = render_setup_env(
        token=resolved_token,
        endpoint=endpoint,
        team_id=team_id,
        install_id=resolved_install_id,
    )
    if not dry_run:
        write_setup_env_file(path=target, text=env_text, force=force)
    return {
        "mode": "cloud-setup",
        "status": "dry_run" if dry_run else "written",
        "path": str(target),
        "endpoint": endpoint,
        "team_id": team_id,
        "install_id": resolved_install_id,
        "token_prefix": _token_prefix(resolved_token),
        "shell": f"source {shlex.quote(str(target))}",
    }


def _safe_kind(value: str) -> str:
    kind = value.strip()
    if kind not in SAFE_REPORT_KINDS:
        allowed = ", ".join(sorted(SAFE_REPORT_KINDS))
        raise CloudBundleError(f"unsupported report kind {value!r}; allowed: {allowed}")
    return kind


def _safe_event_type(value: str) -> str:
    event_type = value.strip()
    if event_type not in SAFE_EVENT_TYPES:
        allowed = ", ".join(sorted(SAFE_EVENT_TYPES))
        raise CloudBundleError(
            f"unsupported event type {value!r}; allowed: {allowed}"
        )
    return event_type


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _run_git(repo_path: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _repo_slug_from_remote(remote_url: str) -> str:
    remote = remote_url.strip()
    if not remote:
        return ""
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("https://github.com/"):
        remote = remote.removeprefix("https://github.com/")
    elif remote.startswith("http://github.com/"):
        remote = remote.removeprefix("http://github.com/")
    else:
        return ""
    remote = remote.removesuffix(".git").strip("/")
    parts = remote.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _detect_repo_slug(repo_path: Path) -> str:
    return _repo_slug_from_remote(_run_git(repo_path, ["config", "--get", "remote.origin.url"]))


def _build_dogfood_event(
    *,
    repo_path: Path,
    plan: DogfoodPlan,
) -> dict[str, Any]:
    status = _run_git(repo_path, ["status", "--porcelain"])
    return {
        "schema": EVENT_SCHEMA,
        "event_id": str(uuid.uuid4()),
        "event_type": "dogfood_upload",
        "created_at": _utc_now(),
        "repo_slug": plan.repo_slug,
        "team_id": plan.team_id,
        "install_id": plan.install_id,
        "source": plan.source,
        "provider": "",
        "lens": "",
        "status": "observed",
        "git": {
            "sha": _run_git(repo_path, ["rev-parse", "HEAD"]),
            "branch": _run_git(repo_path, ["branch", "--show-current"]),
            "dirty": bool(status),
        },
        "metrics": {
            "report_count": plan.report_count,
            "extra_event_count": plan.extra_event_count,
        },
        "dimensions": {
            "command": "code-mower cloud dogfood",
        },
    }


def _event_id_from_github_run(repo_slug: str, run: dict[str, Any]) -> str:
    database_id = str(run.get("databaseId") or "").strip()
    if database_id:
        run_url = f"https://github.com/{repo_slug}/actions/runs/{database_id}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, run_url))
    run_url = str(run.get("url") or "").strip()
    if run_url:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, run_url))
    return str(uuid.uuid4())


def _run_gh_run_list(
    *,
    repo_slug: str,
    limit: int,
    repo_path: Path,
) -> list[dict[str, Any]]:
    command = [
        "gh",
        "run",
        "list",
        "--repo",
        repo_slug,
        "--limit",
        str(limit),
        "--json",
        ",".join(GITHUB_RUN_LIST_FIELDS),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CloudBundleError("GitHub Actions catch-up requires the `gh` CLI") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CloudBundleError(
            "unable to read GitHub Actions runs with `gh run list`"
            + (f": {detail}" if detail else "")
        )
    try:
        parsed = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise CloudBundleError("`gh run list --json` returned invalid JSON") from exc
    if not isinstance(parsed, list):
        raise CloudBundleError("`gh run list --json` must return a JSON array")
    runs: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise CloudBundleError("`gh run list --json` returned a non-object run")
        runs.append(item)
    return runs


def _build_workflow_run_event(
    *,
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    run: dict[str, Any],
    include_git_ref: bool,
) -> dict[str, Any]:
    status = str(run.get("conclusion") or run.get("status") or "observed")
    dimensions: dict[str, Any] = {
        "workflow_name": str(run.get("name") or ""),
        "trigger": str(run.get("event") or ""),
        "run_status": str(run.get("status") or ""),
        "run_conclusion": str(run.get("conclusion") or ""),
        "run_url": str(run.get("url") or ""),
        "updated_at": str(run.get("updatedAt") or ""),
    }
    if include_git_ref:
        dimensions["head_branch"] = str(run.get("headBranch") or "")
        dimensions["head_sha"] = str(run.get("headSha") or "")
    event = {
        "schema": EVENT_SCHEMA,
        "event_id": _event_id_from_github_run(repo_slug, run),
        "event_type": "workflow_run",
        "created_at": str(run.get("createdAt") or _utc_now()),
        "repo_slug": repo_slug,
        "team_id": team_id,
        "install_id": install_id,
        "source": source,
        "provider": "",
        "lens": "base",
        "status": status,
        "metrics": {},
        "dimensions": dimensions,
    }
    return _normalize_event(event, "workflow_run")


def _normalize_event(value: dict[str, Any], event_type: str) -> dict[str, Any]:
    validate_metadata_payload(value)
    normalized = dict(value)
    normalized["schema"] = str(normalized.get("schema") or EVENT_SCHEMA)
    if normalized["schema"] != EVENT_SCHEMA:
        raise CloudBundleError(
            f"unsupported event schema {normalized['schema']!r}; expected {EVENT_SCHEMA}"
        )
    normalized["event_type"] = _safe_event_type(
        str(normalized.get("event_type") or event_type)
    )
    normalized["event_id"] = str(normalized.get("event_id") or uuid.uuid4())
    normalized["created_at"] = str(normalized.get("created_at") or _utc_now())
    for key in ("repo_slug", "team_id", "install_id", "source", "provider", "lens", "status"):
        normalized[key] = str(normalized.get(key) or "")
    metrics = normalized.get("metrics")
    if metrics is None:
        normalized["metrics"] = {}
    elif not isinstance(metrics, dict):
        raise CloudBundleError("structured event metrics must be an object")
    dimensions = normalized.get("dimensions")
    if dimensions is None:
        normalized["dimensions"] = {}
    elif not isinstance(dimensions, dict):
        raise CloudBundleError("structured event dimensions must be an object")
    validate_metadata_payload(normalized)
    return normalized


def _load_event_file(path: Path, event_type: str) -> list[dict[str, Any]]:
    source = path.expanduser()
    if not source.is_file():
        raise CloudBundleError(f"event file does not exist or is not a file: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CloudBundleError(f"event file is not UTF-8 text: {source}") from exc
    except OSError as exc:
        raise CloudBundleError(f"unable to read event file {source}: {exc}") from exc
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CloudBundleError(
                    f"event file {source} line {line_number} is not JSON"
                ) from exc
    if isinstance(parsed, dict):
        parsed_events = [parsed]
    elif isinstance(parsed, list):
        parsed_events = parsed
    else:
        raise CloudBundleError(f"event file must contain an object, array, or JSONL: {source}")
    events: list[dict[str, Any]] = []
    for item in parsed_events:
        if not isinstance(item, dict):
            raise CloudBundleError(f"event file contains a non-object event: {source}")
        events.append(_normalize_event(item, event_type))
    return events


def _parse_event_args(values: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in values:
        if "=" not in raw:
            raise CloudBundleError(
                "--event entries must use EVENT_TYPE=PATH, for example reviewer_run=run.json"
            )
        event_type, path_text = raw.split("=", 1)
        events.extend(_load_event_file(Path(path_text), _safe_event_type(event_type)))
    if len(events) > MAX_EVENT_COUNT:
        raise CloudBundleError(
            f"too many events: {len(events)}; max {MAX_EVENT_COUNT}"
        )
    return events


def _safe_target_name(path: Path, index: int, *, anonymous: bool = False) -> str:
    suffix = path.suffix.lower()
    if suffix not in {".json", ".jsonl", ".md", ".txt"}:
        raise CloudBundleError(
            f"unsupported report file extension for {path}; expected .json, .jsonl, .md, or .txt"
        )
    if anonymous:
        return f"{index:02d}-report{suffix}"
    stem = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in path.stem)
    stem = stem.strip("-_") or f"report-{index}"
    return f"{index:02d}-{stem}{suffix}"


def _plan_report(
    path: Path,
    output_dir: Path,
    kind: str,
    index: int,
    *,
    anonymous: bool,
) -> dict[str, Any]:
    source = path.expanduser()
    if not source.is_file():
        raise CloudBundleError(f"report does not exist or is not a file: {source}")
    target_name = _safe_target_name(source, index, anonymous=anonymous)
    reports_dir = output_dir / "reports"
    try:
        if source.resolve().is_relative_to(reports_dir.resolve()):
            raise CloudBundleError(
                f"report source must not be inside the bundle reports directory: {source}"
            )
    except OSError as exc:
        raise CloudBundleError(f"unable to resolve report path {source}: {exc}") from exc
    return {
        "kind": kind,
        "source": source,
        "source_basename": "" if anonymous else source.name,
        "target_name": target_name,
        "target": f"reports/{target_name}",
    }


def _stage_reports(planned_reports: list[dict[str, Any]], output_dir: Path) -> tuple[list[dict[str, Any]], Path]:
    stage_dir = output_dir / ".reports.tmp"
    if stage_dir.exists():
        if not stage_dir.is_dir():
            raise CloudBundleError(f"bundle staging path is not a directory: {stage_dir}")
        try:
            shutil.rmtree(stage_dir)
        except OSError as exc:
            raise CloudBundleError(f"unable to clear stale staging directory: {exc}") from exc
    try:
        stage_dir.mkdir(parents=True)
    except OSError as exc:
        raise CloudBundleError(f"unable to create bundle staging directory: {exc}") from exc

    included_reports: list[dict[str, Any]] = []
    try:
        for report in planned_reports:
            source = report["source"]
            target = stage_dir / report["target_name"]
            shutil.copyfile(source, target)
            stat = target.stat()
            included_reports.append(
                {
                    "kind": report["kind"],
                    "source_basename": report["source_basename"],
                    "target": report["target"],
                    "bytes": stat.st_size,
                }
            )
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise CloudBundleError(f"unable to stage bundle reports: {exc}") from exc
    return included_reports, stage_dir


def _swap_reports(stage_dir: Path, reports_dir: Path) -> None:
    try:
        if reports_dir.exists():
            shutil.rmtree(reports_dir)
        stage_dir.replace(reports_dir)
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise CloudBundleError(f"unable to install staged bundle reports: {exc}") from exc


def _existing_bundle_manifest(output_dir: Path) -> bool:
    manifest_path = output_dir / BUNDLE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return is_bundle_manifest(manifest)


def _unexpected_bundle_entries(output_dir: Path) -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in output_dir.iterdir()
            if entry.name not in EXPECTED_BUNDLE_ENTRIES
        )
    except OSError as exc:
        raise CloudBundleError(f"unable to inspect bundle output directory: {exc}") from exc


def build_cloud_bundle(
    *,
    reports: list[tuple[Path, str]],
    events: list[dict[str, Any]] | None = None,
    output_dir: Path,
    repo_slug: str = "",
    install_id: str = "",
    team_id: str = "",
    anonymous: bool = False,
) -> dict[str, Any]:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise CloudBundleError(f"bundle output path is not a directory: {output_dir}")
        try:
            has_existing_content = any(output_dir.iterdir())
        except OSError as exc:
            raise CloudBundleError(f"unable to inspect bundle output directory: {exc}") from exc
        if has_existing_content and not _existing_bundle_manifest(output_dir):
            raise CloudBundleError(
                "refusing to write into an existing non-bundle output directory; "
                "choose a fresh --output-dir"
            )
        unexpected_entries = _unexpected_bundle_entries(output_dir)
        if unexpected_entries:
            sample = ", ".join(unexpected_entries[:3])
            if len(unexpected_entries) > 3:
                sample += f", ... ({len(unexpected_entries)} total)"
            raise CloudBundleError(
                "refusing to reuse bundle directory with unexpected entries: "
                f"{sample}"
            )
    else:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CloudBundleError(
                f"unable to create bundle output directory {output_dir}: {exc}"
            ) from exc
    planned_reports = [
        _plan_report(path, output_dir, _safe_kind(kind), index, anonymous=anonymous)
        for index, (path, kind) in enumerate(reports, start=1)
    ]
    included_reports, stage_dir = _stage_reports(planned_reports, output_dir)
    included_events = [
        _normalize_event(event, str(event.get("event_type") or "dogfood_upload"))
        for event in events or []
    ]
    if len(included_events) > MAX_EVENT_COUNT:
        raise CloudBundleError(
            f"too many events: {len(included_events)}; max {MAX_EVENT_COUNT}"
        )
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "privacy_mode": "anonymous" if anonymous else "metadata_and_reports",
        "upload_ready": False,
        "upload_status": "local_export_only",
        "repo_slug": "" if anonymous else repo_slug,
        "team_id": "" if anonymous else team_id,
        "install_id": "" if anonymous else install_id,
        "included_reports": included_reports,
        "events": [] if anonymous else included_events,
        "excluded_content": list(EXCLUDED_CONTENT),
        "notes": [
            "This bundle is local-only; upload support must present a dry-run before network transfer.",
            "Do not include source code, raw diffs, raw transcripts, raw stdout/stderr, auth output, or secrets.",
            "Reports are copied exactly as supplied; review them before sharing outside your machine.",
            "Structured events are metadata-only and should not contain source, diffs, transcripts, stdout/stderr, auth output, or secrets.",
        ],
    }
    manifest_path = output_dir / BUNDLE_MANIFEST_FILENAME
    readme = output_dir / "README.md"
    manifest_tmp = output_dir / f".{BUNDLE_MANIFEST_FILENAME}.tmp"
    readme_tmp = output_dir / ".README.md.tmp"
    try:
        manifest_tmp.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise CloudBundleError(f"unable to write bundle manifest {manifest_tmp}: {exc}") from exc
    try:
        readme_tmp.write_text(render_bundle_readme(manifest), encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        manifest_tmp.unlink(missing_ok=True)
        raise CloudBundleError(f"unable to write bundle README {readme_tmp}: {exc}") from exc
    _swap_reports(stage_dir, output_dir / "reports")
    try:
        manifest_tmp.replace(manifest_path)
        readme_tmp.replace(readme)
    except OSError as exc:
        raise CloudBundleError(f"unable to install bundle manifest files: {exc}") from exc
    return {
        "mode": "cloud-export",
        "output_dir": str(output_dir),
        "manifest": str(manifest_path),
        "readme": str(readme),
        "included_reports": included_reports,
        "event_count": len(manifest["events"]),
        "upload_ready": False,
    }


def run_cloud_doctor(
    *,
    bundle_dir: Path,
    endpoint: str,
    token_env: str,
    require_token: bool = True,
    probe_service: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    endpoint_is_valid = False

    try:
        _validate_upload_endpoint(endpoint)
        endpoint_is_valid = True
        checks.append(
            {
                "name": "endpoint",
                "status": "pass",
                "message": f"upload endpoint is allowed: {endpoint}",
            }
        )
    except CloudBundleError as exc:
        checks.append(
            {
                "name": "endpoint",
                "status": "fail",
                "message": str(exc),
            }
        )

    if probe_service and endpoint_is_valid:
        checks.append(_probe_cloud_service(endpoint, timeout=timeout))
    elif endpoint_is_valid:
        checks.append(
            {
                "name": "service",
                "status": "skip",
                "message": "service health probe skipped; pass --probe-service to check it",
                "detail": {"health_url": health_url_for_endpoint(endpoint)},
            }
        )

    token_present = bool(os.environ.get(token_env, ""))
    if token_present:
        checks.append(
            {
                "name": "token",
                "status": "pass",
                "message": f"{token_env} is set",
            }
        )
    elif _is_local_http_endpoint(endpoint):
        checks.append(
            {
                "name": "token",
                "status": "warn",
                "message": (
                    f"{token_env} is not set; local configless ingest may still work"
                ),
            }
        )
    elif not require_token:
        checks.append(
            {
                "name": "token",
                "status": "warn",
                "message": f"{token_env} is not set; upload will remain disabled",
                "remediation": (
                    "Run `code-mower cloud setup --token-stdin` or export "
                    f"{token_env} before using --yes."
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "token",
                "status": "fail",
                "message": f"{token_env} is not set",
                "remediation": (
                    "Set CODE_MOWER_CLOUD_TOKEN to a team ingest token before "
                    "running cloud upload --yes."
                ),
            }
        )

    manifest_path = bundle_dir / BUNDLE_MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            payload = build_upload_payload(bundle_dir=bundle_dir, include_reports=False)
            checks.append(
                {
                    "name": "bundle",
                    "status": "pass",
                    "message": (
                        f"bundle is readable with {len(payload['reports'])} report summaries "
                        f"and {len(payload['events'])} structured events"
                    ),
                }
            )
        except CloudBundleError as exc:
            checks.append(
                {
                    "name": "bundle",
                    "status": "fail",
                    "message": str(exc),
                }
            )
    else:
        checks.append(
            {
                "name": "bundle",
                "status": "warn",
                "message": (
                    f"bundle manifest not found at {manifest_path}; run cloud export first"
                ),
            }
        )

    failures = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    return {
        "mode": "cloud-doctor",
        "status": "fail" if failures else "pass",
        "endpoint": endpoint,
        "dashboard_url": dashboard_url_for_endpoint(endpoint),
        "health_url": health_url_for_endpoint(endpoint),
        "token_env": token_env,
        "bundle_dir": str(bundle_dir),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "next_steps": [
            {
                "id": "open-dashboard",
                "label": "Open the Code Mower Cloud dashboard",
                "url": dashboard_url_for_endpoint(endpoint),
            },
            {
                "id": "setup-token",
                "label": "Store a dashboard-issued token locally",
                "command": (
                    "code-mower cloud setup --token-stdin "
                    "--team-id YOUR_TEAM_SLUG --install-id YOUR_INSTALL_ID"
                ),
            },
            {
                "id": "dry-run-upload",
                "label": "Preview the metadata upload without network transfer",
                "command": f"code-mower cloud upload {shlex.quote(str(bundle_dir))} --dry-run --json",
            },
            {
                "id": "upload",
                "label": "Upload metadata after inspecting the dry run",
                "command": f"code-mower cloud upload {shlex.quote(str(bundle_dir))} --yes --json",
            },
        ],
    }


def render_cloud_doctor_text(report: dict[str, Any]) -> str:
    lines = [
        "Code Mower cloud doctor",
        f"Status: {report['status']}",
        f"Endpoint: {report['endpoint']}",
        f"Dashboard: {report.get('dashboard_url', '')}",
        f"Token env: {report['token_env']}",
        f"Bundle: {report['bundle_dir']}",
        "",
    ]
    for check in report["checks"]:
        lines.append(
            f"- {check['status'].upper()} {check['name']}: {check['message']}"
        )
        if check.get("remediation"):
            lines.append(f"  remediation: {check['remediation']}")
    return "\n".join(lines) + "\n"


def render_bundle_readme(manifest: dict[str, Any]) -> str:
    lines = [
        "# Code Mower Cloud Benchmark Bundle",
        "",
        "This local bundle is the cloud-ready handoff shape for opt-in benchmark reporting.",
        "It is not uploaded by the OSS CLI.",
        "",
        "Excluded by default:",
    ]
    lines.extend(f"- {item}" for item in manifest["excluded_content"])
    lines.extend(["", "Included reports:"])
    if manifest["included_reports"]:
        lines.extend(
            f"- {entry['target']} ({entry['kind']}, {entry['bytes']} bytes)"
            for entry in manifest["included_reports"]
        )
    else:
        lines.append("- none")
    lines.extend(["", "Structured events:"])
    events = manifest.get("events", [])
    if events:
        lines.extend(
            f"- {entry.get('event_type', 'unknown')} ({entry.get('event_id', 'no-id')})"
            for entry in events
        )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Before sharing this bundle, inspect every file under `reports/`.",
            "Future upload support should require an explicit dry run and user confirmation.",
            "",
        ]
    )
    return "\n".join(lines)


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


def _default_dogfood_reports(repo_path: Path) -> list[tuple[Path, str]]:
    return default_dogfood_reports(repo_path)


def _dogfood_upload(
    *,
    repo_path: Path,
    output_dir: Path,
    reports: list[tuple[Path, str]],
    events: list[dict[str, Any]],
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    endpoint: str,
    token_env: str,
    include_reports: bool,
    yes: bool,
    timeout: float,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    detected_repo_slug = repo_slug or _detect_repo_slug(repo_path)
    if not detected_repo_slug:
        raise CloudBundleError(
            "unable to detect repo slug; pass --repo-slug OWNER/REPO"
        )
    resolved_team_id = team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "")
    resolved_install_id = install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "")
    resolved_reports = reports or _default_dogfood_reports(repo_path)
    dogfood_plan = build_dogfood_plan(
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        source=source,
        reports=resolved_reports,
        events=events,
    )
    all_events = [
        _build_dogfood_event(
            repo_path=repo_path,
            plan=dogfood_plan,
        ),
        *events,
    ]
    export_result = build_cloud_bundle(
        reports=resolved_reports,
        events=all_events,
        output_dir=output_dir,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        anonymous=False,
    )
    doctor_result = run_cloud_doctor(
        bundle_dir=output_dir,
        endpoint=endpoint,
        token_env=token_env,
        require_token=yes,
    )
    if doctor_result["failures"]:
        return {
            "mode": "cloud-dogfood",
            "status": "doctor_failed",
            "export": export_result,
            "doctor": doctor_result,
        }
    payload = build_upload_payload(
        bundle_dir=output_dir,
        include_reports=include_reports,
    )
    if not yes:
        return {
            "mode": "cloud-dogfood",
            "status": "dry_run",
            "export": export_result,
            "doctor": doctor_result,
            "upload": build_dogfood_dry_run_preview(endpoint=endpoint, payload=payload),
        }
    token = os.environ.get(token_env, "")
    if not token and not _is_local_http_endpoint(endpoint):
        raise CloudBundleError(
            f"{token_env} is not set; refusing non-local upload without a token"
        )
    return {
        "mode": "cloud-dogfood",
        "status": "uploaded",
        "export": export_result,
        "doctor": doctor_result,
        "upload": post_upload_payload(
            payload=payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
        ),
    }


def _catch_up_upload(
    *,
    repo_path: Path,
    output_dir: Path,
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    limit: int,
    endpoint: str,
    token_env: str,
    yes: bool,
    timeout: float,
    include_git_ref: bool,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_EVENT_COUNT:
        raise CloudBundleError(f"--limit must be between 1 and {MAX_EVENT_COUNT}")
    repo_path = repo_path.expanduser().resolve()
    detected_repo_slug = repo_slug or _detect_repo_slug(repo_path)
    if not detected_repo_slug:
        raise CloudBundleError(
            "unable to detect repo slug; pass --repo-slug OWNER/REPO"
        )
    resolved_team_id = team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "")
    resolved_install_id = install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "")
    runs = _run_gh_run_list(
        repo_slug=detected_repo_slug,
        limit=limit,
        repo_path=repo_path,
    )
    events = [
        _build_workflow_run_event(
            repo_slug=detected_repo_slug,
            team_id=resolved_team_id,
            install_id=resolved_install_id,
            source=source,
            run=run,
            include_git_ref=include_git_ref,
        )
        for run in runs
    ]
    export_result = build_cloud_bundle(
        reports=[],
        events=events,
        output_dir=output_dir,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        anonymous=False,
    )
    doctor_result = run_cloud_doctor(
        bundle_dir=output_dir,
        endpoint=endpoint,
        token_env=token_env,
        require_token=yes,
    )
    if doctor_result["failures"]:
        return {
            "mode": "cloud-catch-up",
            "status": "doctor_failed",
            "repo_slug": detected_repo_slug,
            "run_count": len(runs),
            "export": export_result,
            "doctor": doctor_result,
        }
    payload = build_upload_payload(bundle_dir=output_dir, include_reports=False)
    if not yes:
        return {
            "mode": "cloud-catch-up",
            "status": "dry_run",
            "repo_slug": detected_repo_slug,
            "run_count": len(runs),
            "export": export_result,
            "doctor": doctor_result,
            "upload": build_dogfood_dry_run_preview(endpoint=endpoint, payload=payload),
        }
    token = os.environ.get(token_env, "")
    if not token and not _is_local_http_endpoint(endpoint):
        raise CloudBundleError(
            f"{token_env} is not set; refusing non-local upload without a token"
        )
    return {
        "mode": "cloud-catch-up",
        "status": "uploaded",
        "repo_slug": detected_repo_slug,
        "run_count": len(runs),
        "export": export_result,
        "doctor": doctor_result,
        "upload": post_upload_payload(
            payload=payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
        ),
    }


def _reviewer_runs_upload(
    *,
    repo_path: Path,
    verdicts: Path,
    output_dir: Path,
    repo_slug: str,
    team_id: str,
    install_id: str,
    limit: int,
    endpoint: str,
    token_env: str,
    yes: bool,
    timeout: float,
    include_git_ref: bool,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_EVENT_COUNT:
        raise CloudBundleError(f"--limit must be between 1 and {MAX_EVENT_COUNT}")
    repo_path = repo_path.expanduser().resolve()
    detected_repo_slug = repo_slug or _detect_repo_slug(repo_path)
    if not detected_repo_slug:
        raise CloudBundleError(
            "unable to detect repo slug; pass --repo-slug OWNER/REPO"
        )
    resolved_team_id = team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "")
    resolved_install_id = install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "")
    try:
        events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(
            verdicts,
            repo=detected_repo_slug,
            limit=limit,
            include_git_ref=include_git_ref,
        )
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc
    if not events:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "no_events",
            "repo_slug": detected_repo_slug,
            "event_count": 0,
            "verdicts": str(verdicts.expanduser()),
            "git_ref_included": include_git_ref,
        }
    export_result = build_cloud_bundle(
        reports=[],
        events=events,
        output_dir=output_dir,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        anonymous=False,
    )
    doctor_result = run_cloud_doctor(
        bundle_dir=output_dir,
        endpoint=endpoint,
        token_env=token_env,
        require_token=yes,
    )
    if doctor_result["failures"]:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "doctor_failed",
            "repo_slug": detected_repo_slug,
            "event_count": len(events),
            "export": export_result,
            "doctor": doctor_result,
        }
    payload = build_upload_payload(bundle_dir=output_dir, include_reports=False)
    if not yes:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "dry_run",
            "repo_slug": detected_repo_slug,
            "event_count": len(events),
            "export": export_result,
            "doctor": doctor_result,
            "upload": build_dogfood_dry_run_preview(endpoint=endpoint, payload=payload),
        }
    token = os.environ.get(token_env, "")
    if not token and not _is_local_http_endpoint(endpoint):
        raise CloudBundleError(
            f"{token_env} is not set; refusing non-local upload without a token"
        )
    return {
        "mode": "cloud-reviewer-runs",
        "status": "uploaded",
        "repo_slug": detected_repo_slug,
        "event_count": len(events),
        "export": export_result,
        "doctor": doctor_result,
        "upload": post_upload_payload(
            payload=payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
        ),
    }


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
    except CloudBundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled cloud command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
