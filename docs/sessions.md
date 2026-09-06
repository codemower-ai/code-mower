# Participants And Sessions

Choose the tools you want to work with. Claude Code and Codex are the default
pair; Devin and other participants are explicit additions. The agent hosting
your conversation is the default orchestrator for that session.

## Choose During Setup

After installing Code Mower, run this in the repository:

```bash
code-mower init --interactive
```

The terminal shows checkboxes with Claude and Codex selected. Type a number to
toggle a participant, Enter to preview the configuration, or `q` to cancel.
For example, toggle `3` to add Devin. To choose and write the reviewable setup
in one pass, use `code-mower init --interactive --apply`.

Agents and scripts use the same selection model without a terminal:

```bash
code-mower init --with claude,codex,devin
code-mower init --with claude,codex,devin --apply
```

The generated `.code-mower.generated/code-mower.yml` contains the selected
reviewer profile and `session_defaults.participants`. Review and install it
with the generated support files following [the first-audit guide](try-in-10-minutes.md).
Later changes can start from your existing config:

```bash
code-mower init code-mower.yml --interactive --apply
code-mower next-steps --config .code-mower.generated/code-mower.yml
```

Participant selection configures review lanes and remembers session defaults.
Builder automation is enabled separately with the existing `init --builders`
flow after the first review works. Existing lane definitions and their promotion
flags are retained. The selected profile's active reviewer list changes to match
your selection: the preview explicitly lists removed reviewers and flags any
that currently have merge authority. Review those removals before installing the
generated files, since they change which reviewers the generated gate requires.
When editing interactively, known reviewers already active in the profile are
preselected alongside saved participants. Custom lanes absent from the picker
are still reported if the selection would remove them.

## Start From Any Agent

You can give the hosting agent this request:

```text
Start a Code Mower session with Claude, Codex, and Devin on OWNER/REPO.
You are the orchestrator because I am starting the session here. Use
code-mower session start, supply your own identity with --host, read the
resulting operating brief, and check participant readiness before assigning
work. Keep one writer per branch and independent current-head peer reviews.
```

From Codex, the corresponding command is:

```bash
code-mower session start --repo OWNER/REPO --with claude,codex,devin --host codex
```

From Claude, only `--host claude` changes. The same convention works for
`cursor`, `devin`, `grok-bot`, and `antigravity`. The agent supplies its own
identity; the user does not have to choose the orchestrator every time.
Wrappers can set `CODE_MOWER_HOST` instead. A plain shell with no host context
requires an explicit host rather than guessing from installed CLIs.

Omit `--with` to reuse the repository's saved participants, or Claude + Codex
when no selection has been saved. An orchestrator can coordinate participants
without also being selected as a builder or reviewer. `--orchestrator claude`
explicitly requests a handoff to Claude if the session starts in another tool.

The command writes a local brief under `.code-mower/sessions/` and reports its
path. `session show PATH` reads it; `session start ... --dry-run` previews it.
This is an agent-coordinated session: the hosting agent drives work through
its available tools, manual handoffs, or Code Mower's existing dispatcher.
Creating the brief does not launch provider processes, authenticate tools, or
prove they are available. Readiness remains unchecked until the agent verifies
the chosen execution path. Live PR progress remains in `code-mower lanes status`.

## Common Roles, Explicit Product Differences

| Concept | Rule |
| --- | --- |
| Participant | A selected product identity, independent of a particular execution transport. |
| Orchestrator | The hosting agent by default; coordinates assignments, evidence, and recovery. |
| Builder | One writer per branch, using an available execution path. |
| Reviewer | An independent current-head verdict through a supported review lane. |
| Merge authority | Repository policy; selecting or coordinating a tool does not grant it. |

Devin selects the local `devin_cli` reviewer and remains informational under
the starter policy. Devin Cloud needs its own execution setup. Cursor's agent
and Cursor Bugbot are separate selections. Grok Bot retains its own identity;
it is not silently translated into Cursor or Grok Build. Where there is no
dedicated transport, the brief calls for an agent handoff and makes no automatic
execution claim. Optional reviewer services such as Gitar cannot orchestrate
or build.

Session selection uses the same participant definitions as installation. The
reviewer registry and repository config continue to define execution and trust
policy, so a new participant does not require another orchestration algorithm.
