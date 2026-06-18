# Code Mower Cloud Contributor Runbook

This runbook is for agents and humans who already run Code Mower locally and
want to contribute sanitized benchmark metadata to Code Mower Cloud.

The default posture is metadata-only. Do not upload source code, raw diffs, raw
model transcripts, raw stdout/stderr, auth probe output, or secrets.

## Shared Endpoint

Use the production ingest endpoint:

```bash
export CODE_MOWER_CLOUD_ENDPOINT="https://codemower.com/api/ingest"
```

Create or manage team ingest tokens from:

```text
https://codemower.com/login
https://codemower.com/dashboard
```

If OAuth providers are not yet enabled for the account you need, ask an
operator to issue a team token through the server-side admin fallback, then
replace it with a self-service dashboard token when login is available.

## Token Files

Each agent/surface should have its own team ingest token. Recommended local
layout:

```text
~/.config/code-mower/tokens/codex-code-mower.env
~/.config/code-mower/tokens/codex-product.env
~/.config/code-mower/tokens/claude-product-web.env
~/.config/code-mower/tokens/claude-product-ios.env
~/.config/code-mower/tokens/claude-product-android.env
```

Each file should be mode `0600` and contain:

```bash
export CODE_MOWER_CLOUD_TOKEN="<issued-team-ingest-token>"
export CODE_MOWER_INSTALL_ID="codex-code-mower"
export CODE_MOWER_CLOUD_TEAM_ID="<team-slug>"
export CODE_MOWER_CLOUD_ENDPOINT="https://codemower.com/api/ingest"
```

Create one with the CLI after issuing a dashboard token:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "<team-slug>" \
  --install-id "codex-code-mower" \
  --out ~/.config/code-mower/tokens/codex-code-mower.env
```

The command writes a private env file and prints only a token prefix. Use
`--force` only when intentionally rotating a token.

## Codex: Code Mower Work

From the Code Mower checkout:

```bash
source ~/.config/code-mower/tokens/codex-code-mower.env

code-mower cloud dogfood --source codex-code-mower --json
code-mower cloud dogfood --source codex-code-mower --yes --json
```

The explicit export path is still available when you want to choose report
files by hand:

```bash
source ~/.config/code-mower/tokens/codex-code-mower.env

code-mower cloud export \
  --report lane-policy=docs/lane-promotion-policy.md \
  --report value-report=docs/reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --repo-slug OWNER/REPO \
  --team-id "$CODE_MOWER_CLOUD_TEAM_ID" \
  --install-id "$CODE_MOWER_INSTALL_ID" \
  --json

code-mower cloud doctor .code-mower/cloud-benchmark-bundle --json
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

For a local operator session that has several active checkouts, use `repo-sync`
to preview dogfood plus reviewer-run metadata for each repo in one pass:

```bash
source ~/.config/code-mower/tokens/codex-code-mower.env

code-mower cloud repo-sync \
  --repo OWNER/CODE_MOWER_REPO=/path/to/code-mower \
  --repo OWNER/CLOUD_REPO=/path/to/codemower.com \
  --repo OWNER/PRODUCT_REPO=/path/to/product-repo \
  --json
```

Rerun with `--yes` only after the dry run is clean. `--mode` is exact
selection, so include every desired mode when you intentionally want recent
GitHub Actions history as sanitized backfill:

```bash
code-mower cloud repo-sync \
  --repo OWNER/CODE_MOWER_REPO=/path/to/code-mower \
  --repo OWNER/CLOUD_REPO=/path/to/codemower.com \
  --repo OWNER/PRODUCT_REPO=/path/to/product-repo \
  --mode dogfood \
  --mode reviewer-runs \
  --mode catch-up \
  --json
```

Use the modes deliberately:

- `dogfood` uploads current repo metadata and current shareable reports;
- `reviewer-runs` uploads existing local verdict artifacts; and
- `catch-up` uploads sanitized GitHub Actions history.

Routine dogfood workflows are not historical benchmark imports. If a repo has
useful history before cloud sharing was enabled, run `code-mower cloud catch-up`
or `code-mower cloud repo-sync --mode catch-up` explicitly, then use dogfood for
ongoing current-state uploads.

## Codex: Product Repository Work

Use a product-specific Codex token and the product repo slug:

```bash
source ~/.config/code-mower/tokens/codex-product.env

code-mower cloud dogfood \
  --repo-slug OWNER/PRODUCT_REPO \
  --source codex-product \
  --json

code-mower cloud dogfood \
  --repo-slug OWNER/PRODUCT_REPO \
  --source codex-product \
  --yes \
  --json
```

The explicit export path remains useful when product-specific report files are
available:

```bash
source ~/.config/code-mower/tokens/codex-product.env

code-mower cloud export \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --repo-slug OWNER/PRODUCT_REPO \
  --team-id "$CODE_MOWER_CLOUD_TEAM_ID" \
  --install-id "$CODE_MOWER_INSTALL_ID" \
  --json

code-mower cloud doctor .code-mower/cloud-benchmark-bundle --json
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

Prefer adding `--report reviewer-metrics=...`, `--report lane-policy=...`, or
`--report value-report=...` when those artifacts exist. Empty metadata bundles
are acceptable for smoke tests but less useful.

## Claude Sessions

Claude sessions should source the matching surface token:

```bash
source ~/.config/code-mower/tokens/claude-product-ios.env
```

Then run the same export, doctor, dry-run, and upload sequence. Use the repo
slug for the surface being worked:

- `OWNER/WEB_REPO` for web work;
- `OWNER/IOS_REPO` for iOS work; and
- `OWNER/ANDROID_REPO` for Android work.

Prefer `code-mower cloud dogfood --source claude-product-ios --repo-slug
OWNER/IOS_REPO --yes --json` when no custom report files need to be selected.

## GitHub Actions

For always-on dogfood uploads, add a low-cost workflow that runs on `main`
pushes and exits successfully when the token is not configured:

```yaml
env:
  CODE_MOWER_CLOUD_TOKEN: ${{ secrets.CODE_MOWER_CLOUD_TOKEN }}
  CODE_MOWER_CLOUD_TEAM_ID: ${{ vars.CODE_MOWER_CLOUD_TEAM_ID }}
  CODE_MOWER_INSTALL_ID: github-actions-code-mower
run: |
  if [ -z "${CODE_MOWER_CLOUD_TOKEN:-}" ]; then
    echo "CODE_MOWER_CLOUD_TOKEN is not configured; skipping."
    exit 0
  fi
  code-mower cloud dogfood --repo-slug "${GITHUB_REPOSITORY}" --source github-actions --yes --json
```

## Safety Rules

- Always run `cloud doctor` before `cloud upload --yes`.
- Always run a dry run before the real upload.
- Leave `--include-reports` off unless the report contents have been inspected
  and are intentionally shareable.
- Revoke a token immediately if it is pasted into chat, committed, or exposed in
  logs.
