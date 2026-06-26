"""Structured cloud event and repository metadata helpers."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from code_mower import __version__
from code_mower.provider_registry import REFERENCE_PROVIDERS
from code_mower.providers import (
    build_code_mower_tool_provenance,
    build_provider_lane_tool_provenance,
    normalize_tool_provenance,
)

from .bundle import MAX_EVENT_COUNT, SAFE_EVENT_TYPES, SAFE_REPORT_KINDS, validate_metadata_payload
from .dogfood import DogfoodPlan
from .errors import CloudBundleError
from .git_metadata import run_git


EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
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


def safe_kind(value: str) -> str:
    kind = value.strip()
    if kind not in SAFE_REPORT_KINDS:
        allowed = ", ".join(sorted(SAFE_REPORT_KINDS))
        raise CloudBundleError(f"unsupported report kind {value!r}; allowed: {allowed}")
    return kind


def safe_event_type(value: str) -> str:
    event_type = value.strip()
    if event_type not in SAFE_EVENT_TYPES:
        allowed = ", ".join(sorted(SAFE_EVENT_TYPES))
        raise CloudBundleError(
            f"unsupported event type {value!r}; allowed: {allowed}"
        )
    return event_type


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def build_dogfood_event(
    *,
    repo_path: Path,
    plan: DogfoodPlan,
) -> dict[str, Any]:
    status = run_git(repo_path, ["status", "--porcelain"])
    return {
        "schema": EVENT_SCHEMA,
        "event_id": str(uuid.uuid4()),
        "event_type": "dogfood_upload",
        "created_at": utc_now(),
        "repo_slug": plan.repo_slug,
        "team_id": plan.team_id,
        "install_id": plan.install_id,
        "source": plan.source,
        "provider": "",
        "lens": "",
        "status": "observed",
        "tool": build_code_mower_tool_provenance(
            source=plan.source,
            version=__version__,
            role="reporter",
        ),
        "git": {
            "sha": run_git(repo_path, ["rev-parse", "HEAD"]),
            "branch": run_git(repo_path, ["branch", "--show-current"]),
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


def event_id_from_github_run(repo_slug: str, run: dict[str, Any]) -> str:
    database_id = str(run.get("databaseId") or "").strip()
    if database_id:
        run_url = f"https://github.com/{repo_slug}/actions/runs/{database_id}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, run_url))
    run_url = str(run.get("url") or "").strip()
    if run_url:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, run_url))
    return str(uuid.uuid4())


def run_gh_run_list(
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


def build_workflow_run_event(
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
        "event_id": event_id_from_github_run(repo_slug, run),
        "event_type": "workflow_run",
        "created_at": str(run.get("createdAt") or utc_now()),
        "repo_slug": repo_slug,
        "team_id": team_id,
        "install_id": install_id,
        "source": source,
        "provider": "",
        "lens": "base",
        "status": status,
        "tool": normalize_tool_provenance(
            {
                "role": "workflow",
                "tool_name": "github-actions",
                "provider": "github",
                "integration": "github-actions",
                "lens": "base",
                "source": source,
            }
        ),
        "metrics": {},
        "dimensions": dimensions,
    }
    return normalize_event(event, "workflow_run")


def build_provider_catalog_snapshot_events(
    *,
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    include_version_probe: bool = True,
) -> list[dict[str, Any]]:
    """Build metadata-only events describing configured provider lanes.

    Catalog snapshots are coverage evidence, not reviewer-quality evidence.
    They let CodeMower.com report which configured AI tools have exact
    tool/model/version metadata and which still need adapter work.
    """

    events: list[dict[str, Any]] = []
    for lane_id, lane in sorted(REFERENCE_PROVIDERS.items()):
        tool, detail = build_provider_lane_tool_provenance(
            lane_id,
            lane,
            source=source,
            include_version_probe=include_version_probe,
        )
        event = {
            "schema": EVENT_SCHEMA,
            "event_id": str(uuid.uuid4()),
            "event_type": "provider_catalog_snapshot",
            "created_at": utc_now(),
            "repo_slug": repo_slug,
            "team_id": team_id,
            "install_id": install_id,
            "source": source,
            "provider": tool.get("provider", ""),
            "lens": tool.get("lens", ""),
            "status": "observed",
            "tool": tool,
            "metrics": {},
            "dimensions": {
                "lane_id": lane_id,
                "lane_type": lane.lane_type,
                "driver": lane.driver,
                "informational": lane.informational,
                "merge_authority": lane.merge_authority,
                "enabled_by_default": lane.enabled_by_default,
                "trigger_policy": lane.trigger_policy,
                "spend_policy": lane.spend_policy,
                "model_known": bool(tool.get("model")),
                "version_known": bool(tool.get("tool_version")),
                "catalog_snapshot": True,
                **detail,
            },
        }
        events.append(normalize_event(event, "provider_catalog_snapshot"))
    return events


def normalize_event(value: dict[str, Any], event_type: str) -> dict[str, Any]:
    validate_metadata_payload(value)
    normalized = dict(value)
    normalized["schema"] = str(normalized.get("schema") or EVENT_SCHEMA)
    if normalized["schema"] != EVENT_SCHEMA:
        raise CloudBundleError(
            f"unsupported event schema {normalized['schema']!r}; expected {EVENT_SCHEMA}"
        )
    normalized["event_type"] = safe_event_type(
        str(normalized.get("event_type") or event_type)
    )
    normalized["event_id"] = str(normalized.get("event_id") or uuid.uuid4())
    normalized["created_at"] = str(normalized.get("created_at") or utc_now())
    for key in (
        "repo_slug",
        "team_id",
        "install_id",
        "source",
        "provider",
        "lens",
        "status",
    ):
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
    if normalized.get("tool") is None and normalized["event_type"] in {
        "lane_policy_snapshot",
        "value_report_snapshot",
    }:
        normalized["tool"] = build_code_mower_tool_provenance(
            source=normalized.get("source") or f"code-mower {normalized['event_type']}",
            version=__version__,
            role="reporter",
        )
    tool = normalize_tool_provenance(normalized.get("tool"), event=normalized)
    normalized["tool"] = tool
    if not normalized["provider"] and tool.get("provider"):
        normalized["provider"] = tool["provider"]
    if not normalized["lens"] and tool.get("lens"):
        normalized["lens"] = tool["lens"]
    validate_metadata_payload(normalized)
    return normalized


def load_event_file(path: Path, event_type: str) -> list[dict[str, Any]]:
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
        raise CloudBundleError(
            f"event file must contain an object, array, or JSONL: {source}"
        )
    events: list[dict[str, Any]] = []
    for item in parsed_events:
        if not isinstance(item, dict):
            raise CloudBundleError(
                f"event file contains a non-object event: {source}"
            )
        events.append(normalize_event(item, event_type))
    return events


def parse_event_args(values: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in values:
        if "=" not in raw:
            raise CloudBundleError(
                "--event entries must use EVENT_TYPE=PATH, for example reviewer_run=run.json"
            )
        event_type, path_text = raw.split("=", 1)
        events.extend(load_event_file(Path(path_text), safe_event_type(event_type)))
    if len(events) > MAX_EVENT_COUNT:
        raise CloudBundleError(
            f"too many events: {len(events)}; max {MAX_EVENT_COUNT}"
        )
    return events
