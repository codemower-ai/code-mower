# First-User Install Rehearsal

This is the release-gate rehearsal for Code Mower's early-adopter path. It
installs Code Mower into a clean virtual environment, creates a fresh toy Git
repository, runs the easy-mode setup, runs the first-run doctor, generates a
starter reviewer value report, and proves the optional cloud path stays dry-run
until explicitly confirmed.

The goal is simple: before a release is widened, a maintainer should be able to
prove the first-user path without relying on any local repository history.

## What It Proves

The rehearsal verifies:

- package installation into a clean virtual environment;
- `code-mower --version`;
- first-user-focused `code-mower --help` output;
- `code-mower init --easy --apply`;
- generated smoke tests;
- `code-mower doctor --easy`;
- recommended next-step output;
- standalone wrapper behavior;
- optional external-repo readiness when `--repo-path` points at a repository
  without Code Mower wrapper files;
- starter calibration plan, evidence, metrics, lane policy, and value report;
- draft calibration auto-discovery can turn recent PR metadata into a reviewable
  corpus without promoting it to ground truth;
- cloud export bundle creation;
- cloud upload dry run; and
- CodeMower.com dogfood dry run; and
- a first-user readiness scorecard summarizing the gates above.

No source code, raw diffs, model transcripts, auth output, or secrets are
uploaded. The cloud upload and dogfood checks are dry-run-only in this rehearsal.
Public CI runs this rehearsal from the current checkout, and release candidates
should also run it against the exact public tag or package-index candidate before
being widened. Package-index rehearsals require `--allow-package-index`; add
`--upgrade-pip` only for deliberate release/integration checks that should also
touch the package index for pip itself.

## Canonical Command

Use the current public tag or release candidate:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --python "$(command -v python3.12)" \
  --json
```

The rehearsal accepts command names such as `--python python3.12` when they are
on `PATH`, but release notes and copied logs should prefer the absolute
`command -v` form so it is obvious which interpreter was used.

For a source checkout, run the same command from the checkout with a local
package spec:

```bash
scripts/dev-python -m code_mower.migration package-install-rehearsal \
  --package-spec . \
  --python "$(command -v python3.12)" \
  --json
```

For a fixed output directory:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --python "$(command -v python3.12)" \
  --work-dir /tmp/code-mower-first-user-rehearsal \
  --json
```

For a TestPyPI candidate, use the exact version published to TestPyPI:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==<candidate-version> \
  --allow-package-index \
  --upgrade-pip \
  --pip-index-url https://test.pypi.org/simple/ \
  --pip-extra-index-url https://pypi.org/simple/ \
  --python "$(command -v python3.12)" \
  --json
```

For a GitHub tag fallback, pass the tag URL explicitly:

```bash
code-mower migration package-install-rehearsal \
  --package-spec "git+https://github.com/codemower-ai/code-mower.git@v0.9.3-beta.1" \
  --python "$(command -v python3.12)" \
  --json
```

## Cache-Bypass And Local Wheel Checks

When a release has just been published, use a cache-bypassing install before
deciding the package index or the release is broken. For pipx:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==0.9.3b1
code-mower --version
```

For uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==0.9.3b1
code-mower --version
```

Before PyPI or TestPyPI has the candidate, validate the local wheel from the
release checkout:

```bash
scripts/dev-python -m build
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" dist/code_mower-*.whl
uv tool install --python 3.12 --reinstall dist/code_mower-*.whl
```

If exact-version installs repeatedly report "no matching distribution" or index
timeouts right after publication, record that as PyPI/TestPyPI propagation and
retry before changing source. If the install succeeds but `code-mower --version`
is wrong, the CLI fails to start, or this rehearsal fails, treat that as a
release blocker.

## External Repo Rehearsal

Use `--repo-path` to prove the installed Code Mower CLI can inspect a real
repository after the package install succeeds:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --repo-path /path/to/external-repo \
  --python "$(command -v python3.12)" \
  --json
```

When the target repository does not contain `tools/code_mower`, Code Mower
does not try to run mirror-removal or wrapper-parity checks. Instead it records
`external_repo_readiness` by running:

```bash
code-mower checks detect --repo-path /path/to/external-repo --json
code-mower checks run --repo-path /path/to/external-repo --dry-run --json
code-mower doctor --easy --json
```

This is the right path for early adopters and private-repo pilots such as
mobile apps or web apps that have never installed Code Mower before.

## Required Setup Failure Transcript

A fresh product repository should fail preflight until the human automation
token, branch-protection source, and repository auto-merge settings are fixed.
The important failures look like this in the doctor JSON:

```bash
code-mower doctor --preflight --json
```

```json
{
  "name": "github.human_automation_token",
  "status": "fail",
  "message": "OWNER/REPO is missing the DISPATCH_TOKEN human automation token secret",
  "remediation": "Create one human-owned fine-grained PAT secret with `gh secret set DISPATCH_TOKEN`. Grant repository Contents read, Issues read/write, and Pull requests read/write."
}
```

After the token is present, a branch-protection rule bound to the Actions
check-run instead of the Code Mower commit status is also a failure:

```json
{
  "name": "github.branch_protection",
  "status": "fail",
  "message": "OWNER/REPO@main requires code-mower/gate from GitHub Actions instead of Any source",
  "detail": {
    "required_status_check_bindings": [
      {
        "context": "code-mower/gate",
        "app_id": 15368
      }
    ]
  },
  "remediation": "Rebind `code-mower/gate` in branch protection to Any source, not GitHub Actions."
}
```

The correct branch-protection API shape has `"app_id": null` for
`code-mower/gate`. The repository must also allow auto-merge:

```json
{
  "name": "github.repo.auto_merge",
  "status": "fail",
  "message": "OWNER/REPO does not allow auto-merge",
  "remediation": "Enable repository auto-merge with `gh api -X PATCH repos/OWNER/REPO -f allow_auto_merge=true`."
}
```

These failures are intentional first-run blockers. They prevent the "all checks
green, nothing merges" state caused by bot-authored automation events or by
binding branch protection to the wrong status source.

## Product Repo Comparison

When a product repository already has Code Mower wrapper files, the same
`--repo-path` option compares the installed package against those wrappers:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --repo-path /path/to/product-repo \
  --python "$(command -v python3.12)" \
  --json
```

That wrapper comparison is useful during mirror-removal migrations. It is not
required for a new Code Mower user.

## Output Artifacts

The JSON payload includes `first_user_artifacts` with paths to:

- `.code-mower/calibration-plan.json`
- `.code-mower/draft-calibration-corpus.json`
- `.code-mower/draft-reviewer-value-report.md`
- `calibration-evidence.json`
- `reviewer-metrics.json`
- `lane-policy.json`
- `reviewer-value-report.md`
- `cloud-export.json`
- `cloud-upload-dry-run.json`
- `cloud-dogfood-dry-run.json`

The JSON payload also includes `first_user_readiness`, a compact scorecard with
one row per first-user gate. It is written separately to:

```text
outputs/first-user-readiness.json
```

This is the easiest artifact to attach to release notes or CI logs because it
answers "what did this release prove?" without requiring someone to read every
command log.

The full rehearsal payload is also written to:

```text
outputs/package-install-rehearsal.json
```

## Passing Criteria

Treat the rehearsal as passing only when:

- `status` is `pass`;
- `first_user_readiness.status` is `pass`;
- every step has `returncode` 0;
- the value report path exists;
- external repo readiness is `pass` when `--repo-path` points at a repo without
  Code Mower wrapper files;
- cloud upload reports dry-run mode;
- dogfood reports dry-run mode; and
- no step output contains secrets, raw source, or raw model transcripts.

If this fails, fix the first-user path before cutting or promoting a release.

## v0.9 Beta Package-Index Release Procedure

Publish and rehearse the package-index artifacts in this order. After the
release tag exists at the release commit, dispatch both package-index
publication runs with
`--ref v0.9.3-beta.1`; never substitute mutable `main`, because the TestPyPI
and production PyPI builds must check out identical source.

First, run `release.yml` for TestPyPI only:

```bash
gh workflow run release.yml \
  --repo codemower-ai/code-mower \
  --ref v0.9.3-beta.1 \
  -f publish_testpypi=true \
  -f publish_pypi=false
```

After that workflow run finishes, record its workflow run link and rehearse the
candidate from TestPyPI:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --upgrade-pip \
  --pip-index-url https://test.pypi.org/simple/ \
  --pip-extra-index-url https://pypi.org/simple/ \
  --python "$(command -v python3.12)" \
  --work-dir /tmp/code-mower-v092-beta1-testpypi-rehearsal \
  --json
```

Then run `release.yml` for production PyPI only:

```bash
gh workflow run release.yml \
  --repo codemower-ai/code-mower \
  --ref v0.9.3-beta.1 \
  -f publish_testpypi=false \
  -f publish_pypi=true
```

After that workflow run finishes, record its workflow run link and rehearse the
production package from PyPI:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --upgrade-pip \
  --python "$(command -v python3.12)" \
  --work-dir /tmp/code-mower-v092-beta1-pypi-rehearsal \
  --json
```

For each package-index run, record the workflow run link, rehearsal command,
JSON output path, status, installed version, first-user readiness counts, and
package source after execution. The workflow run links are the publication
evidence; the rehearsal JSON is the install-path evidence.

After production PyPI rehearsal passes, repeat the package-install rehearsal
against the private
[DrinkBetter-AI/mobile-app](https://github.com/DrinkBetter-AI/mobile-app)
repository:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.3b1 \
  --allow-package-index \
  --repo-path "$REPO_PATH" \
  --work-dir "$WORK_DIR" \
  --json
```

Record status, first-user readiness counts, external repo readiness, wrapper
presence, and detected repository-native checks after execution. That run is
the proof that a private JavaScript/mobile repo can try Code Mower from PyPI,
detect its native check surface, and preview setup without first adopting
repo-local wrappers.
