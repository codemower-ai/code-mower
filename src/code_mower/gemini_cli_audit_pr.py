#!/usr/bin/env python3
"""Run Gemini CLI as an informational Code Mower calibration reviewer."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import prompts as code_mower_prompts
        from code_mower import secrets as code_mower_secrets
        from code_mower.provider_runners import resolve_github_token_from_env_or_gh
    else:
        from tools import code_mower_prompts, code_mower_secrets
        from tools.provider_runners import resolve_github_token_from_env_or_gh
elif __package__ == "tools":
    from tools import code_mower_prompts, code_mower_secrets
    from tools.provider_runners import resolve_github_token_from_env_or_gh
else:  # pragma: no cover - exercised after package extraction.
    from . import prompts as code_mower_prompts
    from . import secrets as code_mower_secrets
    from .provider_runners import resolve_github_token_from_env_or_gh


DEFAULT_GEMINI_COMMAND = "gemini"
DEFAULT_GEMINI_MODE = "gemini-cli-audit"
DEFAULT_GEMINI_OUTPUT_STEM = "gemini-cli"
DEFAULT_GEMINI_DISPLAY_NAME = "Gemini CLI"
DEFAULT_BASE_REF = "origin/main"
DEFAULT_MAX_DIFF_BYTES = 140_000
DEFAULT_TIMEOUT_SECONDS = 900
RESPONSE_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
AUDIT_INPUT_INSUFFICIENT_CATEGORY = "audit_input_insufficient"
CODE_REVIEW_CATEGORY = "code_review"
GEMINI_STDIN_PROMPT = (
    "Use the complete Code Mower audit prompt supplied on stdin. "
    "Return only the requested JSON verdict."
)
GEMINI_STDIN_HELP_SENTINEL = "Appended to input on stdin"
PROMPT_FILE_HELP_SENTINELS = ("--print", "--print-timeout", "--sandbox", "--add-dir")
GEMINI_ENV_ALLOWLIST = (
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GOOGLE_API_KEY",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
)
GEMINI_KEY_ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
GEMINI_KEY_FILE_ENV_NAMES = ("GEMINI_API_KEY_FILE", "GOOGLE_API_KEY_FILE")


class GeminiCliHeadChangedError(RuntimeError):
    pass


class GeminiCliUnsupportedError(RuntimeError):
    pass


def _gh_request(
    method: str,
    path: str,
    *,
    token: str,
    accept: str = "application/vnd.github+json",
    timeout: int = 30,
) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    text = raw.decode("utf-8", errors="replace")
    if accept.endswith("diff"):
        return text
    return json.loads(text) if text else None


def fetch_pull_request(repo: str, pr_number: int, *, token: str) -> Mapping[str, Any]:
    payload = _gh_request("GET", f"/repos/{repo}/pulls/{pr_number}", token=token)
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub pull request response was not an object")
    return payload


def fetch_pull_request_diff(repo: str, pr_number: int, *, token: str) -> str:
    return str(
        _gh_request(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}",
            token=token,
            accept="application/vnd.github.v3.diff",
        )
    )


def _git(
    repo_path: Path,
    args: list[str],
    *,
    timeout: int = 30,
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


def fetch_local_checkout_diff(repo_path: Path, *, base_ref: str) -> tuple[str, str]:
    resolved_repo_path = repo_path.expanduser().resolve()
    if not resolved_repo_path.is_dir():
        raise ValueError(f"repo path does not exist: {resolved_repo_path}")
    head_sha = _local_head_sha(resolved_repo_path)
    diff = _git(
        resolved_repo_path,
        ["diff", "--no-ext-diff", "--find-renames", f"{base_ref}...HEAD"],
        timeout=120,
    ).stdout
    return head_sha, diff


def resolve_github_token() -> str:
    return resolve_github_token_from_env_or_gh()


def parse_api_key_file(text: str) -> str:
    """Parse a raw key file or a one-line shell-style API key assignment."""

    return code_mower_secrets.parse_secret_file_text(
        text,
        supported_env_names=set(GEMINI_KEY_ENV_NAMES),
    ).value


def resolve_gemini_api_key() -> str:
    for name in GEMINI_KEY_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in GEMINI_KEY_FILE_ENV_NAMES:
        path_text = os.environ.get(name, "").strip()
        if not path_text:
            continue
        try:
            value = parse_api_key_file(
                Path(path_text).expanduser().read_text(encoding="utf-8")
            )
        except OSError:
            continue
        if value:
            return value
    return ""


def build_gemini_child_env(
    home_dir: Path,
    *,
    gemini_api_key: str | None = None,
    exclude_env: tuple[str, ...] = (),
    preserve_ambient_home: bool = False,
) -> dict[str, str]:
    excluded = set(exclude_env)
    child_env = {
        key: value
        for key in GEMINI_ENV_ALLOWLIST
        if key not in excluded and (value := os.environ.get(key))
    }
    if gemini_api_key:
        child_env["GEMINI_API_KEY"] = gemini_api_key
        child_env["GOOGLE_API_KEY"] = gemini_api_key
    if preserve_ambient_home:
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            if os.environ.get(key):
                child_env[key] = os.environ[key]
    else:
        child_env["HOME"] = str(home_dir)
        child_env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
        child_env["XDG_CACHE_HOME"] = str(home_dir / ".cache")
        child_env["XDG_STATE_HOME"] = str(home_dir / ".local" / "state")
    return child_env


def _google_cli_safety_settings() -> dict[str, Any]:
    settings = {
        "tools": {
            "core": [],
            "allowed": [],
            "confirmationRequired": ["*"],
            "exclude": ["*"],
            "sandboxAllowedPaths": [],
            "sandboxNetworkAccess": False,
        },
        "mcp": {
            "allowed": [],
            "excluded": ["*"],
        },
        "useWriteTodos": False,
        "security": {
            "disableYoloMode": True,
            "disableAlwaysAllow": True,
            "enablePermanentToolApproval": False,
        },
    }
    return settings


def write_google_cli_safety_settings(
    home_dir: Path,
    *,
    settings_subdirs: tuple[str, ...] = (".gemini",),
) -> tuple[Path, ...]:
    settings = _google_cli_safety_settings()
    paths: list[Path] = []
    for subdir in settings_subdirs:
        settings_path = home_dir / subdir / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths.append(settings_path)
    return tuple(paths)


def write_gemini_safety_settings(home_dir: Path) -> Path:
    return write_google_cli_safety_settings(home_dir)[0]


def verify_gemini_stdin_contract(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    display_name: str = DEFAULT_GEMINI_DISPLAY_NAME,
) -> None:
    completed = subprocess.run(
        [command, "--help"],
        capture_output=True,
        cwd=cwd,
        env=dict(env),
        text=True,
        check=False,
        timeout=10,
    )
    help_text = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or GEMINI_STDIN_HELP_SENTINEL not in help_text:
        raise GeminiCliUnsupportedError(
            f"{display_name} must support appending stdin to --prompt for "
            "Code Mower calibration. Update the CLI or use a compatible "
            f"command; missing help sentinel: {GEMINI_STDIN_HELP_SENTINEL!r}."
        )


def verify_prompt_file_contract(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    display_name: str,
) -> None:
    completed = subprocess.run(
        [command, "--help"],
        capture_output=True,
        cwd=cwd,
        env=dict(env),
        text=True,
        check=False,
        timeout=10,
    )
    help_text = f"{completed.stdout}\n{completed.stderr}"
    missing = [
        sentinel
        for sentinel in PROMPT_FILE_HELP_SENTINELS
        if sentinel not in help_text
    ]
    if completed.returncode != 0 or missing:
        raise GeminiCliUnsupportedError(
            f"{display_name} must support --print, --print-timeout, --sandbox, "
            "and --add-dir for Code Mower calibration prompt-file transport. "
            f"Missing help sentinel(s): {missing!r}."
        )


def _clip_diff(diff: str, max_bytes: int) -> tuple[str, bool, int, int]:
    raw = diff.encode("utf-8", errors="replace")
    full_bytes = len(raw)
    if full_bytes <= max_bytes:
        return diff, False, full_bytes, full_bytes
    clipped = raw[:max_bytes].decode("utf-8", errors="replace")
    clipped += (
        "\n\n[Code Mower truncated this PR diff for Gemini CLI calibration: "
        f"included {max_bytes} of {full_bytes} bytes. Treat missing context as a "
        "review limitation, not permission to guess.]\n"
    )
    return clipped, True, full_bytes, len(clipped.encode("utf-8"))


def build_prompt(
    *,
    repo: str,
    pr_number: int,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    diff: str,
    prompt_lenses: tuple[str, ...],
    prompt_dir: Path | None = None,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    historical_calibration: bool = False,
    display_name: str = DEFAULT_GEMINI_DISPLAY_NAME,
    context_pack_text: str = "",
) -> tuple[str, dict[str, Any]]:
    review_prompt = code_mower_prompts.load_review_prompt(
        prompt_lenses,
        prompt_dir=prompt_dir,
    )
    clipped_diff, truncated, full_diff_bytes, included_diff_bytes = _clip_diff(
        diff,
        max_diff_bytes,
    )
    body = str(pr_meta.get("body") or "").strip() or "(empty)"
    title = str(pr_meta.get("title") or "").strip() or "(untitled)"
    historical_note = ""
    if historical_calibration:
        historical_note = """
# Historical Calibration Mode

This is a non-merge-authority run against an archived PR head. Treat stale PR
metadata and current branch ownership as calibration context only: measure the
review signal on the supplied head and diff, report real code findings if you
can, and classify missing/truncated audit input as a review limitation rather
than guessing.

"""
    context_pack_section = ""
    if context_pack_text.strip():
        context_pack_section = f"""
# Selected Context Packs

These bounded context files were selected for this calibration item. Use them as
supporting evidence for the diff. If the selected context is still insufficient,
classify the limitation as audit_input_insufficient instead of guessing.

{context_pack_text.strip()}

"""
    prompt = f"""You are the {display_name} informational reviewer inside Code Mower.

This is calibration evidence only. Do not claim merge authority and do not ask
the operator to run tests. Review the PR for bugs CI is unlikely to catch.
{historical_note}

# Code Mower Review Doctrine

{review_prompt.strip()}

# Required Response

Return exactly one JSON object with this shape and no markdown:

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
low-signal notes.

# Pull Request

Repository: {repo}
PR: #{pr_number}
Head SHA: {head_sha}
Title: {title}

Body:
{body}

{context_pack_section}
# Diff

```diff
{clipped_diff}
```
"""
    diagnostics = {
        "full_diff_bytes": full_diff_bytes,
        "included_diff_bytes": included_diff_bytes,
        "max_diff_bytes": max_diff_bytes,
        "diff_truncated": truncated,
        "prompt_lenses": list(prompt_lenses),
        "context_pack_bytes": len(context_pack_text.encode("utf-8")),
        "context_pack_included": bool(context_pack_text.strip()),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "historical_calibration": historical_calibration,
    }
    return prompt, diagnostics


def _unwrap_fenced_json(text: str) -> str:
    stripped = text.strip()
    match = RESPONSE_JSON_RE.fullmatch(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def parse_response_json(text: str) -> dict[str, Any] | None:
    stripped = _unwrap_fenced_json(text)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return dict(payload) if isinstance(payload, Mapping) else None


AUDIT_INPUT_INSUFFICIENT_PATTERNS = (
    "audit input incomplete",
    "audit input is incomplete",
    "audit input was incomplete",
    "diff is incomplete",
    "diff was incomplete",
    "diff was truncated",
    "diff truncation",
    "incomplete diff",
    "incomplete review context",
    "insufficient audit input",
    "review context is incomplete",
    "truncated diff",
)


def _finding_is_audit_input_insufficient(finding: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(finding.get(key) or "").strip().lower()
        for key in ("title", "detail", "summary", "text", "message")
    )
    return any(pattern in text for pattern in AUDIT_INPUT_INSUFFICIENT_PATTERNS)


def _audit_input_insufficient_result(findings: list[Mapping[str, Any]]) -> bool:
    blockers = [
        finding
        for finding in findings
        if str(finding.get("severity") or "").strip().upper() in {"P0", "P1", "P2"}
    ]
    return bool(blockers) and all(
        _finding_is_audit_input_insufficient(finding) for finding in blockers
    )


def _validate_verdict(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {
            "verdict": "unknown",
            "summary": "Gemini response did not contain parseable verdict JSON.",
            "findings": [],
            "blocker_count": 0,
            "parse_failed": True,
            "result_category": "parse_failed",
        }
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "blocked"}:
        verdict = "unknown"
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    blocker_count = 0
    normalized_findings: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        severity = str(finding.get("severity") or "").strip().upper()
        if severity in {"P0", "P1", "P2"}:
            blocker_count += 1
        try:
            line = int(finding.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        normalized_findings.append(
            {
                "severity": severity,
                "title": str(finding.get("title") or "").strip(),
                "file": str(finding.get("file") or "").strip(),
                "line": line,
                "detail": str(finding.get("detail") or "").strip(),
            }
        )
    if blocker_count and verdict == "pass":
        verdict = "blocked"
    result_category = (
        AUDIT_INPUT_INSUFFICIENT_CATEGORY
        if _audit_input_insufficient_result(normalized_findings)
        else CODE_REVIEW_CATEGORY
    )
    return {
        "verdict": verdict,
        "summary": str(payload.get("summary") or "").strip(),
        "findings": normalized_findings,
        "blocker_count": blocker_count,
        "parse_failed": False,
        "result_category": result_category,
    }


def _verdict_is_usable(verdict: Any) -> bool:
    if not isinstance(verdict, Mapping):
        return False
    return (
        not verdict.get("parse_failed")
        and str(verdict.get("verdict") or "") in {"pass", "blocked"}
    )


def run_gemini_cli_audit(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    command: str = DEFAULT_GEMINI_COMMAND,
    expected_head_sha: str | None = None,
    prompt_lenses: tuple[str, ...] = code_mower_prompts.DEFAULT_REVIEW_LENSES,
    prompt_dir: Path | None = None,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    gemini_api_key: str | None = None,
    repo_path: Path | None = None,
    base_ref: str = DEFAULT_BASE_REF,
    allow_historical_head: bool = False,
    historical_calibration: bool = False,
    mode: str = DEFAULT_GEMINI_MODE,
    output_stem: str = DEFAULT_GEMINI_OUTPUT_STEM,
    display_name: str = DEFAULT_GEMINI_DISPLAY_NAME,
    settings_subdirs: tuple[str, ...] = (".gemini",),
    model_env: str = "GEMINI_MODEL",
    child_env_exclude: tuple[str, ...] = (),
    cli_transport: str = "stdin_json",
    preserve_ambient_home: bool = False,
    context_pack_text: str = "",
) -> dict[str, Any]:
    pr_meta = fetch_pull_request(repo, pr_number, token=github_token)
    pr_head_sha = str(pr_meta.get("head", {}).get("sha") or "")
    if not pr_head_sha:
        raise ValueError("GitHub pull request response did not include head.sha")
    normalized_expected = str(expected_head_sha or "").strip().lower()
    diff_source = "github_pr"
    if repo_path is None:
        head_sha = pr_head_sha
        if normalized_expected and normalized_expected != head_sha.lower():
            raise GeminiCliHeadChangedError(
                "PR head does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        diff = fetch_pull_request_diff(repo, pr_number, token=github_token)
    else:
        head_sha, diff = fetch_local_checkout_diff(repo_path, base_ref=base_ref)
        diff_source = "local_checkout"
        if normalized_expected and normalized_expected != head_sha.lower():
            raise GeminiCliHeadChangedError(
                "local checkout does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        if (
            not allow_historical_head
            and not historical_calibration
            and head_sha.lower() != pr_head_sha.lower()
        ):
            raise GeminiCliHeadChangedError(
                "local checkout is not at the current PR head; pass "
                "--historical-calibration for archived calibration runs. "
                f"local={head_sha} current_pr={pr_head_sha}."
            )
    if not diff.strip():
        raise ValueError(
            "Gemini CLI calibration diff is empty; check --repo-path and --base-ref"
        )
    prompt, diagnostics = build_prompt(
        repo=repo,
        pr_number=pr_number,
        pr_meta=pr_meta,
        head_sha=head_sha,
        diff=diff,
        prompt_lenses=prompt_lenses,
        prompt_dir=prompt_dir,
        max_diff_bytes=max_diff_bytes,
        historical_calibration=historical_calibration,
        display_name=display_name,
        context_pack_text=context_pack_text,
    )
    diagnostics["diff_source"] = diff_source
    diagnostics["base_ref"] = base_ref if repo_path is not None else None
    diagnostics["cli_transport"] = cli_transport
    diagnostics["preserve_ambient_home"] = preserve_ambient_home

    started = time.monotonic()
    gemini_model = os.environ.get(model_env, "").strip()
    with tempfile.TemporaryDirectory(prefix="code-mower-gemini-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        home_dir = temp_dir / "home"
        workspace_dir = temp_dir / "workspace"
        home_dir.mkdir()
        workspace_dir.mkdir()
        write_google_cli_safety_settings(
            home_dir,
            settings_subdirs=settings_subdirs,
        )
        child_env = build_gemini_child_env(
            home_dir,
            gemini_api_key=gemini_api_key,
            exclude_env=child_env_exclude,
            preserve_ambient_home=preserve_ambient_home,
        )
        if cli_transport == "stdin_json":
            gemini_args = [
                command,
                "-p",
                GEMINI_STDIN_PROMPT,
                "--output-format",
                "json",
                "--approval-mode",
                "plan",
                "--skip-trust",
            ]
            if gemini_model:
                gemini_args.extend(["--model", gemini_model])
            verify_gemini_stdin_contract(
                command,
                cwd=workspace_dir,
                env=child_env,
                display_name=display_name,
            )
            completed = subprocess.run(
                gemini_args,
                input=prompt,
                capture_output=True,
                cwd=workspace_dir,
                env=child_env,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        elif cli_transport == "prompt_file":
            prompt_path = workspace_dir / f"{output_stem}.prompt-input.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            prompt_instruction = (
                f"Read {prompt_path.name} from the current workspace. Follow it as "
                "the complete Code Mower audit prompt. Return only the requested "
                "JSON verdict."
            )
            gemini_args = [
                command,
                "--sandbox",
                "--add-dir",
                str(workspace_dir),
                "--print-timeout",
                f"{timeout_seconds}s",
            ]
            if gemini_model:
                gemini_args.extend(["--model", gemini_model])
            gemini_args.extend(["--print", prompt_instruction])
            verify_prompt_file_contract(
                command,
                cwd=workspace_dir,
                env=child_env,
                display_name=display_name,
            )
            completed = subprocess.run(
                gemini_args,
                capture_output=True,
                cwd=workspace_dir,
                env=child_env,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        else:
            raise ValueError(f"unsupported CLI transport: {cli_transport}")
    duration_seconds = time.monotonic() - started

    raw_payload: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            loaded = json.loads(completed.stdout)
            if isinstance(loaded, Mapping):
                raw_payload = dict(loaded)
        except json.JSONDecodeError:
            raw_payload = None
    response_text = completed.stdout
    parsed_response: Mapping[str, Any] | None = None
    if raw_payload is not None:
        raw_response = raw_payload.get("response")
        if isinstance(raw_response, str):
            response_text = raw_response
            parsed_response = parse_response_json(response_text)
        elif isinstance(raw_response, Mapping):
            response_text = json.dumps(raw_response, sort_keys=True)
            parsed_response = raw_response
        elif "verdict" in raw_payload or "findings" in raw_payload:
            parsed_response = raw_payload
    if parsed_response is None:
        parsed_response = parse_response_json(response_text)
    verdict = _validate_verdict(parsed_response)
    if repo_path is None:
        head_after_meta = fetch_pull_request(repo, pr_number, token=github_token)
        head_after = str(head_after_meta.get("head", {}).get("sha") or "")
        if head_after != head_sha:
            raise GeminiCliHeadChangedError(
                "PR head changed during Gemini CLI audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )
    else:
        head_after = _local_head_sha(repo_path.expanduser().resolve())
        if head_after != head_sha:
            raise GeminiCliHeadChangedError(
                "local checkout head changed during Gemini CLI audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )

    payload: dict[str, Any] = {
        "mode": mode,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "head_sha_end": head_after,
        "pr_head_sha": pr_head_sha,
        "command": command,
        "model": gemini_model or None,
        "returncode": completed.returncode,
        "duration_seconds": round(duration_seconds, 3),
        "diagnostics": diagnostics,
        "response_text": response_text,
        "parsed_response": parsed_response,
        "verdict": verdict,
        "stderr": completed.stderr,
        "historical_calibration": historical_calibration,
    }
    if raw_payload is not None:
        payload["raw_output"] = raw_payload
        stats = raw_payload.get("stats")
        if isinstance(stats, Mapping):
            payload["stats"] = stats
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "prompt": output_dir / f"{output_stem}.prompt.txt",
            "response": output_dir / f"{output_stem}.response.md",
            "summary": output_dir / f"{output_stem}.summary.json",
        }
        paths["prompt"].write_text(prompt, encoding="utf-8")
        paths["response"].write_text(response_text, encoding="utf-8")
        payload["output_paths"] = {name: str(path) for name, path in paths.items()}
        paths["summary"].write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return payload


def render_text(payload: Mapping[str, Any]) -> str:
    verdict = payload.get("verdict", {})
    if not isinstance(verdict, Mapping):
        verdict = {}
    lines = [
        f"Gemini CLI audit for {payload.get('repo')}#{payload.get('pr_number')}",
        f"head: {payload.get('head_sha')}",
        f"verdict: {verdict.get('verdict', 'unknown')}",
        f"findings: {len(verdict.get('findings', []) or [])}",
        f"runtime: {payload.get('duration_seconds')}s",
    ]
    if payload.get("output_paths"):
        lines.extend(["", "Artifacts:"])
        output_paths = payload.get("output_paths", {})
        if isinstance(output_paths, Mapping):
            for name, path in sorted(output_paths.items()):
                lines.append(f"- {name}: {path}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--expected-head-sha", default=None)
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="optional local checkout to diff for archived calibration heads",
    )
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument(
        "--allow-historical-head",
        action="store_true",
        help="allow --repo-path HEAD to differ from the current GitHub PR head",
    )
    parser.add_argument(
        "--historical-calibration",
        action="store_true",
        help=(
            "mark this as non-merge-authority evidence against an archived PR "
            "head; implies --allow-historical-head for local checkouts"
        ),
    )
    parser.add_argument(
        "--command",
        default=os.environ.get("GEMINI_CLI_COMMAND", DEFAULT_GEMINI_COMMAND),
    )
    parser.add_argument(
        "--prompt-lenses",
        default=",".join(code_mower_prompts.DEFAULT_REVIEW_LENSES),
    )
    parser.add_argument("--prompt-dir", type=Path, default=None)
    parser.add_argument(
        "--context-pack-file",
        action="append",
        type=Path,
        default=[],
        help="Bounded context-pack text file to append to the audit prompt.",
    )
    parser.add_argument("--max-diff-bytes", type=int, default=DEFAULT_MAX_DIFF_BYTES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    token = resolve_github_token()
    if not token:
        print(
            "error: set GITHUB_TOKEN or authenticate gh so `gh auth token` works",
            file=sys.stderr,
        )
        return 1
    gemini_api_key = resolve_gemini_api_key()
    if not gemini_api_key:
        print(
            "error: set GEMINI_API_KEY, GOOGLE_API_KEY, GEMINI_API_KEY_FILE, or GOOGLE_API_KEY_FILE for Gemini CLI",
            file=sys.stderr,
        )
        return 1
    try:
        context_pack_text = "\n\n".join(
            path.read_text(encoding="utf-8") for path in args.context_pack_file
        )
        payload = run_gemini_cli_audit(
            repo=args.repo,
            pr_number=args.pr,
            github_token=token,
            command=args.command,
            expected_head_sha=args.expected_head_sha,
            prompt_lenses=code_mower_prompts.split_lenses(args.prompt_lenses),
            prompt_dir=args.prompt_dir,
            max_diff_bytes=args.max_diff_bytes,
            timeout_seconds=args.timeout,
            output_dir=args.output_dir,
            gemini_api_key=gemini_api_key,
            repo_path=args.repo_path,
            base_ref=args.base_ref,
            allow_historical_head=args.allow_historical_head,
            historical_calibration=args.historical_calibration,
            context_pack_text=context_pack_text,
        )
    except GeminiCliHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        GeminiCliUnsupportedError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        urllib.error.URLError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(payload), end="")
    if payload.get("returncode") != 0:
        return 1
    return 0 if _verdict_is_usable(payload.get("verdict")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
