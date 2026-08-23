#!/usr/bin/env python3
"""Machine-readable owner/orchestrator decision markers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence


DECISION_MARKER_RE = re.compile(
    r"<!--\s*CODE_MOWER_DECISION:\s*(.*?)\s*-->",
    flags=re.IGNORECASE | re.DOTALL,
)
FINDING_HEADER_RE = re.compile(
    r"^\s*-\s+\[(P[0-3])\]\s+(.*?)(?:\s+--\s+`?([^`\n]+)`?)?\s*$",
    flags=re.IGNORECASE,
)
TRUSTED_DECISION_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
VALID_DECISION_SCOPES = frozenset({"finding", "topic"})
VALID_DECISION_ACTORS = frozenset({"owner", "orchestrator"})
DEFAULT_DECISION_COMMENT_PAGE_CAP = 10
MAX_DECISION_FIELD_CHARS = 500
MAX_DECISION_RENDERED = 50


@dataclass(frozen=True)
class DecisionRecord:
    id: str
    scope: str
    resolves: str
    by: str
    ref: str = ""
    source: str = ""


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    title: str
    location: str
    detail: str

    @property
    def blocker(self) -> bool:
        return self.severity.upper() in {"P0", "P1", "P2"}

    @property
    def searchable_text(self) -> str:
        return " ".join(part for part in (self.title, self.location, self.detail) if part)


def _compact(value: object, max_chars: int = MAX_DECISION_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:max_chars]


def _marker_value(value: str) -> str:
    text = _compact(value)
    if re.fullmatch(r"[A-Za-z0-9._:/#-]+", text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_decision_marker(record: DecisionRecord) -> str:
    fields = [
        ("id", record.id),
        ("scope", record.scope),
        ("resolves", record.resolves),
        ("by", record.by),
    ]
    if record.ref:
        fields.append(("ref", record.ref))
    payload = " ".join(f"{key}={_marker_value(value)}" for key, value in fields)
    return f"<!-- CODE_MOWER_DECISION: {payload} -->"


def render_decision_comment(record: DecisionRecord, *, note: str = "") -> str:
    visible_note = _compact(note, 1_000)
    visible = (
        f"Code Mower decision `{record.id}` records `{record.by}` resolution for "
        f"`{record.resolves}`"
    )
    if record.ref:
        visible += f" ({record.ref})"
    lines = [visible + "."]
    if visible_note:
        lines.extend(["", visible_note])
    lines.extend(["", render_decision_marker(record)])
    return "\n".join(lines) + "\n"


def _record_from_fields(fields: Mapping[str, str]) -> DecisionRecord | None:
    decision_id = _compact(fields.get("id"))
    scope = _compact(fields.get("scope")).lower()
    resolves = _compact(fields.get("resolves"))
    by = _compact(fields.get("by")).lower()
    ref = _compact(fields.get("ref"))
    if not decision_id or not resolves:
        return None
    if scope not in VALID_DECISION_SCOPES:
        return None
    if by not in VALID_DECISION_ACTORS:
        return None
    return DecisionRecord(
        id=decision_id,
        scope=scope,
        resolves=resolves,
        by=by,
        ref=ref,
    )


def parse_decision_markers(text: str) -> tuple[DecisionRecord, ...]:
    records: list[DecisionRecord] = []
    for match in DECISION_MARKER_RE.finditer(text or ""):
        try:
            parts = shlex.split(match.group(1), comments=False, posix=True)
        except ValueError:
            continue
        fields: dict[str, str] = {}
        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            if key:
                fields[key] = value
        record = _record_from_fields(fields)
        if record is not None:
            records.append(record)
    return tuple(records)


def decision_comment_is_trusted(comment: Mapping[str, Any]) -> bool:
    association = str(
        comment.get("author_association") or comment.get("authorAssociation") or ""
    ).upper()
    return association in TRUSTED_DECISION_AUTHOR_ASSOCIATIONS


def collect_decision_records_from_comments(
    comments: Iterable[Mapping[str, Any]],
    *,
    trusted_only: bool = True,
) -> tuple[DecisionRecord, ...]:
    records: list[DecisionRecord] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for comment in comments:
        if trusted_only and not decision_comment_is_trusted(comment):
            continue
        body = str(comment.get("body") or "")
        source = _compact(
            comment.get("html_url")
            or comment.get("url")
            or (f"comment:{comment.get('id')}" if comment.get("id") else "")
        )
        for record in parse_decision_markers(body):
            sourced = replace(record, source=source)
            key = (sourced.id, sourced.scope, sourced.resolves, sourced.by, sourced.ref)
            if key in seen:
                continue
            seen.add(key)
            records.append(sourced)
    return tuple(records)


def render_decision_registry_context(decisions: Sequence[DecisionRecord]) -> str:
    if not decisions:
        return ""
    lines = [
        "Code Mower decision registry",
        "",
        "Recorded CODE_MOWER_DECISION markers from trusted PR or issue comments:",
        "",
    ]
    for decision in decisions[:MAX_DECISION_RENDERED]:
        ref = decision.ref or "none"
        source = decision.source or "unknown"
        lines.append(
            "- "
            f"id={decision.id}; "
            f"scope={decision.scope}; "
            f"resolves={decision.resolves!r}; "
            f"by={decision.by}; "
            f"ref={ref}; "
            f"source={source}"
        )
    omitted = len(decisions) - MAX_DECISION_RENDERED
    if omitted > 0:
        lines.extend(["", f"... {omitted} additional decision marker(s) omitted."])
    return "\n".join(lines) + "\n"


def extract_audit_findings(body: str) -> tuple[AuditFinding, ...]:
    findings: list[AuditFinding] = []
    current: dict[str, str] | None = None
    detail_lines: list[str] = []

    def finish_current() -> None:
        if current is None:
            return
        findings.append(
            AuditFinding(
                severity=current["severity"].upper(),
                title=_compact(current["title"], 1_000),
                location=_compact(current.get("location", ""), 1_000),
                detail="\n".join(detail_lines).strip(),
            )
        )

    for raw_line in (body or "").splitlines():
        match = FINDING_HEADER_RE.match(raw_line)
        if match:
            finish_current()
            current = {
                "severity": match.group(1).upper(),
                "title": match.group(2).strip(),
                "location": (match.group(3) or "").strip(),
            }
            detail_lines = []
            continue
        if current is None:
            continue
        if DECISION_MARKER_RE.search(raw_line):
            continue
        detail_lines.append(raw_line.strip())
    finish_current()
    return tuple(findings)


def _normalized_match_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def decision_matches_text(decision: DecisionRecord, text: str) -> bool:
    needle = _compact(decision.resolves)
    if not needle:
        return False
    haystack = text or ""
    if needle.lower() in haystack.lower():
        return True
    normalized_needle = _normalized_match_text(needle)
    normalized_haystack = _normalized_match_text(haystack)
    return bool(normalized_needle and normalized_needle in normalized_haystack)


def decision_for_finding(
    finding: AuditFinding,
    decisions: Sequence[DecisionRecord],
) -> DecisionRecord | None:
    for decision in decisions:
        if decision.scope not in VALID_DECISION_SCOPES:
            continue
        if decision_matches_text(decision, finding.searchable_text):
            return decision
    return None


def audit_blockers_are_decision_covered(
    body: str,
    decisions: Sequence[DecisionRecord],
) -> bool:
    blockers = [finding for finding in extract_audit_findings(body) if finding.blocker]
    if not blockers:
        return False
    if not decisions:
        return False
    return all(decision_for_finding(finding, decisions) is not None for finding in blockers)


def decision_covered_blocker_ids(
    body: str,
    decisions: Sequence[DecisionRecord],
) -> tuple[str, ...]:
    ids: list[str] = []
    for finding in extract_audit_findings(body):
        if not finding.blocker:
            continue
        decision = decision_for_finding(finding, decisions)
        if decision is not None:
            ids.append(decision.id)
    return tuple(dict.fromkeys(ids))


def _github_request(
    method: str,
    path: str,
    *,
    token: str,
    body: Mapping[str, Any] | None = None,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
        return json.loads(text) if text else None


def _post_issue_comment(repo: str, issue_number: int, body: str, *, token: str) -> str:
    posted = _github_request(
        "POST",
        f"/repos/{repo}/issues/{issue_number}/comments",
        token=token,
        body={"body": body},
    )
    return str((posted or {}).get("html_url") or "")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="code-mower decide",
        description="Render or post a Code Mower decision marker comment.",
    )
    parser.add_argument("--id", required=True, help="Decision id, for example ADR-007.")
    parser.add_argument(
        "--scope",
        choices=sorted(VALID_DECISION_SCOPES),
        default="finding",
        help="Whether the decision resolves a finding id/title or broader topic.",
    )
    parser.add_argument(
        "--resolves",
        required=True,
        help="Finding/topic text auditors should match, for example HOST_DISPLAY_NAME.",
    )
    parser.add_argument(
        "--by",
        choices=sorted(VALID_DECISION_ACTORS),
        default="owner",
        help="Decision authority recorded in the marker.",
    )
    parser.add_argument("--ref", default="", help="ADR, issue, PR, or URL reference.")
    parser.add_argument("--note", default="", help="Optional visible context paragraph.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--post", action="store_true", help="Post the comment to GitHub.")
    parser.add_argument("--repo", default="", help="owner/repo for --post.")
    parser.add_argument("--issue", type=int, default=0, help="Issue or PR number for --post.")
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="Environment variable containing a GitHub token for --post.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    record = DecisionRecord(
        id=_compact(args.id),
        scope=args.scope,
        resolves=_compact(args.resolves),
        by=args.by,
        ref=_compact(args.ref),
    )
    if not record.id or not record.resolves:
        print("error: --id and --resolves must be non-empty", file=sys.stderr)
        return 1
    body = render_decision_comment(record, note=args.note)
    if args.json:
        print(
            json.dumps(
                {
                    "decision": record.__dict__,
                    "marker": render_decision_marker(record),
                    "body": body,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.post:
        if not args.repo or not args.issue:
            print("error: --repo and --issue are required with --post", file=sys.stderr)
            return 1
        token = os.environ.get(args.token_env, "").strip()
        if not token:
            print(f"error: {args.token_env} is required with --post", file=sys.stderr)
            return 1
        try:
            url = _post_issue_comment(args.repo, args.issue, body, token=token)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            print(f"error: failed to post decision comment: {exc}", file=sys.stderr)
            return 1
        print(url or "posted")
        return 0
    print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
