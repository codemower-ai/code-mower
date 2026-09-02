# Install And Bootstrap

Code Mower requires Python 3.12 or newer. Use one install path per machine or
agent, then verify the installed command before touching a repository.

## Choose An Install Path

| Environment | Recommended install | Use when |
| --- | --- | --- |
| Laptop or workstation | `pipx` | You want one stable user-level Code Mower command. |
| Hosted agent, CI box, or minimal Linux VM | `uv tool install` | The machine already uses uv, lacks pipx, or should avoid changing shell startup files. |
| Code Mower contributor checkout | editable venv | You are changing Code Mower itself and need tests against this checkout. |

## Laptop Or Workstation

Install with pipx and an explicit Python 3.12+ interpreter:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.6.0b3
code-mower --version
```

If `code-mower` is not on `PATH` after install:

```bash
pipx ensurepath
exec "$SHELL" -l
```

To follow the newest prerelease instead of the pinned friendly-user beta:

```bash
pipx install --python "$CODE_MOWER_PYTHON" --pip-args="--pre" code-mower
```

## Hosted Agent, CI Box, Or Minimal Linux VM

Use uv when the environment does not have pipx or should stay isolated from the
interactive shell profile:

```bash
uv python install 3.12
uv tool install --python 3.12 code-mower==0.6.0b3
code-mower --version
```

If the uv tool directory is not on `PATH`, use uv's printed path hint or run the
installed command directly from the uv tool bin directory for that session.

## Release Rehearsal Installs

When validating a newly published beta, bypass installer caches before deciding
that PyPI or the package is broken.

For pipx:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==0.6.0b3
code-mower --version
```

For uv:

```bash
uv python install 3.12
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==0.6.0b3
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
.venv/bin/python -m pip install -e .
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
gh auth status
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

## First Repository Commands

After install, start with the manual reviewer-gate path:

```bash
code-mower init --easy
code-mower doctor --adoption --repo OWNER/REPO --json
code-mower lanes status --repo OWNER/REPO
code-mower board serve --repo OWNER/REPO
```

If this machine is a hosted-builder observer or orchestrator only, and will not
run Codex or Claude local CLI audits itself, keep the GitHub/cloud/setup checks
but skip local CLI probes:

```bash
code-mower doctor --adoption --hosted-builders --repo OWNER/REPO --json
code-mower doctor --adoption --orchestrator-only --repo OWNER/REPO --json
```

Then follow [Try Code Mower In 10 Minutes](try-in-10-minutes.md) for the first
audited PR or [Build Loop In 30 Minutes](build-loop-in-30-minutes.md) after the
reviewer gate is working.
