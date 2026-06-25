# First-User Demo Transcript

This is a sanitized transcript of the first-user install rehearsal shape. It is
designed for someone evaluating Code Mower for the first time: no private repo,
no provider tokens, no uploads, and no hidden local machine assumptions.

The transcript uses `$WORK_DIR` instead of a real local path.

## Command

```bash
python3.12 -m venv "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/python" -m pip install --upgrade pip
"$WORK_DIR/venv/bin/python" -m pip install code-mower==0.5.0b36
"$WORK_DIR/venv/bin/code-mower" migration package-install-rehearsal \
  --package-spec code-mower==0.5.0b36 \
  --python "$(command -v python3.12)" \
  --json
```

## Shortened Output

```json
{
  "mode": "package-install-rehearsal",
  "status": "pass",
  "steps": 27,
  "package_spec": "code-mower==0.5.0b36",
  "toy_repo": "$WORK_DIR/toy-repo",
  "doctor_status": "warn",
  "generated_artifacts": {
    "calibration_plan": true,
    "draft_calibration_corpus": true,
    "draft_reviewer_value_report": true,
    "calibration_evidence": true,
    "reviewer_metrics": true,
    "lane_policy": true,
    "value_report": true,
    "cloud_export_bundle": true,
    "cloud_upload_dry_run": true,
    "dogfood_dry_run": true
  },
  "cloud_upload_mode": "cloud-upload-dry-run",
  "dogfood_status": "dry_run"
}
```

## What The Warning Means

`doctor_status=warn` is expected in a clean toy repository. The rehearsal does
not require provider credentials, GitHub repository permissions, or a Code
Mower Cloud token. Warnings are setup guidance; failed checks are the release
blocker.

## What Was Proven

- Code Mower installed into a clean virtual environment.
- A fresh toy Git repository could run `init --easy`.
- Generated smoke tests passed.
- `doctor --preflight` completed without failed checks.
- Calibration plan, evidence, metrics, lane policy, and value report artifacts
  were generated.
- Draft auto-discovery corpus and draft reviewer value report artifacts were
  generated from offline PR metadata.
- Cloud export produced an inspectable metadata bundle.
- Cloud upload and dogfood stayed dry-run without `--yes`.

## What Was Not Sent

The rehearsal did not upload data. It also did not include source code, raw
diffs, raw transcripts, auth probe output, or secrets in any shareable bundle.

For the release-gate command sequence, see
[First-User Install Rehearsal](first-user-install-rehearsal.md).
