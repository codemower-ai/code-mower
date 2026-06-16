"""Calibration corpus parsing helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .truth import normalize_truth


def load_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def parse_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{field} must be an integer")


def load_corpus(path: Path) -> dict[str, Any]:
    payload = dict(load_json_object(path))
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
        pr_number = parse_int(
            item.get("pr_number", item.get("pr")),
            field=f"corpus[{index}].pr_number",
        )
        head_sha = str(item.get("head_sha") or item.get("head") or "").strip()
        key = (repo, pr_number, head_sha)
        if key in seen:
            raise ValueError(
                f"duplicate corpus PR entry: {repo}#{pr_number} "
                f"{head_sha or '(head unspecified)'}"
            )
        seen.add(key)
        source = str(item.get("source") or "known-pr")
        expected_findings = list(item.get("expected_findings", []))
        truth = normalize_truth(item, source=source)
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
