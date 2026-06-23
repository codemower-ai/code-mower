# Launch Command Surface

Code Mower has more machinery than a first-time user should need to think
about. This page separates the v0.5 launch-safe path from advanced/operator
commands.

## Launch-Safe Commands

These are the commands early adopters should be able to run in the first
session.

| Command | Purpose | Writes? | Network? |
| --- | --- | --- | --- |
| `code-mower --version` | Confirm install. | no | no |
| `code-mower init --easy` | Preview generated setup. | no | no |
| `code-mower init --easy --apply --output-dir .code-mower.generated` | Write reviewable generated setup files. | yes, local only | no |
| `code-mower project-context init --project-name "My Product"` | Create editable local project doctrine docs. | yes, local only | no |
| `code-mower context add --external path/to/doc.md` | Record external planning context as a metadata-only local manifest. | yes, local only | no |
| `code-mower plan from-github-issue owner/repo#123 --post` | Turn a GitHub issue into a plan and post a structured plan comment back to the issue. | optional local output | GitHub |
| `code-mower plan from-issue ...` | Turn copied issue text into a local/offline planning artifact. | yes, local only | no |
| `code-mower work-order draft ...` | Create an implementation contract from a plan or prompt, plus a metadata-only `*.cloud-event.json` sidecar. | yes, local only | no |
| `code-mower doctor --preflight --json` | Check Python, GitHub, provider CLIs, cloud token posture, and private-repo cost traps. | no | optional GitHub/provider probes |
| `code-mower next-steps --profile recommended --repo OWNER/REPO` | Print the next recommended setup actions. | no | no |
| `code-mower migration package-install-rehearsal ...` | Prove install, toy repo, starter report, and cloud dry-run path. | yes, scratch workspace | no uploads |
| `code-mower calibration auto-discover --repo OWNER/REPO --last-n 20 --output .code-mower/draft-calibration-corpus.json` | Bootstrap a draft corpus from recent merged PRs and review signals. | yes, local output file | GitHub |
| `code-mower calibration value-report ...` | Generate a local reviewer value report. | yes, local output file | no |
| `code-mower cloud export --event work_order=...` | Build an inspectable metadata bundle, including issue/work-order provenance when supplied. | yes, local output dir | no |
| `code-mower cloud upload ... --dry-run` | Preview upload payload without sending it. | no | no upload |
| `code-mower cloud dogfood --json` | Preview routine metadata upload. | no | no upload |
| `code-mower cloud dogfood --yes --json` | Upload sanitized metadata after explicit confirmation. | no | yes |

## Advanced Or Operator Commands

These commands are real, but they are not the first-user spine.

- Provider runners: `codex-audit`, `claude-audit`, `gemini-cli`,
  `antigravity-cli`, `hermes-cli`, `coderabbit-cli`, `local-llm`.
- Workflow helpers: `trailer-comment-labeler`, `saas-reviewer-labeler`,
  `clear-stale`, `blind-review`.
- Migration and packaging internals: `package`, `bootstrap`, advanced
  `migration` subcommands.
- Planning and authoring-intelligence surfaces: `work-order`,
  `builder-experiment`, `telemetry`, future ACP or orchestrator bridges.

The planning commands are local-first and safe to try early, but they are not
required for a first audit. See [planning-work-orders.md](planning-work-orders.md)
when a team wants project doctrine, issue-derived plans, or builder experiment
seeds before implementation starts.

Treat these as opt-in after the launch-safe commands are boring in one
repository.

## Promotion Rule

Manual and informational lanes can be tried early. Merge-gating lanes should
wait until:

1. `doctor --preflight` has no unexplained failures;
2. the provider can run a real prompt or audit smoke;
3. known-clean controls stay quiet;
4. known-blocked controls are caught; and
5. the value report supports promotion.

That rule is what keeps Code Mower from becoming a pile of bots with labels.
