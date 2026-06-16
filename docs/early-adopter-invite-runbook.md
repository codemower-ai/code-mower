# Early Adopter Invite Runbook

Use this runbook for the first 5-10 friendly users before widening Code Mower
to 20-50 early OSS users.

## Goal

The first invite cohort should prove that a new user can get value without
knowing the history of the reference repos:

1. install Code Mower from the tagged alpha;
2. run `init --easy` and the v0.5 doctor preset;
3. generate the starter calibration/value report;
4. optionally connect a CodeMower.com team token; and
5. upload sanitized metadata after reviewing the local bundle.

## Invite Criteria

Pick users who have:

- one active GitHub repository they can safely test on;
- willingness to run local CLI commands;
- permission to install provider CLIs such as Codex or Claude if they want
  actual reviewer output;
- comfort with opt-in metadata sharing, or willingness to stay local-only; and
- enough product context to label at least one known-clean and one known-buggy
  pull request later.

Avoid users who need GitLab, Bitbucket, enterprise SSO, hosted model routing,
or no-terminal setup for the first cohort.

## Send This Short Invite

```text
Want to try Code Mower for 10 minutes?

It is an OSS local-first tool for setting up AI peer-programmer/reviewer lanes
on your real codebase, with optional privacy-first cloud reporting.

Start here:
https://github.com/codemower-ai/code-mower/blob/v0.5.0-alpha.40/docs/try-in-10-minutes.md

Cloud sharing is optional. The default bundle excludes source code, raw diffs,
model transcripts, raw stdout/stderr, auth output, and secrets.
```

## Operator Prep

Before inviting a user:

1. Verify the tagged install command in a fresh repo:

   ```bash
   pipx install --python python3.12 "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.40"
   code-mower --version
   ```

2. Confirm CodeMower.com health:

   ```bash
   curl -fsS https://codemower.com/api/health
   ```

3. If the user wants cloud sharing, ask them to sign in at:

   https://codemower.com/dashboard

   Then have them create a team token and run `code-mower cloud setup
   --token-stdin`. If login is temporarily rough, issue an operator token and
   send it out of band.

## Success Signals

The user is successful when they can send back:

- `code-mower --version` output;
- `code-mower doctor --v05 --json` status;
- the generated `reviewer-value-report.md`; and
- if they opted into cloud, the upload ID from `code-mower cloud upload
  --yes --json`.

## Triage Rules

- Install failure: improve docs or packaging before inviting more users.
- Doctor false alarm: fix doctor wording or severity before adding features.
- Provider auth failure: keep the lane optional unless the user explicitly
  wants that provider.
- Cloud upload concern: default to local-only and inspect the bundle together.
- Dashboard confusion: fix CodeMower.com copy before widening the cohort.

## Widening Bar

Do not expand past 10 friendly users until:

- at least three fresh installs complete without hands-on rescue;
- one private repo and one public repo have completed the local path;
- at least two optional cloud uploads appear on the dashboard;
- token creation, revocation, and last-used status are understandable; and
- the top three user questions are answered in docs.
