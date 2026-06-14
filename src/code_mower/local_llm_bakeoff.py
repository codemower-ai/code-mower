#!/usr/bin/env python3
"""Run a local/private LLM bakeoff against one PR.

The bakeoff is informational only: it never posts GitHub comments and never
changes audit labels. It exists to compare local model behavior under the same
PR context and reviewer prompt.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import local_llm_audit_pr, local_llm_profiles
    else:
        from tools import local_llm_audit_pr, local_llm_profiles
elif __package__ == "tools":
    from tools import local_llm_audit_pr, local_llm_profiles
else:  # pragma: no cover - exercised after package extraction.
    from . import local_llm_audit_pr, local_llm_profiles


DEFAULT_BAKEOFF_PROFILES = (
    "qwen3-coder-next-lmstudio",
    "gemma4-ollama",
)


class BakeoffHeadChangedError(RuntimeError):
    pass


def _split_profiles(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _artifact_name(profile_id: str, suffix: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in profile_id)
    return f"{safe}.{suffix}"


def resolve_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return ""
    return completed.stdout.strip()


def summarize_audit_result(
    profile: local_llm_profiles.LocalLlmProfile,
    result: local_llm_audit_pr.AuditResult,
    *,
    duration_seconds: float,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    http_timeout: int | None = None,
) -> dict[str, Any]:
    blocker_findings = [
        {"path": finding.path, "blockers": finding.blockers}
        for finding in result.file_findings
        if finding.blockers
    ]
    concern_findings = [
        {"path": finding.path, "concerns": finding.concerns}
        for finding in result.file_findings
        if finding.concerns and not finding.blockers
    ]
    parse_failure_count = sum(1 for finding in result.file_findings if finding.parse_failed)
    return {
        "profile_id": profile.profile_id,
        "description": profile.description,
        "api_base": profile.api_base,
        "model": profile.model,
        "context_window": profile.context_window,
        "max_files": max_files if max_files is not None else profile.max_files,
        "max_file_bytes": (
            max_file_bytes if max_file_bytes is not None else profile.max_file_bytes
        ),
        "http_timeout": http_timeout if http_timeout is not None else profile.http_timeout,
        "duration_seconds": round(duration_seconds, 3),
        "repo": result.repo,
        "pr_number": result.pr_number,
        "head_sha_start": result.head_sha_start,
        "head_sha_end": result.head_sha_end,
        "verdict": result.verdict,
        "files_reviewed": len(result.file_findings),
        "blocker_file_count": len(blocker_findings),
        "concern_file_count": len(concern_findings),
        "parse_failure_count": parse_failure_count,
        "json_repair_used_count": sum(1 for finding in result.file_findings if finding.json_repair_used),
        "parse_attempts_total": sum(finding.parse_attempts for finding in result.file_findings),
        "pr_level_blockers": list(result.pr_level_blockers),
        "blocker_findings": blocker_findings,
        "concern_findings": concern_findings,
        "trailer": result.trailer,
    }


def _run_profile_bakeoff(
    *,
    profile: local_llm_profiles.LocalLlmProfile,
    repo: str,
    pr_number: int,
    github_token: str,
    expected_head_sha: str,
    max_files: int | None,
    max_file_bytes: int | None,
    http_timeout: int | None,
    api_key: str | None,
    json_repair_retries: int | None,
    repo_path: Path | None,
    base_ref: str,
    allow_historical_head: bool,
) -> tuple[str, local_llm_audit_pr.AuditResult, dict[str, Any]]:
    started = time.monotonic()
    config = local_llm_audit_pr.AuditConfig(
        github_token=github_token,
        api_base=profile.api_base,
        model=profile.model,
        api_key=api_key or os.environ.get("LOCAL_LLM_API_KEY") or profile.api_key,
        http_timeout=http_timeout if http_timeout is not None else profile.http_timeout,
        max_file_bytes=max_file_bytes if max_file_bytes is not None else profile.max_file_bytes,
        max_files=max_files if max_files is not None else profile.max_files,
        profile_id=profile.profile_id,
        context_window=profile.context_window,
        json_repair_retries=(
            json_repair_retries
            if json_repair_retries is not None
            else 1
        ),
        repo_path=repo_path,
        base_ref=base_ref,
        allow_historical_head=allow_historical_head,
        dry_run=True,
    )
    result = local_llm_audit_pr.audit_pr(config, repo, pr_number)
    duration = time.monotonic() - started
    if (
        result.head_sha_start != expected_head_sha
        or result.head_sha_end != expected_head_sha
    ):
        raise BakeoffHeadChangedError(
            "PR head changed during bakeoff; "
            f"expected {expected_head_sha}, "
            f"got start={result.head_sha_start} end={result.head_sha_end}. "
            "Discard this comparison and rerun on the current head."
        )
    summary = summarize_audit_result(
        profile,
        result,
        duration_seconds=duration,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        http_timeout=http_timeout,
    )
    return profile.profile_id, result, summary


def run_bakeoff(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    profile_ids: list[str],
    expected_head_sha: str | None = None,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    http_timeout: int | None = None,
    api_key: str | None = None,
    json_repair_retries: int | None = None,
    output_dir: Path | None = None,
    jobs: int = 1,
    repo_path: Path | None = None,
    base_ref: str = local_llm_audit_pr.DEFAULT_BASE_REF,
    allow_historical_head: bool = False,
) -> dict[str, Any]:
    if not profile_ids:
        raise ValueError("no local LLM profiles selected; pass --profiles")
    seen_profile_ids: set[str] = set()
    for profile_id in profile_ids:
        if profile_id in seen_profile_ids:
            raise ValueError(f"duplicate local LLM profile id: {profile_id}")
        seen_profile_ids.add(profile_id)
    profiles = [local_llm_profiles.get_profile(profile_id) for profile_id in profile_ids]

    runs: list[dict[str, Any]] = []
    output_paths: dict[str, str] = {}
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    pr_meta = local_llm_audit_pr.fetch_pull_request(repo, pr_number, token=github_token)
    pr_head_sha = str(pr_meta["head"]["sha"])
    current_head_sha = (
        local_llm_audit_pr._local_head_sha(repo_path.expanduser().resolve())
        if repo_path is not None
        else pr_head_sha
    )
    if (
        repo_path is not None
        and not allow_historical_head
        and current_head_sha.lower() != pr_head_sha.lower()
    ):
        raise BakeoffHeadChangedError(
            "local checkout is not at the current PR head; pass "
            "--allow-historical-head for archived calibration runs. "
            f"local={current_head_sha} current_pr={pr_head_sha}."
        )
    normalized_expected_head_sha = expected_head_sha.strip().lower() if expected_head_sha else ""
    normalized_current_head_sha = str(current_head_sha).strip().lower()
    if normalized_expected_head_sha and normalized_expected_head_sha != normalized_current_head_sha:
        raise BakeoffHeadChangedError(
            "PR head does not match calibration corpus; "
            f"expected {expected_head_sha}, current={current_head_sha}. "
            "Refresh the corpus or rerun against the pinned head before comparing results."
        )
    expected_head_sha = current_head_sha
    jobs = max(1, min(jobs, len(profiles)))

    completed: dict[str, tuple[local_llm_audit_pr.AuditResult, dict[str, Any]]] = {}
    if jobs == 1:
        for profile in profiles:
            profile_id, result, summary = _run_profile_bakeoff(
                profile=profile,
                repo=repo,
                pr_number=pr_number,
                github_token=github_token,
                expected_head_sha=expected_head_sha,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                http_timeout=http_timeout,
                api_key=api_key,
                json_repair_retries=json_repair_retries,
                repo_path=repo_path,
                base_ref=base_ref,
                allow_historical_head=allow_historical_head,
            )
            completed[profile_id] = (result, summary)
    else:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
        failure: Exception | None = None
        try:
            future_by_profile = {
                executor.submit(
                    _run_profile_bakeoff,
                    profile=profile,
                    repo=repo,
                    pr_number=pr_number,
                    github_token=github_token,
                    expected_head_sha=expected_head_sha,
                    max_files=max_files,
                    max_file_bytes=max_file_bytes,
                    http_timeout=http_timeout,
                    api_key=api_key,
                    json_repair_retries=json_repair_retries,
                    repo_path=repo_path,
                    base_ref=base_ref,
                    allow_historical_head=allow_historical_head,
                ): profile.profile_id
                for profile in profiles
            }
            for future in concurrent.futures.as_completed(future_by_profile):
                try:
                    profile_id, result, summary = future.result()
                except Exception as exc:
                    for pending in future_by_profile:
                        pending.cancel()
                    failure = exc
                    break
                completed[profile_id] = (result, summary)
        finally:
            executor.shutdown(wait=True, cancel_futures=failure is not None)
        if failure is not None:
            raise failure

    for profile in profiles:
        result, summary = completed[profile.profile_id]
        runs.append(summary)
        if output_dir is not None:
            comment_path = output_dir / _artifact_name(profile.profile_id, "comment.md")
            comment_path.write_text(result.comment_body, encoding="utf-8")
            output_paths[f"{profile.profile_id}:comment"] = str(comment_path)

    payload = {
        "mode": "local-llm-bakeoff",
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": expected_head_sha,
        "pr_head_sha": pr_head_sha,
        "diff_source": "local_checkout" if repo_path is not None else "github_pr",
        "base_ref": base_ref if repo_path is not None else None,
        "profiles": profile_ids,
        "jobs": jobs,
        "runs": runs,
    }
    if output_dir is not None:
        summary_path = output_dir / "summary.json"
        payload["output_paths"] = output_paths
        output_paths["summary"] = str(summary_path)
        summary_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return payload


def render_bakeoff_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Local LLM bakeoff for {payload['repo']}#{payload['pr_number']}",
        f"jobs: {payload.get('jobs', 1)}",
        "",
    ]
    for run in payload["runs"]:
        lines.extend(
            [
                f"- {run['profile_id']} ({run['model']})",
                f"  verdict: {run['verdict']}",
                f"  files: {run['files_reviewed']}, blockers: {run['blocker_file_count']}, "
                f"concerns: {run['concern_file_count']}",
                f"  parse failures: {run['parse_failure_count']}, "
                f"json retry used: {run['json_repair_used_count']}",
                f"  runtime: {run['duration_seconds']}s",
            ]
        )
    if payload.get("output_paths"):
        lines.extend(["", "Artifacts:"])
        for name, path in sorted(payload["output_paths"].items()):
            lines.append(f"- {name}: {path}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_BAKEOFF_PROFILES),
        help="Comma-separated local LLM profile ids.",
    )
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--max-file-bytes", type=int, default=None)
    parser.add_argument("--http-timeout", type=int, default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--expected-head-sha",
        default=None,
        help="Fail if the PR current head does not match this corpus-pinned SHA.",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="optional local checkout to review for archived calibration heads",
    )
    parser.add_argument("--base-ref", default=local_llm_audit_pr.DEFAULT_BASE_REF)
    parser.add_argument(
        "--allow-historical-head",
        action="store_true",
        help="allow --repo-path HEAD to differ from the current GitHub PR head",
    )
    parser.add_argument("--json-repair-retries", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Number of profiles to review concurrently.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    token = resolve_github_token()
    if not token:
        print(
            "error: set GITHUB_TOKEN or authenticate gh so `gh auth token` works",
            file=sys.stderr,
        )
        return 1

    try:
        jobs = (
            args.jobs
            if args.jobs is not None
            else int(os.environ.get("LOCAL_LLM_BAKEOFF_JOBS", "1"))
        )
        payload = run_bakeoff(
            repo=args.repo,
            pr_number=args.pr,
            github_token=token,
            profile_ids=_split_profiles(args.profiles),
            expected_head_sha=args.expected_head_sha,
            max_files=args.max_files,
            max_file_bytes=args.max_file_bytes,
            http_timeout=args.http_timeout,
            api_key=args.api_key,
            json_repair_retries=args.json_repair_retries,
            output_dir=args.output_dir,
            jobs=jobs,
            repo_path=args.repo_path,
            base_ref=args.base_ref,
            allow_historical_head=args.allow_historical_head,
        )
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BakeoffHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(f"error: GitHub/API HTTP {exc.code} - {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: network - {exc}", file=sys.stderr)
        return 1
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_bakeoff_text(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
