#!/usr/bin/env python3
"""Reference dispatcher for the future standalone Code Mower CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools import __version__
    from tools import (
        antigravity_cli_audit_pr,
        blind_review_coordinator,
        bootstrap as code_mower_bootstrap,
        coderabbit_cli_audit_pr,
        hermes_cli_audit_pr,
        code_mower_builder_experiment,
        code_mower_cloud,
        code_mower_config,
        code_mower_calibration,
        code_mower_context_packs,
        code_mower_doctor,
        code_mower_init,
        code_mower_merge,
        code_mower_next_steps,
        code_mower_package,
        code_mower_prompts,
        code_mower_telemetry,
        migration as code_mower_migration,
        gemini_cli_audit_pr,
        local_llm_bakeoff,
        local_llm_calibration,
        local_llm_audit_pr,
        local_llm_profiles,
        reviewer_metrics,
        saas_reviewer_labeler,
        trailer_comment_labeler,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import __version__
    from . import blind_review_coordinator
    from . import antigravity_cli_audit_pr
    from . import bootstrap as code_mower_bootstrap
    from . import builder_experiment as code_mower_builder_experiment
    from . import cloud as code_mower_cloud
    from . import coderabbit_cli_audit_pr
    from . import config as code_mower_config
    from . import code_mower_calibration
    from . import code_mower_context_packs
    from . import code_mower_merge
    from . import next_steps as code_mower_next_steps
    from . import doctor as code_mower_doctor
    from . import gemini_cli_audit_pr
    from . import hermes_cli_audit_pr
    from . import init as code_mower_init
    from . import code_mower_telemetry
    from . import local_llm_bakeoff
    from . import local_llm_calibration
    from . import local_llm_audit_pr
    from . import local_llm_profiles
    from . import migration as code_mower_migration
    from . import package as code_mower_package
    from . import prompts as code_mower_prompts
    from . import reviewer_metrics
    from . import saas_reviewer_labeler
    from . import trailer_comment_labeler


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("antigravity-cli", add_help=False)
    subparsers.add_parser("blind-review", add_help=False)
    subparsers.add_parser("bootstrap", add_help=False)
    subparsers.add_parser("builder-experiment", add_help=False)
    subparsers.add_parser("calibration", add_help=False)
    subparsers.add_parser("cloud", add_help=False)
    subparsers.add_parser("config", add_help=False)
    subparsers.add_parser("context-packs", add_help=False)
    subparsers.add_parser("coderabbit-cli", add_help=False)
    subparsers.add_parser("doctor", add_help=False)
    subparsers.add_parser("gemini-cli", add_help=False)
    subparsers.add_parser("hermes-cli", add_help=False)
    subparsers.add_parser("init", add_help=False)
    subparsers.add_parser("local-llm", add_help=False)
    subparsers.add_parser("migration", add_help=False)
    subparsers.add_parser("merge-plan", add_help=False)
    subparsers.add_parser("next-steps", add_help=False)
    subparsers.add_parser("package", add_help=False)
    subparsers.add_parser("prompts", add_help=False)
    subparsers.add_parser("providers", add_help=False)
    subparsers.add_parser("reviewer-metrics", add_help=False)
    subparsers.add_parser("saas-reviewer-labeler", add_help=False)
    subparsers.add_parser("telemetry", add_help=False)
    subparsers.add_parser("trailer-comment-labeler", add_help=False)
    args, rest = parser.parse_known_args(argv)

    if args.command == "antigravity-cli":
        return antigravity_cli_audit_pr.main(rest)
    if args.command == "blind-review":
        return blind_review_coordinator.main(rest)
    if args.command == "bootstrap":
        return code_mower_bootstrap.main(rest)
    if args.command == "builder-experiment":
        return code_mower_builder_experiment.main(rest)
    if args.command == "calibration":
        return code_mower_calibration.main(rest)
    if args.command == "cloud":
        return code_mower_cloud.main(rest)
    if args.command == "config":
        return _config_main(rest)
    if args.command == "context-packs":
        return code_mower_context_packs.main(rest)
    if args.command == "coderabbit-cli":
        return coderabbit_cli_audit_pr.main(rest)
    if args.command == "doctor":
        return code_mower_doctor.main(rest)
    if args.command == "gemini-cli":
        return gemini_cli_audit_pr.main(rest)
    if args.command == "hermes-cli":
        return hermes_cli_audit_pr.main(rest)
    if args.command == "init":
        options_with_values = {"--profile", "--output-dir"}
        if (
            rest[:1] == ["auth"]
            or _has_flag(rest, "--easy")
            or _has_positional_config(rest, options_with_values)
        ):
            return code_mower_init.main(rest)
        return code_mower_init.main(
            _default_config_args(
                rest,
                default_config="code-mower.yml",
                options_with_values=options_with_values,
            )
        )
    if args.command == "local-llm":
        return _local_llm_main(rest)
    if args.command == "migration":
        return code_mower_migration.main(rest)
    if args.command == "merge-plan":
        return code_mower_merge.main(rest)
    if args.command == "next-steps":
        return code_mower_next_steps.main(rest)
    if args.command == "package":
        return code_mower_package.main(
            _default_config_args(
                rest,
                default_config="code-mower.example.yml",
                options_with_values={
                    "--output-dir",
                    "--package-name",
                    "--provider-templates",
                },
            )
        )
    if args.command == "prompts":
        return code_mower_prompts.main(rest)
    if args.command == "providers":
        return _providers_main(rest)
    if args.command == "reviewer-metrics":
        return reviewer_metrics.main(rest)
    if args.command == "saas-reviewer-labeler":
        return saas_reviewer_labeler.main(rest)
    if args.command == "telemetry":
        return code_mower_telemetry.main(rest)
    if args.command == "trailer-comment-labeler":
        return trailer_comment_labeler.main(rest)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
