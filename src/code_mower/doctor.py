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
detect_repo_slug = _doctor_checks.detect_repo_slug
normalize_repo_slug = _doctor_checks.normalize_repo_slug


def _source_repo_uses_starter_config(cwd: Path, config_path: Path) -> bool:
    """Return true when doctor is running from Code Mower's source tree."""

    try:
        config_path.resolve().relative_to(cwd.resolve())
    except ValueError:
        return False
    return (
        config_path.name == "code-mower.example.yml"
        and (cwd / "pyproject.toml").is_file()
        and (cwd / "src" / "code_mower" / "templates" / "code-mower.example.yml").is_file()
    )


def _doctor_config_source_label(
    *,
    config_arg: str,
    config_path: Path,
    easy: bool,
    cwd: Path | None = None,
) -> str:
    """Classify the config source for adoption-facing doctor output."""

    cwd = cwd or Path.cwd()
    if config_arg != "code-mower.yml":
        return "explicit_config"
    if config_path.name == "code-mower.example.yml" and easy:
        if _source_repo_uses_starter_config(cwd, config_path):
            return "source_tree_starter"
        return "packaged_starter"
    return "repository_config"


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
    _doctor_config_source_label,
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
    parser.add_argument(
        "--adoption",
        action="store_true",
        help=(
            "first-run adoption preset: run preflight checks against the "
            "explicit or inferred GitHub repository and surface setup gaps"
        ),
    )
    pilot_group = parser.add_mutually_exclusive_group()
    pilot_group.add_argument(
        "--supervised-pilot",
        action="store_true",
        help=(
            "v1.0 supervised-pilot preset: run adoption, GitHub, cloud, and "
            "readiness rollup checks for manual pilot mode"
        ),
    )
    pilot_group.add_argument(
        "--manual-pilot",
        action="store_true",
        help="alias for --supervised-pilot in manual merge-decision mode",
    )
    pilot_group.add_argument(
        "--promoted-pilot",
        action="store_true",
        help=(
            "run supervised-pilot readiness in promoted mode, where missing "
            "gate, auto-merge, or merge credential checks are blockers"
        ),
    )
    posture_group = parser.add_mutually_exclusive_group()
    posture_group.add_argument(
        "--adoption-posture",
        choices=("reviewer-gate", "hosted-builders", "orchestrator-only"),
        default="reviewer-gate",
        help=(
            "doctor posture for first-run adoption; reviewer-gate checks local "
            "Codex/Claude CLIs, while hosted-builders and orchestrator-only "
            "keep GitHub/cloud/setup checks visible but skip local CLI probes"
        ),
    )
    posture_group.add_argument(
        "--hosted-builders",
        action="store_const",
        const="hosted-builders",
        dest="adoption_posture",
        help=(
            "alias for --adoption-posture hosted-builders; use when this "
            "machine observes or dispatches hosted builder lanes instead of "
            "running local Codex/Claude CLIs"
        ),
    )
    posture_group.add_argument(
        "--orchestrator-only",
        action="store_const",
        const="orchestrator-only",
        dest="adoption_posture",
        help=(
            "alias for --adoption-posture orchestrator-only; use when this "
            "machine coordinates lanes but does not execute local reviewer CLIs"
        ),
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help=(
            "GitHub repository to inspect; adoption mode infers remote.origin "
            "when omitted"
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
    parser.add_argument(
        "--runner",
        action="store_true",
        help="check self-hosted macOS local audit runner readiness",
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
    parser.add_argument(
        "--actionlint-bin",
        default="actionlint",
        help="actionlint executable used by --runner generated-workflow checks",
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.supervised_pilot or args.manual_pilot or args.promoted_pilot:
        args.adoption = True
        args.preflight = True
    pilot_mode = "promoted" if args.promoted_pilot else "manual"
    if args.adoption:
        args.preflight = True
    _apply_first_run_defaults(args)
    if args.easy and args.profile is None:
        args.profile = "recommended"

    try:
        repo_slug = ""
        repo_source = ""
        if args.repo:
            repo_slug = normalize_repo_slug(args.repo, option="--repo")
            repo_source = "explicit"
        elif args.adoption:
            repo_slug = detect_repo_slug(Path.cwd())
            repo_source = "git_remote" if repo_slug else ""
        provider_templates_path = resolve_doctor_provider_templates_path(args.provider_templates)
        config_path = resolve_doctor_config_path(args.config, easy=args.easy)
        report = run_doctor(
            config_path=config_path,
            provider_templates_path=provider_templates_path,
            profile=args.profile,
            repo_slug=repo_slug,
            repo_source=repo_source,
            config_source=_doctor_config_source_label(
                config_arg=args.config,
                config_path=config_path,
                easy=args.easy,
            ),
            adoption=args.adoption,
            adoption_posture=args.adoption_posture,
            supervised_pilot=bool(
                args.supervised_pilot or args.manual_pilot or args.promoted_pilot
            ),
            pilot_mode=pilot_mode,
            probe_runtime=args.probe_runtime,
            github=args.github,
            cloud=args.cloud,
            runner=args.runner,
            http_timeout=args.http_timeout,
            actions_cost_sample=args.actions_cost_sample,
            actionlint_bin=args.actionlint_bin,
        )
    except (code_mower_config.ConfigError, ValueError) as exc:
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
