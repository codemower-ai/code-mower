# Code Mower v0.5 Early Adopter Guide

The v0.5 goal is to make Code Mower useful for 20-50 early OSS users without
requiring them to understand every provider or lane.

## What v0.5 Should Prove

A new user can:

1. install Code Mower;
2. run easy-mode setup checks on a GitHub repo;
3. run or request a first AI audit;
4. generate a local reviewer value report; and
5. optionally share a sanitized benchmark bundle to Code Mower Cloud.

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
- `docs/quickstart.md` works from a clean machine.
- `code-mower doctor --easy --github --probe-runtime` gives actionable
  remediation.
- `code-mower cloud upload --dry-run` previews without network transfer.
- A toy repo and one private real repo have completed install, doctor,
  rehearsal, export, and upload dry-run.
- Known limitations are documented plainly.

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
- docs are sufficient for a new user;
- cloud export and dry-run upload are tested;
- codemower.com can receive an ingest payload;
- the dashboard can show at least project count, upload count, report count,
  and provider/lane summary rows; and
- the privacy boundary is documented and tested.
