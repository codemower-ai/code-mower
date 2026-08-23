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

Code Mower requires Python 3.11 or newer. Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 code-mower==0.5.0b53
code-mower --version
```

`0.5.0b53` is a beta release. To follow the newest beta line instead of
pinning this exact build:

```bash
pipx install --python python3.12 --pip-args="--pre" code-mower
```

## 2. Authenticate GitHub

Use an account that can read the repository, open a setup PR, add labels, and
post audit comments.

```bash
gh auth login -h github.com -s repo,workflow,read:org
gh auth status
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

## 4. Run The Preflight Doctor

```bash
code-mower doctor --preflight --json
```

`--preflight` is the friendly alias for the versioned v0.5 first-run preset.
It expands to the checks early adopters need:

- recommended profile selection;
- Python/runtime checks;
- local provider CLI discovery and smoke probes;
- stale terminal-label hygiene for merge-authority reviewer lanes;
- GitHub repository visibility, permissions, branch protection, and Actions
  cost diagnostics; and
- optional Code Mower Cloud token setup diagnostics.

Warnings are setup guidance. They are only fatal when you pass `--strict`. In
JSON mode, check the top-level `run_plan` field first. It tells you whether the
preflight included GitHub and optional cloud checks before you inspect
individual provider warnings.

For merge-authority lanes such as Codex or Claude audit, look for
`provider.review_hygiene`. It should pass for lanes that can satisfy the merge
bar, because it proves Code Mower can clear stale terminal labels after a PR
receives new commits.

## 5. Open The Setup PR

Create one small PR that installs the generated reviewer-gate support. This PR
is the first audited PR.

```bash
git switch -c chore/code-mower-reviewer-gate
cp -R .code-mower.generated/. .
git status --short
git add .github tools calibration-corpus.json context-packs.json \
  reviewer-spend.json reviewer-value-report.example.md
git commit -m "chore: add code mower reviewer gate"
git push -u origin HEAD
gh pr create \
  --repo "$REPO" \
  --base "$DEFAULT_BRANCH" \
  --head "$(git branch --show-current)" \
  --title "chore: add Code Mower reviewer gate" \
  --body "Install Code Mower generated reviewer-gate support for the first audited PR."
export PR_NUMBER="$(gh pr view --repo "$REPO" --json number --jq .number)"
gh pr edit "$PR_NUMBER" --repo "$REPO" \
  --add-label needs-codex-audit \
  --add-label needs-claude-audit
```

If your repository already has files with the same names, review `git diff`
before committing and keep only the generated support files you actually intend
to enable.

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
  --package-spec code-mower==0.5.0b53 \
  --python "$(command -v python3.12)" \
  --json
```

Use `--repo-path /path/to/repo` to validate the installed Code Mower CLI
against a real repository; if `tools/code_mower` exists, the rehearsal also
runs wrapper parity for mirror-removal, otherwise it detects and dry-runs the
repo's native checks.

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.5.0b53 \
  --repo-path /path/to/repo \
  --python "$(command -v python3.12)" \
  --json
```

See [First-User Install Rehearsal](first-user-install-rehearsal.md) for the
release-gate checklist and expected artifacts.

## 8. Next: Build Loop

When the reviewer gate is useful, move to the builder-plus-orchestrator path.
Next: build loop - [Build Loop In 30 Minutes](build-loop-in-30-minutes.md).
