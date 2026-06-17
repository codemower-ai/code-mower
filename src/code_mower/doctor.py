#!/usr/bin/env python3
"""Run provider-neutral Code Mower setup and runtime checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import config as code_mower_config
        from code_mower import doctor_checks as _doctor_checks
        from code_mower import package as code_mower_package
    else:
        from tools import code_mower_config, code_mower_package, doctor_checks as _doctor_checks
elif __package__ == "tools":
    from tools import code_mower_config, code_mower_package, doctor_checks as _doctor_checks
else:  # pragma: no cover - exercised after package extraction.
    from . import config as code_mower_config
    from . import doctor_checks as _doctor_checks
    from . import package as code_mower_package


ACTIONS_COST_SAMPLE_DEFAULT = _doctor_checks.ACTIONS_COST_SAMPLE_DEFAULT
ACTIONS_COST_SAMPLE_MAX = _doctor_checks.ACTIONS_COST_SAMPLE_MAX
DEFAULT_CLOUD_TOKEN_DIR = _doctor_checks.DEFAULT_CLOUD_TOKEN_DIR
DEFAULT_CLOUD_TOKEN_ENV = _doctor_checks.DEFAULT_CLOUD_TOKEN_ENV
STATUS_FAIL = _doctor_checks.STATUS_FAIL
STATUS_PASS = _doctor_checks.STATUS_PASS
STATUS_SKIP = _doctor_checks.STATUS_SKIP
STATUS_WARN = _doctor_checks.STATUS_WARN
DoctorCheck = _doctor_checks.DoctorCheck
DoctorReport = _doctor_checks.DoctorReport
_auth_probe_output_detail = _doctor_checks.auth_probe_output_detail
_check_cloud_token_surface = _doctor_checks.check_cloud_token_surface
_evaluate_json_probe = _doctor_checks.evaluate_json_probe
_local_cli_probe_remediation = _doctor_checks.local_cli_probe_remediation
render_doctor_text = _doctor_checks.render_doctor_text
resolve_doctor_config_path = _doctor_checks.resolve_doctor_config_path
resolve_doctor_config_path_for_script = _doctor_checks.resolve_doctor_config_path_for_script
resolve_doctor_provider_templates_path = _doctor_checks.resolve_doctor_provider_templates_path
run_doctor = _doctor_checks.run_doctor
_token_file_mentions_cloud_token = _doctor_checks.token_file_mentions_cloud_token
_apply_first_run_defaults = _doctor_checks.apply_first_run_defaults


_DOCTOR_COMPAT_EXPORTS = (
    DEFAULT_CLOUD_TOKEN_DIR,
    DEFAULT_CLOUD_TOKEN_ENV,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    DoctorCheck,
    DoctorReport,
    _auth_probe_output_detail,
    _check_cloud_token_surface,
    _evaluate_json_probe,
    _apply_first_run_defaults,
    _local_cli_probe_remediation,
    resolve_doctor_config_path_for_script,
    resolve_doctor_provider_templates_path,
    _token_file_mentions_cloud_token,
)


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
