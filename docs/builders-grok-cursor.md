# Grok Bot And Cursor Cloud Agents

Code Mower treats hosted coding agents as **builders**, not reviewer lanes,
until they produce an actual pull request. Reviewer lanes still run after the
PR exists.

This distinction matters:

- `grok_build` is the local Grok Build CLI reviewer lane.
- `grok_bot` is a hosted/manual builder or orchestrator identity.
- `cursor_cloud_agent` is a hosted async builder/executor identity.
- `cursor_bugbot` is Cursor's reviewer service and remains informational/manual.

## Recommended v0.5 Flow

Use GitHub Issues as the source of truth:

```bash
code-mower plan from-github-issue OWNER/REPO#123 \
  --output .code-mower/work-orders/example-plan.md \
  --post

code-mower work-order draft \
  --issue-plan .code-mower/work-orders/example-plan.md \
  --repo OWNER/REPO \
  --output .code-mower/work-orders/example.md
```

Give the work order to Grok Bot, Cursor Cloud Agents, or another hosted
builder. After that builder opens a PR, record source-free provenance:

```bash
code-mower builder record \
  --provider grok_bot \
  --executor cursor_cloud_agent \
  --work-order .code-mower/work-orders/example.md \
  --pr OWNER/REPO#124 \
  --branch cursor/example \
  --model grok-4 \
  --output .code-mower/builder-runs/example.cloud-event.json
```

Then attach delivery/reviewer/merge metadata to the work-order event as normal:

```bash
code-mower work-order attach-delivery \
  .code-mower/work-orders/example.cloud-event.json \
  --pr OWNER/REPO#124 \
  --from-github
```

Finally export both sidecars:

```bash
code-mower cloud export \
  --event work_order=.code-mower/work-orders/example.cloud-event.json \
  --event builder_run=.code-mower/builder-runs/example.cloud-event.json \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --repo-slug OWNER/REPO
```

This gives CodeMower.com enough metadata to show:

```text
issue -> posted plan -> work order -> builder run -> PR -> reviewer checks -> merge
```

## Privacy Boundary

`builder record` stores metadata only. It does not store:

- source code;
- issue body text;
- raw diffs;
- prompts or model transcripts;
- stdout/stderr;
- auth output; or
- secrets.

Use safe identifiers and hosted run URLs only when you are comfortable sharing
them with your CodeMower.com team. Do not put credentials, raw prompts, raw
agent logs, or browser-session data into model/version/run fields.

## Promotion Policy

Builder evidence is authoring evidence. It can help answer questions like:

- Which builder opened the PR?
- Which executor actually made the branch?
- How long did the run take?
- How much did it cost?
- How many human interventions were needed?
- Which reviewer lanes caught issues afterward?
- Was the PR merged?

It is not a merge approval. Codex, Claude, Gitar, Grok Build, Cursor BugBot, or
other reviewer lanes still need their own head-bound review evidence under the
normal Code Mower audit protocol.
