# Troubleshooting

Code Mower setup checks should prove that the configured tools can run the
same kind of work the reviewer lanes will ask them to do. A CLI status command
is useful, but it is not enough for merge-gating lanes.

For Slack OAuth, policy binding, private-channel mapping, disable/uninstall and
hosted lifecycle problems, use the dedicated
[Slack setup and troubleshooting guide](slack-setup.md#troubleshooting-upgrade-and-removal).

## Claude Code Reports Logged In But Audits Fail

`claude auth status` can report `loggedIn: true` while real non-interactive
requests still fail with `401 Invalid authentication credentials`. Code Mower
therefore treats the real prompt smoke as the useful signal:

```bash
claude auth status >/dev/null 2>&1 && echo "claude auth ok" || { echo "claude auth NOT ready"; false; }
claude -p "Reply with exactly: ok" --output-format json
```

If prompt auth only fails inside a long-lived parent process such as Codex,
first try the non-destructive bounce helper:

```bash
code-mower claude-bounce --json
code-mower claude-bounce --write-env ~/.config/code-mower/claude-clean-env.sh
source ~/.config/code-mower/claude-clean-env.sh
```

The bounce helper runs the same kind of real Claude prompt smoke twice: once
with the inherited environment and once with known stale Claude/Anthropic auth
override variables removed from the child process. It does not delete Claude
credentials, modify keychain state, or log raw provider output. If `clean_env`
passes while `inherited_env` fails, restart the parent app or source the
generated unset snippet before retrying.

`code-mower doctor --preflight` runs the provider-configured Claude smoke probe
with a bounded model and budget cap. If that probe reports a provider
authentication failure, refresh Claude Code auth and rerun the smoke:

```bash
cp ~/.claude/.credentials.json ~/.claude/.credentials.json.bak.$(date +%Y%m%d-%H%M%S) 2>/dev/null || true
rm -f ~/.claude/.credentials.json

security delete-generic-password -s "claude-code" 2>/dev/null || true
security delete-generic-password -s "Claude Code" 2>/dev/null || true

claude
claude -p "Reply with exactly: ok" --output-format json
```

If the prompt still fails, treat the Claude lane as unavailable until local auth
is repaired. For automation, prefer a provider/API-key credential path instead
of depending on interactive Claude.ai OAuth state.

## Doctor Output Is Safe To Share

Doctor redacts raw provider stdout/stderr. For provider smoke probes it reports
only shape and status metadata such as return code, JSON parse status, expected
sentinel match, output line count, and content-free auth failure flags. Provider
configs can declare `doctor_probe_auth_status_fields` to identify structured
status-code fields such as `api_error_status`, `status_code`, or `http_status`.
Doctor only reports sanitized auth status codes (`401` or `403`), never raw
provider-supplied status text.

## Which Local Session Is Active In This Checkout?

You do not need the session filename. From anywhere inside the checkout:

```bash
code-mower session show --current
code-mower session lease show
code-mower lanes status --repo OWNER/REPO
```

`session show --current` prints the brief the live orchestrator lease names,
read from `.code-mower/sessions/<lease session id>.json`, and exits zero only
when that brief is present, valid, and matches the lease's repository, session
id, and orchestrator. It is read-only and never scans for or guesses a file.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no mutating orchestrator lease is held in this working copy` | no `session start` has run here, or the lease was released | start a session, or pass the brief path to `session show SESSION_FILE` |
| `the orchestrator lease in this working copy has expired` | the last orchestrator stopped renewing | the next `session start` takes the lease over; `session lease show` reports the stale holder |
| `is not a lease this version can read` / `could not be read` | the lease file is malformed or unreadable | inspect it with `session lease show` and check permissions |
| `its saved brief was not found` | the brief was saved outside `.code-mower/sessions/` or removed | rerun with `--state-dir DIR`, or pass the exact `SESSION_FILE` |
| `records a different session, repository, or orchestrator` / `is a symlink or escapes the state directory` | the file at the lease's path belongs to another session, or was replaced by a link | inspect the lease with `session lease show`; only an exact-matching regular file is presented as current |
| `the orchestrator lease changed while the saved brief was being read` | another orchestrator took over or the lease expired mid-read | rerun; the command never presents a raced brief |

The lease file itself is `.code-mower/orchestrator-lease.json` at the
working-copy root. `lanes status` reports the same bounded `orchestrator_lease`
state, provider, and expiry without session ids or local paths, and
`other_repository` means the checkout is leased for a different `--repo`.

## Devin Is Selected But Not Ready

Devin is optional. Claude + Codex stay the default pair, and a repository that
never selected Devin produces no Devin checks at all. When it is selected, one
command reports the whole optional setup:

```bash
code-mower doctor --profile recommended --devin --repo OWNER/REPO --json
```

Use the profile this repository actually selected: `--profile recommended` is the
default, and any other profile name replaces it so the check reports that
profile's Devin lane rather than another one.

Read `provider.devin.selection` first: it names the selected transport and the
authentication that belongs to it. The two authentications are separate, and one
never substitutes for the other.

| Symptom | Meaning | Next action |
|---|---|---|
| `provider.devin.local_cli` warns that the command was not found | `devin_cli` runs Devin locally and needs the CLI on this machine | install `devin` on PATH or point `CODE_MOWER_DEVIN_CLI_COMMAND` at it, then run `devin auth login` in a trusted environment |
| `provider.devin.hosted_credentials` reports missing or malformed credentials | `devin_api_v3` needs dedicated service-user credentials; a local CLI login does not authorize it | set `DEVIN_API_KEY` and the opaque `org-*` `DEVIN_ORG_ID` in the environment or a protected credential profile |
| `provider.devin.repository_scope` does not acknowledge the repository | hosted Devin has no read-only preflight that proves GitHub connection scope, so the exact slug must be acknowledged locally | add the exact `OWNER/REPO` to `CODE_MOWER_DEVIN_REPOSITORIES`; a same-name fork is never accepted |
| `provider.devin.repository_scope` is skipped | no repository target was selected | rerun with `--repo OWNER/REPO` before dispatching paid work |
| `provider.devin.permissions` is skipped | Devin exposes no read-only permission probe, so the requirement is reported rather than verified | have the account owner confirm the create, view, and manage permissions listed in the check |
| `provider.devin.capabilities` lists capability gaps | the transport does not support those capabilities at all | report the unavailable capability and hand that work to a selected participant instead of substituting another product |

One capability gap comes up most often: hosted Devin cannot coordinate a
session, so use `devin_cli`, Codex, or Claude as the host. Hosted sessions do
support remote clarification messages, cancellation, and structured builder
results through `code-mower session message|cancel|collect`, while `devin_cli`
runs locally and has no remote lifecycle at all, so a stalled local run is a
local process action. An uncertain hosted dispatch is still recovered with
`code-mower session status ALIAS --provider devin` — never a second dispatch.

Devin checks report metadata only: no credential values, service-user identity,
organization identifier, configured repository inventory, local path, or raw
provider output.

## Board Shows An Older Version After Upgrade

`code-mower board serve --repo OWNER/REPO` is a long-running local process. If
you upgrade Code Mower while the Board is open, the browser can keep talking to
the older process until you restart it. The Board header shows the serving
version, the installed version, and whether a restart is recommended.

For a scriptable inventory across all local listeners, use:

```bash
code-mower board list --repo OWNER/REPO --json
```

The repository filter fails closed: a listener is included only when its
`/api/identity` response establishes that repository. Legacy and unresponsive
listeners remain visible in the unfiltered inventory, but are never selected
from a process command-line hint. Each row reports the invoking, serving, and
installed versions, `restart_recommended`, `managed`, and its service label.
The invoking CLI marks a Board stale whenever its serving version differs from
the invoking version, even when the old process reports its own serving and
installed versions as equal.

For one known Board, query the local status endpoint on its printed port:

```bash
curl -fsS http://127.0.0.1:PORT/api/status | python3 -m json.tool
```

In `/api/status`, inspect `board.version.serving_version`,
`board.version.installed_version`, and `board.version.restart_recommended`.
`GET /api/identity` is the smaller identity and version probe. `lanes status`
uses the same local inventory fields as `board list`. When a managed Board is
stale, copy its `restart_command`; for an identity-verified transient Board,
copy its `promotion_command` to install the persistent service. Both commands
use `--repo-path .`, so run them from the intended repository checkout, where the
service lifecycle validates the repository origin.

When restart is recommended for a transient Board, stop the old process and
start it again:

```bash
code-mower board serve --repo OWNER/REPO
```

## Supervised Muse Lane Does Not Appear On Board

Code Mower Board recognizes active Muse lanes running under `muse`, versioned
`muse-bin-*` executables, or `code-mower lane-delivery supervise -- ...`. If an
active lane is missing from Board:

1. Verify the process is still running with `ps aux | grep muse`.
2. Check that the working directory of the process is within an active lane checkout
   and not in `/` or an excluded path.
3. Ensure you are running Code Mower with Muse process discovery support, and
   restart `code-mower board serve` if it was started under an older package version.

## `provider.review_hygiene` Mentions Clear-Stale Workflows

Merge-authority reviewer lanes should have clear-stale workflows so old PASS or
BLOCKED labels are removed after a PR head changes. During first setup,
`doctor --adoption` may use the packaged starter config before a repository has
a committed `code-mower.yml`; in that starter mode missing clear-stale
workflows are warnings. If the workflows already exist in `.github/workflows/`,
doctor verifies them from the current checkout. With a real repository
`code-mower.yml`, missing configured clear-stale workflows are failures because
the gate would otherwise be able to trust stale review evidence.

An orchestrator-only observer without a checkout can use:

```bash
code-mower doctor --adoption --orchestrator-only --repo OWNER/REPO --json
```

Doctor uses the packaged-starter remote-observer plan in that case. Checkout
workflow paths, direct local-audit wrapper credentials and repo-path mappings,
unselected campaign providers, optional Cloud upload, and product-side pytest
are out of scope and stay out of the report. Repository Actions secret and
variable presence remains in scope because doctor reads metadata for the
explicit `OWNER/REPO`. If repository metadata is inaccessible, fix the one
`github.repo.metadata` remediation first: verify that `gh auth` can read the
target, including private-repository access when needed.

## Python Is Too Old

Use the checked-in developer wrapper instead of bare `python3`:

```bash
scripts/dev-python --version
scripts/dev-python -m unittest discover -s tests
```

The wrapper resolves Python 3.12+ and refuses old system Python shims.

## Installed Version Or Command Path Looks Wrong

After an upgrade, first check which installer is actually winning on `PATH`:

```bash
command -v code-mower
code-mower --version
```

If pipx should own the command, reinstall the exact release with cache bypass:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.5.2
hash -r
code-mower --version
```

If uv should own the command, avoid leaving an older pipx command earlier on
`PATH`. Either uninstall the pipx copy or call the uv binary by its absolute
path:

```bash
pipx uninstall code-mower
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.5.2
hash -r
command -v code-mower
code-mower --version
```

For sandboxed agents using pipx, keep tool files out of the product checkout
unless that directory is intentionally ignored:

```bash
export CODE_MOWER_AGENT_TOOLS="${RUNNER_TEMP:-$HOME/.cache}/code-mower-tools"
export PIPX_HOME="$CODE_MOWER_AGENT_TOOLS/pipx"
export PIPX_BIN_DIR="$CODE_MOWER_AGENT_TOOLS/bin"
export PIPX_LOG_DIR="$CODE_MOWER_AGENT_TOOLS/logs"
```

For a machine to count as active on a Code Mower pilot, it should be able to
report the install method, `command -v code-mower`, `code-mower --version`, the
posture-specific doctor command it ran, and `code-mower lanes status --repo
OWNER/REPO`. If different agents on the same workstation see different
versions, pin each agent to an isolated pipx or uv tool directory instead of
changing the shared command mid-PR.

## Headless Codex Campaign Says A Keyring Is Unavailable

The isolated Codex campaign home uses the OS keyring by default. A Linux host
without a desktop session can select the v1.5.x explicit file-backed mode instead:

```bash
export CODE_MOWER_CODEX_CAMPAIGN_AUTH_MODE=file
code-mower doctor --adoption --campaign
export CODEX_HOME="$HOME/.config/code-mower/provider-homes/codex"
printf '%s\n' "$OPENAI_API_KEY" | codex login --with-api-key
code-mower doctor --adoption --campaign
```

Run the first doctor command before `codex login`: it writes the selected
file-mode configuration into the isolated home. Authenticating first can place
the credential in the wrong store for the later campaign process.

Keep `CODE_MOWER_CODEX_CAMPAIGN_AUTH_MODE=file` in every runner or service
environment that starts the campaign. The first doctor call prepares the
restricted home and reports the credential missing; the login creates the
credential, and the second doctor call proves that exact isolated home is
ready. Code Mower accepts only a private regular `auth.json`, refuses symlinks
and other file types, and strips ambient API keys from the campaign child.

If you leave the variable unset, keyring mode remains selected and continues to
refuse a readable `auth.json`. For device-code login and the complete boundary,
see [Headless Linux campaign authentication](release-qualification.md#headless-linux-campaign-authentication).

## GitHub Auth Or Private Repo Checks Fail

Verify the GitHub CLI independently:

```bash
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
gh repo view OWNER/REPO --json nameWithOwner,visibility
```

Private repositories need tokens and app installations that can read pull
requests, comments, checks, and Actions metadata. If `doctor --github` reports
recent Actions billing blocks or expensive labeler workflows, review
[docs/github-setup.md](github-setup.md) before enabling hosted reviewer lanes.

## Manual Audit Wrapper Fails Before Reviewing

Direct Codex and Claude audit wrapper runs need two things that are easy to
miss on a cold setup:

- a GitHub posting token, either `GITHUB_TOKEN` in the environment or
  `--read-token-from-stdin`; and
- `--repo-paths` in the exact form
  `OWNER/REPO:/absolute/path/to/pr-head-checkout`.

The PR-head checkout must be separate from the Code Mower support checkout and
must already be at the pull request head. If the wrapper says the path is
missing, relative, or the current working directory, create a detached checkout
for the PR and retry. See [Local Audit Runner](local-audit-runner.md).

If the wrapper posts `UNKNOWN` because a provider could not produce structured
output, treat it as audit infrastructure, not a code-review BLOCKED verdict.
Retry once on the same head; if it repeats, keep the lane informational or
record an owner decision before relying on it. For Antigravity CLI audits,
headless runs require `--dangerously-skip-permissions` inside the temporary
sandbox; verify `agy --help` exposes `--dangerously-skip-permissions` if audits
fail with permission denials or UNKNOWN.

## Board URL Does Not Open From Another Machine

`code-mower board serve --repo OWNER/REPO` binds to loopback by default. The
printed localhost URL is local to that laptop, runner, VM, or hosted-agent
container. Open it from the same environment, or create your own tunnel when
you intentionally want to view it elsewhere. The Board remains read-only and
does not upload data unless you separately run a cloud command such as
`code-mower cloud board-snapshot --yes`.

## Cloud Upload Says The Token Is Missing After Restart

`code-mower cloud setup --token-stdin` writes a private token profile under
`~/.config/code-mower/tokens/` and records the newest setup as the current local
profile. Current `cloud doctor`, `cloud upload`, `cloud dogfood`,
`cloud reviewer-runs`, and `cloud repo-sync` commands can load that profile even
when `CODE_MOWER_CLOUD_TOKEN` is not set in the shell.

If the machine has several token profiles and no current profile marker, Code
Mower refuses to guess. Rerun with one of:

```bash
code-mower cloud doctor --install-id your-install-id
code-mower cloud upload .code-mower/cloud-benchmark-bundle \
  --token-file ~/.config/code-mower/tokens/your-install-id.env \
  --yes \
  --json
source ~/.config/code-mower/tokens/your-install-id.env
```

Doctor output lists token filenames and safe source commands only. It never
prints token values.

## Gate Is Green But Auto-Merge Does Not Turn On

The default GitHub Actions token can publish `code-mower/gate`, but it may not
be allowed to call GitHub's `enablePullRequestAutoMerge` mutation. For
unattended merges, configure a dedicated machine-user or GitHub App token as
`CODE_MOWER_GATE_AUTOMERGE_TOKEN`. `DISPATCH_TOKEN` remains a compatibility
fallback, but it should not be overloaded with broader permissions unless the
repo owner explicitly accepts that policy.

Also verify that branch protection requires `code-mower/gate` from Any source.
If it is bound to GitHub Actions instead, GitHub may show green workflow checks
while the required commit status remains pending.

## Gate Reports Audit In Flight

When the merge gate waits on an audit, the status log names the Actions run and
job it still considers non-terminal, for example:

```text
audit in flight: Codex (run 123456 job 'audit (codex)' queued since 2026-08-19T16:18:09Z)
```

Open that run first. If the lane has a trusted current-head `*-audit-done`
comment newer than the queued run, the gate treats the lane as complete on the
next evaluation. If the gate still waits, rerun `code-mower-gate.yml` with the
same PR number and head SHA, then check whether the named job is still queued or
whether GitHub returned stale Actions metadata.

## Init Used The Starter While The Repo Has Its Own Config

Successful `code-mower init` output names its config source: `packaged starter`
or `explicit repository config`. When a root `code-mower.yml` exists but the
packaged starter was selected, the plan also prints a `Setup drift` next step:
rerun with `code-mower init code-mower.yml --profile <profile> --dry-run`, or
compare with `code-mower migration setup-drift --repo-path .` following
[Upgrade An Existing Repository](upgrade-existing-repo.md). The default
config-selection behavior is unchanged; only the diagnosis is new.

## Controller Reports A Missing Or Invalid Config

`code-mower controller run` names the requested config and working directory on
local output only, then points at `code-mower init --easy --dry-run` for fresh
checkouts or [Upgrade An Existing Repository](upgrade-existing-repo.md) for
existing repos. Config failures return before any uploadable event is built, so
local paths never leave the machine.
