# Code Mower v1.2.0 Release Notes

Code Mower v1.2.0 makes supervised multi-agent orchestration safer and easier
to inspect. It gives Codex, Claude, and Cursor the same Jira authority contract,
prevents two orchestrators from mutating one working copy at the same time,
and makes hosted release qualification reproduce the validated local command
instead of improvising a result.

Install or upgrade the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.2.0
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.2.0
code-mower --version
```

## One Orchestrator Per Working Copy

`code-mower session start` now takes a local, expiring orchestrator lease. A
second mutating session is refused while the first lease is live; expired or
unreadable leases recover automatically. Read-only brief generation does not
take a lease, and every force release or takeover is explicit. Board shows the
active holder and expiry so operators can resolve ownership without guessing.

## Shared Jira Authority

Session briefs now give every orchestrator host the same Jira rules. Code
Mower's bounded REST transport is authoritative for queue reads and all
mutations. Atlassian Rovo MCP and IDE integrations remain optional read/context
enrichment. Jira writes still require the guarded `tracker mutate` or
`tracker pr-sync` surface, configured write authority, and explicit apply.

Cursor is qualified against this same brief, lease, and controller telemetry
contract used by Codex and Claude. Other recognized hosts remain explicit
handoffs until separately qualified.

## Observable Coordination And Qualification

Controller events include the normalized orchestrator provider in local and
metadata-only cloud evidence. Hosted release-campaign dispatches now include
the exact shell-quoted `code-mower release qualify` command for the bound tag,
package source, context, provider, and executor. Providers must embed the
generated adoption result unchanged, preserving campaign identity and strict
validation.

## Verification And Privacy

The release passed the full unit suite, Ruff, compile checks, generated workflow
validation, privacy scanning, release readiness, source-package rehearsal, and
clean package installation. Python 3.12, 3.13, and 3.14 remain supported.

The privacy boundary is unchanged. Source, Jira summaries and descriptions,
issue bodies, comments, attachments, raw diffs, prompts, transcripts, raw
provider output, authentication output, local paths, and secrets are excluded
from Board and cloud uploads. GitHub remains authoritative for pull requests,
checks, and merge gates.
