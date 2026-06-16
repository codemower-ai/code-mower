#!/usr/bin/env python3
"""Run provider-neutral Code Mower setup and runtime checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import config as code_mower_config
        from code_mower import package as code_mower_package
        from code_mower.doctor_checks import (
            ACTIONS_COST_SAMPLE_DEFAULT,
            ACTIONS_COST_SAMPLE_MAX,
            DEFAULT_CLOUD_TOKEN_DIR,
            DEFAULT_CLOUD_TOKEN_ENV,
            STATUS_FAIL,
            STATUS_PASS,
            STATUS_SKIP,
            STATUS_WARN,
            DoctorCheck,
            DoctorReport,
            auth_probe_output_detail as _auth_probe_output_detail,
            check_cloud_token_surface as _check_cloud_token_surface,
            check_github_auth_surface as _check_github_auth_surface,
            check_github_setup as _check_github_setup,
            check_lane_runtime as _check_lane_runtime,
            check_pytest as _check_pytest,
            check_python_runtime as _check_python_runtime,
            check_ripgrep as _check_ripgrep,
            effective_lane as _effective_lane,
            evaluate_json_probe as _evaluate_json_probe,
            load_inputs as _load_inputs,
            local_cli_probe_remediation as _local_cli_probe_remediation,
            provider_template_coverage as _provider_template_coverage,
            render_doctor_text,
            selected_lanes as _selected_lanes,
            token_file_mentions_cloud_token as _token_file_mentions_cloud_token,
        )
    else:
        from tools import code_mower_config, code_mower_package
        from tools.doctor_checks import (
            ACTIONS_COST_SAMPLE_DEFAULT,
            ACTIONS_COST_SAMPLE_MAX,
            DEFAULT_CLOUD_TOKEN_DIR,
            DEFAULT_CLOUD_TOKEN_ENV,
            STATUS_FAIL,
            STATUS_PASS,
            STATUS_SKIP,
            STATUS_WARN,
            DoctorCheck,
            DoctorReport,
            auth_probe_output_detail as _auth_probe_output_detail,
            check_cloud_token_surface as _check_cloud_token_surface,
            check_github_auth_surface as _check_github_auth_surface,
            check_github_setup as _check_github_setup,
            check_lane_runtime as _check_lane_runtime,
            check_pytest as _check_pytest,
            check_python_runtime as _check_python_runtime,
            check_ripgrep as _check_ripgrep,
            effective_lane as _effective_lane,
            evaluate_json_probe as _evaluate_json_probe,
            load_inputs as _load_inputs,
            local_cli_probe_remediation as _local_cli_probe_remediation,
            provider_template_coverage as _provider_template_coverage,
            render_doctor_text,
            selected_lanes as _selected_lanes,
            token_file_mentions_cloud_token as _token_file_mentions_cloud_token,
        )
elif __package__ == "tools":
    from tools import code_mower_config, code_mower_package
    from tools.doctor_checks import (
        ACTIONS_COST_SAMPLE_DEFAULT,
        ACTIONS_COST_SAMPLE_MAX,
        DEFAULT_CLOUD_TOKEN_DIR,
        DEFAULT_CLOUD_TOKEN_ENV,
        STATUS_FAIL,
        STATUS_PASS,
        STATUS_SKIP,
        STATUS_WARN,
        DoctorCheck,
        DoctorReport,
        auth_probe_output_detail as _auth_probe_output_detail,
        check_cloud_token_surface as _check_cloud_token_surface,
        check_github_auth_surface as _check_github_auth_surface,
        check_github_setup as _check_github_setup,
        check_lane_runtime as _check_lane_runtime,
        check_pytest as _check_pytest,
        check_python_runtime as _check_python_runtime,
        check_ripgrep as _check_ripgrep,
        effective_lane as _effective_lane,
        evaluate_json_probe as _evaluate_json_probe,
        load_inputs as _load_inputs,
        local_cli_probe_remediation as _local_cli_probe_remediation,
        provider_template_coverage as _provider_template_coverage,
        render_doctor_text,
        selected_lanes as _selected_lanes,
        token_file_mentions_cloud_token as _token_file_mentions_cloud_token,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import config as code_mower_config
    from . import package as code_mower_package
    from .doctor_checks import (
        ACTIONS_COST_SAMPLE_DEFAULT,
        ACTIONS_COST_SAMPLE_MAX,
        DEFAULT_CLOUD_TOKEN_DIR,
        DEFAULT_CLOUD_TOKEN_ENV,
        STATUS_FAIL,
        STATUS_PASS,
        STATUS_SKIP,
        STATUS_WARN,
        DoctorCheck,
        DoctorReport,
        auth_probe_output_detail as _auth_probe_output_detail,
        check_cloud_token_surface as _check_cloud_token_surface,
        check_github_auth_surface as _check_github_auth_surface,
        check_github_setup as _check_github_setup,
        check_lane_runtime as _check_lane_runtime,
        check_pytest as _check_pytest,
        check_python_runtime as _check_python_runtime,
        check_ripgrep as _check_ripgrep,
        effective_lane as _effective_lane,
        evaluate_json_probe as _evaluate_json_probe,
        load_inputs as _load_inputs,
        local_cli_probe_remediation as _local_cli_probe_remediation,
        provider_template_coverage as _provider_template_coverage,
        render_doctor_text,
        selected_lanes as _selected_lanes,
        token_file_mentions_cloud_token as _token_file_mentions_cloud_token,
    )


_DOCTOR_COMPAT_EXPORTS = (
    DEFAULT_CLOUD_TOKEN_DIR,
    DEFAULT_CLOUD_TOKEN_ENV,
    STATUS_SKIP,
    STATUS_WARN,
    _auth_probe_output_detail,
    _evaluate_json_probe,
    _local_cli_probe_remediation,
    _token_file_mentions_cloud_token,
)


def resolve_doctor_config_path_for_script(
    config_arg: str,
    *,
    easy: bool = False,
    script_path: Path,
) -> Path:
    path = Path(config_arg)
    if path.is_file() or config_arg != "code-mower.yml" or not easy:
        return path

    script_path = script_path.resolve()
    candidates = [
        script_path.parent / "templates" / "code-mower.example.yml",
        script_path.parents[1] / "code-mower.example.yml",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return path


def resolve_doctor_config_path(config_arg: str, *, easy: bool = False) -> Path:
    return resolve_doctor_config_path_for_script(
        config_arg,
        easy=easy,
        script_path=Path(__file__),
    )


def _global_runtime_checks(
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> tuple[DoctorCheck, ...]:
    return (
        _check_python_runtime(),
        _check_pytest(),
        _check_github_auth_surface(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        ),
        _check_ripgrep(),
    )


def run_doctor(
    *,
    config_path: Path,
    provider_templates_path: Path,
    profile: str | None,
    probe_runtime: bool = False,
    github: bool = False,
    cloud: bool = False,
    http_timeout: int = 5,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
) -> DoctorReport:
    config, templates, checks = _load_inputs(config_path, provider_templates_path)
    if config is None or templates is None:
        return DoctorReport(
            config_path=str(config_path),
            provider_templates_path=str(provider_templates_path),
            profile=profile,
            checks=tuple(checks),
        )

    try:
        lanes = _selected_lanes(config, profile)
    except code_mower_config.ConfigError as exc:
        checks.append(
            DoctorCheck(
                name="profile.select",
                status=STATUS_FAIL,
                message=str(exc),
                remediation=(
                    "Choose an existing profile from code-mower.yml or run "
                    "`code-mower init --easy` to inspect the recommended profile."
                ),
            )
        )
        return DoctorReport(
            config_path=str(config_path),
            provider_templates_path=str(provider_templates_path),
            profile=profile,
            checks=tuple(checks),
        )

    checks.append(
        DoctorCheck(
            name="profile.select",
            status=STATUS_PASS,
            message=(
                f"selected profile {profile}: {', '.join(lanes)}"
                if profile
                else f"selected all lanes: {', '.join(lanes)}"
            ),
            detail={"lanes": list(lanes)},
        )
    )
    checks.append(_provider_template_coverage(lanes, templates))
    checks.extend(
        _global_runtime_checks(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        )
    )

    lane_configs = config.get("lanes")
    if not isinstance(lane_configs, Mapping):
        raise code_mower_config.ConfigError("lanes must be a mapping")
    provider_templates = templates.get("provider_templates")
    if not isinstance(provider_templates, Mapping):
        raise code_mower_config.ConfigError("provider_templates must be a mapping")

    effective_lanes: list[tuple[str, Mapping[str, Any]]] = []
    for lane_id in lanes:
        lane = lane_configs.get(lane_id)
        if not isinstance(lane, Mapping):
            checks.append(
                DoctorCheck(
                    name="lane.load",
                    status=STATUS_FAIL,
                    lane=lane_id,
                    message="selected lane is missing from config",
                    remediation=(
                        "Add the lane to code-mower.yml or remove it from the "
                        "selected profile."
                    ),
                )
            )
            continue
        effective = _effective_lane(lane_id, lane, provider_templates)
        effective_lanes.append((lane_id, effective))
        checks.extend(
            _check_lane_runtime(
                lane_id,
                effective,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )

    if github:
        checks.extend(
            _check_github_setup(
                config=config,
                lanes=effective_lanes,
                http_timeout=http_timeout,
                actions_cost_sample=actions_cost_sample,
            )
        )

    if cloud:
        checks.append(_check_cloud_token_surface())

    return DoctorReport(
        config_path=str(config_path),
        provider_templates_path=str(provider_templates_path),
        profile=profile,
        checks=tuple(checks),
    )


def resolve_doctor_provider_templates_path(path_text: str) -> Path:
    path = Path(path_text)
    if path_text == code_mower_package.DEFAULT_PROVIDER_TEMPLATES and not path.is_absolute():
        project_catalog = Path.cwd() / code_mower_package.DEFAULT_PROVIDER_TEMPLATES
        if project_catalog.exists():
            return project_catalog
    return code_mower_package.resolve_provider_templates_path(path_text)


def _apply_first_run_defaults(args: argparse.Namespace) -> None:
    if not (getattr(args, "v05", False) or getattr(args, "preflight", False)):
        return
    args.easy = True
    if args.profile is None:
        args.profile = "recommended"
    args.probe_runtime = True
    args.github = True
    args.cloud = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="code-mower.yml")
    parser.add_argument(
        "--provider-templates",
        default=code_mower_package.DEFAULT_PROVIDER_TEMPLATES,
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--easy",
        action="store_true",
        help=(
            "first-run alias for --profile recommended; if code-mower.yml is "
            "absent, use the packaged example config"
        ),
    )
    parser.add_argument(
        "--v05",
        action="store_true",
        help=(
            "v0.5 early-adopter preset: --easy --profile recommended "
            "--probe-runtime --github --cloud"
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "friendly alias for the v0.5 first-run preset: --easy "
            "--profile recommended --probe-runtime --github --cloud"
        ),
    )
    parser.add_argument("--probe-runtime", action="store_true")
    parser.add_argument(
        "--github",
        action="store_true",
        help="inspect GitHub repo visibility, branch protection, and provider setup hints",
    )
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="check optional Code Mower Cloud token setup without reading or printing token values",
    )
    parser.add_argument("--http-timeout", type=int, default=5)
    parser.add_argument(
        "--actions-cost-sample",
        type=int,
        default=ACTIONS_COST_SAMPLE_DEFAULT,
        help=(
            "number of recent Actions runs to sample for private-repo cost "
            f"diagnostics, capped at {ACTIONS_COST_SAMPLE_MAX}"
        ),
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    _apply_first_run_defaults(args)
    if args.easy and args.profile is None:
        args.profile = "recommended"

    try:
        provider_templates_path = resolve_doctor_provider_templates_path(args.provider_templates)
        report = run_doctor(
            config_path=resolve_doctor_config_path(args.config, easy=args.easy),
            provider_templates_path=provider_templates_path,
            profile=args.profile,
            probe_runtime=args.probe_runtime,
            github=args.github,
            cloud=args.cloud,
            http_timeout=args.http_timeout,
            actions_cost_sample=args.actions_cost_sample,
        )
    except code_mower_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(render_doctor_text(report), end="")
    if report.failures:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
