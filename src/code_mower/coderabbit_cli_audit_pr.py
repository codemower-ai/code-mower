#!/usr/bin/env python3
"""Capture CodeRabbit CLI review output as Code Mower calibration evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower.provider_runners import resolve_github_token_from_env_or_gh
    else:
        from tools.provider_runners import resolve_github_token_from_env_or_gh
elif __package__ == "tools":
    from tools.provider_runners import resolve_github_token_from_env_or_gh
else:  # pragma: no cover - exercised after package extraction.
    from .provider_runners import resolve_github_token_from_env_or_gh


DEFAULT_CODERABBIT_COMMAND = "coderabbit"
DEFAULT_BASE_REF = "origin/main"
DEFAULT_REVIEW_TYPE = "committed"
DEFAULT_TIMEOUT_SECONDS = 900
RESPONSE_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
CODERABBIT_ENV_ALLOWLIST = (
    "CODERABBIT_API_KEY",
    "CODERABBIT_API_BASE",
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
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


class CoderabbitCliHeadChangedError(RuntimeError):
    pass


class CoderabbitCliWorkspaceError(RuntimeError):
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
    return json.loads(text) if text else None


def fetch_pull_request(repo: str, pr_number: int, *, token: str) -> Mapping[str, Any]:
    payload = _gh_request("GET", f"/repos/{repo}/pulls/{pr_number}", token=token)
    if not isinstance(payload, Mapping):
        raise ValueError("GitHub pull request response was not an object")
    return payload


def resolve_github_token() -> str:
    return resolve_github_token_from_env_or_gh()


def build_coderabbit_child_env() -> dict[str, str]:
    return {
        key: value
        for key in CODERABBIT_ENV_ALLOWLIST
        if (value := os.environ.get(key))
    }


def _git(
    repo_path: Path,
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def _local_head_sha(repo_path: Path) -> str:
    completed = _git(repo_path, ["rev-parse", "HEAD"])
    return completed.stdout.strip()


def _working_tree_status(repo_path: Path) -> str:
    completed = _git(repo_path, ["status", "--porcelain"])
    return completed.stdout


def verify_checkout(
    repo_path: Path,
    *,
    expected_head_sha: str,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    if not repo_path.is_dir():
        raise CoderabbitCliWorkspaceError(f"repo path does not exist: {repo_path}")
    try:
        local_head = _local_head_sha(repo_path)
    except subprocess.CalledProcessError as exc:
        raise CoderabbitCliWorkspaceError(
            f"repo path is not a git checkout: {repo_path}"
        ) from exc
    if local_head.lower() != expected_head_sha.lower():
        raise CoderabbitCliWorkspaceError(
            "local checkout must be at the PR head for CodeRabbit CLI "
            f"calibration; local={local_head} expected={expected_head_sha}."
        )
    status = _working_tree_status(repo_path)
    if status.strip() and not allow_dirty:
        raise CoderabbitCliWorkspaceError(
            "local checkout has uncommitted changes; commit/stash them or pass "
            "--allow-dirty for an explicitly exploratory run."
        )
    return {
        "repo_path": str(repo_path),
        "local_head_sha": local_head,
        "dirty": bool(status.strip()),
    }


def _unwrap_fenced_json(text: str) -> str:
    stripped = text.strip()
    match = RESPONSE_JSON_RE.fullmatch(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _json_fragment(text: str) -> str | None:
    candidates = [
        (text.find("{"), "{", "}"),
        (text.find("["), "[", "]"),
    ]
    candidates = [candidate for candidate in candidates if candidate[0] >= 0]
    if not candidates:
        return None
    start, _, close_char = min(candidates, key=lambda item: item[0])
    end = text.rfind(close_char)
    if end <= start:
        return None
    return text[start : end + 1]


def parse_agent_json(text: str) -> Any | None:
    stripped = _unwrap_fenced_json(text)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        fragment = _json_fragment(stripped)
        if fragment is None:
            return None
        try:
            payload = json.loads(fragment)
        except json.JSONDecodeError:
            return None
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, list):
        return payload
    return None


def parse_agent_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            events.append(dict(payload))
    return events


def _iter_findings(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    if str(value.get("type") or "") == "finding":
        return [value]
    for key in ("findings", "issues", "comments"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
    for key in ("result", "review", "data", "response"):
        nested = value.get(key)
        nested_findings = _iter_findings(nested)
        if nested_findings:
            return nested_findings
    return []


def _complete_event(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if str(event.get("type") or "") == "complete":
            return event
    return None


def _normalize_findings(payload: Any | None) -> list[dict[str, Any]]:
    findings = _iter_findings(payload)
    normalized: list[dict[str, Any]] = []
    for finding in findings:
        try:
            line = int(
                finding.get("line")
                or finding.get("start_line")
                or finding.get("line_number")
                or 0
            )
        except (TypeError, ValueError):
            line = 0
        normalized.append(
            {
                "severity": str(
                    finding.get("severity") or finding.get("level") or ""
                ).strip(),
                "title": str(
                    finding.get("title")
                    or finding.get("rule")
                    or finding.get("message")
                    or finding.get("fileName")
                    or ""
                ).strip(),
                "file": str(
                    finding.get("file")
                    or finding.get("path")
                    or finding.get("filename")
                    or finding.get("fileName")
                    or ""
                ).strip(),
                "line": line,
                "detail": str(
                    finding.get("detail")
                    or finding.get("body")
                    or finding.get("description")
                    or finding.get("message")
                    or finding.get("codegenInstructions")
                    or ""
                ).strip(),
            }
        )
    return normalized


def run_coderabbit_cli_audit(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    repo_path: Path,
    command: str = DEFAULT_CODERABBIT_COMMAND,
    expected_head_sha: str | None = None,
    base_ref: str = DEFAULT_BASE_REF,
    review_type: str = DEFAULT_REVIEW_TYPE,
    config_files: Sequence[Path] = (),
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    allow_dirty: bool = False,
    allow_historical_head: bool = False,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    pr_meta = fetch_pull_request(repo, pr_number, token=github_token)
    pr_head_sha = str(pr_meta.get("head", {}).get("sha") or "")
    if not pr_head_sha:
        raise ValueError("GitHub pull request response did not include head.sha")
    head_sha = _local_head_sha(repo_path) if allow_historical_head else pr_head_sha
    normalized_expected = str(expected_head_sha or "").strip().lower()
    if normalized_expected and normalized_expected != head_sha.lower():
        raise CoderabbitCliHeadChangedError(
            "review head does not match calibration corpus; "
            f"expected {expected_head_sha}, current={head_sha}."
        )
    checkout = verify_checkout(
        repo_path,
        expected_head_sha=head_sha,
        allow_dirty=allow_dirty,
    )

    cli_args = [
        command,
        "review",
        "--agent",
        "--type",
        review_type,
        "--base",
        base_ref,
        "--dir",
        str(repo_path),
    ]
    for config_file in config_files:
        cli_args.extend(["--config", str(config_file)])

    started = time.monotonic()
    completed = subprocess.run(
        cli_args,
        capture_output=True,
        cwd=repo_path,
        env=build_coderabbit_child_env(),
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    duration_seconds = time.monotonic() - started

    parsed_output = parse_agent_json(completed.stdout)
    parsed_events = [] if parsed_output is not None else parse_agent_events(completed.stdout)
    if parsed_output is not None:
        parse_status = "json"
        normalized_findings = _normalize_findings(parsed_output)
    elif parsed_events:
        parse_status = "ndjson"
        normalized_findings = [
            finding
            for event in parsed_events
            for finding in _normalize_findings(event)
        ]
    else:
        parse_status = "raw"
        normalized_findings = []
    final_event = _complete_event(parsed_events)
    finding_count = len(normalized_findings)
    finding_count_source = "parsed_findings"
    if final_event is not None:
        try:
            final_finding_count = int(final_event.get("findings") or 0)
            if final_finding_count > finding_count or not normalized_findings:
                finding_count = final_finding_count
                finding_count_source = "final_event"
        except (TypeError, ValueError):
            if not finding_count:
                finding_count = 0
    head_after = ""
    if allow_historical_head:
        head_after = _local_head_sha(repo_path)
        if head_after == head_sha:
            head_check = {"status": "pass", "source": "local_checkout"}
        else:
            head_check = {
                "status": "changed",
                "source": "local_checkout",
                "message": (
                    "local checkout head changed during CodeRabbit CLI audit; "
                    "discard this run and rerun."
                ),
            }
    else:
        try:
            head_after_meta = fetch_pull_request(repo, pr_number, token=github_token)
            head_after = str(head_after_meta.get("head", {}).get("sha") or "")
            if not head_after:
                head_check = {
                    "status": "error",
                    "message": "post-run PR response did not include head.sha",
                }
            elif head_after == head_sha:
                head_check = {"status": "pass"}
            else:
                head_check = {
                    "status": "changed",
                    "message": "PR head changed during CodeRabbit CLI audit; discard this run and rerun.",
                }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            head_check = {
                "status": "error",
                "message": f"post-run PR head check failed: {exc}",
            }

    payload: dict[str, Any] = {
        "mode": "coderabbit-cli-audit",
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "head_sha_end": head_after,
        "pr_head_sha": pr_head_sha,
        "command": command,
        "base_ref": base_ref,
        "review_type": review_type,
        "returncode": completed.returncode,
        "duration_seconds": round(duration_seconds, 3),
        "checkout": checkout,
        "parse_status": parse_status,
        "parsed_output": parsed_output,
        "parsed_events": parsed_events,
        "final_event": dict(final_event) if final_event is not None else None,
        "findings": normalized_findings,
        "finding_count": finding_count,
        "finding_count_source": finding_count_source,
        "head_check": head_check,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "command": output_dir / "coderabbit-cli.command.json",
            "stdout": output_dir / "coderabbit-cli.stdout.txt",
            "stderr": output_dir / "coderabbit-cli.stderr.txt",
            "summary": output_dir / "coderabbit-cli.summary.json",
        }
        paths["command"].write_text(
            json.dumps({"args": cli_args}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths["stdout"].write_text(completed.stdout, encoding="utf-8")
        paths["stderr"].write_text(completed.stderr, encoding="utf-8")
        payload["output_paths"] = {name: str(path) for name, path in paths.items()}
        paths["summary"].write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return payload


def render_text(payload: Mapping[str, Any]) -> str:
    lines = [
        f"CodeRabbit CLI audit for {payload.get('repo')}#{payload.get('pr_number')}",
        f"head: {payload.get('head_sha')}",
        f"head_check: {payload.get('head_check', {}).get('status') if isinstance(payload.get('head_check'), Mapping) else 'unknown'}",
        f"parse_status: {payload.get('parse_status')}",
        f"findings: {payload.get('finding_count')}",
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
    parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    parser.add_argument("--expected-head-sha", default=None)
    parser.add_argument(
        "--command",
        default=os.environ.get("CODERABBIT_CLI_COMMAND", DEFAULT_CODERABBIT_COMMAND),
    )
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument(
        "--review-type",
        default=DEFAULT_REVIEW_TYPE,
        choices=("all", "committed", "uncommitted"),
    )
    parser.add_argument("--config", dest="config_files", type=Path, action="append", default=[])
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument(
        "--allow-historical-head",
        action="store_true",
        help="allow --repo-path HEAD to differ from the current GitHub PR head",
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
        payload = run_coderabbit_cli_audit(
            repo=args.repo,
            pr_number=args.pr,
            github_token=token,
            repo_path=args.repo_path,
            command=args.command,
            expected_head_sha=args.expected_head_sha,
            base_ref=args.base_ref,
            review_type=args.review_type,
            config_files=tuple(args.config_files),
            timeout_seconds=args.timeout,
            output_dir=args.output_dir,
            allow_dirty=args.allow_dirty,
            allow_historical_head=args.allow_historical_head,
        )
    except CoderabbitCliHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        CoderabbitCliWorkspaceError,
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(payload), end="")
    head_check = payload.get("head_check")
    head_check_status = head_check.get("status") if isinstance(head_check, Mapping) else None
    return 0 if payload.get("returncode") == 0 and head_check_status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
