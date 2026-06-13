#!/usr/bin/env python3
"""Render a non-mutating Code Mower init plan for a setup profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools import code_mower_secrets
    from tools.code_mower_config import (
        ConfigError,
        RenderedPlan,
        _format_issues,
        _labels_for,
        load_config,
        required_secret_entries_for_lane,
        validate_config,
    )
else:  # pragma: no cover - exercised after package extraction.
    from . import secrets as code_mower_secrets
    from .config import (
        ConfigError,
        RenderedPlan,
        _format_issues,
        _labels_for,
        load_config,
        required_secret_entries_for_lane,
        validate_config,
    )


WORKFLOW_TARGETS_BY_DRIVER = {
    "api_model": (
        ".github/workflows/{lane_stem}.yml",
        ".github/workflows/{lane_stem}-labeler.yml",
    ),
    "hosted_bridge": (
        ".github/workflows/{lane_stem}-bridge.yml",
        ".github/workflows/{lane_stem}-labeler.yml",
    ),
    "local_cli": (".github/workflows/{lane_stem}-labeler.yml",),
    "manual": (),
    "saas_event": (".github/workflows/{lane_stem}-labeler.yml",),
}

DEFAULT_APPLY_OUTPUT_DIR = ".code-mower.generated"
APPLY_MANIFEST_FILENAME = "code-mower-init-plan.json"
APPLY_SUMMARY_FILES = ("labels.txt", "required-secrets.txt", "smoke-tests.sh")
REFERENCE_PYTHON = ".code-mower-venv/bin/python"
GEMINI_AUTH_FILE_ENV = "GEMINI_API_KEY_FILE"
GEMINI_AUTH_ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
STARTER_DATA_FILES = (
    (
        "calibration-corpus.json",
        "tools/calibration_corpus.example.json",
        "templates/calibration-corpus.example.json",
        "starter-calibration-corpus",
    ),
    (
        "context-packs.json",
        "tools/context_packs.example.json",
        "templates/context-packs.example.json",
        "starter-context-packs",
    ),
    (
        "reviewer-spend.json",
        "tools/reviewer_spend.example.json",
        "templates/reviewer-spend.example.json",
        "starter-reviewer-spend",
    ),
    (
        "reviewer-value-report.example.md",
        "tools/reviewer_value_report.example.md",
        "templates/reviewer-value-report.example.md",
        "starter-reviewer-value-report",
    ),
)

PRODUCT_SUPPORT_FILES = (
    (
        "tools/code_mower",
        "templates/product-support/code_mower",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/code_mower_standalone_shadow.sh",
        "templates/product-support/code_mower_standalone_shadow.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/code_mower_standalone_pin.env",
        "templates/product-support/code_mower_standalone_pin.env",
        "product-support-config",
        "0644",
    ),
    (
        "tools/run_codex_audit_pr.sh",
        "templates/product-support/run_codex_audit_pr.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/run_claude_audit_pr.sh",
        "templates/product-support/run_claude_audit_pr.sh",
        "product-support-wrapper",
        "0755",
    ),
    (
        "tools/safe_gh_comment.py",
        "templates/product-support/safe_gh_comment.py",
        "product-support-helper",
        "0755",
    ),
)


@dataclass(frozen=True)
class InitProfile:
    profile_id: str
    description: str
    lanes: tuple[str, ...]


def _profile(config: Mapping[str, Any], profile_id: str) -> InitProfile:
    profiles = config.get("profiles")
    if not isinstance(profiles, Mapping) or profile_id not in profiles:
        available = ", ".join(sorted(profiles)) if isinstance(profiles, Mapping) else "none"
        raise ConfigError(f"unknown profile {profile_id!r}; available profiles: {available}")
    profile = profiles[profile_id]
    if not isinstance(profile, Mapping):
        raise ConfigError(f"profile {profile_id!r} must be a mapping")
    lanes = profile.get("lanes", [])
    if not isinstance(lanes, list):
        raise ConfigError(f"profile {profile_id!r} lanes must be a list")
    return InitProfile(
        profile_id=profile_id,
        description=str(profile.get("description", "")),
        lanes=tuple(str(lane) for lane in lanes),
    )


def _workflow_targets(lane_id: str, lane: Mapping[str, Any]) -> tuple[str, ...]:
    templates = WORKFLOW_TARGETS_BY_DRIVER.get(str(lane.get("driver")), ())
    normalized = lane_id.replace("_", "-")
    lane_type = str(lane.get("type"))
    suffix = "-review" if lane_type == "review" else "-audit"
    lane_stem = normalized if normalized.endswith(suffix) else f"{normalized}{suffix}"
    return tuple(template.format(lane_stem=lane_stem) for template in templates)


def _trailer_lane_name(lane_id: str, lane: Mapping[str, Any]) -> str:
    return str(lane.get("trailer_lane") or lane.get("lane_config") or lane_id)


def _lane_module_name(lane_id: str) -> str:
    return lane_id.replace("-", "_")


def _running_as_package() -> bool:
    return bool(__package__ and __package__ != "tools")


def _default_package_command() -> str:
    command = sys.argv[0] or "code-mower"
    name = Path(command).name
    if name.endswith(".py"):
        return f"{shlex.quote(sys.executable)} -m code_mower.cli"
    python_suffix = name.removeprefix("python")
    is_python_launcher = name == "python" or (
        name.startswith("python")
        and bool(python_suffix)
        and python_suffix.replace(".", "").isdigit()
    )
    if is_python_launcher or name in {
        "",
        "-c",
        "__main__.py",
        "pytest",
        "py.test",
    }:
        command = "code-mower"
    return shlex.quote(command)


def _resolve_config_path(config_arg: str) -> Path:
    path = Path(config_arg)
    if path.is_file() or config_arg != "code-mower.example.yml":
        return path

    script_path = Path(__file__).resolve()
    candidates = []
    if _running_as_package():
        candidates.append(script_path.parent / "templates" / "code-mower.example.yml")
    else:
        candidates.append(script_path.parents[1] / "code-mower.example.yml")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return path


def _lane_smoke_tests(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    package_mode: bool,
) -> tuple[str, ...]:
    driver = lane.get("driver")
    if package_mode and driver in {"local_cli", "hosted_bridge", "api_model", "manual", "saas_event"}:
        return ()
    if driver in {"local_cli", "hosted_bridge", "api_model"}:
        trailer_lane = json.dumps(_trailer_lane_name(lane_id, lane))
        code = f"from tools.lane_configs import load_lane_config; load_lane_config({trailer_lane})"
        return (
            f"{REFERENCE_PYTHON} -c {shlex.quote(code)}",
        )
    if driver == "saas_event":
        adapter = json.dumps(str(lane.get("adapter")))
        code = f"from tools.adapters import load_adapter; load_adapter({adapter})"
        return (
            f"{REFERENCE_PYTHON} -c {shlex.quote(code)}",
        )
    if driver == "manual":
        return ("bash -n tools/post_review.sh",)
    return ()


def _lane_warnings(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    package_mode: bool,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if lane.get("spend_policy") == "paid":
        warnings.append(f"{lane_id}: paid lane; init dry-run will not trigger a review")
    if lane.get("enabled_by_default") is False:
        warnings.append(f"{lane_id}: opt-in lane selected by profile")
    if lane.get("trigger_policy") == "manual":
        warnings.append(f"{lane_id}: manual trigger policy; installer must not auto-dispatch")
    if package_mode:
        driver = lane.get("driver")
        if driver in {"local_cli", "hosted_bridge", "api_model"}:
            warnings.append(
                f"{lane_id}: lane-config smoke deferred until package-relative lane configs are extracted"
            )
        elif driver == "saas_event":
            warnings.append(
                f"{lane_id}: adapter smoke deferred until package-relative adapters are extracted"
            )
        elif driver == "manual":
            warnings.append(
                f"{lane_id}: manual review script smoke deferred until repo-local review scripts are installed"
            )
    return tuple(warnings)


def _repo_root() -> Path:
    if __package__ and __package__ != "tools":
        return Path.cwd()
    return Path(__file__).resolve().parents[1]


def default_auth_config_dir(home_dir: Path | None = None) -> Path:
    return (home_dir or Path.home()) / ".config" / "code-mower"


def default_gemini_auth_path(home_dir: Path | None = None) -> Path:
    return default_auth_config_dir(home_dir) / "gemini.env"


def _shell_export_line(name: str, value: str) -> str:
    return f"export {name}={shlex.quote(value)}"


def _parse_gemini_secret_source(text: str) -> str:
    return code_mower_secrets.parse_secret_file_text(
        text,
        supported_env_names=set(GEMINI_AUTH_ENV_NAMES),
    ).value


def write_gemini_auth_file(
    secret_value: str,
    path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    value = _parse_gemini_secret_source(secret_value)
    if not value:
        raise ConfigError("Gemini key source was empty or not a supported Gemini key assignment")
    destination = (path or default_gemini_auth_path()).expanduser()
    if destination.is_symlink():
        raise ConfigError(f"{destination} is a symlink; refusing to write secrets through it")
    if destination.exists() and not destination.is_file():
        raise ConfigError(f"{destination} is not a regular file; refusing to write secrets")
    if destination.exists() and not force:
        raise ConfigError(f"{destination} already exists; pass --force to overwrite it")
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path is None or not parent_existed:
        destination.parent.chmod(0o700)
    if destination.exists():
        destination.chmod(0o600)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise ConfigError(f"{destination} already exists; pass --force to overwrite it") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value + "\n")
    finally:
        if fd != -1:
            os.close(fd)
    destination.chmod(0o600)
    return {
        "mode": "auth",
        "provider": "gemini",
        "path": str(destination),
        "file_env": GEMINI_AUTH_FILE_ENV,
        "shell_export": _shell_export_line(GEMINI_AUTH_FILE_ENV, str(destination)),
    }


def _render_gemini_auth_instructions(path: Path | None = None) -> str:
    destination = (path or default_gemini_auth_path()).expanduser()
    return "\n".join(
        [
            "Code Mower Gemini auth setup",
            f"Credential file: {destination}",
            "",
            "Write the key without putting it in shell history:",
            f"  printf '%s\\n' \"$GEMINI_API_KEY\" | code-mower init auth gemini --from-stdin --path {shlex.quote(str(destination))}",
            "",
            "Then make the file discoverable:",
            f"  {_shell_export_line(GEMINI_AUTH_FILE_ENV, str(destination))}",
            "",
        ]
    )


def _auth_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="code-mower init auth")
    subparsers = parser.add_subparsers(dest="provider", required=True)
    gemini = subparsers.add_parser("gemini")
    gemini.add_argument(
        "--from-stdin",
        action="store_true",
        help="read the Gemini key or GEMINI_API_KEY assignment from stdin",
    )
    gemini.add_argument(
        "--from-env",
        nargs="?",
        const="GEMINI_API_KEY",
        help="read the key from an environment variable, defaulting to GEMINI_API_KEY",
    )
    gemini.add_argument(
        "--path",
        type=Path,
        default=None,
        help="credential file path, defaulting to ~/.config/code-mower/gemini.env",
    )
    gemini.add_argument("--force", action="store_true", help="overwrite an existing file")
    gemini.add_argument("--print-shell", action="store_true", help="print shell export setup")
    gemini.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.provider != "gemini":  # pragma: no cover - argparse guards this.
        raise AssertionError(f"unhandled auth provider: {args.provider}")
    if args.from_stdin and args.from_env:
        print("error: choose either --from-stdin or --from-env", file=sys.stderr)
        return 1
    if not args.from_stdin and not args.from_env:
        text = _render_gemini_auth_instructions(args.path)
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": "auth",
                        "provider": "gemini",
                        "path": str((args.path or default_gemini_auth_path()).expanduser()),
                        "file_env": GEMINI_AUTH_FILE_ENV,
                        "created": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(text, end="")
        return 0

    if args.from_stdin:
        source_text = sys.stdin.read()
    else:
        env_name = str(args.from_env)
        if env_name not in GEMINI_AUTH_ENV_NAMES:
            print(
                "error: --from-env must be GEMINI_API_KEY or GOOGLE_API_KEY",
                file=sys.stderr,
            )
            return 1
        source_text = os.environ.get(env_name, "")
        if not source_text:
            print(f"error: {env_name} is not set", file=sys.stderr)
            return 1

    try:
        result = write_gemini_auth_file(source_text, args.path, force=args.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({**result, "created": True}, indent=2, sort_keys=True))
    else:
        print(f"Code Mower Gemini auth file written: {result['path']}")
        if args.print_shell:
            print(result["shell_export"])
        else:
            print(f"Set {GEMINI_AUTH_FILE_ENV} for future shells:")
            print(f"  {result['shell_export']}")
    return 0


def _safe_output_path(output_dir: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ConfigError(f"unsafe generated path: {relative_path}")
    destination = output_dir.joinpath(path)
    try:
        destination.resolve().relative_to(output_dir.resolve())
    except ValueError as exc:
        raise ConfigError(f"generated path escapes output directory: {relative_path}") from exc
    return destination


def _placeholder_text(path: str, source: str) -> str:
    return (
        f"# Code Mower generated placeholder for {path}\n"
        f"# Source template: {source}\n"
        "# This reference apply mode writes placeholders only when the source\n"
        "# file is not present in the checkout. The standalone package will\n"
        "# render this file from bundled templates.\n"
    )


def _copy_source_candidates(source_root: Path, entry: Mapping[str, Any], path: str) -> tuple[Path, ...]:
    candidates: list[Path] = []
    package_copy_from = entry.get("package_copy_from")
    if entry.get("package_copy_first") and package_copy_from:
        candidates.append(Path(__file__).resolve().parent / str(package_copy_from))
    copy_from = str(entry.get("copy_from", path))
    candidates.append(source_root / copy_from)
    if package_copy_from and not entry.get("package_copy_first"):
        candidates.append(Path(__file__).resolve().parent / str(package_copy_from))
    return tuple(candidates)


def _previous_apply_paths(output_dir: Path) -> list[Path]:
    manifest_path = output_dir / APPLY_MANIFEST_FILENAME
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    paths = [manifest_path, *(output_dir / filename for filename in APPLY_SUMMARY_FILES)]
    generated_files = manifest.get("generated_files", [])
    if isinstance(generated_files, list):
        for entry in generated_files:
            if not isinstance(entry, dict):
                continue
            try:
                paths.append(_safe_output_path(output_dir, str(entry.get("path", ""))))
            except ConfigError:
                continue
    return paths


def _prune_previous_apply(output_dir: Path) -> None:
    previous_paths = _previous_apply_paths(output_dir)
    for path in sorted(previous_paths, key=lambda item: len(item.parts), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()

    generated_parents: set[Path] = set()
    for path in previous_paths:
        parent = path.parent
        while parent != output_dir and output_dir in parent.parents:
            generated_parents.add(parent)
            parent = parent.parent

    for directory in sorted(generated_parents, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_init_plan(
    plan: RenderedPlan,
    output_dir: Path,
    *,
    source_root: Path | None = None,
) -> dict[str, Any]:
    source_root = (source_root or _repo_root()).resolve()
    resolved_output_dir = output_dir.resolve()
    if resolved_output_dir == source_root:
        raise ConfigError(
            "refusing to write generated output into the source root; "
            "choose a dedicated --output-dir such as .code-mower.generated"
        )

    generated_destinations: list[tuple[dict[str, Any], str, Path]] = []
    for entry in plan.data["generated_files"]:
        path = str(entry["path"])
        generated_destinations.append((entry, path, _safe_output_path(output_dir, path)))

    output_dir.mkdir(parents=True, exist_ok=True)
    _prune_previous_apply(output_dir)
    written_files: list[str] = []
    placeholder_files: list[str] = []

    manifest_path = output_dir / APPLY_MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(plan.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written_files.append(str(manifest_path))

    summary_files = {
        "labels.txt": "\n".join(plan.data["labels"]) + "\n",
        "required-secrets.txt": "\n".join(plan.data["required_secrets"]) + "\n",
        "smoke-tests.sh": "#!/usr/bin/env bash\nset -euo pipefail\n\n"
        + "\n".join(plan.data["smoke_tests"])
        + "\n",
    }
    for filename, text in summary_files.items():
        destination = output_dir / filename
        destination.write_text(text, encoding="utf-8")
        if filename.endswith(".sh"):
            destination.chmod(0o755)
        written_files.append(str(destination))

    for entry, path, destination in generated_destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = next(
            (candidate for candidate in _copy_source_candidates(source_root, entry, path) if candidate.is_file()),
            None,
        )
        if source is not None:
            shutil.copyfile(source, destination)
        else:
            destination.write_text(_placeholder_text(path, str(entry["source"])), encoding="utf-8")
            placeholder_files.append(str(destination))
        if entry.get("mode") == "0755":
            destination.chmod(0o755)
        written_files.append(str(destination))

    return {
        "mode": "apply",
        "output_dir": str(output_dir),
        "written_files": written_files,
        "placeholder_files": placeholder_files,
    }


def render_init_plan(
    config: Mapping[str, Any],
    profile_id: str = "recommended",
    config_path: str = "code-mower.example.yml",
    *,
    package_mode: bool | None = None,
    package_command: str | None = None,
) -> RenderedPlan:
    issues = validate_config(config)
    if issues:
        raise ConfigError(f"invalid Code Mower config:\n{_format_issues(issues)}")

    profile = _profile(config, profile_id)
    lanes: Mapping[str, Mapping[str, Any]] = config["lanes"]
    selected_lanes = {lane_id: lanes[lane_id] for lane_id in profile.lanes}

    labels: list[str] = []
    workflows: list[dict[str, str]] = []
    generated_files: list[dict[str, str]] = []
    workflow_targets: set[str] = set()
    generated_paths: set[str] = set()
    required_secrets: set[str] = set()
    quoted_config_path = shlex.quote(config_path)
    quoted_profile_id = shlex.quote(profile.profile_id)
    if package_mode is None:
        package_mode = _running_as_package()
    smoke_command_prefix = (
        shlex.quote(package_command)
        if package_mode and package_command
        else _default_package_command()
        if package_mode
        else f"{REFERENCE_PYTHON} tools/code_mower"
    )
    smoke_tests: list[str] = [
        (
            f"{smoke_command_prefix} config validate {quoted_config_path}"
            if package_mode
            else f"{smoke_command_prefix}_config.py {quoted_config_path} --validate-only"
        ),
        (
            f"{smoke_command_prefix} init {quoted_config_path} --profile {quoted_profile_id} --dry-run --json"
            if package_mode
            else f"{smoke_command_prefix}_init.py {quoted_config_path} --profile {quoted_profile_id} --dry-run --json"
        ),
    ]
    warnings: list[str] = []
    merge_authority_lanes: list[str] = []
    informational_lanes: list[str] = []

    for lane_id, lane in selected_lanes.items():
        lane_labels = _labels_for(lane)
        labels.extend(str(lane_labels[key]) for key in ("needs", "done", "blocked"))
        if lane.get("merge_authority"):
            merge_authority_lanes.append(lane_id)
        if lane.get("informational"):
            informational_lanes.append(lane_id)
        required_secrets.update(required_secret_entries_for_lane(lane))
        for target in _workflow_targets(lane_id, lane):
            if target in workflow_targets:
                warnings.append(f"{lane_id}: workflow target {target} collides with another lane")
                continue
            workflow_targets.add(target)
            workflows.append(
                {
                    "lane": lane_id,
                    "driver": str(lane["driver"]),
                    "target": target,
                }
            )
            if target not in generated_paths:
                generated_paths.add(target)
                generated_files.append({"path": target, "source": "workflow-template"})
        if lane.get("driver") in {"local_cli", "hosted_bridge", "api_model"}:
            trailer_lane = _trailer_lane_name(lane_id, lane)
            trailer_module = _lane_module_name(trailer_lane)
            path = f"tools/lane_configs/{trailer_module}.py"
            if path in generated_paths:
                warnings.append(f"{lane_id}: generated file {path} collides with another lane")
            else:
                generated_paths.add(path)
                generated_files.append(
                    {
                        "path": path,
                        "source": "lane-config-template",
                    }
                )
        smoke_tests.extend(_lane_smoke_tests(lane_id, lane, package_mode=package_mode))
        warnings.extend(_lane_warnings(lane_id, lane, package_mode=package_mode))

    cleanup_path = ".github/workflows/audit-label-cleanup.yml"
    if cleanup_path not in generated_paths:
        required_secrets.add("AUDIT_LABEL_CLEANUP_TOKEN")
        generated_paths.add(cleanup_path)
        generated_files.append(
            {
                "path": cleanup_path,
                "source": "shared-cleanup-template",
            }
        )
    for lane_id, lane in selected_lanes.items():
        hygiene = lane.get("review_hygiene")
        if not isinstance(hygiene, Mapping):
            continue
        stale_path = str(hygiene["workflow"])
        if stale_path not in generated_paths:
            generated_paths.add(stale_path)
            generated_files.append(
                {
                    "path": stale_path,
                    "source": "shared-stale-label-template",
                }
            )
    for target, copy_from, package_copy_from, source_name in STARTER_DATA_FILES:
        if target in generated_paths:
            warnings.append(f"starter data target {target} collides with another generated file")
            continue
        generated_paths.add(target)
        generated_files.append(
            {
                "path": target,
                "source": source_name,
                "copy_from": copy_from,
                "package_copy_from": package_copy_from,
            }
        )
    for target, package_copy_from, source_name, mode in PRODUCT_SUPPORT_FILES:
        if target in generated_paths:
            warnings.append(f"product support target {target} collides with another generated file")
            continue
        generated_paths.add(target)
        generated_files.append(
            {
                "path": target,
                "source": source_name,
                "package_copy_from": package_copy_from,
                "package_copy_first": True,
                "mode": mode,
            }
        )

    if not merge_authority_lanes:
        warnings.append(
            f"{profile.profile_id}: profile has no merge-authority lanes; keep informational only"
        )

    data = {
        "mode": "dry-run",
        "profile": {
            "id": profile.profile_id,
            "description": profile.description,
            "lanes": list(profile.lanes),
        },
        "labels": sorted(set(labels)),
        "workflows": workflows,
        "generated_files": generated_files,
        "required_secrets": sorted(required_secrets),
        "merge_authority_lanes": merge_authority_lanes,
        "informational_lanes": informational_lanes,
        "smoke_tests": smoke_tests,
        "warnings": warnings,
    }

    lines = [
        "Code Mower init dry-run",
        f"Profile: {profile.profile_id}",
        f"Description: {profile.description}",
        "",
        "Selected lanes:",
    ]
    for lane_id, lane in selected_lanes.items():
        if lane.get("merge_authority"):
            role = "merge-authority"
        elif lane.get("informational"):
            role = "informational"
        else:
            role = "standard"
        lines.append(f"- {lane_id}: {lane['driver']} / {lane['provider']} ({role})")

    lines.extend(["", "Labels to ensure:"])
    lines.extend(f"- {label}" for label in data["labels"])

    lines.extend(["", "Workflow files to render:"])
    if workflows:
        lines.extend(f"- {workflow['target']} ({workflow['lane']})" for workflow in workflows)
    else:
        lines.append("- none")

    lines.extend(["", "Generated file manifest:"])
    lines.extend(
        f"- {entry['path']} [{entry['source']}]" for entry in data["generated_files"]
    )

    lines.extend(["", "Required secrets/PAT fallbacks:"])
    if data["required_secrets"]:
        lines.extend(f"- {secret}" for secret in data["required_secrets"])
    else:
        lines.append("- none beyond GITHUB_TOKEN")

    lines.extend(["", "Smoke tests after render:"])
    lines.extend(f"- {test}" for test in smoke_tests)

    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)

    return RenderedPlan(text="\n".join(lines) + "\n", data=data)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["auth"]:
        return _auth_main(argv[1:])

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="code-mower.example.yml")
    parser.add_argument("--profile", default="recommended")
    parser.add_argument(
        "--easy",
        action="store_true",
        help=(
            "safe first-run alias for --profile recommended --dry-run; combine "
            "with --apply to write generated output instead"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="render the init plan")
    parser.add_argument("--apply", action="store_true", help="write generated files to --output-dir")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_APPLY_OUTPUT_DIR,
        help="safe output directory for --apply mode",
    )
    parser.add_argument("--json", action="store_true", help="emit dry-run plan as JSON")
    args = parser.parse_args(argv)

    if args.easy:
        args.profile = "recommended"
        if not args.dry_run and not args.apply:
            args.dry_run = True
    if args.apply and args.dry_run:
        print("error: choose either --dry-run or --apply", file=sys.stderr)
        return 1
    if not args.dry_run and not args.apply:
        print("error: choose --dry-run or --apply", file=sys.stderr)
        return 1

    try:
        config_source = _resolve_config_path(args.config)
        rendered_config_path = (
            str(config_source) if config_source != Path(args.config) else args.config
        )
        plan = render_init_plan(
            load_config(config_source),
            profile_id=args.profile,
            config_path=rendered_config_path,
        )
        apply_result = (
            apply_init_plan(plan, Path(args.output_dir))
            if args.apply
            else None
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(apply_result or plan.data, indent=2, sort_keys=True))
    elif apply_result:
        print(f"Code Mower init apply wrote {len(apply_result['written_files'])} files")
        print(f"Output: {apply_result['output_dir']}")
        if apply_result["placeholder_files"]:
            print("Placeholders:")
            for path in apply_result["placeholder_files"]:
                print(f"- {path}")
    else:
        print(plan.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
