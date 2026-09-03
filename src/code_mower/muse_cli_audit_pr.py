#!/usr/bin/env python3
"""Run Muse Code as an informational Code Mower calibration reviewer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import gemini_cli_audit_pr
        from code_mower import prompts as code_mower_prompts
        from code_mower import secrets as code_mower_secrets
        from code_mower.provider_runners import build_allowlisted_child_env
    else:
        from tools import gemini_cli_audit_pr, code_mower_prompts, code_mower_secrets
        from tools.provider_runners import build_allowlisted_child_env
elif __package__ == "tools":
    from tools import gemini_cli_audit_pr, code_mower_prompts, code_mower_secrets
    from tools.provider_runners import build_allowlisted_child_env
else:  # pragma: no cover - exercised after package extraction.
    from . import gemini_cli_audit_pr
    from . import prompts as code_mower_prompts
    from . import secrets as code_mower_secrets
    from .provider_runners import build_allowlisted_child_env


DEFAULT_MUSE_COMMAND = "muse"
DEFAULT_MUSE_MODE = "muse-cli-audit"
DEFAULT_MUSE_OUTPUT_STEM = "muse-cli"
DEFAULT_MUSE_DISPLAY_NAME = "Muse CLI"
DEFAULT_MUSE_MAX_STEPS = 12
MUSE_AMBIENT_HOME_ENV = "MUSE_CLI_USE_AMBIENT_HOME"
MUSE_MODEL_ENV_NAMES = ("CODE_MOWER_MUSE_MODEL", "MUSE_MODEL", "META_MUSE_MODEL")
MUSE_REASONING_EFFORT_ENV_NAMES = (
    "CODE_MOWER_MUSE_REASONING_EFFORT",
    "MUSE_REASONING_EFFORT",
)
MUSE_KEY_ENV_NAMES = ("META_API_KEY",)
MUSE_KEY_FILE_ENV_NAMES = ("META_API_KEY_FILE",)
MUSE_VERDICT_FALLBACK_PAYLOAD_TYPES = {
    "run.terminal.completed",
    "task.lifecycle.output",
}
MUSE_ENV_ALLOWLIST = (
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
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

MuseCliHeadChangedError = gemini_cli_audit_pr.GeminiCliHeadChangedError
MuseCliUnsupportedError = gemini_cli_audit_pr.GeminiCliUnsupportedError


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def parse_api_key_file(text: str) -> str:
    return code_mower_secrets.parse_secret_file_text(
        text,
        supported_env_names=set(MUSE_KEY_ENV_NAMES),
    ).value


def resolve_muse_api_key() -> str:
    for name in MUSE_KEY_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in MUSE_KEY_FILE_ENV_NAMES:
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


def resolve_muse_model() -> str:
    return _first_env_value(MUSE_MODEL_ENV_NAMES)


def resolve_muse_reasoning_effort() -> str:
    return _first_env_value(MUSE_REASONING_EFFORT_ENV_NAMES)


def build_muse_child_env(home_dir: Path, *, preserve_ambient_home: bool) -> dict[str, str]:
    return build_allowlisted_child_env(
        MUSE_ENV_ALLOWLIST,
        home_env={
            "HOME": home_dir,
            "XDG_CONFIG_HOME": home_dir / ".config",
            "XDG_CACHE_HOME": home_dir / ".cache",
            "XDG_STATE_HOME": home_dir / ".local" / "state",
        },
        preserve_ambient_home=preserve_ambient_home,
    )


def _strict_verdict_json_text(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return ""
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return ""
    if isinstance(loaded, Mapping) and ("verdict" in loaded or "findings" in loaded):
        return json.dumps(loaded, sort_keys=True)
    return ""


def muse_jsonl_response(text: str) -> tuple[str, dict[str, Any]]:
    """Extract response text and metadata from Muse JSONL without keeping raw events."""

    output_parts: list[str] = []
    fallback_candidates: list[str] = []
    payload_types: set[str] = set()
    event_count = 0
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event_count += 1
        payload_type = str(event.get("payload_type") or "").strip()
        if payload_type:
            payload_types.add(payload_type)
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        payload_text = payload.get("text")
        if not isinstance(payload_text, str):
            continue
        if payload_type == "run.output.delta":
            output_parts.append(payload_text)
        elif payload_type in MUSE_VERDICT_FALLBACK_PAYLOAD_TYPES:
            fallback_candidates.append(payload_text)
    response_text = "".join(output_parts).strip()
    if not response_text:
        for candidate in reversed(fallback_candidates):
            if response_text := _strict_verdict_json_text(candidate):
                break
    if not response_text and event_count == 0:
        response_text = text.strip()
    return response_text, {
        "muse_jsonl_event_count": event_count,
        "muse_jsonl_payload_types": sorted(payload_types),
    }


def run_muse_cli_audit(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    command: str = DEFAULT_MUSE_COMMAND,
    expected_head_sha: str | None = None,
    prompt_lenses: tuple[str, ...] = code_mower_prompts.DEFAULT_REVIEW_LENSES,
    prompt_dir: Path | None = None,
    max_diff_bytes: int = gemini_cli_audit_pr.DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: int = gemini_cli_audit_pr.DEFAULT_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    muse_api_key: str | None = None,
    repo_path: Path | None = None,
    base_ref: str = gemini_cli_audit_pr.DEFAULT_BASE_REF,
    allow_historical_head: bool = False,
    historical_calibration: bool = False,
    allow_ambient_home: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_model_steps: int = DEFAULT_MUSE_MAX_STEPS,
    context_pack_text: str = "",
) -> dict[str, Any]:
    if not muse_api_key and not allow_ambient_home:
        raise ValueError(
            "Muse CLI requires META_API_KEY/META_API_KEY_FILE or explicit "
            f"local login opt-in via {MUSE_AMBIENT_HOME_ENV}=1 in trusted "
            "environments."
        )

    pr_meta = gemini_cli_audit_pr.fetch_pull_request(repo, pr_number, token=github_token)
    pr_head_sha = str(pr_meta.get("head", {}).get("sha") or "")
    if not pr_head_sha:
        raise ValueError("GitHub pull request response did not include head.sha")
    normalized_expected = str(expected_head_sha or "").strip().lower()
    diff_source = "github_pr"
    if repo_path is None:
        head_sha = pr_head_sha
        if normalized_expected and normalized_expected != head_sha.lower():
            raise MuseCliHeadChangedError(
                "PR head does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        diff = gemini_cli_audit_pr.fetch_pull_request_diff(repo, pr_number, token=github_token)
    else:
        head_sha, diff = gemini_cli_audit_pr.fetch_local_checkout_diff(
            repo_path,
            base_ref=base_ref,
        )
        diff_source = "local_checkout"
        if normalized_expected and normalized_expected != head_sha.lower():
            raise MuseCliHeadChangedError(
                "local checkout does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        if (
            not allow_historical_head
            and not historical_calibration
            and head_sha.lower() != pr_head_sha.lower()
        ):
            raise MuseCliHeadChangedError(
                "local checkout is not at the current PR head; pass "
                "--historical-calibration for archived calibration runs. "
                f"local={head_sha} current_pr={pr_head_sha}."
            )
    if not diff.strip():
        raise ValueError(
            "Muse CLI calibration diff is empty; check --repo-path and --base-ref"
        )

    prompt, diagnostics = gemini_cli_audit_pr.build_prompt(
        repo=repo,
        pr_number=pr_number,
        pr_meta=pr_meta,
        head_sha=head_sha,
        diff=diff,
        prompt_lenses=prompt_lenses,
        prompt_dir=prompt_dir,
        max_diff_bytes=max_diff_bytes,
        historical_calibration=historical_calibration,
        display_name=DEFAULT_MUSE_DISPLAY_NAME,
        context_pack_text=context_pack_text,
    )
    diagnostics["diff_source"] = diff_source
    diagnostics["base_ref"] = base_ref if repo_path is not None else None
    diagnostics["cli_transport"] = "jsonl_prompt_file"
    diagnostics["preserve_ambient_home"] = allow_ambient_home
    diagnostics["disable_shell"] = True
    diagnostics["disable_write"] = True
    diagnostics["disable_web_tools"] = True
    diagnostics["raw_jsonl_retained"] = False
    diagnostics["max_model_steps"] = max(1, max_model_steps)

    muse_model = (model or resolve_muse_model()).strip()
    muse_reasoning_effort = (reasoning_effort or resolve_muse_reasoning_effort()).strip()

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="code-mower-muse-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        home_dir = temp_dir / "home"
        workspace_dir = temp_dir / "workspace"
        home_dir.mkdir()
        workspace_dir.mkdir()
        prompt_path = workspace_dir / f"{DEFAULT_MUSE_OUTPUT_STEM}.prompt-input.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        child_env = build_muse_child_env(
            home_dir,
            preserve_ambient_home=allow_ambient_home,
        )
        muse_args = [
            command,
            "exec",
            "--json",
            "--prompt-file",
            str(prompt_path),
            "--provider",
            "meta",
            "--approval-mode",
            "never",
            "--disable-shell",
            "--disable-write",
            "--disable-web-tools",
            "--no-foreign-personal-context",
            "--no-session-log",
            "--max-model-steps",
            str(max(1, max_model_steps)),
        ]
        if muse_api_key:
            muse_args.append("--api-key-stdin")
        if muse_model:
            muse_args.extend(["--model", muse_model])
        if muse_reasoning_effort:
            muse_args.extend(["--reasoning-effort", muse_reasoning_effort])
        completed = subprocess.run(
            muse_args,
            input=muse_api_key if muse_api_key else None,
            capture_output=True,
            cwd=workspace_dir,
            env=child_env,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    duration_seconds = time.monotonic() - started

    response_text, jsonl_meta = muse_jsonl_response(completed.stdout)
    diagnostics.update(jsonl_meta)
    diagnostics["raw_stdout_retained"] = False
    diagnostics["raw_stderr_retained"] = False
    diagnostics["stderr_bytes"] = len(completed.stderr.encode("utf-8", errors="replace"))
    diagnostics["stderr_line_count"] = len(completed.stderr.splitlines())
    parsed_response = gemini_cli_audit_pr.parse_response_json(response_text)
    verdict = gemini_cli_audit_pr._validate_verdict(
        parsed_response,
        display_name=DEFAULT_MUSE_DISPLAY_NAME,
    )
    if repo_path is None:
        head_after_meta = gemini_cli_audit_pr.fetch_pull_request(
            repo,
            pr_number,
            token=github_token,
        )
        head_after = str(head_after_meta.get("head", {}).get("sha") or "")
        if head_after != head_sha:
            raise MuseCliHeadChangedError(
                "PR head changed during Muse CLI audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )
    else:
        head_after = gemini_cli_audit_pr._local_head_sha(repo_path.expanduser().resolve())
        if head_after != head_sha:
            raise MuseCliHeadChangedError(
                "local checkout head changed during Muse CLI audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )

    payload: dict[str, Any] = {
        "mode": DEFAULT_MUSE_MODE,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "head_sha_end": head_after,
        "pr_head_sha": pr_head_sha,
        "command": command,
        "model": muse_model or None,
        "reasoning_effort": muse_reasoning_effort or None,
        "returncode": completed.returncode,
        "duration_seconds": round(duration_seconds, 3),
        "diagnostics": diagnostics,
        "response_text": response_text,
        "parsed_response": parsed_response,
        "verdict": verdict,
        "historical_calibration": historical_calibration,
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "prompt": output_dir / f"{DEFAULT_MUSE_OUTPUT_STEM}.prompt.txt",
            "response": output_dir / f"{DEFAULT_MUSE_OUTPUT_STEM}.response.md",
            "summary": output_dir / f"{DEFAULT_MUSE_OUTPUT_STEM}.summary.json",
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
    text = gemini_cli_audit_pr.render_text(payload)
    return text.replace("Gemini CLI audit", "Muse CLI audit", 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--expected-head-sha", default=None)
    parser.add_argument("--repo-path", type=Path, default=None)
    parser.add_argument("--base-ref", default=gemini_cli_audit_pr.DEFAULT_BASE_REF)
    parser.add_argument("--allow-historical-head", action="store_true")
    parser.add_argument("--historical-calibration", action="store_true")
    parser.add_argument(
        "--command",
        default=os.environ.get("MUSE_CLI_COMMAND", DEFAULT_MUSE_COMMAND),
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--max-model-steps", type=int, default=DEFAULT_MUSE_MAX_STEPS)
    parser.add_argument(
        "--prompt-lenses",
        default=",".join(code_mower_prompts.DEFAULT_REVIEW_LENSES),
    )
    parser.add_argument("--prompt-dir", type=Path, default=None)
    parser.add_argument("--context-pack-file", action="append", type=Path, default=[])
    parser.add_argument(
        "--max-diff-bytes",
        type=int,
        default=gemini_cli_audit_pr.DEFAULT_MAX_DIFF_BYTES,
    )
    parser.add_argument("--timeout", type=int, default=gemini_cli_audit_pr.DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--allow-ambient-home",
        action="store_true",
        default=_env_flag_enabled(MUSE_AMBIENT_HOME_ENV),
        help=(
            "Allow Muse CLI to inherit local login/session state from HOME. "
            f"Equivalent env opt-in: {MUSE_AMBIENT_HOME_ENV}=1."
        ),
    )
    args = parser.parse_args(argv)

    token = gemini_cli_audit_pr.resolve_github_token()
    if not token:
        print(
            "error: set GITHUB_TOKEN or authenticate gh so `gh auth token` works",
            file=sys.stderr,
        )
        return 1
    try:
        context_pack_text = "\n\n".join(
            path.read_text(encoding="utf-8") for path in args.context_pack_file
        )
        payload = run_muse_cli_audit(
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
            muse_api_key=resolve_muse_api_key(),
            repo_path=args.repo_path,
            base_ref=args.base_ref,
            allow_historical_head=args.allow_historical_head,
            historical_calibration=args.historical_calibration,
            allow_ambient_home=args.allow_ambient_home,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_model_steps=args.max_model_steps,
            context_pack_text=context_pack_text,
        )
    except MuseCliHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        MuseCliUnsupportedError,
        OSError,
        ValueError,
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
    return 0 if gemini_cli_audit_pr._verdict_is_usable(payload.get("verdict")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
