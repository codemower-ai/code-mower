"""Generated package content builders and command inventory."""

from __future__ import annotations

from pathlib import Path


if __package__ in {None, "", "tools"}:
    try:
        from tools import code_mower_versioning
    except ImportError:  # pragma: no cover - package-installed fallback.
        from code_mower import versioning as code_mower_versioning
else:  # pragma: no cover - exercised after package extraction.
    from . import versioning as code_mower_versioning


def current_alpha_package_spec(version: str) -> str:
    """Return the documented installable package spec for public users."""

    return code_mower_versioning.public_package_spec(version)


def _init_py_text(version: str) -> str:
    return f'"""Code Mower package."""\n\n__version__ = "{version}"\n'


def _pyproject_text(package_name: str, *, version: str) -> str:
    return "\n".join(
        [
            "[build-system]",
            'requires = ["setuptools>=68"]',
            'build-backend = "setuptools.build_meta"',
            "",
            "[project]",
            f'name = "{package_name}"',
            f'version = "{version}"',
            'description = "Multi-reviewer AI code audit orchestration"',
            'requires-python = ">=3.12"',
            'readme = "README.md"',
            'license = {text = "Apache-2.0"}',
            'dependencies = ["PyYAML>=6.0", "packaging>=23.2"]',
            "",
            "[project.optional-dependencies]",
            'coworker = ["mcp>=2.2.0,<2.3", "httpx2>=2.12,<3", "PyJWT[crypto]>=2.10,<3", "keyring>=25,<26"]',
            "",
            "[project.scripts]",
            f'{package_name} = "code_mower.cli:main"',
            "",
            "[tool.setuptools.packages.find]",
            'where = ["src"]',
            "",
            "[tool.setuptools.package-data]",
            'code_mower = ["*.json", "templates/**/*.json", "templates/**/*.md", "templates/**/*.yml", "templates/**/*.yaml", "templates/**/*.j2", "templates/lanes/*", "templates/product-support/*"]',
            "",
        ]
    )


def _workflow_template_file_text(target: str) -> str | None:
    module_dir = Path(__file__).resolve().parent
    filename = Path(target).name
    candidates = (module_dir / "templates" / "workflows" / filename,)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return None


def _workflow_template_text(target: str) -> str:
    file_text = _workflow_template_file_text(target)
    if file_text is None:
        raise FileNotFoundError(f"workflow template not found: {target}")
    return file_text

def _lane_template_file_text(target: str) -> str | None:
    module_dir = Path(__file__).resolve().parent
    filename = Path(target).name
    candidates = (module_dir / "templates" / "lanes" / filename,)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return None


def _lane_template_text(target: str) -> str:
    file_text = _lane_template_file_text(target)
    if file_text is None:
        raise FileNotFoundError(f"lane template not found: {target}")
    return file_text


def _config_template_text() -> str:
    return "\n".join(
        [
            "# Code Mower config template",
            "version: 1",
            "project:",
            '  name: "{{ project_name }}"',
            '  state_dir: "{{ state_dir }}"',
            "repositories:",
            '  - slug: "{{ repository_slug }}"',
            "    default_branch: main",
            "lanes: {}",
            "profiles: {}",
            "",
        ]
    )


def cli_commands(version: str) -> tuple[str, ...]:
    return (
        "code-mower config validate code-mower.yml",
        "code-mower config plan code-mower.yml --json",
        "code-mower init --easy",
        "code-mower init --easy --apply --output-dir .code-mower.generated",
        "code-mower init --builders codex,claude,cursor --dry-run",
        "code-mower init --builders codex,claude,cursor --apply --output-dir .code-mower.generated",
        "code-mower next-steps --profile recommended",
        "code-mower next-steps --profile recommended --json",
        "code-mower lanes status --repo owner/repo --json",
        "code-mower controller run --repo owner/repo --json",
        "code-mower board serve --repo owner/repo",
        "code-mower builder-experiment plan builder-experiment.json --json",
        "code-mower builder-experiment report builder-experiment.json --runs builder-results.json --output builder-experiment-report.md",
        "code-mower calibration plan calibration-corpus.json --replicates 2 --json",
        "code-mower calibration run calibration-corpus.json --lanes antigravity-cli,gemini-cli,hermes-cli --results-dir .code-mower/calibration-results --json",
        "code-mower calibration evidence calibration-corpus.json --json",
        "code-mower calibration value-report calibration-corpus.json --runs .code-mower/calibration-results/calibration-run-results.json --output reviewer-value-report.md --html-output reviewer-value-report.html",
        "code-mower calibration overlap calibration.json --json",
        "code-mower doctor --easy --json",
        "code-mower doctor --profile recommended --json",
        "code-mower doctor --profile privacy --probe-runtime --json",
        "code-mower checks detect --json",
        "code-mower checks run --dry-run --json",
        "code-mower init --profile recommended --dry-run",
        "code-mower init --profile recommended --apply --output-dir .code-mower.generated",
        "code-mower merge-plan owner/repo#123 --json",
        "code-mower clear-stale --lane devin --repo owner/repo --pr 123 --json",
        "code-mower migration wrapper-rehearsal --repo-path /path/to/product-repo --json",
        "code-mower migration mirror-removal-plan --repo-path /path/to/product-repo --shadow-cycles 1 --standalone-default-cycles 1 --json",
        "code-mower migration runner-aliases --json",
        (
            "code-mower migration package-install-rehearsal "
            f"--package-spec {current_alpha_package_spec(version)} "
            "--allow-package-index --repo-path /path/to/product-repo --json"
        ),
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
        "code-mower cloud export --report reviewer-metrics=reviewer-metrics.json --report lane-policy=lane-policy.json --report value-report=reviewer-value-report.md --output-dir .code-mower/cloud-benchmark-bundle --json",
        "code-mower cloud setup --token-stdin --team-id YOUR_TEAM_SLUG --install-id YOUR_INSTALL_ID --out ~/.config/code-mower/tokens/YOUR_INSTALL_ID.env",
        "code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json",
        "python scripts/smoke_easy_mode.py --json",
        "python scripts/fresh_clone_rehearsal.py --json",
    )
