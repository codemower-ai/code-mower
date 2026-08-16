"""Reviewer spend ledger helpers.

The ledger is append-only local metadata. It intentionally records model,
timing, token, cost, PR, and SHA fields without source, diffs, prompts,
transcripts, stdout/stderr, or issue body text.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping


SPEND_SCHEMA = "code_mower.reviewerSpend.v1"
EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
DEFAULT_SPEND_PATH = Path(".code-mower/reviewer-spend.json")
TOKEN_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_tokens",
}
COST_KEYS = {"cost_usd", "total_cost_usd", "usd"}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def load_spend_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SPEND_SCHEMA, "profiles": {}, "runs": []}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read reviewer spend file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("reviewer spend file must contain a JSON object")
    return payload


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        text = value.strip().replace("$", "")
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _walk_json_numbers(value: Any) -> Iterable[tuple[str, float | int]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = _normalize_metric_key(str(key))
            number = _number(item)
            if number is not None:
                yield normalized_key, number
            yield from _walk_json_numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_numbers(item)


def _normalize_metric_key(key: str) -> str:
    text = key.strip().replace("-", "_").replace(" ", "_")
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", text)
    return re.sub(r"_+", "_", text).lower()


def _json_payloads(text: str) -> Iterable[Any]:
    stripped = text.strip()
    if not stripped:
        return
    if stripped.startswith(("{", "[")):
        try:
            yield json.loads(stripped)
            return
        except json.JSONDecodeError:
            pass
    for line in text.splitlines():
        stripped_line = line.strip()
        if not stripped_line.startswith(("{", "[")):
            continue
        try:
            yield json.loads(stripped_line)
        except json.JSONDecodeError:
            continue


def extract_usage_metrics(*texts: str) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for text in texts:
        for parsed in _json_payloads(text):
            for key, number in _walk_json_numbers(parsed):
                if key in TOKEN_KEYS:
                    metrics[key] = int(number)
                elif key in COST_KEYS:
                    metrics["cost_usd"] = float(number)
    return metrics


def model_from_env(names: Iterable[str], default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def build_spend_run(
    *,
    lane: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    model: str,
    wall_seconds: float,
    verdict: str,
    usage: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    usage = dict(usage or {})
    cost = _number(usage.get("cost_usd"))
    run = {
        "run_id": str(uuid.uuid4()),
        "created_at": created_at or utc_now(),
        "lane": lane,
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": head_sha,
        "model": model,
        "wall_seconds": round(float(wall_seconds), 3),
        "verdict": verdict,
    }
    if cost is not None:
        run["cost_usd"] = round(float(cost), 6)
    for key in sorted(TOKEN_KEYS):
        number = _number(usage.get(key))
        if number is not None:
            run[key] = int(number)
    return run


def append_spend_run(path: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    destination = path.expanduser()
    payload = load_spend_file(destination)
    payload.setdefault("schema", SPEND_SCHEMA)
    profiles = payload.get("profiles")
    if profiles is not None and not isinstance(profiles, Mapping):
        raise ValueError("reviewer spend profiles must be an object")
    runs = payload.get("runs", [])
    if not isinstance(runs, list):
        raise ValueError("reviewer spend runs must be a list")
    payload["runs"] = [*runs, dict(run)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(f".{destination.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(destination)
    return payload


def provider_from_lane(lane: str) -> str:
    if lane.startswith("claude"):
        return "claude"
    if lane.startswith("codex"):
        return "codex"
    return lane.split("-", 1)[0]


def spend_runs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    runs = payload.get("runs", [])
    if runs is None:
        return []
    if not isinstance(runs, list):
        raise ValueError("reviewer spend runs must be a list")
    normalized: list[dict[str, Any]] = []
    for item in runs:
        if not isinstance(item, Mapping):
            continue
        normalized.append(dict(item))
    return normalized


def spend_runs_to_events(
    payload: Mapping[str, Any],
    *,
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
    source: str = "reviewer-spend",
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run in spend_runs(payload):
        lane = str(run.get("lane") or "").strip()
        if not lane:
            continue
        provider = provider_from_lane(lane)
        metrics: dict[str, Any] = {}
        for key in ("wall_seconds", "cost_usd", *sorted(TOKEN_KEYS)):
            if key in run:
                metrics[key] = run[key]
        model = str(run.get("model") or "")
        event = {
            "schema": EVENT_SCHEMA,
            "event_id": str(run.get("run_id") or uuid.uuid4()),
            "event_type": "reviewer_run",
            "created_at": str(run.get("created_at") or utc_now()),
            "repo_slug": str(run.get("repo") or repo_slug),
            "team_id": team_id,
            "install_id": install_id,
            "source": source,
            "provider": provider,
            "lens": lane,
            "status": str(run.get("verdict") or "").lower(),
            "tool": {
                "role": "reviewer",
                "tool_name": provider,
                "provider": provider,
                "model": model,
                "model_source": "env" if model else "missing",
                "version_source": "not_probed",
                "integration": "cli",
                "lens": lane,
            },
            "metrics": metrics,
            "dimensions": {
                "lane": lane,
                "pr_number": str(run.get("pr_number") or ""),
                "head_sha": str(run.get("head_sha") or ""),
                "spend_run_id": str(run.get("run_id") or ""),
            },
        }
        events.append(event)
    return events
