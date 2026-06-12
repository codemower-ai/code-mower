#!/usr/bin/env python3
"""Plan and report Code Mower reviewer calibration pilots."""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import (
            code_mower_context_packs,
            code_mower_telemetry,
            reviewer_metrics,
        )
    else:
        from tools import code_mower_context_packs, code_mower_telemetry, reviewer_metrics
elif __package__ == "tools":
    from tools import code_mower_context_packs, code_mower_telemetry, reviewer_metrics
else:  # pragma: no cover - exercised after package extraction.
    from . import code_mower_context_packs, code_mower_telemetry, reviewer_metrics


DEFAULT_LOCAL_LLM_PROFILES = (
    "qwen3-coder-next-lmstudio",
    "gemma4-ollama",
)
DEFAULT_CLI_LANES = ("gemini_cli", "antigravity_cli", "hermes_cli", "coderabbit_cli")
CONTEXT_PACK_CLI_LANES = {"antigravity-cli", "gemini-cli", "hermes-cli"}
SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
MATCH_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
MATCH_STOPWORDS = {
    "about",
    "after",
    "before",
    "catch",
    "caught",
    "instead",
    "issue",
    "only",
    "should",
    "specific",
    "that",
    "this",
    "until",
    "with",
}
KNOWN_EVIDENCE_DISPOSITIONS = {
    "true_positive",
    "useful",
    "false_positive",
    "noise",
    "unknown",
}
USEFUL_EVIDENCE_DISPOSITIONS = {"true_positive", "useful"}
TRUTH_EXPECTATION_UNKNOWN = "unknown"
TRUTH_EXPECTATION_KNOWN_CLEAN = "known_clean"
TRUTH_EXPECTATION_KNOWN_BLOCKED = "known_blocked"
TRUTH_EXPECTATION_ALIASES = {
    "blocked": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "bug": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "catch": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "known-blocked": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "known_blocked": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "seeded-bug": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "seeded_bug": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "clean": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "known-clean": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "known_clean": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "no-blocker": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "no_blocker": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "pass": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "unknown": TRUTH_EXPECTATION_UNKNOWN,
}
RUN_STATUS_PASS = "pass"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_AUDIT_INPUT_INSUFFICIENT = "audit_input_insufficient"
RUN_STATUS_INFRA_ERROR = "infra_error"
RUN_STATUS_UNKNOWN = "unknown"
RUN_STATUS_CATEGORY_ALIASES = {
    "pass": RUN_STATUS_PASS,
    "passed": RUN_STATUS_PASS,
    "complete": RUN_STATUS_PASS,
    "completed": RUN_STATUS_PASS,
    "done": RUN_STATUS_PASS,
    "success": RUN_STATUS_PASS,
    "succeeded": RUN_STATUS_PASS,
    "block": RUN_STATUS_BLOCKED,
    "blocked": RUN_STATUS_BLOCKED,
    "audit_input_insufficient": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "context_insufficient": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "fail": RUN_STATUS_BLOCKED,
    "error": RUN_STATUS_INFRA_ERROR,
    "failed": RUN_STATUS_INFRA_ERROR,
    "failure": RUN_STATUS_INFRA_ERROR,
    "infra_error": RUN_STATUS_INFRA_ERROR,
    "input_insufficient": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "insufficient_context": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "invalid_summary": RUN_STATUS_INFRA_ERROR,
    "launch_failed": RUN_STATUS_INFRA_ERROR,
    "missing_summary": RUN_STATUS_INFRA_ERROR,
    "rate_limit": RUN_STATUS_INFRA_ERROR,
    "rate_limited": RUN_STATUS_INFRA_ERROR,
    "setup_error": RUN_STATUS_INFRA_ERROR,
    "stale": RUN_STATUS_INFRA_ERROR,
    "timeout": RUN_STATUS_INFRA_ERROR,
    "timed_out": RUN_STATUS_INFRA_ERROR,
}
NON_BLOCKING_CODERABBIT_SEVERITIES = {
    "info",
    "informational",
    "low",
    "minor",
    "nit",
    "notice",
    "style",
    "suggestion",
}
MERGE_GATE_USEFUL_RATE = 0.60
SELECTIVE_USEFUL_RATE = 0.50
MERGE_GATE_MIN_FINDINGS = 10
MERGE_GATE_MIN_CLEAN_RUNS = 2
CALIBRATION_RUN_RESULTS_MODE = "code-mower-calibration-run-results"
CALIBRATION_RUN_RESULTS_SCHEMA = "code_mower.calibrationRunResults.v1"


def _safe_slug(value: Any, fallback: str = "item") -> str:
    text = SAFE_SLUG_RE.sub("-", str(value or "").strip()).strip("._-")
    while ".." in text:
        text = text.replace("..", ".")
    text = text.strip("._-")
    return text or fallback


def _head_slug(value: Any) -> str:
    head = str(value or "").strip()
    if not head:
        return ""
    safe_head = _safe_slug(head, "")
    digest = hashlib.sha256(head.encode("utf-8")).hexdigest()[:12]
    return f"{safe_head[:12] or digest}-{digest}"


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{field} must be an integer")


def load_corpus(path: Path) -> dict[str, Any]:
    payload = dict(_load_json(path))
    if payload.get("version", 1) not in {1, "1"}:
        raise ValueError("calibration corpus version must be 1")
    raw_items = payload.get("corpus", payload.get("pull_requests"))
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("calibration corpus must include a non-empty corpus list")

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise ValueError(f"corpus[{index}] must be a JSON object")
        repo = str(item.get("repo") or "").strip()
        if "/" not in repo:
            raise ValueError(f"corpus[{index}].repo must be an owner/repo slug")
        pr_number = _int(item.get("pr_number", item.get("pr")), field=f"corpus[{index}].pr_number")
        head_sha = str(item.get("head_sha") or item.get("head") or "").strip()
        key = (repo, pr_number, head_sha)
        if key in seen:
            raise ValueError(f"duplicate corpus PR entry: {repo}#{pr_number} {head_sha or '(head unspecified)'}")
        seen.add(key)
        source = str(item.get("source") or "known-pr")
        expected_findings = list(item.get("expected_findings", []))
        truth = _normalize_truth(item, source=source)
        expected_findings = list(truth.get("expected_findings") or expected_findings)
        items.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "base_ref": str(item.get("base_ref") or ""),
                "difficulty": str(item.get("difficulty") or "unknown"),
                "review_class": str(item.get("review_class") or "general"),
                "source": source,
                "truth": truth,
                "known_clean": truth["known_clean"],
                "known_blocked": truth["known_blocked"],
                "expected_findings": expected_findings,
                "context_packs": list(item.get("context_packs", [])),
                "reviewer_evidence": list(item.get("reviewer_evidence", [])),
                "reviewer_runs": list(item.get("reviewer_runs", [])),
                "reviewer_run_dispositions": list(
                    item.get("reviewer_run_dispositions", [])
                ),
                "notes": str(item.get("notes") or ""),
            }
        )

    return {
        "version": 1,
        "name": str(payload.get("name") or path.stem),
        "description": str(payload.get("description") or ""),
        "corpus": items,
    }


def default_arms() -> list[dict[str, Any]]:
    return [
        {
            "arm_id": "topology-baseline",
            "kind": "cross-provider",
            "reviewers": [
                {"reviewer_id": "codex-audit", "provider": "codex", "lenses": ["base-audit"]},
                {"reviewer_id": "claude-audit", "provider": "claude", "lenses": ["base-audit"]},
            ],
            "purpose": "Measure decorrelation from different model families.",
        },
        {
            "arm_id": "same-provider-lenses",
            "kind": "lens-shift",
            "reviewers": [
                {"reviewer_id": "claude-base-audit", "provider": "claude", "lenses": ["base-audit"]},
                {
                    "reviewer_id": "claude-package-runtime",
                    "provider": "claude",
                    "lenses": ["base-audit", "package-runtime"],
                },
            ],
            "purpose": "Measure whether prompt lenses create useful disagreement.",
        },
        {
            "arm_id": "same-provider-doctrine-lenses",
            "kind": "lens-shift",
            "reviewers": [
                {"reviewer_id": "claude-base-audit", "provider": "claude", "lenses": ["base-audit"]},
                {
                    "reviewer_id": "claude-generic-programming",
                    "provider": "claude",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "claude-context-driven-quality",
                    "provider": "claude",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Measure whether the same model with materially different review "
                "doctrine creates useful disagreement beyond the same-model noise floor."
            ),
        },
        {
            "arm_id": "same-provider-risk-ops-lenses",
            "kind": "lens-shift",
            "reviewers": [
                {"reviewer_id": "claude-base-audit", "provider": "claude", "lenses": ["base-audit"]},
                {
                    "reviewer_id": "claude-security-threat-model",
                    "provider": "claude",
                    "lenses": ["base-audit", "security-threat-model"],
                },
                {
                    "reviewer_id": "claude-operability",
                    "provider": "claude",
                    "lenses": ["base-audit", "operability"],
                },
            ],
            "purpose": (
                "Measure whether security and operability lenses catch useful "
                "production-risk findings beyond the base audit and same-model noise floor."
            ),
        },
        {
            "arm_id": "gemini-risk-ops-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "gemini-base-audit",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "gemini-security-threat-model",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "security-threat-model"],
                },
                {
                    "reviewer_id": "gemini-operability",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "operability"],
                },
            ],
            "purpose": (
                "Execute Gemini CLI against the same head with base, security, "
                "and operability lenses so lens evidence can be collected without "
                "manual command fan-out."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "gemini-doctrine-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "gemini-base-audit",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "gemini-generic-programming",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "gemini-context-driven-quality",
                    "provider": "local-cli",
                    "lane_id": "gemini_cli",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Execute Gemini CLI against the same head with base, generic "
                "programming, and context-driven quality lenses so same-model "
                "doctrine shifts can be measured with real lane outputs."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "hermes-doctrine-lens-fanout",
            "kind": "executable-lens-fanout",
            "reviewers": [
                {
                    "reviewer_id": "hermes-base-audit",
                    "provider": "local-cli",
                    "lane_id": "hermes_cli",
                    "lenses": ["base-audit"],
                },
                {
                    "reviewer_id": "hermes-generic-programming",
                    "provider": "local-cli",
                    "lane_id": "hermes_cli",
                    "lenses": ["base-audit", "generic-programming"],
                },
                {
                    "reviewer_id": "hermes-context-driven-quality",
                    "provider": "local-cli",
                    "lane_id": "hermes_cli",
                    "lenses": ["base-audit", "context-driven-quality"],
                },
            ],
            "purpose": (
                "Execute Hermes CLI against the same head with base, generic "
                "programming, and context-driven quality lenses so its one-shot "
                "agent profile can be compared with Gemini/Claude doctrine "
                "evidence without granting merge authority."
            ),
            "requires_explicit_arm": True,
        },
        {
            "arm_id": "same-provider-control",
            "kind": "noise-floor",
            "reviewers": [
                {"reviewer_id": "claude-base-audit-a", "provider": "claude", "lenses": ["base-audit"]},
                {"reviewer_id": "claude-base-audit-b", "provider": "claude", "lenses": ["base-audit"]},
            ],
            "purpose": "Measure repeated-run variance for the same model and same lens.",
        },
        {
            "arm_id": "local-cli-models",
            "kind": "informational-bakeoff",
            "reviewers": [
                *(
                    {
                        "reviewer_id": profile_id,
                        "provider": "local-llm",
                        "profile_id": profile_id,
                        "lenses": ["base-audit"],
                    }
                    for profile_id in DEFAULT_LOCAL_LLM_PROFILES
                ),
                *(
                    {
                        "reviewer_id": lane_id,
                        "provider": "local-cli",
                        "lane_id": lane_id,
                        "lenses": ["base-audit"],
                    }
                    for lane_id in DEFAULT_CLI_LANES
                ),
            ],
            "purpose": "Compare cheap informational reviewers before promotion.",
        },
    ]


def _run_output_dir(output_dir: Path, item: Mapping[str, Any], arm_id: str, replicate: int) -> Path:
    repo_slug = _safe_slug(str(item["repo"]).replace("/", "__"), "repo")
    head_slug = _head_slug(item.get("head_sha", ""))
    pr_dir = f"pr-{item['pr_number']}" + (f"-{head_slug}" if head_slug else "")
    return output_dir / repo_slug / pr_dir / f"r{replicate}" / arm_id


def _run_id(item: Mapping[str, Any], *, arm_id: str, replicate: int) -> str:
    base = f"{_safe_slug(str(item['repo']).replace('/', '__'))}-pr-{item['pr_number']}"
    head_slug = _head_slug(item.get("head_sha", ""))
    if head_slug:
        base = f"{base}-{head_slug}"
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


def _gemini_cli_command(
    item: Mapping[str, Any],
    *,
    output_dir: Path,
    prompt_lenses: Sequence[str] = ("base-audit",),
    output_leaf: str = "gemini-cli",
    repo_path: str = "/path/to/pr-worktree",
) -> list[str]:
    command = [
        "code-mower",
        "gemini-cli",
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


def _antigravity_cli_command(
    item: Mapping[str, Any],
    *,
    output_dir: Path,
    prompt_lenses: Sequence[str] = ("base-audit",),
    output_leaf: str = "antigravity-cli",
    repo_path: str = "/path/to/pr-worktree",
) -> list[str]:
    command = [
        "code-mower",
        "antigravity-cli",
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


def _hermes_cli_command(
    item: Mapping[str, Any],
    *,
    output_dir: Path,
    prompt_lenses: Sequence[str] = ("base-audit",),
    output_leaf: str = "hermes-cli",
    repo_path: str = "/path/to/pr-worktree",
) -> list[str]:
    command = [
        "code-mower",
        "hermes-cli",
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
                        if lane_id == "gemini_cli":
                            output_leaf = (
                                "gemini-cli"
                                if reviewer_id in {lane_slug, lane_id, "gemini_cli"}
                                else _safe_slug(reviewer_id, "gemini-cli")
                            )
                            commands.append(
                                _gemini_cli_command(
                                    item,
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
                        elif lane_id == "antigravity_cli":
                            output_leaf = (
                                "antigravity-cli"
                                if reviewer_id in {
                                    lane_slug,
                                    lane_id,
                                    "antigravity_cli",
                                }
                                else _safe_slug(reviewer_id, "antigravity-cli")
                            )
                            commands.append(
                                _antigravity_cli_command(
                                    item,
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
                        elif lane_id == "hermes_cli":
                            output_leaf = (
                                "hermes-cli"
                                if reviewer_id in {
                                    lane_slug,
                                    lane_id,
                                    "hermes_cli",
                                }
                                else _safe_slug(reviewer_id, "hermes-cli")
                            )
                            commands.append(
                                _hermes_cli_command(
                                    item,
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


def _utc_now_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_code_mower_command() -> list[str]:
    module_path = Path(__file__).resolve()
    sibling_cli = module_path.with_name("code_mower_cli.py")
    if sibling_cli.exists():
        return [sys.executable, str(sibling_cli)]
    packaged_cli = module_path.with_name("cli.py")
    if packaged_cli.exists():  # pragma: no cover - exercised after package extraction.
        return [sys.executable, "-m", "code_mower.cli"]
    return ["code-mower"]


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


def _repo_path_for_item(item: Mapping[str, Any], repo_path_map: Mapping[str, str]) -> str:
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


def _command_lane_id(command: Sequence[Any]) -> str:
    parts = [str(part) for part in command]
    if len(parts) >= 2 and parts[0] == "code-mower":
        if parts[1] == "local-llm" and len(parts) >= 3 and parts[2] == "bakeoff":
            return "local-llm"
        return parts[1].replace("_", "-")
    return _safe_slug(parts[0] if parts else "command", "command")


def _option_value(command: Sequence[str], option: str) -> str:
    for index, part in enumerate(command):
        if part == option and index + 1 < len(command):
            return command[index + 1]
        if part.startswith(f"{option}="):
            return part.split("=", 1)[1]
    return ""


def _reviewer_id_from_command(command: Sequence[Any]) -> str:
    lane_id = _command_lane_id(command)
    output_dir = _option_value([str(part) for part in command], "--output-dir")
    output_leaf = _safe_slug(Path(output_dir).name if output_dir else "", "")
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


def _command_metadata_for_run(run: Mapping[str, Any], command_index: int) -> dict[str, Any]:
    command_metadata = run.get("command_metadata", [])
    if (
        isinstance(command_metadata, list)
        and 0 <= command_index < len(command_metadata)
        and isinstance(command_metadata[command_index], Mapping)
    ):
        return dict(command_metadata[command_index])
    return {}


def _local_llm_profiles_from_command(command: Sequence[Any]) -> list[str]:
    profiles = _option_value([str(part) for part in command], "--profiles")
    return [profile.strip() for profile in profiles.split(",") if profile.strip()]


def _set_option_value(command: list[str], option: str, value: str) -> None:
    for index, part in enumerate(command):
        if part == option and index + 1 < len(command):
            command[index + 1] = value
            return
        if part.startswith(f"{option}="):
            command[index] = f"{option}={value}"
            return
    command.extend([option, value])


def _has_flag(command: Sequence[str], flag: str) -> bool:
    return any(part == flag for part in command)


def _rewrite_code_mower_command(
    command: Sequence[Any],
    *,
    code_mower_command: Sequence[str],
) -> list[str]:
    parts = [str(part) for part in command]
    if parts and parts[0] == "code-mower":
        return [*code_mower_command, *parts[1:]]
    return parts


def _materialize_command(
    command: Sequence[Any],
    *,
    item: Mapping[str, Any],
    code_mower_command: Sequence[str],
    repo_path_map: Mapping[str, str],
    allow_historical_head: bool,
) -> list[str]:
    materialized = _rewrite_code_mower_command(
        command,
        code_mower_command=code_mower_command,
    )
    lane_id = _command_lane_id(command)
    repo = str(item.get("repo") or "")
    repo_path = _repo_path_for_item(item, repo_path_map)
    historical_local_cli_lanes = {"antigravity-cli", "gemini-cli", "hermes-cli"}
    if lane_id in {"coderabbit-cli", "local-llm", *historical_local_cli_lanes}:
        existing_repo_path = _option_value(materialized, "--repo-path")
        if repo_path:
            _set_option_value(materialized, "--repo-path", repo_path)
            if lane_id in {"coderabbit-cli", "local-llm", *historical_local_cli_lanes} and allow_historical_head:
                if not _has_flag(materialized, "--allow-historical-head"):
                    materialized.append("--allow-historical-head")
                if lane_id in historical_local_cli_lanes and not _has_flag(
                    materialized, "--historical-calibration"
                ):
                    materialized.append("--historical-calibration")
        elif existing_repo_path == "/path/to/pr-worktree":
            raise ValueError(
                f"{lane_id} for {repo} needs --repo-path-map {repo}=/path/to/pr-worktree"
            )
    return materialized


def _summary_path_for_command(command: Sequence[Any]) -> Path | None:
    lane_id = _command_lane_id(command)
    output_dir = _option_value([str(part) for part in command], "--output-dir")
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


def _status_from_verdict(value: Any, *, returncode: int | None = None) -> str:
    category = _normalize_run_status_category(value)
    if category != RUN_STATUS_UNKNOWN:
        return category
    if returncode is not None and returncode != 0:
        return RUN_STATUS_INFRA_ERROR
    return RUN_STATUS_UNKNOWN


def _count_normalized_findings(findings: Any) -> int:
    if not isinstance(findings, list):
        return 0
    return sum(1 for finding in findings if isinstance(finding, Mapping))


AUDIT_INPUT_INSUFFICIENT_PATTERNS = (
    "audit input incomplete",
    "audit input is incomplete",
    "audit input was incomplete",
    "diff is incomplete",
    "diff was incomplete",
    "diff was truncated",
    "diff truncation",
    "incomplete diff",
    "incomplete review context",
    "insufficient audit input",
    "review context is incomplete",
    "truncated diff",
)


def _finding_is_audit_input_insufficient(finding: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(finding.get(key) or "").strip().lower()
        for key in ("summary", "text", "message", "body", "title", "detail")
    )
    return any(pattern in text for pattern in AUDIT_INPUT_INSUFFICIENT_PATTERNS)


def _audit_input_insufficient_result(findings: Any) -> bool:
    if not isinstance(findings, list):
        return False
    blockers = [
        finding
        for finding in findings
        if isinstance(finding, Mapping)
        and str(finding.get("severity") or "").strip().upper() in {"P0", "P1", "P2"}
    ]
    return bool(blockers) and all(
        _finding_is_audit_input_insufficient(finding) for finding in blockers
    )


def _coderabbit_blocking_findings(findings: Any) -> list[Mapping[str, Any]]:
    if not isinstance(findings, list):
        return []
    blocking: list[Mapping[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        severity = str(finding.get("severity") or "").strip().lower()
        if severity in NON_BLOCKING_CODERABBIT_SEVERITIES:
            continue
        blocking.append(finding)
    return blocking


def _known_clean_source(source: str) -> bool:
    return source.startswith("known-clean")


def _known_blocked_source(source: str) -> bool:
    return source.startswith("known-blocked") or source.startswith("seeded-bug")


def _normalize_truth_expectation(value: Any) -> str:
    expectation = str(value or "").strip().lower().replace("-", "_")
    if not expectation:
        return TRUTH_EXPECTATION_UNKNOWN
    return TRUTH_EXPECTATION_ALIASES.get(expectation, TRUTH_EXPECTATION_UNKNOWN)


def _truth_from_source(source: str) -> str:
    if _known_clean_source(source):
        return TRUTH_EXPECTATION_KNOWN_CLEAN
    if _known_blocked_source(source):
        return TRUTH_EXPECTATION_KNOWN_BLOCKED
    return TRUTH_EXPECTATION_UNKNOWN


def _normalize_truth(item: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    """Return the first-class calibration truth block for a corpus item.

    Older corpora encoded ground truth in ``source`` prefixes and per-run
    ``known_clean`` / ``known_blocked`` booleans. Keep those working, but prefer
    an explicit ``truth.expectation`` field for new corpora so value reports do
    not depend on naming conventions.
    """

    raw_truth = item.get("truth")
    truth_mapping = raw_truth if isinstance(raw_truth, Mapping) else {}
    expectation = _normalize_truth_expectation(
        truth_mapping.get("expectation")
        or truth_mapping.get("expected_outcome")
        or truth_mapping.get("outcome")
        or truth_mapping.get("status")
    )
    if expectation == TRUTH_EXPECTATION_UNKNOWN:
        if bool(item.get("known_clean")):
            expectation = TRUTH_EXPECTATION_KNOWN_CLEAN
        elif bool(item.get("known_blocked")):
            expectation = TRUTH_EXPECTATION_KNOWN_BLOCKED
        else:
            expectation = _truth_from_source(str(source if source is not None else item.get("source") or ""))
    expected_findings = list(
        truth_mapping.get("expected_findings")
        or item.get("expected_findings")
        or []
    )
    expected_themes = [
        str(theme)
        for theme in truth_mapping.get("expected_themes", []) or []
        if str(theme).strip()
    ]
    return {
        "expectation": expectation,
        "known_clean": expectation == TRUTH_EXPECTATION_KNOWN_CLEAN,
        "known_blocked": expectation == TRUTH_EXPECTATION_KNOWN_BLOCKED,
        "expected_findings": expected_findings,
        "expected_themes": expected_themes,
        "notes": str(truth_mapping.get("notes") or ""),
    }


def _truth_for_item(item: Mapping[str, Any]) -> dict[str, Any]:
    truth = item.get("truth")
    if isinstance(truth, Mapping):
        return _normalize_truth({**dict(item), "truth": truth}, source=str(item.get("source") or ""))
    return _normalize_truth(item, source=str(item.get("source") or ""))


def _finding_path(finding: Mapping[str, Any]) -> str:
    return str(finding.get("path") or finding.get("file") or finding.get("filename") or "")


def _finding_text(finding: Mapping[str, Any]) -> str:
    parts = [
        str(finding.get(key) or "").strip()
        for key in ("summary", "text", "message", "body", "title", "detail")
    ]
    return " ".join(part for part in parts if part)


def _match_tokens(value: str) -> set[str]:
    return {
        token
        for token in (match.group(0).lower() for match in MATCH_TOKEN_RE.finditer(value))
        if len(token) > 2 and token not in MATCH_STOPWORDS
    }


def _path_matches(expected_path: str, finding_path: str) -> bool:
    if not expected_path:
        return True
    if not finding_path:
        return False
    expected = expected_path.strip().lower()
    found = finding_path.strip().lower()
    return found == expected or found.endswith(f"/{expected}") or expected.endswith(f"/{found}")


def _text_matches(expected_summary: str, finding_text: str) -> bool:
    if not expected_summary:
        return True
    if not finding_text:
        return False
    expected_tokens = _match_tokens(expected_summary)
    if not expected_tokens:
        return expected_summary.lower() in finding_text.lower()
    overlap = expected_tokens & _match_tokens(finding_text)
    return len(overlap) >= min(2, len(expected_tokens))


def _expected_finding_matches(expected_findings: Any, findings: Any) -> int:
    if not isinstance(expected_findings, list) or not expected_findings:
        return 0
    if not isinstance(findings, list) or not findings:
        return 0
    matches = 0
    for expected in expected_findings:
        if not isinstance(expected, Mapping):
            continue
        expected_path = str(expected.get("path") or expected.get("file") or "")
        expected_summary = str(expected.get("summary") or expected.get("text") or "")
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            if _path_matches(expected_path, _finding_path(finding)) and _text_matches(
                expected_summary,
                _finding_text(finding),
            ):
                matches += 1
                break
    return matches


def _local_llm_findings(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for finding_group in run.get("blocker_findings", []) or []:
        if not isinstance(finding_group, Mapping):
            continue
        path = str(finding_group.get("path") or "")
        for text in finding_group.get("blockers", []) or []:
            findings.append({"path": path, "text": str(text)})
    for text in run.get("pr_level_blockers", []) or []:
        findings.append({"path": "__pr__", "text": str(text)})
    for finding_group in run.get("concern_findings", []) or []:
        if not isinstance(finding_group, Mapping):
            continue
        path = str(finding_group.get("path") or "")
        for text in finding_group.get("concerns", []) or []:
            findings.append({"path": path, "text": str(text)})
    return findings


def _observed_head_field(observed_head_sha: str, head_sha: str) -> dict[str, str]:
    if observed_head_sha and observed_head_sha != head_sha:
        return {"observed_head_sha": observed_head_sha}
    return {}


def _run_records_from_summary(
    *,
    summary: Mapping[str, Any],
    item: Mapping[str, Any],
    command_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    artifact = str(command_result.get("summary_path") or "")
    returncode = command_result.get("returncode")
    try:
        returncode_int = int(returncode) if returncode is not None else None
    except (TypeError, ValueError):
        returncode_int = None
    mode = str(summary.get("mode") or "")
    repo = str(summary.get("repo") or item.get("repo") or "")
    pr_number = int(summary.get("pr_number") or item.get("pr_number") or 0)
    head_sha = str(item.get("head_sha") or "")
    observed_head_sha = str(summary.get("head_sha") or "")
    truth = _truth_for_item(item)
    known_clean = bool(truth.get("known_clean"))
    known_blocked = bool(truth.get("known_blocked"))
    expected_findings = item.get("expected_findings", [])
    calibration_run_id = str(command_result.get("run_id") or "")
    replicate = command_result.get("replicate")

    def expected_matches(status: str, finding_count: int, findings: Any = None) -> int:
        if not known_blocked or status != RUN_STATUS_BLOCKED or finding_count <= 0:
            return 0
        if expected_findings:
            return _expected_finding_matches(expected_findings, findings)
        return 1

    if mode in {"antigravity-cli-audit", "gemini-cli-audit", "hermes-cli-audit"}:
        default_reviewer = (
            "antigravity-cli"
            if mode == "antigravity-cli-audit"
            else "hermes-cli"
            if mode == "hermes-cli-audit"
            else "gemini-cli"
        )
        reviewer_id = str(command_result.get("reviewer_id") or default_reviewer)
        verdict = summary.get("verdict", {})
        if not isinstance(verdict, Mapping):
            verdict = {}
        findings = verdict.get("findings", [])
        parse_failed = bool(verdict.get("parse_failed"))
        parse_status = "parse_failed" if parse_failed else "json"
        result_category = _normalize_run_status_category(verdict.get("result_category"))
        audit_input_insufficient = (
            result_category == RUN_STATUS_AUDIT_INPUT_INSUFFICIENT
            or _audit_input_insufficient_result(findings)
        )
        if parse_failed:
            status = RUN_STATUS_INFRA_ERROR
        elif audit_input_insufficient:
            status = RUN_STATUS_AUDIT_INPUT_INSUFFICIENT
        else:
            status = _status_from_verdict(verdict.get("verdict"), returncode=returncode_int)
        finding_count = _count_normalized_findings(findings)
        return [
            {
                "reviewer": reviewer_id,
                "status": status,
                "finding_count": finding_count,
                "expected_finding_matches": expected_matches(status, finding_count, findings),
                "result_category": status if audit_input_insufficient else "",
                "audit_input_insufficient_count": 1 if audit_input_insufficient else 0,
                "known_clean": known_clean,
                "known_blocked": known_blocked,
                "duration_seconds": summary.get("duration_seconds"),
                "parse_status": parse_status,
                "artifact": artifact,
                "model": summary.get("model") or "",
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                **_observed_head_field(observed_head_sha, head_sha),
                "calibration_run_id": calibration_run_id,
                "replicate": replicate,
            }
    ]
    if mode == "coderabbit-cli-audit":
        reviewer_id = str(command_result.get("reviewer_id") or "coderabbit-cli")
        try:
            raw_finding_count = int(summary.get("finding_count") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid coderabbit finding_count") from exc
        findings = summary.get("findings", [])
        if isinstance(findings, list) and findings:
            parsed_finding_count = _count_normalized_findings(findings)
            blocking_findings = _coderabbit_blocking_findings(findings)
            if raw_finding_count > parsed_finding_count:
                finding_count = raw_finding_count
            else:
                finding_count = len(blocking_findings)
        else:
            parsed_finding_count = 0
            blocking_findings = []
            finding_count = raw_finding_count
        parse_status = str(summary.get("parse_status") or "").strip().lower()
        head_check = summary.get("head_check", {})
        head_check_status = (
            str(head_check.get("status") or "")
            if isinstance(head_check, Mapping)
            else ""
        )
        if parse_status in {"raw", "parse_failed"}:
            status = RUN_STATUS_INFRA_ERROR
        elif head_check_status and head_check_status != "pass":
            status = RUN_STATUS_INFRA_ERROR
        elif returncode_int not in {None, 0} and not finding_count:
            status = RUN_STATUS_INFRA_ERROR
        else:
            status = RUN_STATUS_BLOCKED if finding_count else RUN_STATUS_PASS
        record = {
                "reviewer": reviewer_id,
                "status": status,
                "finding_count": finding_count,
                "expected_finding_matches": expected_matches(
                    status,
                    finding_count,
                    blocking_findings,
                ),
                "known_clean": known_clean,
                "known_blocked": known_blocked,
                "duration_seconds": summary.get("duration_seconds"),
                "parse_status": parse_status,
                "artifact": artifact,
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                **_observed_head_field(observed_head_sha, head_sha),
                "calibration_run_id": calibration_run_id,
                "replicate": replicate,
            }
        if raw_finding_count != finding_count:
            record["raw_finding_count"] = raw_finding_count
        if parsed_finding_count != raw_finding_count:
            record["parsed_finding_count"] = parsed_finding_count
        if raw_finding_count > parsed_finding_count:
            record["unparsed_finding_count"] = raw_finding_count - parsed_finding_count
        non_blocking_finding_count = parsed_finding_count - len(blocking_findings)
        if non_blocking_finding_count:
            record["non_blocking_finding_count"] = non_blocking_finding_count
        return [record]
    if mode == "local-llm-bakeoff":
        records: list[dict[str, Any]] = []
        for run in summary.get("runs", []) or []:
            if not isinstance(run, Mapping):
                continue
            finding_count = (
                int(run.get("blocker_file_count") or 0)
                + int(run.get("concern_file_count") or 0)
                + len(run.get("pr_level_blockers", []) or [])
            )
            findings = _local_llm_findings(run)
            status = _status_from_verdict(run.get("verdict"), returncode=returncode_int)
            records.append(
                {
                    "reviewer": str(run.get("profile_id") or "local-llm"),
                    "status": status,
                    "finding_count": finding_count,
                    "expected_finding_matches": expected_matches(
                        status,
                        finding_count,
                        findings,
                    ),
                    "known_clean": known_clean,
                    "known_blocked": known_blocked,
                    "duration_seconds": run.get("duration_seconds"),
                    "parse_status": (
                        "parse_failed"
                        if int(run.get("parse_failure_count") or 0)
                        else "json"
                    ),
                    "artifact": artifact,
                    "model": str(run.get("model") or ""),
                    "repo": str(run.get("repo") or repo),
                    "pr_number": int(run.get("pr_number") or pr_number),
                    "head_sha": head_sha,
                    **_observed_head_field(
                        str(run.get("head_sha_end") or observed_head_sha),
                        head_sha,
                    ),
                    "calibration_run_id": calibration_run_id,
                    "replicate": replicate,
                }
            )
        return records
    return []


def _load_summary(path: Path | None) -> Mapping[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return _load_json(path)


def _resolve_path_for_cwd(path: Path | None, cwd: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_absolute():
        return path
    return (cwd or Path.cwd()) / path


def _text_from_timeout_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _infra_run_record(
    *,
    lane_id: str,
    item: Mapping[str, Any],
    status: str,
    duration_seconds: float,
    artifact: str,
) -> dict[str, Any]:
    reviewer = lane_id
    if reviewer == "local-llm":
        reviewer = "local-llm"
    truth = _truth_for_item(item)
    return {
        "reviewer": reviewer,
        "status": status,
        "finding_count": 0,
        "expected_finding_matches": 0,
        "known_clean": bool(truth.get("known_clean")),
        "known_blocked": bool(truth.get("known_blocked")),
        "duration_seconds": round(duration_seconds, 3),
        "parse_status": status,
        "artifact": artifact,
        "repo": str(item.get("repo") or ""),
        "pr_number": int(item.get("pr_number") or 0),
        "head_sha": str(item.get("head_sha") or ""),
        "calibration_run_id": str(item.get("calibration_run_id") or ""),
        "replicate": item.get("replicate"),
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _repo_roots_from_path_map(repo_path_map: Mapping[str, str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for selector, path_text in repo_path_map.items():
        repo = re.split(r"[#@]", str(selector), maxsplit=1)[0]
        if "/" in repo and str(path_text).strip():
            roots.setdefault(repo, Path(path_text).expanduser())
    return roots


def _changed_files_from_checkout(repo_path: Path, base_ref: str) -> list[dict[str, str]]:
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


def _selected_context_pack_manifest(
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
        "changed_files": _changed_files_from_checkout(repo_path, base_ref),
        "packs": [packs_by_id[pack_id] for pack_id in sorted(selected_ids)],
    }


def _render_materialized_context_pack_prompt_text(report: Mapping[str, Any]) -> str:
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


def _context_pack_file_for_command(
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
    repo_path_text = _repo_path_for_item(item, repo_path_map)
    if not repo_path_text:
        raise ValueError(
            "context-pack calibration needs --repo-path-map for "
            f"{item.get('repo')}#{item.get('pr_number')}"
        )
    repo_path = Path(repo_path_text).expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"context-pack repo path is not a directory: {repo_path}")
    manifest = _selected_context_pack_manifest(
        context_pack_manifest=context_pack_manifest,
        item=item,
        repo_path=repo_path,
    )
    if manifest is None:
        return None
    plan = code_mower_context_packs.build_context_pack_plan(manifest)
    output_dir = context_pack_output_dir / _safe_slug(
        str(item.get("calibration_run_id") or result_dir.parent.name),
        "run",
    ) / _safe_slug(lane_id, "lane")
    report = code_mower_context_packs.materialize_context_pack_plan(
        plan,
        repo_root=repo_path,
        output_dir=output_dir,
        require_files=require_context_pack_files,
        repo_roots=_repo_roots_from_path_map(repo_path_map),
    )
    context_text_path = result_dir / "context-pack.txt"
    context_text_path.write_text(
        _render_materialized_context_pack_prompt_text(report),
        encoding="utf-8",
    )
    return context_text_path


def _result_command_dir(results_dir: Path, run_id: str, command_index: int, lane_id: str) -> Path:
    return results_dir / _safe_slug(run_id, "run") / f"{command_index:02d}-{_safe_slug(lane_id, 'lane')}"


def run_calibration_commands(
    corpus: Mapping[str, Any],
    *,
    replicates: int = 1,
    output_dir: Path = Path(".code-mower/calibration"),
    results_dir: Path = Path(".code-mower/calibration-results"),
    lanes: Sequence[str] = (),
    arms: Sequence[str] = (),
    jobs: int = 1,
    code_mower_command: Sequence[str] | None = None,
    repo_path_map: Mapping[str, str] | None = None,
    context_pack_manifest: Mapping[str, Any] | None = None,
    context_pack_output_dir: Path = Path(".code-mower/calibration-context-packs"),
    require_context_pack_files: bool = False,
    allow_historical_head: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    timeout_seconds: int = 1800,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run selected calibration commands and persist raw output plus summaries."""

    selected_lanes = {lane.replace("_", "-") for lane in lanes if lane}
    selected_arms = {str(arm).strip() for arm in arms if str(arm).strip()}
    code_mower_command = tuple(code_mower_command or _default_code_mower_command())
    repo_path_map = repo_path_map or {}
    plan = build_pilot_plan(
        corpus,
        replicates=replicates,
        output_dir=output_dir,
        jobs=jobs,
    )
    started_at = _utc_now_iso()
    command_results: list[dict[str, Any]] = []
    reviewer_runs: list[dict[str, Any]] = []
    executed = 0
    skipped = 0
    prevalidated = 0

    for run in plan.get("runs", []) or []:
        if not isinstance(run, Mapping):
            continue
        arm_id = str(run.get("arm_id") or "")
        if selected_arms:
            if arm_id not in selected_arms:
                continue
        elif run.get("requires_explicit_arm"):
            continue
        item = {
            "repo": run.get("repo"),
            "pr_number": run.get("pr_number"),
            "head_sha": run.get("head_sha"),
            "base_ref": run.get("base_ref"),
        }
        for command in run.get("commands", []) or []:
            if not isinstance(command, list):
                continue
            lane_id = _command_lane_id(command)
            if selected_lanes and lane_id not in selected_lanes:
                continue
            if limit is not None and prevalidated >= limit:
                continue
            _materialize_command(
                command,
                item=item,
                code_mower_command=code_mower_command,
                repo_path_map=repo_path_map,
                allow_historical_head=allow_historical_head,
            )
            prevalidated += 1

    for run in plan.get("runs", []) or []:
        if not isinstance(run, Mapping):
            continue
        arm_id = str(run.get("arm_id") or "")
        if selected_arms:
            if arm_id not in selected_arms:
                continue
        elif run.get("requires_explicit_arm"):
            continue
        item = {
            "repo": run.get("repo"),
            "pr_number": run.get("pr_number"),
            "head_sha": run.get("head_sha"),
            "base_ref": run.get("base_ref"),
            "calibration_run_id": run.get("run_id"),
            "replicate": run.get("replicate"),
        }
        corpus_item = next(
            (
                candidate
                for candidate in corpus.get("corpus", []) or []
                if isinstance(candidate, Mapping)
                and candidate.get("repo") == run.get("repo")
                and candidate.get("pr_number") == run.get("pr_number")
                and candidate.get("head_sha", "") == run.get("head_sha", "")
            ),
            item,
        )
        item["context_packs"] = list(corpus_item.get("context_packs", []) or [])
        for command_index, command in enumerate(run.get("commands", []) or []):
            if not isinstance(command, list):
                continue
            lane_id = _command_lane_id(command)
            if selected_lanes and lane_id not in selected_lanes:
                skipped += 1
                continue
            if limit is not None and executed >= limit:
                skipped += 1
                continue
            command_metadata = _command_metadata_for_run(run, command_index)
            reviewer_id = str(
                command_metadata.get("reviewer_id")
                or _reviewer_id_from_command(command)
            )
            materialized = _materialize_command(
                command,
                item=item,
                code_mower_command=code_mower_command,
                repo_path_map=repo_path_map,
                allow_historical_head=allow_historical_head,
            )
            result_dir = _result_command_dir(
                results_dir,
                str(run.get("run_id") or "run"),
                command_index,
                lane_id,
            )
            result_dir.mkdir(parents=True, exist_ok=True)
            context_pack_path = _context_pack_file_for_command(
                item=item,
                lane_id=lane_id,
                result_dir=result_dir,
                repo_path_map=repo_path_map,
                context_pack_manifest=context_pack_manifest,
                context_pack_output_dir=context_pack_output_dir,
                require_context_pack_files=require_context_pack_files,
            )
            if context_pack_path is not None:
                materialized.extend(["--context-pack-file", str(context_pack_path)])
            planned_args = list(command)
            if context_pack_path is not None:
                planned_args.extend(["--context-pack-file", str(context_pack_path)])
            command_path = result_dir / "command.json"
            stdout_path = result_dir / "stdout.txt"
            stderr_path = result_dir / "stderr.txt"
            _write_json(
                command_path,
                {
                    "args": materialized,
                    "planned_args": planned_args,
                    "run_id": run.get("run_id"),
                    "lane_id": lane_id,
                    "reviewer_id": reviewer_id,
                    "command_metadata": command_metadata,
                    "context_pack_file": str(context_pack_path) if context_pack_path else "",
                    "dry_run": dry_run,
                },
            )
            started = time.monotonic()
            summary_path = _summary_path_for_command(command)
            resolved_summary_path = _resolve_path_for_cwd(summary_path, cwd)
            if not dry_run and resolved_summary_path is not None and resolved_summary_path.exists():
                resolved_summary_path.unlink()
            if dry_run:
                returncode = None
                stdout = ""
                stderr = ""
                duration_seconds = 0.0
            else:
                try:
                    completed = subprocess.run(
                        materialized,
                        capture_output=True,
                        text=True,
                        check=False,
                        cwd=str(cwd) if cwd is not None else None,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    returncode = None
                    stdout = _text_from_timeout_stream(exc.stdout)
                    stderr = _text_from_timeout_stream(exc.stderr)
                    duration_seconds = time.monotonic() - started
                    command_status = "timeout"
                except OSError as exc:
                    returncode = None
                    stdout = ""
                    stderr = f"{type(exc).__name__}: {exc}"
                    duration_seconds = time.monotonic() - started
                    command_status = "launch_failed"
                else:
                    returncode = completed.returncode
                    stdout = completed.stdout
                    stderr = completed.stderr
                    duration_seconds = time.monotonic() - started
                    command_status = "finished"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            summary_error = ""
            try:
                summary = None if dry_run else _load_summary(resolved_summary_path)
            except ValueError as exc:
                summary = None
                summary_error = str(exc)
            command_result: dict[str, Any] = {
                "run_id": run.get("run_id"),
                "repo": run.get("repo"),
                "pr_number": run.get("pr_number"),
                "head_sha": run.get("head_sha"),
                "arm_id": run.get("arm_id"),
                "replicate": run.get("replicate"),
                "command_index": command_index,
                "lane_id": lane_id,
                "reviewer_id": reviewer_id,
                "command_metadata": command_metadata,
                "status": "planned" if dry_run else command_status,
                "returncode": returncode,
                "duration_seconds": round(duration_seconds, 3),
                "command_path": str(command_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "summary_path": str(summary_path) if summary_path is not None else "",
                "summary_found": bool(summary),
            }
            if summary_error:
                command_result["summary_error"] = summary_error
            extracted: list[dict[str, Any]] = []
            if summary is not None:
                command_result["summary_mode"] = summary.get("mode")
                try:
                    extracted = _run_records_from_summary(
                        summary=summary,
                        item=corpus_item,
                        command_result=command_result,
                    )
                except (TypeError, ValueError) as exc:
                    summary_error = str(exc)
                    command_result["summary_error"] = summary_error
                command_result["extracted_reviewer_runs"] = len(extracted)
                reviewer_runs.extend(extracted)
            if not dry_run and not extracted:
                infra_status = (
                    str(command_result["status"])
                    if command_result["status"] in {"timeout", "launch_failed"}
                    else "invalid_summary"
                    if summary is not None or summary_error
                    else "failed"
                    if returncode is not None and int(returncode) != 0
                    else "missing_summary"
                )
                infra_item = {**dict(corpus_item), **item}
                infra_reviewers = (
                    _local_llm_profiles_from_command(materialized)
                    if lane_id == "local-llm"
                    else []
                ) or [reviewer_id]
                reviewer_runs.extend(
                    _infra_run_record(
                        lane_id=reviewer,
                        item=infra_item,
                        status=infra_status,
                        duration_seconds=duration_seconds,
                        artifact=str(result_dir / "result.json"),
                    )
                    for reviewer in infra_reviewers
                )
            _write_json(result_dir / "result.json", command_result)
            command_results.append(command_result)
            executed += 1

    payload = {
        "mode": CALIBRATION_RUN_RESULTS_MODE,
        "schema": CALIBRATION_RUN_RESULTS_SCHEMA,
        "run_results_id": uuid.uuid4().hex,
        "corpus_name": corpus.get("name", ""),
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "replicates": replicates,
        "output_dir": str(output_dir),
        "results_dir": str(results_dir),
        "context_pack_manifest": bool(context_pack_manifest),
        "context_pack_output_dir": str(context_pack_output_dir),
        "selected_lanes": sorted(selected_lanes),
        "selected_arms": sorted(selected_arms),
        "dry_run": dry_run,
        "command_count": len(command_results),
        "skipped_command_count": skipped,
        "commands": command_results,
        "reviewer_runs": reviewer_runs,
    }
    _write_json(results_dir / "calibration-run-results.json", payload)
    return payload


def _load_run_results(paths: Iterable[Path]) -> list[Mapping[str, Any]]:
    reports: list[Mapping[str, Any]] = []
    for path in paths:
        payload = _load_json(path)
        if payload.get("mode") != CALIBRATION_RUN_RESULTS_MODE:
            raise ValueError(f"{path} is not a Code Mower calibration run-results file")
        reports.append(payload)
    return reports


def _csv_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return {str(part).strip() for part in value if str(part).strip()}
    if value is None:
        return set()
    return {str(value).strip()} if str(value).strip() else set()


def _run_matches_disposition_rule(run: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    reviewers = _csv_values(rule.get("reviewer") or rule.get("profile_id") or rule.get("lane"))
    if reviewers:
        reviewer = str(
            run.get("reviewer")
            or run.get("profile_id")
            or run.get("lane")
            or "unknown-reviewer"
        ).strip()
        if reviewer not in reviewers:
            return False
    statuses = {
        _normalize_run_status_category(status)
        for status in _csv_values(rule.get("status") or rule.get("status_category"))
    }
    if statuses:
        run_status = _normalize_run_status_category(
            run.get("status") or run.get("verdict")
        )
        if run_status not in statuses:
            return False
    result_categories = _csv_values(rule.get("result_category"))
    if result_categories:
        if str(run.get("result_category") or "").strip() not in result_categories:
            return False
    try:
        min_findings = int(rule.get("min_finding_count") or 0)
    except (TypeError, ValueError):
        min_findings = 0
    if min_findings:
        try:
            finding_count = int(run.get("finding_count") or 0)
        except (TypeError, ValueError):
            finding_count = 0
        if finding_count < min_findings:
            return False
    return True


def _apply_run_disposition_rules(run: dict[str, Any], item: Mapping[str, Any]) -> None:
    for rule in item.get("reviewer_run_dispositions", []) or []:
        if not isinstance(rule, Mapping):
            continue
        if not _run_matches_disposition_rule(run, rule):
            continue
        disposition = _normalize_disposition(rule.get("disposition"))
        if disposition != "unknown" and not run.get("disposition"):
            run["disposition"] = disposition
        if rule.get("expected_blocker_caught") is not None:
            run["expected_blocker_caught"] = bool(rule.get("expected_blocker_caught"))
        if rule.get("notes") and not run.get("disposition_notes"):
            run["disposition_notes"] = str(rule.get("notes") or "")
        # Apply the first matching adjudication rule. Additional notes can be
        # modeled as reviewer_evidence if a corpus needs more detail.
        return


def corpus_with_run_results(
    corpus: Mapping[str, Any],
    run_results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(corpus))
    items = merged.get("corpus", [])
    if not isinstance(items, list):
        raise ValueError("calibration corpus must include a corpus list")
    item_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("repo") or ""),
            int(item.get("pr_number") or 0),
            str(item.get("head_sha") or ""),
        )
        item_by_key[key] = item

    for result_index, result_report in enumerate(run_results):
        manifest_id = str(
            result_report.get("run_results_id")
            or result_report.get("started_at")
            or result_index
        )
        for run in result_report.get("reviewer_runs", []) or []:
            if not isinstance(run, Mapping):
                continue
            key = (
                str(run.get("repo") or ""),
                int(run.get("pr_number") or 0),
                str(run.get("head_sha") or ""),
            )
            item = item_by_key.get(key)
            if item is None:
                continue
            folded_run = {
                key: value
                for key, value in run.items()
                if key not in {"repo", "pr_number", "head_sha"}
            }
            folded_run.setdefault("calibration_manifest_id", manifest_id)
            _apply_run_disposition_rules(folded_run, item)
            item.setdefault("reviewer_runs", []).append(folded_run)
    return merged


def _normalize_disposition(value: Any) -> str:
    disposition = str(value or "unknown").strip().lower().replace("-", "_")
    if disposition not in KNOWN_EVIDENCE_DISPOSITIONS:
        return "unknown"
    return disposition


def _normalize_run_status_category(value: Any) -> str:
    """Return the semantic category used for reviewer-run policy decisions."""

    status = str(value or "").strip().lower().replace("-", "_")
    return RUN_STATUS_CATEGORY_ALIASES.get(status, RUN_STATUS_UNKNOWN)


def build_reviewer_evidence_report(corpus: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize adjudicated reviewer evidence embedded in a calibration corpus."""

    profile_counts: dict[str, dict[str, int]] = {}
    profile_runs: dict[str, set[tuple[Any, ...]]] = {}
    profile_durations: dict[str, float] = {}
    profile_clean_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_blocking_false_positive_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_known_blocked_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_known_blocked_caught_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_known_blocked_missed_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_audit_input_insufficient_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_infra_error_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_run_statuses: dict[str, dict[str, int]] = {}
    profile_review_classes: dict[str, set[str]] = {}
    profile_context_packs: dict[str, set[str]] = {}
    profile_useful_review_classes: dict[str, set[str]] = {}
    profile_useful_context_packs: dict[str, set[str]] = {}
    models: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    run_dispositions: list[dict[str, Any]] = []
    run_records: list[dict[str, Any]] = []

    for item in corpus.get("corpus", []) or []:
        if not isinstance(item, Mapping):
            continue
        repo = str(item.get("repo") or "")
        pr_number = int(item.get("pr_number") or 0)
        head_sha = str(item.get("head_sha") or "")
        review_class = str(item.get("review_class") or "general")
        context_packs = [
            str(pack)
            for pack in item.get("context_packs", []) or []
            if str(pack).strip()
        ]
        truth = _truth_for_item(item)
        run_key = (repo, pr_number, head_sha)
        for index, evidence in enumerate(item.get("reviewer_evidence", []) or []):
            if not isinstance(evidence, Mapping):
                continue
            reviewer = str(
                evidence.get("reviewer")
                or evidence.get("profile_id")
                or evidence.get("lane")
                or "unknown-reviewer"
            ).strip()
            if not reviewer:
                reviewer = "unknown-reviewer"
            disposition = _normalize_disposition(evidence.get("disposition"))
            profile_review_classes.setdefault(reviewer, set()).add(review_class)
            profile_context_packs.setdefault(reviewer, set()).update(context_packs)
            if disposition in USEFUL_EVIDENCE_DISPOSITIONS:
                profile_useful_review_classes.setdefault(reviewer, set()).add(review_class)
                profile_useful_context_packs.setdefault(reviewer, set()).update(context_packs)
            profile_counts.setdefault(reviewer, {})
            profile_counts[reviewer][disposition] = profile_counts[reviewer].get(disposition, 0) + 1
            profile_runs.setdefault(reviewer, set()).add(run_key)
            if evidence.get("duration_seconds") is not None:
                try:
                    profile_durations[reviewer] = profile_durations.get(reviewer, 0.0) + float(
                        evidence.get("duration_seconds")
                    )
                except (TypeError, ValueError):
                    pass
            if evidence.get("model"):
                models[reviewer] = str(evidence.get("model"))
            findings.append(
                {
                    "profile_id": reviewer,
                    "repo": repo,
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "difficulty": str(item.get("difficulty") or "unknown"),
                    "review_class": review_class,
                    "context_packs": context_packs,
                    "source": str(item.get("source") or ""),
                    "evidence_index": index,
                    "disposition": disposition,
                    "path": str(evidence.get("path") or ""),
                    "severity": str(evidence.get("severity") or ""),
                    "text": str(evidence.get("summary") or evidence.get("text") or ""),
                }
            )
        for run_index, run in enumerate(item.get("reviewer_runs", []) or []):
            if not isinstance(run, Mapping):
                continue
            reviewer = str(
                run.get("reviewer")
                or run.get("profile_id")
                or run.get("lane")
                or "unknown-reviewer"
            ).strip()
            if not reviewer:
                reviewer = "unknown-reviewer"
            profile_review_classes.setdefault(reviewer, set()).add(review_class)
            profile_context_packs.setdefault(reviewer, set()).update(context_packs)
            run_identity_parts = [
                str(run.get("calibration_manifest_id") or "").strip(),
                str(
                    run.get("calibration_run_id")
                    or run.get("run_id")
                    or run.get("replicate")
                    or ""
                ).strip(),
            ]
            run_identity = "::".join(part for part in run_identity_parts if part)
            reviewer_run_key: tuple[Any, ...] = (
                (*run_key, run_identity) if run_identity else run_key
            )
            profile_runs.setdefault(reviewer, set()).add(reviewer_run_key)
            if run.get("duration_seconds") is not None:
                try:
                    profile_durations[reviewer] = profile_durations.get(reviewer, 0.0) + float(
                        run.get("duration_seconds")
                    )
                except (TypeError, ValueError):
                    pass
            if run.get("model"):
                models[reviewer] = str(run.get("model"))
            run_disposition = _normalize_disposition(
                run.get("disposition")
                or (
                    run.get("adjudication", {}).get("disposition")
                    if isinstance(run.get("adjudication"), Mapping)
                    else None
                )
            )
            status = str(run.get("status") or run.get("verdict") or "unknown").strip().lower()
            status_category = _normalize_run_status_category(status)
            profile_run_statuses.setdefault(reviewer, {})
            profile_run_statuses[reviewer][status] = (
                profile_run_statuses[reviewer].get(status, 0) + 1
            )
            known_clean = bool(run.get("known_clean") or truth.get("known_clean"))
            known_blocked = bool(run.get("known_blocked") or truth.get("known_blocked"))
            try:
                finding_count = int(run.get("finding_count") or 0)
            except (TypeError, ValueError):
                finding_count = 0
            try:
                expected_finding_matches = int(run.get("expected_finding_matches") or 0)
            except (TypeError, ValueError):
                expected_finding_matches = 0
            if (
                known_clean
                and finding_count == 0
                and status_category == RUN_STATUS_PASS
            ):
                profile_clean_run_keys.setdefault(reviewer, set()).add(reviewer_run_key)
            if known_clean and status_category == RUN_STATUS_BLOCKED:
                profile_blocking_false_positive_run_keys.setdefault(reviewer, set()).add(
                    reviewer_run_key
                )
            expected_blocker_caught = bool(
                run.get("expected_blocker_caught")
                or run.get("caught_expected_blocker")
                or (
                    run_disposition in USEFUL_EVIDENCE_DISPOSITIONS
                    and status_category == RUN_STATUS_BLOCKED
                )
                or expected_finding_matches > 0
            )
            evidence_disposition = run_disposition
            evidence_notes = str(
                run.get("disposition_notes")
                or (
                    run.get("adjudication", {}).get("notes")
                    if isinstance(run.get("adjudication"), Mapping)
                    else ""
                )
                or ""
            )
            if evidence_disposition == "unknown" and expected_finding_matches > 0:
                evidence_disposition = "true_positive"
                evidence_notes = evidence_notes or "Matched an expected calibration finding."
            if (
                evidence_disposition == "unknown"
                and known_clean
                and status_category == RUN_STATUS_BLOCKED
            ):
                evidence_disposition = "false_positive"
                evidence_notes = evidence_notes or "Blocked a known-clean calibration control."
            if evidence_disposition != "unknown":
                profile_counts.setdefault(reviewer, {})
                profile_counts[reviewer][evidence_disposition] = (
                    profile_counts[reviewer].get(evidence_disposition, 0) + 1
                )
                if evidence_disposition in USEFUL_EVIDENCE_DISPOSITIONS:
                    profile_useful_review_classes.setdefault(reviewer, set()).add(
                        review_class
                    )
                    profile_useful_context_packs.setdefault(reviewer, set()).update(
                        context_packs
                    )
                run_dispositions.append(
                    {
                        "profile_id": reviewer,
                        "repo": repo,
                        "pr_number": pr_number,
                        "head_sha": head_sha,
                        "difficulty": str(item.get("difficulty") or "unknown"),
                        "review_class": review_class,
                        "context_packs": context_packs,
                        "source": str(item.get("source") or ""),
                        "run_index": run_index,
                        "disposition": evidence_disposition,
                        "inferred": run_disposition == "unknown",
                        "notes": evidence_notes,
                    }
                )
            if known_blocked:
                profile_known_blocked_run_keys.setdefault(reviewer, set()).add(
                    reviewer_run_key
                )
                if expected_blocker_caught:
                    profile_known_blocked_caught_run_keys.setdefault(reviewer, set()).add(
                        reviewer_run_key
                    )
                elif status_category in {RUN_STATUS_PASS, RUN_STATUS_BLOCKED}:
                    profile_known_blocked_missed_run_keys.setdefault(reviewer, set()).add(
                        reviewer_run_key
                    )
            if status_category == RUN_STATUS_INFRA_ERROR:
                profile_infra_error_run_keys.setdefault(reviewer, set()).add(reviewer_run_key)
            if status_category == RUN_STATUS_AUDIT_INPUT_INSUFFICIENT:
                profile_audit_input_insufficient_run_keys.setdefault(
                    reviewer, set()
                ).add(reviewer_run_key)
            run_records.append(
                {
                    "profile_id": reviewer,
                    "repo": repo,
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "difficulty": str(item.get("difficulty") or "unknown"),
                    "review_class": review_class,
                    "context_packs": context_packs,
                    "source": str(item.get("source") or ""),
                    "run_index": run_index,
                    "status": status,
                    "status_category": status_category,
                    "known_clean": known_clean,
                    "known_blocked": known_blocked,
                    "finding_count": finding_count,
                    "expected_finding_matches": expected_finding_matches,
                    "expected_blocker_caught": expected_blocker_caught,
                    "disposition": evidence_disposition,
                    "duration_seconds": run.get("duration_seconds"),
                    "parse_status": str(run.get("parse_status") or ""),
                    "result_category": str(run.get("result_category") or ""),
                    "audit_input_insufficient_count": int(
                        run.get("audit_input_insufficient_count") or 0
                    ),
                    "artifact": str(run.get("artifact") or ""),
                    "calibration_run_id": run_identity,
                }
            )

    profiles = {
        reviewer: {
            "model": models.get(reviewer, ""),
            "runs": len(profile_runs.get(reviewer, set())),
            "duration_seconds_total": round(profile_durations.get(reviewer, 0.0), 3),
            "dispositions": dict(sorted(counts.items())),
            "finding_count": sum(counts.values()),
            "known_clean_pass_runs": len(profile_clean_run_keys.get(reviewer, set())),
            "blocking_false_positive_runs": len(
                profile_blocking_false_positive_run_keys.get(reviewer, set())
            ),
            "known_blocked_runs": len(profile_known_blocked_run_keys.get(reviewer, set())),
            "known_blocked_caught_runs": len(
                profile_known_blocked_caught_run_keys.get(reviewer, set())
            ),
            "known_blocked_missed_runs": len(
                profile_known_blocked_missed_run_keys.get(reviewer, set())
            ),
            "infra_error_runs": len(profile_infra_error_run_keys.get(reviewer, set())),
            "audit_input_insufficient_runs": len(
                profile_audit_input_insufficient_run_keys.get(reviewer, set())
            ),
            "run_statuses": dict(sorted(profile_run_statuses.get(reviewer, {}).items())),
            "review_classes": sorted(profile_review_classes.get(reviewer, set())),
            "context_packs": sorted(profile_context_packs.get(reviewer, set())),
            "useful_review_classes": sorted(
                profile_useful_review_classes.get(reviewer, set())
            ),
            "useful_context_packs": sorted(
                profile_useful_context_packs.get(reviewer, set())
            ),
        }
        for reviewer in sorted(set(profile_counts) | set(profile_runs))
        for counts in [profile_counts.get(reviewer, {})]
    }
    return {
        "mode": "reviewer-evidence-calibration",
        "corpus_name": corpus.get("name", ""),
        "description": corpus.get("description", ""),
        "source_item_count": len(corpus.get("corpus", []) or []),
        "evidence_count": len(findings) + len(run_dispositions),
        "finding_evidence_count": len(findings),
        "run_disposition_count": len(run_dispositions),
        "sources": [str(corpus.get("name", "calibration-corpus"))],
        "profiles": profiles,
        "findings": findings,
        "run_dispositions": run_dispositions,
        "reviewer_runs": run_records,
        "caveat": (
            "This report summarizes historical adjudicated evidence embedded in "
            "the corpus. Use it to bootstrap calibration, then confirm with fresh "
            "blind runs before promoting lanes."
        ),
    }


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def build_lane_policy_report(metrics: Mapping[str, Any]) -> dict[str, Any]:
    if metrics.get("mode") != "reviewer-metrics":
        raise ValueError("lane policy expects a reviewer-metrics report")
    profiles = metrics.get("profiles", {})
    if not isinstance(profiles, Mapping):
        raise ValueError("reviewer-metrics report profiles must be a mapping")

    policies: dict[str, dict[str, Any]] = {}
    for profile_id, stats in sorted(profiles.items()):
        if not isinstance(stats, Mapping):
            continue
        useful_rate = _float(stats.get("useful_rate"))
        useful_findings = int(stats.get("useful_findings") or 0)
        known_findings = int(stats.get("known_disposition_count") or 0)
        clean_pass_runs = int(stats.get("known_clean_pass_runs") or 0)
        false_positive_runs = int(stats.get("blocking_false_positive_runs") or 0)
        known_blocked_missed_runs = int(stats.get("known_blocked_missed_runs") or 0)
        infra_error_runs = int(stats.get("infra_error_runs") or 0)
        audit_input_insufficient_runs = int(
            stats.get("audit_input_insufficient_runs") or 0
        )
        review_classes = [
            str(item)
            for item in stats.get("review_classes", []) or []
            if str(item).strip()
        ]
        useful_review_classes = [
            str(item)
            for item in stats.get("useful_review_classes", []) or []
            if str(item).strip()
        ]
        context_packs = [
            str(item)
            for item in stats.get("context_packs", []) or []
            if str(item).strip()
        ]
        useful_context_packs = [
            str(item)
            for item in stats.get("useful_context_packs", []) or []
            if str(item).strip()
        ]
        narrow_useful_review_classes = [
            review_class
            for review_class in useful_review_classes
            if review_class not in {"", "general"}
        ]
        event_log = stats.get("event_log", {})
        observed_pr_count = (
            int(event_log.get("observed_pr_count") or 0)
            if isinstance(event_log, Mapping)
            else 0
        )
        reasons: list[str] = []
        if known_findings < MERGE_GATE_MIN_FINDINGS:
            reasons.append(
                f"needs at least {MERGE_GATE_MIN_FINDINGS} adjudicated findings"
            )
        if clean_pass_runs < MERGE_GATE_MIN_CLEAN_RUNS:
            reasons.append(
                f"needs at least {MERGE_GATE_MIN_CLEAN_RUNS} known-clean zero-blocker runs"
            )
        if false_positive_runs:
            reasons.append("has known-clean blocking false positives")
        if known_blocked_missed_runs:
            reasons.append("missed known-blocked calibration runs")
        if infra_error_runs:
            reasons.append("has infra/setup failures to stabilize before promotion")
        if audit_input_insufficient_runs:
            reasons.append("needs richer audit input/context before promotion")
        if useful_rate < SELECTIVE_USEFUL_RATE:
            reasons.append("useful-rate below selective-trigger threshold")
        if (
            known_findings >= MERGE_GATE_MIN_FINDINGS
            and clean_pass_runs >= MERGE_GATE_MIN_CLEAN_RUNS
            and false_positive_runs == 0
            and known_blocked_missed_runs == 0
            and infra_error_runs == 0
            and audit_input_insufficient_runs == 0
            and useful_rate >= MERGE_GATE_USEFUL_RATE
        ):
            classification = "merge_gate_candidate"
        elif (
            useful_findings > 0
            and useful_rate >= SELECTIVE_USEFUL_RATE
            and bool(narrow_useful_review_classes)
            and false_positive_runs == 0
            and clean_pass_runs >= MERGE_GATE_MIN_CLEAN_RUNS
            and known_blocked_missed_runs == 0
            and infra_error_runs == 0
            and audit_input_insufficient_runs == 0
        ):
            classification = "selective_trigger_candidate"
        else:
            classification = "informational"

        if (
            classification == "informational"
            and useful_findings > 0
            and useful_rate >= SELECTIVE_USEFUL_RATE
            and not narrow_useful_review_classes
        ):
            reasons.append("needs useful non-general review-class evidence for selective triggers")

        if classification == "merge_gate_candidate":
            recommended_role = "merge_gate_eligible"
            automatic_trigger = "repo_merge_bar_opt_in"
        elif classification == "selective_trigger_candidate":
            recommended_role = "selective_trigger"
            automatic_trigger = "matching_review_class_only"
        else:
            recommended_role = "informational"
            automatic_trigger = "manual_or_calibration_only"

        suggested_trigger_classes = (
            narrow_useful_review_classes
            if classification == "selective_trigger_candidate"
            else []
        )

        policies[str(profile_id)] = {
            "profile_id": str(profile_id),
            "classification": classification,
            "recommended_role": recommended_role,
            "automatic_trigger": automatic_trigger,
            "suggested_trigger_classes": suggested_trigger_classes,
            "review_classes": review_classes,
            "context_packs": context_packs,
            "useful_review_classes": useful_review_classes,
            "useful_context_packs": useful_context_packs,
            "useful_rate": useful_rate if stats.get("useful_rate") is not None else None,
            "useful_findings": useful_findings,
            "known_disposition_count": known_findings,
            "known_clean_pass_runs": clean_pass_runs,
            "blocking_false_positive_runs": false_positive_runs,
            "known_blocked_runs": int(stats.get("known_blocked_runs") or 0),
            "known_blocked_caught_runs": int(stats.get("known_blocked_caught_runs") or 0),
            "known_blocked_missed_runs": known_blocked_missed_runs,
            "infra_error_runs": infra_error_runs,
            "audit_input_insufficient_runs": audit_input_insufficient_runs,
            "observed_pr_count": observed_pr_count,
            "reasons": reasons
            or ["evidence meets current threshold heuristics; keep human review in the loop"],
        }

    return {
        "mode": "code-mower-lane-policy",
        "source_mode": metrics.get("mode"),
        "thresholds": {
            "merge_gate_useful_rate": MERGE_GATE_USEFUL_RATE,
            "selective_useful_rate": SELECTIVE_USEFUL_RATE,
            "merge_gate_min_findings": MERGE_GATE_MIN_FINDINGS,
            "merge_gate_min_clean_runs": MERGE_GATE_MIN_CLEAN_RUNS,
        },
        "policies": policies,
        "caveat": (
            "This is a policy recommendation from calibration evidence, not an "
            "automatic repository merge-rule change."
        ),
    }


def _finding_key(finding: Mapping[str, Any]) -> str:
    raw = "\n".join(
        [
            str(finding.get("repo") or ""),
            str(finding.get("pr_number") or ""),
            str(finding.get("head_sha") or ""),
            str(finding.get("severity") or ""),
            str(finding.get("path") or ""),
            " ".join(str(finding.get("text") or "").lower().split()),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_overlap_report(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    profile_findings: dict[str, set[str]] = {}
    profile_sources: dict[str, set[str]] = {}
    report_count = 0

    for report in reports:
        report_count += 1
        if report.get("mode") != "local-llm-calibration":
            raise ValueError("overlap reports currently expect local-llm-calibration inputs")
        source = ",".join(str(item) for item in report.get("sources", []) or [])
        profiles = report.get("profiles")
        if isinstance(profiles, Mapping):
            for profile_id in profiles:
                profile_id = str(profile_id)
                if not profile_id:
                    continue
                profile_findings.setdefault(profile_id, set())
                if source:
                    profile_sources.setdefault(profile_id, set()).add(source)
        for finding in report.get("findings", []) or []:
            if not isinstance(finding, Mapping):
                continue
            profile_id = str(finding.get("profile_id") or "")
            if not profile_id:
                continue
            profile_findings.setdefault(profile_id, set()).add(_finding_key(finding))
            if source:
                profile_sources.setdefault(profile_id, set()).add(source)

    pairs: list[dict[str, Any]] = []
    for left, right in combinations(sorted(profile_findings), 2):
        left_set = profile_findings[left]
        right_set = profile_findings[right]
        union = left_set | right_set
        intersection = left_set & right_set
        jaccard_similarity = len(intersection) / len(union) if union else 1.0
        pairs.append(
            {
                "left": left,
                "right": right,
                "left_findings": len(left_set),
                "right_findings": len(right_set),
                "shared_findings": len(intersection),
                "union_findings": len(union),
                "jaccard_similarity": round(jaccard_similarity, 4),
                "jaccard_distance": round(1.0 - jaccard_similarity, 4),
            }
        )

    return {
        "mode": "code-mower-calibration-overlap",
        "source_report_count": report_count,
        "profiles": {
            profile_id: {
                "finding_count": len(findings),
                "sources": sorted(profile_sources.get(profile_id, set())),
            }
            for profile_id, findings in sorted(profile_findings.items())
        },
        "pairs": pairs,
        "caveat": (
            "Exact text overlap is a conservative proxy. Human adjudication or "
            "semantic clustering is still required before making merge-gate decisions."
        ),
    }


def render_plan_text(plan: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower calibration pilot plan",
        f"Corpus: {plan.get('corpus_name', '')}",
        f"Runs: {plan.get('run_count', 0)}",
        "",
        "Arms:",
    ]
    for arm in plan.get("arms", []) or []:
        if isinstance(arm, Mapping):
            lines.append(f"- {arm.get('arm_id')}: {arm.get('kind')} - {arm.get('purpose')}")
    lines.extend(["", "First commands:"])
    shown = 0
    for run in plan.get("runs", []) or []:
        if not isinstance(run, Mapping):
            continue
        if run.get("requires_explicit_arm"):
            continue
        for command in run.get("commands", []) or []:
            if shown >= 3:
                break
            lines.append("- " + " ".join(str(part) for part in command))
            shown += 1
        if shown >= 3:
            break
    if shown == 0:
        lines.append("- none; this corpus currently needs manual structured audit invocations")
    return "\n".join(lines) + "\n"


def render_overlap_text(report: Mapping[str, Any]) -> str:
    lines = ["Code Mower calibration overlap", "", "Pairs:"]
    for pair in report.get("pairs", []) or []:
        if isinstance(pair, Mapping):
            lines.append(
                f"- {pair.get('left')} vs {pair.get('right')}: "
                f"shared={pair.get('shared_findings')} "
                f"distance={pair.get('jaccard_distance')}"
            )
    if not report.get("pairs"):
        lines.append("- none")
    lines.extend(["", f"Caveat: {report.get('caveat', '')}"])
    return "\n".join(lines) + "\n"


def render_evidence_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower reviewer evidence",
        f"Corpus: {report.get('corpus_name', '')}",
        f"Adjudicated evidence: {report.get('evidence_count', 0)}",
        f"Finding evidence: {report.get('finding_evidence_count', 0)}",
        f"Run dispositions: {report.get('run_disposition_count', 0)}",
        "",
        "Profiles:",
    ]
    profiles = report.get("profiles", {})
    if isinstance(profiles, Mapping) and profiles:
        for profile_id, stats in profiles.items():
            if not isinstance(stats, Mapping):
                continue
            dispositions = stats.get("dispositions", {})
            lines.append(
                f"- {profile_id}: runs={stats.get('runs', 0)} "
                f"findings={stats.get('finding_count', 0)} "
                f"clean_passes={stats.get('known_clean_pass_runs', 0)} "
                f"dispositions={dispositions}"
            )
    else:
        lines.append("- none")
    lines.extend(["", f"Caveat: {report.get('caveat', '')}"])
    return "\n".join(lines) + "\n"


def render_policy_text(report: Mapping[str, Any]) -> str:
    lines = ["Code Mower lane policy", "", "Policies:"]
    policies = report.get("policies", {})
    if isinstance(policies, Mapping) and policies:
        for profile_id, policy in policies.items():
            if not isinstance(policy, Mapping):
                continue
            lines.append(
                f"- {profile_id}: {policy.get('classification')} "
                f"role={policy.get('recommended_role')} "
                f"trigger={policy.get('automatic_trigger')} "
                f"useful_rate={policy.get('useful_rate')} "
                f"clean_passes={policy.get('known_clean_pass_runs', 0)}"
            )
            reasons = policy.get("reasons", [])
            if isinstance(reasons, list) and reasons:
                lines.append(f"  reasons: {'; '.join(str(reason) for reason in reasons)}")
    else:
        lines.append("- none")
    lines.extend(["", f"Caveat: {report.get('caveat', '')}"])
    return "\n".join(lines) + "\n"


def build_value_report(
    corpus: Mapping[str, Any],
    *,
    spend: Mapping[str, Any] | None = None,
    event_summaries: Iterable[Mapping[str, Any]] = (),
    run_results: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    corpus = corpus_with_run_results(corpus, run_results)
    evidence = build_reviewer_evidence_report(corpus)
    metrics = reviewer_metrics.build_reviewer_metrics(
        [evidence],
        spend=spend,
        event_summaries=event_summaries,
    )
    policy = build_lane_policy_report(metrics)
    return {
        "mode": "code-mower-reviewer-value-report",
        "corpus_name": corpus.get("name", ""),
        "description": corpus.get("description", ""),
        "source_item_count": evidence["source_item_count"],
        "evidence_count": evidence["evidence_count"],
        "finding_evidence_count": evidence.get("finding_evidence_count", 0),
        "run_disposition_count": evidence.get("run_disposition_count", 0),
        "reviewer_run_count": len(evidence.get("reviewer_runs", [])),
        "evidence": evidence,
        "metrics": metrics,
        "policy": policy,
    }


def render_value_report_text(report: Mapping[str, Any]) -> str:
    metrics = report.get("metrics", {})
    policy = report.get("policy", {})
    profiles = metrics.get("profiles", {}) if isinstance(metrics, Mapping) else {}
    policies = policy.get("policies", {}) if isinstance(policy, Mapping) else {}
    lines = [
        "# Code Mower Reviewer Value Report",
        "",
        f"Corpus: `{report.get('corpus_name', '')}`",
        f"Items: {report.get('source_item_count', 0)}",
        f"Adjudicated evidence: {report.get('evidence_count', 0)}",
        f"Finding evidence: {report.get('finding_evidence_count', 0)}",
        f"Run dispositions: {report.get('run_disposition_count', 0)}",
        f"Reviewer runs: {report.get('reviewer_run_count', 0)}",
        "",
        "| Reviewer | Runs | Useful | Negative | Useful rate | Known-clean pass | Known-blocked caught/missed | Infra errors | Input gaps | Cost | Sec/run | Cost/useful | Policy | Recommended role |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    if isinstance(profiles, Mapping) and profiles:
        for profile_id, stats in sorted(profiles.items()):
            if not isinstance(stats, Mapping):
                continue
            profile_policy = policies.get(profile_id, {}) if isinstance(policies, Mapping) else {}
            if not isinstance(profile_policy, Mapping):
                profile_policy = {}
            caught_missed = (
                f"{stats.get('known_blocked_caught_runs', 0)}/"
                f"{stats.get('known_blocked_missed_runs', 0)}"
            )
            useful_rate = stats.get("useful_rate")
            useful_rate_text = "" if useful_rate is None else str(useful_rate)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{profile_id}`",
                        str(stats.get("runs", 0)),
                        str(stats.get("useful_findings", 0)),
                        str(stats.get("negative_findings", 0)),
                        useful_rate_text,
                        str(stats.get("known_clean_pass_runs", 0)),
                        caught_missed,
                        str(stats.get("infra_error_runs", 0)),
                        str(stats.get("audit_input_insufficient_runs", 0)),
                        str(stats.get("cost_usd", 0)),
                        (
                            ""
                            if stats.get("seconds_per_run") is None
                            else str(stats.get("seconds_per_run"))
                        ),
                        (
                            ""
                            if stats.get("cost_per_useful_finding") is None
                            else str(stats.get("cost_per_useful_finding"))
                        ),
                        f"`{profile_policy.get('classification', '')}`",
                        f"`{profile_policy.get('recommended_role', '')}`",
                    ]
                )
                + " |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 |  | 0 | 0/0 | 0 | 0 | 0 |  |  |  |  |")

    recommendations = metrics.get("recommendations", []) if isinstance(metrics, Mapping) else []
    lines.extend(["", "## Recommendations"])
    if isinstance(recommendations, list) and recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- Collect more adjudicated reviewer evidence before changing merge policy.")

    lines.extend(["", "## Policy Reasons"])
    if isinstance(policies, Mapping) and policies:
        for profile_id, profile_policy in sorted(policies.items()):
            if not isinstance(profile_policy, Mapping):
                continue
            reasons = profile_policy.get("reasons", [])
            reason_text = "; ".join(str(reason) for reason in reasons) if isinstance(reasons, list) else ""
            trigger_classes = profile_policy.get("suggested_trigger_classes", [])
            trigger_text = (
                f"; suggested classes: {', '.join(str(item) for item in trigger_classes)}"
                if isinstance(trigger_classes, list) and trigger_classes
                else ""
            )
            lines.append(
                f"- `{profile_id}`: `{profile_policy.get('classification', '')}`"
                f" / `{profile_policy.get('recommended_role', '')}`"
                f" / `{profile_policy.get('automatic_trigger', '')}`"
                + (f" - {reason_text}{trigger_text}" if reason_text or trigger_text else "")
            )
    else:
        lines.append("- No policy rows available.")
    caveat = policy.get("caveat") if isinstance(policy, Mapping) else None
    if caveat:
        lines.extend(["", f"_Caveat: {caveat}_"])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("corpus", type=Path)
    plan_parser.add_argument("--replicates", type=int, default=1)
    plan_parser.add_argument("--output-dir", type=Path, default=Path(".code-mower/calibration"))
    plan_parser.add_argument("--jobs", type=int, default=1)
    plan_parser.add_argument("--json", action="store_true")

    overlap_parser = subparsers.add_parser("overlap")
    overlap_parser.add_argument("calibration_reports", nargs="+", type=Path)
    overlap_parser.add_argument("--json", action="store_true")

    evidence_parser = subparsers.add_parser("evidence")
    evidence_parser.add_argument("corpus", type=Path)
    evidence_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("corpus", type=Path)
    run_parser.add_argument("--replicates", type=int, default=1)
    run_parser.add_argument("--output-dir", type=Path, default=Path(".code-mower/calibration"))
    run_parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(".code-mower/calibration-results"),
    )
    run_parser.add_argument(
        "--lanes",
        default="",
        help=(
            "Comma-separated command lanes to execute, e.g. "
            "antigravity-cli,gemini-cli,coderabbit-cli,local-llm."
        ),
    )
    run_parser.add_argument(
        "--arms",
        default="",
        help=(
            "Comma-separated calibration arm IDs to execute. Spend-heavy fan-out "
            "arms require explicit selection."
        ),
    )
    run_parser.add_argument("--jobs", type=int, default=1)
    run_parser.add_argument(
        "--code-mower-command",
        nargs="+",
        default=None,
        help="Command used to replace the generated `code-mower` executable.",
    )
    run_parser.add_argument(
        "--repo-path-map",
        action="append",
        default=[],
        help=(
            "Map OWNER/REPO, OWNER/REPO#PR, OWNER/REPO@HEAD, or "
            "OWNER/REPO#PR@HEAD to a clean local PR worktree for CLI reviewers."
        ),
    )
    run_parser.add_argument(
        "--context-pack-manifest",
        type=Path,
        default=None,
        help=(
            "Optional context-pack manifest. Corpus items with context_packs "
            "materialize matching packs from their local PR checkout and pass "
            "the bounded text to supported local CLI reviewers."
        ),
    )
    run_parser.add_argument(
        "--context-pack-output-dir",
        type=Path,
        default=Path(".code-mower/calibration-context-packs"),
        help="Directory for materialized calibration context-pack artifacts.",
    )
    run_parser.add_argument(
        "--require-context-pack-files",
        action="store_true",
        help="Fail if a selected context-pack file is missing.",
    )
    run_parser.add_argument("--allow-historical-head", action="store_true")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--timeout", type=int, default=1800)
    run_parser.add_argument("--json", action="store_true")

    policy_parser = subparsers.add_parser("policy")
    policy_parser.add_argument("metrics_report", type=Path)
    policy_parser.add_argument("--json", action="store_true")

    value_parser = subparsers.add_parser("value-report")
    value_parser.add_argument("corpus", type=Path)
    value_parser.add_argument("--spend", type=Path, default=None)
    value_parser.add_argument(
        "--events",
        nargs="+",
        type=Path,
        default=[],
        help="Optional Code Mower audit event JSONL logs to fold into reviewer metrics.",
    )
    value_parser.add_argument(
        "--runs",
        nargs="+",
        type=Path,
        default=[],
        help="Optional calibration run-results JSON files to fold into reviewer metrics.",
    )
    value_parser.add_argument("--output", type=Path, default=None)
    value_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "plan":
            payload = build_pilot_plan(
                load_corpus(args.corpus),
                replicates=args.replicates,
                output_dir=args.output_dir,
                jobs=args.jobs,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_plan_text(payload), end="")
            return 0
        if args.command == "overlap":
            payload = build_overlap_report(_load_json(path) for path in args.calibration_reports)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_overlap_text(payload), end="")
            return 0
        if args.command == "evidence":
            payload = build_reviewer_evidence_report(load_corpus(args.corpus))
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_evidence_text(payload), end="")
            return 0
        if args.command == "run":
            lanes = tuple(
                lane.strip()
                for lane in str(args.lanes or "").split(",")
                if lane.strip()
            )
            arms = tuple(
                arm.strip()
                for arm in str(args.arms or "").split(",")
                if arm.strip()
            )
            payload = run_calibration_commands(
                load_corpus(args.corpus),
                replicates=args.replicates,
                output_dir=args.output_dir,
                results_dir=args.results_dir,
                lanes=lanes,
                arms=arms,
                jobs=args.jobs,
                code_mower_command=args.code_mower_command,
                repo_path_map=parse_repo_path_map(args.repo_path_map),
                context_pack_manifest=_load_json(args.context_pack_manifest)
                if args.context_pack_manifest is not None
                else None,
                context_pack_output_dir=args.context_pack_output_dir,
                require_context_pack_files=args.require_context_pack_files,
                allow_historical_head=args.allow_historical_head,
                dry_run=args.dry_run,
                limit=args.limit,
                timeout_seconds=args.timeout,
                cwd=Path.cwd(),
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    "Code Mower calibration run\n"
                    f"commands: {payload.get('command_count', 0)}\n"
                    f"reviewer runs: {len(payload.get('reviewer_runs', []) or [])}\n"
                    f"results: {payload.get('results_dir')}\n",
                    end="",
                )
            return 0
        if args.command == "policy":
            payload = build_lane_policy_report(_load_json(args.metrics_report))
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_policy_text(payload), end="")
            return 0
        if args.command == "value-report":
            event_summaries = [
                code_mower_telemetry.summarize_events(
                    code_mower_telemetry.load_jsonl_events(path)
                )
                for path in args.events
            ]
            payload = build_value_report(
                load_corpus(args.corpus),
                spend=_load_json(args.spend) if args.spend is not None else None,
                event_summaries=event_summaries,
                run_results=_load_run_results(args.runs),
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(render_value_report_text(payload), encoding="utf-8")
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_value_report_text(payload), end="")
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled calibration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
