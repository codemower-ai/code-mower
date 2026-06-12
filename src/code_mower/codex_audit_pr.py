#!/usr/bin/env python3
"""Codex audit CLI — review a single PR via the OpenAI Codex CLI.

Standalone command + Python module. UNLIKE the Devin bridge (cloud webhook)
and the local LLM bridge (local OpenAI-compatible endpoint), the Codex bridge
invokes the local Codex CLI as a subprocess. The first call uses the
built-in review subcommand against a checked-out worktree of the PR
branch; the second call converts that prose review into a schema-shaped
verdict JSON artifact. The built-in reviewer has empirically caught real
bugs the other reviewers miss on the same PR — see the multi-reviewer
calibration in PR #233 (Devin PASS + local LLM 7 false positives + Codex 6
real findings).

Pipeline per audit:

  1. Resolve the LOCAL repo path for `owner/repo` from
     `CODEX_AUDIT_REPO_PATHS` (the CLI requires a real git checkout for
     the built-in review).
  2. Fetch the PR head SHA via the GitHub API.
  3. `git fetch origin pull/<N>/head` + create a temporary worktree at
     the head SHA (detached, so we don't pollute branch namespace).
  4. Run `codex exec --ignore-user-config --sandbox read-only review
     --base origin/main` from that worktree, capturing the final review
     prose via `--output-last-message`.
  5. Run generic `codex exec --ignore-user-config --output-schema`
     outside the PR worktree to convert that prose into the wrapper-owned
     `codeMower.codexAudit.v1` verdict JSON, then validate it with a
     stdlib shape checker. Policy: P0/P1/P2 = blocker; P3 = concern
     (non-blocking). This matches how Codex's #233 findings broke down
     empirically.
  6. Stale-head check: refetch PR head; if it changed mid-review, emit
     the `needs-codex-audit` trailer instead of PASS/BLOCKED.
  7. Post the parsed review as a PR comment ending with the authoritative
     trailer line.
  8. Clean up the worktree.

CLI usage:

    CODEX_AUDIT_REPO_PATHS=\\
      owner/repo:/path/to/reference-app,\\
      owner/other-repo:/path/to/reference-service \\
    GITHUB_TOKEN=... \\
    tools/run_codex_audit_pr.sh --repo owner/other-repo --pr 233

Prefer the wrapper over direct `python3 tools/codex_audit_pr.py`: it
selects a controlled Python interpreter for this script and refuses to
fall back silently to ambient system Python.

Module usage (called by `codex_audit_bridge.py` if/when we add a polling
daemon analogous to local_llm_audit_bridge.py):

    from tools.codex_audit_pr import AuditConfig, audit_pr
    result = audit_pr(config, "owner/other-repo", 233)

Exit codes (CLI mode):
    0  comment posted (or dry-run printed)
    1  generic error (config, network, Codex CLI failure)
    2  stale head SHA detected mid-review (caller may requeue)

Trailer protocol (mirrors Devin/local LLM):
    <!-- CODEX_AUDIT_STATE: codex-audit-done -->     (verdict was PASS)
    <!-- CODEX_AUDIT_STATE: codex-audit-blocked --> (verdict was BLOCKED)
    <!-- CODEX_AUDIT_STATE: needs-codex-audit -->    (stale head; requeue)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ in {None, "", "tools"}:
    try:
        from tools.audit_progress import AuditProgress, run_subprocess_with_progress
    except ImportError:  # pragma: no cover - direct script execution fallback
        from audit_progress import AuditProgress, run_subprocess_with_progress  # type: ignore
else:  # pragma: no cover - exercised after package extraction.
    from .audit_progress import AuditProgress, run_subprocess_with_progress


# ----- Configuration / defaults -----

DEFAULT_CODEX_CLI_PATH = "/Applications/Codex.app/Contents/Resources/codex"
DEFAULT_CODEX_TIMEOUT = 900  # 15 min; Codex review can take 5-10 min on a large diff
DEFAULT_BASE_REF = "origin/main"
DEFAULT_REPOS = "owner/repo,owner/other-repo"
DEFAULT_IGNORE_USER_CONFIG = True
DEFAULT_DIFF_DIAGNOSTIC_BUDGET_BYTES = 600_000
CODEX_AUDIT_SCHEMA_ID = "codeMower.codexAudit.v1"
CODEX_AUDIT_VERDICT_SCHEMA_PATH = (
    Path(__file__).resolve().with_name("codex_audit_verdict.schema.json")
)
MAX_VERDICT_FILE_BYTES = 1_000_000
MAX_GITHUB_COMMENT_CHARS = 64_000
MAX_RENDERED_FINDINGS = 50
MAX_SUMMARY_CHARS = 4_000
MAX_FINDING_TITLE_CHARS = 300
MAX_FINDING_FILE_CHARS = 500
MAX_FINDING_DETAIL_CHARS = 4_000
VERDICT_ARTIFACT_SCHEMA = "code_mower.auditVerdictArtifact.v1"
VERDICT_ARTIFACT_DIR_ENV = "CODE_MOWER_VERDICT_ARTIFACT_DIR"


# ----- Data classes -----


@dataclass
class AuditConfig:
    github_token: str
    repo_paths: Dict[str, Path]  # "owner/repo" → local checkout path
    codex_cli_path: str = DEFAULT_CODEX_CLI_PATH
    base_ref: str = DEFAULT_BASE_REF
    timeout: int = DEFAULT_CODEX_TIMEOUT
    dry_run: bool = False
    # Structured audits are automation, not an interactive Codex session.
    # Default to ignoring user config so personal model/reasoning/plugin
    # settings cannot make audit runtime or behavior depend on the operator's
    # desktop profile. `--use-user-config` is the explicit escape hatch for
    # CLI debugging.
    ignore_user_config: bool = DEFAULT_IGNORE_USER_CONFIG
    # Optional override for the Python venv whose `bin/` is prepended to
    # PATH (and exported as VIRTUAL_ENV) when invoking `codex review`.
    # If None AND `disable_venv` is False, `audit_pr()` auto-discovers
    # `<local_repo>/.venv/`.
    #
    # Without this, `codex review`'s subprocesses (pytest, build_oracle_*,
    # etc.) inherit the audit machine's system PATH — on a typical
    # macOS dev box that resolves to anaconda Python 3.7.6 with stale
    # numpy/Pillow, instead of the canonical .venv (Python 3.12.13,
    # numpy 2.3.5, Pillow 12.2.0 per corpus_manifest.json). The drift
    # produced bogus per-pixel diffs on PR #262 round 3.
    venv_path: Optional[Path] = None
    # Explicit kill switch for venv injection: when True, audit_pr()
    # does NOT auto-discover even if `<local_repo>/.venv/` exists.
    # The CLI sets this via `--venv-path ""` / empty
    # CODEX_AUDIT_VENV_PATH. Separate field rather than overloading
    # venv_path with a `Path("")` sentinel — round-4 P2 on PR #264:
    # `str(Path(""))` returns `"."`, not `""`, so the sentinel could
    # accidentally inject `./bin` if cwd had one.
    disable_venv: bool = False
    # Shared progress emitter for long-running audit phases. When unset,
    # audit_pr() installs the default Code Mower stderr emitter.
    progress: Optional[AuditProgress] = None


@dataclass
class CodexVerdict:
    """Parsed verdict from Codex audit output."""
    verdict: str  # "PASS" | "BLOCKED" | "UNKNOWN"
    prose: str    # the final review block (clean, ready to render as a comment)
    p0_count: int = 0
    p1_count: int = 0
    p2_count: int = 0
    p3_count: int = 0
    findings: List[str] = field(default_factory=list)  # raw [P*] finding lines
    # Local-only diagnostic for structured verdict self-inconsistency
    # (e.g. verdict=pass but P2 findings are present). This is not
    # rendered into the public PR comment; audit_pr() logs it to stderr.
    mismatch_note: str = ""

    @property
    def blocker_count(self) -> int:
        # Policy: P0/P1/P2 = blocker; P3 = concern (non-blocking).
        # Matches the empirical severity breakdown on #233 where every P2
        # finding was a real bug worth fixing.
        return self.p0_count + self.p1_count + self.p2_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "p2_count": self.p2_count,
            "p3_count": self.p3_count,
            "blocker_count": self.blocker_count,
            "findings": self.findings,
            "mismatch_note": self.mismatch_note,
        }


@dataclass
class AuditResult:
    repo: str
    pr_number: int
    head_sha_start: str
    head_sha_end: str
    verdict: str        # "PASS" | "BLOCKED" | "STALE"
    trailer: str
    comment_body: str
    codex_stdout: str   # stdout from the codex CLI (the review proper)
    codex_stderr: str = ""  # stderr captured separately for debugging
    parsed: Optional[CodexVerdict] = None
    posted_comment_url: Optional[str] = None
    verdict_artifact_path: Optional[Path] = None

    def head_changed_during_review(self) -> bool:
        return self.head_sha_start != self.head_sha_end


@dataclass(frozen=True)
class ReviewContextDiagnostics:
    base_ref: str
    head_sha: str
    changed_file_count: int
    diff_bytes: int
    diagnostic_budget_bytes: int = DEFAULT_DIFF_DIAGNOSTIC_BUDGET_BYTES

    @property
    def over_diagnostic_budget(self) -> bool:
        return self.diff_bytes > self.diagnostic_budget_bytes

    def summary(self) -> str:
        return (
            f"base_ref={self.base_ref}; head={self.head_sha[:12]}; "
            f"changed_files={self.changed_file_count}; "
            f"full_diff={self.diff_bytes} bytes; "
            f"diagnostic_budget={self.diagnostic_budget_bytes} bytes; "
            f"wrapper_truncated=no; "
            f"over_diagnostic_budget={'yes' if self.over_diagnostic_budget else 'no'}; "
            "codex_cli_owns_review_context=yes"
        )


# ----- Trailers -----


STALE_TRAILER = "<!-- CODEX_AUDIT_STATE: needs-codex-audit -->"
DONE_TRAILER = "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
BLOCKED_TRAILER = "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"


# ----- GitHub helpers -----


def _gh_request(
    method: str,
    path: str,
    *,
    token: str,
    body: Optional[Dict[str, Any]] = None,
    accept: str = "application/vnd.github+json",
    timeout: int = 30,
) -> Any:
    """GitHub REST call. Returns parsed JSON."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    max_attempts = 3 if method.upper() in {"GET", "HEAD"} else 1
    transient_errors = (
        TimeoutError,
        socket.timeout,
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
        urllib.error.URLError,
    )
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else None
        except urllib.error.HTTPError:
            raise
        except transient_errors:
            if attempt >= max_attempts:
                raise
            time.sleep(min(2 ** (attempt - 1), 4))
    raise AssertionError("unreachable GitHub request retry loop")


def fetch_pull_request(repo: str, pr_number: int, *, token: str) -> Dict[str, Any]:
    return _gh_request("GET", f"/repos/{repo}/pulls/{pr_number}", token=token)


def post_pr_comment(repo: str, pr_number: int, body: str, *, token: str) -> Dict[str, Any]:
    return _gh_request(
        "POST",
        f"/repos/{repo}/issues/{pr_number}/comments",
        token=token,
        body={"body": body},
    )


def _safe_artifact_slug(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    safe = safe.strip("._-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "item"


def _verdict_artifact_root() -> Path:
    configured = os.environ.get(VERDICT_ARTIFACT_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "code-mower-audits" / "verdicts"


def write_audit_verdict_artifact(
    *,
    lane_id: str,
    repo: str,
    pr_number: int,
    head_sha_start: str,
    head_sha_end: str,
    verdict: str,
    trailer: str,
    comment_body: str,
) -> Optional[Path]:
    """Persist the rendered audit comment before posting to GitHub.

    This artifact is intentionally just the public comment body plus metadata.
    It gives operators a replay source if the final GitHub POST fails after the
    expensive model review has already completed.
    """

    root = _verdict_artifact_root()
    repo_slug = _safe_artifact_slug(repo.replace("/", "__"))
    head_slug = _safe_artifact_slug(head_sha_start[:16] or "unknown-head")
    lane_slug = _safe_artifact_slug(lane_id)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"{timestamp}-{lane_slug}-{_safe_artifact_slug(verdict.lower())}.json"
    path = root / repo_slug / f"pr-{pr_number}" / head_slug / filename
    payload = {
        "schema": VERDICT_ARTIFACT_SCHEMA,
        "lane_id": lane_id,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha_start": head_sha_start,
        "head_sha_end": head_sha_end,
        "verdict": verdict,
        "trailer": trailer,
        "comment_body": comment_body,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "posted_comment_url": None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
    except OSError as exc:
        print(
            f"warning: failed to write audit verdict artifact {path}: {exc}",
            file=sys.stderr,
        )
        return None


def load_audit_verdict_artifact(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verdict artifact must contain a JSON object")
    if payload.get("schema") != VERDICT_ARTIFACT_SCHEMA:
        raise ValueError(
            f"unsupported verdict artifact schema: {payload.get('schema')!r}"
        )
    for key in ("repo", "pr_number", "comment_body"):
        if key not in payload:
            raise ValueError(f"verdict artifact missing {key}")
    return payload


def repost_audit_verdict_artifact(path: Path, *, token: str) -> Dict[str, Any]:
    artifact = load_audit_verdict_artifact(path)
    return post_pr_comment(
        str(artifact["repo"]),
        int(artifact["pr_number"]),
        str(artifact["comment_body"]),
        token=token,
    )


# ----- Structured verdict validation -----


def _unknown_structured_verdict(reason: str) -> CodexVerdict:
    return CodexVerdict(
        verdict="UNKNOWN",
        prose=(
            f"(structured Codex verdict is unusable: {reason}; "
            "requeue and retry)"
        ),
    )


def _clip_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    suffix = "... [truncated]"
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def _one_line(value: str, limit: int) -> str:
    return _clip_text(
        value.replace("\r", " ").replace("\n", " ").replace("`", "'"),
        limit,
    )


def _require_exact_keys(
    value: Dict[str, Any],
    required: set,
    where: str,
) -> Optional[str]:
    keys = set(value.keys())
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        return f"{where} missing required keys: {', '.join(missing)}"
    if extra:
        return f"{where} contains unsupported keys: {', '.join(extra)}"
    return None


def _render_structured_prose(
    summary: str,
    findings: List[Dict[str, Any]],
    total_findings: int,
) -> str:
    lines = [
        "Summary:",
        "",
        _clip_text(summary, MAX_SUMMARY_CHARS),
        "",
    ]
    if not findings:
        lines.append("Findings: none.")
        return "\n".join(lines)

    lines.extend(["Findings:", ""])
    for finding in findings:
        severity = finding["severity"]
        title = _one_line(finding["title"], MAX_FINDING_TITLE_CHARS)
        file_path = _one_line(finding["file"], MAX_FINDING_FILE_CHARS)
        line = finding["line"]
        detail = _clip_text(finding["detail"], MAX_FINDING_DETAIL_CHARS)
        lines.append(f"- [{severity}] {title} -- `{file_path}:{line}`")
        for detail_line in detail.splitlines():
            lines.append(f"  {detail_line}")

    omitted = total_findings - len(findings)
    if omitted > 0:
        lines.extend([
            "",
            f"... {omitted} additional finding(s) omitted from the comment "
            "to stay within GitHub's comment limits.",
        ])
    return "\n".join(lines)


def parse_structured_codex_verdict(data: Any) -> CodexVerdict:
    """Validate and classify a structured Codex audit verdict.

    This is deliberately small and stdlib-only. The Codex CLI's
    `--output-schema` shapes the final message, but the wrapper remains
    authoritative for the label policy and re-checks every field before
    posting a trailer-bearing PR comment.
    """
    top_keys = {"schema", "verdict", "summary", "findings"}
    finding_keys = {"severity", "title", "file", "line", "detail"}

    if not isinstance(data, dict):
        return _unknown_structured_verdict("top-level value is not an object")

    key_error = _require_exact_keys(data, top_keys, "top-level object")
    if key_error:
        return _unknown_structured_verdict(key_error)

    if data["schema"] != CODEX_AUDIT_SCHEMA_ID:
        return _unknown_structured_verdict(
            f"schema is {data['schema']!r}, expected {CODEX_AUDIT_SCHEMA_ID!r}"
        )

    declared_verdict = data["verdict"]
    if declared_verdict not in ("pass", "blocked"):
        return _unknown_structured_verdict(
            f"verdict is {declared_verdict!r}, expected 'pass' or 'blocked'"
        )

    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        return _unknown_structured_verdict("summary must be a non-empty string")

    raw_findings = data["findings"]
    if not isinstance(raw_findings, list):
        return _unknown_structured_verdict("findings must be an array")

    p_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    rendered_findings: List[Dict[str, Any]] = []
    finding_lines: List[str] = []

    for idx, raw_finding in enumerate(raw_findings):
        where = f"findings[{idx}]"
        if not isinstance(raw_finding, dict):
            return _unknown_structured_verdict(f"{where} is not an object")

        key_error = _require_exact_keys(raw_finding, finding_keys, where)
        if key_error:
            return _unknown_structured_verdict(key_error)

        severity = raw_finding["severity"]
        if severity not in ("P0", "P1", "P2", "P3"):
            return _unknown_structured_verdict(
                f"{where}.severity is {severity!r}, expected P0/P1/P2/P3"
            )

        for field_name in ("title", "file", "detail"):
            field_value = raw_finding[field_name]
            if not isinstance(field_value, str) or not field_value.strip():
                return _unknown_structured_verdict(
                    f"{where}.{field_name} must be a non-empty string"
                )

        file_path = raw_finding["file"]
        if "\n" in file_path or "\r" in file_path:
            return _unknown_structured_verdict(
                f"{where}.file must be a single-line path"
            )

        line = raw_finding["line"]
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            return _unknown_structured_verdict(
                f"{where}.line must be an integer >= 1"
            )

        p_counts[int(severity[1])] += 1
        finding_lines.append(
            f"- [{severity}] {_one_line(raw_finding['title'], MAX_FINDING_TITLE_CHARS)} "
            f"-- {_one_line(file_path, MAX_FINDING_FILE_CHARS)}:{line}"
        )
        if len(rendered_findings) < MAX_RENDERED_FINDINGS:
            rendered_findings.append(raw_finding)

    blocker_count = p_counts[0] + p_counts[1] + p_counts[2]
    if blocker_count > 0:
        verdict = "BLOCKED"
        mismatch_note = (
            "structured verdict declared pass but blocker findings are present"
            if declared_verdict == "pass" else ""
        )
    elif declared_verdict == "pass":
        verdict = "PASS"
        mismatch_note = ""
    else:
        return _unknown_structured_verdict(
            "verdict declared blocked but no P0/P1/P2 findings were present"
        )

    return CodexVerdict(
        verdict=verdict,
        prose=_render_structured_prose(summary, rendered_findings, len(raw_findings)),
        p0_count=p_counts[0],
        p1_count=p_counts[1],
        p2_count=p_counts[2],
        p3_count=p_counts[3],
        findings=finding_lines,
        mismatch_note=mismatch_note,
    )


def parse_structured_codex_verdict_text(text: str) -> CodexVerdict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _unknown_structured_verdict(f"invalid JSON: {exc}")
    return parse_structured_codex_verdict(data)


def load_structured_codex_verdict(path: Path) -> CodexVerdict:
    if not path.exists():
        return _unknown_structured_verdict(f"verdict file was not created at {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _unknown_structured_verdict(f"could not stat verdict file: {exc}")
    if size <= 0:
        return _unknown_structured_verdict("verdict file is empty")
    if size > MAX_VERDICT_FILE_BYTES:
        return _unknown_structured_verdict(
            f"verdict file is too large ({size} bytes)"
        )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _unknown_structured_verdict(f"could not read verdict file: {exc}")
    return parse_structured_codex_verdict_text(text)


# ----- CLI diagnostic capture -----


DEFAULT_CLI_FAILURE_DIR = (
    Path.home() / ".cache" / "code-mower-audits" / "cli-failures"
)


def dump_cli_failure(
    repo: str,
    pr_number: int,
    head_sha: str,
    stdout: str,
    stderr: str,
    reason: str,
    base_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Persist raw Codex CLI output to disk when the comment body lacks
    the diagnostic content needed to investigate the verdict.

    Called from the UNKNOWN audit_pr branch: the structured verdict file
    was missing, malformed, failed schema validation, or was
    self-inconsistent. The orchestrator auto-requeues, but without this
    dump there's no way to diagnose "real structured-output drift or a
    one-shot Codex CLI flake?" once the worktree is torn down and the next
    audit overwrites the in-memory capture.

    Output: `<base_dir>/<owner>_<name>_pr<N>_<sha[:12]>_<UTC>.log`.
    The file is self-describing — header includes repo/pr/sha/reason
    plus stdout/stderr byte counts so it stays useful when attached
    to an issue without the surrounding context.

    File permissions: 0o600 (owner read/write only). Directory: 0o700.
    The raw output can contain source snippets and credentials
    accidentally printed by PR-controlled subprocesses, so on a
    shared audit host the default umask (often 0o644 file / 0o755
    dir) would leak audit data to other users on the box. Created
    via `os.open(..., mode=0o600)` rather than `write_text()` +
    `chmod()` so there's no TOCTOU window where the file briefly
    exists with the umask perms before being restricted. (Codex P2
    audit on reference-app#196 5e97011.)

    Symlink / pre-existing-path safety: open uses `O_EXCL | O_NOFOLLOW`
    in addition to `O_CREAT`, so a PR-controlled subprocess that
    pre-creates the dump path (predictable filename) as either a
    symlink-to-sensitive-target or a world-readable regular file
    cannot trick this helper into truncating an arbitrary file or
    leaving our 0o600 mode unenforced (`mode` is ignored for
    existing targets). A collision fails the open; the OSError
    handler returns None and the dump is just skipped — preferable
    to writing potentially-credential-laden content into an attacker-
    controlled path. The timestamp includes microseconds to make
    natural collisions vanishingly rare. (Codex P2 audit on
    reference-app#196 17b4156.)

    Returns the dump path on success, or None if the dump failed
    (best-effort; we never want this helper to interrupt an audit).

    `base_dir` is parameterized for tests; defaults to
    `DEFAULT_CLI_FAILURE_DIR` (`~/.cache/code-mower-audits/cli-failures/`).
    """
    try:
        if base_dir is None:
            base_dir = DEFAULT_CLI_FAILURE_DIR
        # `mkdir(mode=0o700)` only takes effect on dirs we create; if the
        # dir already exists with looser perms, mkdir(exist_ok=True) is
        # a no-op. Chmod afterward to make it idempotent. Wrap in its
        # own try so a chmod failure (we don't own a pre-existing dir)
        # doesn't drop the dump — the file's own 0o600 still protects
        # the content.
        base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(base_dir, 0o700)
        except OSError:
            pass

        owner_name = repo.replace("/", "_")
        short_sha = (head_sha or "unknown")[:12]
        # Microsecond resolution — same-second collisions across audits
        # would have caused O_EXCL (below) to refuse the open.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = base_dir / f"{owner_name}_pr{pr_number}_{short_sha}_{ts}.log"

        # Codex P3 audit on reference-app#196 / reference-service#358 0eff691/9cd9213:
        # `len(str)` counts Python characters, not UTF-8 bytes. The
        # CLI output frequently contains non-ASCII (em-dashes, smart
        # quotes, etc.), so a char-count header on a multi-byte body
        # would under-report and mislead anyone investigating
        # truncation. Encode then measure to get the real byte count
        # that landed on disk via `fdopen(..., encoding="utf-8")`.
        stdout_bytes = len(stdout.encode("utf-8"))
        stderr_bytes = len(stderr.encode("utf-8"))
        header = (
            "# Codex CLI raw output — structured verdict was unusable\n"
            f"# repo: {repo}\n"
            f"# pr: {pr_number}\n"
            f"# head_sha: {head_sha}\n"
            f"# captured_at: {ts}\n"
            f"# diagnostic_reason: {reason}\n"
            f"# stdout_bytes: {stdout_bytes}\n"
            f"# stderr_bytes: {stderr_bytes}\n"
        )
        body = (
            header
            + "\n===== STDOUT =====\n"
            + (stdout if stdout else "<empty>\n")
            + "\n===== STDERR =====\n"
            + (stderr if stderr else "<empty>\n")
        )
        # Atomic create-with-mode + symlink/pre-existing refusal:
        # - O_CREAT|O_EXCL: refuses if path already exists; together
        #   they guarantee our 0o600 mode is enforced (the mode arg
        #   is ignored by the kernel for existing targets).
        # - O_NOFOLLOW: refuses if the final path component is a
        #   symlink, so a pre-planted symlink-to-sensitive-file
        #   cannot redirect our write.
        # On collision/symlink: open raises OSError → caught by the
        # outer try → dump skipped (None returned). Better to lose a
        # diagnostic dump than write credential-laden bytes to an
        # attacker-controlled path.
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        return path
    except OSError as exc:
        # Never propagate: a logging failure must not break the audit.
        print(
            f"  warning: failed to dump CLI output: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None


# ----- Worktree management -----


def _create_temp_worktree(local_repo: Path, head_sha: str) -> Path:
    """Create a detached worktree at `head_sha` under a tempdir. Returns
    the worktree path."""
    # First make sure the SHA is present locally. `git fetch origin
    # pull/<N>/head` would also work but we don't know the PR number here;
    # use a direct fetch of the commit.
    # Actually for PR commits not on main, we need to ensure the SHA is
    # locally available. The caller (audit_pr) handles this by passing
    # the head SHA after fetching pull/<n>/head.
    tmp_root = Path(tempfile.mkdtemp(prefix="codex-audit-"))
    worktree_path = tmp_root / "wt"
    subprocess.run(
        ["git", "-C", str(local_repo), "worktree", "add", "--detach",
         str(worktree_path), head_sha],
        check=True, capture_output=True, text=True,
    )
    return worktree_path


def _remove_worktree(local_repo: Path, worktree_path: Path) -> None:
    """Remove a worktree, ignoring errors (best-effort cleanup)."""
    try:
        subprocess.run(
            ["git", "-C", str(local_repo), "worktree", "remove", "--force",
             str(worktree_path)],
            check=False, capture_output=True, text=True,
        )
    except Exception:  # noqa: BLE001
        pass
    # Also rmdir the tempdir parent
    try:
        worktree_path.parent.rmdir()
    except Exception:  # noqa: BLE001
        pass


def _fetch_pr_head(local_repo: Path, pr_number: int) -> None:
    """Ensure the PR's head commit is locally available."""
    subprocess.run(
        ["git", "-C", str(local_repo), "fetch", "origin",
         f"pull/{pr_number}/head"],
        check=True, capture_output=True, text=True,
    )


def _fetch_base_ref(local_repo: Path, base_ref: str) -> None:
    """Refresh the base ref (typically `origin/main`) so the codex review
    diff is against current upstream, not a stale local snapshot. Codex
    round 1 of #234 — P2: without this, a long-lived local checkout
    whose `origin/main` is stale would have Codex diff the PR against
    an older base, including already-merged changes and/or missing
    current-base context.

    `base_ref` is in the form passed to `codex review --base` — typically
    `origin/main`. We `git fetch origin main` (the remote branch part)
    to update the corresponding tracking ref.
    """
    # Parse `origin/<branch>` or fall back to the full ref string.
    # Codex meta-review round 2 — P2: avoid str.removeprefix() (Python 3.9+
    # only). The repo's local `python3` is 3.7, and we don't want this
    # tool to require .venv just to extract a branch name.
    if "/" in base_ref and base_ref.startswith("origin/"):
        remote_branch = base_ref[len("origin/"):]
        subprocess.run(
            ["git", "-C", str(local_repo), "fetch", "origin", remote_branch],
            check=True, capture_output=True, text=True,
        )
    else:
        # Generic fetch — let git figure out the remote.
        subprocess.run(
            ["git", "-C", str(local_repo), "fetch", "origin", base_ref],
            check=True, capture_output=True, text=True,
        )


def _run_git_text(local_repo: Path, args: List[str], *, timeout: int = 60) -> str:
    result = subprocess.run(
        ["git", "-C", str(local_repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def _count_git_stdout_bytes(
    local_repo: Path,
    args: List[str],
    *,
    limit_bytes: int,
    timeout: int = 60,
) -> int:
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be greater than zero")
    command = ["git", "-C", str(local_repo), *args]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    total = 0
    selector = selectors.DefaultSelector()
    try:
        if process.stdout is None:
            raise RuntimeError("git stdout pipe was not created")
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(command, timeout)
            if not selector.select(timeout=remaining):
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(command, timeout)
            chunk = os.read(
                process.stdout.fileno(),
                max(1, min(65536, limit_bytes + 1 - total)),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > limit_bytes:
                process.kill()
                process.communicate(timeout=timeout)
                return total
        _, stderr = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    finally:
        selector.close()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=b"",
            stderr=stderr,
        )
    return total


def _build_review_context_diagnostics(
    local_repo: Path,
    *,
    base_ref: str,
    head_sha: str,
    diagnostic_budget_bytes: int = DEFAULT_DIFF_DIAGNOSTIC_BUDGET_BYTES,
) -> ReviewContextDiagnostics:
    diff_range = f"{base_ref}...{head_sha}"
    names = _run_git_text(
        local_repo,
        ["diff", "--name-only", "--find-renames", diff_range],
    )
    diff_bytes = _count_git_stdout_bytes(
        local_repo,
        ["diff", "--find-renames", "--unified=80", diff_range],
        limit_bytes=diagnostic_budget_bytes,
        timeout=120,
    )
    changed_file_count = len([line for line in names.splitlines() if line.strip()])
    return ReviewContextDiagnostics(
        base_ref=base_ref,
        head_sha=head_sha,
        changed_file_count=changed_file_count,
        diff_bytes=diff_bytes,
        diagnostic_budget_bytes=diagnostic_budget_bytes,
    )


# ----- Codex CLI invocation -----


def _discover_venv(local_repo: Path) -> Optional[Path]:
    """Look for a venv at `<local_repo>/.venv/` and return its
    ABSOLUTE path if it contains a working `bin/python`. Returns None
    otherwise.

    Used by `audit_pr()` when `config.venv_path` is unset and
    `config.disable_venv` is False, so most repos get the right
    Python without any explicit configuration. Repos without a venv
    (e.g. reference-app, which uses npm/vitest for tests) get a no-op.

    The returned path is `resolve()`d so the subprocess (which runs
    with `cwd=worktree_path`) can still find `bin/python` when the
    caller's `local_repo` was a relative path. Round-4 P2 on PR #264.
    """
    candidate = (local_repo / ".venv").resolve()
    if (candidate / "bin" / "python").exists():
        return candidate
    return None


def _build_subprocess_env(venv_path: Optional[Path]) -> Dict[str, str]:
    """Return an env dict for the `codex review` subprocess.

    When `venv_path` is a real path with `bin/python`, prepend
    `<venv>/bin` to PATH and export VIRTUAL_ENV so any Python
    invocations from `codex review` (pytest, diagnostic tools, etc.)
    pick up the right interpreter. Also clear PYTHONHOME if set —
    leaving it pointing at a different Python install breaks the
    venv's site-packages resolution.

    `venv_path` is `resolve()`d to an absolute path before injection.
    The subprocess runs with `cwd=worktree_path`, so any RELATIVE
    PATH entry would resolve from the temp worktree, not the
    caller's cwd. Round-4 P2 on PR #264: previously a relative
    `--venv-path .venv` got injected literally and the subprocess
    then searched `<worktree>/.venv/bin` (which doesn't exist) and
    silently fell back to the ambient interpreter.

    No-op (returns os.environ unchanged) when `venv_path` is None
    or its resolved form doesn't point at a working `bin/python`.
    Codex P3 fix for PR #262 (issue tracked as Task #97): without
    this, audits inherit the system PATH which on a typical macOS
    dev box resolves to anaconda Python 3.7.6 with numpy 1.18 /
    Pillow 9.5 — producing per-pixel drift relative to the
    canonical .venv (3.12.13 / 2.3.5 / 12.2.0).
    """
    env = dict(os.environ)
    # The `codex review` subprocess executes scripts/tooling from the
    # PR worktree under audit — that is untrusted code by definition.
    # GITHUB_TOKEN / GH_TOKEN in this script's parent env are used for
    # GitHub API calls from THIS process (PR fetch, comment post). They
    # must not leak into the codex subprocess where the PR code runs.
    # Caught by Codex P1 audit on reference-app#194 / reference-service#354:
    # `gh auth token` fallback in the wrapper made every audit caller a
    # potential exposure, but the same issue applied to manually-exported
    # tokens before that. This sanitization closes both paths.
    for sensitive in ("GITHUB_TOKEN", "GH_TOKEN"):
        env.pop(sensitive, None)
    if venv_path is None:
        return env
    # Path("") and Path(".") both stringify as "." and resolve to cwd.
    # If cwd happens to have ./bin/python (e.g. the audit machine
    # was started from a directory shaped like a venv) the resolution
    # below would naively inject cwd as VIRTUAL_ENV — exactly the
    # silent-cwd-injection bug round-4 was meant to fix. Round-4
    # round-2 P2 on PR #264: refuse to inject for empty-ish paths
    # regardless of cwd contents. CLI callers never reach here with
    # Path("") because main() routes `--venv-path ""` through
    # `disable_venv=True`, but library callers can.
    if str(venv_path) in ("", "."):
        return env
    abs_venv = venv_path.resolve()
    if not (abs_venv / "bin" / "python").exists():
        warnings.warn(
            f"venv path {abs_venv} has no bin/python; "
            "leaving subprocess environment unchanged",
            RuntimeWarning,
            stacklevel=2,
        )
        return env
    venv_bin = str(abs_venv / "bin")
    env["VIRTUAL_ENV"] = str(abs_venv)
    existing_path = env.get("PATH", "")
    env["PATH"] = (
        f"{venv_bin}{os.pathsep}{existing_path}"
        if existing_path else venv_bin
    )
    env.pop("PYTHONHOME", None)
    return env


def _trusted_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _make_secure_temp_dir(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(path, 0o700)
    return path


def _read_last_message_file(path: Path, fallback: str = "") -> str:
    if not path.exists():
        return fallback
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def _resolve_executable_path(path_text: str, *, label: str, env_name: str) -> str:
    """Resolve either a filesystem path or a command name from PATH."""
    expanded = Path(path_text).expanduser()
    if os.sep in path_text or (os.altsep and os.altsep in path_text):
        if expanded.exists():
            return str(expanded.resolve())
        raise FileNotFoundError(
            f"{label} not found at {path_text}. Install it or override {env_name}."
        )

    resolved = shutil.which(path_text)
    if resolved:
        return resolved
    raise FileNotFoundError(
        f"{label} command not found on PATH: {path_text}. Install it or override {env_name}."
    )


def preflight_codex_cli(config: AuditConfig) -> str:
    """Check the Codex CLI capabilities this wrapper relies on.

    We track and log the installed CLI version rather than hard-pinning,
    but fail fast if the relevant `exec`/`exec review` flags disappear.
    """
    config.codex_cli_path = _resolve_executable_path(
        config.codex_cli_path,
        label="Codex CLI",
        env_name="CODEX_CLI_PATH",
    )

    env = _build_subprocess_env(None)
    version = subprocess.run(
        [config.codex_cli_path, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if version.returncode != 0:
        raise subprocess.CalledProcessError(
            version.returncode,
            [config.codex_cli_path, "--version"],
            output=version.stdout,
            stderr=version.stderr,
        )

    # Probe help without `--ignore-user-config` even when audits will use it.
    # Otherwise an old CLI that rejects the flag would fail here with a raw
    # CalledProcessError before reaching the explicit missing-capability check.
    exec_help = subprocess.run(
        [config.codex_cli_path, "exec", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    review_help = subprocess.run(
        [config.codex_cli_path, "exec", "review", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    for result, command_name in (
        (exec_help, "codex exec --help"),
        (review_help, "codex exec review --help"),
    ):
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                command_name.split(),
                output=result.stdout,
                stderr=result.stderr,
            )

    exec_text = exec_help.stdout + exec_help.stderr
    review_text = review_help.stdout + review_help.stderr
    missing: List[str] = []
    if config.ignore_user_config and "--ignore-user-config" not in exec_text:
        missing.append("codex exec --ignore-user-config")
    for flag in ("--output-schema", "--output-last-message"):
        if flag not in exec_text:
            missing.append(f"codex exec {flag}")
    for flag in ("--base", "--output-last-message"):
        if flag not in review_text:
            missing.append(f"codex exec review {flag}")
    if missing:
        raise RuntimeError(
            "Codex CLI is missing required structured-audit capability: "
            + ", ".join(missing)
        )

    return (version.stdout or version.stderr).strip()


def _codex_exec_command(config: AuditConfig, *args: str) -> List[str]:
    command = [config.codex_cli_path, "exec"]
    if config.ignore_user_config:
        command.append("--ignore-user-config")
    command.extend(args)
    return command


def _structured_verdict_prompt(review_text: str) -> str:
    return (
        "Convert the Codex review prose below into the exact structured "
        "audit verdict schema. This is a transport step, not a second "
        "review. Use only findings that are explicitly present in the "
        "review prose; do not invent new findings. Preserve P0/P1/P2/P3 "
        "severity labels. Verdict policy: if any P0, P1, or P2 finding "
        "is present, verdict must be \"blocked\". If only P3 findings "
        "or no findings are present, verdict must be \"pass\". Use schema "
        f"{CODEX_AUDIT_SCHEMA_ID!r}. For line ranges, use the first line. "
        "If the review says no blocking issues were found, return an "
        "empty findings array.\n\n"
        "Review prose:\n"
        "----- BEGIN REVIEW -----\n"
        f"{review_text.rstrip()}\n"
        "----- END REVIEW -----\n"
    )


def run_codex_review(
    config: AuditConfig,
    worktree_path: Path,
) -> Tuple[str, str]:
    """Run the built-in Codex review from the PR worktree.

    The final review prose is captured via `--output-last-message`, which
    avoids scraping the streaming CLI log. Returns `(review_prose,
    diagnostics)`, where diagnostics is stderr from the review process.

    Raises `subprocess.CalledProcessError` on non-zero exit; a failed
    built-in review is a hard audit failure rather than a structured
    verdict failure.
    """
    config.codex_cli_path = _resolve_executable_path(
        config.codex_cli_path,
        label="Codex CLI",
        env_name="CODEX_CLI_PATH",
    )
    env = _build_subprocess_env(config.venv_path)
    tmp_dir = _make_secure_temp_dir("codex-audit-review-")
    review_path = tmp_dir / "review.txt"
    command = _codex_exec_command(
        config,
        "--sandbox",
        "read-only",
        "review",
        "--base",
        config.base_ref,
        "--output-last-message",
        str(review_path),
    )
    try:
        result = run_subprocess_with_progress(
            command,
            progress=config.progress or AuditProgress("codex-audit"),
            phase="codex-review",
            run=subprocess.run,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=config.timeout,
            env=env,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )
        return _read_last_message_file(review_path, result.stdout), result.stderr
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def run_codex_verdict_structuring(
    config: AuditConfig,
    review_text: str,
) -> Tuple[CodexVerdict, str, str]:
    """Convert review prose to a schema-validated verdict JSON artifact."""
    config.codex_cli_path = _resolve_executable_path(
        config.codex_cli_path,
        label="Codex CLI",
        env_name="CODEX_CLI_PATH",
    )
    if not CODEX_AUDIT_VERDICT_SCHEMA_PATH.exists():
        raise FileNotFoundError(
            f"Codex audit verdict schema not found at "
            f"{CODEX_AUDIT_VERDICT_SCHEMA_PATH}"
        )

    tmp_dir = _make_secure_temp_dir("codex-audit-verdict-")
    verdict_path = tmp_dir / "verdict.json"
    command = _codex_exec_command(
        config,
        "--sandbox",
        "read-only",
        "--output-schema",
        str(CODEX_AUDIT_VERDICT_SCHEMA_PATH),
        "--output-last-message",
        str(verdict_path),
        "-",
    )
    try:
        result = run_subprocess_with_progress(
            command,
            progress=config.progress or AuditProgress("codex-audit"),
            phase="codex-structure",
            run=subprocess.run,
            cwd=str(_trusted_repo_root()),
            input=_structured_verdict_prompt(review_text),
            capture_output=True,
            text=True,
            timeout=config.timeout,
            env=_build_subprocess_env(None),
        )
        if result.returncode != 0:
            return (
                _unknown_structured_verdict(
                    "Codex structured-output pass exited "
                    f"{result.returncode}"
                ),
                result.stdout,
                result.stderr,
            )
        return load_structured_codex_verdict(verdict_path), result.stdout, result.stderr
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ----- Comment formatting -----


def _limit_comment_body(body: str, trailer: str) -> str:
    if len(body) <= MAX_GITHUB_COMMENT_CHARS:
        return body

    note = (
        "\n\n[Codex audit comment truncated to stay under GitHub's "
        "comment-size limit.]\n\n"
    )
    suffix = note + trailer + "\n"
    allowed_prefix_len = MAX_GITHUB_COMMENT_CHARS - len(suffix)
    if allowed_prefix_len < 0:
        return suffix[-MAX_GITHUB_COMMENT_CHARS:]

    prefix = body.rsplit(trailer, 1)[0] if trailer in body else body
    return prefix[:allowed_prefix_len].rstrip() + suffix


def format_comment(
    parsed: CodexVerdict,
    head_sha: str,
    is_stale: bool = False,
    stale_end_sha: Optional[str] = None,
    is_unknown: bool = False,
) -> str:
    """Build the GitHub comment body with header, prose, and trailer."""
    header = "## Codex audit (calibration phase — informational only)\n\n"
    header += f"Head SHA: `{head_sha}`\n"
    if is_stale:
        body = (
            header
            + f"\nHead SHA changed during review (`{head_sha[:8]}` → "
            + f"`{(stale_end_sha or '?')[:8]}`). Skipping this verdict and "
            + f"requeuing for re-review of the new head.\n\n"
            + STALE_TRAILER
            + "\n"
        )
        return _limit_comment_body(body, STALE_TRAILER)
    if is_unknown:
        body = (
            header
            + "\nCould not validate a Codex structured verdict artifact. "
            + "The CLI may have produced no review, "
            + "the structured-output pass may have failed, or the verdict "
            + "format may have drifted. "
            + "Requeuing for re-review.\n\n"
            + STALE_TRAILER
            + "\n"
        )
        return _limit_comment_body(body, STALE_TRAILER)

    header += (
        f"Findings: P0={parsed.p0_count}, P1={parsed.p1_count}, "
        f"P2={parsed.p2_count}, P3={parsed.p3_count} "
        f"(blocker policy: any P0/P1/P2 → BLOCKED)\n\n"
    )

    verdict_line = (
        "Codex Audit: BLOCKED" if parsed.verdict == "BLOCKED"
        else "Codex Audit: PASS"
    )
    trailer = BLOCKED_TRAILER if parsed.verdict == "BLOCKED" else DONE_TRAILER

    body_lines = [
        header.rstrip(),
        "",
        verdict_line,
        "",
        parsed.prose.rstrip(),
        "",
        trailer,
    ]
    return _limit_comment_body("\n".join(body_lines) + "\n", trailer)


# ----- Orchestration -----


def audit_pr(config: AuditConfig, repo: str, pr_number: int) -> AuditResult:
    """End-to-end audit of one PR. Creates a temporary worktree at the PR
    head, runs Codex review, structures its verdict, formats + posts a
    comment."""
    local_repo = config.repo_paths.get(repo)
    if local_repo is None:
        raise ValueError(
            f"no local repo path configured for {repo}. Set "
            f"CODEX_AUDIT_REPO_PATHS=owner/repo:/path[,owner/repo:/path,...]"
        )
    if not local_repo.exists():
        raise FileNotFoundError(
            f"configured local repo path does not exist: {local_repo}"
        )
    if config.progress is None:
        config = replace(config, progress=AuditProgress("codex-audit"))

    pr_meta = fetch_pull_request(repo, pr_number, token=config.github_token)
    head_sha_start = pr_meta["head"]["sha"]

    config.progress.emit(
        "audit",
        status="start",
        detail=f"{repo}#{pr_number} head={head_sha_start[:8]}",
    )
    print(f"audit {repo}#{pr_number} head={head_sha_start[:8]} "
          f"(local: {local_repo})", file=sys.stderr, flush=True)
    codex_version = preflight_codex_cli(config)
    print(
        f"  codex CLI: {codex_version or '(version unavailable)'}",
        file=sys.stderr,
        flush=True,
    )

    # Auto-discover a venv under the local repo and use it for the
    # `codex review` subprocess unless the caller explicitly set one
    # or explicitly disabled discovery via `--venv-path ""`. Without
    # this, audits inherit the system PATH which on a typical macOS
    # dev box resolves to anaconda Python 3.7.6 instead of the
    # canonical .venv (Task #97 / Codex P3 on PR #262). Mutating the
    # config here is local to `audit_pr` (the caller's config dict
    # isn't shared across audits via mutable state).
    #
    # Round-4 P2 on PR #264: the previous version used `Path("")` as
    # the disable sentinel and checked `str(venv_path) == ""` in
    # `_build_subprocess_env`. But `str(Path(""))` returns `"."`,
    # not `""`, so the sentinel never tripped — and if cwd happened
    # to have `./bin/python` the "disable" path silently injected
    # that bogus Python. Replaced with an explicit `disable_venv`
    # bool field; sentinel is gone.
    if config.venv_path is None and not config.disable_venv:
        discovered_venv = _discover_venv(local_repo)
        if discovered_venv is not None:
            print(
                f"  using venv {discovered_venv} for subprocess Python "
                "(set --venv-path to override or empty string to disable)",
                file=sys.stderr, flush=True,
            )
            config = replace(config, venv_path=discovered_venv)

    # Ensure both the PR head AND the base ref are locally fetched
    # before running the review. Stale base = wrong diff = wrong review.
    _fetch_pr_head(local_repo, pr_number)
    _fetch_base_ref(local_repo, config.base_ref)
    review_context_summary = "review context diagnostics unavailable"
    try:
        review_context = _build_review_context_diagnostics(
            local_repo,
            base_ref=config.base_ref,
            head_sha=head_sha_start,
        )
        review_context_summary = review_context.summary()
        print(
            f"  review context: {review_context_summary}",
            file=sys.stderr,
            flush=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(
            f"  warning: failed to build review context diagnostics: {exc}",
            file=sys.stderr,
            flush=True,
        )

    # Codex round 6 of #234 — P3: handle the force-push race where
    # `head_sha_start` was read before the user force-pushed and our
    # `pull/<N>/head` fetch now points at the new SHA. If the old SHA
    # isn't locally available, treat it as a stale-head condition
    # rather than crashing on the worktree create.
    try:
        worktree_path = _create_temp_worktree(local_repo, head_sha_start)
    except subprocess.CalledProcessError as exc:
        # Worktree create failed — likely because the SHA isn't in the
        # local repo anymore (force-push race). Refetch and compare;
        # if the head has moved, emit the stale-requeue comment.
        pr_meta_after = fetch_pull_request(repo, pr_number, token=config.github_token)
        head_sha_after = pr_meta_after["head"]["sha"]
        if head_sha_after != head_sha_start:
            print(f"  force-push race: head moved from {head_sha_start[:8]} "
                  f"to {head_sha_after[:8]} during fetch; emitting STALE",
                  file=sys.stderr, flush=True)
            comment_body = format_comment(
                CodexVerdict(verdict="UNKNOWN",
                             prose="(force-push detected before worktree created)"),
                head_sha_start, is_stale=True, stale_end_sha=head_sha_after,
            )
            result = AuditResult(
                repo=repo, pr_number=pr_number,
                head_sha_start=head_sha_start, head_sha_end=head_sha_after,
                verdict="STALE", trailer=STALE_TRAILER,
                comment_body=comment_body, codex_stdout="", codex_stderr="",
            )
            if not config.dry_run:
                artifact_path = write_audit_verdict_artifact(
                    lane_id="codex-audit",
                    repo=repo,
                    pr_number=pr_number,
                    head_sha_start=head_sha_start,
                    head_sha_end=head_sha_after,
                    verdict=result.verdict,
                    trailer=result.trailer,
                    comment_body=comment_body,
                )
                result.verdict_artifact_path = artifact_path
                if artifact_path is not None:
                    print(
                        f"  saved verdict artifact before posting: {artifact_path}",
                        file=sys.stderr,
                        flush=True,
                    )
                # Loud-failure contract: post_pr_comment() raises on any
                # HTTP error or network failure. DO NOT catch here. The
                # wrapper (tools/run_codex_audit_pr.sh) relies on this
                # script exiting non-zero when posting fails so its
                # finish_lock trap can log status="failed" instead of
                # "completed". A silent catch would re-create the
                # reference-app#184 silent-failure mode where three audit
                # runs reported exitCode=0 but never posted a GitHub
                # comment. See run_codex_audit_pr.sh's finish_lock
                # comment for the wrapper-side history and the memory
                # file feedback_silent_success_silent_failure.md for the
                # general lesson.
                posted = post_pr_comment(repo, pr_number, comment_body,
                                          token=config.github_token)
                result.posted_comment_url = posted.get("html_url")
            config.progress.emit(
                "audit",
                status="finish",
                detail=f"{repo}#{pr_number} verdict=STALE",
            )
            return result
        # Same head; the worktree failure is for some other reason.
        # Re-raise so the outer error path runs.
        raise

    try:
        t0 = time.time()
        review_text, review_stderr = run_codex_review(config, worktree_path)
        dt = time.time() - t0
        print(f"  codex review completed in {dt:.0f}s", file=sys.stderr, flush=True)
    finally:
        _remove_worktree(local_repo, worktree_path)

    # Convert Codex's prose review into a structured, schema-shaped
    # verdict artifact. This second call runs outside the PR worktree and
    # receives only the captured review prose.
    t0 = time.time()
    parsed, structure_stdout, structure_stderr = run_codex_verdict_structuring(
        config,
        review_text,
    )
    dt = time.time() - t0
    print(
        f"  codex verdict structuring completed in {dt:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    if parsed.mismatch_note:
        print(
            f"  structured-verdict mismatch: {parsed.mismatch_note}",
            file=sys.stderr,
            flush=True,
        )
    codex_stdout = review_text
    codex_stderr = (
        "===== CODEX REVIEW CONTEXT =====\n"
        + review_context_summary
        + "\n\n"
        "===== CODEX REVIEW STDERR =====\n"
        + (review_stderr if review_stderr else "<empty>\n")
        + "\n===== STRUCTURE STDOUT =====\n"
        + (structure_stdout if structure_stdout else "<empty>\n")
        + "\n===== STRUCTURE STDERR =====\n"
        + (structure_stderr if structure_stderr else "<empty>\n")
    )

    # Stale-head check: refetch PR head and compare. If it changed mid-review,
    # the verdict applies to a no-longer-current SHA — requeue.
    pr_meta_after = fetch_pull_request(repo, pr_number, token=config.github_token)
    head_sha_end = pr_meta_after["head"]["sha"]
    is_stale = head_sha_start != head_sha_end

    if is_stale:
        comment_body = format_comment(parsed, head_sha_start, is_stale=True,
                                       stale_end_sha=head_sha_end)
        result_verdict = "STALE"
        trailer = STALE_TRAILER
    elif parsed.verdict == "UNKNOWN":
        # When the structured verdict is missing, malformed, or
        # unactionable, requeue rather than auto-PASS. The trailer is
        # the same as STALE because the downstream outcome (re-run,
        # don't trust this comment) is identical.
        #
        # Persist the review prose and structuring diagnostics. The
        # post-comment trailer auto-requeues, but the next attempt
        # overwrites the in-memory capture and the worktree is gone, so
        # without this dump there's no way to diagnose "real structured
        # output drift or one-shot Codex CLI flake?" after the fact.
        # Best-effort: a logging failure must not interrupt the audit.
        dump_path = dump_cli_failure(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha_start,
            stdout=codex_stdout,
            stderr=codex_stderr,
            reason=parsed.prose,
        )
        if dump_path is not None:
            print(
                f"  captured CLI output for diagnosis: {dump_path}",
                file=sys.stderr,
                flush=True,
            )
        comment_body = format_comment(parsed, head_sha_start, is_unknown=True)
        result_verdict = "UNKNOWN"
        trailer = STALE_TRAILER
    else:
        comment_body = format_comment(parsed, head_sha_start)
        result_verdict = parsed.verdict
        trailer = BLOCKED_TRAILER if parsed.verdict == "BLOCKED" else DONE_TRAILER

    result = AuditResult(
        repo=repo,
        pr_number=pr_number,
        head_sha_start=head_sha_start,
        head_sha_end=head_sha_end,
        verdict=result_verdict,
        trailer=trailer,
        comment_body=comment_body,
        codex_stdout=codex_stdout,
        codex_stderr=codex_stderr,
        parsed=parsed,
    )

    if not config.dry_run:
        artifact_path = write_audit_verdict_artifact(
            lane_id="codex-audit",
            repo=repo,
            pr_number=pr_number,
            head_sha_start=head_sha_start,
            head_sha_end=head_sha_end,
            verdict=result_verdict,
            trailer=trailer,
            comment_body=comment_body,
        )
        result.verdict_artifact_path = artifact_path
        if artifact_path is not None:
            print(
                f"  saved verdict artifact before posting: {artifact_path}",
                file=sys.stderr,
                flush=True,
            )
        # Loud-failure contract: see the matching comment ~70 lines above
        # at the STALE-handler call site. tl;dr: post_pr_comment() raising
        # is the only signal the wrapper's finish_lock has that posting
        # failed. Do not catch the exception here.
        posted = post_pr_comment(repo, pr_number, comment_body,
                                  token=config.github_token)
        result.posted_comment_url = posted.get("html_url")
        print(f"posted {repo}#{pr_number} verdict={result_verdict} "
              f"url={result.posted_comment_url}", file=sys.stderr)

    config.progress.emit(
        "audit",
        status="finish",
        detail=f"{repo}#{pr_number} verdict={result_verdict}",
    )
    return result


# ----- CLI entry point -----


def _parse_repo_paths(spec: str) -> Dict[str, Path]:
    """Parse `owner/repo:/path,owner/repo:/path,...` into a dict."""
    out: Dict[str, Path] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"bad CODEX_AUDIT_REPO_PATHS entry: {entry!r} (expected owner/repo:/path)"
            )
        repo, path = entry.split(":", 1)
        out[repo.strip()] = Path(path.strip())
    return out


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Codex audit CLI — review a single pull request.",
    )
    ap.add_argument("--repo", help="owner/repo, e.g. owner/repo")
    ap.add_argument("--pr", type=int, help="PR number")
    ap.add_argument(
        "--repost-verdict-artifact",
        type=Path,
        default=None,
        help=(
            "Post a previously saved verdict artifact comment body and exit "
            "without rerunning Codex."
        ),
    )
    ap.add_argument("--codex-cli-path",
                    default=os.environ.get("CODEX_CLI_PATH", DEFAULT_CODEX_CLI_PATH))
    ap.add_argument("--base-ref", default=os.environ.get("CODEX_BASE_REF", DEFAULT_BASE_REF),
                    help="base ref to diff against (default: origin/main)")
    ap.add_argument("--repo-paths",
                    default=os.environ.get("CODEX_AUDIT_REPO_PATHS", ""),
                    help="comma-separated owner/repo:/path entries. Required.")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("CODEX_AUDIT_TIMEOUT", DEFAULT_CODEX_TIMEOUT)))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=_env_flag("CODEX_AUDIT_DRY_RUN"),
        help="Print the audit comment to stdout instead of posting it.",
    )
    ap.add_argument(
        "--use-user-config",
        action="store_true",
        default=_env_flag("CODEX_AUDIT_USE_USER_CONFIG"),
        help=(
            "Allow codex exec subprocesses to load the operator's user "
            "Codex config. Default is to pass --ignore-user-config so "
            "structured audit runtime is independent of personal "
            "model/reasoning/plugin settings."
        ),
    )
    ap.add_argument(
        "--venv-path",
        default=os.environ.get("CODEX_AUDIT_VENV_PATH"),
        help=(
            "Python venv whose bin/ is prepended to PATH (and exported "
            "as VIRTUAL_ENV) when running `codex review`. Default: "
            "auto-discover `<local_repo>/.venv/`. Pass empty string to "
            "disable. Without this, audits inherit the system PATH "
            "which typically resolves to a stale system Python instead "
            "of the canonical .venv (see corpus_manifest.json for the "
            "pinned environment)."
        ),
    )
    ap.add_argument(
        "--read-token-from-stdin",
        action="store_true",
        help=(
            "Read GITHUB_TOKEN from the first line of stdin instead of "
            "os.environ. The wrapper passes this so the token never "
            "appears in this Python process's initial environment, "
            "which on Linux remains visible via /proc/<pid>/environ "
            "to PR-controlled subprocesses even after "
            "os.environ.pop(). When set, GITHUB_TOKEN / GH_TOKEN env "
            "vars are also cleared as defense-in-depth."
        ),
    )
    return ap.parse_args(argv)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _extract_and_clear_github_token() -> Optional[str]:
    """Legacy: read GITHUB_TOKEN from os.environ, then remove it (and
    GH_TOKEN) from os.environ.

    `os.environ.pop` clears the var from Python's view, but on Linux
    the kernel-exposed /proc/<pid>/environ shows the INITIAL exec
    environment block, not Python's live runtime env. So if the
    parent process exec'd Python with GITHUB_TOKEN in env, that
    initial-env exposure persists for the lifetime of the Python
    process regardless of pop(). The wrapper avoids this by piping
    the token via stdin and passing --read-token-from-stdin instead;
    this function is kept for callers that invoke `codex_audit_pr.py`
    directly without the wrapper, where the /proc exposure is
    accepted as part of the caller's threat model.

    Returns the token value (kept only in a local variable, NOT
    re-exported via os.environ), or None if no token was present.
    """
    token = os.environ.pop("GITHUB_TOKEN", None)
    # GH_TOKEN is the gh CLI's alternate env name; strip it too even if
    # unused here, since the same exposure applies.
    os.environ.pop("GH_TOKEN", None)
    return token


def _resolve_github_token(read_from_stdin: bool) -> Optional[str]:
    """Return the GITHUB_TOKEN, either piped via stdin (wrapper path —
    avoids /proc/<pid>/environ exposure of the initial Python env on
    Linux) or read from os.environ (legacy path for direct callers).

    The wrapper passes --read-token-from-stdin and pipes the token as
    the first line of stdin. When that flag is set, this function
    reads stdin and ALSO clears GITHUB_TOKEN / GH_TOKEN from
    os.environ as defense-in-depth (in case the caller accidentally
    also exported the env vars — the wrapper unsets them before exec
    but a misuse path could leave them).

    Returns None if no token can be resolved by either mechanism.
    """
    if read_from_stdin:
        line = sys.stdin.readline()
        # Belt-and-suspenders: even though the wrapper unsets these
        # before exec, clear them if they somehow made it through.
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GH_TOKEN", None)
        if not line:
            return None
        return line.rstrip("\r\n")
    return _extract_and_clear_github_token()


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    token = _resolve_github_token(args.read_token_from_stdin)
    if not token:
        if args.read_token_from_stdin:
            print(
                "error: --read-token-from-stdin was passed but stdin "
                "did not contain a token",
                file=sys.stderr,
            )
        else:
            print("error: GITHUB_TOKEN env var is required (or pass "
                  "--read-token-from-stdin and pipe the token in)",
                  file=sys.stderr)
        return 1
    if args.repost_verdict_artifact is not None:
        try:
            posted = repost_audit_verdict_artifact(
                args.repost_verdict_artifact,
                token=token,
            )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as exc:
            print(f"error: failed to repost verdict artifact: {exc}", file=sys.stderr)
            return 1
        print(posted.get("html_url") or "posted")
        return 0
    if not args.repo or args.pr is None:
        print("error: --repo and --pr are required unless --repost-verdict-artifact is used", file=sys.stderr)
        return 1
    if not args.repo_paths:
        print("error: --repo-paths or CODEX_AUDIT_REPO_PATHS is required",
              file=sys.stderr)
        return 1

    try:
        repo_paths = _parse_repo_paths(args.repo_paths)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # --venv-path semantics:
    #   not set         (None)         -> auto-discover <local_repo>/.venv/
    #   non-empty str                  -> use that path (resolved to absolute)
    #   empty str       ("")           -> explicit disable (disable_venv=True)
    #
    # Round-4 P2 on PR #264:
    # - Empty string is handled via the explicit `disable_venv` field,
    #   not a `Path("")` sentinel. `str(Path(""))` returns `"."` and
    #   could accidentally inject `./bin` if cwd had one.
    # - Non-empty paths are resolved to absolute BEFORE storing on the
    #   config, so a relative `--venv-path .venv` still finds its
    #   bin/python when the subprocess runs with `cwd=worktree_path`.
    explicit_venv: Optional[Path]
    disable_venv = False
    if args.venv_path is None:
        explicit_venv = None
    elif args.venv_path == "":
        explicit_venv = None
        disable_venv = True
    else:
        explicit_venv = Path(args.venv_path).resolve()
    config = AuditConfig(
        github_token=token,
        repo_paths=repo_paths,
        codex_cli_path=args.codex_cli_path,
        base_ref=args.base_ref,
        timeout=args.timeout,
        dry_run=args.dry_run,
        ignore_user_config=not args.use_user_config,
        venv_path=explicit_venv,
        disable_venv=disable_venv,
    )

    try:
        result = audit_pr(config, args.repo, args.pr)
    except urllib.error.HTTPError as exc:
        print(f"error: GitHub API HTTP {exc.code} — {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: network — {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(f"error: codex audit timed out after {args.timeout}s",
              file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: subprocess failed — {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(result.comment_body)

    # Codex round 7 of #234 — P2: both STALE and UNKNOWN result in a
    # `needs-codex-audit` requeue comment. Automation using the exit
    # code to decide whether to retry must see exit 2 for BOTH cases;
    # otherwise UNKNOWN looks like a successful audit even though the
    # comment requests re-review.
    if result.verdict in ("STALE", "UNKNOWN"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
