# v0.5.0-alpha.9 First-User Install Rehearsal

This is the recorded release-gate shape for the `v0.5.0-alpha.9` candidate.
It proves that the package can install into a clean virtual environment, run
easy mode in a fresh toy repository, generate the starter report path, bootstrap
a draft project corpus from PR metadata, generate a draft reviewer value report,
and exercise both lower-level cloud upload preview and routine dogfood preview
without uploading.

The public tag should be rechecked with the same command sequence immediately
after release.

## Install Candidate

```bash
python3.12 -m venv "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/python" -m pip install --upgrade pip
"$WORK_DIR/venv/bin/python" -m pip install "git+https://github.com/codemower-ai/code-mower.git@codex/v050-alpha9-release"
"$WORK_DIR/venv/bin/code-mower" --version
```

Expected:

```text
code-mower 0.5.0a9
```

## Fresh Toy Repository

```bash
mkdir "$WORK_DIR/toy-repo"
cd "$WORK_DIR/toy-repo"
git init
git config user.email code-mower-smoke@example.com
git config user.name "Code Mower Smoke"
printf '# Toy Repo\n' > README.md
git add README.md
git commit -m 'Initial commit'
```

## First-User Flow

```bash
"$WORK_DIR/venv/bin/code-mower" providers list
"$WORK_DIR/venv/bin/code-mower" init --easy --apply --output-dir .code-mower.generated --json
bash .code-mower.generated/smoke-tests.sh
"$WORK_DIR/venv/bin/code-mower" doctor --preflight --json
"$WORK_DIR/venv/bin/code-mower" next-steps --profile recommended --json
```

## Starter Value Report And Cloud Preview

```bash
"$WORK_DIR/venv/bin/code-mower" calibration plan .code-mower.generated/calibration-corpus.json --replicates 2 --json
"$WORK_DIR/venv/bin/code-mower" calibration evidence .code-mower.generated/calibration-corpus.json --json > calibration-evidence.json
"$WORK_DIR/venv/bin/code-mower" reviewer-metrics calibration-evidence.json --spend .code-mower.generated/reviewer-spend.json --json > reviewer-metrics.json
"$WORK_DIR/venv/bin/code-mower" calibration policy reviewer-metrics.json --json > lane-policy.json
"$WORK_DIR/venv/bin/code-mower" calibration value-report .code-mower.generated/calibration-corpus.json --spend .code-mower.generated/reviewer-spend.json --output reviewer-value-report.md
"$WORK_DIR/venv/bin/code-mower" cloud export \
  --report reviewer-metrics=reviewer-metrics.json \
  --report lane-policy=lane-policy.json \
  --report value-report=reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --json
"$WORK_DIR/venv/bin/code-mower" cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
"$WORK_DIR/venv/bin/code-mower" cloud dogfood \
  --repo-path "$WORK_DIR/toy-repo" \
  --repo-slug example/toy-repo \
  --source first-user-alpha9-smoke \
  --endpoint http://localhost:3000/api/ingest \
  --json
```

Expected dogfood result:

```json
{
  "mode": "cloud-dogfood",
  "status": "dry_run"
}
```

## Draft Auto-Discovery Rehearsal

The package-install rehearsal also creates an offline PR-list fixture at
`.code-mower/auto-discovery-prs.json`, then proves:

```bash
"$WORK_DIR/venv/bin/code-mower" calibration auto-discover \
  --repo example/toy-repo \
  --input .code-mower/auto-discovery-prs.json \
  --output .code-mower/draft-calibration-corpus.json \
  --json
"$WORK_DIR/venv/bin/code-mower" calibration value-report .code-mower/draft-calibration-corpus.json --output .code-mower/draft-reviewer-value-report.md
```

In a real repository, use `--last-n 20` instead of `--input` to inspect recent
merged PRs through GitHub. The generated corpus is draft evidence; a human
should confirm every disposition before lane promotion.

## Result

Status: candidate branch rehearsal passed on 2026-06-14.

Observed summary:

```text
code-mower 0.5.0a9
doctor_status=warn
dogfood_status=dry_run
cloud_upload_mode=cloud-upload-dry-run
value_report_lines=35
draft_corpus_present=true
draft_value_report_present=true
```

The `doctor --preflight` warning state is expected in a clean toy repository
without configured provider credentials, GitHub auth, or cloud token. It is
acceptable for this rehearsal as long as no check reports `fail`.

Completion criteria:

- Fresh install reports `code-mower 0.5.0a9`.
- Generated smoke tests pass.
- `doctor --preflight` exits without failed checks.
- Starter value report is generated.
- Draft auto-discovery corpus is generated and clearly treated as human-review
  input.
- Draft value report is generated from the auto-discovered corpus.
- Lower-level `cloud upload --dry-run` previews without a token.
- Routine `cloud dogfood` previews without network upload unless `--yes` is
  supplied.
- Re-run the same flow against
  `git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.9` after the
  public tag is cut.
