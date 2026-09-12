# Upgrade An Existing Repository

Use this flow when a repository already has Code Mower generated workflows,
wrappers, labels, or support files. Keep the upgrade as one reviewed PR so
setup drift is visible before it can affect lanes.

## 1. Record The Current Install

Run these from the machine that will operate the repository:

```bash
command -v code-mower
code-mower --version
```

If multiple agents share the machine, decide which installer owns this command
before upgrading it. Use the install matrix in [Install And Bootstrap](install.md)
for pipx, uv, and contributor-checkout paths.
When a hosted or peer agent performs the upgrade rehearsal, give it the
universal prompt in [Orchestrator Prompt Pack](orchestrator-prompt-pack.md) so
it reports the same active command, exact version, posture-specific doctor,
lanes status, and owner click-list as the primary orchestrator.

## 2. Generate Fresh Setup Output

From a clean repository checkout:

```bash
code-mower init --easy --apply --output-dir .code-mower.generated
```

Treat `.code-mower.generated` as review input. Do not copy it wholesale until
you have compared it with the existing repository files.

## 3. Inspect Setup Drift

Run the read-only drift report:

```bash
code-mower migration setup-drift --repo-path . --json
code-mower migration setup-drift --repo-path .
```

If the repository already has generated builder or dispatch files, pass the
current builder set so the comparison includes those files instead of reporting
them as repo-only upgrade noise:

```bash
code-mower migration setup-drift --repo-path . --builders codex,claude,cursor
```

Run setup-drift from the full repository checkout. Thin workspaces, empty
directories, or paths without git tracking can make every generated file appear
`new`; the report prints a repo path hint when that posture is likely. If the
repo only has reviewer-lane workflows and no builder-dispatch files, the
builder hint says so and you should pass `--builders` only when builder lanes
are actually part of that repository.

The report classifies paths only; it does not include source, diffs,
transcripts, issue body text, auth output, local secret values, or secrets.

- `same`: current file already matches the generated output.
- `differs`: review the file diff before copying the generated replacement.
- `new`: generated file does not exist in the repo yet.
- `missing-from-output`: existing Code Mower file is no longer generated.
- `repo-only`: file appears Code Mower-related but is intentionally outside the
  generator contract.

Do not delete `repo-only` or `missing-from-output` files automatically. They may
be product-specific shims, pinned wrappers, hand-written docs, or rollback
support. Keep, edit, or remove them only as an explicit review decision.

The JSON report includes a `standalone_pin` block and the text report prints a
concise standalone pin line even when the pin file is absent. A warning there
means the checked-in standalone ref is missing, placeholder, unreadable, or
different from the currently running Code Mower package. Treat it as an upgrade
review item: decide whether the repo should keep its current reviewed pin or
move the pin in the same upgrade PR.

When builder files are tracked but `--builders` was omitted, the report prints a
builder hint with the safest inferred `--builders` option. Rerun with that option
before copying generated setup if those builder lanes are still enabled.

## 4. Copy Only Intended Files

Open a branch for the upgrade PR, then copy the generated files you intend to
adopt:

```bash
git switch -c chore/code-mower-upgrade
cp -R .code-mower.generated/. .
git status --short
```

Review the diff before committing. Preserve repository-specific edits in
`code-mower.yml`, local wrapper files, workflow permissions, and owner-surface
labels unless this upgrade intentionally changes them.

## 5. Check Builder And Reviewer Identity

Confirm the generated builder identity matches how agents actually open PRs:

- `builder:<lane>` labels are the strongest merge-gate signal.
- authenticated PR authors are trusted when configured in `builder_identity`.
- `builder_identity.branch_prefixes` can infer labels such as `builder:cursor`.
- PR-body trailers are useful for provenance experiments, but are not trusted
  as merge-authority author-exclusion input.

For audit comments, set trusted author repository variables such as
`CLAUDE_AUDIT_BOT_AUTHORS` and `CODEX_BOT_AUTHORS` to the GitHub logins that
may post manual pilot verdicts. `doctor --adoption --github` verifies only
variable names and presence status; it never prints variable values.

## 6. Check Wrapper And Pin Drift

If the product repository uses standalone support wrappers, inspect:

- `tools/code_mower`
- `tools/code_mower_standalone_shadow.sh`
- `tools/code_mower_standalone_pin.env`
- `tools/run_codex_audit_pr.sh`
- `tools/run_claude_audit_pr.sh`

The pin file should point at a reviewed Code Mower release, tag, or package
source. Product wrappers should delegate to the pinned standalone package unless
the repository is intentionally keeping a local fallback. For deeper migrations,
use [Mirror-Removal Runbook](mirror-removal-runbook.md).

## 7. Verify And Open The PR

Run:

```bash
bash .code-mower.generated/smoke-tests.sh
code-mower doctor --adoption --repo OWNER/REPO
code-mower lanes status --repo OWNER/REPO
```

Then commit the reviewed setup changes and open the upgrade PR. Run the usual
peer audits and merge only when the current head has clean audit evidence and
`code-mower/gate` is green for repositories where the gate is required.

After merge, record the installed path/version again:

```bash
command -v code-mower
code-mower --version
```

## Devin Provider Identity And Compatibility

Code Mower now distinguishes these Devin identities:

- `devin` — the canonical hosted Devin lane. It now uses the Devin Sessions API
  v3 instead of GitHub issue-comment triggering. Read credentials from
  `DEVIN_API_KEY` and `DEVIN_ORG_ID`; require the exact repository in
  `CODE_MOWER_DEVIN_REPOSITORIES` before dispatch.
- `devin_cloud` — an accepted alias that resolves to the same `devin` hosted
  lane. It uses the same API credentials and response timeout.
- `devin_cli` — the local Devin CLI lane, informational and not merge authority.

Existing `code-mower.yml` files that select `devin` continue to work unchanged.
The builder provenance identity stays `builder:devin` for hosted and local
identities, so branch ownership, trailer prefixes, and dispatch label logic keep
working.

To adopt the new identities:

| Goal | Action |
|---|---|
| Keep using hosted Devin | Leave `devin` in `code-mower.yml` as-is. |
| Make hosted Devin explicit | Select `devin` in your profile; set service-user `DEVIN_API_KEY`, opaque `DEVIN_ORG_ID`, and the exact `OWNER/REPO` in `CODE_MOWER_DEVIN_REPOSITORIES`. The `devin_cloud` alias resolves to the same lane in campaigns and telemetry. |
| Try local Devin CLI | Select `devin_cli` in your profile, install `devin` on PATH, and set `CODE_MOWER_DEVIN_CLI_MODEL` or `DEVIN_CLI_MODEL`. |

`devin_cli` remains disabled by default and reports version and auth status with
bounded, privacy-safe output. Doctor never persists raw `devin auth status`
output or account identity, and records only the executable basename for this
lane, never a local filesystem path. Because Devin CLI uses the ambient login
state, doctor describes the auth probe as the ambient Devin CLI session rather
than an isolated campaign home. It participates in release campaigns through
the maintained `code_mower.campaign_adapters` adapter and is eligible for the
local audit and builder runner when explicitly selected. Install `devin` on
PATH, run `devin auth login` in a trusted environment, and set
`CODE_MOWER_DEVIN_CLI_MODEL` or `DEVIN_CLI_MODEL` before enabling it.

`devin` is now an API-first campaign transport. The `--issue` parameter is
optional and only records an audit marker; the `DEVIN_API_KEY` and
`DEVIN_ORG_ID` credentials are the dispatch trigger. The full slug must also
appear in `CODE_MOWER_DEVIN_REPOSITORIES`; a same-name personal fork therefore
cannot satisfy the intended organization target. The opaque `org-*`
`DEVIN_ORG_ID` is not compared with the GitHub owner name.

### Check One Devin Posture Before Assigning Work

Devin stays optional: Claude + Codex remain the default pair, and a repository
that never selected Devin sees no Devin checks. After selecting it, run one
command for the whole optional setup:

```bash
code-mower doctor --profile recommended --devin --repo OWNER/REPO --json
```

Pin the profile you selected in place of `recommended`, so a configuration with
more than one Devin lane is reported for that profile only. The `--devin` flag
also works before selection and prints the local CLI, hosted
API, and unavailable postures with the next action for each. Doctor reports the
selected transport, which authentication belongs to it, the create/view/manage
permissions the account owner must grant, the capabilities the transport does
not support, and the lifecycle recovery commands. It never reports credential
values, the service-user identity, the organization identifier, the configured
repository inventory, a local path, or raw provider output.

Pick exactly one posture; the two authentications are not interchangeable.

| Posture | Selection | Authentication | Next action when not ready |
|---|---|---|---|
| Local CLI | `code-mower init code-mower.yml --profile recommended --set-transport devin=devin_cli --apply` (replace the path and profile with the ones you inspect) | the ambient Devin Desktop/CLI login on this machine | install `devin` on PATH (or set `CODE_MOWER_DEVIN_CLI_COMMAND`), then run `devin auth login` in a trusted environment |
| Hosted API | `code-mower init code-mower.yml --profile recommended --set-transport devin=devin_api_v3 --apply` (replace the path and profile with the ones you inspect) | dedicated service-user credentials plus exact repository scope | set `DEVIN_API_KEY` and `org-*` `DEVIN_ORG_ID`, and add the exact `OWNER/REPO` to `CODE_MOWER_DEVIN_REPOSITORIES` |
| Unavailable | keep the default pair | none | report the unavailable capability and hand the work to a selected participant instead of substituting another product |

`--set-transport` replaces only Devin's transport, its own profile lane, and its
own participant alias: every other participant and profile lane stays exactly as
configured. It previews the change unless `--apply` is set. A profile whose Devin
lanes are custom-named is edited with `code-mower init <config> --profile <name>
--interactive` instead, because no generated command can rewrite a lane the
repository owns.

Hosted credentials do not enable local execution, and a local login does not
authorize hosted sessions. Hosted Devin also cannot coordinate a session: use
`devin_cli`, Codex, or Claude as the host.

## Local Devin Builder Lane

Separately from the `devin_cli` reviewer contract above, `devin` can now also
run as a local **builder** lane on the self-hosted Mac lane runner, alongside
`codex` and `claude`. This reuses the same engine as those lanes: trusted-author
work-order filtering, one-issue/one-PR selection, single-writer pre-push
branch protection, and peer-audit dispatch.

To adopt it in a new or existing repo:

1. Install the `devin` CLI on the runner's `PATH` (or set
   `CODE_MOWER_DEVIN_CLI_COMMAND` to an absolute path) and authenticate it
   ahead of time; the lane runner never handles interactive login.
2. Add `devin` to the `--builders` list passed to `code-mower init` (for
   example `--builders codex,claude,devin`). This generates
   `docs/lanes/devin.md`, adds `devin` to the Mac lane runner script and
   workflow, and adds `builder:devin` / `dispatched:devin` labels.
3. Optionally set `CODE_MOWER_DEVIN_CLI_MODEL` (or `DEVIN_CLI_MODEL`/
   `DEVIN_MODEL`) to record which model Devin CLI is running, and
   `LANE_DEVIN_EXTRA_FLAGS` for owner-approved extra CLI flags. The runner
   refuses to start if that override includes `--export`, `--continue`/`-c`,
   `--resume`/`-r`, or an attempt to override `--permission-mode`,
   `--sandbox`, `--prompt-file`, `--print`, `--respect-workspace-trust`, or
   `--config` (including their `--flag=value` forms).

Permission posture: the runner invokes the Devin CLI's non-interactive
`--print` mode with `--sandbox --permission-mode autonomous`. The Devin CLI's
own OS sandbox — not the lane's dedicated, disposable checkout by itself — is
the actual security boundary; the checkout-scoping is the same one used for
Codex and Claude, but a dedicated cwd alone is never described as a safety
boundary here. Autonomous mode auto-approves shell commands but still
requires interactive confirmation for Devin's dedicated file write/edit
tools, which a noninteractive `--print` run cannot answer and which would
abort the run, so the frozen work-order prompt explicitly instructs Devin to
perform every file creation and edit through shell commands only and never
call those dedicated tools. The frozen work order is written to a mode-0600
prompt file and passed with `--prompt-file`, never piped over stdin (the CLI
is invoked with stdin closed) and never inlined as prompt text in argv. The
runner never invokes `--export`, `--continue`, or `--resume`, and there is no
session continuation or export in this lane; it never uploads the prompt,
transcript, source, diff, issue body, raw stdout/stderr, auth output, or
local paths. After a successful build or fix round it records only bounded
provenance (provider/executor, model, tool version, elapsed time,
intervention count of zero, PR/head identity, and status) via `code-mower
builder record`, using `--provider devin_cli --executor devin_cli` — a
distinct local identity from a hosted Devin session (`devin`/`devin_cloud`),
even though both share the `builder:devin` label.

`code-mower lanes status` and the Board's local process discovery recognize
a running `devin` process as the Devin lane, reporting only its checkout
path (redacted by default) — never the prompt file path or prompt text.
