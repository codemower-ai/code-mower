# Planning And Work Orders

Code Mower's reviewer lanes are useful only after a change exists. The planning
surface gives teams a lightweight way to turn product context, external docs,
and GitHub issues into implementation contracts before a builder starts.

The design is intentionally local-first:

- project doctrine lives in editable repo-local Markdown;
- external documents are recorded as metadata-only manifests by default;
- work orders are plain Markdown contracts;
- critique prompts are generated locally for whichever agents you want to ask;
- builder experiments can be seeded from the same work order without exposing
  source, raw diffs, raw transcripts, or private docs to CodeMower.com.

## 1. Establish Project Context

Create starter doctrine files:

```bash
code-mower project-context init --project-name "My Product"
```

The same command is available as an init alias:

```bash
code-mower init project-context --project-name "My Product"
```

By default this writes:

- `.code-mower/project-context/architecture.md`
- `.code-mower/project-context/hosting-environment.md`
- `.code-mower/project-context/ci-cd.md`
- `.code-mower/project-context/design-system.md`
- `.code-mower/project-context/quality-bar.md`
- `.code-mower/project-context/agent-team.md`
- `.code-mower/project-context/work-spec-template.md`
- `.code-mower/project-context/project-context-manifest.json`

Treat these as living repo doctrine. Put them in source control only when their
contents are safe for your repo. They are planning input, not cloud-bound data.

## 2. Add External Context

Record external docs without copying them into a cloud payload:

```bash
code-mower context add \
  --external ~/Downloads/product-requirements.md \
  --external ~/Downloads/testing-notes.md
```

This writes `.code-mower/context/external/external-context-manifest.json` with
path, byte size, checksum, and file metadata. Raw files stay where they are.

If you want a bounded local preview for text files, opt in explicitly:

```bash
code-mower context add \
  --external ~/Downloads/product-requirements.md \
  --include-preview
```

Previews are still local artifacts. Do not upload them unless you explicitly
intend to share their contents.

## 3. Create An Issue Plan

GitHub Issues are a better home than pull requests for architecture notes,
product requirements, and implementation specs. A PR should remain the coding
artifact.

Draft an issue-derived plan from copied issue text:

```bash
code-mower plan from-issue \
  --repo owner/repo \
  --issue-url https://github.com/owner/repo/issues/123 \
  --title "Add billing settings" \
  --body-file issue-body.md \
  --output .code-mower/work-orders/billing-settings-plan.md
```

The output is a small Markdown planning artifact with problem, context,
non-goals, acceptance criteria, and review protocol sections.

## 4. Draft A Work Order

Turn the issue plan into an implementation contract:

```bash
code-mower work-order draft \
  --issue-plan .code-mower/work-orders/billing-settings-plan.md \
  --repo owner/repo \
  --context-manifest .code-mower/context/external/external-context-manifest.json \
  --output .code-mower/work-orders/billing-settings.md
```

The work order includes role/lens sections for product, architecture,
implementation, QA, security, operability, and devil's advocate review. These
sections are deliberately prompts for thinking, not requirements to spawn a
heavy multi-agent runtime.

## 5. Generate Critique Prompts

Ask multiple agents to improve the plan before implementation:

```bash
code-mower work-order critique-plan \
  .code-mower/work-orders/billing-settings.md \
  --reviewer codex \
  --reviewer claude \
  --reviewer gemini
```

This writes one prompt per reviewer under
`.code-mower/work-orders/critique-prompts/`. The prompt asks for plan
improvements, blockers, and questions, not code.

## 6. Seed A Builder Experiment

When you want to measure authoring loops, seed a builder experiment from the
same work order:

```bash
code-mower work-order builder-experiment \
  .code-mower/work-orders/billing-settings.md \
  --repo owner/repo \
  --builder codex-desktop \
  --builder claude-code \
  --context-pack project-context \
  --prompt-lens context-driven-quality \
  --output .code-mower/work-orders/billing-settings-builder-experiment.json
```

Then plan or report with the existing builder-experiment surface:

```bash
code-mower builder-experiment plan \
  .code-mower/work-orders/billing-settings-builder-experiment.json \
  --json
```

## What This Does Not Do Yet

This is planning and measurement scaffolding, not a full autonomous builder
orchestrator. It does not:

- fetch GitHub issue bodies automatically;
- execute agent sessions;
- upload external docs;
- decide merge readiness;
- replace the normal Code Mower audit protocol.

That boundary is deliberate. The v1.0 path is to make planning artifacts useful
and measurable first, then add optional provider adapters where the value is
clear.
