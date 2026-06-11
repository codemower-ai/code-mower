#!/usr/bin/env python3
"""Run Hermes Agent as an informational Code Mower calibration reviewer."""

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
    else:
        from tools import gemini_cli_audit_pr, code_mower_prompts
elif __package__ == "tools":
    from tools import gemini_cli_audit_pr, code_mower_prompts
else:  # pragma: no cover - exercised after package extraction.
    from . import gemini_cli_audit_pr
    from . import prompts as code_mower_prompts


DEFAULT_HERMES_COMMAND = "hermes"
DEFAULT_HERMES_MODE = "hermes-cli-audit"
DEFAULT_HERMES_OUTPUT_STEM = "hermes-cli"
DEFAULT_HERMES_DISPLAY_NAME = "Hermes CLI"
DEFAULT_HERMES_MODEL_ENV = "HERMES_INFERENCE_MODEL"
DEFAULT_HERMES_PROVIDER_ENV = "HERMES_PROVIDER"
HERMES_AMBIENT_HOME_ENV = "HERMES_CLI_USE_AMBIENT_HOME"
HERMES_HELP_SENTINELS = (
    "--oneshot",
    "--ignore-user-config",
    "--ignore-rules",
    "--toolsets",
)
HERMES_ENV_ALLOWLIST = (
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

HermesCliHeadChangedError = gemini_cli_audit_pr.GeminiCliHeadChangedError
HermesCliUnsupportedError = gemini_cli_audit_pr.GeminiCliUnsupportedError


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_hermes_child_env(home_dir: Path, *, preserve_ambient_home: bool) -> dict[str, str]:
    child_env = {
        key: value
        for key in HERMES_ENV_ALLOWLIST
        if (value := os.environ.get(key))
    }
    if preserve_ambient_home:
        for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            if os.environ.get(key):
                child_env[key] = os.environ[key]
    else:
        child_env["HOME"] = str(home_dir)
        child_env["HERMES_HOME"] = str(home_dir / ".hermes")
        child_env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
        child_env["XDG_CACHE_HOME"] = str(home_dir / ".cache")
        child_env["XDG_STATE_HOME"] = str(home_dir / ".local" / "state")
    # Keep oneshot review deterministic and quiet. The CLI still needs ambient
    # auth in current releases, but Code Mower does not need project rules,
    # tool progress, or default toolsets for a diff-only calibration review.
    child_env["HERMES_IGNORE_USER_CONFIG"] = "1"
    child_env["HERMES_IGNORE_RULES"] = "1"
    child_env["HERMES_CORE_TOOLS"] = ""
    child_env["HERMES_TOOL_PROGRESS"] = "0"
    child_env["HERMES_QUIET"] = "1"
    return child_env


def verify_hermes_oneshot_contract(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
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
    missing = [sentinel for sentinel in HERMES_HELP_SENTINELS if sentinel not in help_text]
    if completed.returncode != 0 or missing:
        raise HermesCliUnsupportedError(
            "Hermes CLI must support oneshot mode plus config/rules/toolset "
            f"controls for Code Mower calibration. Missing help sentinel(s): {missing!r}."
        )


def hermes_prompt_file_arg(prompt_path: Path) -> str:
    """Return Hermes' single-query context-reference argument for a prompt file."""
    # Hermes expands @ context references such as @file.txt in single-query and
    # oneshot flows before sending the message to the model. Keep the prompt
    # file in the subprocess cwd so argv contains only this short reference,
    # not the full PR diff and audit prompt.
    return f"@{prompt_path.name}"


def run_hermes_cli_audit(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    command: str = DEFAULT_HERMES_COMMAND,
    expected_head_sha: str | None = None,
    prompt_lenses: tuple[str, ...] = code_mower_prompts.DEFAULT_REVIEW_LENSES,
    prompt_dir: Path | None = None,
    max_diff_bytes: int = gemini_cli_audit_pr.DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: int = gemini_cli_audit_pr.DEFAULT_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    repo_path: Path | None = None,
    base_ref: str = gemini_cli_audit_pr.DEFAULT_BASE_REF,
    allow_historical_head: bool = False,
    historical_calibration: bool = False,
    allow_ambient_home: bool = False,
    model: str | None = None,
    provider: str | None = None,
    context_pack_text: str = "",
) -> dict[str, Any]:
    if not allow_ambient_home:
        raise ValueError(
            "Hermes CLI oneshot currently uses local Hermes auth/session state. "
            f"Set {HERMES_AMBIENT_HOME_ENV}=1 only in a trusted local "
            "environment to opt into inheriting that ambient state."
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
            raise HermesCliHeadChangedError(
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
            raise HermesCliHeadChangedError(
                "local checkout does not match calibration corpus; "
                f"expected {expected_head_sha}, current={head_sha}."
            )
        if (
            not allow_historical_head
            and not historical_calibration
            and head_sha.lower() != pr_head_sha.lower()
        ):
            raise HermesCliHeadChangedError(
                "local checkout is not at the current PR head; pass "
                "--historical-calibration for archived calibration runs. "
                f"local={head_sha} current_pr={pr_head_sha}."
            )
    if not diff.strip():
        raise ValueError(
            "Hermes CLI calibration diff is empty; check --repo-path and --base-ref"
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
        display_name=DEFAULT_HERMES_DISPLAY_NAME,
        context_pack_text=context_pack_text,
    )
    diagnostics["diff_source"] = diff_source
    diagnostics["base_ref"] = base_ref if repo_path is not None else None
    diagnostics["cli_transport"] = "oneshot_at_context_file_arg"
    diagnostics["preserve_ambient_home"] = True
    diagnostics["ignore_user_config"] = True
    diagnostics["ignore_rules"] = True
    diagnostics["toolsets"] = ""

    hermes_model = (model or os.environ.get("HERMES_CLI_MODEL") or os.environ.get(DEFAULT_HERMES_MODEL_ENV) or "").strip()
    hermes_provider = (provider or os.environ.get("HERMES_CLI_PROVIDER") or os.environ.get(DEFAULT_HERMES_PROVIDER_ENV) or "").strip()

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="code-mower-hermes-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        home_dir = temp_dir / "home"
        workspace_dir = temp_dir / "workspace"
        home_dir.mkdir()
        workspace_dir.mkdir()
        child_env = build_hermes_child_env(
            home_dir,
            preserve_ambient_home=True,
        )
        verify_hermes_oneshot_contract(command, cwd=workspace_dir, env=child_env)
        prompt_path = workspace_dir / "code-mower-hermes-prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        hermes_args = [
            command,
            "--ignore-user-config",
            "--ignore-rules",
            "--toolsets",
            "",
        ]
        if hermes_model:
            hermes_args.extend(["--model", hermes_model])
        if hermes_provider:
            hermes_args.extend(["--provider", hermes_provider])
        hermes_args.extend(["--oneshot", hermes_prompt_file_arg(prompt_path)])
        completed = subprocess.run(
            hermes_args,
            capture_output=True,
            cwd=workspace_dir,
            env=child_env,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    duration_seconds = time.monotonic() - started

    response_text = completed.stdout
    parsed_response = gemini_cli_audit_pr.parse_response_json(response_text)
    verdict = gemini_cli_audit_pr._validate_verdict(parsed_response)
    if repo_path is None:
        head_after_meta = gemini_cli_audit_pr.fetch_pull_request(repo, pr_number, token=github_token)
        head_after = str(head_after_meta.get("head", {}).get("sha") or "")
        if head_after != head_sha:
            raise HermesCliHeadChangedError(
                "PR head changed during Hermes CLI audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )
    else:
        head_after = gemini_cli_audit_pr._local_head_sha(repo_path.expanduser().resolve())
        if head_after != head_sha:
            raise HermesCliHeadChangedError(
                "local checkout head changed during Hermes CLI audit; "
                f"start={head_sha} end={head_after}. Discard this run and rerun."
            )

    payload: dict[str, Any] = {
        "mode": DEFAULT_HERMES_MODE,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "head_sha_end": head_after,
        "pr_head_sha": pr_head_sha,
        "command": command,
        "model": hermes_model or None,
        "provider": hermes_provider or None,
        "returncode": completed.returncode,
        "duration_seconds": round(duration_seconds, 3),
        "diagnostics": diagnostics,
        "response_text": response_text,
        "parsed_response": parsed_response,
        "verdict": verdict,
        "stderr": completed.stderr,
        "historical_calibration": historical_calibration,
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "prompt": output_dir / f"{DEFAULT_HERMES_OUTPUT_STEM}.prompt.txt",
            "response": output_dir / f"{DEFAULT_HERMES_OUTPUT_STEM}.response.md",
            "summary": output_dir / f"{DEFAULT_HERMES_OUTPUT_STEM}.summary.json",
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
    return text.replace("Gemini CLI audit", "Hermes CLI audit", 1)


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
    parser.add_argument("--base-ref", default=gemini_cli_audit_pr.DEFAULT_BASE_REF)
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
        default=os.environ.get("HERMES_CLI_COMMAND", DEFAULT_HERMES_COMMAND),
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
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
    parser.add_argument(
        "--max-diff-bytes",
        type=int,
        default=gemini_cli_audit_pr.DEFAULT_MAX_DIFF_BYTES,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=gemini_cli_audit_pr.DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
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
        payload = run_hermes_cli_audit(
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
            repo_path=args.repo_path,
            base_ref=args.base_ref,
            allow_historical_head=args.allow_historical_head,
            historical_calibration=args.historical_calibration,
            allow_ambient_home=_env_flag_enabled(HERMES_AMBIENT_HOME_ENV),
            model=args.model,
            provider=args.provider,
            context_pack_text=context_pack_text,
        )
    except HermesCliHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        HermesCliUnsupportedError,
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
    return 0 if gemini_cli_audit_pr._verdict_is_usable(payload.get("verdict")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
