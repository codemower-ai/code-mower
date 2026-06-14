#!/usr/bin/env python3
"""Build repo-scoped merge and post-merge verification command plans."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


PR_SPEC_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<pr>[1-9][0-9]*)$")


@dataclass(frozen=True)
class PullRequestSpec:
    repo: str
    pr_number: int

    @property
    def api_repo(self) -> str:
        return self.repo


def parse_pr_spec(value: str) -> PullRequestSpec:
    match = PR_SPEC_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"PR spec must be OWNER/REPO#NUMBER: {value!r}")
    return PullRequestSpec(
        repo=match.group("repo"),
        pr_number=int(match.group("pr")),
    )


def _shell_join(parts: Sequence[str]) -> str:
    rendered = []
    for part in parts:
        if "$MERGE_SHA" in part:
            pieces = part.split("$MERGE_SHA")
            rendered.append("$MERGE_SHA".join(shlex.quote(piece) for piece in pieces))
        else:
            rendered.append(shlex.quote(part))
    return " ".join(rendered)


def _merge_command(spec: PullRequestSpec, *, method: str, delete_branch: bool) -> list[str]:
    command = [
        "gh",
        "pr",
        "merge",
        str(spec.pr_number),
        "--repo",
        spec.repo,
        f"--{method}",
    ]
    if delete_branch:
        command.append("--delete-branch")
    return command


def _preflight_command(spec: PullRequestSpec) -> list[str]:
    return [
        "gh",
        "pr",
        "view",
        str(spec.pr_number),
        "--repo",
        spec.repo,
        "--json",
        "state,isDraft,headRefName,headRefOid,labels,mergeStateStatus,reviewDecision,statusCheckRollup",
    ]


def _post_merge_pr_command(spec: PullRequestSpec) -> list[str]:
    return [
        "gh",
        "pr",
        "view",
        str(spec.pr_number),
        "--repo",
        spec.repo,
        "--json",
        "state,mergedAt,mergeCommit,headRefName,headRefOid",
    ]


def _check_runs_command(spec: PullRequestSpec) -> list[str]:
    return [
        "gh",
        "api",
        f"repos/{spec.api_repo}/commits/$MERGE_SHA/check-runs",
        "--paginate",
        "--jq",
        ".check_runs[] | {name,status,conclusion,html_url,completed_at}",
    ]


def _status_command(spec: PullRequestSpec) -> list[str]:
    return [
        "gh",
        "api",
        f"repos/{spec.api_repo}/commits/$MERGE_SHA/status",
        "--jq",
        "{state, statuses:[.statuses[] | {context,state,description,target_url}]}",
    ]


def build_merge_plan(
    specs: Sequence[PullRequestSpec],
    *,
    method: str = "squash",
    delete_branch: bool = True,
) -> dict[str, Any]:
    if method not in {"squash", "merge", "rebase"}:
        raise ValueError("merge method must be squash, merge, or rebase")
    if not specs:
        raise ValueError("at least one PR spec is required")

    prs = []
    for spec in specs:
        preflight = _preflight_command(spec)
        merge = _merge_command(spec, method=method, delete_branch=delete_branch)
        post_pr = _post_merge_pr_command(spec)
        check_runs = _check_runs_command(spec)
        statuses = _status_command(spec)
        prs.append(
            {
                "repo": spec.repo,
                "pr_number": spec.pr_number,
                "preflight_command": preflight,
                "merge_command": merge,
                "post_merge_pr_command": post_pr,
                "check_runs_command": check_runs,
                "status_command": statuses,
                "commands": {
                    "preflight": _shell_join(preflight),
                    "merge": _shell_join(merge),
                    "post_merge_pr": _shell_join(post_pr),
                    "check_runs": _shell_join(check_runs),
                    "statuses": _shell_join(statuses),
                },
            }
        )

    return {
        "mode": "code-mower-merge-plan",
        "method": method,
        "delete_branch": delete_branch,
        "pull_requests": prs,
        "guardrails": [
            "Run from any directory; every GitHub command is repo-scoped with --repo or repos/OWNER/REPO.",
            "Verify the PR is open, non-draft, mergeStateStatus is CLEAN, and required audit labels are current-head before merging.",
            "After merge, set MERGE_SHA from gh pr view .mergeCommit.oid before running check/status commands.",
            "This planner does not grant merge authority; it only renders safer commands for already-approved merges.",
        ],
    }


def render_merge_plan_text(plan: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower merge plan",
        f"Method: {plan.get('method')}",
        f"Delete branch: {plan.get('delete_branch')}",
        "",
    ]
    for pr in plan.get("pull_requests", []) or []:
        if not isinstance(pr, Mapping):
            continue
        commands = pr.get("commands", {})
        if not isinstance(commands, Mapping):
            commands = {}
        lines.extend(
            [
                f"{pr.get('repo')}#{pr.get('pr_number')}",
                f"- preflight: {commands.get('preflight')}",
                f"- merge: {commands.get('merge')}",
                f"- post-merge PR: {commands.get('post_merge_pr')}",
                f"- check-runs: {commands.get('check_runs')}",
                f"- statuses: {commands.get('statuses')}",
                "",
            ]
        )
    lines.append("Guardrails:")
    for guardrail in plan.get("guardrails", []) or []:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pull_requests", nargs="+", help="PR specs like OWNER/REPO#123")
    parser.add_argument(
        "--method",
        choices=("squash", "merge", "rebase"),
        default="squash",
    )
    parser.add_argument(
        "--keep-branch",
        action="store_true",
        help="Render merge commands without --delete-branch.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        specs = [parse_pr_spec(value) for value in args.pull_requests]
        plan = build_merge_plan(
            specs,
            method=args.method,
            delete_branch=not args.keep_branch,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(render_merge_plan_text(plan), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
