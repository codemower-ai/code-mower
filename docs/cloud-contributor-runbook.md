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

## Codex: Code Mower Work

From the Code Mower checkout:

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

## Codex: Product Repository Work

Use a product-specific Codex token and the product repo slug:

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

## Safety Rules

- Always run `cloud doctor` before `cloud upload --yes`.
- Always run a dry run before the real upload.
- Leave `--include-reports` off unless the report contents have been inspected
  and are intentionally shareable.
- Revoke a token immediately if it is pasted into chat, committed, or exposed in
  logs.
