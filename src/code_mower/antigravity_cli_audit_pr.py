#!/usr/bin/env python3
"""Run Antigravity CLI as an informational Code Mower calibration reviewer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

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


DEFAULT_ANTIGRAVITY_COMMAND = "agy"
DEFAULT_ANTIGRAVITY_MODE = "antigravity-cli-audit"
DEFAULT_ANTIGRAVITY_OUTPUT_STEM = "antigravity-cli"
DEFAULT_ANTIGRAVITY_DISPLAY_NAME = "Antigravity CLI"
ANTIGRAVITY_SETTINGS_SUBDIRS = (".gemini", ".gemini/antigravity-cli")
ANTIGRAVITY_ALTERNATE_COMMANDS = ("antigravity",)
ANTIGRAVITY_AMBIENT_HOME_ENV = "ANTIGRAVITY_CLI_USE_AMBIENT_HOME"

AntigravityCliHeadChangedError = gemini_cli_audit_pr.GeminiCliHeadChangedError
AntigravityCliUnsupportedError = gemini_cli_audit_pr.GeminiCliUnsupportedError


def resolve_antigravity_api_key() -> str:
    """Antigravity CLI 1.0.7 uses local OAuth; Gemini API keys are not auth."""

    return ""


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_antigravity_command(command: str) -> str:
    """Resolve the configured Antigravity command, honoring documented aliases."""

    if shutil.which(command):
        return command
    if command == DEFAULT_ANTIGRAVITY_COMMAND:
        for alternate in ANTIGRAVITY_ALTERNATE_COMMANDS:
            if shutil.which(alternate):
                return alternate
    return command


def run_antigravity_cli_audit(
    *,
    repo: str,
    pr_number: int,
    github_token: str,
    command: str = DEFAULT_ANTIGRAVITY_COMMAND,
    expected_head_sha: str | None = None,
    prompt_lenses: tuple[str, ...] = code_mower_prompts.DEFAULT_REVIEW_LENSES,
    prompt_dir: Path | None = None,
    max_diff_bytes: int = gemini_cli_audit_pr.DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: int = gemini_cli_audit_pr.DEFAULT_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    antigravity_api_key: str | None = None,
    repo_path: Path | None = None,
    base_ref: str = gemini_cli_audit_pr.DEFAULT_BASE_REF,
    allow_historical_head: bool = False,
    historical_calibration: bool = False,
    allow_ambient_home: bool = False,
) -> dict[str, object]:
    if antigravity_api_key:
        raise ValueError(
            "Antigravity CLI does not currently support Gemini API keys as "
            "noninteractive auth. Use local OAuth with "
            f"{ANTIGRAVITY_AMBIENT_HOME_ENV}=1 in trusted environments."
        )
    use_ambient_home = allow_ambient_home
    if not use_ambient_home:
        raise ValueError(
            "Antigravity CLI local OAuth auth requires explicit ambient-home "
            f"opt-in. Set {ANTIGRAVITY_AMBIENT_HOME_ENV}=1 only in a trusted "
            "local environment, or use a future Antigravity noninteractive auth "
            "mode when available."
        )
    return gemini_cli_audit_pr.run_gemini_cli_audit(
        repo=repo,
        pr_number=pr_number,
        github_token=github_token,
        command=resolve_antigravity_command(command),
        expected_head_sha=expected_head_sha,
        prompt_lenses=prompt_lenses,
        prompt_dir=prompt_dir,
        max_diff_bytes=max_diff_bytes,
        timeout_seconds=timeout_seconds,
        output_dir=output_dir,
        gemini_api_key=None,
        repo_path=repo_path,
        base_ref=base_ref,
        allow_historical_head=allow_historical_head,
        historical_calibration=historical_calibration,
        mode=DEFAULT_ANTIGRAVITY_MODE,
        output_stem=DEFAULT_ANTIGRAVITY_OUTPUT_STEM,
        display_name=DEFAULT_ANTIGRAVITY_DISPLAY_NAME,
        settings_subdirs=() if use_ambient_home else ANTIGRAVITY_SETTINGS_SUBDIRS,
        model_env="ANTIGRAVITY_MODEL",
        child_env_exclude=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"),
        cli_transport="prompt_file",
        preserve_ambient_home=use_ambient_home,
    )


def render_text(payload: dict[str, object]) -> str:
    text = gemini_cli_audit_pr.render_text(payload)
    return text.replace("Gemini CLI audit", "Antigravity CLI audit", 1)


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
        default=os.environ.get("ANTIGRAVITY_CLI_COMMAND", DEFAULT_ANTIGRAVITY_COMMAND),
    )
    parser.add_argument(
        "--prompt-lenses",
        default=",".join(code_mower_prompts.DEFAULT_REVIEW_LENSES),
    )
    parser.add_argument("--prompt-dir", type=Path, default=None)
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
    antigravity_api_key = resolve_antigravity_api_key() or None
    try:
        payload = run_antigravity_cli_audit(
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
            antigravity_api_key=antigravity_api_key,
            repo_path=args.repo_path,
            base_ref=args.base_ref,
            allow_historical_head=args.allow_historical_head,
            historical_calibration=args.historical_calibration,
            allow_ambient_home=_env_flag_enabled(ANTIGRAVITY_AMBIENT_HOME_ENV),
        )
    except AntigravityCliHeadChangedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        AntigravityCliUnsupportedError,
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
