"""Context-pack input materialization for calibration runs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .commands import repo_path_for_item
from .identity import safe_slug

if __package__ == "tools.calibration":  # pragma: no cover - legacy product layout.
    from tools import code_mower_context_packs
else:  # pragma: no cover - import shape depends on package layout.
    from .. import code_mower_context_packs


CONTEXT_PACK_CLI_LANES = {"antigravity-cli", "gemini-cli", "hermes-cli"}


def repo_roots_from_path_map(repo_path_map: Mapping[str, str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for selector, path_text in repo_path_map.items():
        repo = re.split(r"[#@]", str(selector), maxsplit=1)[0]
        if "/" in repo and str(path_text).strip():
            roots.setdefault(repo, Path(path_text).expanduser())
    return roots


def changed_files_from_checkout(repo_path: Path, base_ref: str) -> list[dict[str, str]]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=repo_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            "unable to list changed files for context packs with "
            f"{base_ref}...HEAD in {repo_path}: {completed.stderr.strip()}"
        )
    return [
        {"filename": line.strip()}
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def selected_context_pack_manifest(
    *,
    context_pack_manifest: Mapping[str, Any],
    item: Mapping[str, Any],
    repo_path: Path,
) -> dict[str, Any] | None:
    selected_ids = {
        str(pack_id).strip()
        for pack_id in item.get("context_packs", []) or []
        if str(pack_id).strip()
    }
    if not selected_ids:
        return None
    pack_values = context_pack_manifest.get("packs")
    if not isinstance(pack_values, list):
        raise ValueError("context pack manifest must include a packs list")
    packs_by_id = {
        str(pack.get("id") or "").strip(): pack
        for pack in pack_values
        if isinstance(pack, Mapping)
    }
    missing = sorted(pack_id for pack_id in selected_ids if pack_id not in packs_by_id)
    if missing:
        raise ValueError(f"context pack manifest is missing pack id(s): {', '.join(missing)}")
    base_ref = str(item.get("base_ref") or "origin/main")
    return {
        "repo": str(item.get("repo") or context_pack_manifest.get("repo") or ""),
        "pr_number": item.get("pr_number"),
        "head_sha": str(item.get("head_sha") or context_pack_manifest.get("head_sha") or ""),
        "changed_files": changed_files_from_checkout(repo_path, base_ref),
        "packs": [packs_by_id[pack_id] for pack_id in sorted(selected_ids)],
    }


def render_materialized_context_pack_prompt_text(report: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "Code Mower selected context packs",
        f"Manifest: {report.get('manifest_path', '')}",
        "",
    ]
    for pack in report.get("packs", []) or []:
        if not isinstance(pack, Mapping):
            continue
        pack_id = str(pack.get("id") or "context-pack")
        reason = str(pack.get("reason") or "").strip()
        lines.append(f"## Context Pack: {pack_id}")
        if reason:
            lines.append(f"Reason: {reason}")
        files = pack.get("files", [])
        if not isinstance(files, list) or not files:
            lines.append("(no files materialized)")
            lines.append("")
            continue
        for file_entry in files:
            if not isinstance(file_entry, Mapping):
                continue
            path = str(file_entry.get("path") or "")
            repo = str(file_entry.get("repo") or report.get("repo") or "")
            if file_entry.get("exists") is False:
                reason_text = str(file_entry.get("reason") or "missing")
                lines.append(f"### {path}")
                if repo:
                    lines.append(f"Repository: {repo}")
                lines.append(f"[not included: {reason_text}]")
                lines.append("")
                continue
            artifact_path = Path(str(file_entry.get("artifact_path") or ""))
            try:
                content = artifact_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                content = f"[unable to read context artifact: {exc}]"
            lines.append(f"### {path}")
            if repo:
                lines.append(f"Repository: {repo}")
            if file_entry.get("truncated"):
                lines.append(
                    "[truncated to "
                    f"{file_entry.get('bytes_written')} of "
                    f"{file_entry.get('source_bytes')} bytes]"
                )
            lines.append("```")
            lines.append(content.rstrip())
            lines.append("```")
            lines.append("")
    warnings = [
        str(warning)
        for warning in report.get("warnings", []) or []
        if str(warning).strip()
    ]
    if warnings:
        lines.append("## Context Pack Warnings")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines).rstrip() + "\n"


def context_pack_file_for_command(
    *,
    item: Mapping[str, Any],
    lane_id: str,
    result_dir: Path,
    repo_path_map: Mapping[str, str],
    context_pack_manifest: Mapping[str, Any] | None,
    context_pack_output_dir: Path,
    require_context_pack_files: bool,
) -> Path | None:
    if lane_id not in CONTEXT_PACK_CLI_LANES or context_pack_manifest is None:
        return None
    selected_ids = [
        str(pack_id).strip()
        for pack_id in item.get("context_packs", []) or []
        if str(pack_id).strip()
    ]
    if not selected_ids:
        return None
    repo_path_text = repo_path_for_item(item, repo_path_map)
    if not repo_path_text:
        raise ValueError(
            "context-pack calibration needs --repo-path-map for "
            f"{item.get('repo')}#{item.get('pr_number')}"
        )
    repo_path = Path(repo_path_text).expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"context-pack repo path is not a directory: {repo_path}")
    manifest = selected_context_pack_manifest(
        context_pack_manifest=context_pack_manifest,
        item=item,
        repo_path=repo_path,
    )
    if manifest is None:
        return None
    plan = code_mower_context_packs.build_context_pack_plan(manifest)
    output_dir = context_pack_output_dir / safe_slug(
        str(item.get("calibration_run_id") or result_dir.parent.name),
        "run",
    ) / safe_slug(lane_id, "lane")
    report = code_mower_context_packs.materialize_context_pack_plan(
        plan,
        repo_root=repo_path,
        output_dir=output_dir,
        require_files=require_context_pack_files,
        repo_roots=repo_roots_from_path_map(repo_path_map),
    )
    context_text_path = result_dir / "context-pack.txt"
    context_text_path.write_text(
        render_materialized_context_pack_prompt_text(report),
        encoding="utf-8",
    )
    return context_text_path
