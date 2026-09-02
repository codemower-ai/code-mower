# Board Demo Rehearsal

This public demo shows the native Code Mower Board shape without connecting to a
private repository or running provider CLIs. It is synthetic, small, and safe to
use in an adoption walkthrough.

## Files

- `board-events.jsonl` contains two synthetic `code_mower.boardEvent.v1`
  snapshots.
- `reviewer-spend.json` contains matching synthetic
  `code_mower.reviewerSpend.v1` rows.

Both files are metadata-only fixtures for demonstrating Board state.

## Try The Board Shape Locally

From the repository root:

```bash
code-mower board events --store-path examples/board-demo/board-events.jsonl
```

To open the local browser Board with the same sample history and spend data:

```bash
code-mower board serve \
  --repo example/widget-service \
  --store-path examples/board-demo/board-events.jsonl \
  --spend-path examples/board-demo/reviewer-spend.json
```

The live GitHub panel may say the demo repository is unavailable. That is fine:
the Recent Local History, Reviewer Verdict Timeline, and Spend And Latency
panels still show the sample local artifacts.

To inspect the Board diagnostic shape without exposing local paths:

```bash
code-mower board doctor \
  --repo example/widget-service \
  --store-path examples/board-demo/board-events.jsonl \
  --spend-path examples/board-demo/reviewer-spend.json \
  --json
```

## What To Look For

- one ready PR lane with a passing Claude audit;
- one blocked PR lane with a failing Codex audit;
- a gate alert and owner queue item for the blocked lane;
- reviewer spend and latency totals by lane; and
- redacted local path posture in command output.

## Privacy Boundary

This example contains no source code, raw diffs, transcripts, issue body text,
raw stdout/stderr, auth output, browser history, local paths, local secret
values, or secrets.
