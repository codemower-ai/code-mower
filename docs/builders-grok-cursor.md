# Cursor Cloud Agent And Grok Bot Separation

Code Mower distinguishes between Cursor's hosted builder and review capabilities:

- `cursor_cloud_agent` is the hosted async builder/executor identity that can take work orders and execute package-install qualification campaigns.
- `cursor_bugbot` (also known as Grok Bot or BugBot) is Cursor's review service and remains informational/manual; it cannot execute work orders.
- `grok_build` is the local Grok Build CLI reviewer lane.

This distinction matters:

- `cursor_cloud_agent` has `role: builder` and `capability: work_order_execution`, qualifying it for release campaigns.
- `cursor_bugbot` has `role: reviewer` and `capability: code_review`, excluding it from builder tasks.

## Provider Aliases and Compatibility

The provider alias map routes spellings to canonical identities:

- `cursor` → `cursor_cloud_agent` (builder)
- `cursor_cloud_agent` → `cursor_cloud_agent` (builder)
- `cursor_bugbot` → `cursor_bugbot` (reviewer)
- `cursor_grok_bot` → `cursor_bugbot` (reviewer)
- `grok_bot` → `cursor_bugbot` (reviewer)

Historical `cursor_bugbot` campaigns created before v1.0.8 remain valid but are not accepted for new release qualification work. The separation preserves existing stored evidence through an explicit compatibility path without silently reinterpreting old data.

## Recommended Current Flow

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

Give the work order to Cursor Cloud Agent. After it opens a PR, record source-free provenance:

```bash
code-mower builder record \
  --provider cursor_cloud_agent \
  --executor cursor_cloud_agent \
  --work-order .code-mower/work-orders/example.md \
  --pr OWNER/REPO#124 \
  --branch cursor/example \
  --model grok-4 \
  --output .code-mower/builder-runs/example.cloud-event.json
```

For hosted PRs with recognizable metadata, `auto-record` can write the same
sidecar from a GitHub `pull_request` event payload or `gh pr view --json`
output. It recognizes safe markers such as Cursor agent links, the
`chatgpt-codex-connector` author, `claude[bot]`, and `cursor/`, `codex/`, or
`claude/` branch prefixes, but it does not store the PR body text:

```bash
code-mower builder auto-record \
  --pr-json "$GITHUB_EVENT_PATH" \
  --repo "$GITHUB_REPOSITORY" \
  --output .code-mower/builder-runs/pr-124.cloud-event.json \
  --force
```

The bundled `templates/workflows/builder-provenance.yml.j2` workflow runs this
on pull requests and uploads the generated sidecar as a workflow artifact.

Treat each PR branch as single-writer. The owning `builder:<lane>` identity is
the only lane that should push commits to that branch; other builders and audit
lanes should leave PR comments or open follow-up work instead. If the owning
builder must rewrite history, use `git push --force-with-lease` so another
lane's newer commits cannot be silently dropped.

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
