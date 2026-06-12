#!/usr/bin/env python3
"""Render the future standalone Code Mower package/install dry-run."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __package__ in {None, "", "tools"}:
    from tools.code_mower_config import (
        ConfigError,
        RenderedPlan,
        SAFE_IDENTIFIER_RE,
        _format_issues,
        load_config,
        validate_config,
    )
else:  # pragma: no cover - exercised after package extraction.
    from .config import (
        ConfigError,
        RenderedPlan,
        SAFE_IDENTIFIER_RE,
        _format_issues,
        load_config,
        validate_config,
    )


DEFAULT_PROVIDER_TEMPLATES = "code-mower.provider-templates.yml"

PACKAGE_FILES = (
    ("tools/CODE_MOWER_APACHE_LICENSE.txt", "LICENSE", "package"),
    ("tools/CODE_MOWER_NOTICE.txt", "NOTICE", "package"),
    ("tools/code_mower_cli.py", "src/code_mower/cli.py", "core"),
    ("tools/code_mower_bootstrap.py", "src/code_mower/bootstrap.py", "core"),
    ("tools/code_mower_builder_experiment.py", "src/code_mower/builder_experiment.py", "core"),
    ("tools/code_mower_requirements.txt", "requirements/code-mower.txt", "tooling"),
    ("tools/code_mower_calibration.py", "src/code_mower/code_mower_calibration.py", "core"),
    ("tools/code_mower_cloud.py", "src/code_mower/cloud.py", "core"),
    ("tools/code_mower_config.py", "src/code_mower/config.py", "core"),
    ("tools/code_mower_context_packs.py", "src/code_mower/code_mower_context_packs.py", "core"),
    ("tools/code_mower_doctor.py", "src/code_mower/doctor.py", "core"),
    ("tools/code_mower_init.py", "src/code_mower/init.py", "core"),
    ("tools/code_mower_merge.py", "src/code_mower/code_mower_merge.py", "core"),
    ("tools/code_mower_next_steps.py", "src/code_mower/next_steps.py", "core"),
    ("tools/code_mower_package.py", "src/code_mower/package.py", "core"),
    ("tools/code_mower_prompts.py", "src/code_mower/prompts.py", "core"),
    ("tools/code_mower_secrets.py", "src/code_mower/secrets.py", "core"),
    ("tools/code_mower_telemetry.py", "src/code_mower/code_mower_telemetry.py", "core"),
    ("tools/reviewer_metrics.py", "src/code_mower/reviewer_metrics.py", "core"),
    ("tools/provider_registry.py", "src/code_mower/provider_registry.py", "core"),
    ("tools/blind_review_coordinator.py", "src/code_mower/blind_review_coordinator.py", "core"),
    ("tools/blind_review_artifacts.py", "src/code_mower/blind_review_artifacts.py", "core"),
    ("tools/audit_handoff_log.py", "src/code_mower/audit_handoff_log.py", "core"),
    ("tools/audit_labeler_lib.py", "src/code_mower/audit_labeler_lib.py", "core"),
    ("tools/audit_progress.py", "src/code_mower/audit_progress.py", "core"),
    ("tools/codex_audit_pr.py", "src/code_mower/codex_audit_pr.py", "reviewer"),
    (
        "tools/codex_audit_env_preflight.py",
        "src/code_mower/codex_audit_env_preflight.py",
        "reviewer",
    ),
    (
        "tools/codex_audit_schema_smoke.py",
        "src/code_mower/codex_audit_schema_smoke.py",
        "reviewer",
    ),
    (
        "tools/codex_audit_verdict.schema.json",
        "src/code_mower/codex_audit_verdict.schema.json",
        "reviewer",
    ),
    ("tools/claude_audit_pr.py", "src/code_mower/claude_audit_pr.py", "reviewer"),
    ("tools/trailer_comment_labeler.py", "src/code_mower/trailer_comment_labeler.py", "labeler"),
    ("tools/saas_reviewer_labeler.py", "src/code_mower/saas_reviewer_labeler.py", "labeler"),
    ("tools/local_llm_audit_pr.py", "src/code_mower/local_llm_audit_pr.py", "reviewer"),
    ("tools/local_llm_bakeoff.py", "src/code_mower/local_llm_bakeoff.py", "reviewer"),
    ("tools/local_llm_calibration.py", "src/code_mower/local_llm_calibration.py", "reviewer"),
    ("tools/local_llm_profiles.py", "src/code_mower/local_llm_profiles.py", "reviewer"),
    ("tools/gemini_cli_audit_pr.py", "src/code_mower/gemini_cli_audit_pr.py", "reviewer"),
    ("tools/antigravity_cli_audit_pr.py", "src/code_mower/antigravity_cli_audit_pr.py", "reviewer"),
    ("tools/hermes_cli_audit_pr.py", "src/code_mower/hermes_cli_audit_pr.py", "reviewer"),
    ("tools/coderabbit_cli_audit_pr.py", "src/code_mower/coderabbit_cli_audit_pr.py", "reviewer"),
    ("tools/lane_configs/__init__.py", "src/code_mower/lane_configs/__init__.py", "lane-config"),
    ("tools/lane_configs/claude.py", "src/code_mower/lane_configs/claude.py", "lane-config"),
    ("tools/lane_configs/codex.py", "src/code_mower/lane_configs/codex.py", "lane-config"),
    ("tools/lane_configs/devin.py", "src/code_mower/lane_configs/devin.py", "lane-config"),
    ("tools/lane_configs/local_llm.py", "src/code_mower/lane_configs/local_llm.py", "lane-config"),
    ("tools/lane_configs/aider.py", "src/code_mower/lane_configs/aider.py", "lane-config"),
    ("tools/lane_configs/gemini_cli.py", "src/code_mower/lane_configs/gemini_cli.py", "lane-config"),
    ("tools/lane_configs/antigravity_cli.py", "src/code_mower/lane_configs/antigravity_cli.py", "lane-config"),
    ("tools/lane_configs/hermes_cli.py", "src/code_mower/lane_configs/hermes_cli.py", "lane-config"),
    ("tools/lane_prompts/base-audit.md", "src/code_mower/templates/lane_prompts/base-audit.md", "prompt"),
    ("tools/lane_prompts/calibration-policy.md", "src/code_mower/templates/lane_prompts/calibration-policy.md", "prompt"),
    (
        "tools/lane_prompts/context-driven-quality.md",
        "src/code_mower/templates/lane_prompts/context-driven-quality.md",
        "prompt",
    ),
    ("tools/lane_prompts/docs-design.md", "src/code_mower/templates/lane_prompts/docs-design.md", "prompt"),
    (
        "tools/lane_prompts/generic-programming.md",
        "src/code_mower/templates/lane_prompts/generic-programming.md",
        "prompt",
    ),
    ("tools/lane_prompts/operability.md", "src/code_mower/templates/lane_prompts/operability.md", "prompt"),
    ("tools/lane_prompts/package-runtime.md", "src/code_mower/templates/lane_prompts/package-runtime.md", "prompt"),
    (
        "tools/lane_prompts/security-threat-model.md",
        "src/code_mower/templates/lane_prompts/security-threat-model.md",
        "prompt",
    ),
    ("code-mower.example.yml", "src/code_mower/templates/code-mower.example.yml", "config"),
    ("tools/calibration_corpus.example.json", "templates/calibration-corpus.json", "config"),
    ("tools/calibration_corpus.example.json", "templates/calibration-corpus.example.json", "config"),
    ("tools/builder_experiment.example.json", "templates/builder-experiment.example.json", "config"),
    ("tools/context_packs.example.json", "templates/context-packs.example.json", "config"),
    ("tools/reviewer_spend.example.json", "templates/reviewer-spend.example.json", "config"),
    (".cursor/BUGBOT.md", "templates/cursor/BUGBOT.md", "config"),
    (
        "tools/calibration_corpus.example.json",
        "src/code_mower/templates/calibration-corpus.json",
        "config",
    ),
    (
        "tools/calibration_corpus.example.json",
        "src/code_mower/templates/calibration-corpus.example.json",
        "config",
    ),
    (
        "tools/builder_experiment.example.json",
        "src/code_mower/templates/builder-experiment.example.json",
        "config",
    ),
    ("tools/context_packs.example.json", "src/code_mower/templates/context-packs.example.json", "config"),
    ("tools/reviewer_spend.example.json", "src/code_mower/templates/reviewer-spend.example.json", "config"),
    (
        "src/code_mower/templates/product-support/code_mower",
        "src/code_mower/templates/product-support/code_mower",
        "template",
    ),
    (
        "src/code_mower/templates/product-support/code_mower_standalone_pin.env",
        "src/code_mower/templates/product-support/code_mower_standalone_pin.env",
        "template",
    ),
    (
        "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh",
        "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh",
        "template",
    ),
    (
        "src/code_mower/templates/product-support/run_claude_audit_pr.sh",
        "src/code_mower/templates/product-support/run_claude_audit_pr.sh",
        "template",
    ),
    (
        "src/code_mower/templates/product-support/run_codex_audit_pr.sh",
        "src/code_mower/templates/product-support/run_codex_audit_pr.sh",
        "template",
    ),
    (
        "src/code_mower/templates/product-support/safe_gh_comment.py",
        "src/code_mower/templates/product-support/safe_gh_comment.py",
        "template",
    ),
    ("tools/ACP_BRIDGE_SPIKE.md", "docs/acp-bridge-spike.md", "doc"),
    ("tools/CODE_MOWER_CALIBRATION_PILOT.md", "docs/calibration-pilot.md", "doc"),
    ("tools/CODE_MOWER_CALIBRATION_NOTES.md", "docs/calibration-notes.md", "doc"),
    ("tools/CODE_MOWER_LENS_CALIBRATION_REPORT.md", "docs/lens-calibration-report.md", "doc"),
    ("tools/CODE_MOWER_REVIEWER_VALUE_REPORT.md", "docs/reviewer-value-report.md", "doc"),
    ("tools/CODE_MOWER_LANE_PROMOTION_POLICY.md", "docs/lane-promotion-policy.md", "doc"),
    ("tools/CODE_MOWER_PACKAGE_CUSTOMIZATION.md", "docs/package-customization.md", "doc"),
    ("tools/CODE_MOWER_AUTHORING_INTELLIGENCE.md", "docs/authoring-intelligence.md", "doc"),
    ("tools/CODE_MOWER_BUILDER_EXPERIMENTS.md", "docs/builder-experiments.md", "doc"),
    ("tools/CODE_MOWER_CLOUD_BENCHMARKING.md", "docs/cloud-benchmarking.md", "doc"),
    ("tools/CODE_MOWER_REPO_STRATEGY.md", "docs/repo-strategy.md", "doc"),
    ("tools/CODE_MOWER_COMMERCIAL_BOUNDARY.md", "docs/commercial-boundary.md", "doc"),
    ("tools/CODE_MOWER_PUBLIC_RELEASE_CHECKLIST.md", "docs/public-release-checklist.md", "doc"),
    ("tools/CODE_MOWER_GITHUB_SETUP.md", "docs/github-setup.md", "doc"),
    (
        "tools/CODE_MOWER_MIRROR_REMOVAL_RUNBOOK.md",
        "docs/mirror-removal-runbook.md",
        "doc",
    ),
    ("tools/CODE_MOWER_PROVIDER_MATRIX.md", "docs/provider-matrix.md", "doc"),
    ("tools/CODE_MOWER_OSS_V1_CHECKLIST.md", "docs/oss-v1-checklist.md", "doc"),
    ("tools/adapters/__init__.py", "src/code_mower/adapters/__init__.py", "adapter"),
    ("tools/adapters/_base.py", "src/code_mower/adapters/_base.py", "adapter"),
    ("tools/adapters/cursor_bugbot.py", "src/code_mower/adapters/cursor_bugbot.py", "adapter"),
    ("tools/adapters/gitar.py", "src/code_mower/adapters/gitar.py", "adapter"),
    ("tools/adapters/greptile.py", "src/code_mower/adapters/greptile.py", "adapter"),
    ("tools/adapters/qodo.py", "src/code_mower/adapters/qodo.py", "adapter"),
)

DEFERRED_PACKAGE_FILES = (
    (
        "tools/local_llm_audit_bridge.py",
        "src/code_mower/local_llm_audit_bridge.py",
        "daemon defaults and scheduling policy remain repo-local",
    ),
)

TEMPLATE_FILES = (
    ("package", "LICENSE"),
    ("package", "NOTICE"),
    ("package", "pyproject.toml"),
    ("package", "README.md"),
    ("package", "MANIFEST.in"),
    ("package", ".gitignore"),
    ("workflow", "templates/workflows/trailer-comment-labeler.yml.j2"),
    ("workflow", "templates/workflows/saas-reviewer-labeler.yml.j2"),
    ("workflow", "templates/workflows/local-cli-audit.yml.j2"),
    ("workflow", "templates/workflows/blind-review-artifacts-dry-run.yml.j2"),
    ("workflow", "templates/workflows/hosted-bridge.yml.j2"),
    ("workflow", "templates/workflows/audit-label-cleanup.yml.j2"),
    ("workflow", "templates/workflows/review-clear-stale.yml.j2"),
    ("workflow", "templates/workflows/private-standalone-shadow.yml.j2"),
    ("workflow", "src/code_mower/templates/workflows/private-standalone-shadow.yml.j2"),
    ("config", "templates/code-mower.yml.j2"),
    ("provider-config", "templates/providers.yml"),
    ("provider-catalog", "src/code_mower/templates/providers.yml"),
)

STATIC_PACKAGE_FILES = (
    ("src/code_mower/__init__.py", '"""Code Mower package."""\n\n__version__ = "0.0.0"\n'),
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


def _pyproject_text(package_name: str) -> str:
    return (
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=68"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                f'name = "{package_name}"',
                'version = "0.0.0"',
                'description = "Multi-reviewer AI code audit orchestration"',
                'requires-python = ">=3.11"',
                'readme = "README.md"',
                'license = {text = "Apache-2.0"}',
                'dependencies = ["PyYAML>=6.0"]',
                "",
                "[project.scripts]",
                f'{package_name} = "code_mower.cli:main"',
                "",
                "[tool.setuptools.packages.find]",
                'where = ["src"]',
                "",
                "[tool.setuptools.package-data]",
                'code_mower = ["*.json", "templates/**/*.json", "templates/**/*.md", "templates/**/*.yml", "templates/**/*.yaml", "templates/**/*.j2", "templates/product-support/*"]',
                "",
            ]
        )
    )


def _workflow_template_text(target: str) -> str:
    if target.endswith("blind-review-artifacts-dry-run.yml.j2"):
        return "\n".join(
            [
                "# Code Mower blind-review artifact dry-run template",
                "# Install as .github/workflows/code-mower-blind-review-artifacts-dry-run.yml.",
                "name: Code Mower blind-review artifact dry run",
                "on:",
                "  workflow_dispatch:",
                "permissions:",
                "  contents: read",
                "  actions: write",
                "jobs:",
                "  dry-run:",
                "    runs-on: ubuntu-latest",
                "    timeout-minutes: 10",
                "    steps:",
                "      - name: Check out workflow code",
                "        uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
                "        with:",
                "          persist-credentials: false",
                "      - name: Build synthetic manifests",
                "        run: |",
                "          set -euo pipefail",
                "          mkdir -p .code-mower/dry-run-source",
                "          printf 'Codex Audit: PASS\\n' > .code-mower/dry-run-source/codex.md",
                "          printf 'Claude Audit: PASS\\n' > .code-mower/dry-run-source/claude.md",
                "          python3 - <<'PY'",
                "          import json",
                "          import os",
                "          import subprocess",
                "          repo = os.environ.get('GITHUB_REPOSITORY', 'owner/repo')",
                "          head_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()",
                "          base = {'repo': repo, 'pr_number': 0, 'head_sha': head_sha, 'required_lanes': ['codex', 'claude_audit']}",
                "          hold = {**base, 'events': [{'lane': 'codex', 'state': 'done', 'artifact': 'dry-run-source/codex.md', 'head_sha': head_sha}, {'lane': 'claude_audit', 'state': 'running', 'head_sha': head_sha}]}",
                "          release = {**base, 'events': [{'lane': 'codex', 'state': 'done', 'artifact': 'dry-run-source/codex.md', 'head_sha': head_sha}, {'lane': 'claude_audit', 'state': 'done', 'artifact': 'dry-run-source/claude.md', 'head_sha': head_sha}]}",
                "          for name, payload in (('hold', hold), ('release', release)):",
                "              with open(f'.code-mower/{name}-manifest.json', 'w', encoding='utf-8') as handle:",
                "                  json.dump(payload, handle, indent=2, sort_keys=True)",
                "                  handle.write('\\n')",
                "          PY",
                "      - name: Install Code Mower",
                "        run: |",
                "          python3 -m pip install --upgrade pip",
                "          python3 -m pip install code-mower",
                "          code-mower --help",
                "      - name: Materialize held artifacts",
                "        run: |",
                "          code-mower blind-review artifacts .code-mower/hold-manifest.json --write --json",
                "      - name: Upload held artifacts",
                "        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                "        with:",
                "          name: code-mower-blind-review-held-dry-run",
                "          path: .code-mower/blind-review",
                "          include-hidden-files: true",
                "          retention-days: 1",
                "      - name: Download held artifacts",
                "        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                "        with:",
                "          name: code-mower-blind-review-held-dry-run",
                "          path: .code-mower/downloaded-held",
                "      - name: Release downloaded held artifacts",
                "        run: |",
                "          rm -f .code-mower/dry-run-source/codex.md",
                "          code-mower blind-review artifacts .code-mower/release-manifest.json --output-dir .code-mower/downloaded-held --write --json | tee .code-mower/release-plan.json",
                "          python3 - <<'PY'",
                "          import hashlib",
                "          import json",
                "          from pathlib import Path",
                "          def sha256_file(path):",
                "              digest = hashlib.sha256()",
                "              with path.open('rb') as handle:",
                "                  for chunk in iter(lambda: handle.read(1024 * 1024), b''):",
                "                      digest.update(chunk)",
                "              return digest.hexdigest()",
                "          plan = json.loads(Path('.code-mower/release-plan.json').read_text())",
                "          release_path = Path(plan['release_manifest']['path'])",
                "          if not release_path.is_file():",
                "              raise SystemExit(f'release manifest was not written: {release_path}')",
                "          for artifact in plan['release_manifest']['artifacts']:",
                "              path = Path(artifact['release_path'])",
                "              if not path.is_file():",
                "                  raise SystemExit(f'release artifact was not written: {path}')",
                "              actual = sha256_file(path)",
                "              if artifact.get('release_sha256') != actual:",
                "                  raise SystemExit(f'release artifact hash mismatch: {path}')",
                "          PY",
                "",
            ]
        )
    if target.endswith("private-standalone-shadow.yml.j2"):
        return r"""name: Code Mower standalone shadow

on:
  pull_request:
    paths:
      - "tools/code_mower"
      - "tools/code_mower_standalone_shadow.sh"
      - "tools/code_mower_standalone_pin.env"
      - "tools/code_mower_*.py"
      - "tools/CODE_MOWER*.md"
      - "code-mower*.yml"
      - ".github/workflows/code-mower-standalone-shadow.yml"
  workflow_dispatch:

permissions:
  contents: read

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"
  CODE_MOWER_STANDALONE_REPO_URL: {{ code_mower_standalone_repo_url | default('https://github.com/OWNER/code-mower.git', true) }}
  CODE_MOWER_STANDALONE_REINSTALL: "1"
  CODE_MOWER_BOOTSTRAP_PYTHON: python3

jobs:
  shadow:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out product repo
        uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10
        with:
          persist-credentials: false

      - name: Configure Code Mower deploy key
        shell: bash
        env:
          CODE_MOWER_STANDALONE_DEPLOY_KEY: {% raw %}${{ secrets.CODE_MOWER_STANDALONE_DEPLOY_KEY }}{% endraw %}
        run: |
          set -euo pipefail
          if [ -z "${CODE_MOWER_STANDALONE_DEPLOY_KEY}" ]; then
            echo "::error::Missing CODE_MOWER_STANDALONE_DEPLOY_KEY. Add a read-only deploy key for the configured Code Mower standalone repository to this repository's Actions secrets."
            exit 1
          fi
          install -m 700 -d ~/.ssh
          printf '%s\n' "${CODE_MOWER_STANDALONE_DEPLOY_KEY}" > ~/.ssh/code_mower_standalone
          chmod 600 ~/.ssh/code_mower_standalone
          ssh-keyscan github.com > ~/.ssh/known_hosts
          cat > ~/.ssh/config <<'EOF'
          Host github.com
            IdentityFile ~/.ssh/code_mower_standalone
            IdentitiesOnly yes
            StrictHostKeyChecking yes
          EOF
          chmod 600 ~/.ssh/config

      - name: Run standalone wrapper shadow proof
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p .code-mower
          tools/code_mower --version
          tools/code_mower doctor --easy --json | tee .code-mower/standalone-doctor.json
          tools/code_mower migration wrapper-rehearsal \
            --repo-path "$GITHUB_WORKSPACE" \
            --local-command "env CODE_MOWER_USE_LOCAL=1 tools/code_mower" \
            --package-command "tools/code_mower" \
            --timeout 120 \
            --json | tee .code-mower/standalone-wrapper-rehearsal.json

      - name: Upload shadow proof artifacts
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
        with:
          name: code-mower-standalone-shadow
          path: |
            .code-mower/standalone-doctor.json
            .code-mower/standalone-wrapper-rehearsal.json
          if-no-files-found: ignore
          retention-days: 7
""".strip() + "\n"
    workflow_name = Path(target).stem.replace("-", " ").title()
    return "\n".join(
        [
            "# Code Mower workflow template",
            "# Replace placeholders before installing this in .github/workflows/.",
            f"name: {workflow_name}",
            "on:",
            "  workflow_dispatch:",
            "jobs:",
            "  configure-code-mower:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - run: echo \"Install this generated template in a repository workflow.\"",
            "",
        ]
    )


def _config_template_text() -> str:
    return "\n".join(
        [
            "# Code Mower config template",
            "version: 1",
            "project:",
            "  name: \"{{ project_name }}\"",
            "  state_dir: \"{{ state_dir }}\"",
            "repositories:",
            "  - slug: \"{{ repository_slug }}\"",
            "    default_branch: main",
            "lanes: {}",
            "profiles: {}",
            "",
        ]
    )


CLI_COMMANDS = (
    "code-mower config validate code-mower.yml",
    "code-mower config plan code-mower.yml --json",
    "code-mower init --easy",
    "code-mower init --easy --apply --output-dir .code-mower.generated",
    "code-mower next-steps --profile recommended",
    "code-mower next-steps --profile recommended --json",
    "code-mower builder-experiment plan builder-experiment.json --json",
    "code-mower builder-experiment report builder-experiment.json --runs builder-results.json --output builder-experiment-report.md",
    "code-mower calibration plan calibration-corpus.json --replicates 2 --json",
    "code-mower calibration run calibration-corpus.json --lanes antigravity-cli,gemini-cli,hermes-cli --results-dir .code-mower/calibration-results --json",
    "code-mower calibration evidence calibration-corpus.json --json",
    "code-mower calibration value-report calibration-corpus.json --runs .code-mower/calibration-results/calibration-run-results.json --output reviewer-value-report.md",
    "code-mower calibration overlap calibration.json --json",
    "code-mower doctor --easy --json",
    "code-mower doctor --profile recommended --json",
    "code-mower doctor --profile privacy --probe-runtime --json",
    "code-mower init --profile recommended --dry-run",
    "code-mower init --profile recommended --apply --output-dir .code-mower.generated",
    "code-mower merge-plan owner/repo#123 --json",
    "code-mower migration wrapper-rehearsal --repo-path /path/to/product-repo --json",
    "code-mower migration mirror-removal-plan --repo-path /path/to/product-repo --shadow-cycles 1 --standalone-default-cycles 1 --json",
    "code-mower migration runner-aliases --json",
    "code-mower migration package-install-rehearsal --package-spec code-mower --repo-path /path/to/product-repo --json",
    "code-mower local-llm profiles --json",
    "code-mower local-llm probe --profile qwen3-coder-next-lmstudio --json",
    "code-mower local-llm probe --profile gemma4-ollama --json",
    "code-mower local-llm audit --repo owner/repo --pr 123 --dry-run --max-files 8",
    "code-mower local-llm bakeoff --repo owner/repo --pr 123 --profiles qwen3-coder-next-lmstudio,gemma4-ollama --json",
    "code-mower local-llm calibrate /tmp/code-mower-local-bakeoff/summary.json --json",
    "code-mower gemini-cli --repo owner/repo --pr 123 --output-dir .code-mower/calibration/pr-123/gemini-cli --json",
    "code-mower antigravity-cli --repo owner/repo --pr 123 --output-dir .code-mower/calibration/pr-123/antigravity-cli --json",
    "code-mower hermes-cli --repo owner/repo --pr 123 --output-dir .code-mower/calibration/pr-123/hermes-cli --json",
    "code-mower coderabbit-cli --repo owner/repo --pr 123 --repo-path /path/to/pr-worktree --output-dir .code-mower/calibration/pr-123/coderabbit-cli --json",
    "code-mower local-llm calibrate /tmp/code-mower-local-bakeoff/summary.json --write-disposition-template dispositions.json",
    "code-mower context-packs context-packs.json --json",
    "code-mower context-packs templates/context-packs.example.json --json",
    "code-mower context-packs context-packs.json --write --output-dir .code-mower/context-packs --json",
    "code-mower prompts list --json",
    "code-mower prompts show base-audit --json",
    "code-mower prompts validate --lenses base-audit,calibration-policy,package-runtime --json",
    "code-mower prompts validate --lenses base-audit,generic-programming,context-driven-quality --json",
    "code-mower prompts validate --lenses base-audit,security-threat-model,operability --json",
    "code-mower reviewer-metrics calibration.json --spend templates/reviewer-spend.example.json --json",
    "code-mower blind-review plan blind-review-manifest.json --json",
    "code-mower blind-review artifacts blind-review-manifest.json --json",
    "code-mower blind-review artifacts blind-review-manifest.json --write --require-sources --json",
    "code-mower providers list",
    "code-mower providers show <provider>",
    "code-mower telemetry summarize ~/.cache/code-mower-audits/events.jsonl --json",
    "code-mower cloud export --report reviewer-metrics=reviewer-metrics.json --report value-report=reviewer-value-report.md --output-dir .code-mower/cloud-benchmark-bundle --json",
    "python scripts/smoke_easy_mode.py --json",
    "python scripts/fresh_clone_rehearsal.py --json",
)


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ConfigError(f"{name} must be a mapping")


def load_provider_templates(path: Path) -> Mapping[str, Any]:
    templates = load_config(path)
    if templates.get("version") not in {1, "1"}:
        raise ConfigError("provider templates version must be 1")
    _as_mapping(templates.get("provider_templates"), "provider_templates")
    profiles = _as_mapping(templates.get("profiles"), "profiles")
    for profile_id, profile in profiles.items():
        if not isinstance(profile, Mapping):
            raise ConfigError(f"provider template profile {profile_id!r} must be a mapping")
    return templates


def resolve_provider_templates_path(path_text: str) -> Path:
    path = Path(path_text)
    if path_text != DEFAULT_PROVIDER_TEMPLATES or path.is_absolute():
        return path

    module_dir = Path(__file__).resolve().parent
    candidates = (
        module_dir.parent / DEFAULT_PROVIDER_TEMPLATES,
        module_dir / "templates" / "providers.yml",
        Path.cwd() / DEFAULT_PROVIDER_TEMPLATES,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _provider_template_rows(
    config: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lanes = _as_mapping(config.get("lanes"), "lanes")
    templates = _as_mapping(provider_templates.get("provider_templates"), "provider_templates")
    rows: list[dict[str, Any]] = []
    missing = sorted(set(lanes) - set(templates))

    for lane_id in sorted(templates):
        if not SAFE_IDENTIFIER_RE.fullmatch(str(lane_id)):
            raise ConfigError(
                "provider template lane ids must match "
                f"[A-Za-z0-9][A-Za-z0-9_-]*: {lane_id}"
            )
        template = templates.get(lane_id)
        if not isinstance(template, Mapping):
            raise ConfigError(f"provider template for {lane_id} must be a mapping")
        lane = lanes.get(lane_id, {})
        if not isinstance(lane, Mapping):
            lane = {}
        token_env = lane.get("token_env", template.get("token_env", []))
        token_env_any = lane.get("token_env_any", template.get("token_env_any", []))
        review_hygiene = lane.get("review_hygiene", template.get("review_hygiene", {}))
        if not token_env and isinstance(review_hygiene, Mapping):
            review_token = review_hygiene.get("token_env")
            token_env = [review_token] if review_token else []
        rows.append(
            {
                "lane": str(lane_id),
                "provider": str(lane.get("provider", template.get("provider", ""))),
                "driver": str(lane.get("driver", template.get("driver", ""))),
                "type": str(lane.get("type", template.get("type", ""))),
                "adapter": lane.get("adapter", template.get("adapter")),
                "trailer_lane": lane.get("trailer_lane", template.get("trailer_lane")),
                "spend_policy": str(lane.get("spend_policy", template.get("spend_policy", "none"))),
                "merge_authority": bool(lane.get("merge_authority", template.get("merge_authority", False))),
                "informational": bool(lane.get("informational", template.get("informational", False))),
                "enabled_by_default": lane.get(
                    "enabled_by_default",
                    template.get("enabled_by_default", True),
                ),
                "events": list(lane.get("events", template.get("events", []))),
                "token_env": list(token_env),
                "token_env_any": list(token_env_any),
                "trigger_policy": lane.get(
                    "trigger_policy",
                    template.get("trigger_policy", "label"),
                ),
                "provider_config": lane.get(
                    "provider_config",
                    template.get("provider_config", {}),
                ),
                "review_hygiene": review_hygiene if isinstance(review_hygiene, Mapping) else {},
                "template_path": f"templates/providers/{lane_id}.yml",
            }
        )

    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ConfigError(f"provider templates missing configured lanes: {missing_text}")
    return rows


def render_package_plan(
    config: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
    package_name: str = "code-mower",
) -> RenderedPlan:
    if not SAFE_IDENTIFIER_RE.fullmatch(package_name):
        raise ConfigError("package name must match [A-Za-z0-9][A-Za-z0-9_-]*")

    issues = validate_config(config)
    if issues:
        raise ConfigError(f"invalid Code Mower config:\n{_format_issues(issues)}")

    template_rows = _provider_template_rows(config, provider_templates)
    catalog_profiles = _as_mapping(provider_templates.get("profiles"), "profiles")
    repo_profiles = _as_mapping(config.get("profiles", {}), "profiles")
    profiles: dict[str, dict[str, Any]] = {
        profile_id: {
            "description": profile.get("description", ""),
            "lanes": profile.get("lanes", []),
            "source": "catalog",
        }
        for profile_id, profile in catalog_profiles.items()
    }
    for profile_id, profile in repo_profiles.items():
        profiles[profile_id] = {
            "description": profile.get(
                "description",
                profiles.get(profile_id, {}).get("description", "Repo-local profile."),
            ),
            "lanes": profile.get("lanes", []),
            "source": "repo",
        }

    package_files = [
        {"source": source, "target": target, "kind": kind}
        for source, target, kind in PACKAGE_FILES
    ]
    deferred_package_files = [
        {"source": source, "target": target, "reason": reason}
        for source, target, reason in DEFERRED_PACKAGE_FILES
    ]
    template_files = [
        {"kind": kind, "target": target}
        for kind, target in TEMPLATE_FILES
    ]
    template_files.extend(
        {"kind": "provider-template", "target": row["template_path"]}
        for row in template_rows
    )
    data = {
        "mode": "dry-run",
        "package": {
            "name": package_name,
            "module": "code_mower",
            "console_script": f"{package_name}=code_mower.cli:main",
            "source_layout": "src/code_mower",
        },
        "package_files": package_files,
        "deferred_package_files": deferred_package_files,
        "template_files": template_files,
        "provider_templates": template_rows,
        "profiles": profiles,
        "cli_commands": list(CLI_COMMANDS),
        "install_docs": [
            "README.md",
            "docs/getting-started.md",
            "docs/package-skeleton.md",
            "docs/package-customization.md",
            "docs/repo-strategy.md",
            "docs/mirror-removal-runbook.md",
            "docs/commercial-boundary.md",
            "docs/public-release-checklist.md",
            "docs/github-setup.md",
            "docs/provider-matrix.md",
            "docs/providers.md",
            "docs/security.md",
            "docs/workflow-templates.md",
        ],
    }

    lines = [
        "Code Mower OSS package dry-run",
        f"Package: {data['package']['name']}",
        f"Module: {data['package']['module']}",
        f"Console script: {data['package']['console_script']}",
        "",
        "Package files to extract:",
    ]
    lines.extend(
        f"- {entry['source']} -> {entry['target']} [{entry['kind']}]"
        for entry in package_files
    )
    lines.extend(["", "Deferred package files:"])
    lines.extend(
        f"- {entry['source']} -> {entry['target']} ({entry['reason']})"
        for entry in deferred_package_files
    )

    lines.extend(["", "Template files to ship:"])
    lines.extend(f"- {entry['target']} [{entry['kind']}]" for entry in template_files)

    lines.extend(["", "Provider templates:"])
    lines.extend(
        f"- {entry['lane']}: {entry['driver']} / {entry['provider']} "
        f"({entry['spend_policy']}) -> {entry['template_path']}"
        for entry in template_rows
    )

    lines.extend(["", "CLI commands to document:"])
    lines.extend(f"- {command}" for command in CLI_COMMANDS)

    return RenderedPlan(text="\n".join(lines) + "\n", data=data)


def _write_text(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise ConfigError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_file(source: Path, target: Path, *, force: bool) -> None:
    if not source.is_file():
        raise ConfigError(f"package source file does not exist: {source}")
    if target.exists() and not force:
        raise ConfigError(f"refusing to overwrite existing file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _missing_package_sources(repo_root: Path) -> list[str]:
    return sorted(
        source
        for source, _, _ in PACKAGE_FILES
        if not (repo_root / source).is_file()
    )


def _planned_materialized_targets(plan: RenderedPlan) -> list[str]:
    targets = [target for _, target, _ in PACKAGE_FILES]
    targets.extend(target for target, _ in STATIC_PACKAGE_FILES)
    targets.extend(entry["target"] for entry in plan.data["template_files"])
    targets.extend(
        [
            "pyproject.toml",
            "src/code_mower/templates/providers.yml",
            "code-mower-package-manifest.json",
        ]
    )
    targets.extend(
        f"templates/providers/{row['lane']}.yml"
        for row in plan.data["provider_templates"]
    )
    return sorted(set(targets))


def _preflight_output_collisions(plan: RenderedPlan, output_dir: Path) -> None:
    collisions = [
        target
        for target in _planned_materialized_targets(plan)
        if (output_dir / target).exists()
    ]
    parent_collisions: list[str] = []
    for target in _planned_materialized_targets(plan):
        relative_parent = Path(target).parent
        while str(relative_parent) not in {"", "."}:
            parent_path = output_dir / relative_parent
            if parent_path.exists() and not parent_path.is_dir():
                parent_collisions.append(str(relative_parent))
                break
            relative_parent = relative_parent.parent
    collisions.extend(sorted(set(parent_collisions)))
    if not collisions:
        return
    sample = ", ".join(collisions[:3])
    if len(collisions) > 3:
        sample += f", ... ({len(collisions)} total)"
    raise ConfigError(f"refusing to overwrite existing file(s): {sample}")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _yaml_empty_container(value: Any) -> str | None:
    if isinstance(value, Mapping) and not value:
        return "{}"
    if isinstance(value, (list, tuple)) and not value:
        return "[]"
    return None


def _yaml_inline_sequence(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)):
        return None
    if any(isinstance(item, (Mapping, list, tuple)) for item in value):
        return None
    return "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"


def _render_yaml(value: Any, *, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            empty_container = _yaml_empty_container(item)
            if empty_container is not None:
                lines.append(f"{prefix}{key}: {empty_container}")
            elif isinstance(item, (Mapping, list, tuple)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_render_yaml(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            empty_container = _yaml_empty_container(item)
            inline_sequence = _yaml_inline_sequence(item)
            if empty_container is not None:
                lines.append(f"{prefix}- {empty_container}")
            elif inline_sequence is not None:
                lines.append(f"{prefix}- {inline_sequence}")
            elif isinstance(item, Mapping):
                items = list(item.items())
                first_key, first_value = items[0]
                first_empty = _yaml_empty_container(first_value)
                if first_empty is not None:
                    lines.append(f"{prefix}- {first_key}: {first_empty}")
                elif isinstance(first_value, (Mapping, list, tuple)):
                    lines.append(f"{prefix}- {first_key}:")
                    lines.extend(_render_yaml(first_value, indent=indent + 4))
                else:
                    lines.append(f"{prefix}- {first_key}: {_yaml_scalar(first_value)}")
                for key, nested in items[1:]:
                    nested_empty = _yaml_empty_container(nested)
                    if nested_empty is not None:
                        lines.append(f"{' ' * (indent + 2)}{key}: {nested_empty}")
                    elif isinstance(nested, (Mapping, list, tuple)):
                        lines.append(f"{' ' * (indent + 2)}{key}:")
                        lines.extend(_render_yaml(nested, indent=indent + 4))
                    else:
                        lines.append(
                            f"{' ' * (indent + 2)}{key}: {_yaml_scalar(nested)}"
                        )
            elif isinstance(item, (list, tuple)):
                lines.append(f"{prefix}-")
                lines.extend(_render_yaml(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines
    return [f"{prefix}{_yaml_scalar(value)}"]


def _render_provider_catalog(data: Mapping[str, Any]) -> str:
    return "\n".join(_render_yaml(data)) + "\n"


def materialize_package_plan(
    plan: RenderedPlan,
    *,
    output_dir: Path,
    repo_root: Path | None = None,
    force: bool = False,
) -> RenderedPlan:
    """Write the planned standalone package tree to ``output_dir``."""

    repo_root = repo_root or Path.cwd()
    missing_sources = _missing_package_sources(repo_root)
    if missing_sources:
        sample = ", ".join(missing_sources[:3])
        if len(missing_sources) > 3:
            sample += f", ... ({len(missing_sources)} total)"
        raise ConfigError(
            "package materialization requires the reference repository checkout; "
            f"missing source file(s): {sample}"
        )
    if output_dir.exists() and not output_dir.is_dir():
        raise ConfigError(f"output path is not a directory: {output_dir}")
    if not force:
        _preflight_output_collisions(plan, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, str]] = []

    for source, target, kind in PACKAGE_FILES:
        target_path = output_dir / target
        _copy_file(repo_root / source, target_path, force=force)
        written.append({"target": target, "source": source, "kind": kind})

    for target, content in STATIC_PACKAGE_FILES:
        if target == "scripts/smoke_easy_mode.py":
            content = content.replace(
                "__CODE_MOWER_CONSOLE_SCRIPT__",
                str(plan.data["package"]["name"]),
            )
        _write_text(output_dir / target, content, force=force)
        written.append({"target": target, "source": "generated", "kind": "package"})
    _write_text(
        output_dir / "pyproject.toml",
        _pyproject_text(str(plan.data["package"]["name"])),
        force=force,
    )
    written.append(
        {"target": "pyproject.toml", "source": "generated", "kind": "package"}
    )

    requirements_dir = output_dir / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)

    provider_templates = {
        row["lane"]: {
            key: value
            for key, value in row.items()
            if key not in {"lane", "template_path"}
        }
        for row in plan.data["provider_templates"]
    }
    provider_catalog = {
        "version": 1,
        "provider_templates": provider_templates,
        "profiles": plan.data["profiles"],
    }
    catalog_target = "src/code_mower/templates/providers.yml"
    _write_text(
        output_dir / catalog_target,
        _render_provider_catalog(provider_catalog),
        force=force,
    )
    written.append(
        {"target": catalog_target, "source": "generated", "kind": "provider-catalog"}
    )

    for entry in plan.data["template_files"]:
        target = entry["target"]
        kind = entry["kind"]
        if target in {"pyproject.toml", "README.md", "MANIFEST.in", catalog_target}:
            continue
        if kind == "provider-template":
            continue
        if target == "templates/providers.yml":
            content = _render_provider_catalog(provider_catalog)
        elif target == "templates/code-mower.yml.j2":
            content = _config_template_text()
        elif target.startswith("templates/workflows/") or target.startswith("src/code_mower/templates/workflows/"):
            content = _workflow_template_text(target)
        else:
            continue
        _write_text(output_dir / target, content, force=force)
        written.append({"target": target, "source": "generated", "kind": kind})

    for row in plan.data["provider_templates"]:
        lane = row["lane"]
        target = f"templates/providers/{lane}.yml"
        _write_text(
            output_dir / target,
            _render_provider_catalog({lane: provider_templates[lane]}),
            force=force,
        )
        written.append(
            {"target": target, "source": "generated", "kind": "provider-template"}
        )

    manifest = {
        "mode": "materialize",
        "package": plan.data["package"],
        "output_dir": str(output_dir),
        "files_written": written,
        "deferred_package_files": list(plan.data["deferred_package_files"]),
    }
    manifest_path = output_dir / "code-mower-package-manifest.json"
    written.append(
        {
            "target": "code-mower-package-manifest.json",
            "source": "generated",
            "kind": "manifest",
        }
    )
    manifest["files_written"] = written
    _write_text(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        force=force,
    )

    lines = [
        "Code Mower package materialized",
        f"Output: {output_dir}",
        f"Files written: {len(written)}",
        "",
        "Deferred package files:",
    ]
    if manifest["deferred_package_files"]:
        lines.extend(
            f"- {entry['source']} ({entry['reason']})"
            for entry in manifest["deferred_package_files"]
        )
    else:
        lines.append("- none")

    return RenderedPlan(text="\n".join(lines) + "\n", data=manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="code-mower.example.yml")
    parser.add_argument(
        "--provider-templates",
        default=None,
        help="provider template catalog to include in the package plan",
    )
    parser.add_argument("--package-name", default="code-mower")
    parser.add_argument("--dry-run", action="store_true", help="render the package plan")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="materialize the package tree into this directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing files under --output-dir",
    )
    parser.add_argument("--json", action="store_true", help="emit package plan as JSON")
    args = parser.parse_args(argv)

    if not args.dry_run and args.output_dir is None:
        print("error: pass --dry-run or --output-dir", file=sys.stderr)
        return 1
    if args.dry_run and args.output_dir is not None:
        print("error: --dry-run and --output-dir are mutually exclusive", file=sys.stderr)
        return 1

    try:
        plan = render_package_plan(
            load_config(Path(args.config)),
            load_provider_templates(
                resolve_provider_templates_path(DEFAULT_PROVIDER_TEMPLATES)
                if args.provider_templates is None
                else Path(args.provider_templates)
            ),
            package_name=args.package_name,
        )
        if args.output_dir is not None:
            plan = materialize_package_plan(
                plan,
                output_dir=args.output_dir,
                repo_root=Path(__file__).resolve().parents[1],
                force=args.force,
            )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan.data, indent=2, sort_keys=True))
    else:
        print(plan.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
