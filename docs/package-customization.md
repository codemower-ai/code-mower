# Code Mower Package Customization

Code Mower should be headless by default, but not opaque. The package exposes
three review-runtime customization surfaces that teams can edit without changing
provider wrappers.

## Prompt Lenses

Prompt lenses live in `tools/lane_prompts/` in the reference repo and in
`src/code_mower/templates/lane_prompts/` after package extraction. A lane can
load one or more lenses with `CODE_MOWER_REVIEW_LENSES` or the lane-specific
wrapper flag.

Use lenses for review doctrine, not PR content. Good lenses describe what to
catch, what to ignore, severity policy, and when to return a terse clean pass.
They should not mention external inspiration sources, private conversation
threads, or one-off implementation history.

Recommended package overrides:

- Keep `base-audit` as the default correctness lens.
- Add `calibration-policy` for calibration corpus, value-report, lane policy,
  spend, latency, and reviewer disposition changes.
- Add `docs-design` for roadmap, PRD, architecture, or process PRs.
- Add `package-runtime` for bootstrap, provider catalog, CLI dispatch, prompt,
  artifact, and extracted-package changes.

Inspect and validate packaged lenses with:

```bash
code-mower prompts list --json
code-mower prompts show base-audit --json
code-mower prompts validate --lenses base-audit,calibration-policy,package-runtime --json
```

## Context Packs

Context packs are bounded manifests for surrounding files. They let a review
lane request important context explicitly instead of stuffing every audit prompt
with full file contents.

Example:

```json
{
  "repo": "owner/repo",
  "pr_number": 123,
  "head_sha": "abc123",
  "changed_files": [
    {"filename": "tools/code_mower_cli.py"},
    {"filename": "tools/code_mower_package.py"}
  ],
  "packs": [
    {
      "id": "package-runtime",
      "reason": "package import and CLI context",
      "include": ["tools/code_mower_*.py"],
      "extra_files": [
        {"repo": "owner/related-backend", "path": "app.py"}
      ],
      "max_files": 8,
      "max_file_bytes": 60000
    }
  ]
}
```

Render the plan with:

```bash
code-mower context-packs templates/context-packs.example.json --json
```

The plan lists file paths and byte caps. It does not read file contents. A lane
runner can materialize only the packs it needs:

```bash
code-mower context-packs templates/context-packs.example.json \
  --write \
  --output-dir .code-mower/context-packs \
  --repo-root-map owner/related-backend=/path/to/related-backend \
  --json
```

Materialization writes bounded file copies plus
`.code-mower/context-packs/context-pack-manifest.json`. Use `--require-files`
when missing context should fail the lane instead of producing a warning.
Related-repo files are written under a reserved `_repos/<owner__repo>/`
artifact directory, so `owner/related-backend:app.py` cannot collide with
primary-repo paths such as `owner__related-backend/app.py`.

The standalone package ships `templates/context-packs.example.json` as the
starter customization file. It includes generic starter packs for recurring
classes such as `auth-context`, `history-policy`, `ios-solver-runtime`,
`package-runtime`, `calibration-policy`, `prompt-lenses`, `debug-upload-*`, and
`build-info`. Keep context pack ids stable enough to reference from calibration
corpus entries; changing an id breaks historical run comparisons.

## Calibration And Metrics

Use `calibration run` to collect reviewer outputs, calibration dispositions to
adjudicate them, and reviewer metrics to compare accuracy and value.

Set up local Gemini auth without committing secrets:

```bash
code-mower init auth gemini --from-stdin --print-shell
export GEMINI_API_KEY_FILE="$HOME/.config/code-mower/gemini.env"
```

`code-mower doctor --probe-runtime` runs a small Gemini sentinel prompt when
`gemini-cli` is selected. The shareable JSON report records whether the CLI
returned parseable JSON and the expected sentinel, but it redacts raw CLI output
because provider CLIs can print account state, local paths, or auth hints. The
doctor smoke uses Gemini's non-interactive trust bypass for the throwaway
working directory; full audit runs already use the same explicit headless trust
flag for stdin transport.

For JSON-speaking provider probes, configure `doctor_probe_error_fields` for
fields that indicate provider/API failure and `doctor_probe_auth_status_fields`
for fields that carry HTTP-style auth status codes. Doctor reports expose only
sanitized `401`/`403` auth status codes and content-free flags, not raw
provider-supplied error strings.

Google's June 18, 2026 migration makes Gemini CLI a legacy/compatibility lane
for most individual free/Pro/Ultra setups. Keep it for historical comparison
and enterprise/API-key continuity, but prefer Antigravity CLI for new Google
provider calibration once the local `agy` path is authenticated and stable.
See:
<https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/>.

When running historical or multi-repo calibration, pass one
`--repo-path-map` flag per mapped checkout:

```bash
code-mower calibration run templates/calibration-corpus.json \
  --repo-path-map owner/repo#123@abc123=/tmp/repo-pr-123 \
  --repo-path-map owner/other-repo#456@def456=/tmp/other-pr-456 \
  --context-pack-manifest templates/context-packs.example.json \
  --results-dir .code-mower/calibration-results
```

When `--context-pack-manifest` is provided, corpus entries with
`context_packs` materialize only the named packs from their mapped local PR
checkout. Supported local CLI lanes receive a generated `--context-pack-file`
that is included in the review prompt. Missing files are warnings by default;
add `--require-context-pack-files` for experiments where missing context should
invalidate the run.

Do not join mappings with commas; each flag value is parsed as one complete
mapping.

## Private Standalone Shadow Workflow

The package includes `templates/workflows/private-standalone-shadow.yml.j2` for
product repos that consume a private Code Mower source checkout before public
package publication. It expects:

- `tools/code_mower` and `tools/code_mower_standalone_shadow.sh` in the product
  repo;
- a pinned `tools/code_mower_standalone_pin.env`;
- `code_mower_standalone_repo_url` rendered to the source checkout URL for your
  standalone Code Mower repository, such as an SSH deploy-key URL while the
  source repo is private;
- `code_mower_standalone_package_repo_url` rendered to the pip-installable form
  of the same source, such as
  `git+ssh://git@github.com/OWNER/code-mower.git`;
- a read-only deploy key on the standalone Code Mower repository; and
- the private half of that key stored as
  `CODE_MOWER_STANDALONE_DEPLOY_KEY` in the product repo's Actions secrets.

The workflow fetches the pinned standalone checkout over SSH, runs
`doctor --easy`, compares safe read-only commands between the pinned standalone
package and the repo-local mirror, and then runs `package-install-rehearsal`
from the same pinned ref using a `git+ssh://...@REF` package spec. It is a
proof and migration guard, not a reviewer lane or merge gate.

When adapting the workflow for a private standalone repository, keep the SSH
repository URL in the job environment, prefer an explicit
`CODE_MOWER_STANDALONE_REF` workflow override when present, and otherwise read
only `CODE_MOWER_STANDALONE_REF` from `tools/code_mower_standalone_pin.env`
before the package-install step. Sourcing the whole pin file can overwrite the
SSH deploy-key URL with an HTTPS URL and break the next wrapper invocation.
The generated workflow exposes `CODE_MOWER_STANDALONE_PACKAGE_REPO_URL` so that
private repos can keep the package-install rehearsal on the authenticated
`git+ssh` path while public repos can use `git+https`.

For a public standalone repository, the same rehearsal can use a pin-derived
HTTPS package spec:

```bash
code_mower_ref="$(sed -n 's/^CODE_MOWER_STANDALONE_REF="\([^"]*\)"/\1/p' tools/code_mower_standalone_pin.env)"
code_mower_repo="$(sed -n 's/^CODE_MOWER_STANDALONE_REPO_URL="\([^"]*\)"/\1/p' tools/code_mower_standalone_pin.env)"
package_spec="git+${code_mower_repo}@${code_mower_ref}"
```

During mirror removal, keep the thin product wrapper and pin files in place.
Move GitHub workflow calls from mirrored scripts to standalone wrapper commands
before deleting implementation files:

```bash
tools/code_mower trailer-comment-labeler --lane codex
tools/code_mower saas-reviewer-labeler --adapter gitar
tools/code_mower bootstrap --print-python
```

If the standalone Code Mower source repository is still private and the product
repo's GitHub Actions jobs do not have authenticated standalone checkout,
workflow entrypoints should temporarily use the explicit local fallback:

```bash
CODE_MOWER_USE_LOCAL=1 tools/code_mower trailer-comment-labeler --lane codex
CODE_MOWER_USE_LOCAL=1 tools/code_mower saas-reviewer-labeler --adapter gitar
CODE_MOWER_USE_LOCAL=1 tools/code_mower bootstrap --print-python
```

That fallback keeps private-repo labeler workflows from trying to clone the
standalone repository over unauthenticated HTTPS, but it also means mirrored
repo-local implementation files are still required. Before removing mirrors,
run:

```bash
code-mower migration package-install-rehearsal \
  --package-spec "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.47" \
  --repo-path /path/to/product-repo \
  --json
```

During alpha testing, `--package-spec` can be a local path or git URL. The
rehearsal installs Code Mower non-editably in a clean venv, proves the
easy-mode starter path in a fresh toy repo, then compares product-wrapper
behavior against the installed package. `migration mirror-removal-plan` treats
local fallback workflow calls as mirror-removal blockers until the standalone
package is public/package-installable or the workflows have authenticated
standalone access.

`tools/code_mower`, `tools/code_mower_standalone_shadow.sh`, and
`tools/code_mower_standalone_pin.env` are migration support files. They should
remain in product repos until the package is installed through a normal public
dependency path.

The Antigravity CLI lane uses the local `agy` authentication state created by
`agy install`/login. Because that OAuth state currently lives in the operator's
normal home directory, Code Mower fails closed by default: set
`ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1` only in a trusted local environment where
you accept inheriting the local Antigravity config, with Code Mower still passing
`--sandbox` and a prompt-file workspace. Verify the CLI itself with
`agy -p "Reply with exactly: ok"`. Prefer `antigravity-cli` for new Google CLI
calibration once the `agy` command is installed, and keep `gemini-cli` for
compatibility with earlier local runs.

The Antigravity doctor probe is currently a version check, not a paid model
call. Run a full calibration or audit command when you want evidence about the
authenticated review path.

The Hermes CLI lane uses local Hermes Agent authentication created by
`hermes setup`. Because Hermes currently relies on local session/config state,
Code Mower fails closed unless the operator explicitly opts into inheriting that
trusted local state:

```bash
hermes setup
hermes --oneshot "Reply with exactly: ok"
export HERMES_CLI_USE_AMBIENT_HOME=1
```

Code Mower still passes `--ignore-user-config`, `--ignore-rules`, and an empty
`--toolsets` list for the audit run, and it uses Hermes' `@prompt-file`
context-reference expansion so the full audit prompt is not placed in process
argv.
Treat `hermes-cli` as an informational calibration lane until it has
known-clean, known-blocked, latency, and spend evidence in the value report.

The Hermes doctor probe is currently a version check. It confirms the CLI and
trusted ambient-home opt-in are wired, while calibration runs measure reviewer
quality and cost.

```bash
code-mower calibration plan templates/calibration-corpus.json \
  --replicates 2 \
  --json

code-mower calibration run templates/calibration-corpus.json \
  --lanes antigravity-cli,gemini-cli,hermes-cli,coderabbit-cli,local-llm \
  --repo-path-map owner/repo#123@HEAD_SHA=/path/to/pr-worktree \
  --results-dir .code-mower/calibration-results \
  --json

code-mower calibration evidence templates/calibration-corpus.json --json

code-mower reviewer-metrics calibration.json \
  --spend templates/reviewer-spend.example.json \
  --json

code-mower calibration value-report templates/calibration-corpus.json \
  --runs .code-mower/calibration-results/calibration-run-results.json \
  --output reviewer-value-report.md
```

The runner persists raw command arguments, stdout, stderr, lane summaries, and a
single `calibration-run-results.json` manifest. Treat raw stdout/stderr files as
local debugging artifacts because reviewers may print account state, local paths,
or token-shaped strings. Share the manifest and value report by default; the
manifest records paths and structured summaries, while doctor auth probes redact
probe output content. Feed the manifest back into the value report with `--runs`;
terminal output alone is not durable calibration evidence.

`--repo-path-map` accepts `owner/repo=PATH`, `owner/repo#PR=PATH`,
`owner/repo@HEAD=PATH`, and `owner/repo#PR@HEAD=PATH`. Prefer the specific forms
for multi-PR corpora so each archived PR head uses the matching clean worktree.
If a corpus item includes `base_ref`, generated Antigravity CLI, Gemini CLI,
Hermes CLI, CodeRabbit CLI, and local LLM commands pass it through as
`--base-ref`, which keeps historical calibration diffs reproducible.

Spend reports can be a plain profile-to-USD mapping or a `profiles` object:

```json
{
  "profiles": {
    "gemini-cli": {"cost_usd": 0.42},
    "coderabbit-cli": {"cost_usd": 0.0}
  }
}
```

The value report includes cost per run, seconds per run, cost per useful
finding, and seconds per known-blocked catch when those inputs are available.

For archived or merged PR heads, use a detached clean checkout plus
`--allow-historical-head`:

```bash
code-mower gemini-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-head \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --context-pack-file .code-mower/context-packs/pr-123/context-pack.txt \
  --output-dir .code-mower/calibration/pr-123/gemini-cli \
  --json

code-mower antigravity-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-head \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --context-pack-file .code-mower/context-packs/pr-123/context-pack.txt \
  --output-dir .code-mower/calibration/pr-123/antigravity-cli \
  --json

code-mower hermes-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-head \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --historical-calibration \
  --context-pack-file .code-mower/context-packs/pr-123/context-pack.txt \
  --output-dir .code-mower/calibration/pr-123/hermes-cli \
  --json
```

Treat these reports as evidence for lane promotion, trigger policy, and spend
policy. Corpus items can set `review_class` and `context_packs`; generated policy
uses those fields to distinguish routine merge-gate candidates from selective
package/runtime, auth, docs/design, or calibration-policy triggers. They are not
automatic merge authority by themselves.

Product-repo compatibility wrappers for structured Codex and Claude audits can
also save the exact public verdict comment before attempting to post it. If a
network hiccup or GitHub error interrupts posting, replay the saved artifact
instead of rerunning the model:

```bash
tools/run_codex_audit_pr.sh --repost-verdict-artifact /path/to/verdict.json
tools/run_claude_audit_pr.sh --repost-verdict-artifact /path/to/verdict.json
```

In mirror-removal mode, those shell wrappers should be thin compatibility
shims around the standalone package:

```bash
tools/code_mower codex-audit --repo OWNER/REPO --pr 123
tools/code_mower claude-audit --repo OWNER/REPO --pr 123
tools/code_mower codex-audit-env-preflight
tools/code_mower codex-audit-schema-smoke
```

This keeps token handling and existing operator commands in the product repo
while moving reviewer implementation ownership into the package.

By default, verdict artifacts live under
`~/.cache/code-mower-audits/verdicts/`. Use
`CODE_MOWER_VERDICT_ARTIFACT_DIR` in CI or package installs when that cache
should be pinned to a workspace-owned state directory.

## Cloud Benchmark Export And Upload Dry Run

The package should deliver value locally without cloud upload. Export a local,
inspectable benchmark bundle with:

```bash
code-mower cloud export \
  --report reviewer-metrics=reviewer-metrics.json \
  --report lane-policy=lane-policy.json \
  --report value-report=reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --json
```

The bundle excludes source code, raw diffs, raw model transcripts, raw
stdout/stderr, auth output, and secrets by default. Preview the optional cloud
handoff without sending data:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Network transfer requires `--yes`. Report text is excluded from the upload
payload unless the caller explicitly adds `--include-reports`.

## Merge Command Planning

Use `merge-plan` when a PR already satisfies the repository merge bar and you
want repo-scoped GitHub commands that do not depend on the current local
checkout:

```bash
code-mower merge-plan owner/repo#123 --json
```

The rendered commands use `gh pr ... --repo owner/repo` and
`gh api repos/owner/repo/...` for post-merge verification. The planner is a
command generator only; it does not merge, grant merge authority, or replace
the audit protocol.
