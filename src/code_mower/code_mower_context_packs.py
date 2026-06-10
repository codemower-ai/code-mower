#!/usr/bin/env python3
"""Plan bounded context packs for Code Mower review lanes.

Context packs are manifests, not file dumps. They let a lane ask for important
surrounding files by path/pattern without bloating every audit prompt by
default. A later runner can materialize the listed files under the declared byte
caps.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
from pathlib import Path


DEFAULT_PACK_ID = "changed-files-context"
DEFAULT_MAX_FILES = 12
DEFAULT_MAX_FILE_BYTES = 60_000
MAX_MATERIALIZED_FILE_BYTES = 250_000
MAX_CONTEXT_PACKS = 20


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _safe_relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute():
        raise ValueError(f"context pack path must be relative: {text!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"context pack path must not contain . or ..: {text!r}")
    return str(path)


def _safe_pack_id(value: Any) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text.replace("\\", "/"))
    if not text or path.is_absolute() or len(path.parts) != 1:
        raise ValueError(f"context pack id must be a safe file name: {text!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"context pack id must not contain . or ..: {text!r}")
    return text


def _file_entries(values: Iterable[Any], *, default_repo: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        if isinstance(item, Mapping):
            raw_path = item.get("filename", item.get("path"))
            repo = str(item.get("repo") or default_repo)
        else:
            raw_path = item
            repo = default_repo
        path = _safe_relative_path(raw_path)
        key = (repo, path)
        if key not in seen:
            seen.add(key)
            entry = {"path": path}
            if repo and repo != default_repo:
                entry["repo"] = repo
            entries.append(entry)
    return entries


def _patterns_for_pack(pack: Mapping[str, Any]) -> list[str]:
    includes = pack.get("include")
    if includes is None:
        includes = pack.get("paths")
    if includes is None:
        return ["*"]
    if not isinstance(includes, list):
        raise ValueError("context pack include must be a list")
    patterns = []
    for pattern in includes:
        text = str(pattern or "").strip().replace("\\", "/")
        if not text:
            continue
        if PurePosixPath(text).is_absolute() or ".." in PurePosixPath(text).parts:
            raise ValueError(f"context pack pattern must be relative: {text!r}")
        patterns.append(text)
    return patterns or ["*"]


def _repos_for_pack(pack: Mapping[str, Any], default_repo: str) -> set[str]:
    repos = pack.get("repos")
    if repos is None:
        repo = str(pack.get("repo") or default_repo)
        return {repo}
    if not isinstance(repos, list):
        raise ValueError("context pack repos must be a list")
    normalized = {str(repo).strip() for repo in repos if str(repo).strip()}
    return normalized or {default_repo}


def _plan_file_entry(path: str, repo: str, default_repo: str, max_bytes: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": path, "max_bytes": max_bytes}
    if repo and repo != default_repo:
        entry["repo"] = repo
    return entry


def _int_option(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _file_bytes_option(value: Any, default: int, name: str) -> tuple[int, bool]:
    parsed = _int_option(value, default, name)
    if parsed > MAX_MATERIALIZED_FILE_BYTES:
        return MAX_MATERIALIZED_FILE_BYTES, True
    return parsed, False


def _default_pack() -> dict[str, Any]:
    return {
        "id": DEFAULT_PACK_ID,
        "reason": "bounded surrounding context for changed files",
        "include": ["*"],
    }


def build_context_pack_plan(manifest: Mapping[str, Any]) -> dict[str, Any]:
    default_repo = str(manifest.get("repo") or "")
    changed_values = manifest.get("changed_files", [])
    if not isinstance(changed_values, list):
        raise ValueError("changed_files must be a list")
    changed_files = _file_entries(changed_values, default_repo=default_repo)

    pack_values = manifest.get("packs") or [_default_pack()]
    if not isinstance(pack_values, list):
        raise ValueError("packs must be a list")
    if len(pack_values) > MAX_CONTEXT_PACKS:
        raise ValueError(f"too many context packs: max {MAX_CONTEXT_PACKS}")

    planned_packs: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_pack_ids: set[str] = set()

    for pack_value in pack_values:
        if not isinstance(pack_value, Mapping):
            raise ValueError("each context pack must be an object")
        pack_id = _safe_pack_id(pack_value.get("id") or DEFAULT_PACK_ID)
        if pack_id in seen_pack_ids:
            raise ValueError(f"duplicate context pack id: {pack_id}")
        seen_pack_ids.add(pack_id)

        patterns = _patterns_for_pack(pack_value)
        pack_repos = _repos_for_pack(pack_value, default_repo)
        if (
            not default_repo
            and pack_value.get("repo") is None
            and pack_value.get("repos") is None
        ):
            pack_repos = {
                str(file_entry.get("repo") or default_repo)
                for file_entry in changed_files
            } or {default_repo}
        max_files = _int_option(pack_value.get("max_files"), DEFAULT_MAX_FILES, "max_files")
        max_file_bytes, capped_bytes = _file_bytes_option(
            pack_value.get("max_file_bytes"),
            DEFAULT_MAX_FILE_BYTES,
            "max_file_bytes",
        )
        if capped_bytes:
            warnings.append(
                f"{pack_id}: max_file_bytes capped at {MAX_MATERIALIZED_FILE_BYTES}"
            )
        matched_changed_entries = []
        for file_entry in changed_files:
            path = file_entry["path"]
            repo = file_entry.get("repo", default_repo)
            if repo in pack_repos and any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
                matched_changed_entries.append(
                    _plan_file_entry(path, repo, default_repo, max_file_bytes)
                )
        extra_values = pack_value.get("extra_files", pack_value.get("context_files", []))
        if extra_values is None:
            extra_values = []
        if not isinstance(extra_values, list):
            raise ValueError("context pack extra_files must be a list")
        extra_entries = []
        extra_default_repo = (
            str(pack_value.get("repo"))
            if pack_value.get("repo")
            else next(iter(pack_repos))
            if len(pack_repos) == 1
            else default_repo
        )
        for file_entry in _file_entries(extra_values, default_repo=extra_default_repo):
            repo = file_entry.get("repo", extra_default_repo)
            extra_entries.append(
                _plan_file_entry(file_entry["path"], repo, default_repo, max_file_bytes)
            )
        pack_repos = pack_repos | {
            str(file_entry.get("repo") or default_repo)
            for file_entry in extra_entries
        }
        deduped_entries: list[dict[str, Any]] = []
        seen_files: set[tuple[str, str]] = set()
        for file_entry in [*matched_changed_entries, *extra_entries]:
            repo = str(file_entry.get("repo") or default_repo)
            key = (repo, str(file_entry.get("path") or ""))
            if key not in seen_files:
                seen_files.add(key)
                deduped_entries.append(file_entry)
        if len(deduped_entries) > max_files:
            warnings.append(
                f"{pack_id}: matched {len(deduped_entries)} files; keeping first {max_files} with explicit extra_files first"
            )
            priority_entries: list[dict[str, Any]] = []
            seen_priority_files: set[tuple[str, str]] = set()
            for file_entry in [*extra_entries, *matched_changed_entries]:
                repo = str(file_entry.get("repo") or default_repo)
                key = (repo, str(file_entry.get("path") or ""))
                if key not in seen_priority_files:
                    seen_priority_files.add(key)
                    priority_entries.append(file_entry)
            deduped_entries = priority_entries[:max_files]
        if not deduped_entries:
            warnings.append(f"{pack_id}: no changed files matched include patterns")

        planned_packs.append(
            {
                "id": pack_id,
                "reason": str(pack_value.get("reason") or ""),
                "include": patterns,
                "repos": sorted(pack_repos),
                "max_files": max_files,
                "max_file_bytes": max_file_bytes,
                "files": deduped_entries,
            }
        )

    return {
        "mode": "code-mower-context-packs",
        "repo": default_repo,
        "pr_number": manifest.get("pr_number"),
        "head_sha": str(manifest.get("head_sha") or ""),
        "changed_file_count": len(changed_files),
        "packs": planned_packs,
        "warnings": warnings,
    }


def render_context_pack_text(plan: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower context pack plan",
        f"Changed files: {plan.get('changed_file_count', 0)}",
        "",
        "Packs:",
    ]
    packs = plan.get("packs", [])
    if isinstance(packs, list) and packs:
        for pack in packs:
            if not isinstance(pack, Mapping):
                continue
            files = pack.get("files", [])
            lines.append(
                f"- {pack.get('id')}: {len(files) if isinstance(files, list) else 0} files"
            )
            if isinstance(files, list):
                for file_entry in files:
                    if isinstance(file_entry, Mapping):
                        lines.append(
                            f"  - {file_entry.get('path')} "
                            f"(max {file_entry.get('max_bytes')} bytes)"
                        )
    else:
        lines.append("- none")
    warnings = plan.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def _read_bounded_file(source: Path, max_bytes: int) -> tuple[bytes, int, bool]:
    source_bytes = source.stat().st_size
    with source.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data, source_bytes, truncated


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def materialize_context_pack_plan(
    plan: Mapping[str, Any],
    *,
    repo_root: Path,
    output_dir: Path,
    require_files: bool = False,
    repo_roots: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Write bounded context pack files and a manifest under ``output_dir``."""

    if plan.get("mode") != "code-mower-context-packs":
        raise ValueError("context materialization expects a context pack plan")

    default_repo = str(plan.get("repo") or "")
    repo_root = repo_root.resolve()
    resolved_repo_roots = {
        str(repo): root.resolve()
        for repo, root in (repo_roots or {}).items()
    }
    resolved_repo_roots.setdefault(default_repo, repo_root)
    plan_file_repos = {
        str(file_entry.get("repo") or default_repo)
        for pack in plan.get("packs", [])
        if isinstance(pack, Mapping)
        for file_entry in pack.get("files", [])
        if isinstance(file_entry, Mapping)
    }
    non_default_plan_file_repos = {
        repo for repo in plan_file_repos if repo and repo != default_repo
    }
    if not default_repo and len(non_default_plan_file_repos) == 1:
        resolved_repo_roots.setdefault(next(iter(non_default_plan_file_repos)), repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings = list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else []
    materialized_packs: list[dict[str, Any]] = []

    packs = plan.get("packs", [])
    if not isinstance(packs, list):
        raise ValueError("context pack plan packs must be a list")

    for pack in packs:
        if not isinstance(pack, Mapping):
            continue
        pack_id = _safe_pack_id(pack.get("id") or DEFAULT_PACK_ID)
        materialized_files: list[dict[str, Any]] = []
        files = pack.get("files", [])
        if not isinstance(files, list):
            raise ValueError("context pack files must be a list")

        for file_entry in files:
            if not isinstance(file_entry, Mapping):
                continue
            file_repo = str(file_entry.get("repo") or default_repo)
            relative_path = _safe_relative_path(file_entry.get("path"))
            max_bytes, capped_bytes = _file_bytes_option(
                file_entry.get("max_bytes"),
                DEFAULT_MAX_FILE_BYTES,
                "max_bytes",
            )
            if capped_bytes:
                warnings.append(
                    f"{pack_id}: {relative_path} max_bytes capped at "
                    f"{MAX_MATERIALIZED_FILE_BYTES}"
                )
            source_root = resolved_repo_roots.get(file_repo)
            if source_root is None:
                message = (
                    f"{pack_id}: no repo root configured for related repo "
                    f"{file_repo or '(unknown)'} file {relative_path}"
                )
                if require_files:
                    raise ValueError(message)
                warnings.append(message)
                missing_entry = {
                    "path": relative_path,
                    "exists": False,
                    "reason": "missing-repo-root",
                }
                if file_repo and file_repo != default_repo:
                    missing_entry["repo"] = file_repo
                materialized_files.append(missing_entry)
                continue
            source = source_root / relative_path
            try:
                resolved_source = source.resolve(strict=True)
            except OSError:
                resolved_source = source
            source_is_symlink = source.is_symlink()
            resolved_outside_repo = not _path_is_relative_to(resolved_source, source_root)
            missing_or_nonfile = not resolved_source.is_file()
            reason = None
            if source_is_symlink:
                reason = "symlink"
                message = f"{pack_id}: skipped symlinked context file {relative_path}"
            elif resolved_outside_repo:
                reason = "outside-repo"
                message = f"{pack_id}: skipped context file outside repo_root {relative_path}"
            elif missing_or_nonfile:
                reason = "missing"
                message = f"{pack_id}: missing context file {relative_path}"

            if reason is not None:
                if require_files:
                    raise ValueError(message)
                warnings.append(message)
                skipped_entry = {
                    "path": relative_path,
                    "exists": False,
                    "reason": reason,
                }
                if file_repo and file_repo != default_repo:
                    skipped_entry["repo"] = file_repo
                materialized_files.append(skipped_entry)
                continue

            data, source_bytes, truncated = _read_bounded_file(resolved_source, max_bytes)
            repo_prefix = file_repo.replace("/", "__") if file_repo and file_repo != default_repo else ""
            relative_artifact_path = PurePosixPath(relative_path)
            if repo_prefix:
                artifact_relative_path = (
                    PurePosixPath(pack_id) / "_repos" / repo_prefix / relative_artifact_path
                )
            elif relative_artifact_path.parts and relative_artifact_path.parts[0] == "_repos":
                artifact_relative_path = PurePosixPath(pack_id) / "_primary" / relative_artifact_path
            else:
                artifact_relative_path = PurePosixPath(pack_id) / relative_artifact_path
            artifact_path = output_dir / artifact_relative_path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(data)
            materialized_entry = {
                "path": relative_path,
                "exists": True,
                "artifact_path": str(artifact_path),
                "artifact_relative_path": str(artifact_relative_path),
                "bytes_written": len(data),
                "source_bytes": source_bytes,
                "max_bytes": max_bytes,
                "truncated": truncated,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            if file_repo and file_repo != default_repo:
                materialized_entry["repo"] = file_repo
            materialized_files.append(materialized_entry)

        materialized_packs.append(
            {
                "id": pack_id,
                "reason": str(pack.get("reason") or ""),
                "files": materialized_files,
            }
        )

    manifest = {
        "mode": "code-mower-context-pack-materialization",
        "repo": str(plan.get("repo") or ""),
        "pr_number": plan.get("pr_number"),
        "head_sha": str(plan.get("head_sha") or ""),
        "repo_root": str(repo_root),
        "repo_roots": {
            repo: str(root)
            for repo, root in sorted(resolved_repo_roots.items())
            if repo
        },
        "output_dir": str(output_dir),
        "packs": materialized_packs,
        "warnings": warnings,
    }
    manifest_path = output_dir / "context-pack-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def render_context_pack_materialization_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower context pack materialization",
        f"Output: {report.get('output_dir')}",
        "",
        "Packs:",
    ]
    packs = report.get("packs", [])
    if isinstance(packs, list) and packs:
        for pack in packs:
            if not isinstance(pack, Mapping):
                continue
            files = pack.get("files", [])
            lines.append(
                f"- {pack.get('id')}: {len(files) if isinstance(files, list) else 0} files"
            )
            if isinstance(files, list):
                for file_entry in files:
                    if not isinstance(file_entry, Mapping):
                        continue
                    if file_entry.get("exists") is False:
                        lines.append(f"  - {file_entry.get('path')} (missing)")
                    else:
                        suffix = " truncated" if file_entry.get("truncated") else ""
                        lines.append(
                            f"  - {file_entry.get('path')} -> "
                            f"{file_entry.get('artifact_relative_path')} "
                            f"({file_entry.get('bytes_written')} bytes{suffix})"
                        )
    else:
        lines.append("- none")
    warnings = report.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root used when materializing files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".code-mower/context-packs"),
        help="Directory for materialized context pack artifacts.",
    )
    parser.add_argument(
        "--repo-root-map",
        action="append",
        default=[],
        metavar="OWNER/REPO=PATH",
        help="Additional repository root used when materializing related-repo context.",
    )
    parser.add_argument("--require-files", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        plan = build_context_pack_plan(_load_json(args.manifest))
        payload: Mapping[str, Any]
        if args.write:
            repo_roots = {}
            for item in args.repo_root_map:
                repo, sep, path_text = item.partition("=")
                if not sep or not repo.strip() or not path_text.strip():
                    raise ValueError("--repo-root-map must be OWNER/REPO=PATH")
                repo_roots[repo.strip()] = Path(path_text)
            payload = materialize_context_pack_plan(
                plan,
                repo_root=args.repo_root,
                output_dir=args.output_dir,
                require_files=args.require_files,
                repo_roots=repo_roots,
            )
        else:
            payload = plan
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if args.write:
            print(render_context_pack_materialization_text(payload), end="")
        else:
            print(render_context_pack_text(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
