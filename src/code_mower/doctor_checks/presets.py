"""First-run doctor presets and package-aware path resolution."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from code_mower import package as code_mower_package


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
        script_path.parent.parent / "templates" / "code-mower.example.yml",
        script_path.parents[1] / "code-mower.example.yml",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return path


def resolve_doctor_config_path(
    config_arg: str,
    *,
    easy: bool = False,
    script_path: Path | None = None,
) -> Path:
    return resolve_doctor_config_path_for_script(
        config_arg,
        easy=easy,
        script_path=script_path or Path(__file__),
    )


def resolve_doctor_provider_templates_path(path_text: str) -> Path:
    path = Path(path_text)
    if path_text == code_mower_package.DEFAULT_PROVIDER_TEMPLATES and not path.is_absolute():
        project_catalog = Path.cwd() / code_mower_package.DEFAULT_PROVIDER_TEMPLATES
        if project_catalog.exists():
            return project_catalog
    return code_mower_package.resolve_provider_templates_path(path_text)


def apply_first_run_defaults(args: Namespace) -> None:
    if not (getattr(args, "v05", False) or getattr(args, "preflight", False)):
        return
    args.easy = True
    if args.profile is None:
        args.profile = "recommended"
    args.probe_runtime = True
    args.github = True
    args.cloud = True
