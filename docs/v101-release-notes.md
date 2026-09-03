# Code Mower v1.0.1 Release Notes

Code Mower v1.0.1 is the first productivity-intelligence patch on top of the
v1.0 supervised autonomous pilot. It keeps the v1.0 privacy boundary and merge
semantics unchanged while making the loop more observable: local productivity
reports, Board summaries, provider scorecards, CodeMower.com productivity
views, and safer Board multi-instance administration.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.1
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.1
code-mower --version
```

## What Is New

- `code-mower productivity report --repo OWNER/REPO` produces a local
  metadata-only effectiveness snapshot from Board history, reviewer-spend rows,
  and optional aggregate productivity events.
- The local Board embeds a compact productivity summary so an operator can see
  active lanes, reviewer activity, spend/latency, and next actions from one
  loopback page.
- CodeMower.com has a protected productivity dashboard that reads the same
  `productivity_summary` contract without requiring source, diffs, transcripts,
  issue body text, raw command output, auth output, local paths, or secrets.
- Provider scorecards summarize reviewer pass/block rates, cost, wall time, and
  promotion readiness inputs without claiming broad provider superiority from a
  single repository's dogfood data.
- `code-mower board list` and `code-mower board stop` make multiple local Board
  listeners easier to inspect and retire after upgrades.
- The docs now include a v1.0.1 effectiveness assessment that separates
  measured Code Mower dogfood evidence from qualitative adoption feedback on
  Bridge Pro, CubeSnap/ctvd, and other candidate agent hosts.

## Recommended Update

For an existing v1.0.0 install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.1
code-mower --version
code-mower board list
```

If a Board from an older install is still running, inspect it and stop the
chosen listener with:

```bash
code-mower board list
code-mower board stop --port PORT --yes
code-mower board serve --repo OWNER/REPO --record-events
```

Use `code-mower migration setup-drift --repo-path . --builders codex,claude,cursor`
to review generated setup drift before copying new support files into an
already-configured repository.

## Release Proof

The release PR records the v1.0.1 release-readiness check, focused and full test
passes, Code Mower dogfood upload, dashboard deploy verification, and public
package-install rehearsal. The release keeps uploads opt-in and metadata-only:
no source, raw diffs, transcripts, issue body text, raw stdout/stderr, auth
output, local secret values, or secrets.
