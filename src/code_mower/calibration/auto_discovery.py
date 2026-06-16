"""Draft calibration corpus discovery from GitHub PR metadata."""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .corpus import parse_int
from .identity import safe_slug
from .run_status import RUN_STATUS_BLOCKED, RUN_STATUS_PASS, RUN_STATUS_UNKNOWN

AUTO_DISCOVERY_SCHEMA = "code_mower.calibrationAutoDiscover.v1"
TRUTH_EXPECTATION_UNKNOWN = "unknown"
TRUTH_EXPECTATION_KNOWN_CLEAN = "known_clean"
TRUTH_EXPECTATION_KNOWN_BLOCKED = "known_blocked"

AUDIT_STATE_RE = re.compile(
    r"<!--\s*(?P<provider>[A-Z0-9_]+)_AUDIT_STATE:\s*"
    r"(?P<state>[a-z0-9_-]+)\s*-->",
    re.IGNORECASE,
)
FINDINGS_COUNT_RE = re.compile(
    r"Findings:\s*P0=(?P<p0>\d+),\s*P1=(?P<p1>\d+),\s*"
    r"P2=(?P<p2>\d+),\s*P3=(?P<p3>\d+)",
    re.IGNORECASE,
)
HEAD_SHA_RE = re.compile(r"Head SHA:\s*`?(?P<head>[0-9a-f]{7,40})`?", re.IGNORECASE)


def gh_pr_list_fields() -> str:
    return ",".join(
        [
            "number",
            "title",
            "headRefOid",
            "baseRefName",
            "mergedAt",
            "changedFiles",
            "comments",
            "reviews",
            "labels",
        ]
    )


def load_auto_discovery_input(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    raw_items = (
        payload.get("pull_requests", payload.get("prs", payload))
        if isinstance(payload, Mapping)
        else payload
    )
    if not isinstance(raw_items, list):
        raise ValueError("auto-discover input must be a GitHub PR list JSON array")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise ValueError(f"auto-discover input[{index}] must be a JSON object")
        items.append(dict(item))
    return items


def fetch_merged_prs_for_auto_discovery(
    *,
    repo: str,
    last_n: int,
    runner: Any = subprocess.run,
) -> list[dict[str, Any]]:
    if last_n < 1:
        raise ValueError("--last-n must be at least 1")
    completed = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            str(last_n),
            "--json",
            gh_pr_list_fields(),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        raise ValueError(
            "gh pr list failed for calibration auto-discovery"
            + (f": {stderr}" if stderr else "")
        )
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"gh pr list returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("gh pr list returned a non-list payload")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _provider_from_audit_state(provider: str, state: str) -> str:
    provider_slug = provider.lower().replace("_", "-")
    if provider_slug in {"codex", "claude", "devin", "gitar"}:
        return f"{provider_slug}-audit"
    state_parts = state.split("-audit-", 1)
    if len(state_parts) == 2 and state_parts[0]:
        return f"{state_parts[0]}-audit"
    return f"{provider_slug}-audit"


def _audit_status_from_state(state: str) -> str:
    normalized = state.lower()
    if normalized.endswith("-done") or normalized in {"done", "pass", "passed"}:
        return RUN_STATUS_PASS
    if normalized.endswith("-blocked") or normalized in {"blocked", "fail", "failed"}:
        return RUN_STATUS_BLOCKED
    return RUN_STATUS_UNKNOWN


def _finding_count_from_comment(body: str) -> int:
    match = FINDINGS_COUNT_RE.search(body)
    if not match:
        return 0
    return sum(int(match.group(name)) for name in ("p0", "p1", "p2", "p3"))


def _head_sha_from_comment(body: str) -> str:
    match = HEAD_SHA_RE.search(body)
    return match.group("head") if match else ""


def _audit_runs_from_comments(comments: Sequence[Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        body = str(comment.get("body") or "")
        if not body:
            continue
        for match in AUDIT_STATE_RE.finditer(body):
            state = match.group("state")
            reviewer = _provider_from_audit_state(match.group("provider"), state)
            status = _audit_status_from_state(state)
            run: dict[str, Any] = {
                "reviewer": reviewer,
                "status": status,
                "finding_count": _finding_count_from_comment(body),
                "source": "github-comment-trailer",
            }
            head_sha = _head_sha_from_comment(body)
            if head_sha:
                run["head_sha"] = head_sha
            if status == RUN_STATUS_BLOCKED:
                run["expected_blocker_caught"] = True
                run["disposition"] = "true_positive"
            runs.append(run)
    return runs


def _review_signal_count(reviews: Sequence[Any], comments: Sequence[Any]) -> int:
    count = 0
    for review in reviews:
        if not isinstance(review, Mapping):
            continue
        state = str(review.get("state") or "").upper()
        if state == "CHANGES_REQUESTED":
            count += 1
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        body = str(comment.get("body") or "").lower()
        if "changes requested" in body or "requested changes" in body:
            count += 1
    return count


def _difficulty_from_changed_files(value: Any) -> str:
    changed_files = parse_int(value or 0, field="changedFiles")
    if changed_files >= 20:
        return "hard"
    if changed_files >= 6:
        return "medium"
    return "easy"


def build_auto_discovered_corpus(
    *,
    repo: str,
    pull_requests: Sequence[Mapping[str, Any]],
    last_n: int,
) -> dict[str, Any]:
    if "/" not in repo:
        raise ValueError("--repo must be an owner/repo slug")
    items: list[dict[str, Any]] = []
    for raw in pull_requests:
        pr_number = parse_int(raw.get("number"), field="pull_request.number")
        final_head_sha = str(raw.get("headRefOid") or "")
        comments = list(raw.get("comments", []) or [])
        reviews = list(raw.get("reviews", []) or [])
        reviewer_runs = _audit_runs_from_comments(comments)
        blocked_runs = [
            run for run in reviewer_runs if run.get("status") == RUN_STATUS_BLOCKED
        ]
        pass_runs = [
            run for run in reviewer_runs if run.get("status") == RUN_STATUS_PASS
        ]
        review_signals = _review_signal_count(reviews, comments)
        shared_signal_summary = {
            "audit_blocked_runs": len(blocked_runs),
            "review_signal_count": review_signals,
            "audit_run_count": len(reviewer_runs),
        }

        def make_item(
            *,
            head_sha: str,
            source: str,
            expectation: str,
            runs: list[dict[str, Any]],
            signal_summary: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "base_ref": str(raw.get("baseRefName") or ""),
                "difficulty": _difficulty_from_changed_files(raw.get("changedFiles")),
                "review_class": "auto-discovered",
                "source": source,
                "truth": {
                    "expectation": expectation,
                    "notes": (
                        "Draft auto-discovered from merged PR metadata. Confirm this "
                        "disposition before using it for lane promotion or merge policy."
                    ),
                },
                "expected_findings": [],
                "reviewer_runs": runs,
                "notes": (
                    f"PR title: {raw.get('title') or ''}. "
                    f"Discovery signals: {json.dumps(dict(signal_summary), sort_keys=True)}"
                ),
                "auto_discovery": dict(signal_summary),
            }

        blocked_runs_by_head: dict[str, list[dict[str, Any]]] = {}
        for run in blocked_runs:
            head_sha = str(run.get("head_sha") or "")
            blocked_runs_by_head.setdefault(head_sha, []).append(run)

        for head_sha, runs in sorted(blocked_runs_by_head.items()):
            signal_summary = {
                **shared_signal_summary,
                "case": "historical-blocked-head",
                "case_head_sha": head_sha,
            }
            items.append(
                make_item(
                    head_sha=head_sha,
                    source="auto-discovered-structured-blocker",
                    expectation=TRUTH_EXPECTATION_KNOWN_BLOCKED,
                    runs=runs,
                    signal_summary=signal_summary,
                )
            )

        final_pass_runs = [
            run
            for run in pass_runs
            if not run.get("head_sha") or run.get("head_sha") == final_head_sha
        ]
        explicit_blocked_heads = {
            str(run.get("head_sha")) for run in blocked_runs if run.get("head_sha")
        }
        if (
            final_head_sha
            and final_head_sha not in explicit_blocked_heads
            and (not blocked_runs or final_pass_runs)
        ):
            signal_summary = {
                **shared_signal_summary,
                "case": "merged-final-head",
                "case_head_sha": final_head_sha,
            }
            items.append(
                make_item(
                    head_sha=final_head_sha,
                    source=(
                        "auto-discovered-merged-clean-after-fix"
                        if blocked_runs
                        else "auto-discovered-merged-clean"
                    ),
                    expectation=TRUTH_EXPECTATION_KNOWN_CLEAN
                    if not review_signals or blocked_runs
                    else TRUTH_EXPECTATION_UNKNOWN,
                    runs=final_pass_runs,
                    signal_summary=signal_summary,
                )
            )
        elif review_signals and not blocked_runs:
            signal_summary = {
                **shared_signal_summary,
                "case": "review-signal-needs-human-disposition",
                "case_head_sha": final_head_sha,
            }
            items.append(
                make_item(
                    head_sha=final_head_sha,
                    source="auto-discovered-review-signal-needs-human-disposition",
                    expectation=TRUTH_EXPECTATION_UNKNOWN,
                    runs=[],
                    signal_summary=signal_summary,
                )
            )
    return {
        "version": 1,
        "name": f"auto-discovered-{safe_slug(repo)}",
        "description": (
            "Draft calibration corpus generated from recent merged GitHub PRs. "
            "Review every disposition before treating it as benchmark truth."
        ),
        "discovery": {
            "schema": AUTO_DISCOVERY_SCHEMA,
            "repo": repo,
            "last_n": last_n,
            "generated_at": _dt.datetime.now(tz=_dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "source": "gh pr list --state merged",
            "caveat": (
                "Known-clean and known-blocked labels are heuristics from merged "
                "PR history, structured audit trailers, and review request signals. "
                "They are starter dispositions, not automatic merge-gating truth."
            ),
        },
        "corpus": items,
    }
