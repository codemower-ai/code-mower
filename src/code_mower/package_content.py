"""Generated package content builders and command inventory."""

from __future__ import annotations

from pathlib import Path

def _init_py_text(version: str) -> str:
    return f'"""Code Mower package."""\n\n__version__ = "{version}"\n'


def _pyproject_text(package_name: str, *, version: str) -> str:
    return (
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=68"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                f'name = "{package_name}"',
                f'version = "{version}"',
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
  CODE_MOWER_STANDALONE_PACKAGE_REPO_URL: {{ code_mower_standalone_package_repo_url | default('git+ssh://git@github.com/OWNER/code-mower.git', true) }}
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
          code_mower_ref="${CODE_MOWER_STANDALONE_REF:-}"
          if [ -z "${code_mower_ref}" ]; then
            code_mower_ref="$(sed -n 's/^CODE_MOWER_STANDALONE_REF="\([^"]*\)"/\1/p' tools/code_mower_standalone_pin.env)"
          fi
          if [ -z "${code_mower_ref}" ]; then
            echo "::error::tools/code_mower_standalone_pin.env does not define CODE_MOWER_STANDALONE_REF"
            exit 1
          fi
          if [ "${code_mower_ref}" = "<pin-a-reviewed-code-mower-commit-or-tag>" ]; then
            echo "::error::replace the placeholder CODE_MOWER_STANDALONE_REF before package-install rehearsal"
            exit 1
          fi
          if [ "${CODE_MOWER_STANDALONE_PACKAGE_REPO_URL:-}" = "" ] || [ "${CODE_MOWER_STANDALONE_PACKAGE_REPO_URL}" = "git+ssh://git@github.com/OWNER/code-mower.git" ]; then
            echo "::error::replace CODE_MOWER_STANDALONE_PACKAGE_REPO_URL with a pip-installable Code Mower package source"
            exit 1
          fi
          package_spec="${CODE_MOWER_STANDALONE_PACKAGE_REPO_URL}@${code_mower_ref}"
          tools/code_mower migration package-install-rehearsal \
            --package-spec "$package_spec" \
            --repo-path "$GITHUB_WORKSPACE" \
            --local-command "env CODE_MOWER_USE_LOCAL=1 tools/code_mower" \
            --work-dir "$GITHUB_WORKSPACE/.code-mower/package-install-rehearsal" \
            --timeout 180 \
            --json | tee .code-mower/package-install-rehearsal.json

      - name: Upload shadow proof artifacts
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
        with:
          name: code-mower-standalone-shadow
          path: |
            .code-mower/standalone-doctor.json
            .code-mower/standalone-wrapper-rehearsal.json
            .code-mower/package-install-rehearsal.json
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
    (
        "code-mower migration package-install-rehearsal "
        "--package-spec git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.44 "
        "--repo-path /path/to/product-repo --json"
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


