#!/usr/bin/env python3
"""Run Grok Build as an informational Code Mower calibration reviewer."""

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


DEFAULT_GROK_COMMAND = "grok"
DEFAULT_GROK_MODE = "grok-build-audit"
DEFAULT_GROK_OUTPUT_STEM = "grok-build"
DEFAULT_GROK_DISPLAY_NAME = "Grok Build"
DEFAULT_GROK_MODEL_ENV = "GROK_MODEL"
CODE_MOWER_GROK_MODEL_ENV = "CODE_MOWER_GROK_MODEL"
GROK_AMBIENT_HOME_ENV = "GROK_BUILD_USE_AMBIENT_HOME"
GROK_KEY_ENV_NAMES = ("XAI_API_KEY", "GROK_DEPLOYMENT_KEY", "GROK_API_KEY")
GROK_KEY_FILE_ENV_NAMES = tuple(f"{name}_FILE" for name in GROK_KEY_ENV_NAMES)
GROK_MODEL_ENV_NAMES = (CODE_MOWER_GROK_MODEL_ENV, DEFAULT_GROK_MODEL_ENV, "XAI_MODEL")
GROK_ENV_ALLOWLIST = (
    "PATH",
    "USER",
    "LOGNAME",
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
    *GROK_KEY_ENV_NAMES,
)

GrokBuildHeadChangedError = gemini_cli_audit_pr.GeminiCliHeadChangedError
GrokBuildUnsupportedError = gemini_cli_audit_pr.GeminiCliUnsupportedError


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_api_key_file(text: str) -> str:
    return code_mower_secrets.parse_secret_file_text(
        text,
        supported_env_names=set(GROK_KEY_ENV_NAMES),
    ).value


def resolve_grok_api_key() -> str:
    for name in GROK_KEY_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in GROK_KEY_FILE_ENV_NAMES:
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


def resolve_grok_model() -> str:
    for name in GROK_MODEL_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_grok_child_env(
    home_dir: Path,
    *,
    grok_api_key: str | None = None,
    preserve_ambient_home: bool,
) -> dict[str, str]:
    extra_env: dict[str, str] = {}
    if grok_api_key:
        extra_env["XAI_API_KEY"] = grok_api_key
    return build_allowlisted_child_env(
        GROK_ENV_ALLOWLIST,
        extra_env=extra_env,
        home_env={
            "HOME": home_dir,
            "XDG_CONFIG_HOME": home_dir / ".config",
            "XDG_CACHE_HOME": home_dir / ".cache",
            "XDG_STATE_HOME": home_dir / ".local" / "state",
        },
        preserve_ambient_home=preserve_ambient_home,
    )


def _response_text_from_grok_payload(
    raw_payload: Mapping[str, Any] | None,
    stdout: str,
) -> tuple[str, Mapping[str, Any] | None]:
    response_text = stdout
    parsed_response: Mapping[str, Any] | None = None
    if raw_payload is not None:
        raw_response = raw_payload.get("text")
        if isinstance(raw_response, str):
            response_text = raw_response
            parsed_response = gemini_cli_audit_pr.parse_response_json(response_text)
        elif isinstance(raw_response, Mapping):
            response_text = json.dumps(raw_response, sort_keys=True)
            parsed_response = raw_response
        elif "verdict" in raw_payload or "findings" in raw_payload:
            parsed_response = raw_payload
    if parsed_response is None:
        parsed_response = gemini_cli_audit_pr.parse_response_json(response_text)
    return response_text, parsed_response


def run_grok_build_audit(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    command: str = DEFAULT_GROK_COMMAND,
    expected_head_sha: str | None = None,
    prompt_lenses: tuple[str, ...] = code_mower_prompts.DEFAULT_REVIEW_LENSES,
    prompt_dir: Path | None = None,
    max_diff_bytes: int = gemini_cli_audit_pr.DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: int = gemini_cli_audit_pr.DEFAULT_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    grok_api_key: str | None = None,
    repo_path: Path | None = None,
    base_ref: str = gemini_cli_audit_pr.DEFAULT_BASE_REF,
    allow_historical_head: bool = False,
    historical_calibration: bool = False,
    allow_ambient_home: bool = False,
    context_pack_text: str = "",
) -> dict[str, Any]:
    if not grok_api_key and not allow_ambient_home:
        raise ValueError(
            "Grok Build requires XAI_API_KEY/GROK_DEPLOYMENT_KEY or explicit "
            f"local OAuth opt-in via {GROK_AMBIENT_HOME_ENV}=1 in trusted "
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
            raise GrokBuildHeadChangedError(
                "PR head does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        diff = gemini_cli_audit_pr.fetch_pull_request_diff(
            repo,
            pr_number,
            token=github_token,
        )
    else:
        head_sha, diff = gemini_cli_audit_pr.fetch_local_checkout_diff(
            repo_path,
            base_ref=base_ref,
        )
        diff_source = "local_checkout"
        if normalized_expected and normalized_expected != head_sha.lower():
            raise GrokBuildHeadChangedError(
                "local checkout does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        if (
            not allow_historical_head
            and not historical_calibration
            and head_sha.lower() != pr_head_sha.lower()
        ):
            raise GrokBuildHeadChangedError(
                "local checkout is not at the current PR head; pass "
                "--historical-calibration for archived calibration runs. "
                f"local={head_sha} current_pr={pr_head_sha}."
            )
    if not diff.strip():
        raise ValueError("Grok Build calibration diff is empty; check repo/base refs")
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
        display_name=DEFAULT_GROK_DISPLAY_NAME,
        context_pack_text=context_pack_text,
    )
    diagnostics["diff_source"] = diff_source
    diagnostics["base_ref"] = base_ref if repo_path is not None else None
    diagnostics["cli_transport"] = "prompt_file"
    diagnostics["preserve_ambient_home"] = allow_ambient_home

    started = time.monotonic()
    grok_model = resolve_grok_model()
    with tempfile.TemporaryDirectory(prefix="code-mower-grok-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        home_dir = temp_dir / "home"
        workspace_dir = temp_dir / "workspace"
        home_dir.mkdir()
        workspace_dir.mkdir()
        prompt_path = workspace_dir / f"{DEFAULT_GROK_OUTPUT_STEM}.prompt-input.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        child_env = build_grok_child_env(
            home_dir,
            grok_api_key=grok_api_key,
            preserve_ambient_home=allow_ambient_home,
        )
        # Grok Build supports `--prompt-file` in headless mode. Use it for real
        # audits so large PR diffs do not have to travel through argv.
        grok_args = [
            command,
            "--prompt-file",
            str(prompt_path),
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--disable-web-search",
            "--no-memory",
            "--max-turns",
            "1",
        ]
        if grok_model:
            grok_args.extend(["--model", grok_model])
        completed = subprocess.run(
            grok_args,
            capture_output=True,
            cwd=workspace_dir,
            env=child_env,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    duration_seconds = time.monotonic() - started

    raw_payload: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            loaded = json.loads(completed.stdout)
            if isinstance(loaded, Mapping):
                raw_payload = dict(loaded)
        except json.JSONDecodeError:
            raw_payload = None
    response_text, parsed_response = _response_text_from_grok_payload(
        raw_payload,
        completed.stdout,
    )
    verdict = gemini_cli_audit_pr._validate_verdict(parsed_response)
    if repo_path is None:
        head_after_meta = gemini_cli_audit_pr.fetch_pull_request(
            repo,
            pr_number,
            token=github_token,
        )
        head_after = str(head_after_meta.get("head", {}).get("sha") or "")
        if head_after != head_sha:
            raise GrokBuildHeadChangedError(
                "PR head changed during Grok Build audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )
    else:
        head_after = gemini_cli_audit_pr._local_head_sha(repo_path.expanduser().resolve())
        if head_after != head_sha:
            raise GrokBuildHeadChangedError(
                "local checkout head changed during Grok Build audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )

    payload: dict[str, Any] = {
        "mode": DEFAULT_GROK_MODE,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "head_sha_end": head_after,
        "pr_head_sha": pr_head_sha,
        "command": command,
        "model": grok_model or None,
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
        for key in ("usage", "modelUsage", "total_cost_usd"):
            if key in raw_payload:
                payload[key] = raw_payload[key]
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "prompt": output_dir / f"{DEFAULT_GROK_OUTPUT_STEM}.prompt.txt",
            "response": output_dir / f"{DEFAULT_GROK_OUTPUT_STEM}.response.md",
            "summary": output_dir / f"{DEFAULT_GROK_OUTPUT_STEM}.summary.json",
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
    return text.replace("Gemini CLI audit", "Grok Build audit", 1)


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
        default=os.environ.get("GROK_BUILD_COMMAND", DEFAULT_GROK_COMMAND),
    )
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
        default=_env_flag_enabled(GROK_AMBIENT_HOME_ENV),
        help=(
            "Allow Grok Build to inherit local OAuth/session state from HOME. "
            f"Equivalent env opt-in: {GROK_AMBIENT_HOME_ENV}=1."
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
    grok_api_key = resolve_grok_api_key()
    try:
        context_pack_text = "\n\n".join(
            path.read_text(encoding="utf-8") for path in args.context_pack_file
        )
        payload = run_grok_build_audit(
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
            grok_api_key=grok_api_key,
            repo_path=args.repo_path,
            base_ref=args.base_ref,
            allow_historical_head=args.allow_historical_head,
            historical_calibration=args.historical_calibration,
            allow_ambient_home=args.allow_ambient_home,
            context_pack_text=context_pack_text,
        )
    except GrokBuildHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        GrokBuildUnsupportedError,
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
