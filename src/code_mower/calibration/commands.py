"""Calibration command materialization helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .identity import safe_slug


def parse_repo_path_map(entries: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(
                "repo path map entries must be OWNER/REPO=PATH, "
                f"OWNER/REPO#PR=PATH, or OWNER/REPO@HEAD=PATH: {entry}"
            )
        selector, path = entry.split("=", 1)
        selector = selector.strip()
        path = path.strip()
        repo = re.split(r"[#@]", selector, maxsplit=1)[0]
        if "/" not in repo or not path:
            raise ValueError(
                "repo path map entries must be OWNER/REPO=PATH, "
                f"OWNER/REPO#PR=PATH, or OWNER/REPO@HEAD=PATH: {entry}"
            )
        mapping[selector] = path
    return mapping


def repo_path_for_item(item: Mapping[str, Any], repo_path_map: Mapping[str, str]) -> str:
    repo = str(item.get("repo") or "")
    pr_number = str(item.get("pr_number") or "")
    head_sha = str(item.get("head_sha") or "")
    selectors = [
        f"{repo}#{pr_number}@{head_sha}" if repo and pr_number and head_sha else "",
        f"{repo}#{pr_number}" if repo and pr_number else "",
        f"{repo}@{head_sha}" if repo and head_sha else "",
        repo,
    ]
    for selector in selectors:
        if selector and selector in repo_path_map:
            return repo_path_map[selector]
    return ""


def command_lane_id(command: Sequence[Any]) -> str:
    parts = [str(part) for part in command]
    if len(parts) >= 2 and parts[0] == "code-mower":
        if parts[1] == "local-llm" and len(parts) >= 3 and parts[2] == "bakeoff":
            return "local-llm"
        return parts[1].replace("_", "-")
    return safe_slug(parts[0] if parts else "command", "command")


def option_value(command: Sequence[str], option: str) -> str:
    for index, part in enumerate(command):
        if part == option and index + 1 < len(command):
            return command[index + 1]
        if part.startswith(f"{option}="):
            return part.split("=", 1)[1]
    return ""


def reviewer_id_from_command(command: Sequence[Any]) -> str:
    lane_id = command_lane_id(command)
    output_dir = option_value([str(part) for part in command], "--output-dir")
    output_leaf = safe_slug(Path(output_dir).name if output_dir else "", "")
    default_leaf = {
        "antigravity-cli": "antigravity-cli",
        "gemini-cli": "gemini-cli",
        "hermes-cli": "hermes-cli",
        "coderabbit-cli": "coderabbit-cli",
        "local-llm": "local-llm",
    }.get(lane_id, lane_id)
    if output_leaf and output_leaf != default_leaf:
        return output_leaf
    return lane_id


def command_metadata_for_run(run: Mapping[str, Any], command_index: int) -> dict[str, Any]:
    command_metadata = run.get("command_metadata", [])
    if (
        isinstance(command_metadata, list)
        and 0 <= command_index < len(command_metadata)
        and isinstance(command_metadata[command_index], Mapping)
    ):
        return dict(command_metadata[command_index])
    return {}


def local_llm_profiles_from_command(command: Sequence[Any]) -> list[str]:
    profiles = option_value([str(part) for part in command], "--profiles")
    return [profile.strip() for profile in profiles.split(",") if profile.strip()]


def set_option_value(command: list[str], option: str, value: str) -> None:
    for index, part in enumerate(command):
        if part == option and index + 1 < len(command):
            command[index + 1] = value
            return
        if part.startswith(f"{option}="):
            command[index] = f"{option}={value}"
            return
    command.extend([option, value])


def has_flag(command: Sequence[str], flag: str) -> bool:
    return any(part == flag for part in command)


def rewrite_code_mower_command(
    command: Sequence[Any],
    *,
    code_mower_command: Sequence[str],
) -> list[str]:
    parts = [str(part) for part in command]
    if parts and parts[0] == "code-mower":
        return [*code_mower_command, *parts[1:]]
    return parts


def materialize_command(
    command: Sequence[Any],
    *,
    item: Mapping[str, Any],
    code_mower_command: Sequence[str],
    repo_path_map: Mapping[str, str],
    allow_historical_head: bool,
) -> list[str]:
    materialized = rewrite_code_mower_command(
        command,
        code_mower_command=code_mower_command,
    )
    lane_id = command_lane_id(command)
    repo = str(item.get("repo") or "")
    repo_path = repo_path_for_item(item, repo_path_map)
    historical_local_cli_lanes = {"antigravity-cli", "gemini-cli", "hermes-cli"}
    if lane_id in {"coderabbit-cli", "local-llm", *historical_local_cli_lanes}:
        existing_repo_path = option_value(materialized, "--repo-path")
        if repo_path:
            set_option_value(materialized, "--repo-path", repo_path)
            if lane_id in {"coderabbit-cli", "local-llm", *historical_local_cli_lanes} and allow_historical_head:
                if not has_flag(materialized, "--allow-historical-head"):
                    materialized.append("--allow-historical-head")
                if lane_id in historical_local_cli_lanes and not has_flag(
                    materialized,
                    "--historical-calibration",
                ):
                    materialized.append("--historical-calibration")
        elif existing_repo_path == "/path/to/pr-worktree":
            raise ValueError(
                f"{lane_id} for {repo} needs --repo-path-map {repo}=/path/to/pr-worktree"
            )
    return materialized


def summary_path_for_command(command: Sequence[Any]) -> Path | None:
    lane_id = command_lane_id(command)
    output_dir = option_value([str(part) for part in command], "--output-dir")
    if not output_dir:
        return None
    root = Path(output_dir)
    if lane_id == "local-llm":
        return root / "summary.json"
    if lane_id == "antigravity-cli":
        return root / "antigravity-cli.summary.json"
    if lane_id == "gemini-cli":
        return root / "gemini-cli.summary.json"
    if lane_id == "hermes-cli":
        return root / "hermes-cli.summary.json"
    if lane_id == "coderabbit-cli":
        return root / "coderabbit-cli.summary.json"
    return None


def resolve_path_for_cwd(path: Path | None, cwd: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_absolute():
        return path
    return (cwd or Path.cwd()) / path


def text_from_timeout_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
