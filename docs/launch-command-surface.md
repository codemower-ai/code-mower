# Launch Command Surface

Code Mower has more machinery than a first-time user should need to think
about. This page separates the launch-safe path from advanced/operator
commands.

## Launch-Safe Commands

These are the commands early adopters should be able to run in the first
session. Choose the install path first from
[Install And Bootstrap](install.md).

| Command | Purpose | Writes? | Network? |
| --- | --- | --- | --- |
| `code-mower --version` | Confirm install. | no | no |
| `code-mower init --easy` | Preview generated setup. | no | no |
| `code-mower init --easy --apply --output-dir .code-mower.generated` | Write reviewable generated setup files. | yes, local only | no |
| `code-mower init code-mower.yml --add-repo OWNER/REPO --apply` | Render the same lane/label/workflow setup for a sibling repo target. | yes, local only | no |
| `code-mower project-context init --project-name "My Product"` | Create editable local project doctrine docs. | yes, local only | no |
| `code-mower context add --external path/to/doc.md` | Record external planning context as a metadata-only local manifest. | yes, local only | no |
| `code-mower plan from-github-issue owner/repo#123 --post` | Turn a GitHub issue into a plan and post a structured plan comment back to the issue. | optional local output | GitHub |
| `code-mower plan from-issue ...` | Turn copied issue text into a local/offline planning artifact. | yes, local only | no |
| `code-mower work-order draft ...` | Create an implementation contract from a plan or prompt, plus a metadata-only `*.cloud-event.json` sidecar. | yes, local only | no |
| `code-mower work-order attach-delivery ...` | Attach PR, reviewer-check, and merge identifiers to a work-order sidecar without source, diffs, transcripts, or issue bodies. | yes, local only | no |
| `code-mower builder record --provider grok_bot --executor cursor_cloud_agent ...` | Record source-free builder provenance after an agent opens a branch or PR. | yes, local only | no |
| `code-mower lanes status --repo OWNER/REPO` | Show active PR lanes, gate/check state, local board/process hints, stale audit requeue guidance, and the next action. | no | GitHub optional |
| `code-mower lanes status --repo OWNER/REPO --show-local-paths` | Include local cwd paths in the status snapshot for local debugging. | no | GitHub optional |
| `code-mower productivity report --repo OWNER/REPO` | Summarize local Board history, reviewer spend, quality catches, fix rounds, owner actions, and optional cloud aggregate productivity events. | no | no |
| `code-mower productivity report --repo OWNER/REPO --cloud-event PATH --json` | Include metadata-only `productivity_summary` aggregate event files and print the stable report JSON. | no | no |
| `code-mower board serve --repo OWNER/REPO` | Serve redacted lane status plus owner queue and local verdict/spend timelines in a local read-only browser board. | no | GitHub optional |
| `code-mower board serve --repo OWNER/REPO --record-events` | Serve the board and append throttled metadata-only local history snapshots while it is open. | yes, local only | GitHub optional |
| `code-mower board serve --repo OWNER/REPO --agent-adapters-path PATH` | Read opt-in local agent cards from a custom metadata-only adapter directory. | no | no |
| `code-mower board record --repo OWNER/REPO` | Append one redacted status snapshot to `.code-mower/board/events.jsonl` for local board history. | yes, local only | GitHub optional |
| `code-mower board events` | Print recent local board-history events without calling GitHub. | no | no |
| `code-mower board doctor --repo OWNER/REPO` | Diagnose Board inputs, local history, gate alerts, owner queue, and optional agent cards with redacted local paths by default. | no | GitHub optional |
| `code-mower board reset --repo OWNER/REPO --yes` | Delete only the local Board history file after explicit confirmation. | yes, local only | no |
| `code-mower doctor --preflight --json` | Check Python, GitHub, provider CLIs, cloud token posture, and private-repo cost traps. | no | optional GitHub/provider probes |
| `code-mower doctor --adoption --hosted-builders --repo OWNER/REPO --json` | Check hosted-builder or orchestrator setup without requiring local Codex/Claude CLIs on this machine. | no | optional GitHub/provider probes |
| `code-mower doctor --supervised-pilot --repo OWNER/REPO --json` | Summarize v1.0 manual-pilot readiness with blockers, owner actions, warnings, promotion to-dos, cloud token, and Board visibility. | no | optional GitHub/provider probes |
| `code-mower doctor --promoted-pilot --repo OWNER/REPO --json` | Check the stricter posture needed before green audits may drive auto-merge. | no | optional GitHub/provider probes |
| `code-mower next-steps --profile recommended --repo OWNER/REPO` | Print the next recommended setup actions. | no | no |
| `code-mower migration setup-drift --repo-path .` | Classify existing generated setup files before an upgrade PR without printing source or diffs. | no | no |
| `code-mower migration package-install-rehearsal ...` | Prove install, toy repo, starter report, and cloud dry-run path. | yes, scratch workspace | no uploads |
| `code-mower calibration auto-discover --repo OWNER/REPO --last-n 20 --output .code-mower/draft-calibration-corpus.json` | Bootstrap a draft corpus from recent merged PRs and review signals. | yes, local output file | GitHub |
| `code-mower calibration value-report ...` | Generate a local reviewer value report. | yes, local output file | no |
| `code-mower cloud export --event work_order=...` | Build an inspectable metadata bundle, including issue/work-order provenance when supplied. | yes, local output dir | no |
| `code-mower cloud upload ... --dry-run` | Preview upload payload without sending it. | no | no upload |
| `code-mower cloud dogfood --json` | Preview routine metadata upload. | no | no upload |
| `code-mower cloud dogfood --yes --json` | Upload sanitized metadata after explicit confirmation. | no | yes |
| `code-mower cloud board-snapshot --repo-slug OWNER/REPO --json` | Preview a metadata-only Board mirror event with zero reports. | no | no upload |
| `code-mower cloud board-snapshot --repo-slug OWNER/REPO --yes --json` | Upload the explicit Board mirror event after inspection. | no | yes |

## Advanced Or Operator Commands

These commands are real, but they are not the first-user spine.

- Provider runners: `codex-audit`, `claude-audit`, `gemini-cli`,
  `antigravity-cli`, `hermes-cli`, `coderabbit-cli`, `local-llm`.
- Workflow helpers: `trailer-comment-labeler`, `saas-reviewer-labeler`,
  `clear-stale`, `blind-review`, `decide`.
- Migration and packaging internals: `package`, `bootstrap`, advanced
  `migration` subcommands.
- Planning and authoring-intelligence surfaces: `work-order`, `builder`,
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

1. `doctor --adoption --repo OWNER/REPO` has no unexplained failures;
2. the provider can run a real prompt or audit smoke;
3. known-clean controls stay quiet;
4. known-blocked controls are caught; and
5. the value report supports promotion.

That rule is what keeps Code Mower from becoming a pile of bots with labels.
