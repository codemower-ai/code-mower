#!/usr/bin/env python3
"""Render blind-review artifact storage and release manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        sys.path.insert(0, str(module_dir.parent))
        from code_mower import blind_review_coordinator
    else:
        sys.path.insert(0, str(module_dir.parent))
        from tools import blind_review_coordinator
elif __package__ == "tools":
    from tools import blind_review_coordinator
else:  # pragma: no cover - exercised after package extraction.
    from . import blind_review_coordinator


DEFAULT_OUTPUT_DIR = ".code-mower/blind-review"
DEFAULT_STORAGE_BACKEND = "github_actions_artifact"
SUPPORTED_STORAGE_BACKENDS = {DEFAULT_STORAGE_BACKEND}
DEFAULT_RETENTION_DAYS = 14
GITHUB_ACTIONS_MAX_RETENTION_DAYS = 90
SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    text = SAFE_SLUG_RE.sub("-", text).strip("._-")
    while ".." in text:
        text = text.replace("..", ".")
    text = text.strip("._-")
    return text or fallback


def _repo_slug(repo: Any) -> str:
    return _safe_slug(str(repo or "").replace("/", "__"), "unknown-repo")


def _pr_slug(pr_number: Any) -> str:
    return _safe_slug(pr_number, "unknown-pr")


def _head_slug(head_sha: Any) -> str:
    text = str(head_sha or "").strip()
    if text:
        return _safe_slug(text, "unknown-head")
    return "unknown-head"


def _required_manifest_value(value: Any, field: str) -> Any:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"blind-review artifact manifest requires {field}")
    return value


def _artifact_name(
    repo_slug: str,
    pr_slug: str,
    head_slug: str,
    lane_slug: str,
) -> str:
    return f"code-mower-blind-{repo_slug}-pr-{pr_slug}-{head_slug}-{lane_slug}"


def build_blind_review_artifact_plan(
    manifest: Mapping[str, Any],
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    storage_backend: str = DEFAULT_STORAGE_BACKEND,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    """Build deterministic hidden-artifact and release paths for a manifest."""

    if storage_backend not in SUPPORTED_STORAGE_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_STORAGE_BACKENDS))
        raise ValueError(
            f"unsupported storage_backend {storage_backend!r}; supported: {supported}"
        )
    if retention_days <= 0:
        raise ValueError("retention_days must be greater than zero")
    if (
        storage_backend == "github_actions_artifact"
        and retention_days > GITHUB_ACTIONS_MAX_RETENTION_DAYS
    ):
        raise ValueError(
            "github_actions_artifact retention_days must be between "
            f"1 and {GITHUB_ACTIONS_MAX_RETENTION_DAYS}"
        )

    plan = blind_review_coordinator.build_blind_review_plan(manifest)
    repo = _required_manifest_value(plan.get("repo", manifest.get("repo")), "repo")
    pr_number = _required_manifest_value(
        plan.get("pr_number", manifest.get("pr_number")),
        "pr_number",
    )
    head_sha = _required_manifest_value(
        plan.get("head_sha", manifest.get("head_sha")),
        "head_sha",
    )
    repo_id = _repo_slug(repo)
    pr_id = _pr_slug(pr_number)
    head_id = _head_slug(head_sha)
    base_path = Path(output_dir) / repo_id / f"pr-{pr_id}" / head_id
    lane_states = plan.get("lane_states", {})
    held_artifacts: list[dict[str, Any]] = []
    seen_lanes: set[str] = set()
    seen_lane_slugs: dict[str, str] = {}

    for lane in plan.get("required_lanes", []):
        if lane in seen_lanes:
            raise ValueError(f"blind-review lane is duplicated: {lane!r}")
        seen_lanes.add(lane)
        lane_slug = _safe_slug(lane, "unknown-lane")
        previous_lane = seen_lane_slugs.get(lane_slug)
        if previous_lane is not None and previous_lane != lane:
            raise ValueError(
                "blind-review lanes produce duplicate artifact slug "
                f"{lane_slug!r}: {previous_lane!r}, {lane!r}"
            )
        seen_lane_slugs[lane_slug] = lane
        state = lane_states.get(lane, {}) if isinstance(lane_states, Mapping) else {}
        if not isinstance(state, Mapping):
            state = {}
        lane_path = base_path / lane_slug
        artifact_id = _artifact_name(repo_id, pr_id, head_id, lane_slug)
        held_artifacts.append(
            {
                "lane": lane,
                "state": state.get("state", "pending"),
                "source_artifact": state.get("artifact", ""),
                "head_sha": state.get("head_sha", ""),
                "artifact_id": artifact_id,
                "held_path": str(lane_path / "held.md"),
                "metadata_path": str(lane_path / "metadata.json"),
                "github_actions_artifact": {
                    "name": artifact_id,
                    "path": str(lane_path),
                    "retention_days": retention_days,
                },
            }
        )

    artifacts_by_lane = {
        artifact["lane"]: artifact
        for artifact in held_artifacts
    }
    release_ready = bool(plan.get("ready_to_release"))
    release_artifacts: list[dict[str, Any]] = []
    if release_ready:
        for lane in plan.get("release_order", []):
            artifact = artifacts_by_lane.get(lane)
            if artifact is None:
                continue
            lane_slug = _safe_slug(lane, "unknown-lane")
            release_artifact = dict(artifact)
            release_artifact["release_path"] = str(
                base_path / "release" / f"{lane_slug}.md"
            )
            release_artifacts.append(release_artifact)
    release_manifest = {
        "ready_to_release": release_ready,
        "release_order": list(plan.get("release_order", [])),
        "artifacts": release_artifacts,
    }
    if release_ready:
        release_manifest["path"] = str(base_path / "release" / "manifest.json")

    return {
        "mode": "blind-review-artifacts",
        "storage": {
            "backend": storage_backend,
            "output_dir": str(output_dir),
            "retention_days": retention_days,
        },
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "plan": plan,
        "held_artifacts": held_artifacts,
        "release_manifest": release_manifest,
        "github_actions": {
            "upload_when": "lane_completed",
            "download_when": "coordinator_ready_to_release",
            "artifact_name_pattern": "code-mower-blind-{repo}-pr-{pr}-{head}-{lane}",
            "retention_days": retention_days,
        },
        "warnings": list(plan.get("warnings", [])),
    }


def _write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise ValueError(f"refusing to overwrite existing artifact file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: Mapping[str, Any], *, force: bool) -> None:
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", force=force)


def _copy_file(source: Path, target: Path, *, force: bool) -> None:
    if target.exists() and not force:
        raise ValueError(f"refusing to overwrite existing artifact file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _resolve_source_artifact(
    source_artifact: str,
    *,
    source_base_dir: Path | None = None,
) -> Path | None:
    source_text = str(source_artifact or "").strip()
    if not source_text:
        return None
    source_path = Path(source_text).expanduser()
    if not source_path.is_absolute() and source_base_dir is not None:
        source_path = source_base_dir / source_path
    return source_path


def _placeholder_artifact_text(artifact: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Blind Review Artifact Placeholder",
            "",
            "The source artifact was not available when this plan was materialized.",
            "",
            f"Lane: {artifact.get('lane')}",
            f"State: {artifact.get('state')}",
            f"Head SHA: {artifact.get('head_sha')}",
            f"Source artifact: {artifact.get('source_artifact') or '(none)'}",
            "",
        ]
    )


def _is_placeholder_artifact(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readline().strip() == "# Blind Review Artifact Placeholder"
    except UnicodeDecodeError:
        return False


def _metadata_marks_missing_source(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, Mapping) and payload.get("source_available") is False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_blind_review_artifact_plan(
    artifact_plan: Mapping[str, Any],
    *,
    force: bool = False,
    require_sources: bool = False,
    source_base_dir: Path | None = None,
) -> dict[str, Any]:
    """Write held artifacts, metadata, and release files for a plan."""

    plan = json.loads(json.dumps(artifact_plan))
    writes: list[dict[str, Any]] = []
    release_sources: dict[str, Path] = {}
    release_held_available: dict[str, bool] = {}
    storage = plan.get("storage", {})
    release_manifest = plan.get("release_manifest", {})
    release_ready = (
        bool(release_manifest.get("ready_to_release"))
        if isinstance(release_manifest, Mapping)
        else False
    )

    for artifact in plan.get("held_artifacts", []):
        if not isinstance(artifact, Mapping):
            continue
        held_path_text = str(artifact.get("held_path") or "").strip()
        metadata_path_text = str(artifact.get("metadata_path") or "").strip()
        if not held_path_text or not metadata_path_text:
            raise ValueError("held artifact paths must be present before materializing")
        held_path = Path(held_path_text)
        metadata_path = Path(metadata_path_text)

        source_path = _resolve_source_artifact(
            str(artifact.get("source_artifact") or ""),
            source_base_dir=source_base_dir,
        )
        source_available = bool(source_path and source_path.is_file())
        source_sha256 = (
            _sha256_file(source_path)
            if source_available and source_path is not None
            else ""
        )
        held_available = held_path.is_file()
        if require_sources and not source_available:
            raise ValueError(
                "source artifact is required but missing for lane "
                f"{artifact.get('lane')}: {artifact.get('source_artifact') or '(none)'}"
            )
        if release_ready and not source_available:
            if not held_available or _is_placeholder_artifact(held_path):
                raise ValueError(
                    "refusing to release placeholder artifact for lane "
                    f"{artifact.get('lane')}: {artifact.get('source_artifact') or '(none)'}"
                )

        metadata = {
            "lane": artifact.get("lane"),
            "state": artifact.get("state"),
            "head_sha": artifact.get("head_sha"),
            "artifact_id": artifact.get("artifact_id"),
            "source_artifact": artifact.get("source_artifact"),
            "source_available": source_available,
            "source_sha256": source_sha256,
            "storage": storage,
        }

        if source_available and source_path is not None:
            refresh_placeholder = held_path.is_file() and _is_placeholder_artifact(held_path)
            reuse_existing_held = held_path.is_file() and not refresh_placeholder and not force
            if reuse_existing_held:
                if not metadata_path.is_file():
                    _write_json(metadata_path, metadata, force=force)
                    writes.append({"kind": "metadata", "path": str(metadata_path)})
            else:
                _write_json(
                    metadata_path,
                    metadata,
                    force=force
                    or refresh_placeholder
                    or _metadata_marks_missing_source(metadata_path),
                )
                writes.append({"kind": "metadata", "path": str(metadata_path)})
                _copy_file(source_path, held_path, force=force or refresh_placeholder)
                writes.append(
                    {
                        "kind": "held_artifact",
                        "path": str(held_path),
                        "source": str(source_path),
                        "source_available": True,
                        "sha256": _sha256_file(held_path),
                    }
                )
            releasable = True
        elif release_ready and held_available:
            if not metadata_path.is_file():
                _write_json(metadata_path, metadata, force=force)
                writes.append({"kind": "metadata", "path": str(metadata_path)})
            plan.setdefault("warnings", []).append(
                f"source artifact missing for lane {artifact.get('lane')}; "
                "using existing held artifact for release"
            )
            releasable = True
        else:
            if held_available:
                if not metadata_path.is_file():
                    _write_json(metadata_path, metadata, force=force)
                    writes.append({"kind": "metadata", "path": str(metadata_path)})
                if _is_placeholder_artifact(held_path):
                    plan.setdefault("warnings", []).append(
                        f"source artifact missing for lane {artifact.get('lane')}; "
                        "reused held placeholder"
                    )
                    releasable = False
                else:
                    plan.setdefault("warnings", []).append(
                        f"source artifact missing for lane {artifact.get('lane')}; "
                        "using existing held artifact"
                    )
                    releasable = True
            else:
                _write_json(
                    metadata_path,
                    metadata,
                    force=force or _metadata_marks_missing_source(metadata_path),
                )
                writes.append({"kind": "metadata", "path": str(metadata_path)})
                _write_text(held_path, _placeholder_artifact_text(artifact), force=force)
                plan.setdefault("warnings", []).append(
                    f"source artifact missing for lane {artifact.get('lane')}; "
                    "materialized held placeholder"
                )
                writes.append(
                    {
                        "kind": "held_artifact",
                        "path": str(held_path),
                        "source": str(source_path) if source_path is not None else "",
                        "source_available": False,
                        "sha256": _sha256_file(held_path),
                    }
                )
                releasable = False
        lane = str(artifact.get("lane") or "")
        release_sources[lane] = held_path
        release_held_available[lane] = releasable

    if isinstance(release_manifest, Mapping) and release_manifest.get("ready_to_release"):
        for artifact in release_manifest.get("artifacts", []) or []:
            if not isinstance(artifact, Mapping):
                continue
            lane = str(artifact.get("lane") or "")
            held_path = release_sources.get(lane)
            release_path = Path(str(artifact.get("release_path") or ""))
            if held_path is None:
                raise ValueError(f"release artifact has no held source for lane {lane}")
            if not release_held_available.get(lane, False):
                raise ValueError(
                    f"refusing to release placeholder artifact for lane {lane}; "
                    "held artifact was unavailable"
                )
            held_sha256 = _sha256_file(held_path)
            _copy_file(held_path, release_path, force=force)
            release_sha256 = _sha256_file(release_path)
            if isinstance(artifact, dict):
                artifact["held_sha256"] = held_sha256
                artifact["release_sha256"] = release_sha256
            writes.append(
                {
                    "kind": "release_artifact",
                    "path": str(release_path),
                    "source": str(held_path),
                    "sha256": release_sha256,
                }
            )
        manifest_path = release_manifest.get("path")
        if manifest_path:
            _write_json(Path(str(manifest_path)), release_manifest, force=force)
            writes.append({"kind": "release_manifest", "path": str(manifest_path)})

    plan["writes"] = writes
    return plan


def render_blind_review_artifacts_text(artifact_plan: Mapping[str, Any]) -> str:
    storage = artifact_plan.get("storage", {})
    release_manifest = artifact_plan.get("release_manifest", {})
    storage_backend = storage.get("backend", "") if isinstance(storage, Mapping) else ""
    ready = (
        release_manifest.get("ready_to_release")
        if isinstance(release_manifest, Mapping)
        else False
    )
    lines = [
        f"Blind review artifacts for {artifact_plan.get('repo') or 'unknown repo'}#{artifact_plan.get('pr_number') or '?'}",
        f"storage_backend: {storage_backend}",
        f"ready_to_release: {str(ready).lower()}",
        "",
        "Held artifacts:",
    ]
    for artifact in artifact_plan.get("held_artifacts", []):
        if not isinstance(artifact, Mapping):
            continue
        lines.append(
            f"- {artifact.get('lane')}: {artifact.get('state')} "
            f"name={artifact.get('artifact_id')} path={artifact.get('held_path')}"
        )
    if isinstance(release_manifest, Mapping) and release_manifest.get("artifacts"):
        lines.extend(["", "Release artifacts:"])
        for artifact in release_manifest.get("artifacts", []):
            if isinstance(artifact, Mapping):
                lines.append(f"- {artifact.get('lane')}: {artifact.get('release_path')}")
    if artifact_plan.get("warnings"):
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in artifact_plan.get("warnings", []))
    if artifact_plan.get("writes"):
        lines.extend(["", "Writes:"])
        for write in artifact_plan.get("writes", []):
            if isinstance(write, Mapping):
                lines.append(f"- {write.get('kind')}: {write.get('path')}")
    return "\n".join(lines) + "\n"


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("blind-review artifact manifest must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--storage-backend",
        choices=sorted(SUPPORTED_STORAGE_BACKENDS),
        default=DEFAULT_STORAGE_BACKEND,
    )
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Materialize held artifacts, metadata, and release files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --write to overwrite existing artifact files.",
    )
    parser.add_argument(
        "--require-sources",
        action="store_true",
        help="Fail --write if a source artifact path is missing or unreadable.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = _load_manifest(args.manifest)
        artifact_plan = build_blind_review_artifact_plan(
            manifest,
            output_dir=args.output_dir,
            storage_backend=args.storage_backend,
            retention_days=args.retention_days,
        )
        if args.write:
            artifact_plan = materialize_blind_review_artifact_plan(
                artifact_plan,
                force=args.force,
                require_sources=args.require_sources,
                source_base_dir=args.manifest.parent,
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(artifact_plan, indent=2, sort_keys=True))
    else:
        print(render_blind_review_artifacts_text(artifact_plan), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
