#!/usr/bin/env python3
"""Local LLM audit CLI — review a single PR.

Standalone command + Python module. Reviews ONE PR end-to-end:

  1. Fetch PR metadata + per-file diffs from GitHub API.
  2. For each changed file (under size budget): fetch full file content at
     head SHA. This is the "local checkout" context Codex flagged as
     necessary — diff-only review is too weak.
  3. Per-file LLM pass: prompt sees full file content + diff hunks + PR
     title/body. Asks the LLM to classify findings as BLOCKER / CONCERN /
     NONE for that file.
  4. Synthesis LLM pass: gathers per-file findings + cross-cutting
     concerns (test coverage, doc-index, lane discipline) → final verdict.
  5. Refetch head SHA at end. If it changed mid-review, emit the
     `needs-local-llm-audit` trailer instead of PASS/BLOCKED.
  6. Post a structured comment ending with the authoritative trailer.

CLI usage:

    GITHUB_TOKEN=... python3 tools/local_llm_audit_pr.py \
        --repo owner/repo --pr 142

Module usage (called by `local_llm_audit_bridge.py`):

    from tools.local_llm_audit_pr import AuditConfig, audit_pr
    result = audit_pr(config, "owner/repo", 142)
    # result.posted_comment_url, result.verdict, result.trailer

Exit codes (CLI mode):
    0  comment posted (or dry-run printed)
    1  generic error (config, network, API)
    2  stale head SHA detected mid-review (caller may requeue)

Severity contract (REQUIRED in the prompt):
    BLOCKER  — must fix before merge (correctness, test gap, secrets, etc.)
    CONCERN  — non-blocking observation
    NONE     — no issues

Final verdict:
    BLOCKED  — any BLOCKER findings
    PASS     — no BLOCKERs (CONCERNs allowed and surfaced)

Codex's "don't approve by vibes" safety net is enforced around reviewer
coverage: parse failures, missing changed-file coverage, and unreviewed file
budgets become blockers. PR-description claims are handled with more nuance:
unverifiable claims are blockers only when the missing evidence could hide a
correctness, security, schema, or test-coverage gap; otherwise they are
concerns so informational local models do not block on harmless release prose.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import local_llm_profiles
        from code_mower import prompts as code_mower_prompts
        from code_mower.provider_runners import (
            fetch_pull_request as _fetch_pull_request,
            fetch_pull_request_files as _fetch_pull_request_files,
            resolve_github_token_from_env_or_gh,
        )
    else:
        from tools import code_mower_prompts, local_llm_profiles
        from tools.provider_runners import (
            fetch_pull_request as _fetch_pull_request,
            fetch_pull_request_files as _fetch_pull_request_files,
            resolve_github_token_from_env_or_gh,
        )
elif __package__ == "tools":
    from tools import code_mower_prompts, local_llm_profiles
    from tools.provider_runners import (
        fetch_pull_request as _fetch_pull_request,
        fetch_pull_request_files as _fetch_pull_request_files,
        resolve_github_token_from_env_or_gh,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import local_llm_profiles
    from . import prompts as code_mower_prompts
    from .provider_runners import (
        fetch_pull_request as _fetch_pull_request,
        fetch_pull_request_files as _fetch_pull_request_files,
        resolve_github_token_from_env_or_gh,
    )


# ----- Configuration / defaults -----

DEFAULT_API_BASE = "http://localhost:1234/v1"  # LM Studio
DEFAULT_MODEL = "qwen/qwen3-coder-next"  # LM Studio model id; override via LOCAL_LLM_MODEL
DEFAULT_API_KEY = "EMPTY"
DEFAULT_HTTP_TIMEOUT = 600
DEFAULT_PROMPT_REF = "origin/main"
DEFAULT_BASE_REF = "origin/main"

# File-size budgets. Files over MAX_FILE_BYTES are referenced by name only;
# the LLM sees the diff hunks but not the full content. Files under that limit
# are included in full.
MAX_FILE_BYTES = 60_000          # ~1500 lines of typical code
MAX_FILES_PER_REVIEW = 25        # synthesis pass struggles past ~25 per-file findings
MAX_PER_FILE_PROMPT_TOKENS = 6000  # rough budget; we truncate content if hit
MAX_FETCHED_PR_FILES = 500       # GitHub pulls/files pagination cap in fetch_pr_files()


# ----- Data classes -----


@dataclass
class AuditConfig:
    github_token: str
    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    api_key: str = DEFAULT_API_KEY
    http_timeout: int = DEFAULT_HTTP_TIMEOUT
    dry_run: bool = False
    max_file_bytes: int = MAX_FILE_BYTES
    max_files: int = MAX_FILES_PER_REVIEW
    profile_id: str = ""
    context_window: int = 0
    json_repair_retries: int = 1
    prompt_lenses: tuple[str, ...] = field(
        default_factory=lambda: code_mower_prompts.DEFAULT_REVIEW_LENSES
    )
    prompt_dir: Optional[Path] = None
    prompt_ref: str = DEFAULT_PROMPT_REF
    prompt_repo: Optional[Path] = None
    repo_path: Optional[Path] = None
    base_ref: str = DEFAULT_BASE_REF
    allow_historical_head: bool = False


@dataclass
class FileFinding:
    """Per-file finding emitted by the per-file LLM pass."""
    path: str
    status: str  # "added" | "modified" | "removed" | "renamed" | "skipped-binary" | "skipped-toobig"
    blockers: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    raw_response: str = ""  # for debugging / dry-run inspection
    parse_attempts: int = 1
    json_repair_used: bool = False
    parse_failed: bool = False

    def has_blocker(self) -> bool:
        return bool(self.blockers)


@dataclass
class AuditResult:
    repo: str
    pr_number: int
    head_sha_start: str
    head_sha_end: str
    file_findings: List[FileFinding]
    synthesis_response: str
    verdict: str        # "PASS" | "BLOCKED" | "STALE"
    trailer: str        # the full HTML-comment trailer line
    comment_body: str
    posted_comment_url: Optional[str] = None
    # PR-level blockers — issues that aren't tied to a single file but that
    # gate the verdict. Currently used for the file-truncation case (PR has
    # more changed files than config.max_files): Codex blocker on #231
    # required this to be surfaced rather than silently dropped.
    pr_level_blockers: List[str] = field(default_factory=list)

    def head_changed_during_review(self) -> bool:
        return self.head_sha_start != self.head_sha_end


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
    """Single GitHub REST call. Returns parsed JSON, or text for diff Accept."""
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
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body_bytes = response.read()
        if accept.endswith("diff"):
            return body_bytes.decode("utf-8", errors="replace")
        text = body_bytes.decode("utf-8")
        return json.loads(text) if text else None


def fetch_pull_request(repo: str, pr_number: int, *, token: str) -> Dict[str, Any]:
    payload = _fetch_pull_request(repo, pr_number, token=token)
    if not isinstance(payload, dict):
        raise ValueError("GitHub pull request response was not an object")
    return payload


def fetch_pr_files(repo: str, pr_number: int, *, token: str) -> List[Dict[str, Any]]:
    """Return per-file diff entries (status, filename, patch, etc.)."""

    return _fetch_pull_request_files(
        repo,
        pr_number,
        token=token,
        max_pages=MAX_FETCHED_PR_FILES // 100,
        per_page=100,
    )


def fetch_file_content(repo: str, path: str, ref: str, *, token: str) -> Optional[bytes]:
    """Fetch raw file content at a given ref. Returns None if not found (e.g.
    file deleted in the PR), or if the path is a directory / symlink / submodule."""
    try:
        meta = _gh_request(
            "GET",
            f"/repos/{repo}/contents/{path}?ref={ref}",
            token=token,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(meta, dict):
        # Directory listing — we asked for a path that's actually a directory.
        return None
    if meta.get("type") != "file":
        return None
    encoding = meta.get("encoding", "")
    content = meta.get("content", "")
    if encoding == "base64":
        try:
            return base64.b64decode(content)
        except (ValueError, TypeError):
            return None
    # Large files: GitHub returns a download_url instead of inline content.
    download_url = meta.get("download_url")
    if download_url:
        try:
            with urllib.request.urlopen(download_url, timeout=30) as response:
                return response.read()
        except urllib.error.URLError:
            return None
    return None


def _git(
    repo_path: Path,
    args: List[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )


def _local_head_sha(repo_path: Path) -> str:
    return _git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()


def _local_file_content(repo_path: Path, path: str) -> Optional[bytes]:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    resolved_repo_path = repo_path.expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(resolved_repo_path), "show", f"HEAD:{path}"],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return completed.stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _status_name(code: str) -> str:
    if code.startswith("A"):
        return "added"
    if code.startswith("D"):
        return "removed"
    if code.startswith("R"):
        return "renamed"
    if code.startswith("C"):
        return "copied"
    return "modified"


def fetch_local_pr_files(repo_path: Path, *, base_ref: str) -> List[Dict[str, Any]]:
    resolved_repo_path = repo_path.expanduser().resolve()
    status_lines = _git(
        resolved_repo_path,
        ["diff", "--name-status", "--find-renames", f"{base_ref}...HEAD"],
    ).stdout.splitlines()
    files: List[Dict[str, Any]] = []
    for line in status_lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_code = parts[0]
        filename = parts[-1]
        patch = _git(
            resolved_repo_path,
            [
                "diff",
                "--no-ext-diff",
                "--find-renames",
                f"{base_ref}...HEAD",
                "--",
                filename,
            ],
            timeout=120,
        ).stdout
        files.append(
            {
                "filename": filename,
                "status": _status_name(status_code),
                "patch": patch,
            }
        )
    return files


def post_pr_comment(repo: str, pr_number: int, body: str, *, token: str) -> Dict[str, Any]:
    return _gh_request(
        "POST",
        f"/repos/{repo}/issues/{pr_number}/comments",
        token=token,
        body={"body": body},
    )


# ----- LLM call -----


def call_llm(config: AuditConfig, system: str, user: str, *, max_tokens: int = 2048) -> str:
    """Single LLM chat-completion. Returns raw text."""
    req_body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    data = json.dumps(req_body).encode("utf-8")
    req = urllib.request.Request(
        f"{config.api_base.rstrip('/')}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.http_timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


# ----- Prompts -----


PER_FILE_SYSTEM_PROMPT = """\
You are an automated code reviewer. Your job is to find BLOCKERS — issues
that must be fixed before merge — and surface non-blocking CONCERNS.

You will see ONE file at a time: its full current contents (at PR head)
plus the unified diff of what changed. You also see the PR's title and
body for context.

You MUST classify every observation into one of three severities:

    BLOCKER  — must fix before merge. Examples:
                 - correctness bug introduced by the change
                 - missing test for new behavior (when the PR claims tests)
                 - schema / contract break
                 - committed secret or credential
                 - claim in PR body cannot be verified from the visible code
                   AND the missing evidence could hide a correctness,
                   security, schema, or test-coverage gap
    CONCERN  — non-blocking observation (style, naming, refactor opportunity)
                 - PR prose claims that are not fully visible but do not
                   create a concrete merge risk
    NONE     — no issues to report

Output STRICTLY in this JSON format and NOTHING else (no prose around it,
no markdown fences):

    {"blockers": ["...", "..."], "concerns": ["...", "..."]}

Empty arrays are fine. Each entry is one specific finding, ideally with
file location ("line ~123: ...") and what the fix would be.
"""


PER_FILE_REPAIR_SYSTEM_PROMPT = PER_FILE_SYSTEM_PROMPT + """\

Your previous response for this same file was not parseable JSON. Re-review
the file and return ONLY the strict JSON object. Do not include markdown,
commentary, or a code fence.
"""


SYNTHESIS_SYSTEM_PROMPT = """\
You are an automated code reviewer producing the FINAL verdict for a
pull request. The per-file pass already produced findings for each
changed file. Your job is two things:

  1. Surface CROSS-FILE / PR-LEVEL issues the per-file pass cannot see:
       - test coverage: does the changed behavior have tests that
         actually exercise the changed code paths?
       - doc-index consistency: did the PR add tools/fixtures that
         should be registered in README / index docs?
       - lane discipline: does the PR touch paths that are owned by
         a different lane (e.g., Codex-owned production code in a
         Claude PR) without coordination?
       - secrets / config: any `.env`, credentials, or tokens?
       - schema / regression-gate: if production geometry / recognizer
         behavior changed, does the PR include the row-level baseline
         diff (not just aggregate A/B)?

  2. Produce the FINAL human-readable comment.

Severity rules (same as per-file pass):
    BLOCKER → must fix before merge
    CONCERN → non-blocking
    NONE    → no issues

Final verdict logic:
    Any BLOCKER (from per-file findings OR your cross-cutting pass) → BLOCKED
    Else                                                            → PASS

Output STRICTLY in this format (no JSON, no markdown fences around the
whole output — but you may use markdown WITHIN the human-readable body):

Local LLM Audit: <PASS or BLOCKED>

<one or two sentence headline>

<detailed body — markdown OK. List BLOCKERs first if any, then CONCERNs,
then "Cross-cutting notes" with anything from your PR-level pass.>

<!-- LOCAL_LLM_AUDIT_STATE: local-llm-audit-done -->
or
<!-- LOCAL_LLM_AUDIT_STATE: local-llm-audit-blocked -->

Pick exactly ONE trailer matching the verdict. The trailer MUST be the
final line of your output.

Important:
- Do not approve by vibes. If a claim made in the PR body is unverifiable and
  could hide a correctness, security, schema, or test-coverage gap, treat it as
  a BLOCKER ("cannot verify: ...").
- If an unverifiable claim is just release prose or background context with no
  concrete merge risk, list it as a CONCERN rather than blocking the PR.
- If you only have CONCERNs (no BLOCKERs), the verdict is PASS, and
  the CONCERNs are listed in the body for the author to consider.
"""


def _review_doctrine(config: AuditConfig) -> str:
    return code_mower_prompts.load_review_prompt(
        config.prompt_lenses,
        prompt_dir=config.prompt_dir,
        trusted_git_ref=None if config.prompt_dir else config.prompt_ref,
        repo_root=config.prompt_repo or Path.cwd(),
        missing_ok=True,
    )


def _with_review_doctrine(system_prompt: str, doctrine: str) -> str:
    return code_mower_prompts.append_review_prompt(system_prompt, doctrine)


def _safe_decode(content_bytes: bytes) -> Tuple[Optional[str], str]:
    """Return (decoded_text, reason) where reason is 'ok', 'binary', or 'too-big'."""
    if b"\x00" in content_bytes[:8192]:
        return None, "binary"
    try:
        return content_bytes.decode("utf-8"), "ok"
    except UnicodeDecodeError:
        return None, "binary"


def build_per_file_user_prompt(
    file_meta: Dict[str, Any],
    file_content: Optional[str],
    pr_meta: Dict[str, Any],
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> str:
    """Compose the per-file LLM prompt."""
    patch = file_meta.get("patch", "") or "(no patch — likely a binary or rename-only)"
    status = file_meta.get("status", "modified")
    path = file_meta.get("filename", "?")

    if file_content is None:
        content_block = "(file content not available — file may have been deleted,\nbe binary, exceed size limit, or be a directory/submodule)"
    elif len(file_content) > max_file_bytes:
        content_block = (
            file_content[:max_file_bytes]
            + f"\n\n... (truncated: file is {len(file_content)} bytes, showing first {max_file_bytes})\n"
        )
    else:
        content_block = file_content

    pr_title = pr_meta.get("title", "")
    pr_body = (pr_meta.get("body") or "").strip()
    pr_body_short = pr_body[:2000] + ("\n... (truncated)\n" if len(pr_body) > 2000 else "")

    return textwrap.dedent(
        """\
        PR title: {title}
        PR body (truncated to 2000 chars):
        ---
        {body}
        ---

        File path: {path}
        Status: {status}

        ## Full file content at PR head SHA

        ```
        {content}
        ```

        ## Diff (unified)

        ```diff
        {patch}
        ```

        Review this single file. Output the JSON object only.
        """
    ).format(
        title=pr_title,
        body=pr_body_short or "(empty)",
        path=path,
        status=status,
        content=content_block,
        patch=patch,
    )


def build_synthesis_user_prompt(
    pr_meta: Dict[str, Any],
    findings: List[FileFinding],
    pr_level_blockers: Optional[List[str]] = None,
) -> str:
    """Compose the synthesis LLM prompt."""
    pr_level_blockers = pr_level_blockers or []
    pr_title = pr_meta.get("title", "")
    pr_body = (pr_meta.get("body") or "").strip()
    pr_body_short = pr_body[:3000] + ("\n... (truncated)\n" if len(pr_body) > 3000 else "")
    repo = pr_meta.get("base", {}).get("repo", {}).get("full_name", "?")
    head_sha = pr_meta.get("head", {}).get("sha", "?")

    file_list_lines = []
    findings_lines = []
    for f in findings:
        file_list_lines.append(f"- `{f.path}` ({f.status})")
        if f.blockers or f.concerns:
            findings_lines.append(f"### {f.path}")
            for b in f.blockers:
                findings_lines.append(f"- BLOCKER: {b}")
            for c in f.concerns:
                findings_lines.append(f"- CONCERN: {c}")
            findings_lines.append("")
        elif f.status.startswith("skipped"):
            findings_lines.append(f"### {f.path}")
            findings_lines.append(f"- {f.status} (no review)")
            findings_lines.append("")

    files_block = "\n".join(file_list_lines) or "(none)"
    findings_block = "\n".join(findings_lines) or "(per-file pass found no issues)"

    pr_level_block_text = ""
    if pr_level_blockers:
        pr_level_lines = ["## PR-level BLOCKERs (these gate the verdict regardless of per-file findings)\n"]
        for b in pr_level_blockers:
            pr_level_lines.append(f"- {b}")
        pr_level_block_text = "\n".join(pr_level_lines) + "\n\n"

    return textwrap.dedent(
        """\
        Repository: {repo}
        PR #{pr_number}: {title}
        Head SHA: {head_sha}
        Author: {author}
        Files changed ({n_files}):
        {files}

        {pr_level_block}## PR body (truncated to 3000 chars)

        {body}

        ## Per-file findings

        {findings}

        Now produce the FINAL audit comment per your system prompt. Remember:
        - Surface cross-file / PR-level issues the per-file pass cannot see.
        - If you cannot verify a claim in the PR body and the missing evidence
          could hide a correctness, security, schema, or test-coverage gap,
          that's a BLOCKER, not a PASS.
        - If the unverifiable claim does not create a concrete merge risk,
          list it as a CONCERN.
        - If the PR-level BLOCKERs section above is non-empty, the verdict
          MUST be BLOCKED and you must reference those PR-level blockers
          in the body.
        - Final line MUST be the trailer:
            <!-- LOCAL_LLM_AUDIT_STATE: local-llm-audit-done -->   (PASS)
            <!-- LOCAL_LLM_AUDIT_STATE: local-llm-audit-blocked --> (BLOCKED)
        """
    ).format(
        repo=repo,
        pr_number=pr_meta.get("number", "?"),
        title=pr_title,
        head_sha=head_sha,
        author=pr_meta.get("user", {}).get("login", "?"),
        n_files=len(findings),
        files=files_block,
        pr_level_block=pr_level_block_text,
        body=pr_body_short or "(empty)",
        findings=findings_block,
    )


# ----- Response parsing -----


def parse_per_file_response(text: str) -> Optional[Tuple[List[str], List[str]]]:
    """Parse the JSON `{blockers: [...], concerns: [...]}` from the per-file
    pass. Tolerant of extra prose / markdown fences.

    Returns `(blockers, concerns)` on successful parse (either list may be
    empty, meaning "the LLM reviewed this file and found nothing").

    Returns ``None`` when no parseable JSON object was found in the response
    OR when the JSON didn't have the expected shape. Callers MUST treat
    ``None`` differently from `([], [])` — a parse failure is a reviewer
    breakdown, not a clean review. The caller in `review_one_file` converts
    this into a BLOCKER per Codex's "don't approve by vibes" rule on PR #231.
    """
    text = text.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    # If the parsed object doesn't expose at least one of the expected keys,
    # treat as parse failure — the LLM emitted JSON in a format we can't trust.
    if "blockers" not in obj and "concerns" not in obj:
        return None
    blockers = [str(x) for x in obj.get("blockers", []) if x]
    concerns = [str(x) for x in obj.get("concerns", []) if x]
    return blockers, concerns


STALE_TRAILER = "<!-- LOCAL_LLM_AUDIT_STATE: needs-local-llm-audit -->"
DONE_TRAILER = "<!-- LOCAL_LLM_AUDIT_STATE: local-llm-audit-done -->"
BLOCKED_TRAILER = "<!-- LOCAL_LLM_AUDIT_STATE: local-llm-audit-blocked -->"


def ensure_trailer(synthesis_text: str, verdict: str) -> str:
    """Ensure the synthesis response ends with the correct authoritative trailer.
    Strips any other LOCAL_LLM_AUDIT_STATE trailers the LLM may have emitted and
    appends the canonical one for the verdict."""
    canonical = DONE_TRAILER if verdict == "PASS" else BLOCKED_TRAILER
    cleaned_lines = [
        line for line in synthesis_text.splitlines()
        if "LOCAL_LLM_AUDIT_STATE" not in line
    ]
    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()
    cleaned_lines.append("")
    cleaned_lines.append(canonical)
    return "\n".join(cleaned_lines) + "\n"


# ----- Orchestration -----


def _is_likely_binary_filename(path: str) -> bool:
    binary_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac",
        ".pyc", ".so", ".dylib", ".dll", ".exe",
        ".onnx", ".pt", ".pth", ".bin", ".npy", ".npz",
    }
    lower = path.lower()
    return any(lower.endswith(ext) for ext in binary_exts)


def review_one_file(
    config: AuditConfig,
    repo: str,
    head_sha: str,
    file_meta: Dict[str, Any],
    pr_meta: Dict[str, Any],
    *,
    log: bool = True,
    review_doctrine: str | None = None,
) -> FileFinding:
    path = file_meta.get("filename", "?")
    status = file_meta.get("status", "modified")

    if _is_likely_binary_filename(path):
        if log:
            print(f"  skip-binary {path}", file=sys.stderr, flush=True)
        return FileFinding(path=path, status="skipped-binary")

    # Removed files have no content at head; only patch.
    file_content: Optional[str] = None
    if status != "removed":
        raw = (
            _local_file_content(config.repo_path, path)
            if config.repo_path is not None
            else fetch_file_content(repo, path, head_sha, token=config.github_token)
        )
        if raw is None:
            file_content = None
        else:
            text, reason = _safe_decode(raw)
            if reason == "binary":
                if log:
                    print(f"  skip-binary-content {path}", file=sys.stderr, flush=True)
                return FileFinding(path=path, status="skipped-binary")
            if len(raw) > config.max_file_bytes:
                if log:
                    print(f"  truncate-toobig {path} ({len(raw)}B)", file=sys.stderr, flush=True)
            file_content = text

    user_prompt = build_per_file_user_prompt(
        file_meta,
        file_content,
        pr_meta,
        max_file_bytes=config.max_file_bytes,
    )

    if log:
        size_hint = len(file_content) if file_content else 0
        print(f"  review {path} (content={size_hint}ch)", file=sys.stderr, flush=True)

    response = ""
    parsed: Optional[Tuple[List[str], List[str]]] = None
    parse_attempts = 0
    json_repair_used = False
    call_error: Optional[Exception] = None
    doctrine = _review_doctrine(config) if review_doctrine is None else review_doctrine
    prompts = [_with_review_doctrine(PER_FILE_SYSTEM_PROMPT, doctrine)] + [
        _with_review_doctrine(PER_FILE_REPAIR_SYSTEM_PROMPT, doctrine)
        for _ in range(max(0, config.json_repair_retries))
    ]

    for attempt, system_prompt in enumerate(prompts, start=1):
        parse_attempts = attempt
        if attempt > 1:
            json_repair_used = True
            if log:
                print(f"  json-retry {path} attempt={attempt}", file=sys.stderr, flush=True)
        try:
            # 2048 tokens is empirically enough for the verbose-concerns case on
            # files up to ~15K chars (observed on reference-app#142 per-file passes;
            # 1024 was hitting length truncation mid-JSON, which previously
            # silently became PASS — now becomes a parse-failure BLOCKER per
            # Codex's #231 review).
            response = call_llm(config, system_prompt, user_prompt, max_tokens=2048)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            call_error = exc
            break

        parsed = parse_per_file_response(response)
        if parsed is not None:
            break

        preview = response.strip().replace("\n", " ")[:200]
        if log:
            print(
                f"  parse-failure {path} attempt={attempt}: {preview[:80]}",
                file=sys.stderr,
                flush=True,
            )

    if call_error is not None:
        if log:
            print(f"  llm-error {path}: {call_error}", file=sys.stderr, flush=True)
        return FileFinding(
            path=path,
            status=status,
            blockers=[f"Per-file LLM call failed: {call_error}. Cannot review this file."],
            raw_response=response,
            parse_attempts=parse_attempts,
            json_repair_used=json_repair_used,
        )

    if parsed is None:
        # Codex blocker on #231: malformed LLM output must NOT be treated
        # as clean. Emit a BLOCKER so the synthesis pass and final verdict
        # reflect the reviewer's breakdown.
        preview = response.strip().replace("\n", " ")[:200]
        return FileFinding(
            path=path,
            status=status,
            blockers=[
                "Per-file LLM output was not parseable JSON after retry — "
                "cannot trust the verdict for this file. Response preview: "
                f"{preview!r}"
            ],
            raw_response=response,
            parse_attempts=parse_attempts,
            json_repair_used=json_repair_used,
            parse_failed=True,
        )

    blockers, concerns = parsed
    return FileFinding(
        path=path,
        status=status,
        blockers=blockers,
        concerns=concerns,
        raw_response=response,
        parse_attempts=parse_attempts,
        json_repair_used=json_repair_used,
    )


def audit_pr(config: AuditConfig, repo: str, pr_number: int) -> AuditResult:
    """End-to-end audit of one PR. Returns AuditResult (does NOT post by itself
    unless caller wants — this function is pure orchestration plus optional
    posting controlled by config.dry_run).
    """
    pr_meta = fetch_pull_request(repo, pr_number, token=config.github_token)
    pr_head_sha = pr_meta["head"]["sha"]
    if config.repo_path is not None:
        repo_path = config.repo_path.expanduser().resolve()
        head_sha_start = _local_head_sha(repo_path)
        if not config.allow_historical_head and head_sha_start != pr_head_sha:
            raise RuntimeError(
                "local checkout is not at the current PR head; use "
                "allow_historical_head for archived calibration runs"
            )
        pr_meta = dict(pr_meta)
        pr_meta["head"] = {**dict(pr_meta.get("head", {})), "sha": head_sha_start}
    else:
        repo_path = None
        head_sha_start = pr_head_sha

    all_files = (
        fetch_local_pr_files(repo_path, base_ref=config.base_ref)
        if repo_path is not None
        else fetch_pr_files(repo, pr_number, token=config.github_token)
    )
    if repo_path is not None and not all_files:
        raise RuntimeError(
            "local checkout diff produced no files; set --base-ref to the "
            "PR base or merge-base for archived calibration runs"
        )
    pr_level_blockers: List[str] = []
    reported_changed_files = None if repo_path is not None else pr_meta.get("changed_files")
    if isinstance(reported_changed_files, int) and reported_changed_files > len(all_files):
        pr_level_blockers.append(
            f"GitHub reports {reported_changed_files} changed files, but the "
            f"reviewer fetched only {len(all_files)} via the pulls/files API "
            f"pagination cap ({MAX_FETCHED_PR_FILES}). Files beyond the fetch "
            "cap were not reviewed, so this verdict must remain blocked until "
            "the PR is split or the fetcher is expanded."
        )
    if len(all_files) > config.max_files:
        # Codex blocker on #231: silently reviewing the first N files of a
        # bigger PR could allow a blocker in an omitted file to slip past
        # as PASS. Surface this explicitly as a PR-level blocker that gates
        # the verdict; the synthesis prompt also sees the omitted-files list.
        omitted = [f.get("filename", "?") for f in all_files[config.max_files :]]
        pr_level_blockers.append(
            f"PR has {len(all_files)} changed files, exceeding the per-audit "
            f"budget of {config.max_files}. Reviewed only the first "
            f"{config.max_files}; {len(omitted)} files omitted: "
            + ", ".join(omitted[:10])
            + (f", ... ({len(omitted) - 10} more)" if len(omitted) > 10 else "")
            + ". Split the PR into smaller pieces, or re-audit with a larger "
            "max_files setting, before treating this verdict as authoritative."
        )
        files = all_files[: config.max_files]
    else:
        files = all_files

    print(
        f"audit {repo}#{pr_number} head={head_sha_start[:8]} files={len(files)}"
        f"{f' (of {len(all_files)} — truncated)' if pr_level_blockers else ''}",
        file=sys.stderr, flush=True,
    )

    findings: List[FileFinding] = []
    review_doctrine = _review_doctrine(config)
    for file_meta in files:
        finding = review_one_file(
            config,
            repo,
            head_sha_start,
            file_meta,
            pr_meta,
            review_doctrine=review_doctrine,
        )
        findings.append(finding)

    # Stale HEAD check before synthesis. If HEAD changed during the per-file
    # passes, requeue rather than produce a misleading verdict.
    if repo_path is not None:
        head_sha_end = _local_head_sha(repo_path)
    else:
        pr_meta_after = fetch_pull_request(repo, pr_number, token=config.github_token)
        head_sha_end = pr_meta_after["head"]["sha"]

    if head_sha_start != head_sha_end:
        comment_body = _format_stale_comment(repo, pr_number, head_sha_start, head_sha_end)
        result = AuditResult(
            repo=repo,
            pr_number=pr_number,
            head_sha_start=head_sha_start,
            head_sha_end=head_sha_end,
            file_findings=findings,
            synthesis_response="",
            verdict="STALE",
            trailer=STALE_TRAILER,
            comment_body=comment_body,
        )
        if not config.dry_run:
            posted = post_pr_comment(repo, pr_number, comment_body, token=config.github_token)
            result.posted_comment_url = posted.get("html_url")
        return result

    # Synthesis pass. The PR-level blockers (if any) are passed alongside
    # per-file findings so the LLM can incorporate them into its reasoning.
    synth_user = build_synthesis_user_prompt(pr_meta, findings, pr_level_blockers)
    synthesis_system_prompt = _with_review_doctrine(
        SYNTHESIS_SYSTEM_PROMPT,
        review_doctrine,
    )
    try:
        synth_response = call_llm(config, synthesis_system_prompt, synth_user, max_tokens=3000)
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
        synth_response = (
            f"Local LLM Audit: BLOCKED\n\n"
            f"Synthesis pass failed: {exc}. The per-file pass produced the "
            f"findings above but the final synthesis could not complete. "
            f"Treating as BLOCKED out of caution.\n"
        )

    # Determine verdict from per-file findings, PR-level blockers, and the
    # synthesis self-claim. ANY of the three trips BLOCKED — per-file or
    # PR-level blockers cannot be overridden by a synthesis-pass PASS claim
    # (Codex's "don't approve by vibes" rule).
    has_per_file_blocker = any(f.has_blocker() for f in findings)
    has_pr_level_blocker = bool(pr_level_blockers)
    llm_says_blocked = "Local LLM Audit: BLOCKED" in synth_response
    verdict = "BLOCKED" if (has_per_file_blocker or has_pr_level_blocker or llm_says_blocked) else "PASS"

    full_body = ensure_trailer(synth_response, verdict)
    comment_body = (
        _format_top_matter(repo, pr_number, head_sha_start, findings, pr_level_blockers)
        + full_body
    )

    result = AuditResult(
        repo=repo,
        pr_number=pr_number,
        head_sha_start=head_sha_start,
        head_sha_end=head_sha_end,
        file_findings=findings,
        synthesis_response=synth_response,
        verdict=verdict,
        trailer=DONE_TRAILER if verdict == "PASS" else BLOCKED_TRAILER,
        comment_body=comment_body,
        pr_level_blockers=pr_level_blockers,
    )

    if not config.dry_run:
        posted = post_pr_comment(repo, pr_number, comment_body, token=config.github_token)
        result.posted_comment_url = posted.get("html_url")

    return result


def _format_top_matter(
    repo: str,
    pr_number: int,
    head_sha: str,
    findings: List[FileFinding],
    pr_level_blockers: Optional[List[str]] = None,
) -> str:
    n_files = len(findings)
    n_blocker_files = sum(1 for f in findings if f.has_blocker())
    n_concern_files = sum(1 for f in findings if f.concerns and not f.has_blocker())
    n_clean = n_files - n_blocker_files - n_concern_files

    top = (
        f"## Local LLM audit (calibration phase — informational only)\n\n"
        f"Head SHA: `{head_sha}`\n"
        f"Files reviewed: {n_files} "
        f"(blocker findings: {n_blocker_files}, concern-only: {n_concern_files}, clean: {n_clean})\n\n"
    )

    if pr_level_blockers:
        top += "### PR-level blockers\n\n"
        for b in pr_level_blockers:
            top += f"- {b}\n"
        top += "\n"

    return top


def _format_stale_comment(repo: str, pr_number: int, start_sha: str, end_sha: str) -> str:
    return (
        f"## Local LLM audit (calibration phase — informational only)\n\n"
        f"Head SHA changed during review (`{start_sha[:8]}` → `{end_sha[:8]}`). "
        f"Skipping this verdict and requeuing for re-review of the new head.\n\n"
        f"{STALE_TRAILER}\n"
    )


# ----- CLI entry point -----


def _env_int(name: str) -> Optional[int]:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"env var {name} must be an integer, got {value!r}") from exc


def _resolve_int_option(
    explicit_value: Optional[int],
    env_name: str,
    profile_value: Optional[int],
    default_value: int,
) -> int:
    if explicit_value is not None:
        return explicit_value
    env_value = _env_int(env_name)
    if env_value is not None:
        return env_value
    if profile_value is not None:
        return profile_value
    return default_value


def resolve_runtime_options(
    *,
    profile_id: Optional[str] = None,
    api_base: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    http_timeout: Optional[int] = None,
    max_files: Optional[int] = None,
    max_file_bytes: Optional[int] = None,
    context_window: Optional[int] = None,
    json_repair_retries: Optional[int] = None,
) -> Dict[str, Any]:
    """Resolve local LLM runtime options.

    Precedence is explicit CLI value, environment variable, named profile,
    hardcoded default. Profiles are data-only and never grant merge authority.
    """
    resolved_profile_id = profile_id or os.environ.get("LOCAL_LLM_PROFILE") or ""
    profile = (
        local_llm_profiles.get_profile(resolved_profile_id)
        if resolved_profile_id
        else None
    )
    return {
        "profile_id": resolved_profile_id,
        "api_base": (
            api_base
            or os.environ.get("LOCAL_LLM_API_BASE")
            or (profile.api_base if profile else DEFAULT_API_BASE)
        ),
        "model": (
            model
            or os.environ.get("LOCAL_LLM_MODEL")
            or (profile.model if profile else DEFAULT_MODEL)
        ),
        "api_key": (
            api_key
            or os.environ.get("LOCAL_LLM_API_KEY")
            or (profile.api_key if profile else DEFAULT_API_KEY)
        ),
        "http_timeout": _resolve_int_option(
            http_timeout,
            "LOCAL_LLM_HTTP_TIMEOUT",
            profile.http_timeout if profile else None,
            DEFAULT_HTTP_TIMEOUT,
        ),
        "max_files": _resolve_int_option(
            max_files,
            "LOCAL_LLM_MAX_FILES",
            profile.max_files if profile else None,
            MAX_FILES_PER_REVIEW,
        ),
        "max_file_bytes": _resolve_int_option(
            max_file_bytes,
            "LOCAL_LLM_MAX_FILE_BYTES",
            profile.max_file_bytes if profile else None,
            MAX_FILE_BYTES,
        ),
        "context_window": _resolve_int_option(
            context_window,
            "LOCAL_LLM_CONTEXT_WINDOW",
            profile.context_window if profile else None,
            0,
        ),
        "json_repair_retries": _resolve_int_option(
            json_repair_retries,
            "LOCAL_LLM_JSON_REPAIR_RETRIES",
            None,
            1,
        ),
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Local LLM audit CLI — review a single pull request.",
    )
    ap.add_argument("--repo", required=True, help="owner/repo, e.g. owner/repo")
    ap.add_argument("--pr", type=int, required=True, help="PR number")
    ap.add_argument(
        "--profile",
        choices=local_llm_profiles.profile_ids(),
        default=None,
        help="Named local LLM runtime profile.",
    )
    ap.add_argument("--api-base", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument(
        "--http-timeout",
        type=int,
        default=None,
        help="HTTP timeout in seconds for local LLM calls.",
    )
    ap.add_argument(
        "--max-files",
        type=int,
        default=None,
        help=(
            "Maximum changed files to review before emitting a PR-level "
            "truncation blocker."
        ),
    )
    ap.add_argument(
        "--max-file-bytes",
        type=int,
        default=None,
        help="Maximum file-content bytes included in each per-file prompt.",
    )
    ap.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="Declared model context window for metadata and bakeoff reporting.",
    )
    ap.add_argument(
        "--json-repair-retries",
        type=int,
        default=None,
        help="Malformed per-file JSON retry count before fail-closed blocker.",
    )
    ap.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="optional local checkout to review for archived calibration heads",
    )
    ap.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    ap.add_argument(
        "--allow-historical-head",
        action="store_true",
        help="allow --repo-path HEAD to differ from the current GitHub PR head",
    )
    ap.add_argument(
        "--prompt-lenses",
        default=(
            os.environ.get("LOCAL_LLM_PROMPT_LENSES")
            or os.environ.get("CODE_MOWER_REVIEW_LENSES")
            or ",".join(code_mower_prompts.DEFAULT_REVIEW_LENSES)
        ),
        help="Comma-separated Code Mower review prompt lenses.",
    )
    ap.add_argument(
        "--prompt-dir",
        type=Path,
        default=(
            os.environ.get("LOCAL_LLM_PROMPT_DIR")
            or os.environ.get("CODE_MOWER_PROMPT_DIR")
            or None
        ),
        help="Directory containing Code Mower review lens markdown files.",
    )
    ap.add_argument(
        "--prompt-ref",
        default=(
            os.environ.get("LOCAL_LLM_PROMPT_REF")
            or os.environ.get("CODE_MOWER_PROMPT_REF")
            or DEFAULT_PROMPT_REF
        ),
        help=(
            "Trusted git ref for default review lenses when --prompt-dir is "
            "not supplied."
        ),
    )
    ap.add_argument(
        "--prompt-repo",
        type=Path,
        default=(
            os.environ.get("LOCAL_LLM_PROMPT_REPO")
            or os.environ.get("CODE_MOWER_PROMPT_REPO")
            or None
        ),
        help="Repository root used with --prompt-ref. Defaults to cwd.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=bool(os.environ.get("LOCAL_LLM_AUDIT_DRY_RUN")),
        help="Print the audit comment to stdout instead of posting it.",
    )
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    token = resolve_github_token_from_env_or_gh()
    if not token:
        print(
            "error: set GITHUB_TOKEN or authenticate gh so `gh auth token` works",
            file=sys.stderr,
        )
        return 1
    try:
        runtime_options = resolve_runtime_options(
            profile_id=args.profile,
            api_base=args.api_base,
            model=args.model,
            api_key=args.api_key,
            http_timeout=args.http_timeout,
            max_files=args.max_files,
            max_file_bytes=args.max_file_bytes,
            context_window=args.context_window,
            json_repair_retries=args.json_repair_retries,
        )
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    config = AuditConfig(
        github_token=token,
        api_base=runtime_options["api_base"],
        model=runtime_options["model"],
        api_key=runtime_options["api_key"],
        http_timeout=runtime_options["http_timeout"],
        max_file_bytes=runtime_options["max_file_bytes"],
        max_files=runtime_options["max_files"],
        profile_id=runtime_options["profile_id"],
        context_window=runtime_options["context_window"],
        json_repair_retries=runtime_options["json_repair_retries"],
        prompt_lenses=code_mower_prompts.split_lenses(args.prompt_lenses),
        prompt_dir=args.prompt_dir,
        prompt_ref=args.prompt_ref,
        prompt_repo=args.prompt_repo,
        repo_path=args.repo_path,
        base_ref=args.base_ref,
        allow_historical_head=args.allow_historical_head,
        dry_run=args.dry_run,
    )

    try:
        result = audit_pr(config, args.repo, args.pr)
    except urllib.error.HTTPError as exc:
        print(f"error: GitHub API HTTP {exc.code} — {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: network — {exc}", file=sys.stderr)
        return 1
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(result.comment_body)
    else:
        print(
            f"posted {args.repo}#{args.pr} verdict={result.verdict} "
            f"url={result.posted_comment_url}",
            file=sys.stderr,
        )

    if result.verdict == "STALE":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
