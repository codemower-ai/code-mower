#!/usr/bin/env python3
"""Plan and report Code Mower builder-side calibration experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

BUILDER_PLAN_MODE = "code-mower-builder-experiment-plan"
BUILDER_PLAN_SCHEMA = "code_mower.builderExperimentPlan.v1"
BUILDER_REPORT_MODE = "code-mower-builder-experiment-report"
BUILDER_REPORT_SCHEMA = "code_mower.builderExperimentReport.v1"
SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
DEFAULT_METRICS = (
    "elapsed_seconds",
    "cost_usd",
    "user_interventions",
    "audit_blockers",
    "resolved_blockers",
    "tests_passed",
    "post_merge_health",
)


def _safe_slug(value: Any, fallback: str = "item") -> str:
    text = SAFE_SLUG_RE.sub("-", str(value or "").strip()).strip("._-")
    while ".." in text:
        text = text.replace("..", ".")
    return text or fallback


def _stable_suffix(value: Any, length: int = 8) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _identity_slug(value: Any, fallback: str) -> str:
    return f"{_safe_slug(value, fallback)}-{_stable_suffix(value)}"


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if str(item).strip()]


def normalize_spec(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    if payload.get("version", 1) not in {1, "1"}:
        raise ValueError("builder experiment spec version must be 1")
    tasks = payload.get("tasks")
    builders = payload.get("builders")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("builder experiment spec must include a non-empty tasks list")
    if not isinstance(builders, list) or not builders:
        raise ValueError("builder experiment spec must include a non-empty builders list")

    normalized_tasks: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            raise ValueError(f"tasks[{index}] must be a JSON object")
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id:
            raise ValueError(f"tasks[{index}].task_id is required")
        if task_id in seen_tasks:
            raise ValueError(f"duplicate task_id: {task_id}")
        seen_tasks.add(task_id)
        repo = str(task.get("repo") or "").strip()
        if "/" not in repo:
            raise ValueError(f"tasks[{index}].repo must be an owner/repo slug")
        normalized_tasks.append(
            {
                "task_id": task_id,
                "repo": repo,
                "base_ref": str(task.get("base_ref") or "origin/main"),
                "task_class": str(task.get("task_class") or "general"),
                "prompt": str(task.get("prompt") or ""),
                "success_criteria": _str_list(task.get("success_criteria")),
                "context_packs": _str_list(task.get("context_packs")),
                "review_classes": _str_list(task.get("review_classes")),
                "notes": str(task.get("notes") or ""),
            }
        )

    normalized_builders: list[dict[str, Any]] = []
    seen_builders: set[str] = set()
    for index, builder in enumerate(builders):
        if not isinstance(builder, Mapping):
            raise ValueError(f"builders[{index}] must be a JSON object")
        builder_id = str(builder.get("builder_id") or builder.get("id") or "").strip()
        if not builder_id:
            raise ValueError(f"builders[{index}].builder_id is required")
        if builder_id in seen_builders:
            raise ValueError(f"duplicate builder_id: {builder_id}")
        seen_builders.add(builder_id)
        normalized_builders.append(
            {
                "builder_id": builder_id,
                "provider": str(builder.get("provider") or "unknown"),
                "tool": str(builder.get("tool") or ""),
                "model": str(builder.get("model") or ""),
                "prompt_lenses": _str_list(builder.get("prompt_lenses")),
                "context_packs": _str_list(builder.get("context_packs")),
                "command_template": _str_list(builder.get("command_template")),
                "cost_policy": str(builder.get("cost_policy") or "unknown"),
                "notes": str(builder.get("notes") or ""),
            }
        )

    return {
        "version": 1,
        "name": str(payload.get("name") or ""),
        "description": str(payload.get("description") or ""),
        "tasks": normalized_tasks,
        "builders": normalized_builders,
        "metrics": _str_list(payload.get("metrics")) or list(DEFAULT_METRICS),
        "review_protocol": dict(payload.get("review_protocol") or {}),
    }


def load_spec(path: Path) -> dict[str, Any]:
    payload = dict(_load_json(path))
    normalized = normalize_spec(payload)
    if not str(payload.get("name") or "").strip():
        normalized["name"] = path.stem
    return normalized


def _experiment_id(spec: Mapping[str, Any]) -> str:
    raw = "\n".join(
        [
            str(spec.get("name") or ""),
            json.dumps(spec.get("tasks", []), sort_keys=True),
            json.dumps(spec.get("builders", []), sort_keys=True),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{_safe_slug(spec.get('name'), 'builder-experiment')}-{digest}"


def _run_id(
    experiment_id: str,
    task_id: str,
    builder_id: str,
    replicate: int,
) -> str:
    return (
        f"{_safe_slug(experiment_id, 'experiment')}-"
        f"{_identity_slug(task_id, 'task')}-"
        f"{_identity_slug(builder_id, 'builder')}-r{replicate}"
    )


def build_plan(
    spec: Mapping[str, Any],
    *,
    output_dir: Path = Path(".code-mower/builder-experiments"),
    replicates: int = 1,
) -> dict[str, Any]:
    spec = normalize_spec(spec)
    if replicates <= 0:
        raise ValueError("replicates must be greater than zero")
    experiment_id = _experiment_id(spec)
    runs: list[dict[str, Any]] = []
    metrics = _str_list(spec.get("metrics")) or list(DEFAULT_METRICS)
    review_protocol = dict(spec.get("review_protocol") or {})

    for task in spec.get("tasks", []) or []:
        if not isinstance(task, Mapping):
            continue
        for builder in spec.get("builders", []) or []:
            if not isinstance(builder, Mapping):
                continue
            for replicate in range(1, replicates + 1):
                task_id = str(task["task_id"])
                builder_id = str(builder["builder_id"])
                run_id = _run_id(experiment_id, task_id, builder_id, replicate)
                run_dir = (
                    output_dir
                    / _safe_slug(experiment_id, "experiment")
                    / _identity_slug(task_id, "task")
                    / _identity_slug(builder_id, "builder")
                    / f"r{replicate}"
                )
                runs.append(
                    {
                        "run_id": run_id,
                        "experiment_id": experiment_id,
                        "task_id": task_id,
                        "task_class": task.get("task_class", "general"),
                        "repo": task["repo"],
                        "base_ref": task.get("base_ref", "origin/main"),
                        "builder_id": builder_id,
                        "provider": builder.get("provider", ""),
                        "tool": builder.get("tool", ""),
                        "model": builder.get("model", ""),
                        "prompt_lenses": list(builder.get("prompt_lenses", [])),
                        "context_packs": sorted(
                            set(task.get("context_packs", []))
                            | set(builder.get("context_packs", []))
                        ),
                        "success_criteria": list(task.get("success_criteria", [])),
                        "review_classes": list(task.get("review_classes", [])),
                        "command_template": list(builder.get("command_template", [])),
                        "output_dir": str(run_dir),
                        "replicate": replicate,
                        "metrics_required": metrics,
                        "blind_review_required": True,
                        "merge_authority": False,
                        "review_protocol": review_protocol
                        or {
                            "required_before_merge": [
                                "structured audits pass",
                                "Code Mower merge bar is clean",
                                "post-merge health is verified",
                            ],
                        },
                    }
                )

    return {
        "mode": BUILDER_PLAN_MODE,
        "schema": BUILDER_PLAN_SCHEMA,
        "experiment_id": experiment_id,
        "name": str(spec.get("name") or ""),
        "description": str(spec.get("description") or ""),
        "replicates": replicates,
        "output_dir": str(output_dir),
        "task_count": len(spec.get("tasks", []) or []),
        "builder_count": len(spec.get("builders", []) or []),
        "run_count": len(runs),
        "tasks": list(spec.get("tasks", []) or []),
        "builders": list(spec.get("builders", []) or []),
        "runs": runs,
        "guardrails": [
            "Use a fresh worktree for every builder run.",
            "Keep reviewer output hidden until the builder declares the run complete.",
            "Record user interventions and audit blocker iterations explicitly.",
            "Do not treat builder experiments as merge authority by themselves.",
            "Only count a delivery as verified after merge and post-merge health checks.",
        ],
    }


def load_run_results(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        payload = _load_json(path)
        raw_runs = payload.get("runs", payload.get("run_results"))
        if raw_runs is None and payload.get("run_id"):
            raw_runs = [payload]
        if not isinstance(raw_runs, list):
            raise ValueError(f"{path} must include a runs list or one run_id object")
        for index, run in enumerate(raw_runs):
            if not isinstance(run, Mapping):
                raise ValueError(f"{path}: runs[{index}] must be a JSON object")
            records.append(dict(run))
    return records


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _status_category(value: Any) -> str:
    status = str(value or "").strip().lower().replace("-", "_")
    if status in {"merged", "verified", "success", "succeeded"}:
        return "verified" if status == "verified" else "merged"
    if status in {"blocked", "failed", "failure", "abandoned"}:
        return "failed"
    if status in {"running", "started", "in_progress"}:
        return "running"
    if status in {"planned", "pending"}:
        return "planned"
    return status or "unknown"


def build_report(
    plan: Mapping[str, Any],
    run_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    planned_runs = {
        str(run.get("run_id")): run
        for run in plan.get("runs", []) or []
        if isinstance(run, Mapping) and run.get("run_id")
    }
    result_by_run_id: dict[str, Mapping[str, Any]] = {}
    for result in run_results:
        run_id = str(result.get("run_id") or "").strip()
        if run_id:
            result_by_run_id[run_id] = result
    unmatched_result_run_ids = sorted(set(result_by_run_id) - set(planned_runs))

    builder_stats: dict[str, dict[str, Any]] = {}
    run_rows: list[dict[str, Any]] = []
    matched_reported_run_count = 0
    for run_id, planned in planned_runs.items():
        result = result_by_run_id.get(run_id, {})
        builder_id = str(planned.get("builder_id") or "unknown-builder")
        stats = builder_stats.setdefault(
            builder_id,
            {
                "builder_id": builder_id,
                "planned_runs": 0,
                "reported_runs": 0,
                "merged_runs": 0,
                "verified_runs": 0,
                "failed_runs": 0,
                "running_runs": 0,
                "total_elapsed_seconds": 0.0,
                "total_cost_usd": 0.0,
                "total_user_interventions": 0,
                "total_audit_blockers": 0,
                "total_resolved_blockers": 0,
            },
        )
        stats["planned_runs"] += 1
        status = _status_category(result.get("status") or result.get("merge_result"))
        if result:
            matched_reported_run_count += 1
            stats["reported_runs"] += 1
            stats["total_elapsed_seconds"] += _float(result.get("elapsed_seconds"))
            stats["total_cost_usd"] += _float(result.get("cost_usd"))
            stats["total_user_interventions"] += _int(result.get("user_interventions"))
            stats["total_audit_blockers"] += _int(result.get("audit_blockers"))
            stats["total_resolved_blockers"] += _int(result.get("resolved_blockers"))
        post_merge_health = str(result.get("post_merge_health") or "").strip().lower()
        if status in {"merged", "verified"}:
            stats["merged_runs"] += 1
        if status == "failed":
            stats["failed_runs"] += 1
        elif status == "running":
            stats["running_runs"] += 1
        elif status == "verified" or (
            status == "merged" and post_merge_health == "verified"
        ):
            stats["verified_runs"] += 1
        run_rows.append(
            {
                "run_id": run_id,
                "task_id": planned.get("task_id"),
                "builder_id": builder_id,
                "status": status,
                "reported": bool(result),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "cost_usd": result.get("cost_usd"),
                "user_interventions": result.get("user_interventions"),
                "audit_blockers": result.get("audit_blockers"),
                "resolved_blockers": result.get("resolved_blockers"),
                "post_merge_health": result.get("post_merge_health", ""),
            }
        )

    for stats in builder_stats.values():
        reported = int(stats["reported_runs"])
        verified = int(stats["verified_runs"])
        stats["avg_elapsed_seconds"] = (
            round(float(stats["total_elapsed_seconds"]) / reported, 3)
            if reported
            else None
        )
        stats["verified_rate"] = (
            round(verified / int(stats["reported_runs"]), 4)
            if int(stats["reported_runs"])
            else None
        )
        stats["cost_per_verified_run"] = (
            round(float(stats["total_cost_usd"]) / verified, 4) if verified else None
        )
        stats["total_elapsed_seconds"] = round(float(stats["total_elapsed_seconds"]), 3)
        stats["total_cost_usd"] = round(float(stats["total_cost_usd"]), 4)

    return {
        "mode": BUILDER_REPORT_MODE,
        "schema": BUILDER_REPORT_SCHEMA,
        "report_id": uuid.uuid4().hex,
        "experiment_id": plan.get("experiment_id", ""),
        "name": plan.get("name", ""),
        "planned_run_count": len(planned_runs),
        "reported_run_count": matched_reported_run_count,
        "unmatched_reported_run_count": len(unmatched_result_run_ids),
        "unmatched_result_run_ids": unmatched_result_run_ids,
        "builders": dict(sorted(builder_stats.items())),
        "runs": run_rows,
        "caveat": (
            "Builder experiments measure delivery loops on a real codebase. "
            "Treat early results as directional until each task has matched "
            "clean worktrees, hidden reviewer output, and verified post-merge health."
        ),
    }


def render_plan_text(plan: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower builder experiment plan",
        f"Experiment: {plan.get('experiment_id', '')}",
        f"Tasks: {plan.get('task_count', 0)}",
        f"Builders: {plan.get('builder_count', 0)}",
        f"Runs: {plan.get('run_count', 0)}",
        "",
        "First runs:",
    ]
    for run in (plan.get("runs", []) or [])[:5]:
        if isinstance(run, Mapping):
            lines.append(
                f"- {run.get('run_id')}: {run.get('builder_id')} "
                f"on {run.get('repo')} task={run.get('task_id')}"
            )
    if not plan.get("runs"):
        lines.append("- none")
    lines.extend(["", "Guardrails:"])
    lines.extend(f"- {item}" for item in plan.get("guardrails", []) or [])
    return "\n".join(lines) + "\n"


def render_report_text(report: Mapping[str, Any]) -> str:
    lines = [
        "# Code Mower Builder Experiment Report",
        "",
        f"Experiment: `{report.get('experiment_id', '')}`",
        f"Planned runs: {report.get('planned_run_count', 0)}",
        f"Reported runs: {report.get('reported_run_count', 0)}",
        "",
        "| Builder | Planned | Reported | Merged | Verified | Failed | User interventions | Audit blockers | Cost | Avg sec | Cost/verified |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    builders = report.get("builders", {})
    if isinstance(builders, Mapping):
        for builder_id, stats in sorted(builders.items()):
            if not isinstance(stats, Mapping):
                continue
            lines.append(
                f"| `{builder_id}` | {stats.get('planned_runs', 0)} | "
                f"{stats.get('reported_runs', 0)} | {stats.get('merged_runs', 0)} | "
                f"{stats.get('verified_runs', 0)} | {stats.get('failed_runs', 0)} | "
                f"{stats.get('total_user_interventions', 0)} | "
                f"{stats.get('total_audit_blockers', 0)} | "
                f"{stats.get('total_cost_usd', 0)} | "
                f"{stats.get('avg_elapsed_seconds', 0)} | "
                f"{stats.get('cost_per_verified_run')} |"
            )
    lines.extend(["", f"Caveat: {report.get('caveat', '')}"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower builder-experiment")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("spec", type=Path)
    plan_parser.add_argument("--output-dir", type=Path, default=Path(".code-mower/builder-experiments"))
    plan_parser.add_argument("--replicates", type=int, default=1)
    plan_parser.add_argument("--output", type=Path)
    plan_parser.add_argument("--json", action="store_true")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("spec", type=Path)
    report_parser.add_argument("--runs", type=Path, action="append", default=[])
    report_parser.add_argument("--output-dir", type=Path, default=Path(".code-mower/builder-experiments"))
    report_parser.add_argument("--replicates", type=int, default=1)
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        spec = load_spec(args.spec)
        plan = build_plan(spec, output_dir=args.output_dir, replicates=args.replicates)
        if args.command == "plan":
            if args.output:
                _write_json(args.output, plan)
            print(json.dumps(plan, indent=2, sort_keys=True) if args.json else render_plan_text(plan))
            return 0
        run_results = load_run_results(args.runs)
        report = build_report(plan, run_results)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.output.suffix.lower() == ".json":
                _write_json(args.output, report)
            else:
                args.output.write_text(render_report_text(report), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_report_text(report))
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
