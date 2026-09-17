# Code Mower Build Loop

Builder lanes get work from GitHub Issues. The dispatcher turns ready issues
into lane work, and each lane follows the standing instructions in this
directory. No human should paste a custom prompt unless the issue itself needs
new acceptance criteria.

| Lane | Standing instructions | Builder label | Dispatch label | Trigger |
| --- | --- | --- | --- | --- |
__BUILDER_LANE_ROWS__

Dispatcher rules:
- Ready means open, labeled `__BUILD_LOOP_READY_LABEL__`, labeled for exactly one
  builder lane, not assigned, not carrying `blocked-by:*`, and not carrying any
  owner-blocking label: __OWNER_BLOCKING_LABELS__.
- Ready issues from untrusted authors also need a trusted work-order comment.
  The first content line must start with `# Work Order:`, `## Work Order:`, or
  `Work order:`.
- Dependencies listed under an issue `## Dependencies` section must be closed
  before dispatch.
- A lane stops dispatching when its open PRs plus active dispatched issues reach
  the WIP cap, default `__BUILD_LOOP_MAX_WIP__`.
- Dispatch adds `dispatched:<lane>` before posting the comment. If no PR appears,
  the dispatcher may expire that label after one day and try again.
- A dispatched issue that already has an open PR referencing it is skipped.
- The branch owner is the single writer for that PR. Other lanes audit, comment,
  or request a fix round instead of pushing to the branch.
- Mac runner checkouts install a pre-push guard that rejects pushes outside the
  lane's branch prefixes or the exact targeted PR branch.
- Owner-sitting work uses `__NEEDS_OWNER_LABEL__` with a numbered action list.
  Builder lanes stop that unit and move on.

Operational checks:
- `__DISPATCH_TOKEN_ENV__` should be a human-owned fine-grained PAT or delegated machine
  user token with Issues read/write and Pull requests read/write.
- `__DISPATCH_TOKEN_EXPIRES_VAR__` should hold the token expiry date as
  `YYYY-MM-DD`, or `never` for a non-expiring token.
- Set `CODE_MOWER_MAX_WIP` to override the default WIP cap.
- Set `__LANE_MAC_RUNNER_ENABLED_VAR__=true` only after the self-hosted Mac runner has
  the configured labels and the lane CLIs are authenticated for the runner user.
