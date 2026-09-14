#!/usr/bin/env python3
"""Render the future standalone Code Mower package/install dry-run."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


def _render_provider_catalog_json_fallback(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools.code_mower_config import (
        ConfigError,
        RenderedPlan,
        SAFE_IDENTIFIER_RE,
        _format_issues,
        load_config,
        validate_config,
    )
    from tools.code_mower_package_paths import (
        DEFAULT_PROVIDER_TEMPLATES,
        _as_mapping,
        load_provider_templates,
        resolve_provider_templates_path,
    )
else:  # pragma: no cover - exercised after package extraction.
    from .config import (
        ConfigError,
        RenderedPlan,
        SAFE_IDENTIFIER_RE,
        _format_issues,
        load_config,
        validate_config,
    )
    from .package_paths import (
        DEFAULT_PROVIDER_TEMPLATES,
        _as_mapping,
        load_provider_templates,
        resolve_provider_templates_path,
    )

if __package__ in {None, "", "tools"}:  # pragma: no cover - legacy product-local layout.
    try:
        from tools.code_mower_package_content import (
            cli_commands,
            _config_template_text,
            _init_py_text,
            _lane_template_text,
            _pyproject_text,
            _workflow_template_text,
        )
    except ModuleNotFoundError:
        from code_mower.package_content import (
            cli_commands,
            _config_template_text,
            _init_py_text,
            _lane_template_text,
            _pyproject_text,
            _workflow_template_text,
        )
else:  # pragma: no cover - exercised after package extraction.
    from .package_content import (
        cli_commands,
        _config_template_text,
        _init_py_text,
        _lane_template_text,
        _pyproject_text,
        _workflow_template_text,
    )

if __package__ in {None, ""}:
    from code_mower.package_manifest import (
        DEFAULT_PACKAGE_CONFIG,
        DEFERRED_PACKAGE_FILES,
        PACKAGE_FILES,
        TEMPLATE_FILES,
    )
if __package__ in {None, "", "tools"}:  # pragma: no cover - legacy product-local layout.
    try:
        from tools.code_mower_package_manifest import (
            DEFAULT_PACKAGE_CONFIG,
            DEFERRED_PACKAGE_FILES,
            PACKAGE_FILES,
            TEMPLATE_FILES,
        )
    except ModuleNotFoundError:
        from code_mower.package_manifest import (
            DEFAULT_PACKAGE_CONFIG,
            DEFERRED_PACKAGE_FILES,
            PACKAGE_FILES,
            TEMPLATE_FILES,
        )
else:  # pragma: no cover - exercised after package extraction.
    from .package_manifest import (
        DEFAULT_PACKAGE_CONFIG,
        DEFERRED_PACKAGE_FILES,
        PACKAGE_FILES,
        TEMPLATE_FILES,
    )

if __package__ in {None, "", "tools"}:  # pragma: no cover - legacy product-local layout.
    try:
        from tools.code_mower_package_static import STATIC_PACKAGE_FILES
    except ModuleNotFoundError:
        from code_mower.package_static import STATIC_PACKAGE_FILES
else:  # pragma: no cover - exercised after package extraction.
    from .package_static import STATIC_PACKAGE_FILES

if __package__ in {None, "", "tools"}:  # pragma: no cover - legacy product-local layout.
    try:
        from tools.code_mower_package_rendering import _render_provider_catalog
    except ModuleNotFoundError:
        try:
            from code_mower.package_rendering import _render_provider_catalog
        except ModuleNotFoundError:
            _render_provider_catalog = _render_provider_catalog_json_fallback
else:  # pragma: no cover - exercised after package extraction.
    from .package_rendering import _render_provider_catalog


def _running_code_mower_version(repo_root: Path | None = None) -> str:
    roots = [repo_root] if repo_root is not None else []
    roots.extend(_candidate_package_source_roots())
    version_re = re.compile(r'__version__\s*=\s*"([^"]+)"')
    for root in roots:
        if root is None:
            continue
        init_path = root / "src/code_mower/__init__.py"
        if not init_path.is_file():
            continue
        match = version_re.search(init_path.read_text(encoding="utf-8"))
        if match:
            return match.group(1)

    loaded_package = sys.modules.get("code_mower")
    loaded_version = getattr(loaded_package, "__version__", "")
    if isinstance(loaded_version, str) and loaded_version:
        return loaded_version

    return "0.0.0"


PACKAGED_STARTER_CONFIG_NAME = "code-mower.example.yml"


def packaged_starter_config_path() -> Path:
    """Return the maintained packaged starter configuration.

    The starter is a package resource, so it is located relative to the installed
    module and never relative to the working directory: a cwd-local file named
    like the starter must not be able to redirect an explicit packaged-starter
    selection into a different configuration. A source checkout keeps its
    repository copy next to the package, which is the same maintained file.
    """
    module_dir = Path(__file__).resolve().parent
    candidates = (
        module_dir / "templates" / PACKAGED_STARTER_CONFIG_NAME,
        module_dir.parents[1] / PACKAGED_STARTER_CONFIG_NAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "packaged starter configuration is missing from this installation; "
        "reinstall code-mower or pass an explicit config path"
    )


def resolve_package_config_path(path_text: str, *, explicit: bool = False) -> Path:
    path = Path(path_text)
    if explicit or path_text != DEFAULT_PACKAGE_CONFIG or path.is_absolute():
        return path

    for root in _candidate_package_source_roots():
        source_path = _resolve_package_source(
            root,
            DEFAULT_PACKAGE_CONFIG,
            "src/code_mower/templates/code-mower.example.yml",
        )
        if source_path is not None:
            return source_path

    return Path.cwd() / DEFAULT_PACKAGE_CONFIG


def _provider_template_rows(
    config: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lanes = _as_mapping(config.get("lanes"), "lanes")
    templates = _as_mapping(provider_templates.get("provider_templates"), "provider_templates")
    rows: list[dict[str, Any]] = []
    missing = sorted(set(lanes) - set(templates))

    for lane_id in sorted(templates):
        if not SAFE_IDENTIFIER_RE.fullmatch(str(lane_id)):
            raise ConfigError(
                "provider template lane ids must match "
                f"[A-Za-z0-9][A-Za-z0-9_-]*: {lane_id}"
            )
        template = templates.get(lane_id)
        if not isinstance(template, Mapping):
            raise ConfigError(f"provider template for {lane_id} must be a mapping")
        lane = lanes.get(lane_id, {})
        if not isinstance(lane, Mapping):
            lane = {}
        token_env = lane.get("token_env", template.get("token_env", []))
        token_env_any = lane.get("token_env_any", template.get("token_env_any", []))
        review_hygiene = lane.get("review_hygiene", template.get("review_hygiene", {}))
        if not token_env and isinstance(review_hygiene, Mapping):
            review_token = review_hygiene.get("token_env")
            token_env = [review_token] if review_token else []
        rows.append(
            {
                "lane": str(lane_id),
                "provider": str(lane.get("provider", template.get("provider", ""))),
                "driver": str(lane.get("driver", template.get("driver", ""))),
                "type": str(lane.get("type", template.get("type", ""))),
                "adapter": lane.get("adapter", template.get("adapter")),
                "trailer_lane": lane.get("trailer_lane", template.get("trailer_lane")),
                "spend_policy": str(lane.get("spend_policy", template.get("spend_policy", "none"))),
                "merge_authority": bool(lane.get("merge_authority", template.get("merge_authority", False))),
                "informational": bool(lane.get("informational", template.get("informational", False))),
                "enabled_by_default": lane.get(
                    "enabled_by_default",
                    template.get("enabled_by_default", True),
                ),
                "events": list(lane.get("events", template.get("events", []))),
                "token_env": list(token_env),
                "token_env_any": list(token_env_any),
                "trigger_policy": lane.get(
                    "trigger_policy",
                    template.get("trigger_policy", "label"),
                ),
                "provider_config": lane.get(
                    "provider_config",
                    template.get("provider_config", {}),
                ),
                "review_hygiene": review_hygiene if isinstance(review_hygiene, Mapping) else {},
                "template_path": f"templates/providers/{lane_id}.yml",
            }
        )

    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ConfigError(f"provider templates missing configured lanes: {missing_text}")
    return rows


def render_package_plan(
    config: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
    package_name: str = "code-mower",
) -> RenderedPlan:
    if not SAFE_IDENTIFIER_RE.fullmatch(package_name):
        raise ConfigError("package name must match [A-Za-z0-9][A-Za-z0-9_-]*")

    issues = validate_config(config)
    if issues:
        raise ConfigError(f"invalid Code Mower config:\n{_format_issues(issues)}")

    template_rows = _provider_template_rows(config, provider_templates)
    catalog_profiles = _as_mapping(provider_templates.get("profiles"), "profiles")
    repo_profiles = _as_mapping(config.get("profiles", {}), "profiles")
    profiles: dict[str, dict[str, Any]] = {
        profile_id: {
            "description": profile.get("description", ""),
            "lanes": profile.get("lanes", []),
            "source": "catalog",
        }
        for profile_id, profile in catalog_profiles.items()
    }
    for profile_id, profile in repo_profiles.items():
        profiles[profile_id] = {
            "description": profile.get(
                "description",
                profiles.get(profile_id, {}).get("description", "Repo-local profile."),
            ),
            "lanes": profile.get("lanes", []),
            "source": "repo",
        }

    package_files = [
        {"source": source, "target": target, "kind": kind}
        for source, target, kind in PACKAGE_FILES
    ]
    deferred_package_files = [
        {"source": source, "target": target, "reason": reason}
        for source, target, reason in DEFERRED_PACKAGE_FILES
    ]
    template_files = [
        {"kind": kind, "target": target}
        for kind, target in TEMPLATE_FILES
    ]
    template_files.extend(
        {"kind": "provider-template", "target": row["template_path"]}
        for row in template_rows
    )
    package_version = _running_code_mower_version()
    command_inventory = cli_commands(package_version)
    data = {
        "mode": "dry-run",
        "package": {
            "name": package_name,
            "module": "code_mower",
            "console_script": f"{package_name}=code_mower.cli:main",
            "source_layout": "src/code_mower",
            "version": package_version,
        },
        "package_files": package_files,
        "deferred_package_files": deferred_package_files,
        "template_files": template_files,
        "provider_templates": template_rows,
        "profiles": profiles,
        "cli_commands": list(command_inventory),
        "install_docs": [
            "README.md",
            "docs/getting-started.md",
            "docs/package-skeleton.md",
            "docs/package-customization.md",
            "docs/repo-strategy.md",
            "docs/mirror-removal-runbook.md",
            "docs/commercial-boundary.md",
            "docs/public-release-checklist.md",
            "docs/github-setup.md",
            "docs/troubleshooting.md",
            "docs/provider-matrix.md",
            "docs/providers.md",
            "docs/security.md",
            "docs/workflow-templates.md",
        ],
    }

    lines = [
        "Code Mower OSS package dry-run",
        f"Package: {data['package']['name']}",
        f"Module: {data['package']['module']}",
        f"Console script: {data['package']['console_script']}",
        "",
        "Package files to extract:",
    ]
    lines.extend(
        f"- {entry['source']} -> {entry['target']} [{entry['kind']}]"
        for entry in package_files
    )
    lines.extend(["", "Deferred package files:"])
    lines.extend(
        f"- {entry['source']} -> {entry['target']} ({entry['reason']})"
        for entry in deferred_package_files
    )

    lines.extend(["", "Template files to ship:"])
    lines.extend(f"- {entry['target']} [{entry['kind']}]" for entry in template_files)

    lines.extend(["", "Provider templates:"])
    lines.extend(
        f"- {entry['lane']}: {entry['driver']} / {entry['provider']} "
        f"({entry['spend_policy']}) -> {entry['template_path']}"
        for entry in template_rows
    )

    lines.extend(["", "CLI commands to document:"])
    lines.extend(f"- {command}" for command in command_inventory)

    return RenderedPlan(text="\n".join(lines) + "\n", data=data)


def _write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise ConfigError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_file(source: Path, target: Path, *, force: bool) -> None:
    if not source.is_file():
        raise ConfigError(f"package source file does not exist: {source}")
    if target.exists() and not force:
        raise ConfigError(f"refusing to overwrite existing file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _resolve_package_source(repo_root: Path, source: str, target: str) -> Path | None:
    source_path = repo_root / source
    if source_path.is_file():
        return source_path
    target_path = repo_root / target
    if target_path.is_file():
        return target_path
    return None


def _missing_package_sources(repo_root: Path) -> list[str]:
    return sorted(
        source
        for source, target, _ in PACKAGE_FILES
        if _resolve_package_source(repo_root, source, target) is None
    )


def _candidate_package_source_roots() -> list[Path]:
    module_path = Path(__file__).resolve()
    # Prefer the checkout that loaded this module. Fall back to cwd for
    # installed-package runs that intentionally materialize a local checkout.
    candidates = [
        module_path.parents[2],
        module_path.parents[1],
        Path.cwd().resolve(),
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _default_package_source_root() -> Path:
    candidates = _candidate_package_source_roots()
    for candidate in candidates:
        if not _missing_package_sources(candidate):
            return candidate
    return candidates[0]


def _planned_materialized_targets(plan: RenderedPlan) -> list[str]:
    targets = [target for _, target, _ in PACKAGE_FILES]
    targets.extend(target for target, _ in STATIC_PACKAGE_FILES)
    targets.extend(entry["target"] for entry in plan.data["template_files"])
    targets.extend(
        [
            "pyproject.toml",
            "src/code_mower/templates/providers.yml",
            "code-mower-package-manifest.json",
        ]
    )
    targets.extend(
        f"templates/providers/{row['lane']}.yml"
        for row in plan.data["provider_templates"]
    )
    return sorted(set(targets))


def _preflight_output_collisions(plan: RenderedPlan, output_dir: Path) -> None:
    collisions = [
        target
        for target in _planned_materialized_targets(plan)
        if (output_dir / target).exists()
    ]
    parent_collisions: list[str] = []
    for target in _planned_materialized_targets(plan):
        relative_parent = Path(target).parent
        while str(relative_parent) not in {"", "."}:
            parent_path = output_dir / relative_parent
            if parent_path.exists() and not parent_path.is_dir():
                parent_collisions.append(str(relative_parent))
                break
            relative_parent = relative_parent.parent
    collisions.extend(sorted(set(parent_collisions)))
    if not collisions:
        return
    sample = ", ".join(collisions[:3])
    if len(collisions) > 3:
        sample += f", ... ({len(collisions)} total)"
    raise ConfigError(f"refusing to overwrite existing file(s): {sample}")


def materialize_package_plan(
    plan: RenderedPlan,
    *,
    output_dir: Path,
    repo_root: Path | None = None,
    force: bool = False,
) -> RenderedPlan:
    """Write the planned standalone package tree to ``output_dir``."""

    repo_root = repo_root or Path.cwd()
    missing_sources = _missing_package_sources(repo_root)
    if missing_sources:
        sample = ", ".join(missing_sources[:3])
        if len(missing_sources) > 3:
            sample += f", ... ({len(missing_sources)} total)"
        raise ConfigError(
            "package materialization requires the reference repository checkout; "
            f"missing source file(s): {sample}"
        )
    if output_dir.exists() and not output_dir.is_dir():
        raise ConfigError(f"output path is not a directory: {output_dir}")
    if not force:
        _preflight_output_collisions(plan, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, str]] = []
    package_version = _running_code_mower_version(repo_root)

    for source, target, kind in PACKAGE_FILES:
        source_path = _resolve_package_source(repo_root, source, target)
        if source_path is None:  # pragma: no cover - guarded by preflight above.
            raise ConfigError(f"package source file does not exist: {repo_root / source}")
        target_path = output_dir / target
        _copy_file(source_path, target_path, force=force)
        written.append(
            {
                "target": target,
                "source": source_path.relative_to(repo_root).as_posix(),
                "kind": kind,
            }
        )

    for target, content in STATIC_PACKAGE_FILES:
        if target == "src/code_mower/__init__.py":
            content = _init_py_text(package_version)
        elif target == "scripts/smoke_easy_mode.py":
            content = content.replace(
                "__CODE_MOWER_CONSOLE_SCRIPT__",
                str(plan.data["package"]["name"]),
            )
        _write_text(output_dir / target, content, force=force)
        written.append({"target": target, "source": "generated", "kind": "package"})
    _write_text(
        output_dir / "pyproject.toml",
        _pyproject_text(str(plan.data["package"]["name"]), version=package_version),
        force=force,
    )
    written.append(
        {"target": "pyproject.toml", "source": "generated", "kind": "package"}
    )

    requirements_dir = output_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)

    provider_templates = {
        row["lane"]: {
            key: value
            for key, value in row.items()
            if key not in {"lane", "template_path"}
        }
        for row in plan.data["provider_templates"]
    }
    provider_catalog = {
        "version": 1,
        "provider_templates": provider_templates,
        "profiles": plan.data["profiles"],
    }
    catalog_target = "src/code_mower/templates/providers.yml"
    _write_text(
        output_dir / catalog_target,
        _render_provider_catalog(provider_catalog),
        force=force,
    )
    written.append(
        {"target": catalog_target, "source": "generated", "kind": "provider-catalog"}
    )

    for entry in plan.data["template_files"]:
        target = entry["target"]
        kind = entry["kind"]
        if target in {"pyproject.toml", "README.md", "MANIFEST.in", catalog_target}:
            continue
        if kind == "provider-template":
            continue
        if target == "templates/providers.yml":
            content = _render_provider_catalog(provider_catalog)
        elif target == "templates/code-mower.yml.j2":
            content = _config_template_text()
        elif target.startswith("templates/workflows/") or target.startswith("src/code_mower/templates/workflows/"):
            content = _workflow_template_text(target)
        elif target.startswith("templates/lanes/") or target.startswith("src/code_mower/templates/lanes/"):
            content = _lane_template_text(target)
        else:
            continue
        _write_text(output_dir / target, content, force=force)
        written.append({"target": target, "source": "generated", "kind": kind})

    for row in plan.data["provider_templates"]:
        lane = row["lane"]
        target = f"templates/providers/{lane}.yml"
        _write_text(
            output_dir / target,
            _render_provider_catalog({lane: provider_templates[lane]}),
            force=force,
        )
        written.append(
            {"target": target, "source": "generated", "kind": "provider-template"}
        )

    manifest = {
        "mode": "materialize",
        "package": plan.data["package"],
        "output_dir": str(output_dir),
        "files_written": written,
        "deferred_package_files": list(plan.data["deferred_package_files"]),
    }
    manifest_path = output_dir / "code-mower-package-manifest.json"
    written.append(
        {
            "target": "code-mower-package-manifest.json",
            "source": "generated",
            "kind": "manifest",
        }
    )
    manifest["files_written"] = written
    _write_text(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        force=force,
    )

    lines = [
        "Code Mower package materialized",
        f"Output: {output_dir}",
        f"Files written: {len(written)}",
        "",
        "Deferred package files:",
    ]
    if manifest["deferred_package_files"]:
        lines.extend(
            f"- {entry['source']} ({entry['reason']})"
            for entry in manifest["deferred_package_files"]
        )
    else:
        lines.append("- none")

    return RenderedPlan(text="\n".join(lines) + "\n", data=manifest)


COMMITTED_PACKAGE_MANIFEST = "code-mower-package-manifest.json"
GENERATED_OUTPUT_DIR = "<generated-output-dir>"
MANIFEST_TEXT_FIELDS = ("mode", "output_dir")
MANIFEST_MAPPING_FIELDS = ("package",)
MANIFEST_LIST_FIELDS = ("files_written", "deferred_package_files")
MANIFEST_TOP_LEVEL_FIELDS = (
    *MANIFEST_TEXT_FIELDS,
    *MANIFEST_MAPPING_FIELDS,
    *MANIFEST_LIST_FIELDS,
)
MANIFEST_FILE_FIELDS = ("target", "source", "kind")
MANIFEST_DEFERRED_FIELDS = ("target", "source", "reason")


class PackageManifestError(ValueError):
    """A package manifest that cannot be compared, with bounded problem text."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _manifest_row_problems(
    label: str, rows: object, fields: Sequence[str], unique_targets: bool
) -> list[str]:
    if not isinstance(rows, list):
        return [f"{label} is not a list"]
    problems: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            problems.append(f"{label} row {index} is not an object")
            continue
        missing = [field for field in fields if field not in row]
        if missing:
            problems.append(f"{label} row {index} is missing {', '.join(missing)}")
        unexpected = sorted(set(row) - set(fields))
        if unexpected:
            problems.append(f"{label} row {index} has unexpected {', '.join(unexpected)}")
        non_text = sorted(
            field
            for field in fields
            if field in row and not isinstance(row[field], str)
        )
        if non_text:
            problems.append(f"{label} row {index} has non-string {', '.join(non_text)}")
        target = row.get("target")
        if unique_targets and isinstance(target, str):
            if target in seen:
                problems.append(f"{label} has a duplicate target {target}")
            seen.add(target)
    return problems


def package_manifest_problems(manifest: object) -> list[str]:
    """Describe every structural problem that blocks an exact manifest comparison.

    Normalization is a comparison surface, not a repair step: filtering unknown
    fields or malformed rows would let a corrupted committed artifact normalize
    into apparent agreement with a freshly generated one.
    """

    if not isinstance(manifest, Mapping):
        return ["manifest is not an object"]
    problems: list[str] = []
    missing = [field for field in MANIFEST_TOP_LEVEL_FIELDS if field not in manifest]
    if missing:
        problems.append(f"manifest is missing {', '.join(missing)}")
    unexpected = sorted(set(manifest) - set(MANIFEST_TOP_LEVEL_FIELDS))
    if unexpected:
        problems.append(f"manifest has unexpected {', '.join(unexpected)}")
    for field in MANIFEST_TEXT_FIELDS:
        if field in manifest and not isinstance(manifest[field], str):
            problems.append(f"{field} is not a string")
    for field in MANIFEST_MAPPING_FIELDS:
        value = manifest.get(field)
        if field in manifest and not isinstance(value, Mapping):
            problems.append(f"{field} is not an object")
        elif isinstance(value, Mapping):
            invalid = sorted(
                str(key)
                for key, item in value.items()
                if not isinstance(key, str) or not isinstance(item, str)
            )
            if invalid:
                problems.append(f"{field} has non-string {', '.join(invalid)}")
    problems.extend(
        _manifest_row_problems(
            "files_written",
            manifest.get("files_written") if "files_written" in manifest else [],
            MANIFEST_FILE_FIELDS,
            unique_targets=True,
        )
    )
    problems.extend(
        _manifest_row_problems(
            "deferred_package_files",
            (
                manifest.get("deferred_package_files")
                if "deferred_package_files" in manifest
                else []
            ),
            MANIFEST_DEFERRED_FIELDS,
            unique_targets=False,
        )
    )
    return problems


def normalized_package_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a materialization manifest so committed copies compare exactly.

    The local output directory is replaced by a placeholder and the written-file
    inventory is ordered, so the committed artifact records the package surface
    and its source mappings rather than one machine's materialization run. A
    manifest that cannot be compared raises ``PackageManifestError`` instead of
    being silently repaired.
    """

    problems = package_manifest_problems(manifest)
    if problems:
        raise PackageManifestError(problems)
    files = [
        {field: str(entry[field]) for field in MANIFEST_FILE_FIELDS}
        for entry in manifest["files_written"]
    ]
    deferred = sorted(
        ({field: str(entry[field]) for field in MANIFEST_DEFERRED_FIELDS}
         for entry in manifest["deferred_package_files"]),
        key=lambda entry: tuple(entry[field] for field in MANIFEST_DEFERRED_FIELDS),
    )
    return {
        "mode": str(manifest["mode"]),
        "package": {str(key): str(value) for key, value in manifest["package"].items()},
        "output_dir": GENERATED_OUTPUT_DIR,
        "files_written": sorted(
            files, key=lambda entry: tuple(entry[field] for field in MANIFEST_FILE_FIELDS)
        ),
        "deferred_package_files": deferred,
    }


def generate_committed_package_manifest(repo_root: Path) -> dict[str, Any]:
    """Materialize the package into a scratch tree and normalize its manifest."""

    repo_root = Path(repo_root).expanduser().resolve()
    templates = repo_root / "src" / "code_mower" / "templates"
    plan = render_package_plan(
        load_config(templates / "code-mower.example.yml"),
        load_provider_templates(templates / "providers.yml"),
    )
    with tempfile.TemporaryDirectory() as tmp:
        materialized = materialize_package_plan(
            plan, output_dir=Path(tmp), repo_root=repo_root, force=True
        )
    return normalized_package_manifest(materialized.data)


def committed_package_manifest_text(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?")
    parser.add_argument(
        "--provider-templates",
        default=None,
        help="provider template catalog to include in the package plan",
    )
    parser.add_argument("--package-name", default="code-mower")
    parser.add_argument("--dry-run", action="store_true", help="render the package plan")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="materialize the package tree into this directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing files under --output-dir",
    )
    parser.add_argument("--json", action="store_true", help="emit package plan as JSON")
    args = parser.parse_args(argv)

    if not args.dry_run and args.output_dir is None:
        print("error: pass --dry-run or --output-dir", file=sys.stderr)
        return 1
    if args.dry_run and args.output_dir is not None:
        print("error: --dry-run and --output-dir are mutually exclusive", file=sys.stderr)
        return 1

    try:
        config_path = resolve_package_config_path(
            args.config or DEFAULT_PACKAGE_CONFIG,
            explicit=args.config is not None,
        )
        plan = render_package_plan(
            load_config(config_path),
            load_provider_templates(
                resolve_provider_templates_path(DEFAULT_PROVIDER_TEMPLATES)
                if args.provider_templates is None
                else Path(args.provider_templates)
            ),
            package_name=args.package_name,
        )
        if args.output_dir is not None:
            plan = materialize_package_plan(
                plan,
                output_dir=args.output_dir,
                repo_root=_default_package_source_root(),
                force=args.force,
            )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan.data, indent=2, sort_keys=True))
    else:
        print(plan.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
