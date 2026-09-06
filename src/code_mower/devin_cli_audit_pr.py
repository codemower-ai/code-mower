#!/usr/bin/env python3
"""Devin CLI audit - informational local reviewer lane for Code Mower.

This wrapper runs the installed Devin CLI as a non-interactive, bounded,
read-only reviewer against a separately checked-out PR head. It validates the
exact head, parses a normalized verdict JSON, and posts an authoritative
trailer-bearing PR comment plus an audit verdict artifact.

Exit codes:
    0  PASS, BLOCKED, or stale-head requeue comment posted (or dry-run printed)
    1  configuration, network, or Devin CLI runtime failure
    2  UNKNOWN verdict emitted (caller should investigate)

Trailer protocol:
    <!-- DEVIN_CLI_AUDIT_STATE: devin-cli-audit-done -->
    <!-- DEVIN_CLI_AUDIT_STATE: devin-cli-audit-blocked -->
    <!-- DEVIN_CLI_AUDIT_STATE: needs-devin-cli-audit -->
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    from code_mower import audit_limits as code_mower_audit_limits
    from code_mower import prompts as code_mower_prompts
    from code_mower.provider_runners import (
        audit_exit_code,
        bind_actions_run_comment_id,
        build_allowlisted_child_env,
        clip_text,
        fetch_base_ref_sha,
    fetch_pr_head_sha_unless_local_matches,
    fetch_pull_request,
        format_audit_comment_header,
        limit_comment_body,
        local_head_sha,
        one_line,
        parse_repo_paths,
        post_pr_comment,
        ProviderWorkspaceError,
        require_exact_keys,
        resolve_github_token_from_stdin_or_env,
        run_git,
        validate_repo_path_for_wrapper,
        verify_checkout_at_head,
    working_tree_status,
        write_audit_verdict_artifact,
    )
elif __package__ == "tools":  # pragma: no cover - direct helper execution
    import audit_limits as code_mower_audit_limits  # type: ignore
    import prompts as code_mower_prompts  # type: ignore
    from provider_runners import (  # type: ignore
        audit_exit_code,
        bind_actions_run_comment_id,
        build_allowlisted_child_env,
        clip_text,
        fetch_base_ref_sha,
    fetch_pr_head_sha_unless_local_matches,
    fetch_pull_request,
        format_audit_comment_header,
        limit_comment_body,
        local_head_sha,
        one_line,
        parse_repo_paths,
        post_pr_comment,
        ProviderWorkspaceError,
        require_exact_keys,
        resolve_github_token_from_stdin_or_env,
        run_git,
        validate_repo_path_for_wrapper,
        verify_checkout_at_head,
    working_tree_status,
        write_audit_verdict_artifact,
    )
else:  # pragma: no cover - exercised after package extraction
    from . import audit_limits as code_mower_audit_limits
    from . import prompts as code_mower_prompts
    from .provider_runners import (
        audit_exit_code,
        bind_actions_run_comment_id,
        build_allowlisted_child_env,
        clip_text,
        fetch_base_ref_sha,
    fetch_pr_head_sha_unless_local_matches,
    fetch_pull_request,
        format_audit_comment_header,
        limit_comment_body,
        local_head_sha,
        one_line,
        parse_repo_paths,
        post_pr_comment,
        ProviderWorkspaceError,
        require_exact_keys,
        resolve_github_token_from_stdin_or_env,
        run_git,
        validate_repo_path_for_wrapper,
        verify_checkout_at_head,
    working_tree_status,
        write_audit_verdict_artifact,
    )


DEFAULT_DEVIN_COMMAND = "devin"
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_BASE_REF = "origin/main"
DEFAULT_MAX_DIFF_BYTES = code_mower_audit_limits.DEFAULT_MAX_DIFF_BYTES
DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES = (
    code_mower_audit_limits.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES
)
DEFAULT_PROMPT_LENSES = code_mower_prompts.DEFAULT_REVIEW_LENSES
MAX_RENDERED_FINDINGS = 50
MAX_SUMMARY_CHARS = 4_000
MAX_FINDING_TITLE_CHARS = 300
MAX_FINDING_FILE_CHARS = 500
MAX_FINDING_DETAIL_CHARS = 4_000
TRAILER_PREFIX = "DEVIN_CLI_AUDIT_STATE"
PASS_TRAILER = f"<!-- {TRAILER_PREFIX}: devin-cli-audit-done -->"
BLOCKED_TRAILER = f"<!-- {TRAILER_PREFIX}: devin-cli-audit-blocked -->"
NEEDS_TRAILER = f"<!-- {TRAILER_PREFIX}: needs-devin-cli-audit -->"
_MAX_GIT_STDERR_BYTES = 64 * 1024
VERDICT_JSON_KEYS = {"verdict", "summary", "findings"}
FINDING_KEYS = {"severity", "title", "file", "line", "detail"}
BLOCKER_SEVERITIES = {"P0", "P1", "P2"}
SCHEMA_PLACEHOLDER_TEXT = frozenset({"test", "example", "placeholder", "t", "d"})
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class StaleHeadError(RuntimeError):
    """Raised when the PR head moves during the audit."""


class AuthorExcludedError(RuntimeError):
    """Raised when the PR author is the same as the reviewer lane."""


class MalformedOutputError(RuntimeError):
    """Raised when the Devin CLI output cannot be parsed or is not trustworthy."""


class NoTrustworthyVerdictError(RuntimeError):
    """Raised when no trustworthy PASS/BLOCKED verdict can be produced."""


def _placeholder_equal(value: object) -> bool:
    return str(value or "").strip().strip("`'\"").lower() in SCHEMA_PLACEHOLDER_TEXT


def _normalize_diff_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _diff_file_matches(file_path: str, allowed_files: set[str]) -> bool:
    if file_path in allowed_files:
        return True
    suffix_matches = [
        path
        for path in allowed_files
        if path.endswith(f"/{file_path}") or file_path.endswith(f"/{path}")
    ]
    return len(suffix_matches) == 1


def _resolve_diff_hard_limit(max_diff_bytes: int, max_diff_hard_limit_bytes: int) -> int:
    if max_diff_bytes <= 0:
        raise ValueError("max_diff_bytes must be greater than zero")
    hard_limit = max_diff_hard_limit_bytes
    if hard_limit <= 0:
        raise ValueError("max_diff_hard_limit_bytes must be greater than zero")
    if hard_limit < max_diff_bytes:
        raise ValueError(
            "max_diff_hard_limit_bytes must be greater than or equal to max_diff_bytes"
        )
    return hard_limit


def _run_git_limited(
    cwd: Path,
    args: Sequence[str],
    *,
    max_bytes: int,
    timeout: int = 120,
) -> tuple[str, int, bool]:
    """Run a git command while bounding captured stdout bytes and wall time.

    Both pipes are drained through a deadline-aware selector loop so a stalled
    git process or filter cannot block the lane indefinitely, and stdout is
    never buffered beyond ``max_bytes``.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    process = subprocess.Popen(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    observed_bytes = 0
    stderr_bytes = 0
    truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(timeout=remaining)
            if not events:
                timed_out = True
                break
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except OSError:
                    selector.unregister(key.fileobj)
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_bytes += len(chunk)
                    if stderr_bytes <= _MAX_GIT_STDERR_BYTES:
                        stderr_chunks.append(chunk)
                    continue
                previous_bytes = observed_bytes
                observed_bytes += len(chunk)
                if observed_bytes <= max_bytes:
                    chunks.append(chunk)
                    continue
                keep = max(0, max_bytes - previous_bytes)
                if keep:
                    chunks.append(chunk[:keep])
                truncated = True
            if truncated:
                break
        if timed_out or truncated:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    except Exception:
        process.kill()
        try:
            process.wait(timeout=10)
        except Exception:
            pass
        raise
    finally:
        selector.close()
        for pipe in (process.stdout, process.stderr):
            try:
                pipe.close()
            except Exception:
                pass

    if timed_out:
        # Metadata-only failure reason: no raw process output may reach
        # exceptions, comments, or artifacts.
        raise NoTrustworthyVerdictError(
            f"git diff collection exceeded the {timeout}s deadline; "
            "no trustworthy verdict available"
        )

    if not truncated and process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode,
            ["git", *args],
            output=b"".join(chunks),
            stderr=b"".join(stderr_chunks),
        )
    text = b"".join(chunks).decode("utf-8", errors="ignore")
    if truncated:
        text = (
            text.rstrip()
            + "\n\n[diff truncated by devin-cli-audit wrapper; hard limit reached]\n"
        )
    return text, observed_bytes, truncated


def _resolve_base_ref(repo_path: Path, base_ref: str) -> str:
    """Resolve the current base SHA, fetching when a remote is configured."""

    try:
        return fetch_base_ref_sha(repo_path, base_ref)
    except (subprocess.CalledProcessError, OSError):
        # If no remote is configured, fall back to a purely local ref so the
        # wrapper still works in checked-out test fixtures and offline repos.
        remotes = run_git(repo_path, ["remote"], check=False, timeout=30).stdout.strip()
        if not remotes:
            return run_git(
                repo_path,
                ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
                timeout=30,
            ).stdout.strip()
        raise


def _resolve_diff(
    repo_path: Path,
    pr_number: int,
    base_ref: str,
    expected_head_sha: str,
    max_diff_bytes: int,
    max_diff_hard_limit_bytes: int,
) -> tuple[str, tuple[str, ...]]:
    """Build a bounded diff and changed-files list from exact base/head SHAs."""

    hard_limit = _resolve_diff_hard_limit(max_diff_bytes, max_diff_hard_limit_bytes)

    fetched_head_ref = fetch_pr_head_sha_unless_local_matches(
        repo_path,
        pr_number,
        expected_head_sha=expected_head_sha,
    )
    if fetched_head_ref.lower() != expected_head_sha.lower():
        raise StaleHeadError(
            f"local checkout head changed before diff: {expected_head_sha} -> {fetched_head_ref}"
        )

    fetched_base_ref = _resolve_base_ref(repo_path, base_ref)
    diff_range = f"{fetched_base_ref}...{fetched_head_ref}"

    changed_files_text = run_git(
        repo_path,
        ["-c", "core.quotePath=false", "diff", "--name-only", "--find-renames", diff_range],
        timeout=120,
    ).stdout
    changed_files = tuple(
        line.strip() for line in changed_files_text.splitlines() if line.strip()
    )

    diff, full_diff_bytes, was_truncated = _run_git_limited(
        repo_path,
        ["diff", "--no-ext-diff", "--find-renames", "--unified=80", diff_range],
        max_bytes=hard_limit,
        timeout=120,
    )
    if was_truncated:
        raise NoTrustworthyVerdictError(
            f"PR diff exceeds the configured hard limit ({hard_limit} bytes); "
            f"measured at least {full_diff_bytes} bytes. "
            "Increase audit.max_diff_hard_limit_bytes and requeue."
        )

    return diff, changed_files


def _resolve_command() -> str:
    return os.environ.get("CODE_MOWER_DEVIN_CLI_COMMAND", DEFAULT_DEVIN_COMMAND).strip() or DEFAULT_DEVIN_COMMAND


def _resolve_model() -> str:
    for name in (
        "CODE_MOWER_DEVIN_CLI_MODEL",
        "DEVIN_CLI_MODEL",
        "DEVIN_MODEL",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _is_excluded_author(author: str) -> bool:
    authors_env = os.environ.get("DEVIN_CLI_BOT_AUTHORS", "")
    if authors_env:
        default_authors = tuple(a.strip().lower() for a in authors_env.split(",") if a.strip())
    else:
        default_authors = (
            "devin-cli-audit-bot",
            "devin-cli-audit-bot[bot]",
            "devin-ai-integration",
            "devin-ai-integration[bot]",
        )
    return author.strip().lower() in default_authors


@dataclass
class DevinCliVerdict:
    verdict: str  # PASS, BLOCKED, or UNKNOWN
    prose: str
    summary: str = ""
    findings: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    p0_count: int = 0
    p1_count: int = 0
    p2_count: int = 0
    p3_count: int = 0
    mismatch_note: str = ""

    @property
    def blocker_count(self) -> int:
        return self.p0_count + self.p1_count + self.p2_count


def _unknown_verdict(reason: str) -> DevinCliVerdict:
    return DevinCliVerdict(
        verdict="UNKNOWN",
        prose=f"Devin CLI Audit Result — INCOMPLETE\n\n{one_line(reason, MAX_SUMMARY_CHARS)}",
        summary=reason,
    )


def build_prompt(
    *,
    repo: str,
    pr_number: int,
    pr_meta: Dict[str, Any],
    head_sha: str,
    diff: str,
    prompt_lenses: Tuple[str, ...],
    prompt_dir: Optional[Path] = None,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    max_diff_hard_limit_bytes: int = DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES,
    display_name: str = "Devin CLI",
) -> tuple[str, Dict[str, Any]]:
    review_prompt = code_mower_prompts.load_review_prompt(
        prompt_lenses,
        prompt_dir=prompt_dir,
    )
    # The diff was already bounded at the hard limit by _resolve_diff. Include
    # the complete diff whenever it fits within the hard limit, even when it
    # exceeds the normal target, so a partial review can never become a
    # trustworthy PASS. Fail closed only when the hard limit is exceeded.
    full_bytes = len(diff.encode("utf-8", errors="replace"))
    if full_bytes > max_diff_hard_limit_bytes:
        raise NoTrustworthyVerdictError(
            "PR diff exceeds the configured hard limit "
            f"({max_diff_hard_limit_bytes} bytes); measured {full_bytes} bytes. "
            "Increase audit.max_diff_hard_limit_bytes and requeue."
        )
    clipped_diff = diff
    truncated = False
    included_bytes = full_bytes
    adaptive_expanded = full_bytes > max_diff_bytes
    body = str(pr_meta.get("body") or "").strip() or "(empty)"
    title = str(pr_meta.get("title") or "").strip() or "(untitled)"

    prompt = f"""You are the {display_name} informational reviewer inside Code Mower.

This is an informational, non-merge-authority audit. Do not claim merge
authority, do not ask the operator to run tests, and do not use any source-edit
tools. Review the PR for bugs, security issues, and correctness problems that
CI is unlikely to catch.

# Code Mower Review Doctrine

{review_prompt.strip()}

# Required Response

Return exactly one JSON object with this shape and no markdown, no code fences,
and no commentary outside the JSON object:

{{
  "verdict": "pass" | "blocked",
  "summary": "short summary",
  "findings": [
    {{
      "severity": "P0" | "P1" | "P2" | "P3",
      "title": "short finding title",
      "file": "path/from/repo",
      "line": 1,
      "detail": "specific reason this matters"
    }}
  ]
}}

Use verdict "blocked" if any P0, P1, or P2 finding is present. Use "pass" only
when there are no P0/P1/P2 findings. Keep PASS terse and do not pad it with
low-signal notes. Use P3 for non-blocking observations only.

# Pull Request

Repository: {repo}
PR: #{pr_number}
Head SHA: {head_sha}
Title: {title}

Body:
{body}

# Diff

```diff
{clipped_diff}
```
"""
    diagnostics = {
        "full_diff_bytes": full_bytes,
        "included_diff_bytes": included_bytes,
        "max_diff_bytes": max_diff_bytes,
        "max_diff_hard_limit_bytes": max_diff_hard_limit_bytes,
        "diff_truncated": truncated,
        "adaptive_expanded": adaptive_expanded,
        "prompt_lenses": list(prompt_lenses),
        "prompt_bytes": len(prompt.encode("utf-8")),
    }
    return prompt, diagnostics


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    # Try the whole output first.
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    # Try a fenced JSON block.
    match = FENCE_RE.search(text)
    if match:
        try:
            loaded = json.loads(match.group(1).strip())
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
    # Fall back to the first '{' and last '}'.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            loaded = json.loads(text[start : end + 1])
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
    return None


def _validate_devin_cli_verdict(
    data: Dict[str, Any] | None,
    changed_files: set[str],
) -> DevinCliVerdict:
    if not isinstance(data, dict):
        return _unknown_verdict("Devin CLI output did not contain a parseable JSON object")

    key_error = require_exact_keys(data, VERDICT_JSON_KEYS, "top-level object")
    if key_error:
        return _unknown_verdict(f"structured verdict is unusable: {key_error}")

    declared = str(data.get("verdict") or "").strip().lower()
    if declared not in ("pass", "blocked"):
        return _unknown_verdict(f"verdict is {declared!r}, expected 'pass' or 'blocked'")

    summary = str(data.get("summary") or "").strip()
    if not summary or _placeholder_equal(summary):
        return _unknown_verdict("structured verdict summary is empty or placeholder")

    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        return _unknown_verdict("findings must be an array")

    p_counts: Dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
    rendered: List[Dict[str, Any]] = []

    for idx, raw in enumerate(raw_findings):
        where = f"findings[{idx}]"
        if not isinstance(raw, dict):
            return _unknown_verdict(f"{where} is not an object")
        key_error = require_exact_keys(raw, FINDING_KEYS, where)
        if key_error:
            return _unknown_verdict(f"structured verdict is unusable: {key_error}")

        severity = str(raw.get("severity") or "").strip().upper()
        if severity not in {"P0", "P1", "P2", "P3"}:
            return _unknown_verdict(
                f"{where}.severity is {severity!r}, expected P0/P1/P2/P3"
            )

        for field_name in ("title", "file", "detail"):
            field_value = str(raw.get(field_name) or "").strip()
            if not field_value or _placeholder_equal(field_value):
                return _unknown_verdict(
                    f"{where}.{field_name} is empty or placeholder"
                )

        file_path = _normalize_diff_path(raw.get("file"))
        if severity in BLOCKER_SEVERITIES and (
            not file_path or not _diff_file_matches(file_path, changed_files)
        ):
            return _unknown_verdict(
                f"{where}.file {file_path!r} is not present in the PR diff"
            )

        try:
            line = int(raw.get("line"))
        except (TypeError, ValueError):
            line = 0

        p_counts[int(severity[1])] += 1
        if len(rendered) < MAX_RENDERED_FINDINGS:
            rendered.append(
                {
                    "severity": severity,
                    "title": one_line(str(raw.get("title")), MAX_FINDING_TITLE_CHARS),
                    "file": one_line(file_path, MAX_FINDING_FILE_CHARS),
                    "line": line,
                    "detail": clip_text(str(raw.get("detail")), MAX_FINDING_DETAIL_CHARS),
                }
            )

    blocker_count = p_counts[0] + p_counts[1] + p_counts[2]
    if blocker_count > 0:
        verdict = "BLOCKED"
        mismatch_note = (
            "structured verdict declared pass but blocker findings are present"
            if declared == "pass"
            else ""
        )
    elif declared == "pass":
        verdict = "PASS"
        mismatch_note = ""
    else:
        return _unknown_verdict(
            "structured verdict declared blocked but no P0/P1/P2 findings were present"
        )

    prose = _render_structured_prose(summary, rendered, len(raw_findings))

    return DevinCliVerdict(
        verdict=verdict,
        prose=prose,
        summary=summary,
        findings=tuple(rendered),
        p0_count=p_counts[0],
        p1_count=p_counts[1],
        p2_count=p_counts[2],
        p3_count=p_counts[3],
        mismatch_note=mismatch_note,
    )


def _render_structured_prose(
    summary: str,
    findings: List[Dict[str, Any]],
    total_findings: int,
) -> str:
    lines = ["Devin CLI Audit Result — BLOCKED", "", "Summary:", "", clip_text(summary, MAX_SUMMARY_CHARS), ""]
    if not findings:
        lines.append("Findings: none.")
    else:
        lines.extend(["Findings:", ""])
        for finding in findings:
            lines.append(
                f"- [{finding['severity']}] {finding['title']} -- "
                f"`{finding['file']}:{finding['line']}`"
            )
            for detail_line in finding["detail"].splitlines():
                lines.append(f"  {detail_line}")
    omitted = total_findings - len(findings)
    if omitted > 0:
        lines.extend([
            "",
            f"... {omitted} additional finding(s) omitted from the comment "
            "to stay within GitHub's comment limits.",
        ])
    return "\n".join(lines)


def _render_pass_prose(summary: str) -> str:
    return (
        "Devin CLI Audit Result — PASS\n\n"
        "Summary:\n\n"
        f"{clip_text(summary, MAX_SUMMARY_CHARS)}"
    )


def _render_unknown_prose(reason: str) -> str:
    return (
        "Devin CLI Audit Result — INCOMPLETE\n\n"
        "Summary:\n\n"
        f"{one_line(reason, MAX_SUMMARY_CHARS)}"
    )


def _run_devin_cli(
    *,
    command: str,
    prompt: str,
    model: str,
    cwd: Path,
    timeout: int,
) -> tuple[str, int, float]:
    with tempfile.TemporaryDirectory(prefix="code-mower-devin-cli-") as tmp:
        prompt_path = Path(tmp) / "audit.prompt"
        prompt_path.write_text(prompt, encoding="utf-8")
        prompt_path.chmod(0o600)

        argv = [
            command,
            "--prompt-file",
            str(prompt_path),
            "--print",
            "--sandbox",
            "--permission-mode",
            "auto",
            "--respect-workspace-trust",
            "false",
        ]
        if model:
            argv.extend(["--model", model])

        child_env = build_allowlisted_child_env(
            ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM"),
            preserve_ambient_home=True,
        )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise NoTrustworthyVerdictError(
                f"Devin CLI timed out after {timeout}s; no trustworthy verdict available"
            ) from exc
        duration = time.monotonic() - started
        # Raw stderr is captured only for subprocess plumbing; it is never
        # returned to callers so it cannot reach comments or artifacts.
        return completed.stdout, completed.returncode, duration


def _parse_devin_output(
    stdout: str,
    changed_files: set[str],
) -> DevinCliVerdict:
    if not stdout.strip():
        return _unknown_verdict("Devin CLI produced no output")
    data = _extract_json(stdout)
    if data is None:
        return _unknown_verdict("Devin CLI output did not contain a JSON verdict")
    return _validate_devin_cli_verdict(data, changed_files)


@dataclass
class AuditConfig:
    github_token: str
    repo: str
    pr_number: int
    repo_paths: Dict[str, Path]
    command: str = DEFAULT_DEVIN_COMMAND
    model: str = ""
    base_ref: str = DEFAULT_BASE_REF
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES
    max_diff_hard_limit_bytes: int = DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES
    dry_run: bool = False
    allow_dirty: bool = False
    prompt_lenses: Tuple[str, ...] = field(default_factory=lambda: DEFAULT_PROMPT_LENSES)
    prompt_dir: Optional[Path] = None
    actions_run_id: Optional[str] = None
    calibration_badge: str = ""


@dataclass
class AuditResult:
    repo: str
    pr_number: int
    head_sha_start: str
    head_sha_end: str
    verdict: str
    trailer: str
    comment_body: str
    duration_seconds: float
    posted_comment_url: Optional[str] = None
    verdict_artifact_path: Optional[Path] = None


def _build_comment(
    *,
    provider_name: str,
    head_sha: str,
    verdict_text: str,
    trailer: str,
    merge_authority: bool = False,
    actions_run_id: Optional[str] = None,
    diff_notice: str = "",
    context_notice: str = "",
    calibration_badge: str = "",
) -> str:
    header = format_audit_comment_header(
        provider_name=provider_name,
        head_sha=head_sha,
        merge_authority=merge_authority,
        actions_run_id=actions_run_id,
        diff_notice=diff_notice,
        context_notice=context_notice,
        calibration_badge=calibration_badge,
    )
    body = f"{header}\n{verdict_text}\n\n{trailer}"
    return limit_comment_body(body, trailer, provider_name=provider_name)


def _post_audit_comment(
    repo: str,
    pr_number: int,
    body: str,
    *,
    token: str,
    actions_run_id: Optional[str],
) -> tuple[Dict[str, Any], str]:
    posted = post_pr_comment(repo, pr_number, body, token=token)
    if not actions_run_id:
        return posted, body
    bound_body = bind_actions_run_comment_id(body, posted.get("id"))
    return posted, bound_body


def _write_artifact(
    lane_id: str,
    repo: str,
    pr_number: int,
    head_sha_start: str,
    head_sha_end: str,
    verdict: str,
    trailer: str,
    comment_body: str,
    duration_seconds: float,
) -> Optional[Path]:
    return write_audit_verdict_artifact(
        lane_id=lane_id,
        repo=repo,
        pr_number=pr_number,
        head_sha_start=head_sha_start,
        head_sha_end=head_sha_end,
        verdict=verdict.lower(),
        trailer=trailer,
        comment_body=comment_body,
        duration_seconds=duration_seconds,
    )


def _do_audit_pr(config: AuditConfig) -> AuditResult:
    """Run a non-interactive Devin CLI audit and post the verdict."""
    pr_meta = fetch_pull_request(config.repo, config.pr_number, token=config.github_token)
    pr_head_sha = str(pr_meta.get("head", {}).get("sha") or "")
    if not pr_head_sha:
        raise ValueError("GitHub pull request response did not include head.sha")

    pr_author = str(((pr_meta.get("user") or {}).get("login")) or "").strip()
    if pr_author and _is_excluded_author(pr_author):
        raise AuthorExcludedError(
            f"PR author {pr_author!r} is excluded from the Devin CLI reviewer lane"
        )

    if str(pr_meta.get("head", {}).get("repo", {}).get("full_name") or "") != config.repo:
        raise ValueError("PR head repository does not match the target repository")

    repo_path = validate_repo_path_for_wrapper(config.repo_paths, config.repo)
    if not repo_path.is_dir():
        raise ValueError(f"PR head checkout does not exist: {repo_path}")

    verify = verify_checkout_at_head(
        repo_path,
        expected_head_sha=pr_head_sha,
        allow_dirty=config.allow_dirty,
        purpose="Devin CLI review",
    )
    head_sha_start = verify["local_head_sha"]

    diff, changed_files_tuple = _resolve_diff(
        repo_path,
        config.pr_number,
        config.base_ref,
        head_sha_start,
        config.max_diff_bytes,
        config.max_diff_hard_limit_bytes,
    )

    prompt, diagnostics = build_prompt(
        repo=config.repo,
        pr_number=config.pr_number,
        pr_meta=pr_meta,
        head_sha=head_sha_start,
        diff=diff,
        prompt_lenses=config.prompt_lenses,
        prompt_dir=config.prompt_dir,
        max_diff_bytes=config.max_diff_bytes,
        max_diff_hard_limit_bytes=config.max_diff_hard_limit_bytes,
    )

    changed_files = set(changed_files_tuple)

    command = _resolve_command() if config.command == DEFAULT_DEVIN_COMMAND else config.command
    model = config.model or _resolve_model()

    stdout, returncode, duration = _run_devin_cli(
        command=command,
        prompt=prompt,
        model=model,
        cwd=repo_path,
        timeout=config.timeout,
    )

    if returncode != 0:
        # A non-zero returncode means the Devin CLI did not produce a bounded
        # review. Treat this as UNKNOWN/needs-audit rather than PASS.
        raise NoTrustworthyVerdictError(
            f"Devin CLI exited with code {returncode}; no trustworthy verdict available"
        )

    parsed = _parse_devin_output(stdout, changed_files)

    # Re-verify exact head and cleanliness after the model run before any
    # PASS can be posted.
    head_sha_end = local_head_sha(repo_path)
    if head_sha_end.lower() != head_sha_start.lower():
        raise StaleHeadError(
            f"PR head moved during Devin CLI audit: {head_sha_start} -> {head_sha_end}"
        )
    if working_tree_status(repo_path).strip():
        raise NoTrustworthyVerdictError(
            "local checkout has uncommitted changes after Devin CLI review"
        )

    # Also re-fetch from GitHub to detect a superseding push.
    current_meta = fetch_pull_request(config.repo, config.pr_number, token=config.github_token)
    current_head = str(current_meta.get("head", {}).get("sha") or "")
    if current_head and current_head.lower() != head_sha_start.lower():
        raise StaleHeadError(
            f"PR head moved on GitHub during the audit: {head_sha_start} -> {current_head}"
        )

    if parsed.verdict == "PASS":
        trailer = PASS_TRAILER
        verdict_text = _render_pass_prose(parsed.summary)
    elif parsed.verdict == "BLOCKED":
        trailer = BLOCKED_TRAILER
        verdict_text = parsed.prose
    else:
        trailer = NEEDS_TRAILER
        verdict_text = _render_unknown_prose(
            parsed.mismatch_note or "Devin CLI returned an unusable verdict"
        )

    diff_notice = (
        f"local checkout diff; {diagnostics['included_diff_bytes']} of "
        f"{diagnostics['full_diff_bytes']} bytes"
    )
    if diagnostics.get("adaptive_expanded"):
        diff_notice += (
            "; expanded above the normal target "
            f"({diagnostics['max_diff_bytes']} bytes) within the hard limit "
            f"({diagnostics['max_diff_hard_limit_bytes']} bytes)"
        )
    context_notice = "read-only review; prompt file with mode 0600, empty stdin, no source-edit tools"

    comment_body = _build_comment(
        provider_name="Devin CLI",
        head_sha=head_sha_end,
        verdict_text=verdict_text,
        trailer=trailer,
        actions_run_id=config.actions_run_id,
        diff_notice=diff_notice,
        context_notice=context_notice,
        calibration_badge=config.calibration_badge,
    )

    if not config.dry_run:
        _, comment_body = _post_audit_comment(
            config.repo,
            config.pr_number,
            comment_body,
            token=config.github_token,
            actions_run_id=config.actions_run_id,
        )

    artifact_path = _write_artifact(
        lane_id="devin_cli",
        repo=config.repo,
        pr_number=config.pr_number,
        head_sha_start=head_sha_start,
        head_sha_end=head_sha_end,
        verdict=parsed.verdict,
        trailer=trailer,
        comment_body=comment_body,
        duration_seconds=duration,
    )

    return AuditResult(
        repo=config.repo,
        pr_number=config.pr_number,
        head_sha_start=head_sha_start,
        head_sha_end=head_sha_end,
        verdict=parsed.verdict,
        trailer=trailer,
        comment_body=comment_body,
        duration_seconds=duration,
        posted_comment_url=None if config.dry_run else None,
        verdict_artifact_path=artifact_path,
    )




def audit_pr(config: AuditConfig) -> AuditResult:
    """Run a non-interactive Devin CLI audit and post the verdict."""
    try:
        return _do_audit_pr(config)
    except (
        AuthorExcludedError,
        StaleHeadError,
        NoTrustworthyVerdictError,
        MalformedOutputError,
        ProviderWorkspaceError,
    ) as exc:
        reason = str(exc)
        head_sha = ""
        try:
            pr_meta = fetch_pull_request(config.repo, config.pr_number, token=config.github_token)
            head_sha = str(pr_meta.get("head", {}).get("sha") or "")
        except Exception:
            head_sha = ""
        if not head_sha:
            # Without a trusted PR-head context no comment can be posted
            # safely; surface a hard configuration error instead.
            raise
        # Always render a bounded UNKNOWN comment body, even in dry-run mode,
        # so the needs-audit trailer is observable without posting.
        comment_body = _build_comment(
            provider_name="Devin CLI",
            head_sha=head_sha,
            verdict_text=_render_unknown_prose(reason),
            trailer=NEEDS_TRAILER,
            actions_run_id=config.actions_run_id,
            calibration_badge=config.calibration_badge,
        )
        artifact_path: Optional[Path] = None
        if not config.dry_run:
            _, comment_body = _post_audit_comment(
                config.repo,
                config.pr_number,
                comment_body,
                token=config.github_token,
                actions_run_id=config.actions_run_id,
            )
            artifact_path = _write_artifact(
                lane_id="devin_cli",
                repo=config.repo,
                pr_number=config.pr_number,
                head_sha_start=head_sha,
                head_sha_end=head_sha,
                verdict="unknown",
                trailer=NEEDS_TRAILER,
                comment_body=comment_body,
                duration_seconds=0.0,
            )
        return AuditResult(
            repo=config.repo,
            pr_number=config.pr_number,
            head_sha_start=head_sha,
            head_sha_end=head_sha,
            verdict="UNKNOWN",
            trailer=NEEDS_TRAILER,
            comment_body=comment_body,
            duration_seconds=0.0,
            verdict_artifact_path=artifact_path,
        )
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--repo-paths", required=True, help="OWNER/REPO:/absolute/path,...")
    parser.add_argument(
        "--read-token-from-stdin",
        action="store_true",
        help="read the GitHub token from the first line of stdin",
    )
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument(
        "--command",
        default=DEFAULT_DEVIN_COMMAND,
        help="Devin CLI command (default: devin)",
    )
    parser.add_argument("--model", default="", help="model override")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-diff-bytes", type=int, default=DEFAULT_MAX_DIFF_BYTES)
    parser.add_argument(
        "--max-diff-hard-limit-bytes",
        type=int,
        default=DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--prompt-lenses",
        default=",".join(DEFAULT_PROMPT_LENSES),
    )
    parser.add_argument("--prompt-dir", type=Path, default=None)
    parser.add_argument(
        "--actions-run-id",
        default=os.environ.get("GITHUB_RUN_ID", ""),
        help="GitHub Actions run id for the audit-run trailer",
    )
    parser.add_argument(
        "--calibration-badge",
        default="",
        help="optional human-facing calibration badge text",
    )
    args = parser.parse_args(argv)

    token = resolve_github_token_from_stdin_or_env(args.read_token_from_stdin)
    if not token:
        print(
            "error: set GITHUB_TOKEN or pass --read-token-from-stdin",
            file=sys.stderr,
        )
        return 1

    repo_paths = parse_repo_paths(args.repo_paths)
    prompt_lenses = code_mower_prompts.split_lenses(args.prompt_lenses)

    config = AuditConfig(
        github_token=token,
        repo=args.repo,
        pr_number=args.pr,
        repo_paths=repo_paths,
        command=args.command,
        model=args.model,
        base_ref=args.base_ref,
        timeout=args.timeout,
        max_diff_bytes=args.max_diff_bytes,
        max_diff_hard_limit_bytes=args.max_diff_hard_limit_bytes,
        dry_run=args.dry_run,
        allow_dirty=args.allow_dirty,
        prompt_lenses=prompt_lenses,
        prompt_dir=args.prompt_dir,
        actions_run_id=args.actions_run_id or None,
        calibration_badge=args.calibration_badge,
    )

    try:
        result = audit_pr(config)
        if args.dry_run:
            print(result.comment_body)
        return audit_exit_code(result.verdict)
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        urllib.error.URLError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
