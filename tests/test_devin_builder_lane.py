"""Local Devin CLI builder lane and self-hosted Mac runner support (#745)."""

from __future__ import annotations

import atexit
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from code_mower import builder_runs as code_mower_builder_runs
from code_mower import config as code_mower_config
from code_mower import init as code_mower_init
from code_mower import lane_status


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "src/code_mower/templates/code-mower.example.yml"


def _devin_builders_plan(config: dict | None = None):
    cfg = config or code_mower_config.load_config(CONFIG_PATH)
    return code_mower_init.render_init_plan(
        cfg,
        package_mode=True,
        package_command="code-mower",
        builders=code_mower_init._parse_builder_lanes("codex,claude,devin"),
    )


def _generate(output_dir: Path, config: dict | None = None) -> None:
    plan = _devin_builders_plan(config)
    code_mower_init.apply_init_plan(plan, output_dir)


_FAKE_GIT = """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "clone" ]; then
  mkdir -p "${@: -1}/.git/hooks"
  exit 0
fi
if [ "${1:-}" = "-C" ] && [ "${3:-}" = "config" ]; then
  printf '%s\\n' 'https://github.com/owner/repo.git'
  exit 0
fi
if [ "${1:-}" = "rev-parse" ] && [ "${2:-}" = "--git-path" ]; then
  printf '%s\\n' ".git/${3}"
  exit 0
fi
exit 0
"""

# The generated runner resolves `lane-delivery` from an explicit pin before it
# looks at PATH, so a fixture states which implementation it means instead of
# inheriting whichever code-mower happens to be installed on the machine.
#
# The pin is one executable path, never a command line: the running interpreter
# can live under a directory whose name contains a space, and any argv that came
# from splitting an environment string on whitespace would be truncated there.
# A multi-argument invocation therefore ships an executable wrapper and pins the
# wrapper path.
_LANE_DELIVERY_WRAPPER = f"""#!/usr/bin/env bash
exec {shlex.quote(sys.executable)} -m code_mower.lane_delivery "$@"
"""


def _write_lane_delivery_wrapper(directory: Path) -> Path:
    wrapper = Path(directory) / "lane-delivery"
    wrapper.write_text(_LANE_DELIVERY_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


# The space in the directory name is the regression: a pin whose path contains
# one used to be truncated at the space, which took out every fixture below.
_LANE_DELIVERY_DIR = Path(tempfile.mkdtemp(prefix="code mower lane delivery "))
atexit.register(shutil.rmtree, _LANE_DELIVERY_DIR, ignore_errors=True)
_LANE_DELIVERY_CMD = str(_write_lane_delivery_wrapper(_LANE_DELIVERY_DIR))


def _lane_delivery_env() -> dict[str, str]:
    return {
        "CODE_MOWER_LANE_DELIVERY_CMD": _LANE_DELIVERY_CMD,
        "PYTHONPATH": str(ROOT / "src"),
        "LANE_PYTHON": sys.executable,
    }


# Delivery is a validated issue/PR transition, so every functional fixture has
# to answer the before/after snapshot reads. A fake provider drops
# $HOME/lane-delivered to model "this run opened or advanced the lane's PR";
# a fixture whose provider does not drop it models a run that delivered nothing.
_DELIVERY_MARKER_NAME = "lane-delivered"
#: Every fake `gh` call is recorded, so a test can assert what was *not* done.
_GH_INVOCATION_LOG = "gh-invocations.log"
#: Present when the fixture should fail only the fresh label lookup.
_LABEL_LOOKUP_FAILS_MARKER = "labels-lookup-fails"
_HEAD_BEFORE = "a" * 40
_HEAD_AFTER = "b" * 40
# The snapshot carries the head branch from the same authenticated PR read as
# the head sha, so the fixture has to answer with both.
_HEAD_BRANCH = "devin/issue-12"
_FAKE_GH_DELIVERY_HEADER = f"""#!/usr/bin/env bash
set -euo pipefail
cmd="${{1:-}} ${{2:-}}"
args=" $* "
printf '%s\\n' "$*" >> "$HOME/{_GH_INVOCATION_LOG}"
if [ "$cmd" = "pr view" ] && [[ "$args" == *"--json labels"* ]]; then
  # The fresh label read reconciliation depends on. A lane that cannot see the
  # current labels must refuse rather than reconcile against an empty set.
  if [ -f "$HOME/{_LABEL_LOOKUP_FAILS_MARKER}" ]; then
    printf 'gh: could not resolve labels for this pull request\\n' >&2
    exit 1
  fi
  printf '%s\\n' 'builder:devin'
  exit 0
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--json number,closingIssuesReferences,headRefName,headRefOid,headRepository,labels,author"* ]]; then
  if [ -f "$HOME/{_DELIVERY_MARKER_NAME}" ]; then
    printf '%s\\n' '[{{"number":77,"headRefName":"devin/issue-12","headRefOid":"{_HEAD_AFTER}","headRepository":{{"nameWithOwner":"owner/repo"}},"labels":[{{"name":"builder:devin"}}],"author":{{"login":"devin-ai-integration[bot]"}},"closingIssuesReferences":[{{"number":12,"repository":{{"nameWithOwner":"owner/repo"}},"url":"https://github.com/owner/repo/issues/12"}}]}}]'
  else
    printf '%s\\n' "${{EXISTING_OPEN_PRS_JSON:-[]}}"
  fi
  exit 0
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json labels"* ]]; then
  printf '%s\\n' '["tier:R","builder:devin","dispatched:devin"]'
  exit 0
elif [ "$cmd" = "pr view" ] && [[ "$args" == *"--json headRefName,headRefOid,state,labels"* ]]; then
  if [ -f "$HOME/{_DELIVERY_MARKER_NAME}" ]; then
    printf '%s\\n' '{{"headRefName":"{_HEAD_BRANCH}","headRefOid":"{_HEAD_AFTER}","state":"OPEN","labels":[]}}'
  else
    printf '%s\\n' '{{"headRefName":"{_HEAD_BRANCH}","headRefOid":"{_HEAD_BEFORE}","state":"OPEN","labels":[]}}'
  fi
  exit 0
elif [ "$cmd" = "issue comment" ] || [ "$cmd" = "pr comment" ]; then
  printf 'https://github.com/owner/repo/issues/12#issuecomment-1\\n'
  exit 0
fi
"""

# A fake provider that delivers: it drops the marker the fake `gh` reads back as
# a new pull request or an advanced head.
_FAKE_DEVIN_DELIVERS = f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then
  printf 'devin 3000.6.14\\n'
  exit 0
fi
: > "$HOME/{_DELIVERY_MARKER_NAME}"
printf 'fake devin completed\\n'
"""


class DevinBuilderLaneConfigGenerationTests(unittest.TestCase):
    def test_devin_builder_generates_mac_runner_support(self) -> None:
        plan = _devin_builders_plan()

        self.assertEqual(
            plan.data["builder_loop"]["builders"], ["codex", "claude", "devin"]
        )
        self.assertIn("builder:devin", plan.data["labels"])
        self.assertIn("dispatched:devin", plan.data["labels"])

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "generated"
            code_mower_init.apply_init_plan(plan, output_dir)

            for rel_path in (
                "docs/lanes/devin.md",
                "tools/lanes/run_mac_lane.sh",
                ".github/workflows/lane-mac-runner.yml",
            ):
                self.assertTrue((output_dir / rel_path).is_file(), rel_path)

            devin_doc = (output_dir / "docs/lanes/devin.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("builder:devin", devin_doc)
            self.assertIn("--export", devin_doc)
            self.assertNotIn("__BUILDER_LABEL__", devin_doc)
            self.assertNotIn("__NEEDS_OWNER_LABEL__", devin_doc)

            runner_text = (output_dir / "tools/lanes/run_mac_lane.sh").read_text(
                encoding="utf-8"
            )

        self.assertIn('case "$LANE" in codex|claude|devin)', runner_text)
        self.assertIn(
            'builder_labels_json=\'{"claude":"builder:claude","codex":"builder:codex","cursor":"builder:cursor","devin":"builder:devin"}\'',
            runner_text,
        )
        self.assertIn(
            'branch_prefixes_json=\'{"claude":["claude/"],"codex":["codex/"],"cursor":["cursor/"],"devin":["devin/"]}\'',
            runner_text,
        )
        self.assertIn("devin_command=", runner_text)
        self.assertIn("--print", runner_text)
        self.assertIn("--prompt-file", runner_text)
        self.assertIn("--respect-workspace-trust false", runner_text)
        self.assertIn("--sandbox", runner_text)
        self.assertIn('--permission-mode autonomous', runner_text)
        self.assertNotIn("--permission-mode dangerous", runner_text)
        devin_args_line = next(
            line for line in runner_text.splitlines() if line.strip().startswith("devin_args=(")
        )
        self.assertNotIn("--output-format", devin_args_line)
        self.assertNotIn("devin_args=(run ", runner_text)
        self.assertIn(
            "perform every file creation and edit through shell commands only",
            runner_text.lower(),
        )
        self.assertIn(
            "LANE_DEVIN_EXTRA_FLAGS must not include --export, --continue/-c, "
            "--resume/-r, --permission-mode, --sandbox, --prompt-file, --print, "
            "--respect-workspace-trust, or --config",
            runner_text,
        )
        self.assertIn('chmod 600 "$prompt_file"', runner_text)
        self.assertIn("code-mower builder record", runner_text)
        self.assertIn("--provider devin_cli --executor devin_cli", runner_text)

    def test_lane_delivery_pin_is_one_executable_path_containing_a_space(self) -> None:
        # Regression for the pin being word-split into argv: the wrapper below
        # lives under a directory whose name contains a space, and every
        # functional fixture in this module runs the generated runner against
        # it, so a runner that splits the pin fails those fixtures outright.
        self.assertIn(" ", _LANE_DELIVERY_CMD)
        self.assertTrue(os.access(_LANE_DELIVERY_CMD, os.X_OK))
        completed = subprocess.run(
            [_LANE_DELIVERY_CMD, "--help"],
            env={**os.environ, **_lane_delivery_env(), "PYTHONPATH": str(ROOT / "src")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("scan-prompt", completed.stdout)

    def test_repo_runner_retains_selected_execution_lanes_and_devin_source_identity(self) -> None:
        runner_text = (ROOT / "tools/lanes/run_mac_lane.sh").read_text(encoding="utf-8")
        self.assertIn('case "$LANE" in codex|claude)', runner_text)
        self.assertIn('"devin":["devin/"]', runner_text)
        self.assertIn('"builder:devin":"devin"', runner_text)



class DevinLaneDocTests(unittest.TestCase):
    def test_devin_lane_doc_states_permission_posture_and_privacy_rules(self) -> None:
        text = (ROOT / "docs/lanes/devin.md").read_text(encoding="utf-8")
        self.assertIn("builder:devin", text)
        self.assertIn("--sandbox", text)
        self.assertIn("--permission-mode autonomous", text)
        self.assertNotIn("dangerous permission mode", text)
        self.assertIn("OS sandbox", text)
        self.assertIn("shell commands only", text.lower())
        self.assertIn("--export", text)
        self.assertIn("single-writer", text.lower())
        self.assertIn("needs-owner", text)


class DevinMacLaneRunnerFunctionalTests(unittest.TestCase):
    def _generated_runner(self, output_dir: Path) -> Path:
        _generate(output_dir)
        return output_dir / "tools/lanes/run_mac_lane.sh"

    def test_devin_lane_selects_build_issue_filters_untrusted_content_and_guards_push(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            work = work_root / "devin" / "owner__repo"
            work.joinpath(".git", "hooks").mkdir(parents=True)
            prompt_log = root / "prompt.md"
            argv_log = root / "argv.log"
            mode_log = root / "mode.log"
            stdin_log = root / "stdin.log"

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Untrusted title injection","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"drive-by"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json author,comments"* ]]; then
  printf '%s\\n' '{"author":{"login":"drive-by"},"comments":[{"author":{"login":"owner"},"createdAt":"2026-01-01T00:00:00Z","body":"# Work Order: Trusted task\\n\\nImplement safe devin runner behavior."}]}'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json title,body,labels,url,author"* ]]; then
  printf '%s\\n' '{"title":"Untrusted title injection","body":"Untrusted body injection","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"drive-by"}}'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json comments"* ]]; then
  printf '%s\\n' '{"comments":[{"author":{"login":"owner"},"createdAt":"2026-01-01T00:00:00Z","body":"# Work Order: Trusted task\\n\\nImplement safe devin runner behavior."}]}'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  printf 'devin 3000.6.14\\n'
  exit 0
fi
: > "$HOME/lane-delivered"
printf '%s\\n' "$*" > "$ARGV_LOG"
cat <&0 > "$STDIN_LOG" 2>/dev/null || true
prompt_path=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--prompt-file" ]; then
    prompt_path="$2"
    break
  fi
  shift
done
python3 -c "import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777), end='')" "$prompt_path" > "$MODE_LOG"
cp "$prompt_path" "$PROMPT_LOG"
printf 'fake devin completed\\n'
""",
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "PROMPT_LOG": str(prompt_log),
                    "ARGV_LOG": str(argv_log),
                    "MODE_LOG": str(mode_log),
                    "STDIN_LOG": str(stdin_log),
                    "CODE_MOWER_DEVIN_CLI_MODEL": "swe-1.7",
                    **_lane_delivery_env(),
                },
                text=True,
                capture_output=True,
                check=True,
            )
            prompt = prompt_log.read_text(encoding="utf-8")
            argv = argv_log.read_text(encoding="utf-8")
            mode = mode_log.read_text(encoding="utf-8")
            stdin_content = stdin_log.read_text(encoding="utf-8") if stdin_log.exists() else ""

            hook = work / ".git" / "hooks" / "pre-push"
            good_push = subprocess.run(
                [str(hook)],
                cwd=work,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                input=f"refs/heads/devin/test {'a' * 40} refs/heads/devin/test {'b' * 40}\n",
                text=True,
                capture_output=True,
                check=False,
            )
            bad_push = subprocess.run(
                [str(hook)],
                cwd=work,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                input=f"refs/heads/codex/test {'a' * 40} refs/heads/codex/test {'b' * 40}\n",
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertIn("devin: selected build issue #12", completed.stdout)
        self.assertIn("fake devin completed", completed.stdout)

        # Trusted work-order filtering (frozen contract) is reused verbatim.
        self.assertIn("[omitted: issue title author is not trusted]", prompt)
        self.assertIn("[omitted: issue body author is not trusted]", prompt)
        self.assertIn("# Work Order: Trusted task", prompt)
        self.assertIn("Implement safe devin runner behavior.", prompt)
        self.assertNotIn("Untrusted title injection", prompt)
        self.assertNotIn("Untrusted body injection", prompt)

        # Devin-specific shell-only-edit guidance is frozen into the prompt.
        self.assertIn(
            "perform every file creation and edit through shell commands only",
            prompt.lower(),
        )
        self.assertIn("never call a dedicated write or edit tool", prompt.lower())

        # Issue-linked delivery is part of the frozen contract: the prompt
        # names the exact closing reference the pull request body must carry,
        # so a correctly linked pull request needs no metadata repair.
        self.assertIn("Closes #12", prompt)
        self.assertIn("closing-issue references", prompt)

        # The frozen work order goes over --prompt-file, never argv content
        # and never stdin, and never --export/--continue/--resume.
        self.assertNotIn("--export", argv)
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--resume", argv)
        self.assertNotIn("Work Order", argv)
        prompt_arg = Path(shlex.split(argv)[shlex.split(argv).index("--prompt-file") + 1])
        self.assertTrue(prompt_arg.is_relative_to((work / ".code-mower/runtime/tmp").resolve()))
        self.assertIn("--print", argv)
        self.assertIn("--prompt-file", argv)
        self.assertIn("--respect-workspace-trust false", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("--permission-mode autonomous", argv)
        self.assertNotIn("--permission-mode dangerous", argv)
        self.assertIn("--model swe-1.7", argv)
        self.assertNotIn(" run ", f" {argv} ")
        self.assertNotIn("--output-format", argv)
        self.assertEqual(stdin_content, "")

        # The prompt file is mode 0600.
        self.assertEqual(mode.strip(), oct(0o600))

        # Single-writer push guard covers the devin/ branch prefix.
        self.assertEqual(good_push.returncode, 0, good_push.stderr)
        self.assertNotEqual(bad_push.returncode, 0)
        self.assertIn("refusing devin push to branch codex/test", bad_push.stderr)

    def test_devin_lane_records_local_cli_provenance_after_pr_opens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            work = work_root / "devin" / "owner__repo"
            work.joinpath(".git", "hooks").mkdir(parents=True)
            record_log = root / "record.log"

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[{"author":{"login":"owner"},"createdAt":"2026-01-01T00:00:00Z","body":"# Work Order: Trusted task\\n\\nFix it."}]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                _FAKE_DEVIN_DELIVERS,
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            fake_code_mower = bin_dir / "code-mower"
            fake_code_mower.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$RECORD_LOG"
exit 0
""",
                encoding="utf-8",
            )
            fake_code_mower.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "RECORD_LOG": str(record_log),
                    **_lane_delivery_env(),
                },
                text=True,
                capture_output=True,
                check=True,
            )

            record_argv = record_log.read_text(encoding="utf-8") if record_log.exists() else ""

        self.assertIn("devin: selected build issue #12", completed.stdout)
        # Local Devin CLI provenance is a distinct identity from hosted Devin:
        # it records as devin_cli/devin_cli, never as the hosted devin provider.
        self.assertIn("--provider devin_cli --executor devin_cli", record_argv)
        self.assertNotIn("--provider devin --executor", record_argv)
        # The provenance record names the pull request the delivery snapshot
        # observed, so it cannot disagree with the delivery record for the same
        # run. `_FAKE_GH_DELIVERY_HEADER` opens #77 once the provider delivers.
        self.assertIn("--pr owner/repo#77", record_argv)
        self.assertIn("--status pr-opened", record_argv)

    def _rendered_reconcile_block(self, output_dir: Path) -> str:
        """The generated runner's own label-read-then-reconcile fragment."""

        runner = self._generated_runner(output_dir)
        text = runner.read_text(encoding="utf-8")
        start = text.index("      # The label set handed to reconciliation")
        end = text.index('      done <<< "$reconcile_labels"') + len(
            '      done <<< "$reconcile_labels"'
        )
        return textwrap.dedent(text[start:end])

    def test_devin_lane_refuses_to_reconcile_when_the_label_read_fails(self) -> None:
        """A failed label read is not an empty label set.

        Reconciliation is told which labels are present so it knows which to
        *remove*. Handed none, the move is purely additive: the destination
        lane's label goes on, the source lane's stays, and the pull request
        ends up carrying two builder labels while the run reports success. The
        read is required, and required before anything is published or moved.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            fragment = self._rendered_reconcile_block(output_dir)
            self.assertNotIn("|| true", fragment)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            invocations = root / "gh-invocations.log"
            (bin_dir / "gh").write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> {invocations}\n'
                'if [ "${1:-} ${2:-}" = "pr view" ] '
                '&& [[ " $* " == *"--json labels"* ]]; then\n'
                "  printf 'gh: could not resolve labels\\n' >&2\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (bin_dir / "gh").chmod(0o755)

            harness = root / "reconcile.sh"
            harness.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'LANE="devin"\n'
                'REPO="owner/repo"\n'
                'num="77"\n'
                f'reconcile_head="{_HEAD_AFTER}"\n'
                "reconcile_args=(lineage --publish --reconcile-labels)\n"
                f'lane_delivery=("{bin_dir}/lane-delivery-must-not-run")\n'
                + fragment
                + "\n"
                '"${lane_delivery[@]}" "${reconcile_args[@]}"\n',
                encoding="utf-8",
            )
            harness.chmod(0o755)

            completed = subprocess.run(
                ["bash", str(harness)],
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                text=True,
                capture_output=True,
            )
            attempted = [
                line
                for line in invocations.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("refusing to publish builder lineage", completed.stderr)
        self.assertIn("gh pr view --json labels", completed.stderr)
        # Nothing was published and no label was moved: the delivery CLI that
        # would have done both was never reached.
        self.assertNotIn("lane-delivery-must-not-run", completed.stderr)
        self.assertEqual(
            attempted,
            ["pr view 77 -R owner/repo --json labels -q .labels[].name"],
            "only the required read was attempted",
        )

    def test_devin_lane_reconciles_against_the_labels_it_read(self) -> None:
        """The ordinary path is unchanged: observed labels are passed through."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            fragment = self._rendered_reconcile_block(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "gh").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'builder:devin\\nneeds-codex-audit\\n'\n",
                encoding="utf-8",
            )
            (bin_dir / "gh").chmod(0o755)

            harness = root / "reconcile.sh"
            harness.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'LANE="devin"\n'
                'REPO="owner/repo"\n'
                'num="77"\n'
                f'reconcile_head="{_HEAD_AFTER}"\n'
                "reconcile_args=(lineage)\n"
                + fragment
                + "\n"
                'printf "%s\\n" "${reconcile_args[@]}"\n',
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["bash", str(harness)],
                env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                text=True,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.split(),
            ["lineage", "--label", "builder:devin", "--label", "needs-codex-audit"],
        )

    def test_devin_lane_full_runner_refuses_when_only_the_label_read_fails(
        self,
    ) -> None:
        """The whole generated runner, with only the labels lookup failing.

        The fragment-level case proves the guard refuses; this proves the
        *ordering* -- that the generated runner reaches the guard before it
        publishes a lineage comment or edits a label, so a failed label read
        cannot leave a pull request carrying two builder labels.
        """

        # The lane's private context store refuses to live inside a Git
        # repository, and this runtime's TMPDIR is inside this checkout. That
        # is a real product constraint, so the fixture is placed outside one
        # rather than the constraint being relaxed.
        base = Path(tempfile.gettempdir()).resolve()
        if any((parent / ".git").exists() for parent in (base, *base.parents)):
            base = Path("/tmp").resolve()
        if any((parent / ".git").exists() for parent in (base, *base.parents)):
            self.skipTest("no Git-free temporary directory is available here")
        tmp = tempfile.mkdtemp(prefix="code-mower-full-runner-", dir=str(base))
        self.addCleanup(shutil.rmtree, tmp, True)
        if True:
            root = Path(tmp).resolve()
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            work = work_root / "devin" / "owner__repo"
            work.joinpath(".git", "hooks").mkdir(parents=True)
            # Fail only the fresh label lookup; everything else answers.
            (root / _LABEL_LOOKUP_FAILS_MARKER).write_text("", encoding="utf-8")

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[{"number":77,"labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}],"updatedAt":"2026-01-01T00:00:00Z","headRepository":{"nameWithOwner":"owner/repo"},"headRefName":"devin/issue-12","author":{"login":"devin-ai-integration[bot]"}}]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "pr view" ] && [[ "$args" == *"headRepository"* ]]; then
  printf '%s\\n' '{"headRefName":"devin/issue-12","headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","headRepository":{"nameWithOwner":"owner/repo"},"labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}],"author":{"login":"devin-ai-integration[bot]"}}'
elif [ "$cmd" = "pr view" ] && [[ "$args" == *"title,body"* ]]; then
  printf '%s\\n' '{"title":"Fix round","body":"Body","headRefName":"devin/issue-12","headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","url":"https://github.com/owner/repo/pull/77","labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}],"author":{"login":"devin-ai-integration[bot]"}}'
elif [ "$cmd" = "pr view" ] && [[ "$args" == *"--json comments"* ]]; then
  printf '%s\\n' '{"comments":[{"author":{"login":"owner"},"createdAt":"2026-01-01T00:00:00Z","body":"## Codex audit (merge-authority lane)\\n\\nHead SHA: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`\\n\\nCodex Audit: BLOCKED\\n"}]}'
elif [ "$cmd" = "pr diff" ]; then
  printf 'diff --git a/x b/x\\n'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "pr edit" ]; then
  printf 'label mutation must not happen\\n' >&2
  exit 9
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[{"author":{"login":"owner"},"createdAt":"2026-01-01T00:00:00Z","body":"# Work Order: Trusted task\\n\\nFix it."}]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            (bin_dir / "git").write_text(_FAKE_GIT, encoding="utf-8")
            (bin_dir / "git").chmod(0o755)
            (bin_dir / "devin").write_text(_FAKE_DEVIN_DELIVERS, encoding="utf-8")
            (bin_dir / "devin").chmod(0o755)
            (bin_dir / "code-mower").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
            )
            (bin_dir / "code-mower").chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner), "--lane", "devin", "--repo", "owner/repo",
                    "--max-minutes", "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                text=True,
                capture_output=True,
            )
            invoked = (root / _GH_INVOCATION_LOG).read_text(encoding="utf-8")

        # The run must actually have reached the boundary, not stopped short
        # of it: a fixture that selects nothing would otherwise "prove" the
        # ordering by never exercising it.
        self.assertIn("selected fix pr #77", completed.stdout.lower(), completed.stdout)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn(
            "refusing to publish builder lineage", completed.stderr, completed.stderr
        )
        # Nothing was published and nothing was relabelled: the runner reached
        # the required read before either write.
        for forbidden in ("pr edit", "pr comment"):
            self.assertNotIn(
                f"\n{forbidden} ", f"\n{invoked}", f"the runner ran `gh {forbidden}`"
            )

    def test_devin_lane_warns_but_still_succeeds_when_builder_record_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            work = work_root / "devin" / "owner__repo"
            work.joinpath(".git", "hooks").mkdir(parents=True)

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[{"author":{"login":"owner"},"createdAt":"2026-01-01T00:00:00Z","body":"# Work Order: Trusted task\\n\\nFix it."}]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                _FAKE_DEVIN_DELIVERS,
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            fake_code_mower = bin_dir / "code-mower"
            fake_code_mower.write_text(
                """#!/usr/bin/env bash
if [ "${1:-}" = "builder" ] && [ "${2:-}" = "record" ]; then
  exit 1
fi
exit 0
""",
                encoding="utf-8",
            )
            fake_code_mower.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    **_lane_delivery_env(),
                },
                text=True,
                capture_output=True,
                check=True,
            )

        # A failing `code-mower builder record` is best-effort provenance,
        # not a lane failure: the runner must still exit 0 (check=True above
        # would already raise otherwise), but the failure must not be
        # silently swallowed either.
        self.assertIn("devin: selected build issue #12", completed.stdout)
        self.assertIn("devin: builder provenance record skipped", completed.stderr)

    def test_devin_lane_reports_nothing_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-} ${2:-}"
if [ "$cmd" = "pr list" ]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[]'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("devin: nothing to do", completed.stdout)

    def test_devin_lane_rejects_fix_round_pr_owned_by_other_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-} ${2:-}"
if [ "$cmd" = "pr view" ]; then
  printf '%s\\n' '{"headRefName":"codex/fix","headRepository":{"nameWithOwner":"owner/repo"},"labels":[{"name":"builder:codex"}]}'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                    "--target",
                    "pr:21",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("devin: selected target pr #21", completed.stdout)
        self.assertIn(
            "refusing target PR #21; head branch codex/fix is not owned by this lane",
            completed.stderr,
        )
        self.assertIn("expected branch prefix devin/", completed.stderr)
        self.assertNotIn("expected label", completed.stderr)

    def test_devin_lane_rejects_explicit_target_hosted_devin_branch_sharing_label(
        self,
    ) -> None:
        # Hosted Devin and the local Devin CLI lane share the builder:devin
        # label. The label alone must never be accepted as ownership
        # evidence for an explicit fix-round target: only this lane's own
        # branch-prefix convention proves the branch is this lane's to push
        # to.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-} ${2:-}"
if [ "$cmd" = "pr view" ]; then
  printf '%s\\n' '{"headRefName":"devin-hosted/fix-1","headRepository":{"nameWithOwner":"owner/repo"},"labels":[{"name":"builder:devin"}]}'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                    "--target",
                    "pr:21",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("devin: selected target pr #21", completed.stdout)
        self.assertIn(
            "refusing target PR #21; head branch devin-hosted/fix-1 is not owned by this lane",
            completed.stderr,
        )
        self.assertIn("expected branch prefix devin/", completed.stderr)
        self.assertNotIn("expected label", completed.stderr)

    def test_devin_lane_auto_select_skips_hosted_devin_pr_sharing_label(self) -> None:
        # Automatic fix-round selection must not pick up a same-repo,
        # audit-blocked PR just because it carries the shared builder:devin
        # label; the head branch must also match this lane's own prefix.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-} ${2:-}"
args=" $* "
if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[{"number":21,"labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}],"updatedAt":"2026-01-01T00:00:00Z","headRepository":{"nameWithOwner":"owner/repo"},"headRefName":"devin-hosted/fix-1"}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--json number,closingIssuesReferences,headRefName,headRefOid,headRepository,labels,author"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[]'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("devin: nothing to do", completed.stdout)
        self.assertNotIn("selected fix pr #21", completed.stdout)

    @staticmethod
    def _policy_config() -> dict:
        cfg = code_mower_config.load_config(CONFIG_PATH)
        cfg["repositories"][0]["slug"] = "owner/repo"
        cfg["repositories"][0]["delivery_policy"] = {"branch_template": "fix/{issue_key}-{slug}"}
        return cfg

    def _explicit_target(self, root: Path, pr_json: str, *, handoff: bool = False,
                         ) -> subprocess.CompletedProcess:
        output_dir = root / "generated"
        _generate(output_dir, self._policy_config())
        runner = output_dir / "tools/lanes/run_mac_lane.sh"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake_gh = bin_dir / "gh"
        fake_gh.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
cmd="${{1:-}} ${{2:-}}"
args=" $* "
if [ "$cmd" = "pr view" ] && [[ "$args" == *"--json headRefName,headRefOid,headRepository,labels,author"* ]]; then
  printf '%s\\n' '{pr_json}'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        argv = [str(runner), "--lane", "devin", "--repo", "owner/repo", "--max-minutes", "1",
                "--target", "pr:21"]
        if handoff:
            # Off-policy refusal must happen before this binding is opened.
            argv.extend(["--handoff-source-lane", "codex", "--handoff-expected-head", "a" * 40,
                         "--handoff-source-file", str(root / "private-binding.json")])
        return subprocess.run(
            argv,
            cwd=output_dir,
            env={**os.environ, **_lane_delivery_env(), "HOME": str(root),
                 "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                 **_lane_delivery_env()},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_devin_lane_rejects_explicit_policy_branch_targets_without_its_provenance(
        self,
    ) -> None:
        # fix/12-accessible-label satisfies the repository's delivery policy for
        # every builder and for humans, so the branch name says nothing about
        # ownership. Only this lane's builder label or authenticated author,
        # with no signal mapping to another lane, makes the PR this lane's.
        head = '"headRefName":"fix/12-accessible-label","headRefOid":"' + "a" * 40 + '"'
        cases = {
            "cross_builder_label": (
                '{' + head + ',"headRepository":{"nameWithOwner":"owner/repo"},'
                '"labels":[{"name":"builder:codex"}],"author":{"login":"chatgpt-codex-connector[bot]"}}'
            ),
            "human": (
                '{' + head + ',"headRepository":{"nameWithOwner":"owner/repo"},'
                '"labels":[{"name":"tier:R"}],"author":{"login":"owner"}}'
            ),
            "conflicting_label_and_author": (
                '{' + head + ',"headRepository":{"nameWithOwner":"owner/repo"},'
                '"labels":[{"name":"builder:devin"}],"author":{"login":"claude[bot]"}}'
            ),
            "conflicting_labels": (
                '{' + head + ',"headRepository":{"nameWithOwner":"owner/repo"},'
                '"labels":[{"name":"builder:devin"},{"name":"builder:codex"}],'
                '"author":{"login":"devin-ai-integration[bot]"}}'
            ),
        }
        for name, pr_json in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                completed = self._explicit_target(Path(tmp), pr_json)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "refusing target PR #21; head branch fix/12-accessible-label is not owned by this lane",
                    completed.stderr,
                )
                self.assertIn("expected branch prefix devin/", completed.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            fork = ('{' + head + ',"headRepository":{"nameWithOwner":"fork/repo"},'
                    '"labels":[{"name":"builder:devin"}],"author":{"login":"devin-ai-integration[bot]"}}')
            completed = self._explicit_target(Path(tmp), fork)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("head repository fork/repo does not match owner/repo", completed.stderr)

    def test_devin_lane_accepts_an_explicit_policy_branch_target_it_provably_owns(self) -> None:
        # Ownership established, the run proceeds past the ownership gate (and
        # fails later only because this fixture answers nothing else).
        head = '"headRefName":"fix/12-accessible-label","headRefOid":"' + "a" * 40 + '"'
        own = ('{' + head + ',"headRepository":{"nameWithOwner":"owner/repo"},'
               '"labels":[{"name":"builder:devin"}],"author":{"login":"devin-ai-integration[bot]"}}')
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._explicit_target(Path(tmp), own)
        self.assertNotIn("is not owned by this lane", completed.stderr)
        self.assertNotIn("does not match owner/repo", completed.stderr)

    def test_devin_lane_rejects_off_policy_target_before_explicit_handoff(self) -> None:
        # A recovery handoff transfers ownership only. It cannot waive the
        # repository's configured branch-name policy, even when its source
        # lane and pinned head otherwise describe the target PR.
        pr_json = (
            '{"headRefName":"codex/12-accessible-label","headRefOid":"' + "a" * 40
            + '","headRepository":{"nameWithOwner":"owner/repo"},'
              '"labels":[{"name":"builder:codex"}],'
              '"author":{"login":"chatgpt-codex-connector[bot]"}}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._explicit_target(Path(tmp), pr_json, handoff=True)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("head branch codex/12-accessible-label does not match the owner/repo "
                      "branch policy", completed.stderr)
        self.assertNotIn("accepted explicit handoff", completed.stdout)

    def test_devin_lane_auto_select_skips_policy_branch_prs_without_its_provenance(self) -> None:
        # The lane-label listing already carries builder:devin; a conflicting
        # author, a foreign head repository, or a branch neither lane-prefixed
        # nor policy-conforming still keeps the PR out of automatic selection.
        common = ('"labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}],'
                  '"updatedAt":"2026-01-01T00:00:00Z"')
        cases = {
            "conflicting_author": (
                '[{"number":21,' + common + ',"headRepository":{"nameWithOwner":"owner/repo"},'
                '"headRefName":"fix/12-accessible-label","author":{"login":"claude[bot]"}}]'
            ),
            "fork_head": (
                '[{"number":21,' + common + ',"headRepository":{"nameWithOwner":"fork/repo"},'
                '"headRefName":"fix/12-accessible-label","author":{"login":"devin-ai-integration[bot]"}}]'
            ),
            "off_policy_branch": (
                '[{"number":21,' + common + ',"headRepository":{"nameWithOwner":"owner/repo"},'
                '"headRefName":"hotfix/12","author":{"login":"devin-ai-integration[bot]"}}]'
            ),
        }
        for name, listing in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output_dir = root / "generated"
                _generate(output_dir, self._policy_config())
                runner = output_dir / "tools/lanes/run_mac_lane.sh"
                bin_dir = root / "bin"
                bin_dir.mkdir()
                fake_gh = bin_dir / "gh"
                fake_gh.write_text(
                    f"""#!/usr/bin/env bash
set -euo pipefail
cmd="${{1:-}} ${{2:-}}"
args=" $* "
if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '{listing}'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--json number,closingIssuesReferences,headRefName,headRefOid,headRepository,labels,author"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[]'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                    encoding="utf-8",
                )
                fake_gh.chmod(0o755)
                completed = subprocess.run(
                    [str(runner), "--lane", "devin", "--repo", "owner/repo", "--max-minutes", "1"],
                    cwd=output_dir,
                    env={**os.environ, **_lane_delivery_env(), "HOME": str(root),
                         "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertIn("devin: nothing to do", completed.stdout)
                self.assertNotIn("selected fix pr #21", completed.stdout)

    def test_devin_lane_auto_selects_and_targets_correctly_prefixed_local_branch(
        self,
    ) -> None:
        # The mirror-image case: a PR that actually belongs to the local
        # Devin CLI lane (branch prefix devin/) must still be auto-selected
        # for a fix round and accepted as an explicit target.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            work = work_root / "devin" / "owner__repo"
            work.joinpath(".git", "hooks").mkdir(parents=True)

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[{"number":21,"labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}],"updatedAt":"2026-01-01T00:00:00Z","headRepository":{"nameWithOwner":"owner/repo"},"headRefName":"devin/fix-1"}]'
elif [ "$cmd" = "pr view" ] && [[ "$args" == *"--json headRefName,headRefOid,headRepository,labels,author"* ]]; then
  printf '%s\\n' '{"headRefName":"devin/fix-1","headRefOid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","headRepository":{"nameWithOwner":"owner/repo"},"labels":[{"name":"builder:devin"},{"name":"codex-audit-blocked"}]}'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "pr view" ]; then
  printf '%s\\n' '{"title":"Fix","body":"Body","headRefName":"devin/fix-1","headRefOid":"deadbeef","url":"https://github.com/owner/repo/pull/21","labels":[{"name":"builder:devin"}],"author":{"login":"owner"}}'
elif [ "$cmd" = "api --paginate" ]; then
  printf '%s\\n' '[[]]'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                _FAKE_DEVIN_DELIVERS,
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    **_lane_delivery_env(),
                },
                text=True,
                capture_output=True,
                check=True,
            )

            hook = work / ".git" / "hooks" / "pre-push"
            good_push = subprocess.run(
                [str(hook)],
                cwd=work,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                input=f"refs/heads/devin/fix-1 {'a' * 40} refs/heads/devin/fix-1 {'b' * 40}\n",
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertIn("devin: selected fix pr #21", completed.stdout)
        self.assertIn("fake devin completed", completed.stdout)
        self.assertEqual(good_push.returncode, 0, good_push.stderr)

    def test_devin_lane_hits_cap_comments_and_reports_the_unfinished_unit(self) -> None:
        # The cap is enforced by the supervisor, so the fixture shortens the
        # supervisor's own timeout rather than emulating one with exit 124: a
        # provider is free to return 124 for its own reasons, and the runner
        # decides from the recorded supervision reason, not the raw code.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"

            short_timeout = bin_dir / "lane-delivery-short-timeout"
            short_timeout.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
forwarded=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--timeout-seconds" ]; then
    forwarded+=(--timeout-seconds 2)
    shift 2
    continue
  fi
  forwarded+=("$1")
  shift
done
exec {shlex.quote(sys.executable)} -m code_mower.lane_delivery "${{forwarded[@]}}"
""",
                encoding="utf-8",
            )
            short_timeout.chmod(0o755)

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
elif [ "$cmd" = "issue comment" ]; then
  printf 'commented\\n'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  printf 'devin 3000.6.14\\n'
  exit 0
fi
cat >/dev/null
sleep 120
""",
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "CODE_MOWER_LANE_DELIVERY_CMD": str(short_timeout),
                    "PYTHONPATH": str(ROOT / "src"),
                },
                text=True,
                capture_output=True,
                check=False,
            )

        # The cap comment still names the unit, and the capped run is reported
        # as unfinished rather than as a success: it opened no pull request,
        # advanced no head, and declared no bounded outcome.
        #
        # 3 is the undelivered code. 124 is the supervisor's own, and returning
        # it here would report the cap as a provider failure and hide the
        # classification the caller acts on.
        self.assertIn("devin: hit the 1-minute cap on issue #12", completed.stdout)
        self.assertEqual(completed.returncode, 3)
        self.assertIn("no validated delivery for issue #12", completed.stderr)

    def test_devin_lane_cap_keeps_a_delivery_the_provider_already_made(self) -> None:
        # The mirror of the test above, and the reason classification cannot key
        # on the exit code alone: the provider opened the pull request and was
        # then stopped by the cap. The work is on GitHub, so the unit is
        # finished, and reporting it as unfinished would send the next cycle
        # back to redo it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"

            short_timeout = bin_dir / "lane-delivery-short-timeout"
            short_timeout.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
forwarded=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--timeout-seconds" ]; then
    forwarded+=(--timeout-seconds 2)
    shift 2
    continue
  fi
  forwarded+=("$1")
  shift
done
exec {shlex.quote(sys.executable)} -m code_mower.lane_delivery "${{forwarded[@]}}"
""",
                encoding="utf-8",
            )
            short_timeout.chmod(0o755)

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
elif [ "$cmd" = "issue comment" ]; then
  printf 'commented\\n'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            # Opens the pull request, then hangs until the cap stops it.
            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then
  printf 'devin 3000.6.14\\n'
  exit 0
fi
: > "$HOME/{_DELIVERY_MARKER_NAME}"
printf 'fake devin opened the pr\\n'
sleep 120
""",
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "CODE_MOWER_LANE_DELIVERY_CMD": str(short_timeout),
                    "PYTHONPATH": str(ROOT / "src"),
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertIn("devin: hit the 1-minute cap on issue #12", completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("no validated delivery", completed.stderr)

    def test_devin_lane_missing_cli_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            scratch_tmp = root / "scratch-tmp"
            scratch_tmp.mkdir()

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)
            # Deliberately no fake `devin` executable, and a PATH scoped to
            # only the directories real `gh`/`git`/`jq`/`bash` live in, so a
            # `devin` CLI installed elsewhere on the developer's machine
            # cannot leak into this "missing CLI" scenario.
            minimal_path = os.pathsep.join(
                [
                    str(bin_dir),
                    "/usr/bin",
                    "/bin",
                    "/usr/local/bin",
                    "/opt/homebrew/bin",
                    "/opt/local/bin",
                ]
            )

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": minimal_path,
                    "TMPDIR": str(scratch_tmp),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            leftover = list(scratch_tmp.iterdir())

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("devin cannot act as builder: the selected runtime is unavailable", completed.stderr)
        # The prompt file (issue/PR context) must not survive a missing-CLI
        # exit: it is created and secured before the CLI is even probed.
        self.assertEqual(leftover, [])

    def test_devin_lane_rejects_transport_and_posture_override_extra_flags(self) -> None:
        for rejected_flag in (
            "--export",
            "--continue",
            "-c",
            "--resume",
            "-r",
            "--resume=abc123",
            "--permission-mode",
            "--permission-mode=dangerous",
            "--sandbox",
            "--sandbox=false",
            "--prompt-file",
            "--prompt-file=/tmp/evil.md",
            "--print",
            "--respect-workspace-trust",
            "--respect-workspace-trust=true",
            "--config",
            "--config=/tmp/evil.toml",
        ):
            with self.subTest(flag=rejected_flag), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output_dir = root / "generated"
                runner = self._generated_runner(output_dir)

                bin_dir = root / "bin"
                bin_dir.mkdir()
                work_root = root / "work"
                scratch_tmp = root / "scratch-tmp"
                scratch_tmp.mkdir()

                fake_gh = bin_dir / "gh"
                fake_gh.write_text(
                    _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                    encoding="utf-8",
                )
                fake_gh.chmod(0o755)

                fake_git = bin_dir / "git"
                fake_git.write_text(_FAKE_GIT, encoding="utf-8")
                fake_git.chmod(0o755)

                fake_devin = bin_dir / "devin"
                fake_devin.write_text(
                    """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  printf 'devin 3000.6.14\\n'
  exit 0
fi
echo "fake devin should not run" >&2
exit 1
""",
                    encoding="utf-8",
                )
                fake_devin.chmod(0o755)

                completed = subprocess.run(
                    [
                        str(runner),
                        "--lane",
                        "devin",
                        "--repo",
                        "owner/repo",
                        "--max-minutes",
                        "1",
                    ],
                    cwd=output_dir,
                    env={
                        **os.environ, **_lane_delivery_env(),
                        "HOME": str(root),
                        "LANE_WORK_ROOT": str(work_root),
                        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        "LANE_DEVIN_EXTRA_FLAGS": rejected_flag,
                        "TMPDIR": str(scratch_tmp),
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )

                leftover = list(scratch_tmp.iterdir())

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "LANE_DEVIN_EXTRA_FLAGS must not include --export, --continue/-c, "
                    "--resume/-r, --permission-mode, --sandbox, --prompt-file, --print, "
                    "--respect-workspace-trust, or --config",
                    completed.stderr,
                )
                self.assertNotIn("fake devin should not run", completed.stdout)
                self.assertNotIn("fake devin should not run", completed.stderr)
                # A prohibited extra flag aborts before the CLI ever runs,
                # but the prompt file (issue/PR context) was already
                # created and secured; it must not be left behind.
                self.assertEqual(leftover, [])

    def test_devin_lane_surfaces_auth_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            runner = self._generated_runner(output_dir)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:devin"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:devin"},{"name":"dispatched:devin"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(_FAKE_GIT, encoding="utf-8")
            fake_git.chmod(0o755)

            fake_devin = bin_dir / "devin"
            fake_devin.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  printf 'devin 3000.6.14\\n'
  exit 0
fi
cat >/dev/null
echo "not authenticated" >&2
exit 1
""",
                encoding="utf-8",
            )
            fake_devin.chmod(0o755)

            completed = subprocess.run(
                [
                    str(runner),
                    "--lane",
                    "devin",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=output_dir,
                env={
                    **os.environ, **_lane_delivery_env(),
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    **_lane_delivery_env(),
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("devin: CLI exit 1 for issue #12", completed.stdout)
        # The raw CLI auth failure reaches only the operator's own local
        # terminal (stdout here); nothing in this runner sends it anywhere.
        self.assertIn("not authenticated", completed.stdout)


class DevinBuilderProvenanceInferenceTests(unittest.TestCase):
    def test_branch_prefix_infers_local_devin_cli_builder(self) -> None:
        # A devin/ branch with no hosted-bot author signal is the local Mac
        # lane runner's own provenance identity: devin_cli/devin_cli, never
        # the hosted devin/devin_cloud identity.
        metadata = code_mower_builder_runs.PullRequestMetadata(
            repo="owner/repo",
            number="12",
            url="https://github.com/owner/repo/pull/12",
            author="someone",
            branch="devin/issue-12-fix",
            body="",
        )
        inference = code_mower_builder_runs.infer_builder_from_pr(metadata)
        self.assertIsNotNone(inference)
        assert inference is not None
        self.assertEqual(inference.provider, "devin_cli")
        self.assertEqual(inference.executor, "devin_cli")

    def test_devin_bot_author_infers_hosted_devin_builder(self) -> None:
        # The hosted Devin GitHub bot author is a stronger, high-confidence
        # signal than any branch prefix and must resolve to hosted Devin
        # (provider "devin"), never the local devin_cli identity.
        metadata = code_mower_builder_runs.PullRequestMetadata(
            repo="owner/repo",
            number="12",
            url="https://github.com/owner/repo/pull/12",
            author="devin-ai-integration[bot]",
            branch="some-other-branch",
            body="",
        )
        inference = code_mower_builder_runs.infer_builder_from_pr(metadata)
        self.assertIsNotNone(inference)
        assert inference is not None
        self.assertEqual(inference.provider, "devin")
        self.assertEqual(inference.executor, "devin")

    def test_devin_bot_author_wins_over_devin_branch_prefix(self) -> None:
        # Even when a PR happens to use a devin/ branch name, a hosted bot
        # author is the stronger marker and must still win: hosted identity,
        # not the local-CLI default that the branch prefix alone would imply.
        metadata = code_mower_builder_runs.PullRequestMetadata(
            repo="owner/repo",
            number="12",
            url="https://github.com/owner/repo/pull/12",
            author="devin-ai-integration[bot]",
            branch="devin/issue-12-fix",
            body="",
        )
        inference = code_mower_builder_runs.infer_builder_from_pr(metadata)
        self.assertIsNotNone(inference)
        assert inference is not None
        self.assertEqual(inference.provider, "devin")
        self.assertEqual(inference.executor, "devin")


class DevinLaneStatusProcessDiscoveryTests(unittest.TestCase):
    def test_collect_lane_processes_identifies_devin_without_exposing_prompt_path(
        self,
    ) -> None:
        ps_output = (
            "4242 /usr/local/bin/devin --print --prompt-file /tmp/prompt.tmp "
            "--respect-workspace-trust false --sandbox --permission-mode autonomous\n"
        )

        def fake_command_runner(args):
            if args[:1] == ["ps"] and "-axo" in args:
                return subprocess.CompletedProcess(args, 0, stdout=ps_output, stderr="")
            if args[:1] == ["lsof"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="n/tmp/lanes/devin/owner__repo\n", stderr=""
                )
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

        report = lane_status.collect_lane_processes(fake_command_runner)

        self.assertTrue(report["available"])
        self.assertEqual(len(report["processes"]), 1)
        process = report["processes"][0]
        self.assertEqual(process["provider"], "devin")
        self.assertEqual(process["process"], "devin")
        self.assertNotIn("prompt", str(process).lower())
        self.assertNotIn(".md", str(process))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
