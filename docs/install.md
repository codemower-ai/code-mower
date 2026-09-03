# Install And Bootstrap

Code Mower requires Python 3.12 or newer. Use one install path per machine or
agent, then verify the installed command before touching a repository.

## Choose An Install Path

| Environment | Recommended install | Use when |
| --- | --- | --- |
| Laptop or workstation | `pipx` | You want one stable user-level Code Mower command. |
| Hosted agent, CI box, or minimal Linux VM | `uv tool install` | The machine already uses uv, lacks pipx, or should avoid changing shell startup files. |
| Code Mower contributor checkout | editable venv | You are changing Code Mower itself and need tests against this checkout. |

## Cold Install Vs Upgrade

A cold install means this machine does not already have the `code-mower`
command on `PATH`. Pick one install path from the matrix, install the pinned
package, then verify both the command path and version:

```bash
command -v code-mower
code-mower --version
```

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
before copying new generated output into the repo:

```bash
code-mower migration setup-drift --repo-path . --json
```

The drift report is read-only and metadata-only. It classifies generated setup
paths without including file contents or diffs.
For the full reviewed upgrade PR sequence, including `repo-only` handling and
wrapper/pin drift checks, see
[Upgrade An Existing Repository](upgrade-existing-repo.md).

## Laptop Or Workstation

Install with pipx and an explicit Python 3.12+ interpreter:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.3
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
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.3
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
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.3
"$PIPX_BIN_DIR/code-mower" --version
```

## Hosted Agent, CI Box, Or Minimal Linux VM

Use uv when the environment does not have pipx or should stay isolated from the
interactive shell profile:

```bash
uv python install 3.12
uv tool install --python 3.12 code-mower==1.0.3
code-mower --version
```

If the uv tool directory is not on `PATH`, use uv's printed path hint or run the
installed command directly from the uv tool bin directory for that session.

To replace an existing uv tool install with an exact release:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.3
code-mower --version
```

## Switching Between pipx And uv

Avoid leaving two different `code-mower` commands competing on `PATH`. If this
machine should switch from pipx to uv, first record the current path/version,
then uninstall or stop using the old command:

```bash
command -v code-mower
code-mower --version
pipx uninstall code-mower
uv python install 3.12
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.3
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
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.3
code-mower --version
```

For uv:

```bash
uv python install 3.12
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.3
code-mower --version
```

Before PyPI has the candidate, rehearse the local wheel from a source checkout:

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
code-mower doctor --adoption --repo OWNER/REPO --json
code-mower lanes status --repo OWNER/REPO
code-mower board serve --repo OWNER/REPO
```

For an existing repository with older Code Mower generated files, inspect drift
before copying a newly generated tree into the repo:

```bash
code-mower migration setup-drift --repo-path . --json
code-mower migration setup-drift --repo-path .
```

The drift report is read-only. It compares the current generated setup output
against tracked Code Mower files and classifies paths as `same`, `differs`,
`new`, `repo-only`, or `missing-from-output`. Use it before an upgrade PR so
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

In those observer/coordinator postures, missing local wrapper environment
variables and missing `DISPATCH_TOKEN` setup are surfaced as owner setup or
promotion tasks, not as proof the install is broken. Use the default
reviewer-gate posture on the machine that will actually run local audit
wrappers or unattended dispatch.

Then follow [Try Code Mower In 10 Minutes](try-in-10-minutes.md) for the first
audited PR or [Build Loop In 30 Minutes](build-loop-in-30-minutes.md) after the
reviewer gate is working.
