"""Calibration pilot planning and command templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .arms import default_arms
from .identity import head_slug, safe_slug


def _run_output_dir(output_dir: Path, item: Mapping[str, Any], arm_id: str, replicate: int) -> Path:
    repo_slug = safe_slug(str(item["repo"]).replace("/", "__"), "repo")
    item_head_slug = head_slug(item.get("head_sha", ""))
    pr_dir = f"pr-{item['pr_number']}" + (f"-{item_head_slug}" if item_head_slug else "")
    return output_dir / repo_slug / pr_dir / f"r{replicate}" / arm_id


def _run_id(item: Mapping[str, Any], *, arm_id: str, replicate: int) -> str:
    base = f"{safe_slug(str(item['repo']).replace('/', '__'))}-pr-{item['pr_number']}"
    item_head_slug = head_slug(item.get("head_sha", ""))
    if item_head_slug:
        base = f"{base}-{item_head_slug}"
    return f"{base}-r{replicate}-{arm_id}"


def _local_llm_command(
    item: Mapping[str, Any],
    *,
    profiles: Sequence[str],
    output_dir: Path,
    jobs: int,
    repo_path: str = "/path/to/pr-worktree",
) -> list[str]:
    command = [
        "code-mower",
        "local-llm",
        "bakeoff",
        "--repo",
        str(item["repo"]),
        "--pr",
        str(item["pr_number"]),
        "--profiles",
        ",".join(profiles),
        "--output-dir",
        str(output_dir / "local-llm"),
        "--jobs",
        str(jobs),
        "--json",
    ]
    if item.get("head_sha"):
        command.extend(["--expected-head-sha", str(item["head_sha"])])
    if item.get("base_ref"):
        command.extend(["--repo-path", repo_path])
        command.extend(["--base-ref", str(item["base_ref"])])
    return command


def _local_cli_command(
    item: Mapping[str, Any],
    *,
    lane: str,
    output_dir: Path,
    prompt_lenses: Sequence[str] = ("base-audit",),
    output_leaf: str,
    repo_path: str = "/path/to/pr-worktree",
) -> list[str]:
    command = [
        "code-mower",
        lane,
        "--repo",
        str(item["repo"]),
        "--pr",
        str(item["pr_number"]),
        "--prompt-lenses",
        ",".join(prompt_lenses),
        "--output-dir",
        str(output_dir / output_leaf),
        "--json",
    ]
    if item.get("head_sha"):
        command.extend(["--expected-head-sha", str(item["head_sha"])])
    if item.get("base_ref"):
        command.extend(["--repo-path", repo_path])
        command.extend(["--base-ref", str(item["base_ref"])])
    return command


def _coderabbit_cli_command(
    item: Mapping[str, Any],
    *,
    output_dir: Path,
    repo_path: str = "/path/to/pr-worktree",
    base_ref: str = "origin/main",
) -> list[str]:
    base_ref_value = str(item.get("base_ref") or base_ref)
    command = [
        "code-mower",
        "coderabbit-cli",
        "--repo",
        str(item["repo"]),
        "--pr",
        str(item["pr_number"]),
        "--repo-path",
        repo_path,
        "--base-ref",
        base_ref_value,
        "--output-dir",
        str(output_dir / "coderabbit-cli"),
        "--json",
    ]
    if item.get("head_sha"):
        command.extend(["--expected-head-sha", str(item["head_sha"])])
    return command


def _local_cli_output_leaf(*, lane_slug: str, lane_id: str, reviewer_id: str) -> str:
    if reviewer_id in {lane_slug, lane_id, lane_slug.replace("-", "_")}:
        return lane_slug
    return safe_slug(reviewer_id, lane_slug)


def build_pilot_plan(
    corpus: Mapping[str, Any],
    *,
    replicates: int = 1,
    output_dir: Path = Path(".code-mower/calibration"),
    jobs: int = 1,
) -> dict[str, Any]:
    if replicates <= 0:
        raise ValueError("replicates must be greater than zero")
    if jobs <= 0:
        raise ValueError("jobs must be greater than zero")

    arms = default_arms()
    runs: list[dict[str, Any]] = []
    for item in corpus["corpus"]:
        for replicate in range(1, replicates + 1):
            for arm in arms:
                arm_id = str(arm["arm_id"])
                run_dir = _run_output_dir(output_dir, item, arm_id, replicate)
                run: dict[str, Any] = {
                    "run_id": _run_id(item, arm_id=arm_id, replicate=replicate),
                    "repo": item["repo"],
                    "pr_number": item["pr_number"],
                    "head_sha": item.get("head_sha", ""),
                    "base_ref": item.get("base_ref", ""),
                    "review_class": item.get("review_class", "general"),
                    "context_packs": item.get("context_packs", []),
                    "replicate": replicate,
                    "arm_id": arm_id,
                    "kind": arm["kind"],
                    "reviewers": arm["reviewers"],
                    "output_dir": str(run_dir),
                    "blind_review_required": True,
                    "requires_explicit_arm": bool(arm.get("requires_explicit_arm")),
                }
                local_profiles = [
                    reviewer["profile_id"]
                    for reviewer in arm["reviewers"]
                    if reviewer.get("provider") == "local-llm"
                ]
                if local_profiles:
                    commands = run.setdefault("commands", [])
                    command_metadata = run.setdefault("command_metadata", [])
                    commands.append(
                        _local_llm_command(
                            item,
                            profiles=local_profiles,
                            output_dir=run_dir,
                            jobs=min(jobs, len(local_profiles)),
                        )
                    )
                    command_metadata.append(
                        {
                            "lane_id": "local-llm",
                            "reviewer_ids": list(local_profiles),
                        }
                    )
                cli_reviewers = [
                    reviewer
                    for reviewer in arm["reviewers"]
                    if reviewer.get("provider") == "local-cli"
                ]
                if cli_reviewers:
                    commands = run.setdefault("commands", [])
                    command_metadata = run.setdefault("command_metadata", [])
                    notes = run.setdefault("notes", [])
                    for reviewer in cli_reviewers:
                        lane_id = str(reviewer["lane_id"])
                        lane_slug = lane_id.replace("_", "-")
                        reviewer_id = str(reviewer.get("reviewer_id") or lane_slug)
                        if reviewer_id == lane_id:
                            reviewer_id = lane_slug
                        lenses = [
                            str(lens)
                            for lens in reviewer.get("lenses", ["base-audit"])
                            if str(lens).strip()
                        ] or ["base-audit"]
                        if lane_id in {"antigravity_cli", "gemini_cli", "hermes_cli"}:
                            output_leaf = _local_cli_output_leaf(
                                lane_slug=lane_slug,
                                lane_id=lane_id,
                                reviewer_id=reviewer_id,
                            )
                            commands.append(
                                _local_cli_command(
                                    item,
                                    lane=lane_slug,
                                    output_dir=run_dir,
                                    prompt_lenses=lenses,
                                    output_leaf=output_leaf,
                                )
                            )
                            command_metadata.append(
                                {
                                    "lane_id": lane_slug,
                                    "reviewer_id": reviewer_id,
                                    "lenses": lenses,
                                }
                            )
                        elif lane_id == "coderabbit_cli":
                            commands.append(
                                _coderabbit_cli_command(
                                    item,
                                    output_dir=run_dir,
                                )
                            )
                            command_metadata.append(
                                {
                                    "lane_id": lane_slug,
                                    "reviewer_id": reviewer_id,
                                    "lenses": lenses,
                                }
                            )
                            run.setdefault("manual_steps", []).append(
                                (
                                    "Check out the PR head in a clean local worktree, then "
                                    "replace /path/to/pr-worktree in the coderabbit-cli command."
                                )
                            )
                        else:
                            run.setdefault("manual_steps", []).append(
                                (
                                    f"Run {reviewer_id} against {item['repo']}#{item['pr_number']} "
                                    "under the same head SHA, then store its JSON summary beside this run."
                                )
                            )
                        notes.append(
                            f"{reviewer_id} is informational calibration evidence, not merge authority."
                        )
                if not run.get("commands") and not run.get("manual_steps"):
                    run["manual_steps"] = [
                        "Invoke each listed reviewer through its structured audit lane and keep outputs hidden until every reviewer in this arm finishes."
                    ]
                if run.get("commands") and len(run.get("commands", [])) != len(
                    run.get("command_metadata", [])
                ):
                    raise ValueError(
                        f"command/metadata desync in run {run.get('run_id')}"
                    )
                runs.append(run)

    return {
        "mode": "code-mower-calibration-pilot-plan",
        "corpus_name": corpus.get("name", ""),
        "description": corpus.get("description", ""),
        "replicates": replicates,
        "output_dir": str(output_dir),
        "arms": arms,
        "run_count": len(runs),
        "runs": runs,
        "guardrails": [
            "Run all reviewers on the same head SHA for a corpus item.",
            "Keep arm outputs hidden until every reviewer in that arm completes.",
            "Use this as calibration evidence, not merge authority.",
            "Adjudicate findings after collection before comparing accuracy.",
        ],
    }
