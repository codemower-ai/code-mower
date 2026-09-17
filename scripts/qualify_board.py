#!/usr/bin/env python3
"""Run the local Board qualification suites and emit allowlisted counts only.

No session files, credentials, provider responses, assertion text, or test logs
are copied into the scorecard. External execution and human timing stay unknown.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

SUITES = (
    "test_board_qualification", "test_board", "test_board_observation",
    "test_board_local_observation", "test_board_remote_observation", "test_board_service",
    "test_session_current", "test_role_eligibility",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.head_sha):
        parser.error("head must be a full commit SHA")
    results = []
    for name in SUITES:
        started = time.monotonic()
        suite = unittest.defaultTestLoader.loadTestsFromName(name)
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        status = "failed" if not result.wasSuccessful() else "partial" if result.skipped else "passed"
        results.append({
            "suite": name, "state": status, "tests_run": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped),
            "elapsed_seconds": {"value": round(time.monotonic() - started, 3),
                                "coverage": "complete", "observed": 1, "total": 1},
        })
    passed = sum(row["state"] == "passed" for row in results)
    report = {
        "schema": "code_mower.boardQualification.v1", "head_sha": args.head_sha,
        "state": ("failed" if any(row["state"] == "failed" for row in results)
                  else "passed" if passed == len(results) else "partial"),
        "coverage": {"passed_suites": passed, "total_suites": len(results)}, "suites": results,
        "cost_usd": {"value": None, "coverage": "unavailable", "observed": 0, "total": None},
        "hosted_execution": {"state": "not_run", "coverage": "unavailable"},
        "human_discovery_seconds": {"value": None, "coverage": "unavailable"},
        "productivity_claim": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"state": report["state"], "coverage": report["coverage"]}))
    return int(any(row["state"] == "failed" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
