# Install And Bootstrap

<!-- code-mower:release-facts:start -->
v1.5.2 uses the exact install pin `code-mower==1.5.2` and requires Python
3.12 or newer. Confirm that version is published on the selected index, then
verify the command path and version after installing. The
[qualification contract](v152-qualification.md) defines the required evidence. Use the
[candidate runbook](v152-release-runbook.md) for prepublication local-wheel rehearsals.
<!-- code-mower:release-facts:end -->


Code Mower requires Python 3.12 or newer. Use one install path per machine or
agent, then verify the installed command before touching a repository.

After installation, `code-mower init --interactive` lets you choose the initial
participants with Claude Code and Codex preselected. Agents and scripts can
use `code-mower init --with claude,codex,devin`. Both preview the configuration;
add `--apply` to write reviewable setup files. See
[Participants And Sessions](sessions.md) for the complete selection flow.

## Choose An Install Path

| Environment | Recommended install | Use when |
| --- | --- | --- |
| Laptop or workstation | `pipx` | You want one stable user-level Code Mower command. |
| Hosted agent, CI box, or minimal Linux VM | `uv tool install` | The machine already uses uv, lacks pipx, or should avoid changing shell startup files. |
| Code Mower contributor checkout | editable venv | You are changing Code Mower itself and need tests against this checkout. |

pipx and uv are the recommended paths because each keeps Code Mower and its
dependencies in their own isolated environment, separate from system Python and
from any other tool. Install the installer itself from its official
documentation rather than from a copied shell snippet:

- pipx: <https://pipx.pypa.io/stable/installation/>
- uv: <https://docs.astral.sh/uv/getting-started/installation/>

Code Mower does not publish, and you should not use, a
`curl ... | sh` bootstrap for either installer.

On a hosted Linux image that has Python 3.12 with `venv` support but neither
installer, bootstrap uv in a small isolated environment instead of piping a
remote script into a shell or writing into an externally managed system
Python:

```bash
UV_BOOTSTRAP="$HOME/.local/share/code-mower-bootstrap/uv"
python3.12 -m venv "$UV_BOOTSTRAP"
"$UV_BOOTSTRAP/bin/python" -m pip install --upgrade uv
"$UV_BOOTSTRAP/bin/python" -m uv --version
"$UV_BOOTSTRAP/bin/python" -m uv tool install --python 3.12 code-mower==1.5.2
```

The module form works even when uv is not yet on `PATH`. After installation,
follow uv's printed path hint, then verify
`command -v code-mower` and `code-mower --version` before using the repository.

Confirm the installer is on `PATH` before installing Code Mower with it:

```bash
command -v pipx
command -v uv
```

An empty result means that installer is not installed or not on `PATH` for this
shell; fix that first rather than falling back to an ambient `pip install`.

## Cold Install Vs Upgrade

A cold install means this machine does not already have the `code-mower`
command on `PATH`. Pick one install path from the matrix, install the pinned
package, then verify both the command path and version:

```bash
command -v code-mower
code-mower --version
```

`command -v code-mower` must print the path belonging to the installer you
chose, and `code-mower --version` must print `code-mower 1.5.2`. A version that
does not match, or a path from a different installer, means an older command is
still winning on `PATH`; resolve that before running anything against a
repository.

An upgrade means `code-mower` already exists. Before changing it, record the
current command path and version, then choose whether this machine should keep
using the same installer or switch installers:

```bash
command -v code-mower
code-mower --version
```

An install or upgrade is not complete until the active command on `PATH` is the
intended one, `code-mower --version` matches the chosen release, the
posture-appropriate doctor command has no unexplained failures, and
`code-mower lanes status --repo OWNER/REPO` can summarize the repo or clearly
explain why GitHub/local visibility is unavailable.

For an existing repository with older generated files, inspect setup drift
before running `init --apply` or copying new generated output into the repo:

```bash
code-mower migration setup-drift --repo-path . --json
```

The drift report is read-only and metadata-only. It classifies generated setup
paths without including file contents or diffs.
For the full reviewed upgrade PR sequence, including `repo-only` handling and
wrapper/pin drift checks, see
[Upgrade An Existing Repository](upgrade-existing-repo.md).

## Local audit workflow publication

The local audit publisher introduced in v1.5.0 remains included in v1.5.2. It
generates `.github/workflows/local-audit-publication.yml` alongside the updated
labelers, gate and standalone verification helpers. Commit that generated set to the
repository's default branch before switching local Claude/Codex wrappers to
workflow publication. A PR's copy of the verifier has no publication authority.
Before v1.5.2 is published, use its reviewed candidate wheel; afterward, use the
exact published pin below.

The generated self-hosted audit job seals the verdict digest in an immutable
Actions step before dispatch. The publisher requires that exact source
run/job/attempt and PR head; a personal PAT alone cannot mint a reviewer verdict.
The job dispatches with its short-lived workflow
token (Contents write and Actions read), and the publisher posts with its own
repository token (Issues write). No new long-lived bot credential is required.
Existing `DISPATCH_TOKEN` uses elsewhere in the build loop remain separate.
For saved-artifact publication, explicit direct compatibility and terminal-result
verification, follow [Local Audit Runner](local-audit-runner.md#verified-workflow-publication).

## Laptop Or Workstation

Install with pipx and an explicit Python 3.12+ interpreter:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.5.2
code-mower --version
```

If `code-mower` is not on `PATH` after install:

```bash
pipx ensurepath
exec "$SHELL" -l
```

To follow a future prerelease instead of the pinned supervised-pilot release:

```bash
pipx install --python "$CODE_MOWER_PYTHON" --pip-args="--pre" code-mower
```

To replace an existing pipx install with an exact release, use `--force` so the
old venv cannot keep serving the previous package:

```bash
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.5.2
code-mower --version
```

For sandboxed agents that need pipx but should not write to the normal user
tool directories, set pipx directories explicitly before installing:

```bash
export CODE_MOWER_AGENT_TOOLS="${RUNNER_TEMP:-$HOME/.cache}/code-mower-tools"
export PIPX_HOME="$CODE_MOWER_AGENT_TOOLS/pipx"
export PIPX_BIN_DIR="$CODE_MOWER_AGENT_TOOLS/bin"
export PIPX_LOG_DIR="$CODE_MOWER_AGENT_TOOLS/logs"
mkdir -p "$PIPX_HOME" "$PIPX_BIN_DIR" "$PIPX_LOG_DIR"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.5.2
"$PIPX_BIN_DIR/code-mower" --version
```

## Hosted Agent, CI Box, Or Minimal Linux VM

Use uv when the environment does not have pipx or should stay isolated from the
interactive shell profile:

```bash
uv python install 3.12
uv tool install --python 3.12 code-mower==1.5.2
code-mower --version
```

If the uv tool directory is not on `PATH`, use uv's printed path hint or run the
installed command directly from the uv tool bin directory for that session.

To replace an existing uv tool install with an exact release:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.5.2
code-mower --version
```

## Optional Coworker Support

The base install is enough for Claude, Codex, sessions, reviews, and local
reports. Install the `coworker` extra only when a repository will use an
authorized Coworker organizational-context connection. Use the same installer
and interpreter that own the `code-mower` command.

With pipx:

```bash
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" \
  'code-mower[coworker]==1.5.2'
code-mower context --help
```

With uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower \
  'code-mower[coworker]==1.5.2'
code-mower context --help
```

For a contributor checkout:

```bash
.venv/bin/python -m pip install -e ".[test,coworker]"
.venv/bin/code-mower context --help
```

Do not install this extra through an unrelated ambient `python -m pip`; that
can place the dependency outside the pipx, uv, or contributor environment that
runs Code Mower. Account identity, workspace selection, and credentials remain
in the private connection store. See [Optional Organizational Context
Setup](context-setup.md) for the connection flow.

## Switching Between pipx And uv

Avoid leaving two different `code-mower` commands competing on `PATH`. If this
machine should switch from pipx to uv, first record the current path/version,
then uninstall or stop using the old command:

```bash
command -v code-mower
code-mower --version
pipx uninstall code-mower
uv python install 3.12
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.5.2
hash -r
command -v code-mower
code-mower --version
```

If the old pipx command must stay for another agent, call the uv-installed
binary by its absolute path or adjust only that agent's `PATH`. Do not change a
shared workstation install while another builder owns an active PR branch.

## Release Rehearsal Installs

When validating a newly published release, bypass installer caches before deciding
that PyPI or the package is broken.

For pipx:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.5.2
code-mower --version
```

For uv:

```bash
uv python install 3.12
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.5.2
code-mower --version
```

To rehearse an unpublished build -- a release candidate, or a local source
change -- install the local wheel from a source checkout instead of the index:

```bash
scripts/dev-python -m build
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" dist/code_mower-*.whl
uv tool install --python 3.12 --reinstall dist/code_mower-*.whl
```

If an exact-version install fails right after publication, wait a few minutes
and retry with the cache-bypass command for your installer. Treat repeated
"no matching distribution" or index timeouts as package-index/network
propagation until the same command succeeds or the version is visible on PyPI.
Treat a successful install with the wrong `code-mower --version`, failed CLI
startup, or failed first-user rehearsal as a Code Mower release issue.

## Contributor Checkout

From the Code Mower source checkout, use the checked-in development wrapper so
old system Python shims cannot enter the release path:

```bash
scripts/dev-python --version
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/code-mower --version
```

Run tests through the same interpreter:

```bash
.venv/bin/python -m unittest discover -s tests
```

Avoid hand-wiring `PYTHONPATH` or source import paths to run the CLI. The
editable venv keeps contributor checks on the same package-first path as the
public install.

## Bootstrap Python Without Sudo

Use the package manager that fits the machine:

- macOS: Homebrew, pyenv, asdf, or uv.
- Linux: the distribution package manager, pyenv/asdf, or uv.
- Hosted agents: uv is usually the least invasive option.

If you cannot change the global machine, install Python under the user account
with uv or pyenv, then install Code Mower as a user-level tool. Do not store
GitHub or cloud tokens in the repository to work around missing system access.

## GitHub CLI

Code Mower is GitHub-first. Verify `gh` before running repository diagnostics:

```bash
gh auth login -h github.com -s repo,workflow,read:org
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
gh repo view OWNER/REPO
```

Hosted agents may use a different authenticated channel for repository work,
but Code Mower's GitHub-facing commands still use `gh` unless a specific
command documents another path.

## Multi-Agent Coexistence

Multiple agents can use Code Mower safely as long as they keep write ownership
clear:

- Share the repository `code-mower.yml` only through normal pull requests.
- Keep secrets and cloud token profiles under the user config directory,
  usually `~/.config/code-mower/tokens/`, never in the repository.
- Prefer isolated tool installs for separate hosted agents or boxes.
- On a shared workstation, one pipx install is fine when agents run as the same
  user and agree on the same released version.
- Use separate checkouts or worktrees for concurrent builders; keep one writer
  per PR branch.
- Inspect generated `.code-mower.generated/` output before copying it into a
  product repository.

Code Mower's default cloud and shareable outputs stay metadata-only: no source,
raw diffs, transcripts, issue body text, raw stdout/stderr, auth output, local
secret values, or secrets.

When onboarding another agent, use
[Orchestrator Prompt Pack](orchestrator-prompt-pack.md) so every participant
reports cold-install versus upgrade status, installer choice, exact version,
doctor posture, Board URL, and owner-only setup needs in the same shape.

## First Repository Commands

After install, choose the path that matches the repository.

For a cold repository that has not adopted Code Mower, start with the manual
reviewer-gate path:

```bash
code-mower init --easy
code-mower doctor --adoption --repo OWNER/REPO --concise
code-mower doctor --adoption --repo OWNER/REPO --json
code-mower lanes status --repo OWNER/REPO
code-mower board serve --repo OWNER/REPO
```

Read `--concise` first: every check still runs, and the posture-scoped summary
leads with active failures and owner actions and counts the remaining warnings
by group. Keep `--json` for the complete machine-readable evidence, and use
`--advanced` when you want the full text list of every check. The concise view
is a reading order, not a smaller check set.

A Board that was just started can still be binding its port when a doctor
snapshot runs. `code-mower doctor` now re-observes Board visibility for a short
bounded grace in that case only, so the snapshot agrees with `code-mower board
list`. A Board that is visible is reported immediately, so a stopped,
wrong-repository, stale-version, or unhealthy Board is never hidden by the wait,
and a host with no `lsof`/`ss` still reports the missing listener inventory
without retrying. Set `CODE_MOWER_BOARD_STARTUP_GRACE_SECONDS=0` to turn the
wait off; any other value is a budget in seconds, capped at 10, and a value that
is not a finite non-negative number falls back to the short default rather than
lengthening the wait. The JSON report records the timing it actually used under
`startup_grace`.

For an existing repository with older Code Mower generated files, inspect drift
before copying a newly generated tree into the repo:

```bash
code-mower migration setup-drift --repo-path . --json
code-mower migration setup-drift --repo-path .
```

The drift report is read-only. It compares two named operands:

- source: the generated setup from the installed Code Mower package, for the
  config and profile the report names;
- target: the tracked repository files in the checkout you point `--repo-path`
  at.

Every classification is defined in terms of those two operands, and the report
prints the definitions with the counts:

| Classification | Meaning | Side |
| --- | --- | --- |
| `same` | present on both sides with identical bytes | both |
| `differs` | present on both sides with different bytes | both |
| `new` | present in the generated setup only | source only |
| `repo-only` | tracked in the repository only | target only |
| `missing-from-output` | named by the setup plan but not readable from the installed package | source unreadable |

The comparison is presence and bytes only. A `differs` entry does not say which
side is newer: check the installed package version and the repository history
before deciding which one to keep. The JSON report carries the same metadata
under `comparison`, and each file entry carries its `side`.

The text report also names the
configuration source (`Config source: packaged starter ...` or
`Config source: explicit repository config ...`), using the same terms as
`code-mower init`. Use it before an upgrade PR so
you can review workflow/wrapper changes without source diffs in the report.
Follow [Upgrade An Existing Repository](upgrade-existing-repo.md) when applying
those changes to a repo that already has generated support files.

If this machine is a hosted-builder observer or orchestrator only, and will not
run Codex or Claude local CLI audits itself, keep the GitHub/cloud/setup checks
but skip local CLI probes:

```bash
code-mower doctor --adoption --hosted-builders --repo OWNER/REPO --json
code-mower doctor --adoption --orchestrator-only --repo OWNER/REPO --json
```

Those commands expect the current checkout to contain `code-mower.yml`. On a
remote-only host with no repository checkout, select the maintained packaged
starter explicitly through the easy preset:

```bash
code-mower doctor --easy --orchestrator-only --repo OWNER/REPO --json
```

The resulting filesystem and generated-workflow checks describe the packaged
starter, not the remote repository. Use them for installation posture; use
`lanes status --repo OWNER/REPO` for current remote PR and gate visibility.

Doctor JSON is local diagnostic evidence. It can include bounded local paths
such as the selected config, executable, or workflow path. Review or redact it
before attaching it to an issue or uploading it; the concise text view is the
safer first status summary.

In those observer/coordinator postures, missing local wrapper environment
variables and missing `DISPATCH_TOKEN` setup are surfaced as owner setup or
promotion tasks, not as proof the install is broken. Adoption text output
prints the posture hint before provider-lane detail, so a default
reviewer-gate warning on an observer host reads as a posture mismatch, not a
broken install. Use the default
reviewer-gate posture on the machine that will actually run local audit
wrappers or unattended dispatch.

Then follow [Try Code Mower In 10 Minutes](try-in-10-minutes.md) for the first
audited PR or [Build Loop In 30 Minutes](build-loop-in-30-minutes.md) after the
reviewer gate is working.

## What The Install Does And Does Not Change

Five boundaries survive install, upgrade, and reinstall. Adoption feedback keeps
returning to them, so they are stated here rather than only in the reference:

- **`init --easy` previews and changes nothing.** `--apply` writes a reviewable
  generated tree under `.code-mower.generated/`; it does not copy files into
  your repository, start a provider, enable auto-merge, or upload anything. In a
  checkout with no `code-mower.yml`, init falls back to the packaged starter
  configuration and says so (`Config source: packaged starter ...`), rather than
  failing.
- **A session lease is local, explicit, and releasable.** An eligible
  `code-mower session start` takes a mutating single-orchestrator lease with a
  12-hour default. `code-mower session show --current` and `code-mower session
  lease show` are read-only. A later process renews or releases the same lease
  by passing the session ID: `code-mower session lease renew --session-id
  SESSION_ID`, then `code-mower session lease release --session-id SESSION_ID`
  after its writers stop. Use `--dry-run` for a preview or `--no-lease` for a
  saved read-only brief.
- **Headless authorization uses an explicit credential source.** Codex's
  isolated campaign home uses the OS keyring by default. On headless Linux,
  set `CODE_MOWER_CODEX_CAMPAIGN_AUTH_MODE=file`, run `code-mower doctor
  --adoption --campaign` once to prepare the restricted home, authenticate that
  exact `CODEX_HOME`, then rerun doctor. File mode accepts only a private regular
  `auth.json`; keyring mode still refuses that file, and neither mode consumes
  ambient token variables. You can instead dispatch from a desktop-keyring
  host, use `doctor --hosted-builders` or `--orchestrator-only`, or set
  `CODE_MOWER_CAMPAIGN_AUTH_PROBE=0` for a capability-only check. See
  [Headless Linux campaign authentication](release-qualification.md#headless-linux-campaign-authentication).
- **The Board is loopback-only and does not upload.** `code-mower board serve
  --repo OWNER/REPO` binds a loopback host, redacts local paths by default, and
  prints a URL that is local to that machine unless you build your own tunnel.
  `code-mower board stop --repo OWNER/REPO` resolves exactly one known binding:
  an ambiguous, duplicate, or contradicting repository/port/PID selection stops
  nothing, and a port that a keepalive-managed service would immediately reclaim
  is refused rather than reported as stopped. For a Board that outlives the
  shell, see [Board Service Lifecycle](board-service-lifecycle.md); macOS is the
  supported platform and every other platform refuses.
- **Unselected integrations stay quiet.** Ordinary no-campaign adoption asks for
  no campaign-auth owner action. Optional surfaces -- Coworker, Jira Cloud,
  Graphify, Slack, cloud sharing -- are opt-in and add nothing to a default
  Claude + Codex install until you select them.
