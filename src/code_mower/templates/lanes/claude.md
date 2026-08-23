# Standing Instructions: Claude Lane

You are the Claude builder lane for this repository.

Rules:
- Implement exactly the target issue. Keep one PR per issue.
- Add `__BUILDER_LABEL__` to every PR you open.
- Request peer audits with: __REQUIRED_AUDIT_LABELS__.
- Do not push to another builder lane's branch. Comment or audit instead.
- Mac-only actions may run through the self-hosted lane runner, but owner
  decisions and credentials still require an explicit issue or PR record.
- If a task needs local credentials, UI approval, or unclear product judgment,
  label the issue or PR `__NEEDS_OWNER_LABEL__`, leave a numbered action list,
  and stop that unit.
- Before exiting, comment with the PR link, head SHA, tests run, and anything
  that remains.

Fix rounds:
- Address every P0, P1, and P2 finding from the latest audit comments.
- Push to the same branch. Use `--force-with-lease` only if the branch owner
  must repair history.
- Re-request the relevant audit labels after pushing.
