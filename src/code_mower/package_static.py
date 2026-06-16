"""Static generated package file contents."""

from __future__ import annotations

STATIC_PACKAGE_FILES = (
    ("src/code_mower/__init__.py", ""),
    (
        "README.md",
        "\n".join(
            [
                "# Code Mower",
                "",
                "Code Mower is the fastest way to build a peer-programmer and "
                "reviewer system around the best AI coding agents.",
                "",
                "It helps teams drive from plan to merge at maximum safe velocity "
                "while preserving code quality, architecture, and deployment "
                "confidence. It also turns your real codebase into a "
                "quality-and-velocity benchmark, measuring which AI builders and "
                "reviewers deliver the best quality, speed, and cost results for "
                "your actual product.",
                "",
                "The Code Mower open-source core is licensed under Apache-2.0. "
                "Hosted benchmarking and reporting, managed integrations, "
                "private telemetry and benchmark data products, enterprise "
                "controls, and support are commercial surfaces unless licensed "
                "otherwise.",
                "",
                "Code Mower is extracted from a production multi-repo development "
                "workflow and packaged as a standalone OSS tool. Start with "
                "`code-mower init --easy`, then run "
                "`code-mower doctor --easy` to verify local CLIs, "
                "tokens, provider catalog coverage, and runtime probes.",
                "",
                "For existing repos that still carry product-local Code Mower "
                "tools, run `code-mower migration wrapper-rehearsal "
                "--repo-path /path/to/repo --json` before flipping to a pinned "
                "standalone package. The rehearsal compares safe read-only "
                "commands and gives a low-risk path away from mirrored "
                "maintenance.",
                "",
                "For public release readiness, see `docs/repo-strategy.md`, "
                "`docs/mirror-removal-runbook.md`, "
                "`docs/commercial-boundary.md`, and "
                "`docs/public-release-checklist.md`.",
                "",
            ]
        ),
    ),
    (
        "MANIFEST.in",
        "\n".join(
            [
                "recursive-include src/code_mower/templates *.yml *.yaml *.json",
                "recursive-include src/code_mower/templates *.md",
                "recursive-include src/code_mower/templates *.j2",
                "recursive-include src/code_mower/templates/product-support *",
                "include src/code_mower/*.json",
                "recursive-include templates *.j2 *.json *.md *.yml *.yaml",
                "include requirements/*.txt",
                "include LICENSE",
                "include NOTICE",
                "",
            ]
        ),
    ),
    (
        ".gitignore",
        "\n".join(
            [
                ".venv/",
                "__pycache__/",
                "*.py[cod]",
                "*.egg-info/",
                "build/",
                "dist/",
                ".pytest_cache/",
                ".mypy_cache/",
                ".ruff_cache/",
                ".code-mower/",
                ".code-mower.generated/",
                "",
            ]
        ),
    ),
    (
        ".github/workflows/ci.yml",
        "\n".join(
            [
                "name: Code Mower CI",
                "",
                "on:",
                "  push:",
                "    branches: [main]",
                "  pull_request:",
                "  workflow_dispatch:",
                "",
                "jobs:",
                "  package:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - name: Check out",
                "        uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
                "",
                "      - name: Set up Python",
                "        uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
                "        with:",
                "          python-version: '3.12'",
                "",
                "      - name: Install package",
                "        run: |",
                "          python -m pip install --upgrade pip",
                "          python -m pip install -e .",
                "          python -m pip check",
                "",
                "      - name: Compile sources",
                "        run: python -m compileall src scripts",
                "",
                "      - name: Easy-mode smoke",
                "        run: python scripts/smoke_easy_mode.py --work-dir \"$RUNNER_TEMP/code-mower-smoke\" --json",
                "",
                "      - name: Fresh-clone rehearsal",
                "        run: >-",
                "          python scripts/fresh_clone_rehearsal.py",
                "          --repo-url \"$GITHUB_WORKSPACE\"",
                "          --ref \"$GITHUB_SHA\"",
                "          --work-dir \"$RUNNER_TEMP/code-mower-fresh-clone\"",
                "          --json",
                "",
            ]
        ),
    ),
    (
        "scripts/smoke_easy_mode.py",
        """#!/usr/bin/env python3
\"\"\"Smoke test the standalone Code Mower easy-mode package flow.\"\"\"

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CODE_MOWER_BIN = \"__CODE_MOWER_CONSOLE_SCRIPT__\"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if stdout_path is not None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(completed.stdout, encoding=\"utf-8\")
    result = {
        \"command\": command,
        \"cwd\": str(cwd),
        \"returncode\": completed.returncode,
        \"stdout_path\": str(stdout_path) if stdout_path else None,
        \"stderr\": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(result, indent=2, sort_keys=True))
    return result


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + \"\\n\", encoding=\"utf-8\")


def _resolve_executable(path_text: str) -> Path:
    resolved = shutil.which(path_text)
    if resolved:
        return Path(resolved).resolve()
    return Path(path_text).expanduser().resolve()


def _default_code_mower_bin() -> str:
    resolved = shutil.which(DEFAULT_CODE_MOWER_BIN)
    if resolved:
        return resolved

    sibling_script = Path(sys.executable).parent / DEFAULT_CODE_MOWER_BIN
    if sibling_script.is_file():
        return str(sibling_script)

    resolved_sibling_script = Path(sys.executable).resolve().parent / DEFAULT_CODE_MOWER_BIN
    if resolved_sibling_script.is_file():
        return str(resolved_sibling_script)

    return DEFAULT_CODE_MOWER_BIN


def run_smoke(*, code_mower_bin: Path, work_dir: Path) -> dict[str, Any]:
    if not code_mower_bin.is_file():
        raise RuntimeError(f\"code-mower executable not found: {code_mower_bin}\")

    toy_repo = work_dir / \"toy-repo\"
    outputs = work_dir / \"outputs\"
    if toy_repo.exists():
        shutil.rmtree(toy_repo)
    toy_repo.mkdir(parents=True)
    outputs.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env[\"PATH\"] = f\"{code_mower_bin.parent}{os.pathsep}{env.get('PATH', '')}\"

    git = shutil.which(\"git\")
    if git:
        _run([git, \"init\", \"-q\"], cwd=toy_repo, env=env)
        _run([git, \"config\", \"user.name\", \"Code Mower Smoke\"], cwd=toy_repo, env=env)
        _run([git, \"config\", \"user.email\", \"smoke@example.com\"], cwd=toy_repo, env=env)
        _run([git, \"config\", \"commit.gpgSign\", \"false\"], cwd=toy_repo, env=env)
    (toy_repo / \"README.md\").write_text(
        \"# Code Mower smoke toy repo\\n\", encoding=\"utf-8\"
    )
    if git:
        _run([git, \"add\", \"README.md\"], cwd=toy_repo, env=env)
        _run(
            [git, \"-c\", \"commit.gpgSign=false\", \"commit\", \"-q\", \"-m\", \"Initial smoke repo\"],
            cwd=toy_repo,
            env=env,
        )

    steps: list[dict[str, Any]] = []
    cm = str(code_mower_bin)
    steps.append(
        _run([cm, \"providers\", \"list\"], cwd=toy_repo, env=env, stdout_path=outputs / \"providers.txt\")
    )
    steps.append(
        _run(
            [cm, \"init\", \"--easy\", \"--apply\", \"--output-dir\", \".code-mower.generated\", \"--json\"],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / \"init-apply.json\",
        )
    )
    steps.append(
        _run(
            [\"bash\", \".code-mower.generated/smoke-tests.sh\"],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / \"generated-smoke-tests.txt\",
        )
    )
    steps.append(
        _run([cm, \"doctor\", \"--easy\", \"--json\"], cwd=toy_repo, env=env, stdout_path=outputs / \"doctor.json\")
    )
    steps.append(
        _run(
            [cm, \"next-steps\", \"--profile\", \"recommended\", \"--json\"],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / \"next-steps.json\",
        )
    )
    steps.append(
        _run(
            [
                cm,
                \"migration\",
                \"wrapper-rehearsal\",
                \"--repo-path\",
                str(toy_repo),
                \"--local-command\",
                cm,
                \"--package-command\",
                cm,
                \"--json\",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / \"wrapper-rehearsal.json\",
        )
    )

    code_mower_dir = toy_repo / \".code-mower\"
    code_mower_dir.mkdir(exist_ok=True)
    steps.append(
        _run(
            [
                cm,
                \"calibration\",
                \"plan\",
                \".code-mower.generated/calibration-corpus.json\",
                \"--replicates\",
                \"2\",
                \"--json\",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=code_mower_dir / \"calibration-plan.json\",
        )
    )
    steps.append(
        _run(
            [cm, \"calibration\", \"evidence\", \".code-mower.generated/calibration-corpus.json\", \"--json\"],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / \"calibration-evidence.json\",
        )
    )
    steps.append(
        _run(
            [
                cm,
                \"reviewer-metrics\",
                \"calibration-evidence.json\",
                \"--spend\",
                \".code-mower.generated/reviewer-spend.json\",
                \"--json\",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / \"reviewer-metrics.json\",
        )
    )
    steps.append(
        _run(
            [cm, \"calibration\", \"policy\", \"reviewer-metrics.json\", \"--json\"],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / \"lane-policy.json\",
        )
    )
    steps.append(
        _run(
            [
                cm,
                \"calibration\",
                \"value-report\",
                \".code-mower.generated/calibration-corpus.json\",
                \"--spend\",
                \".code-mower.generated/reviewer-spend.json\",
                \"--output\",
                \"reviewer-value-report.md\",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=outputs / \"value-report.txt\",
        )
    )
    steps.append(
        _run(
            [
                cm,
                \"cloud\",
                \"export\",
                \"--report\",
                \"reviewer-metrics=reviewer-metrics.json\",
                \"--report\",
                \"lane-policy=lane-policy.json\",
                \"--report\",
                \"value-report=reviewer-value-report.md\",
                \"--output-dir\",
                \".code-mower/cloud-benchmark-bundle\",
                \"--json\",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / \"cloud-export.json\",
        )
    )
    steps.append(
        _run(
            [
                cm,
                \"cloud\",
                \"upload\",
                \".code-mower/cloud-benchmark-bundle\",
                \"--dry-run\",
                \"--json\",
            ],
            cwd=toy_repo,
            env=env,
            stdout_path=toy_repo / \"cloud-upload-dry-run.json\",
        )
    )

    summary = {
        \"mode\": \"code-mower-easy-mode-smoke\",
        \"status\": \"pass\",
        \"code_mower_bin\": str(code_mower_bin),
        \"work_dir\": str(work_dir),
        \"toy_repo\": str(toy_repo),
        \"outputs_dir\": str(outputs),
        \"steps\": steps,
    }
    _write_json(outputs / \"smoke-summary.json\", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        \"--code-mower-bin\",
        default=_default_code_mower_bin(),
        help=\"Path to the installed code-mower executable.\",
    )
    parser.add_argument(
        \"--work-dir\",
        default=None,
        help=\"Directory for the generated toy repo and outputs. Defaults to a temp dir.\",
    )
    parser.add_argument(\"--json\", action=\"store_true\")
    args = parser.parse_args(argv)

    work_dir = Path(args.work_dir) if args.work_dir else Path(
        tempfile.mkdtemp(prefix=\"code-mower-easy-smoke-\")
    )
    try:
        summary = run_smoke(code_mower_bin=_resolve_executable(args.code_mower_bin), work_dir=work_dir)
    except RuntimeError as exc:
        print(f\"error: {exc}\", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f\"Code Mower easy-mode smoke passed: {work_dir}\")
    return 0


if __name__ == \"__main__\":
    raise SystemExit(main())
""",
    ),
    (
        "scripts/fresh_clone_rehearsal.py",
        """#!/usr/bin/env python3
\"\"\"Rehearse Code Mower from a fresh clone and clean virtual environment.\"\"\"

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


class RehearsalError(RuntimeError):
    def __init__(self, message: str, steps: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.steps = steps


def _venv_python(venv_dir: Path) -> Path:
    unix_python = venv_dir / \"bin\" / \"python\"
    if unix_python.exists():
        return unix_python
    return venv_dir / \"Scripts\" / \"python.exe\"


def _default_repo_url() -> str:
    package_root = Path(__file__).resolve().parents[1]
    return str(package_root)


def _run(command: list[str], *, cwd: Path, steps: list[dict[str, Any]]) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    step = {
        \"command\": command,
        \"cwd\": str(cwd),
        \"returncode\": completed.returncode,
        \"stdout\": completed.stdout[-4000:],
        \"stderr\": completed.stderr[-4000:],
    }
    steps.append(step)
    if completed.returncode != 0:
        raise RehearsalError(f\"command failed: {' '.join(command)}\", steps)


def run_rehearsal(args: argparse.Namespace) -> dict[str, Any]:
    work_dir = (
        Path(args.work_dir).expanduser().resolve()
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix=\"code-mower-fresh-clone-\"))
    )
    clone_dir = work_dir / \"clone\"
    venv_dir = work_dir / \"venv\"
    smoke_dir = work_dir / \"smoke\"
    steps: list[dict[str, Any]] = []

    if clone_dir.exists() or venv_dir.exists() or smoke_dir.exists():
        raise RuntimeError(f\"work directory is not clean: {work_dir}\")
    work_dir.mkdir(parents=True, exist_ok=True)

    repo_url = args.repo_url or _default_repo_url()
    _run([\"git\", \"clone\", repo_url, str(clone_dir)], cwd=work_dir, steps=steps)
    if args.ref:
        _run([\"git\", \"checkout\", args.ref], cwd=clone_dir, steps=steps)

    python_bin = Path(args.python).expanduser().resolve() if args.python else Path(sys.executable)
    _run([str(python_bin), \"-m\", \"venv\", str(venv_dir)], cwd=work_dir, steps=steps)
    venv_python = _venv_python(venv_dir)
    _run([str(venv_python), \"-m\", \"pip\", \"install\", \"--upgrade\", \"pip\"], cwd=clone_dir, steps=steps)
    _run([str(venv_python), \"-m\", \"pip\", \"install\", \"-e\", \".\"], cwd=clone_dir, steps=steps)
    _run([str(venv_python), \"-m\", \"pip\", \"check\"], cwd=clone_dir, steps=steps)
    _run(
        [
            str(venv_python),
            \"scripts/smoke_easy_mode.py\",
            \"--work-dir\",
            str(smoke_dir),
            \"--json\",
        ],
        cwd=clone_dir,
        steps=steps,
    )

    return {
        \"mode\": \"code-mower-fresh-clone-rehearsal\",
        \"status\": \"pass\",
        \"repo_url\": repo_url,
        \"ref\": args.ref,
        \"work_dir\": str(work_dir),
        \"clone_dir\": str(clone_dir),
        \"steps\": steps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(\"--repo-url\", help=\"Repository URL or local path to clone.\")
    parser.add_argument(\"--ref\", help=\"Branch, tag, or commit to check out after cloning.\")
    parser.add_argument(\"--work-dir\", help=\"Clean work directory for clone, venv, and smoke output.\")
    parser.add_argument(\"--python\", help=\"Python executable used to create the rehearsal venv.\")
    parser.add_argument(\"--json\", action=\"store_true\", help=\"Emit JSON output.\")
    args = parser.parse_args(argv)

    try:
        payload = run_rehearsal(args)
    except Exception as exc:  # pragma: no cover - exercised by CLI failure paths.
        payload = {
            \"mode\": \"code-mower-fresh-clone-rehearsal\",
            \"status\": \"fail\",
            \"error\": str(exc),
            \"steps\": getattr(exc, \"steps\", []),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f\"fresh clone rehearsal failed: {exc}\", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f\"fresh clone rehearsal passed: {payload['clone_dir']}\")
    return 0


if __name__ == \"__main__\":
    raise SystemExit(main())
""",
    ),
)
