"""Calibration command execution orchestration."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .commands import (
    command_lane_id,
    command_metadata_for_run,
    local_llm_profiles_from_command,
    materialize_command,
    resolve_path_for_cwd,
    reviewer_id_from_command,
    summary_path_for_command,
    text_from_timeout_stream,
)
from .context_inputs import context_pack_file_for_command
from .corpus import load_json_object
from .identity import safe_slug
from .planning import build_pilot_plan
from .results import infra_run_record, run_records_from_summary
from .run_results import CALIBRATION_RUN_RESULTS_MODE, CALIBRATION_RUN_RESULTS_SCHEMA


def utc_now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_code_mower_command() -> list[str]:
    module_path = Path(__file__).resolve()
    package_root = module_path.parent.parent
    legacy_cli = package_root / "code_mower_cli.py"
    if legacy_cli.exists():
        return [sys.executable, str(legacy_cli)]
    packaged_cli = package_root / "cli.py"
    if packaged_cli.exists():  # pragma: no cover - exercised after package extraction.
        return [sys.executable, "-m", "code_mower.cli"]
    return ["code-mower"]


def load_summary(path: Path | None) -> Mapping[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return load_json_object(path)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def result_command_dir(results_dir: Path, run_id: str, command_index: int, lane_id: str) -> Path:
    return results_dir / safe_slug(run_id, "run") / f"{command_index:02d}-{safe_slug(lane_id, 'lane')}"


def run_calibration_commands(
    corpus: Mapping[str, Any],
    *,
    replicates: int = 1,
    output_dir: Path = Path(".code-mower/calibration"),
    results_dir: Path = Path(".code-mower/calibration-results"),
    lanes: Sequence[str] = (),
    arms: Sequence[str] = (),
    jobs: int = 1,
    code_mower_command: Sequence[str] | None = None,
    repo_path_map: Mapping[str, str] | None = None,
    context_pack_manifest: Mapping[str, Any] | None = None,
    context_pack_output_dir: Path = Path(".code-mower/calibration-context-packs"),
    require_context_pack_files: bool = False,
    allow_historical_head: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    timeout_seconds: int = 1800,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run selected calibration commands and persist raw output plus summaries."""

    selected_lanes = {lane.replace("_", "-") for lane in lanes if lane}
    selected_arms = {str(arm).strip() for arm in arms if str(arm).strip()}
    code_mower_command = tuple(code_mower_command or default_code_mower_command())
    repo_path_map = repo_path_map or {}
    plan = build_pilot_plan(
        corpus,
        replicates=replicates,
        output_dir=output_dir,
        jobs=jobs,
    )
    started_at = utc_now_iso()
    command_results: list[dict[str, Any]] = []
    reviewer_runs: list[dict[str, Any]] = []
    executed = 0
    skipped = 0
    prevalidated = 0

    for run in plan.get("runs", []) or []:
        if not isinstance(run, Mapping):
            continue
        arm_id = str(run.get("arm_id") or "")
        if selected_arms:
            if arm_id not in selected_arms:
                continue
        elif run.get("requires_explicit_arm"):
            continue
        item = {
            "repo": run.get("repo"),
            "pr_number": run.get("pr_number"),
            "head_sha": run.get("head_sha"),
            "base_ref": run.get("base_ref"),
        }
        for command in run.get("commands", []) or []:
            if not isinstance(command, list):
                continue
            lane_id = command_lane_id(command)
            if selected_lanes and lane_id not in selected_lanes:
                continue
            if limit is not None and prevalidated >= limit:
                continue
            materialize_command(
                command,
                item=item,
                code_mower_command=code_mower_command,
                repo_path_map=repo_path_map,
                allow_historical_head=allow_historical_head,
            )
            prevalidated += 1

    for run in plan.get("runs", []) or []:
        if not isinstance(run, Mapping):
            continue
        arm_id = str(run.get("arm_id") or "")
        if selected_arms:
            if arm_id not in selected_arms:
                continue
        elif run.get("requires_explicit_arm"):
            continue
        item = {
            "repo": run.get("repo"),
            "pr_number": run.get("pr_number"),
            "head_sha": run.get("head_sha"),
            "base_ref": run.get("base_ref"),
            "calibration_run_id": run.get("run_id"),
            "replicate": run.get("replicate"),
        }
        corpus_item = next(
            (
                candidate
                for candidate in corpus.get("corpus", []) or []
                if isinstance(candidate, Mapping)
                and candidate.get("repo") == run.get("repo")
                and candidate.get("pr_number") == run.get("pr_number")
                and candidate.get("head_sha", "") == run.get("head_sha", "")
            ),
            item,
        )
        item["context_packs"] = list(corpus_item.get("context_packs", []) or [])
        for command_index, command in enumerate(run.get("commands", []) or []):
            if not isinstance(command, list):
                continue
            lane_id = command_lane_id(command)
            if selected_lanes and lane_id not in selected_lanes:
                skipped += 1
                continue
            if limit is not None and executed >= limit:
                skipped += 1
                continue
            command_metadata = command_metadata_for_run(run, command_index)
            reviewer_id = str(
                command_metadata.get("reviewer_id")
                or reviewer_id_from_command(command)
            )
            materialized = materialize_command(
                command,
                item=item,
                code_mower_command=code_mower_command,
                repo_path_map=repo_path_map,
                allow_historical_head=allow_historical_head,
            )
            result_dir = result_command_dir(
                results_dir,
                str(run.get("run_id") or "run"),
                command_index,
                lane_id,
            )
            result_dir.mkdir(parents=True, exist_ok=True)
            context_pack_path = context_pack_file_for_command(
                item=item,
                lane_id=lane_id,
                result_dir=result_dir,
                repo_path_map=repo_path_map,
                context_pack_manifest=context_pack_manifest,
                context_pack_output_dir=context_pack_output_dir,
                require_context_pack_files=require_context_pack_files,
            )
            if context_pack_path is not None:
                materialized.extend(["--context-pack-file", str(context_pack_path)])
            planned_args = list(command)
            if context_pack_path is not None:
                planned_args.extend(["--context-pack-file", str(context_pack_path)])
            command_path = result_dir / "command.json"
            stdout_path = result_dir / "stdout.txt"
            stderr_path = result_dir / "stderr.txt"
            write_json(
                command_path,
                {
                    "args": materialized,
                    "planned_args": planned_args,
                    "run_id": run.get("run_id"),
                    "lane_id": lane_id,
                    "reviewer_id": reviewer_id,
                    "command_metadata": command_metadata,
                    "context_pack_file": str(context_pack_path) if context_pack_path else "",
                    "dry_run": dry_run,
                },
            )
            started = time.monotonic()
            summary_path = summary_path_for_command(command)
            resolved_summary_path = resolve_path_for_cwd(summary_path, cwd)
            if not dry_run and resolved_summary_path is not None and resolved_summary_path.exists():
                resolved_summary_path.unlink()
            if dry_run:
                returncode = None
                stdout = ""
                stderr = ""
                duration_seconds = 0.0
            else:
                try:
                    completed = subprocess.run(
                        materialized,
                        capture_output=True,
                        text=True,
                        check=False,
                        cwd=str(cwd) if cwd is not None else None,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    returncode = None
                    stdout = text_from_timeout_stream(exc.stdout)
                    stderr = text_from_timeout_stream(exc.stderr)
                    duration_seconds = time.monotonic() - started
                    command_status = "timeout"
                except OSError as exc:
                    returncode = None
                    stdout = ""
                    stderr = f"{type(exc).__name__}: {exc}"
                    duration_seconds = time.monotonic() - started
                    command_status = "launch_failed"
                else:
                    returncode = completed.returncode
                    stdout = completed.stdout
                    stderr = completed.stderr
                    duration_seconds = time.monotonic() - started
                    command_status = "finished"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            summary_error = ""
            try:
                summary = None if dry_run else load_summary(resolved_summary_path)
            except ValueError as exc:
                summary = None
                summary_error = str(exc)
            command_result: dict[str, Any] = {
                "run_id": run.get("run_id"),
                "repo": run.get("repo"),
                "pr_number": run.get("pr_number"),
                "head_sha": run.get("head_sha"),
                "arm_id": run.get("arm_id"),
                "replicate": run.get("replicate"),
                "command_index": command_index,
                "lane_id": lane_id,
                "reviewer_id": reviewer_id,
                "command_metadata": command_metadata,
                "status": "planned" if dry_run else command_status,
                "returncode": returncode,
                "duration_seconds": round(duration_seconds, 3),
                "command_path": str(command_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "summary_path": str(summary_path) if summary_path is not None else "",
                "summary_found": bool(summary),
            }
            if summary_error:
                command_result["summary_error"] = summary_error
            extracted: list[dict[str, Any]] = []
            if summary is not None:
                command_result["summary_mode"] = summary.get("mode")
                try:
                    extracted = run_records_from_summary(
                        summary=summary,
                        item=corpus_item,
                        command_result=command_result,
                    )
                except (TypeError, ValueError) as exc:
                    summary_error = str(exc)
                    command_result["summary_error"] = summary_error
                command_result["extracted_reviewer_runs"] = len(extracted)
                reviewer_runs.extend(extracted)
            if not dry_run and not extracted:
                infra_status = (
                    str(command_result["status"])
                    if command_result["status"] in {"timeout", "launch_failed"}
                    else "invalid_summary"
                    if summary is not None or summary_error
                    else "failed"
                    if returncode is not None and int(returncode) != 0
                    else "missing_summary"
                )
                infra_item = {**dict(corpus_item), **item}
                infra_reviewers = (
                    local_llm_profiles_from_command(materialized)
                    if lane_id == "local-llm"
                    else []
                ) or [reviewer_id]
                reviewer_runs.extend(
                    infra_run_record(
                        lane_id=reviewer,
                        item=infra_item,
                        status=infra_status,
                        duration_seconds=duration_seconds,
                        artifact=str(result_dir / "result.json"),
                    )
                    for reviewer in infra_reviewers
                )
            write_json(result_dir / "result.json", command_result)
            command_results.append(command_result)
            executed += 1

    payload = {
        "mode": CALIBRATION_RUN_RESULTS_MODE,
        "schema": CALIBRATION_RUN_RESULTS_SCHEMA,
        "run_results_id": uuid.uuid4().hex,
        "corpus_name": corpus.get("name", ""),
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "replicates": replicates,
        "output_dir": str(output_dir),
        "results_dir": str(results_dir),
        "context_pack_manifest": bool(context_pack_manifest),
        "context_pack_output_dir": str(context_pack_output_dir),
        "selected_lanes": sorted(selected_lanes),
        "selected_arms": sorted(selected_arms),
        "dry_run": dry_run,
        "command_count": len(command_results),
        "skipped_command_count": skipped,
        "commands": command_results,
        "reviewer_runs": reviewer_runs,
    }
    write_json(results_dir / "calibration-run-results.json", payload)
    return payload
