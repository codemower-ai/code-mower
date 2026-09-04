"""Green-gate auto-merge robustness across GitHub merge-state transitions (issue #666).

The gate publishes ``code-mower/gate=success`` and then runs one head-pinned
``gh pr merge --auto --squash --match-head-commit`` path that handles both
queue-later (checks still pending) and merge-now (PR already mergeable)
states. Transient merge-state failures get bounded backoff; authorization,
policy, head-mismatch, and permanent validation failures do not retry.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "templates/workflows/code-mower-gate.yml.j2"
MIRROR = ROOT / "src/code_mower/templates/workflows/code-mower-gate.yml.j2"
CHECKED_IN = ROOT / ".github/workflows/code-mower-gate.yml"

BEGIN = "# CODE_MOWER_GATE_AUTOMERGE_BEGIN"
END = "# CODE_MOWER_GATE_AUTOMERGE_END"

HEAD_SHA = "abc123def456abc123def456abc123def456abc1"

FAKE_GH = textwrap.dedent("""\
    #!/bin/bash
    echo "${GH_TOKEN:-} :: $*" >> "${FAKE_GH_LOG}"
    count="$(cat "${FAKE_GH_COUNT}" 2>/dev/null || echo 0)"
    count="$((count + 1))"
    echo "${count}" > "${FAKE_GH_COUNT}"
    case "${FAKE_GH_SCENARIO}" in
      success-queue)
        echo "Pull request #466 will be automatically merged when all requirements are met"
        exit 0
        ;;
      success-now)
        echo "Merged pull request #466 (squashed commit ...)"
        exit 0
        ;;
      unstable-then-success)
        if [ "${count}" -lt 3 ]; then
          echo "GraphQL: Pull request is in unstable status (enablePullRequestAutoMerge)" >&2
          exit 1
        fi
        echo "Merged pull request #466 (squashed commit ...)"
        exit 0
        ;;
      always-unstable)
        echo "GraphQL: Pull request is in unstable status (enablePullRequestAutoMerge)" >&2
        exit 1
        ;;
      head-mismatch)
        echo "GraphQL: Head sha deadbee does not match expected head sha ${HEAD_SHA}" >&2
        exit 1
        ;;
      forbidden)
        echo "GraphQL: Resource not accessible by integration (HTTP 403)" >&2
        exit 1
        ;;
    esac
    echo "unknown scenario: ${FAKE_GH_SCENARIO}" >&2
    exit 2
    """)


def _automerge_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(BEGIN)
    end = text.index(END)
    assert start < end
    return text[start:end]


def _run_block(scenario: str, *, max_attempts: str = "5") -> tuple[int, str, list[str]]:
    """Run the extracted gate block against a fake ``gh``; return exit/output/calls."""
    block = _automerge_block(CANONICAL)
    script = (
        "set -euo pipefail\n"
        'PR_NUMBER="466"\n'
        'GITHUB_REPOSITORY="owner/repo"\n'
        f'HEAD_SHA="{HEAD_SHA}"\n'
        'CODE_MOWER_GATE_AUTOMERGE_TOKEN="merge-token-123"\n'
        'GH_TOKEN="read-token"\n'
        f'CODE_MOWER_GATE_AUTOMERGE_MAX_ATTEMPTS="{max_attempts}"\n'
        'CODE_MOWER_GATE_AUTOMERGE_RETRY_DELAY="0"\n'
        f'FAKE_GH_SCENARIO="{scenario}"\n'
        'export PR_NUMBER GITHUB_REPOSITORY HEAD_SHA CODE_MOWER_GATE_AUTOMERGE_TOKEN '
        "GH_TOKEN CODE_MOWER_GATE_AUTOMERGE_MAX_ATTEMPTS "
        "CODE_MOWER_GATE_AUTOMERGE_RETRY_DELAY FAKE_GH_SCENARIO\n"
        + "\n".join(line[10:] if line.startswith(" " * 10) else line for line in block.splitlines())
        + "\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        gh_path = fake_bin / "gh"
        gh_path.write_text(FAKE_GH, encoding="utf-8")
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        log_path = Path(tmp) / "calls.log"
        count_path = Path(tmp) / "count"
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_GH_LOG"] = str(log_path)
        env["FAKE_GH_COUNT"] = str(count_path)
        env["HEAD_SHA"] = HEAD_SHA
        completed = subprocess.run(
            ["bash", "-c", script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            timeout=60,
        )
        calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        return completed.returncode, completed.stdout, calls


class GateAutomergeTemplateTests(unittest.TestCase):
    def test_canonical_mirror_and_checked_in_blocks_match(self) -> None:
        self.assertEqual(_automerge_block(CANONICAL), _automerge_block(MIRROR))
        self.assertEqual(_automerge_block(CANONICAL), _automerge_block(CHECKED_IN))

    def test_single_head_pinned_merge_command(self) -> None:
        block = _automerge_block(CANONICAL)
        self.assertIn("gh pr merge", block)
        self.assertIn("--auto", block)
        self.assertIn("--squash", block)
        self.assertIn('--match-head-commit "${HEAD_SHA}"', block)
        self.assertIn('"${PR_NUMBER}"', block)
        self.assertIn('--repo "${GITHUB_REPOSITORY}"', block)
        self.assertNotIn("enablePullRequestAutoMerge", block)
        self.assertNotIn("pr_node_id", block)

    def test_no_graphql_automerge_left_in_gate_files(self) -> None:
        for path in (CANONICAL, MIRROR, CHECKED_IN):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("enablePullRequestAutoMerge", text)
                self.assertNotIn("pr_node_id", text)

    def test_merge_uses_dedicated_token(self) -> None:
        block = _automerge_block(CANONICAL)
        self.assertIn('GH_TOKEN="${automerge_token}" gh pr merge', block)
        self.assertIn(
            'automerge_token="${CODE_MOWER_GATE_AUTOMERGE_TOKEN:-${GH_TOKEN:-}}"', block
        )


class GateAutomergeBehaviorTests(unittest.TestCase):
    def test_queue_later_single_call(self) -> None:
        code, output, calls = _run_block("success-queue")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 1, output)
        self.assertIn("--auto", calls[0])
        self.assertIn("--squash", calls[0])
        self.assertIn(f"--match-head-commit {HEAD_SHA}", calls[0])
        self.assertTrue(calls[0].startswith("merge-token-123 ::"), calls[0])
        self.assertIn("merge request accepted", output)

    def test_merge_now_single_call(self) -> None:
        code, output, calls = _run_block("success-now")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 1, output)
        self.assertIn("merge request accepted", output)

    def test_transient_unstable_retries_then_succeeds(self) -> None:
        code, output, calls = _run_block("unstable-then-success")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 3, output)
        self.assertIn("merge request accepted", output)

    def test_persistent_transient_stays_bounded_and_green(self) -> None:
        code, output, calls = _run_block("always-unstable", max_attempts="3")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 3, output)
        self.assertIn("transiently unavailable", output)

    def test_head_mismatch_does_not_retry(self) -> None:
        code, output, calls = _run_block("head-mismatch")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 1, output)
        self.assertIn("was refused", output)

    def test_permission_failure_does_not_retry(self) -> None:
        code, output, calls = _run_block("forbidden")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(calls), 1, output)
        self.assertIn("was refused", output)


if __name__ == "__main__":
    unittest.main()
