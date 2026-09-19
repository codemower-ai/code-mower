# Local Audit Runner

For the full macOS service setup, use
[Self-Hosted Mac Runner](self-hosted-mac-runner.md).

Use `./run.sh` first from the macOS account that owns `gh`, Codex, and Claude
logins. Install the GitHub runner as a service only after the generated local
audit workflow passes the same smoke checks.

For service mode, set `USER`, `LOGNAME`, `SHELL`, and `LANG` in the runner
`.env`, along with `CODE_MOWER_PYTHON` as described below, then fully recycle
the listener after edits. `svc.sh stop/start` may
leave an older `Runner.Listener` process alive with the previous environment.

## Stable Python environment

The source wrappers use `scripts/dev-python` and require Python 3.12+ with the
runtime dependencies declared in the trusted support checkout's `pyproject.toml`
(currently `PyYAML>=6.0` and `packaging>=23.2`). A Python executable alone is not
enough. Provision a dedicated virtual environment outside the runner's disposable
work directories, using an installed Python 3.12+ interpreter and a reviewed
Code Mower source checkout:

```bash
python3.12 -m venv "$HOME/.local/share/code-mower/audit-venv"
"$HOME/.local/share/code-mower/audit-venv/bin/python" -m pip install /absolute/path/to/reviewed/code-mower
"$HOME/.local/share/code-mower/audit-venv/bin/python" -m pip check
```

Installing that checkout installs its declared runtime dependencies. Repeat the
install when those requirements change. Never install dependencies from a PR
checkout into this trusted environment. Configure the runner `.env` with the
**literal absolute path**, for example:

```dotenv
CODE_MOWER_PYTHON=/absolute/path/to/audit-venv/bin/python
```

Replace this example with the full path to the environment created above;
`.env` does not expand `$HOME` or `~`.
Keep the interpreter path stable across support-checkout resets and recycle the
listener after configuring it. Do not rely on shell activation or an interactive
shell's Python selection. `scripts/dev-python` prefers a `.venv` in its current
working directory over `CODE_MOWER_PYTHON`, so keep that job directory free of a
shadowing `.venv`; the preflight below rejects a different selected interpreter.

After the trusted default-branch support checkout, run this preflight in an
actual runner job with `SUPPORT_PATH` set to that checkout. Run from the job
workspace, never the PR checkout. It checks the same interpreter selector and
source imports used by the wrappers and metadata upload, without invoking a
provider or uploading anything:

```bash
set -euo pipefail
export PYTHONPATH="${SUPPORT_PATH}/src${PYTHONPATH:+:${PYTHONPATH}}"
if ! {
  test -n "${CODE_MOWER_PYTHON:-}" &&
  test -x "${CODE_MOWER_PYTHON}" &&
  "${CODE_MOWER_PYTHON}" -m pip check &&
  "${SUPPORT_PATH}/scripts/dev-python" - <<'PY'
import os
import sys
from pathlib import Path
import tomllib

assert sys.version_info >= (3, 12)
assert os.path.abspath(sys.executable) == os.path.abspath(os.environ["CODE_MOWER_PYTHON"])
from importlib.metadata import version
from packaging.requirements import Requirement
import yaml
import code_mower.cli

support = Path(os.environ["SUPPORT_PATH"])
assert Path(code_mower.cli.__file__).resolve() == (support / "src/code_mower/cli.py").resolve()
project = tomllib.loads((support / "pyproject.toml").read_text())["project"]
for declared in project["dependencies"]:
    requirement = Requirement(declared)
    if requirement.marker is None or requirement.marker.evaluate():
        assert requirement.specifier.contains(version(requirement.name))
PY
} >/dev/null 2>&1; then
  echo "::error::Code Mower Python preflight failed; check CODE_MOWER_PYTHON and trusted runtime dependencies."
  exit 1
fi
echo "Code Mower Python preflight passed"
```

Keep installation diagnostics and any provider stdout/stderr in private local
logs. Do not dump the runner environment, tokens, auth status payloads, or raw
provider output into Actions logs or artifacts.

## Runner account preflight

Check `~/Library/LaunchAgents/actions.runner.*.plist` after `svc.sh install`.
If it contains `SessionCreate=true`, remove that key and unload/reload the
LaunchAgent or recycle the listener. That launchd setting creates a new security
session without login-keychain access, so Claude Code OAuth can look logged in
interactively while runner jobs return `Not logged in`.

Verify from a runner job, not only from an interactive terminal:

```bash
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
codex --version
claude auth status >/dev/null 2>&1 && echo "claude auth ok" || { echo "claude auth NOT ready"; false; }
devin auth status >/dev/null 2>&1 && echo "devin auth ok" || { echo "devin auth NOT ready"; false; }
```

Check only providers enabled for this runner. These are auth readiness checks;
they do not start a provider canary or repeat an audit.

Local Claude and Codex merge-authority audits normally publish through
`.github/workflows/local-audit-publication.yml`. The self-hosted audit job uses
its short-lived `GITHUB_TOKEN` to dispatch this default-branch workflow. The
publisher posts as `github-actions[bot]`; its receipt job then sends a narrow
`repository_dispatch` containing only that publication run ID. The labelers fetch
the completed run and verify its workflow identity, receipt, published comment,
lane and current PR head before applying a label. This resets GitHub's three-level
`workflow_run` chain without trusting the dispatch payload or bot comment event.
No additional bot account or long-lived bot credential is needed.

For direct compatibility posting and other build-loop operations, keep the
existing `DISPATCH_TOKEN` and expiry configuration. Direct human-authored audits
still have the normal account-based reviewer floor.

## Verified workflow publication

Install the publisher, both updated labelers, gate, and generated `tools/`
helpers and `local-audit-request.yml` together on the **default branch** before enabling the new wrapper.
Use a source/candidate installation containing this feature until a release
includes it; installing the currently pinned release alone does not activate
new publication code. No release or deployment is performed by this setup.

Claude/Codex CLI runs default to `--publication workflow`. To publish a saved
structured verdict generated and sealed by the trusted local-audit workflow,
without invoking either provider again:

```bash
tools/run_claude_audit_pr.sh --publish-verdict-artifact /path/to/verdict.json
tools/run_codex_audit_pr.sh --publish-verdict-artifact /path/to/verdict.json
```

The caller needs repository-dispatch access (Contents write), Pull requests read,
Issues read and Actions read. The generated runner workflow grants these to its
short-lived token. A local caller can retry an already sealed artifact with its
existing authenticated token; dispatch permission alone grants no reviewer
authority. Unsealed artifacts from arbitrary local/PAT runs are refused.
Publication waits up to 15 minutes for the terminal run and
its verified comment. A dispatch timeout is an unknown delivery result: inspect
the existing run and PR reservation before retrying. Do not rerun the model just
to recover a publication result.

Failed publisher commands emit exactly one bounded line:
`Local audit publication refused [CODE].` Look up the code in `REFUSAL_CODES`
in `tools/audit_publication.py` at the publication run's immutable workflow SHA.
For example, `INVALID_DISPATCH_SCHEMA` identifies the dispatch payload shape,
`WRONG_WORKFLOW_REF_ATTEMPT` identifies the hosted workflow environment binding,
and `SOURCE_REVIEWER_SEAL_MISSING_OR_AMBIGUOUS` identifies the source seal check.
The catalog contains only fixed reason/code literals; it never prints submitted
values, exception text, response bodies, tokens, URLs, paths or identities.
Unrecognized reasons and unexpected exceptions emit `INTERNAL_ERROR` with no
traceback. A code does not relax any publication check or authorize a retry:
inspect the existing reservation and receipt first, then reuse the same sealed
metadata only while its original head and freshness checks still hold.

Only canonical metadata leaves the machine: schema, numeric repository ID, PR
number, reviewer lane, PASS/BLOCKED, full start/end head SHAs, artifact creation
time, and the originating audit run ID/attempt. The repository name, comment prose, findings, code, prompts, transcript,
paths and provider output stay local. The SHA-256 digest covers those exact
canonical metadata bytes. The publisher accepts only equal full start/end SHAs,
an open PR at that SHA, and artifacts no more than 24 hours old. UNKNOWN, STALE,
quarantined, informational and context-bound artifacts cannot be promoted by
this transport. Context-bound reviews retain their context-aware direct path;
publication does not strip a context requirement into an unbound PASS.

The publisher runs immutable default-branch code on GitHub's hosted runner and
never checks out a PR. It creates a neutral reservation, rechecks the head,
posts the existing lane trailer and `CODE_MOWER_AUDIT_RUN`, then checks the head
again. An immutable receipt job binds the metadata digest to the created comment
ID and publishing run. Consumers require successful run completion and that
receipt; a copied or edited comment cannot acquire that binding. Reservations
and successful run receipts reject a second publication of the same metadata.
Deleting a comment does not make its receipt reusable. Re-running a workflow
attempt is refused. Keep receipts and reservations for at least the 24-hour
artifact lifetime. The global publication concurrency group serializes claims;
GitHub may cancel an older queued dispatch, which requires inspecting its result.

The source job stages the metadata, independently validates it, and completes a
`Code Mower reviewer seal <digest>` step before dispatch. The publisher verifies
that immutable Actions step record in the matching `audit (claude|codex)` job,
source run ID/attempt, trusted `local-cli-audit.yml` `repository_dispatch` event,
same repository and default branch. The sealed digest binds the PR, lane and
full start/end head. The small `local-audit-request.yml` trigger requests the
review; the source workflow validates the request against the live PR before
starting a provider. Both source and publisher use `repository_dispatch`, which
always executes default-branch code: a builder cannot counterfeit a source job
using a workflow on an alternate PR base branch.
The enclosing source job may remain in progress while waiting for publication.
A fabricated digest, a builder workflow, a non-default base, a rerun, or a
personal-PAT dispatch without that seal cannot create merge authority. Both
PASS and BLOCKED are sealed. Only allowlisted metadata appears in the seal.

The trusted source workflow checks out the immutable default-branch run SHA. It does not
inject dispatch secrets for Claude/Codex. The wrapper clears configured token
aliases; provider children additionally lose Actions runtime credentials and
command-file variables. Workflow commands are disabled while provider output
is printed. Builder-lineage and context checks remain mandatory.

For an explicit emergency/compatibility path, use `--publication direct` on a
new audit or `--repost-verdict-artifact` to repost a saved direct comment. Those
paths do not create the new workflow attestation and may require the existing
owner decision process. There is no automatic fallback to a direct merge-authority verdict. Quarantined,
stale and inconclusive results leave a fixed, metadata-only UNKNOWN notice so
operators can requeue them; these notices carry no audit trailer or attestation.
Reviewer stdout/stderr stays in a private runner-local log and is never echoed
or uploaded to GitHub. Devin CLI retains its separate legacy trigger and direct
transport when selected.

## Wrapper Contract

The Codex and Claude audit wrappers need a GitHub token and a separate
PR-head checkout.

Token input must use one of these forms:

- `GITHUB_TOKEN` in the process environment.
- `--read-token-from-stdin` with the token piped as the first stdin line.

The generated self-hosted workflow uses `--read-token-from-stdin` so the token
does not appear in the Python process's initial environment. Direct local runs
may use `GITHUB_TOKEN` when that exposure is acceptable for the operator's
machine.

During package-install adoption, `tools/run_codex_audit_pr.sh`,
`tools/run_claude_audit_pr.sh`, and `tools/run_devin_cli_audit_pr.sh` use the
installed `code-mower` on `PATH` while
`tools/code_mower_standalone_pin.env` still has placeholder values. Configure
the standalone pin file, or set `CODE_MOWER_USE_STANDALONE=1`, when you are
ready to shadow a reviewed Code Mower source ref through `tools/code_mower`.

Repository paths must use this format:

```text
OWNER/REPO:/absolute/path/to/pr-head-checkout
```

Pass it with `--repo-paths` or with the lane-specific env var:

```bash
tools/run_codex_audit_pr.sh \
  --repo OWNER/REPO \
  --pr 123 \
  --repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout

tools/run_claude_audit_pr.sh \
  --repo OWNER/REPO \
  --pr 123 \
  --repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout

tools/run_devin_cli_audit_pr.sh \
  --repo OWNER/REPO \
  --pr 123 \
  --repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout
```

The Devin CLI lane uses the `needs-devin-cli-audit` label, runs with
`devin --sandbox --permission-mode auto` (source-edit tools are not approved
for this reviewer), and never passes `--export`, `--continue`, or `--resume`.
Its stdout and stderr are streamed under independent byte bounds against a
wall-clock deadline; a timeout or an overflow on either stream terminates the
whole spawned process group and fails closed to `needs-devin-cli-audit` with a
metadata-only reason, never raw provider output.

The lane has no `--allow-dirty` escape hatch (passing it is rejected by the
argument parser). The checkout must be clean at the exact PR head before the
provider runs, and clean again afterwards, so a verdict is only ever produced
for the committed tree it claims to have reviewed. Commit or stash local
changes before invoking it.

The provider itself never runs in that checkout. It runs in a disposable clone
of the exact head, made with copied objects (no hardlinks and no shared object
store) and with its remote removed, so the clone carries no credentials and no
push target. The clone must also be clean at the exact head before and after
the run, and it is deleted on every exit path -- success, provider failure,
timeout, or unexpected exception -- so a persistent write to an ignored file or
to git metadata, which ordinary `git status` never reports, cannot outlive the
audit. The reusable checkout is used only for the trusted exact-head and diff
computation and for the pre/post GitHub head verification.

The path must point at an existing checkout of the pull request head. It must
not be the Code Mower support checkout or the wrapper's current working
directory. This separation keeps product PR code out of the support checkout
and lets the wrapper verify the PR head SHA before posting a verdict.

Wrapper and doctor errors for missing tokens, malformed `--repo-paths`, relative
paths, missing directories, or same-directory checkouts point back to this page.

Generated local-audit workflows run one matrix job per configured lane and
cancel older runs for the same workflow, PR, head SHA, and lane. A queued or
in-progress required lane keeps `code-mower/gate` pending until the current-head
audit finishes; the generated gate re-evaluates on local-audit workflow
completion so that pending status can settle after the terminal verdict lands.
Matrix lanes whose `needs-*-audit` label is absent exit without uploading audit
metadata, so optional lanes do not duplicate cached reviewer-run or dogfood
events.

## Budgets and diff limits

`code-mower.yml` can set local audit defaults under `audit`:

```yaml
audit:
  budget_usd: ""
  max_diff_bytes: "180000"
  max_diff_hard_limit_bytes: "1500000"
```

Leave `audit.budget_usd` blank or omit it to use the size-aware default. Claude
audit starts at $2, adds $1 for each 150 KB above `audit.max_diff_bytes`, and
caps the default at $10. Set `audit.budget_usd` only when you want a fixed
provider budget instead of that scaling.

`audit.max_diff_bytes` is the normal target. Complete diffs larger than that
target may still be included when they fit under
`audit.max_diff_hard_limit_bytes`. The generated local-audit workflow passes
these values to both wrappers as:

- `CLAUDE_AUDIT_MAX_BUDGET_USD`
- `CLAUDE_AUDIT_MAX_DIFF_BYTES`
- `CLAUDE_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES`
- `CODEX_AUDIT_MAX_BUDGET_USD`
- `CODEX_AUDIT_MAX_DIFF_BYTES`
- `CODEX_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES`

If Claude audit must truncate a diff at the hard limit, it posts an UNKNOWN
requeue with the truncation reason in the verdict header instead of posting a
blocking finding whose only content is that the diff was truncated. Raise
`audit.max_diff_hard_limit_bytes` for repositories whose normal PRs exceed the
hard limit, then regenerate the local-audit workflow.

`code-mower doctor` reports the effective local audit limits. With
`code-mower doctor --github`, it also samples recent PR diff sizes and warns
when the median sampled diff is above the configured hard limit.

After regenerating `.github/workflows/local-cli-audit.yml`, run `actionlint` on
the generated workflow. If GitHub reports a failed workflow run with no jobs,
treat it as workflow syntax or context validation failure before debugging the
self-hosted runner.
