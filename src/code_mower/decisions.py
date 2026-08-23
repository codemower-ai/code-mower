#!/usr/bin/env python3
"""Machine-readable owner/orchestrator decision markers."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
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
AUDIT_FINDINGS_MARKER_RE = re.compile(
    r"<!--\s*CODE_MOWER_AUDIT_FINDINGS:\s*(.*?)\s*-->",
    flags=re.IGNORECASE | re.DOTALL,
)
AUDIT_FINDINGS_SCHEMA = "codeMower.auditFindings.v1"
VALID_DECISION_SCOPES = frozenset({"finding", "topic"})
VALID_DECISION_ACTORS = frozenset({"owner", "orchestrator"})
DEFAULT_DECISION_COMMENT_PAGE_CAP = 10
MAX_DECISION_FIELD_CHARS = 500
MAX_DECISION_RENDERED = 50
FINDING_ID_DIGEST_CHARS = 20


@dataclass(frozen=True)
class DecisionRecord:
    id: str
    scope: str
    resolves: str
    by: str
    finding_id: str = ""
    ref: str = ""
    source: str = ""
    author: str = ""


@dataclass(frozen=True)
class UnauthorizedDecisionMarker:
    author: str
    comment_id: str


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    title: str
    location: str
    detail: str
    lane: str = ""
    finding_id: str = ""

    @property
    def blocker(self) -> bool:
        return self.severity.upper() in {"P0", "P1", "P2"}

    @property
    def primary_file_path(self) -> str:
        return primary_file_path_from_location(self.location)

    @property
    def stable_finding_id(self) -> str:
        if self.finding_id:
            return self.finding_id
        return stable_finding_id(
            self.lane,
            self.title,
            self.primary_file_path,
        )


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
        ("by", record.by),
    ]
    if record.resolves:
        fields.insert(2, ("resolves", record.resolves))
    if record.finding_id:
        fields.append(("finding_id", record.finding_id))
    if record.ref:
        fields.append(("ref", record.ref))
    payload = " ".join(f"{key}={_marker_value(value)}" for key, value in fields)
    return f"<!-- CODE_MOWER_DECISION: {payload} -->"


def render_decision_comment(record: DecisionRecord, *, note: str = "") -> str:
    visible_note = _compact(note, 1_000)
    target = record.finding_id or record.resolves
    visible = (
        f"Code Mower decision `{record.id}` records `{record.by}` resolution for "
        f"`{target}`"
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
    finding_id = _compact(fields.get("finding_id"))
    ref = _compact(fields.get("ref"))
    if not decision_id or (not resolves and not finding_id):
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
        finding_id=finding_id,
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


def _login_from_comment(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, Mapping):
        login = user.get("login")
        if login:
            return _compact(login)
    author = comment.get("author")
    if isinstance(author, Mapping):
        login = author.get("login")
        if login:
            return _compact(login)
    return _compact(comment.get("user_login") or comment.get("author_login") or "")


def _comment_id_from_comment(comment: Mapping[str, Any]) -> str:
    for key in ("id", "databaseId", "database_id", "comment_id"):
        value = comment.get(key)
        if value not in (None, ""):
            return _compact(value)
    return "unknown"


def _normalized_authorities(authorities: Iterable[str] | None) -> frozenset[str]:
    if authorities is None:
        return frozenset()
    return frozenset(author.strip().lower() for author in authorities if author.strip())


def _configured_owner_login(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = [part.strip() for part in value.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        candidates = [str(part).strip() for part in value]
    else:
        candidates = [str(value).strip()]
    return tuple(
        candidate
        for candidate in candidates
        if candidate and candidate.lower() != "todo_owner_login"
    )


def decision_authorities_from_config(config: Mapping[str, Any]) -> tuple[str, ...]:
    authorities: list[str] = []
    owner_surface = config.get("owner_surface")
    if isinstance(owner_surface, Mapping):
        authorities.extend(_configured_owner_login(owner_surface.get("owner_login")))
    decisions_config = config.get("decisions")
    if isinstance(decisions_config, Mapping):
        raw_authorities = decisions_config.get("authorities")
        if isinstance(raw_authorities, str):
            authorities.extend(
                item.strip() for item in raw_authorities.split(",") if item.strip()
            )
        elif isinstance(raw_authorities, Sequence) and not isinstance(
            raw_authorities,
            (bytes, bytearray),
        ):
            authorities.extend(
                str(item).strip()
                for item in raw_authorities
                if str(item).strip()
            )
    return tuple(dict.fromkeys(authorities))


def decision_authorities_from_env(raw: str | None = None) -> tuple[str, ...]:
    text = (
        raw
        if raw is not None
        else os.environ.get("CODE_MOWER_DECISION_AUTHORITIES", "")
    )
    return tuple(item.strip() for item in text.split(",") if item.strip())


def decision_comment_is_trusted(
    comment: Mapping[str, Any],
    *,
    authorities: Iterable[str] | None = None,
) -> bool:
    allowed = _normalized_authorities(authorities)
    if not allowed:
        return False
    return _login_from_comment(comment).lower() in allowed


def collect_decision_records_from_comments(
    comments: Iterable[Mapping[str, Any]],
    *,
    trusted_only: bool = True,
    authorities: Iterable[str] | None = None,
) -> tuple[DecisionRecord, ...]:
    records: list[DecisionRecord] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for comment in comments:
        author = _login_from_comment(comment)
        if trusted_only and not decision_comment_is_trusted(
            comment,
            authorities=authorities,
        ):
            continue
        body = str(comment.get("body") or "")
        source = _compact(
            comment.get("html_url")
            or comment.get("url")
            or (f"comment:{comment.get('id')}" if comment.get("id") else "")
        )
        for record in parse_decision_markers(body):
            sourced = replace(record, source=source, author=author)
            key = (
                sourced.id,
                sourced.scope,
                sourced.resolves,
                sourced.by,
                sourced.finding_id,
                sourced.ref,
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(sourced)
    return tuple(records)


def collect_unauthorized_decision_records_from_comments(
    comments: Iterable[Mapping[str, Any]],
    *,
    authorities: Iterable[str] | None = None,
) -> tuple[UnauthorizedDecisionMarker, ...]:
    records: list[UnauthorizedDecisionMarker] = []
    for comment in comments:
        if decision_comment_is_trusted(comment, authorities=authorities):
            continue
        body = str(comment.get("body") or "")
        author = _login_from_comment(comment) or "unknown"
        comment_id = _comment_id_from_comment(comment)
        for _ in DECISION_MARKER_RE.finditer(body):
            records.append(
                UnauthorizedDecisionMarker(
                    author=author,
                    comment_id=comment_id,
                )
            )
    return tuple(records)


def render_decision_registry_context(
    decisions: Sequence[DecisionRecord],
    *,
    unauthorized: Sequence[UnauthorizedDecisionMarker] = (),
) -> str:
    if not decisions and not unauthorized:
        return ""
    lines = [
        "Code Mower decision registry",
        "",
        "Recorded CODE_MOWER_DECISION markers from configured decision authorities:",
        "Coverage matching is exact. A marker covers a blocker only when "
        "`finding_id` equals the audit lane's stable finding fingerprint, "
        "or when `scope=topic` and `resolves` equals the finding title verbatim.",
        "",
    ]
    if decisions:
        for decision in decisions[:MAX_DECISION_RENDERED]:
            ref = decision.ref or "none"
            source = decision.source or "unknown"
            finding_id = decision.finding_id or "none"
            author = decision.author or "unknown"
            lines.append(
                "- "
                f"id={decision.id}; "
                f"scope={decision.scope}; "
                f"resolves={decision.resolves!r}; "
                f"finding_id={finding_id!r}; "
                f"by={decision.by}; "
                f"author={author}; "
                f"ref={ref}; "
                f"source={source}"
            )
        omitted = len(decisions) - MAX_DECISION_RENDERED
        if omitted > 0:
            lines.extend(["", f"... {omitted} additional decision marker(s) omitted."])
    else:
        lines.append("- none")
    if unauthorized:
        count = len(unauthorized)
        lines.extend(
            [
                "",
                f"Ignored {count} unauthorized CODE_MOWER_DECISION marker(s) "
                "from commenters without decision authority. Report each listed "
                "marker as P3 with title `unauthorized decision marker`. Marker "
                "payload text is intentionally omitted:",
            ]
        )
        for marker in unauthorized[:MAX_DECISION_RENDERED]:
            author = marker.author or "unknown"
            comment_id = marker.comment_id or "unknown"
            lines.append(
                "- "
                f"author={author}; "
                f"comment_id={comment_id}"
            )
        omitted = len(unauthorized) - MAX_DECISION_RENDERED
        if omitted > 0:
            lines.extend(
                ["", f"... {omitted} additional unauthorized marker(s) omitted."]
            )
    return "\n".join(lines) + "\n"


def _normalized_lane(value: str) -> str:
    return _normalized_identifier(value).replace("_", "-")


def render_audit_findings_marker(
    *,
    lane: str,
    findings: Sequence[Mapping[str, Any]],
    complete: bool,
) -> str:
    lane_text = _normalized_lane(lane)
    entries: list[dict[str, Any]] = []
    for finding in findings:
        severity = _compact(finding.get("severity")).upper()
        title = _compact(finding.get("title"), 1_000)
        file_path = _compact(finding.get("file"), 1_000)
        if severity not in {"P0", "P1", "P2", "P3"}:
            continue
        finding_id = stable_finding_id(lane_text, title, file_path)
        if not finding_id:
            continue
        entry: dict[str, Any] = {
            "severity": severity,
            "title": title,
            "file": file_path,
            "finding_id": finding_id,
        }
        line = finding.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line >= 1:
            entry["line"] = line
        entries.append(entry)

    payload = {
        "schema": AUDIT_FINDINGS_SCHEMA,
        "lane": lane_text,
        "complete": bool(complete),
        "findings": entries,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    return f"<!-- CODE_MOWER_AUDIT_FINDINGS: {encoded} -->"


def _decode_audit_findings_payload(raw_payload: str) -> Mapping[str, Any] | None:
    token = "".join(str(raw_payload or "").split())
    if not token or not re.fullmatch(r"[A-Za-z0-9_\-=]+", token):
        return None
    token += "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ):
        return None
    return payload if isinstance(payload, Mapping) else None


def extract_audit_findings(body: str, *, lane: str = "") -> tuple[AuditFinding, ...]:
    """Extract machine-readable findings from a structured audit marker only."""

    marker_matches = list(AUDIT_FINDINGS_MARKER_RE.finditer(body or ""))
    if len(marker_matches) != 1:
        return ()

    payload = _decode_audit_findings_payload(marker_matches[0].group(1))
    if payload is None:
        return ()
    if payload.get("schema") != AUDIT_FINDINGS_SCHEMA:
        return ()
    if payload.get("complete") is not True:
        return ()

    marker_lane = _normalized_lane(str(payload.get("lane") or ""))
    expected_lane = _normalized_lane(lane)
    if not marker_lane or (expected_lane and marker_lane != expected_lane):
        return ()

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        return ()

    findings: list[AuditFinding] = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, Mapping):
            return ()
        severity = _compact(raw_finding.get("severity")).upper()
        title = _compact(raw_finding.get("title"), 1_000)
        file_path = _compact(raw_finding.get("file"), 1_000)
        finding_id = _compact(raw_finding.get("finding_id"), 1_000)
        expected_finding_id = stable_finding_id(marker_lane, title, file_path)
        if (
            severity not in {"P0", "P1", "P2", "P3"}
            or not title
            or not file_path
            or not expected_finding_id
            or finding_id != expected_finding_id
        ):
            return ()
        line = raw_finding.get("line")
        location = file_path
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                return ()
            location = f"{file_path}:{line}"
        findings.append(
            AuditFinding(
                severity=severity,
                title=title,
                location=location,
                detail="",
                lane=marker_lane,
                finding_id=finding_id,
            )
        )
    return tuple(findings)


def _normalized_match_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _normalized_identifier(text: str) -> str:
    return _compact(text).strip("`[]").casefold()


def stable_finding_id(lane: str, title: str, primary_file_path: str) -> str:
    lane_text = _normalized_identifier(lane).replace("_", "-")
    normalized_title = _normalized_match_text(title)
    file_path = _compact(primary_file_path)
    if not lane_text or not normalized_title or not file_path:
        return ""
    payload = "\0".join((lane_text, normalized_title, file_path))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{lane_text}:{digest[:FINDING_ID_DIGEST_CHARS]}"


def primary_file_path_from_location(location: str) -> str:
    text = _compact(location)
    match = re.match(r"^(?P<path>.+?):\d+(?::\d+)?$", text)
    if not match:
        return text
    return match.group("path")


def decision_matches_finding(decision: DecisionRecord, finding: AuditFinding) -> bool:
    if decision.finding_id and decision.finding_id == finding.stable_finding_id:
        return True
    return bool(decision.scope == "topic" and decision.resolves == finding.title)


def decision_for_finding(
    finding: AuditFinding,
    decisions: Sequence[DecisionRecord],
) -> DecisionRecord | None:
    for decision in decisions:
        if decision.scope not in VALID_DECISION_SCOPES:
            continue
        if decision_matches_finding(decision, finding):
            return decision
    return None


def audit_blockers_are_decision_covered(
    body: str,
    decisions: Sequence[DecisionRecord],
    *,
    lane: str = "",
) -> bool:
    blockers = [
        finding
        for finding in extract_audit_findings(body, lane=lane)
        if finding.blocker
    ]
    if not blockers:
        return False
    if not decisions:
        return False
    return all(decision_for_finding(finding, decisions) is not None for finding in blockers)


def decision_covered_blocker_ids(
    body: str,
    decisions: Sequence[DecisionRecord],
    *,
    lane: str = "",
) -> tuple[str, ...]:
    ids: list[str] = []
    for finding in extract_audit_findings(body, lane=lane):
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
        default="",
        help=(
            "Verbatim finding title covered by a scope=topic decision. "
            "Required unless --finding-id is set."
        ),
    )
    parser.add_argument(
        "--finding-id",
        default="",
        help="Stable audit finding fingerprint covered by this decision.",
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
        finding_id=_compact(args.finding_id),
        ref=_compact(args.ref),
    )
    if not record.id or (not record.resolves and not record.finding_id):
        print(
            "error: --id and either --resolves or --finding-id must be non-empty",
            file=sys.stderr,
        )
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
