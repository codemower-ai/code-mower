# Code Mower v0.5 Early Adopter Guide

The v0.5 goal is to make Code Mower useful for 20-50 early OSS users without
requiring them to understand every provider or lane.

## Two Setup Personas

Code Mower has two setup paths, and v0.5 docs should keep them separate.

**Code Mower OSS user setup** is the early-adopter path. A user installs Code
Mower, runs `code-mower init --easy`, runs `code-mower doctor --preflight`,
runs a local audit or value report, and optionally pastes a CodeMower.com
developer/team token into `code-mower cloud setup --token-stdin`. OSS users do
not configure Supabase, Vercel, OAuth apps, DNS, service-role keys, database
migrations, or hosted secrets.

**CodeMower.com operator setup** is the hosted service path. Operators own the
Supabase/Postgres project, Vercel deployment, OAuth provider credentials, DNS
records, service-role/admin secrets, token administration fallback, retention,
abuse handling, and hosted reporting. Those steps live in the private
CodeMower.com repo, not in the normal OSS install guide.

## What v0.5 Should Prove

A new user can:

1. install Code Mower;
2. run easy-mode setup checks on a GitHub repo;
3. run or request a first AI audit;
4. generate a local reviewer value report; and
5. optionally share a sanitized benchmark bundle to Code Mower Cloud.

The one-command first-run diagnostic is:

```bash
code-mower doctor --v05 --json
```

For friendlier user-facing docs, prefer the equivalent alias:

```bash
code-mower doctor --preflight --json
```

Both expand to `--easy --profile recommended --probe-runtime --github --cloud`.

## Recommended First-Run Profile

Keep the default small:

| Provider | Default role | Why |
| --- | --- | --- |
| Codex audit | local structured audit | strongest default peer-review lane |
| Claude audit | local structured audit | second independent local peer-review lane |
| GitHub doctor | setup verifier | catches private repo, token, and Actions-cost traps |
| Gitar | optional informational | useful third signal, but SaaS/app setup varies |

Everything else starts manual or informational: Antigravity, Gemini legacy,
Hermes, CodeRabbit CLI, Cursor BugBot, Qodo, Greptile, Devin, local LLMs, and
future ACP bridges.

## What Not To Do For v0.5

- Do not enable paid or SaaS lanes automatically.
- Do not require cloud upload.
- Do not upload source code, raw diffs, raw transcripts, auth output, or
  secrets.
- Do not make uncalibrated lanes branch-protection requirements.
- Do not run recurring GitHub Actions schedules in private repos by default.

## Early Adopter Support Checklist

Before inviting users:

- README has a five-minute quickstart.
- README shows a concrete sample of `code-mower doctor --preflight` output or links
  to a screenshot/terminal capture near the top.
- `docs/quickstart.md` works from a clean machine.
- `docs/sample-doctor-output.md` shows what a successful/warn first run looks
  like before users install anything.
- OSS-user docs never require Supabase, Vercel, OAuth-app, DNS, service-role,
  migration, or hosted-secret setup.
- CodeMower.com operator docs clearly own hosted setup, token administration,
  and production secrets.
- `code-mower doctor --preflight --json` works as the recommended early-adopter
  preset.
- `code-mower cloud upload --dry-run` previews without network transfer.
- `code-mower cloud doctor --probe-service` verifies CodeMower.com health and
  returns dashboard/setup guidance without echoing tokens.
- `code-mower cloud setup --token-stdin` writes a private local token file
  without echoing the full token.
- `https://codemower.com/login` and `https://codemower.com/dashboard` are
  verified for team token creation, or operator-issued tokens are ready as a
  temporary fallback.
- A toy repo and one private real repo have completed install, doctor,
  rehearsal, export, and upload dry-run.
- Known limitations are documented plainly.
- `docs/early-adopter-invite-runbook.md` has the current tagged install command
  and invite copy for the first 5-10 friendly users.
- A public GitHub Release exists for the current release tag, so users do not
  have to infer release state from tags alone.

## Cloud Sharing Positioning

The local OSS tool should remain useful without Code Mower Cloud. The cloud
service adds:

- longitudinal trend reporting;
- aggregated provider/lane/lens benchmarks;
- cost and latency comparisons;
- recommendations based on similar repositories; and
- shared team dashboards.

Cloud sharing is opt-in. Users should always be able to inspect the local
bundle before upload.

## v0.5 Release Candidate Bar

Cut a v0.5 alpha or beta only after:

- package install works through `pipx`;
- release workflow builds source/wheel distributions, and PyPI publishing is
  either configured or explicitly called out as the next friction-reduction
  milestone;
- alpha/beta GitHub releases are marked as prereleases;
- docs are sufficient for a new user;
- cloud export and dry-run upload are tested;
- codemower.com can receive an ingest payload;
- the dashboard can show at least project count, upload count, report count,
  provider/lane summary rows, and structured event counts;
- GitHub, Google, and Apple login have an operator-owned verification checklist
  with an operator-token fallback; and
- the privacy boundary is documented and tested.

## Current Beta

`v0.5.0-beta.18` is the current friendly-user beta. It keeps the
local-first package path from alpha.1, the cloud doctor service-readiness check
from alpha.5, the hardened provider-auth doctor diagnostics needed for
early-adopter troubleshooting, the dogfood dry-run path that feeds the
CodeMower.com dashboard, and the draft auto-discovery path that helps first
users reach a reviewable value report without hand-building every corpus item.
It also adds a first-user readiness scorecard to the package-install rehearsal
so install, doctor, report, and cloud dry-run privacy gates can be checked at a
glance, and it uses a conventional `requirements/requirements.txt` manifest so
Dependabot's Dependency Graph path stays green. Earlier alpha hardening also keeps the package
materializer self-hosting from installed checkouts, gates release readiness on
generated standalone package versions, and rejects unsafe structured cloud
metadata before export or upload instead of relying on hosted-side cleanup.
