#!/usr/bin/env python3
"""Command dispatcher for the packaged Code Mower CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
if __package__ in {None, ""}:
    raise SystemExit(
        "code_mower.cli is a packaged entrypoint. Install Code Mower with "
        "`pipx install code-mower==0.5.0b37`, or run source checkouts with "
        "`PYTHONPATH=src python -m code_mower.cli`."
    )

from . import __version__
from . import antigravity_cli_audit_pr
from . import blind_review_coordinator
from . import bootstrap as code_mower_bootstrap
from . import builder_experiment as code_mower_builder_experiment
from . import checks as code_mower_checks
from . import claude_audit_pr
from . import claude_cli_bounce
from . import clear_stale
from . import cloud as code_mower_cloud
from . import coderabbit_cli_audit_pr
from . import codex_audit_env_preflight
from . import codex_audit_pr
from . import codex_audit_schema_smoke
from . import code_mower_calibration
from . import code_mower_context_packs
from . import code_mower_merge
from . import code_mower_telemetry
from . import config as code_mower_config
from . import doctor as code_mower_doctor
from . import gemini_cli_audit_pr
from . import hermes_cli_audit_pr
from . import init as code_mower_init
from . import local_llm_audit_pr
from . import local_llm_bakeoff
from . import local_llm_calibration
from . import local_llm_profiles
from . import migration as code_mower_migration
from . import next_steps as code_mower_next_steps
from . import package as code_mower_package
from . import prompts as code_mower_prompts
from . import reviewer_metrics
from . import saas_reviewer_labeler
from . import trailer_comment_labeler
from . import work_orders as code_mower_work_orders
from .provider_registry import REFERENCE_PROVIDERS
from .providers import build_provider_model_env_report


def _has_positional_config(argv: list[str], options_with_values: set[str]) -> bool:
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if any(arg.startswith(f"{option}=") for option in options_with_values):
            continue
        if arg.startswith("-"):
            continue
        return True
    return False


def _default_config_args(
    argv: list[str],
    default_config: str = "code-mower.yml",
    options_with_values: set[str] | None = None,
) -> list[str]:
    if _has_positional_config(argv, options_with_values or set()):
        return argv
    return [default_config, *argv]


def _has_flag(argv: list[str], flag: str) -> bool:
    return any(arg == flag for arg in argv)


def _resolve_provider_templates_path(path_text: str) -> Path:
    return code_mower_package.resolve_provider_templates_path(path_text)


def _config_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="code-mower config")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("config", nargs="?", default="code-mower.yml")
    plan = subparsers.add_parser("plan")
    plan.add_argument("config", nargs="?", default="code-mower.yml")
    plan.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "validate":
        return code_mower_config.main([args.config, "--validate-only"])
    if args.command == "plan":
        forwarded = [args.config]
        if args.json:
            forwarded.append("--json")
        return code_mower_config.main(forwarded)
    raise AssertionError(f"unhandled config command: {args.command}")


def _providers_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="code-mower providers")
    parser.add_argument(
        "--provider-templates",
        default=None,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    show = subparsers.add_parser("show")
    show.add_argument("provider")
    show.add_argument("--json", action="store_true")
    provenance_env = subparsers.add_parser("provenance-env")
    provenance_env.add_argument(
        "--provider",
        action="append",
        default=[],
        help="Limit to a lane id or provider name. May be passed more than once.",
    )
    provenance_env.add_argument("--json", action="store_true")
    provenance_env.add_argument(
        "--shell",
        action="store_true",
        help="Print export command templates for missing model provenance.",
    )
    args = parser.parse_args(argv)

    try:
        provider_templates_path = (
            _resolve_provider_templates_path(code_mower_package.DEFAULT_PROVIDER_TEMPLATES)
            if args.provider_templates is None
            else Path(args.provider_templates)
        )
        templates = code_mower_package.load_provider_templates(
            provider_templates_path
        )
    except code_mower_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    providers = templates["provider_templates"]

    if args.command == "list":
        for provider in sorted(providers):
            print(provider)
        return 0

    if args.command == "provenance-env":
        report = build_provider_model_env_report(
            REFERENCE_PROVIDERS,
            providers=tuple(args.provider),
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        elif args.shell:
            for row in report["providers"]:
                if row["action"] == "set_model_env":
                    print(f"# {row['lane_id']} ({row['provider']})")
                    print(row["export_command"])
            if not any(row["action"] == "set_model_env" for row in report["providers"]):
                print("# No missing model-provenance env vars found.")
        else:
            print("Code Mower provider model provenance env setup")
            print(f"Status: {report['status']}")
            print(f"Providers: {report['provider_count']}")
            print(f"Missing model env: {report['missing_model_env_count']}")
            print(f"Missing CLI version probes: {report['missing_version_probe_count']}")
            for row in report["providers"]:
                if row["action"] == "set_model_env":
                    aliases = ", ".join(row["env_names"]) or "none"
                    print(
                        f"- {row['lane_id']}: set {row['preferred_env']} "
                        f"(aliases: {aliases})"
                    )
                elif row["driver"] == "local_cli" and row["version_source"] == "missing":
                    print(f"- {row['lane_id']}: CLI version probe is missing")
                elif row["model_source"] == "vendor_hidden":
                    print(f"- {row['lane_id']}: provider hides model/version metadata")
                elif row["model_source"] == "default":
                    print(f"- {row['lane_id']}: using configured default model")
                elif row["model_source"].startswith("profile:"):
                    print(f"- {row['lane_id']}: using profile model")
                elif row["model_source"] == "env":
                    print(f"- {row['lane_id']}: model env is configured")
                elif row["model_source"] == "not_applicable":
                    print(f"- {row['lane_id']}: model env is not applicable")
                else:
                    print(f"- {row['lane_id']}: model provenance is unknown")
        if report["unknown_providers"]:
            print(
                "error: unknown provider(s): "
                + ", ".join(report["unknown_providers"]),
                file=sys.stderr,
            )
            return 1
        return 0

    if args.command == "show":
        provider = providers.get(args.provider)
        if not isinstance(provider, dict):
            print(f"error: unknown provider: {args.provider}", file=sys.stderr)
            return 1
        token_env = provider.get("token_env", [])
        review_hygiene = provider.get("review_hygiene", {})
        if not token_env and isinstance(review_hygiene, dict) and review_hygiene.get("token_env"):
            token_env = [review_hygiene["token_env"]]
        data = {
            "lane_id": provider.get("lane_id", args.provider),
            "type": provider.get("type", ""),
            "driver": provider.get("driver", ""),
            "provider": provider.get("provider", ""),
            "adapter": provider.get("adapter"),
            "events": list(provider.get("events", [])),
            "token_env": list(token_env),
            "token_env_any": list(provider.get("token_env_any", [])),
            "informational": bool(provider.get("informational", False)),
            "merge_authority": bool(provider.get("merge_authority", False)),
            "enabled_by_default": provider.get("enabled_by_default", True),
            "trigger_policy": provider.get("trigger_policy", "label"),
            "spend_policy": provider.get("spend_policy", "none"),
            "provider_config": provider.get("provider_config", {}),
        }
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            print(f"{data['lane_id']}: {data['driver']} / {data['provider']}")
            print(f"spend_policy: {data['spend_policy']}")
        return 0

    raise AssertionError(f"unhandled providers command: {args.command}")


def _fetch_local_llm_models(api_base: str, api_key: str, timeout: int = 10) -> list[str]:
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [
        str(entry.get("id"))
        for entry in data
        if isinstance(entry, dict) and entry.get("id")
    ]


def _local_llm_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="code-mower local-llm")
    subparsers = parser.add_subparsers(dest="command", required=True)
    profiles = subparsers.add_parser("profiles")
    profiles.add_argument("--json", action="store_true")
    probe = subparsers.add_parser("probe")
    probe.add_argument(
        "--profile",
        choices=local_llm_profiles.profile_ids(),
        default=None,
    )
    probe.add_argument(
        "--api-base",
        default=None,
    )
    probe.add_argument(
        "--api-key",
        default=None,
    )
    probe.add_argument(
        "--http-timeout",
        type=int,
        default=None,
    )
    probe.add_argument("--json", action="store_true")
    subparsers.add_parser("audit", add_help=False)
    subparsers.add_parser("bakeoff", add_help=False)
    subparsers.add_parser("calibrate", add_help=False)
    args, rest = parser.parse_known_args(argv)

    if args.command == "audit":
        return local_llm_audit_pr.main(rest)
    if args.command == "bakeoff":
        return local_llm_bakeoff.main(rest)
    if args.command == "calibrate":
        return local_llm_calibration.main(rest)
    if args.command == "profiles":
        data = [profile.as_dict() for profile in local_llm_profiles.list_profiles()]
        if args.json:
            print(json.dumps({"profiles": data}, indent=2, sort_keys=True))
        else:
            for profile in data:
                print(f"{profile['profile_id']}: {profile['model']} @ {profile['api_base']}")
        return 0
    if args.command == "probe":
        try:
            probe_timeout = (
                args.http_timeout
                if args.http_timeout is not None
                else int(
                    os.environ.get(
                        "LOCAL_LLM_PROBE_HTTP_TIMEOUT",
                        os.environ.get("LOCAL_LLM_HTTP_TIMEOUT", 10),
                    )
                )
            )
        except ValueError as exc:
            print(
                f"error: local LLM probe timeout must be an integer: {exc}",
                file=sys.stderr,
            )
            return 1
        try:
            profile_id = args.profile or os.environ.get("LOCAL_LLM_PROFILE") or ""
            profile = (
                local_llm_profiles.get_profile(profile_id)
                if profile_id
                else None
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        api_base = (
            args.api_base
            or os.environ.get("LOCAL_LLM_API_BASE")
            or (profile.api_base if profile else local_llm_audit_pr.DEFAULT_API_BASE)
        )
        api_key = (
            args.api_key
            or os.environ.get("LOCAL_LLM_API_KEY")
            or (profile.api_key if profile else local_llm_audit_pr.DEFAULT_API_KEY)
        )
        resolved_profile_id = profile.profile_id if profile else profile_id
        try:
            models = _fetch_local_llm_models(
                api_base,
                api_key,
                probe_timeout,
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"error: local LLM probe failed: {exc}", file=sys.stderr)
            return 1
        payload = {
            "api_base": api_base,
            "profile_id": resolved_profile_id,
            "models": models,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            if resolved_profile_id:
                print(f"profile: {resolved_profile_id}")
            print(f"api_base: {api_base}")
            if models:
                for model in models:
                    print(model)
            else:
                print("no models reported")
        return 0
    raise AssertionError(f"unhandled local-llm command: {args.command}")


CommandHandler = Callable[[list[str]], int]


COMMAND_DESCRIPTIONS: dict[str, str] = {
    "antigravity-cli": "Run an Antigravity/Gemini CLI structured audit lane.",
    "blind-review": "Coordinate hidden/blind review artifacts.",
    "bootstrap": "Bootstrap generated support files and workflow fixtures.",
    "builder-experiment": "Capture builder-side experiment metadata.",
    "calibration": "Create corpora, dispositions, policy, and value reports.",
    "claude-audit": "Run a Claude structured audit lane.",
    "claude-bounce": "Diagnose and retry Claude CLI auth with a clean subprocess env.",
    "clear-stale": "Clear stale terminal audit labels after a PR head changes.",
    "checks": "Detect and run a repository's native lint/test/build surface.",
    "cloud": "Export or upload sanitized benchmark metadata.",
    "config": "Validate or inspect a Code Mower config.",
    "context": "Record local external planning context manifests.",
    "context-packs": "Build selective surrounding-file context packs.",
    "coderabbit-cli": "Run a CodeRabbit CLI informational lane.",
    "codex-audit": "Run a Codex structured audit lane.",
    "codex-audit-env-preflight": "Probe Codex audit environment setup.",
    "codex-audit-schema-smoke": "Smoke-test Codex audit schema parsing.",
    "doctor": "Check runtime, GitHub, providers, privacy, and cloud setup.",
    "gemini-cli": "Run a Gemini CLI structured audit lane.",
    "hermes-cli": "Run a Hermes CLI structured audit lane.",
    "init": "Render safe easy-mode setup output.",
    "local-llm": "Probe and run local OpenAI-compatible model lanes.",
    "migration": "Rehearse standalone package and wrapper migration paths.",
    "merge-plan": "Inspect merge-readiness signals and lane labels.",
    "next-steps": "Print the recommended next actions after setup.",
    "package": "Build or inspect package extraction artifacts.",
    "plan": "Create local issue-derived planning artifacts.",
    "project-context": "Create editable local project-context doctrine docs.",
    "prompts": "Inspect prompt/lens customization artifacts.",
    "providers": "List or inspect provider template definitions.",
    "reviewer-metrics": "Compute reviewer metrics and value reports.",
    "saas-reviewer-labeler": "Apply labels from hosted reviewer comments.",
    "telemetry": "Inspect benchmark telemetry/event helpers.",
    "trailer-comment-labeler": "Apply labels from structured audit trailers.",
    "work-order": "Draft implementation work orders and builder experiment seeds.",
}


FIRST_USER_COMMANDS = (
    "init",
    "doctor",
    "checks",
    "next-steps",
    "calibration",
    "reviewer-metrics",
    "cloud",
    "config",
    "project-context",
)


def _init_main(argv: list[str]) -> int:
    if argv[:1] == ["project-context"]:
        return code_mower_work_orders.project_context_main(["init", *argv[1:]])
    options_with_values = {"--profile", "--output-dir"}
    if (
        argv[:1] == ["auth"]
        or _has_flag(argv, "--easy")
        or _has_positional_config(argv, options_with_values)
    ):
        return code_mower_init.main(argv)
    return code_mower_init.main(
        _default_config_args(
            argv,
            default_config="code-mower.yml",
            options_with_values=options_with_values,
        )
    )


def _package_main(argv: list[str]) -> int:
    return code_mower_package.main(argv)


def _advanced_commands() -> tuple[str, ...]:
    return tuple(command for command in COMMAND_HANDLERS if command not in FIRST_USER_COMMANDS)


def _format_command_rows(commands: tuple[str, ...]) -> list[str]:
    width = max(len(command) for command in commands)
    return [
        f"  {command:<{width}}  {COMMAND_DESCRIPTIONS.get(command, '')}"
        for command in commands
    ]


def _top_level_help(show_all: bool) -> str:
    first_user_rows = "\n".join(_format_command_rows(FIRST_USER_COMMANDS))
    lines = [
        "usage: code-mower [--version] <command> [args]",
        "",
        "Code Mower creates local-first AI peer-review and calibration loops.",
        "",
        "First-user commands:",
        first_user_rows,
    ]
    if show_all:
        advanced_rows = "\n".join(_format_command_rows(_advanced_commands()))
        lines.extend(
            [
                "",
                "Advanced/provider/operator commands:",
                advanced_rows,
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Run code-mower --help-all to show advanced provider/operator commands.",
            ]
        )
    lines.extend(
        [
            "",
            "Common first run:",
            "  code-mower init --easy",
            "  code-mower doctor --preflight",
            "  code-mower next-steps --profile recommended",
            (
                "  code-mower migration package-install-rehearsal "
                f"--package-spec {code_mower_next_steps.current_alpha_package_spec()} --json"
            ),
        ]
    )
    return "\n".join(lines) + "\n"


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "antigravity-cli": antigravity_cli_audit_pr.main,
    "blind-review": blind_review_coordinator.main,
    "bootstrap": code_mower_bootstrap.main,
    "builder-experiment": code_mower_builder_experiment.main,
    "calibration": code_mower_calibration.main,
    "claude-audit": claude_audit_pr.main,
    "claude-bounce": claude_cli_bounce.main,
    "clear-stale": clear_stale.main,
    "checks": code_mower_checks.main,
    "cloud": code_mower_cloud.main,
    "config": _config_main,
    "context": code_mower_work_orders.context_main,
    "context-packs": code_mower_context_packs.main,
    "coderabbit-cli": coderabbit_cli_audit_pr.main,
    "codex-audit": codex_audit_pr.main,
    "codex-audit-env-preflight": codex_audit_env_preflight.main,
    "codex-audit-schema-smoke": codex_audit_schema_smoke.main,
    "doctor": code_mower_doctor.main,
    "gemini-cli": gemini_cli_audit_pr.main,
    "hermes-cli": hermes_cli_audit_pr.main,
    "init": _init_main,
    "local-llm": _local_llm_main,
    "migration": code_mower_migration.main,
    "merge-plan": code_mower_merge.main,
    "next-steps": code_mower_next_steps.main,
    "package": _package_main,
    "plan": code_mower_work_orders.plan_main,
    "project-context": code_mower_work_orders.project_context_main,
    "prompts": code_mower_prompts.main,
    "providers": _providers_main,
    "reviewer-metrics": reviewer_metrics.main,
    "saas-reviewer-labeler": saas_reviewer_labeler.main,
    "telemetry": code_mower_telemetry.main,
    "trailer-comment-labeler": trailer_comment_labeler.main,
    "work-order": code_mower_work_orders.work_order_main,
}


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv or raw_argv == ["-h"] or raw_argv == ["--help"]:
        print(_top_level_help(show_all=False), end="")
        return 0
    if raw_argv == ["--help-all"]:
        print(_top_level_help(show_all=True), end="")
        return 0

    parser = argparse.ArgumentParser(prog="code-mower", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--help-all", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMAND_HANDLERS:
        subparsers.add_parser(command, add_help=False)
    args, rest = parser.parse_known_args(raw_argv)

    if args.help:
        print(_top_level_help(show_all=False), end="")
        return 0
    if args.help_all:
        print(_top_level_help(show_all=True), end="")
        return 0

    try:
        handler = COMMAND_HANDLERS[args.command]
    except KeyError as exc:  # pragma: no cover - argparse validates commands.
        raise AssertionError(f"unhandled command: {args.command}") from exc
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
