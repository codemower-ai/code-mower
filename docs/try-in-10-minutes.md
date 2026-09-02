# Try Code Mower In 10 Minutes

This is path A from the README: get one repository to a merged, audited PR
without enabling the build loop. It is local-first and manual-first. Use
[Quickstart](quickstart.md) later as the reference for every command surface,
token, and optional cloud path.

If you want to see the output shape before installing, read the
[Demo Calibration Example](../examples/demo-calibration/README.md) and
[First-User Demo Transcript](first-user-demo-transcript.md). The demo is
synthetic and contains no source, raw diffs, raw transcripts, auth output, or
private repository names.

## 1. Install

Code Mower requires Python 3.12 or newer. See
[Install And Bootstrap](install.md) for pipx, uv, and contributor checkout
paths, plus upgrade and pipx-to-uv migration details.

Use this install matrix:

| Environment | Command shape |
| --- | --- |
| Laptop/workstation | `pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.0b1` |
| Hosted agent, CI box, or minimal Linux VM | `uv tool install --python 3.12 code-mower==0.9.0b1` |
| Code Mower contributor checkout | `scripts/dev-python -m venv .venv` then `.venv/bin/python -m pip install -e ".[test]"` |

For a cold laptop install:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.0b1
command -v code-mower
code-mower --version
```

For an existing install, first run `command -v code-mower` and
`code-mower --version`, then follow
[Cold Install Vs Upgrade](install.md#cold-install-vs-upgrade). If you switch
from pipx to uv, make sure the command on `PATH` is the one you meant to use
before running `init`.
For a repository that already has generated Code Mower support, follow
[Upgrade An Existing Repository](upgrade-existing-repo.md) before copying a new
`.code-mower.generated` tree.

`0.9.0b1` is a beta release. To follow the newest beta line instead of
pinning this exact build:

```bash
pipx install --python "$CODE_MOWER_PYTHON" --pip-args="--pre" code-mower
```

## 2. Authenticate GitHub

Use an account that can read the repository, open a setup PR, add labels, and
post audit comments.

```bash
gh auth login -h github.com -s repo,workflow,read:org
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
gh repo view OWNER/REPO
```

Set the repository slug once so the remaining commands can be copied:

```bash
export REPO=OWNER/REPO
export DEFAULT_BRANCH=main
```

## 3. Generate Reviewable Setup Output

Run this from a clean checkout of the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
```

`init --easy` is non-mutating by default. `--apply` writes a generated tree for
review in `.code-mower.generated`, creates the missing Code Mower labels when
GitHub access allows it, and still does not trigger reviewers or upload data.

The generated tree includes local Codex and Claude audit lanes, the
`code-mower/gate` workflow, stale-audit cleanup, owner escalation labels, and
starter calibration files. Keep this generated output reviewable: do not enable
paid or hosted lanes until your own calibration data supports them.
It also includes `.code-mower.generated/code-mower.yml`; edit the repository
slug, owner login, decision authorities, status issue, and trusted audit-comment
authors before copying it to the repository root.
The generated `smoke-tests.sh` should run without leaving bytecode caches or
other setup noise in your first PR.

## 4. Run The Preflight Doctor

```bash
code-mower doctor --adoption --repo "$REPO" --json
```

`--adoption` is the friendly first-run preset for a real repository target.
It includes preflight checks and reports when doctor is using packaged starter
defaults before `code-mower.yml` exists. `doctor --v05` remains the versioned
compatibility alias for scripts. The preset expands to the checks early
adopters need:

- recommended profile selection;
- Python/runtime checks;
- local provider CLI discovery and smoke probes when this machine will run
  local lanes;
- stale terminal-label hygiene for merge-authority reviewer lanes;
- GitHub repository visibility, permissions, branch protection, and Actions
  cost diagnostics; and
- optional Code Mower Cloud token setup diagnostics.

Warnings are setup guidance. They are only fatal when you pass `--strict`. In
JSON mode, check the top-level `run_plan` field first. It tells you whether the
preflight included GitHub and optional cloud checks before you inspect
individual provider warnings.
Use `--hosted-builders` or `--orchestrator-only` when this machine observes or
coordinates lanes without running Codex/Claude local audit wrappers; those
postures skip local-wrapper probes and keep missing local wrapper env vars out
of the warning list.

For merge-authority lanes such as Codex or Claude audit, look for
`provider.review_hygiene`. Before generated workflows are applied, it may warn
that workflow file presence was not verified. After the setup PR lands, it
should pass for lanes that can satisfy the merge bar, because it proves Code
Mower can clear stale terminal labels after a PR receives new commits.

## 5. Open The Setup PR

Create one small PR that installs the generated reviewer-gate support. This PR
is the first audited PR.

```bash
git switch -c chore/code-mower-reviewer-gate
cp -R .code-mower.generated/. .
git status --short
git add code-mower.yml .github tools calibration-corpus.json context-packs.json \
  reviewer-spend.json reviewer-value-report.example.md
git commit -m "chore: add code mower reviewer gate"
git push -u origin HEAD
gh pr create \
  --repo "$REPO" \
  --base "$DEFAULT_BRANCH" \
  --head "$(git branch --show-current)" \
  --title "chore: add Code Mower reviewer gate" \
  --body-file - <<'CM_BODY'
Install Code Mower generated reviewer-gate support for the first audited PR.
CM_BODY
export PR_NUMBER="$(gh pr view --repo "$REPO" --json number --jq .number)"
gh pr edit "$PR_NUMBER" --repo "$REPO" \
  --add-label needs-codex-audit \
  --add-label needs-claude-audit
```

If your repository already has files with the same names, review `git diff`
before committing and keep only the generated support files you actually intend
to enable. Existing Code Mower repositories should use
[Upgrade An Existing Repository](upgrade-existing-repo.md) so `repo-only`
wrappers, pins, and local shims are explicit review decisions.

The first setup PR is special: the generated workflows are not on the default
branch yet, so it cannot fully self-gate. Treat it as a manual pilot PR. Merge
only after the local audit comments are clean for the current head SHA and your
normal CI is green.

## 6. Run The Audits

The direct wrappers need a GitHub posting token and a separate checkout of the
PR head. For this first local run, use the token already held by `gh`.

```bash
export SUPPORT_PATH="$(pwd)"
export PR_HEAD_PATH="$(mktemp -d)"
gh repo clone "$REPO" "$PR_HEAD_PATH"
git -C "$PR_HEAD_PATH" fetch origin "pull/${PR_NUMBER}/head:code-mower-pr-${PR_NUMBER}"
git -C "$PR_HEAD_PATH" switch "code-mower-pr-${PR_NUMBER}"
export GITHUB_TOKEN="$(gh auth token)"

tools/run_codex_audit_pr.sh \
  --repo "$REPO" \
  --pr "$PR_NUMBER" \
  --repo-paths "$REPO:$PR_HEAD_PATH" \
  --merge-authority

tools/run_claude_audit_pr.sh \
  --repo "$REPO" \
  --pr "$PR_NUMBER" \
  --repo-paths "$REPO:$PR_HEAD_PATH" \
  --merge-authority
```

Each wrapper posts a structured audit comment tied to the current head SHA. A
clean audit adds `codex-audit-done` or `claude-audit-done`; a blocking audit
adds the matching `*-audit-blocked` label and explains the finding. Fix any
P0, P1, or P2 findings on the same branch, push, and rerun the blocked lane.

If you need to recompute the Code Mower gate after posting manual audit
comments, dispatch it with the PR number and the exact current head SHA:

```bash
export PR_HEAD_SHA="$(gh pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid --jq .headRefOid)"
gh workflow run code-mower-gate.yml --repo "$REPO" \
  -f pr_number="$PR_NUMBER" \
  -f head_sha="$PR_HEAD_SHA"
code-mower lanes status --repo "$REPO"
```

When both audits are clean and your normal CI is green, merge the setup PR:

```bash
gh pr view "$PR_NUMBER" --repo "$REPO" --json labels,mergeStateStatus,statusCheckRollup
gh pr checks "$PR_NUMBER" --repo "$REPO" --watch
gh pr merge "$PR_NUMBER" --repo "$REPO" --squash --delete-branch
```

You now have a merged PR with Code Mower audit evidence. Keep the audit lanes
manual until several real known-clean and known-blocked PRs prove that the
reviewers are useful on your repository.

## 7. Optional: Rehearse The Package Install Path

If you want to prove the public package can run the first-user path from a clean
virtual environment, run:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.0b1 \
  --allow-package-index \
  --python "$(command -v python3.12)" \
  --json
```

Use `--repo-path /path/to/repo` to validate the installed Code Mower CLI
against a real repository; if `tools/code_mower` exists, the rehearsal also
runs wrapper parity for mirror-removal, otherwise it detects and dry-runs the
repo's native checks.

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.0b1 \
  --allow-package-index \
  --repo-path /path/to/repo \
  --python "$(command -v python3.12)" \
  --json
```

See [First-User Install Rehearsal](first-user-install-rehearsal.md) for the
release-gate checklist and expected artifacts.

## 8. Next: Build Loop

When the reviewer gate is useful, move to the builder-plus-orchestrator path.
Next: build loop - [Build Loop In 30 Minutes](build-loop-in-30-minutes.md).
