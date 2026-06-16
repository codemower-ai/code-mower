#!/usr/bin/env python3
"""Plan and report Code Mower reviewer calibration pilots."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import (
            code_mower_context_packs,
            code_mower_telemetry,
        )
        from code_mower.calibration import (
            RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
            RUN_STATUS_BLOCKED,
            RUN_STATUS_CATEGORY_ALIASES,
            RUN_STATUS_INFRA_ERROR,
            RUN_STATUS_PASS,
            RUN_STATUS_UNKNOWN,
            TRUTH_EXPECTATION_ALIASES,
            TRUTH_EXPECTATION_KNOWN_BLOCKED,
            TRUTH_EXPECTATION_KNOWN_CLEAN,
            TRUTH_EXPECTATION_UNKNOWN,
            AUDIT_INPUT_INSUFFICIENT_PATTERNS,
            CALIBRATION_RUN_RESULTS_MODE,
            CALIBRATION_RUN_RESULTS_SCHEMA,
            build_pilot_plan,
            command_lane_id as _command_lane_id,
            command_metadata_for_run as _command_metadata_for_run,
            default_arms,
            build_overlap_report,
            build_lane_policy_report,
            build_reviewer_evidence_report,
            build_value_report,
            audit_input_insufficient_result as _audit_input_insufficient_result,
            coderabbit_blocking_findings as _coderabbit_blocking_findings,
            corpus_with_run_results,
            expected_finding_matches as _expected_finding_matches,
            infra_run_record as _infra_run_record,
            load_corpus,
            load_json_object as _load_json,
            load_run_results as _load_run_results,
            local_llm_profiles_from_command as _local_llm_profiles_from_command,
            local_llm_findings as _local_llm_findings,
            materialize_command as _materialize_command,
            normalize_disposition as _normalize_disposition,
            normalize_run_status_category as _normalize_run_status_category,
            normalize_truth as _normalize_truth,
            normalize_truth_expectation as _normalize_truth_expectation,
            parse_int as _int,
            parse_repo_path_map,
            repo_path_for_item as _repo_path_for_item,
            resolve_path_for_cwd as _resolve_path_for_cwd,
            reviewer_id_from_command as _reviewer_id_from_command,
            run_records_from_summary as _run_records_from_summary,
            render_evidence_text,
            render_overlap_text,
            render_plan_text,
            render_policy_text,
            render_value_report_text,
            safe_slug as _safe_slug,
            summary_path_for_command as _summary_path_for_command,
            text_from_timeout_stream as _text_from_timeout_stream,
            truth_for_item as _truth_for_item,
        )
        from code_mower.calibration.auto_discovery import (
            build_auto_discovered_corpus,
            fetch_merged_prs_for_auto_discovery,
            load_auto_discovery_input,
        )
    else:
        from tools import code_mower_context_packs, code_mower_telemetry
        from tools.calibration import (
            RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
            RUN_STATUS_BLOCKED,
            RUN_STATUS_CATEGORY_ALIASES,
            RUN_STATUS_INFRA_ERROR,
            RUN_STATUS_PASS,
            RUN_STATUS_UNKNOWN,
            TRUTH_EXPECTATION_ALIASES,
            TRUTH_EXPECTATION_KNOWN_BLOCKED,
            TRUTH_EXPECTATION_KNOWN_CLEAN,
            TRUTH_EXPECTATION_UNKNOWN,
            AUDIT_INPUT_INSUFFICIENT_PATTERNS,
            CALIBRATION_RUN_RESULTS_MODE,
            CALIBRATION_RUN_RESULTS_SCHEMA,
            build_pilot_plan,
            command_lane_id as _command_lane_id,
            command_metadata_for_run as _command_metadata_for_run,
            default_arms,
            build_overlap_report,
            build_lane_policy_report,
            build_reviewer_evidence_report,
            build_value_report,
            audit_input_insufficient_result as _audit_input_insufficient_result,
            coderabbit_blocking_findings as _coderabbit_blocking_findings,
            corpus_with_run_results,
            expected_finding_matches as _expected_finding_matches,
            infra_run_record as _infra_run_record,
            load_corpus,
            load_json_object as _load_json,
            load_run_results as _load_run_results,
            local_llm_profiles_from_command as _local_llm_profiles_from_command,
            local_llm_findings as _local_llm_findings,
            materialize_command as _materialize_command,
            normalize_disposition as _normalize_disposition,
            normalize_run_status_category as _normalize_run_status_category,
            normalize_truth as _normalize_truth,
            normalize_truth_expectation as _normalize_truth_expectation,
            parse_int as _int,
            parse_repo_path_map,
            repo_path_for_item as _repo_path_for_item,
            resolve_path_for_cwd as _resolve_path_for_cwd,
            reviewer_id_from_command as _reviewer_id_from_command,
            run_records_from_summary as _run_records_from_summary,
            render_evidence_text,
            render_overlap_text,
            render_plan_text,
            render_policy_text,
            render_value_report_text,
            safe_slug as _safe_slug,
            summary_path_for_command as _summary_path_for_command,
            text_from_timeout_stream as _text_from_timeout_stream,
            truth_for_item as _truth_for_item,
        )
        from tools.calibration.auto_discovery import (
            build_auto_discovered_corpus,
            fetch_merged_prs_for_auto_discovery,
            load_auto_discovery_input,
        )
elif __package__ == "tools":
    from tools import code_mower_context_packs, code_mower_telemetry
    from tools.calibration import (
        RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
        RUN_STATUS_BLOCKED,
        RUN_STATUS_CATEGORY_ALIASES,
        RUN_STATUS_INFRA_ERROR,
        RUN_STATUS_PASS,
        RUN_STATUS_UNKNOWN,
        TRUTH_EXPECTATION_ALIASES,
        TRUTH_EXPECTATION_KNOWN_BLOCKED,
        TRUTH_EXPECTATION_KNOWN_CLEAN,
        TRUTH_EXPECTATION_UNKNOWN,
        AUDIT_INPUT_INSUFFICIENT_PATTERNS,
        CALIBRATION_RUN_RESULTS_MODE,
        CALIBRATION_RUN_RESULTS_SCHEMA,
        build_pilot_plan,
        command_lane_id as _command_lane_id,
        command_metadata_for_run as _command_metadata_for_run,
        default_arms,
        build_overlap_report,
        build_lane_policy_report,
        build_reviewer_evidence_report,
        build_value_report,
        audit_input_insufficient_result as _audit_input_insufficient_result,
        coderabbit_blocking_findings as _coderabbit_blocking_findings,
        corpus_with_run_results,
        expected_finding_matches as _expected_finding_matches,
        infra_run_record as _infra_run_record,
        load_corpus,
        load_json_object as _load_json,
        load_run_results as _load_run_results,
        local_llm_profiles_from_command as _local_llm_profiles_from_command,
        local_llm_findings as _local_llm_findings,
        materialize_command as _materialize_command,
        normalize_disposition as _normalize_disposition,
        normalize_run_status_category as _normalize_run_status_category,
        normalize_truth as _normalize_truth,
        normalize_truth_expectation as _normalize_truth_expectation,
        parse_int as _int,
        parse_repo_path_map,
        repo_path_for_item as _repo_path_for_item,
        resolve_path_for_cwd as _resolve_path_for_cwd,
        reviewer_id_from_command as _reviewer_id_from_command,
        run_records_from_summary as _run_records_from_summary,
        render_evidence_text,
        render_overlap_text,
        render_plan_text,
        render_policy_text,
        render_value_report_text,
        safe_slug as _safe_slug,
        summary_path_for_command as _summary_path_for_command,
        text_from_timeout_stream as _text_from_timeout_stream,
        truth_for_item as _truth_for_item,
    )
    from tools.calibration.auto_discovery import (
        build_auto_discovered_corpus,
        fetch_merged_prs_for_auto_discovery,
        load_auto_discovery_input,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import code_mower_context_packs, code_mower_telemetry
    from .calibration import (
        RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
        RUN_STATUS_BLOCKED,
        RUN_STATUS_CATEGORY_ALIASES,
        RUN_STATUS_INFRA_ERROR,
        RUN_STATUS_PASS,
        RUN_STATUS_UNKNOWN,
        TRUTH_EXPECTATION_ALIASES,
        TRUTH_EXPECTATION_KNOWN_BLOCKED,
        TRUTH_EXPECTATION_KNOWN_CLEAN,
        TRUTH_EXPECTATION_UNKNOWN,
        AUDIT_INPUT_INSUFFICIENT_PATTERNS,
        CALIBRATION_RUN_RESULTS_MODE,
        CALIBRATION_RUN_RESULTS_SCHEMA,
        build_pilot_plan,
        command_lane_id as _command_lane_id,
        command_metadata_for_run as _command_metadata_for_run,
        default_arms,
        build_overlap_report,
        build_lane_policy_report,
        build_reviewer_evidence_report,
        build_value_report,
        audit_input_insufficient_result as _audit_input_insufficient_result,
        coderabbit_blocking_findings as _coderabbit_blocking_findings,
        corpus_with_run_results,
        expected_finding_matches as _expected_finding_matches,
        infra_run_record as _infra_run_record,
        load_corpus,
        load_json_object as _load_json,
        load_run_results as _load_run_results,
        local_llm_profiles_from_command as _local_llm_profiles_from_command,
        local_llm_findings as _local_llm_findings,
        materialize_command as _materialize_command,
        normalize_disposition as _normalize_disposition,
        normalize_run_status_category as _normalize_run_status_category,
        normalize_truth as _normalize_truth,
        normalize_truth_expectation as _normalize_truth_expectation,
        parse_int as _int,
        parse_repo_path_map,
        repo_path_for_item as _repo_path_for_item,
        resolve_path_for_cwd as _resolve_path_for_cwd,
        reviewer_id_from_command as _reviewer_id_from_command,
        run_records_from_summary as _run_records_from_summary,
        render_evidence_text,
        render_overlap_text,
        render_plan_text,
        render_policy_text,
        render_value_report_text,
        safe_slug as _safe_slug,
        summary_path_for_command as _summary_path_for_command,
        text_from_timeout_stream as _text_from_timeout_stream,
        truth_for_item as _truth_for_item,
    )
    from .calibration.auto_discovery import (
        build_auto_discovered_corpus,
        fetch_merged_prs_for_auto_discovery,
        load_auto_discovery_input,
    )

CONTEXT_PACK_CLI_LANES = {"antigravity-cli", "gemini-cli", "hermes-cli"}
_LEGACY_CORPUS_EXPORTS = (load_corpus, _int)
_LEGACY_RUN_STATUS_EXPORTS = (
    RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CATEGORY_ALIASES,
    RUN_STATUS_INFRA_ERROR,
    RUN_STATUS_PASS,
    RUN_STATUS_UNKNOWN,
    _normalize_run_status_category,
)
_LEGACY_EVIDENCE_EXPORTS = (_normalize_disposition,)
_LEGACY_PLANNING_EXPORTS = (default_arms, build_pilot_plan)
_LEGACY_TRUTH_EXPORTS = (
    TRUTH_EXPECTATION_ALIASES,
    TRUTH_EXPECTATION_KNOWN_BLOCKED,
    TRUTH_EXPECTATION_KNOWN_CLEAN,
    TRUTH_EXPECTATION_UNKNOWN,
    _expected_finding_matches,
    _normalize_truth,
    _normalize_truth_expectation,
    _truth_for_item,
)
_LEGACY_RESULT_EXPORTS = (
    AUDIT_INPUT_INSUFFICIENT_PATTERNS,
    _audit_input_insufficient_result,
    _coderabbit_blocking_findings,
    corpus_with_run_results,
    _infra_run_record,
    _local_llm_findings,
    _run_records_from_summary,
)


def _utc_now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_code_mower_command() -> list[str]:
    module_path = Path(__file__).resolve()
    sibling_cli = module_path.with_name("code_mower_cli.py")
    if sibling_cli.exists():
        return [sys.executable, str(sibling_cli)]
    packaged_cli = module_path.with_name("cli.py")
    if packaged_cli.exists():  # pragma: no cover - exercised after package extraction.
        return [sys.executable, "-m", "code_mower.cli"]
    return ["code-mower"]


def _load_summary(path: Path | None) -> Mapping[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return _load_json(path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _repo_roots_from_path_map(repo_path_map: Mapping[str, str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for selector, path_text in repo_path_map.items():
        repo = re.split(r"[#@]", str(selector), maxsplit=1)[0]
        if "/" in repo and str(path_text).strip():
            roots.setdefault(repo, Path(path_text).expanduser())
    return roots


def _changed_files_from_checkout(repo_path: Path, base_ref: str) -> list[dict[str, str]]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=repo_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "unable to list changed files for context packs with "
            f"{base_ref}...HEAD in {repo_path}: {completed.stderr.strip()}"
        )
    return [
        {"filename": line.strip()}
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def _selected_context_pack_manifest(
    *,
    context_pack_manifest: Mapping[str, Any],
    item: Mapping[str, Any],
    repo_path: Path,
) -> dict[str, Any] | None:
    selected_ids = {
        str(pack_id).strip()
        for pack_id in item.get("context_packs", []) or []
        if str(pack_id).strip()
    }
    if not selected_ids:
        return None
    pack_values = context_pack_manifest.get("packs")
    if not isinstance(pack_values, list):
        raise ValueError("context pack manifest must include a packs list")
    packs_by_id = {
        str(pack.get("id") or "").strip(): pack
        for pack in pack_values
        if isinstance(pack, Mapping)
    }
    missing = sorted(pack_id for pack_id in selected_ids if pack_id not in packs_by_id)
    if missing:
        raise ValueError(f"context pack manifest is missing pack id(s): {', '.join(missing)}")
    base_ref = str(item.get("base_ref") or "origin/main")
    return {
        "repo": str(item.get("repo") or context_pack_manifest.get("repo") or ""),
        "pr_number": item.get("pr_number"),
        "head_sha": str(item.get("head_sha") or context_pack_manifest.get("head_sha") or ""),
        "changed_files": _changed_files_from_checkout(repo_path, base_ref),
        "packs": [packs_by_id[pack_id] for pack_id in sorted(selected_ids)],
    }


def _render_materialized_context_pack_prompt_text(report: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "Code Mower selected context packs",
        f"Manifest: {report.get('manifest_path', '')}",
        "",
    ]
    for pack in report.get("packs", []) or []:
        if not isinstance(pack, Mapping):
            continue
        pack_id = str(pack.get("id") or "context-pack")
        reason = str(pack.get("reason") or "").strip()
        lines.append(f"## Context Pack: {pack_id}")
        if reason:
            lines.append(f"Reason: {reason}")
        files = pack.get("files", [])
        if not isinstance(files, list) or not files:
            lines.append("(no files materialized)")
            lines.append("")
            continue
        for file_entry in files:
            if not isinstance(file_entry, Mapping):
                continue
            path = str(file_entry.get("path") or "")
            repo = str(file_entry.get("repo") or report.get("repo") or "")
            if file_entry.get("exists") is False:
                reason_text = str(file_entry.get("reason") or "missing")
                lines.append(f"### {path}")
                if repo:
                    lines.append(f"Repository: {repo}")
                lines.append(f"[not included: {reason_text}]")
                lines.append("")
                continue
            artifact_path = Path(str(file_entry.get("artifact_path") or ""))
            try:
                content = artifact_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                content = f"[unable to read context artifact: {exc}]"
            lines.append(f"### {path}")
            if repo:
                lines.append(f"Repository: {repo}")
            if file_entry.get("truncated"):
                lines.append(
                    "[truncated to "
                    f"{file_entry.get('bytes_written')} of "
                    f"{file_entry.get('source_bytes')} bytes]"
                )
            lines.append("```")
            lines.append(content.rstrip())
            lines.append("```")
            lines.append("")
    warnings = [
        str(warning)
        for warning in report.get("warnings", []) or []
        if str(warning).strip()
    ]
    if warnings:
        lines.append("## Context Pack Warnings")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines).rstrip() + "\n"


def _context_pack_file_for_command(
    *,
    item: Mapping[str, Any],
    lane_id: str,
    result_dir: Path,
    repo_path_map: Mapping[str, str],
    context_pack_manifest: Mapping[str, Any] | None,
    context_pack_output_dir: Path,
    require_context_pack_files: bool,
) -> Path | None:
    if lane_id not in CONTEXT_PACK_CLI_LANES or context_pack_manifest is None:
        return None
    selected_ids = [
        str(pack_id).strip()
        for pack_id in item.get("context_packs", []) or []
        if str(pack_id).strip()
    ]
    if not selected_ids:
        return None
    repo_path_text = _repo_path_for_item(item, repo_path_map)
    if not repo_path_text:
        raise ValueError(
            "context-pack calibration needs --repo-path-map for "
            f"{item.get('repo')}#{item.get('pr_number')}"
        )
    repo_path = Path(repo_path_text).expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"context-pack repo path is not a directory: {repo_path}")
    manifest = _selected_context_pack_manifest(
        context_pack_manifest=context_pack_manifest,
        item=item,
        repo_path=repo_path,
    )
    if manifest is None:
        return None
    plan = code_mower_context_packs.build_context_pack_plan(manifest)
    output_dir = context_pack_output_dir / _safe_slug(
        str(item.get("calibration_run_id") or result_dir.parent.name),
        "run",
    ) / _safe_slug(lane_id, "lane")
    report = code_mower_context_packs.materialize_context_pack_plan(
        plan,
        repo_root=repo_path,
        output_dir=output_dir,
        require_files=require_context_pack_files,
        repo_roots=_repo_roots_from_path_map(repo_path_map),
    )
    context_text_path = result_dir / "context-pack.txt"
    context_text_path.write_text(
        _render_materialized_context_pack_prompt_text(report),
        encoding="utf-8",
    )
    return context_text_path


def _result_command_dir(results_dir: Path, run_id: str, command_index: int, lane_id: str) -> Path:
    return results_dir / _safe_slug(run_id, "run") / f"{command_index:02d}-{_safe_slug(lane_id, 'lane')}"


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
    code_mower_command = tuple(code_mower_command or _default_code_mower_command())
    repo_path_map = repo_path_map or {}
    plan = build_pilot_plan(
        corpus,
        replicates=replicates,
        output_dir=output_dir,
        jobs=jobs,
    )
    started_at = _utc_now_iso()
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
            lane_id = _command_lane_id(command)
            if selected_lanes and lane_id not in selected_lanes:
                continue
            if limit is not None and prevalidated >= limit:
                continue
            _materialize_command(
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
            lane_id = _command_lane_id(command)
            if selected_lanes and lane_id not in selected_lanes:
                skipped += 1
                continue
            if limit is not None and executed >= limit:
                skipped += 1
                continue
            command_metadata = _command_metadata_for_run(run, command_index)
            reviewer_id = str(
                command_metadata.get("reviewer_id")
                or _reviewer_id_from_command(command)
            )
            materialized = _materialize_command(
                command,
                item=item,
                code_mower_command=code_mower_command,
                repo_path_map=repo_path_map,
                allow_historical_head=allow_historical_head,
            )
            result_dir = _result_command_dir(
                results_dir,
                str(run.get("run_id") or "run"),
                command_index,
                lane_id,
            )
            result_dir.mkdir(parents=True, exist_ok=True)
            context_pack_path = _context_pack_file_for_command(
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
            _write_json(
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
            summary_path = _summary_path_for_command(command)
            resolved_summary_path = _resolve_path_for_cwd(summary_path, cwd)
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
                    stdout = _text_from_timeout_stream(exc.stdout)
                    stderr = _text_from_timeout_stream(exc.stderr)
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
                summary = None if dry_run else _load_summary(resolved_summary_path)
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
                    extracted = _run_records_from_summary(
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
                    _local_llm_profiles_from_command(materialized)
                    if lane_id == "local-llm"
                    else []
                ) or [reviewer_id]
                reviewer_runs.extend(
                    _infra_run_record(
                        lane_id=reviewer,
                        item=infra_item,
                        status=infra_status,
                        duration_seconds=duration_seconds,
                        artifact=str(result_dir / "result.json"),
                    )
                    for reviewer in infra_reviewers
                )
            _write_json(result_dir / "result.json", command_result)
            command_results.append(command_result)
            executed += 1

    payload = {
        "mode": CALIBRATION_RUN_RESULTS_MODE,
        "schema": CALIBRATION_RUN_RESULTS_SCHEMA,
        "run_results_id": uuid.uuid4().hex,
        "corpus_name": corpus.get("name", ""),
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
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
    _write_json(results_dir / "calibration-run-results.json", payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("corpus", type=Path)
    plan_parser.add_argument("--replicates", type=int, default=1)
    plan_parser.add_argument("--output-dir", type=Path, default=Path(".code-mower/calibration"))
    plan_parser.add_argument("--jobs", type=int, default=1)
    plan_parser.add_argument("--json", action="store_true")

    overlap_parser = subparsers.add_parser("overlap")
    overlap_parser.add_argument("calibration_reports", nargs="+", type=Path)
    overlap_parser.add_argument("--json", action="store_true")

    evidence_parser = subparsers.add_parser("evidence")
    evidence_parser.add_argument("corpus", type=Path)
    evidence_parser.add_argument("--json", action="store_true")

    auto_discover_parser = subparsers.add_parser("auto-discover")
    auto_discover_parser.add_argument("--repo", required=True, help="GitHub owner/repo slug.")
    auto_discover_parser.add_argument(
        "--last-n",
        type=int,
        default=20,
        help="Number of recent merged PRs to inspect when querying GitHub.",
    )
    auto_discover_parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Optional saved `gh pr list --json ...` payload to convert offline.",
    )
    auto_discover_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path for the draft calibration corpus JSON.",
    )
    auto_discover_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("corpus", type=Path)
    run_parser.add_argument("--replicates", type=int, default=1)
    run_parser.add_argument("--output-dir", type=Path, default=Path(".code-mower/calibration"))
    run_parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(".code-mower/calibration-results"),
    )
    run_parser.add_argument(
        "--lanes",
        default="",
        help=(
            "Comma-separated command lanes to execute, e.g. "
            "antigravity-cli,gemini-cli,coderabbit-cli,local-llm."
        ),
    )
    run_parser.add_argument(
        "--arms",
        default="",
        help=(
            "Comma-separated calibration arm IDs to execute. Spend-heavy fan-out "
            "arms require explicit selection."
        ),
    )
    run_parser.add_argument("--jobs", type=int, default=1)
    run_parser.add_argument(
        "--code-mower-command",
        nargs="+",
        default=None,
        help="Command used to replace the generated `code-mower` executable.",
    )
    run_parser.add_argument(
        "--repo-path-map",
        action="append",
        default=[],
        help=(
            "Map OWNER/REPO, OWNER/REPO#PR, OWNER/REPO@HEAD, or "
            "OWNER/REPO#PR@HEAD to a clean local PR worktree for CLI reviewers."
        ),
    )
    run_parser.add_argument(
        "--context-pack-manifest",
        type=Path,
        default=None,
        help=(
            "Optional context-pack manifest. Corpus items with context_packs "
            "materialize matching packs from their local PR checkout and pass "
            "the bounded text to supported local CLI reviewers."
        ),
    )
    run_parser.add_argument(
        "--context-pack-output-dir",
        type=Path,
        default=Path(".code-mower/calibration-context-packs"),
        help="Directory for materialized calibration context-pack artifacts.",
    )
    run_parser.add_argument(
        "--require-context-pack-files",
        action="store_true",
        help="Fail if a selected context-pack file is missing.",
    )
    run_parser.add_argument("--allow-historical-head", action="store_true")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--timeout", type=int, default=1800)
    run_parser.add_argument("--json", action="store_true")

    policy_parser = subparsers.add_parser("policy")
    policy_parser.add_argument("metrics_report", type=Path)
    policy_parser.add_argument("--json", action="store_true")

    value_parser = subparsers.add_parser("value-report")
    value_parser.add_argument("corpus", type=Path)
    value_parser.add_argument("--spend", type=Path, default=None)
    value_parser.add_argument(
        "--events",
        nargs="+",
        type=Path,
        default=[],
        help="Optional Code Mower audit event JSONL logs to fold into reviewer metrics.",
    )
    value_parser.add_argument(
        "--runs",
        nargs="+",
        type=Path,
        default=[],
        help="Optional calibration run-results JSON files to fold into reviewer metrics.",
    )
    value_parser.add_argument("--output", type=Path, default=None)
    value_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            payload = build_pilot_plan(
                load_corpus(args.corpus),
                replicates=args.replicates,
                output_dir=args.output_dir,
                jobs=args.jobs,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_plan_text(payload), end="")
            return 0
        if args.command == "overlap":
            payload = build_overlap_report(_load_json(path) for path in args.calibration_reports)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_overlap_text(payload), end="")
            return 0
        if args.command == "evidence":
            payload = build_reviewer_evidence_report(load_corpus(args.corpus))
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_evidence_text(payload), end="")
            return 0
        if args.command == "auto-discover":
            raw_prs = (
                load_auto_discovery_input(args.input)
                if args.input is not None
                else fetch_merged_prs_for_auto_discovery(
                    repo=args.repo,
                    last_n=args.last_n,
                )
            )
            payload = build_auto_discovered_corpus(
                repo=args.repo,
                pull_requests=raw_prs,
                last_n=args.last_n,
            )
            if args.output is not None:
                _write_json(args.output, payload)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    "Code Mower auto-discovered draft corpus\n"
                    f"repo: {payload['discovery']['repo']}\n"
                    f"items: {len(payload.get('corpus', []) or [])}\n"
                    f"caveat: {payload['discovery']['caveat']}\n",
                    end="",
                )
            return 0
        if args.command == "run":
            lanes = tuple(
                lane.strip()
                for lane in str(args.lanes or "").split(",")
                if lane.strip()
            )
            arms = tuple(
                arm.strip()
                for arm in str(args.arms or "").split(",")
                if arm.strip()
            )
            payload = run_calibration_commands(
                load_corpus(args.corpus),
                replicates=args.replicates,
                output_dir=args.output_dir,
                results_dir=args.results_dir,
                lanes=lanes,
                arms=arms,
                jobs=args.jobs,
                code_mower_command=args.code_mower_command,
                repo_path_map=parse_repo_path_map(args.repo_path_map),
                context_pack_manifest=_load_json(args.context_pack_manifest)
                if args.context_pack_manifest is not None
                else None,
                context_pack_output_dir=args.context_pack_output_dir,
                require_context_pack_files=args.require_context_pack_files,
                allow_historical_head=args.allow_historical_head,
                dry_run=args.dry_run,
                limit=args.limit,
                timeout_seconds=args.timeout,
                cwd=Path.cwd(),
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    "Code Mower calibration run\n"
                    f"commands: {payload.get('command_count', 0)}\n"
                    f"reviewer runs: {len(payload.get('reviewer_runs', []) or [])}\n"
                    f"results: {payload.get('results_dir')}\n",
                    end="",
                )
            return 0
        if args.command == "policy":
            payload = build_lane_policy_report(_load_json(args.metrics_report))
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_policy_text(payload), end="")
            return 0
        if args.command == "value-report":
            event_summaries = [
                code_mower_telemetry.summarize_events(
                    code_mower_telemetry.load_jsonl_events(path)
                )
                for path in args.events
            ]
            payload = build_value_report(
                load_corpus(args.corpus),
                spend=_load_json(args.spend) if args.spend is not None else None,
                event_summaries=event_summaries,
                run_results=_load_run_results(args.runs),
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(render_value_report_text(payload), encoding="utf-8")
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_value_report_text(payload), end="")
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled calibration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
