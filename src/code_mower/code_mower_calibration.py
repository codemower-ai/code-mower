#!/usr/bin/env python3
"""Plan and report Code Mower reviewer calibration pilots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import code_mower_telemetry
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
            context_pack_file_for_command as _context_pack_file_for_command,
            default_arms,
            build_overlap_report,
            build_lane_policy_report,
            build_reviewer_evidence_report,
            build_value_report,
            build_effect_report,
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
            resolve_path_for_cwd as _resolve_path_for_cwd,
            reviewer_id_from_command as _reviewer_id_from_command,
            run_records_from_summary as _run_records_from_summary,
            render_evidence_text,
            render_overlap_text,
            render_plan_text,
            render_policy_text,
            render_value_report_html,
            render_value_report_text,
            render_effect_report_text,
            safe_slug as _safe_slug,
            summary_path_for_command as _summary_path_for_command,
            text_from_timeout_stream as _text_from_timeout_stream,
            truth_for_item as _truth_for_item,
            default_code_mower_command as _default_code_mower_command,
            load_summary as _load_summary,
            result_command_dir as _result_command_dir,
            run_calibration_commands,
            utc_now_iso as _utc_now_iso,
            write_json as _write_json,
        )
        from code_mower.calibration.auto_discovery import (
            build_auto_discovered_corpus,
            fetch_merged_prs_for_auto_discovery,
            load_auto_discovery_input,
        )
    else:
        from tools import code_mower_telemetry
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
            context_pack_file_for_command as _context_pack_file_for_command,
            default_arms,
            build_overlap_report,
            build_lane_policy_report,
            build_reviewer_evidence_report,
            build_value_report,
            build_effect_report,
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
            resolve_path_for_cwd as _resolve_path_for_cwd,
            reviewer_id_from_command as _reviewer_id_from_command,
            run_records_from_summary as _run_records_from_summary,
            render_evidence_text,
            render_overlap_text,
            render_plan_text,
            render_policy_text,
            render_value_report_html,
            render_value_report_text,
            render_effect_report_text,
            safe_slug as _safe_slug,
            summary_path_for_command as _summary_path_for_command,
            text_from_timeout_stream as _text_from_timeout_stream,
            truth_for_item as _truth_for_item,
            default_code_mower_command as _default_code_mower_command,
            load_summary as _load_summary,
            result_command_dir as _result_command_dir,
            run_calibration_commands,
            utc_now_iso as _utc_now_iso,
            write_json as _write_json,
        )
        from tools.calibration.auto_discovery import (
            build_auto_discovered_corpus,
            fetch_merged_prs_for_auto_discovery,
            load_auto_discovery_input,
        )
elif __package__ == "tools":
    from tools import code_mower_telemetry
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
        context_pack_file_for_command as _context_pack_file_for_command,
        default_arms,
        build_overlap_report,
        build_lane_policy_report,
        build_reviewer_evidence_report,
        build_value_report,
        build_effect_report,
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
        resolve_path_for_cwd as _resolve_path_for_cwd,
        reviewer_id_from_command as _reviewer_id_from_command,
        run_records_from_summary as _run_records_from_summary,
        render_evidence_text,
        render_overlap_text,
        render_plan_text,
        render_policy_text,
        render_value_report_html,
        render_value_report_text,
        render_effect_report_text,
        safe_slug as _safe_slug,
        summary_path_for_command as _summary_path_for_command,
        text_from_timeout_stream as _text_from_timeout_stream,
        truth_for_item as _truth_for_item,
        default_code_mower_command as _default_code_mower_command,
        load_summary as _load_summary,
        result_command_dir as _result_command_dir,
        run_calibration_commands,
        utc_now_iso as _utc_now_iso,
        write_json as _write_json,
    )
    from tools.calibration.auto_discovery import (
        build_auto_discovered_corpus,
        fetch_merged_prs_for_auto_discovery,
        load_auto_discovery_input,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import code_mower_telemetry
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
        context_pack_file_for_command as _context_pack_file_for_command,
        default_arms,
        build_overlap_report,
        build_lane_policy_report,
        build_reviewer_evidence_report,
        build_value_report,
        build_effect_report,
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
        resolve_path_for_cwd as _resolve_path_for_cwd,
        reviewer_id_from_command as _reviewer_id_from_command,
        run_records_from_summary as _run_records_from_summary,
        render_evidence_text,
        render_overlap_text,
        render_plan_text,
        render_policy_text,
        render_value_report_html,
        render_value_report_text,
        render_effect_report_text,
        safe_slug as _safe_slug,
        summary_path_for_command as _summary_path_for_command,
        text_from_timeout_stream as _text_from_timeout_stream,
        truth_for_item as _truth_for_item,
        default_code_mower_command as _default_code_mower_command,
        load_summary as _load_summary,
        result_command_dir as _result_command_dir,
        run_calibration_commands,
        utc_now_iso as _utc_now_iso,
        write_json as _write_json,
    )
    from .calibration.auto_discovery import (
        build_auto_discovered_corpus,
        fetch_merged_prs_for_auto_discovery,
        load_auto_discovery_input,
    )

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
_LEGACY_RUNNER_EXPORTS = (
    CALIBRATION_RUN_RESULTS_MODE,
    CALIBRATION_RUN_RESULTS_SCHEMA,
    _command_lane_id,
    _command_metadata_for_run,
    _context_pack_file_for_command,
    _local_llm_profiles_from_command,
    _materialize_command,
    _resolve_path_for_cwd,
    _reviewer_id_from_command,
    _safe_slug,
    _summary_path_for_command,
    _text_from_timeout_stream,
    _default_code_mower_command,
    _load_summary,
    _result_command_dir,
    run_calibration_commands,
    _utc_now_iso,
)



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
    value_parser.add_argument(
        "--html-output",
        type=Path,
        default=None,
        help="Optional self-contained local HTML value report output path.",
    )
    value_parser.add_argument("--json", action="store_true")

    effect_parser = subparsers.add_parser("effect-report")
    effect_parser.add_argument("corpus", type=Path)
    effect_parser.add_argument(
        "--runs",
        nargs="+",
        type=Path,
        default=[],
        help="Optional calibration run-results JSON files to fold into provider/lens effect metrics.",
    )
    effect_parser.add_argument("--output", type=Path, default=None)
    effect_parser.add_argument("--json", action="store_true")
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
            if args.html_output is not None:
                args.html_output.parent.mkdir(parents=True, exist_ok=True)
                args.html_output.write_text(
                    render_value_report_html(payload),
                    encoding="utf-8",
                )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_value_report_text(payload), end="")
            return 0
        if args.command == "effect-report":
            payload = build_effect_report(
                load_corpus(args.corpus),
                run_results=_load_run_results(args.runs) if args.runs else [],
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                if args.json:
                    _write_json(args.output, payload)
                else:
                    args.output.write_text(
                        render_effect_report_text(payload),
                        encoding="utf-8",
                    )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_effect_report_text(payload), end="")
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled calibration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
