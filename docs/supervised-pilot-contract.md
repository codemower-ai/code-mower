# Supervised Pilot Contract

The Code Mower v1.0 supervised pilot extends the Board visibility surface to show what the supervised controller is doing, why it is waiting, and what action is next.

## Overview

The supervised controller manages the full build loop cycle: dispatch, implementation, review, and merge. The Board must surface enough state for an operator to answer:

- **What is active?** Which issues, PRs, and lanes are currently working.
- **What is waiting?** Which items are blocked on reviews, checks, or owner decisions.
- **What is blocked?** Which items have failed audits or checks.
- **What should happen next?** The concise operator action for each item.

## Metadata-Only Contract

All supervised pilot state is **metadata-only**. The Board must not display:

- Source code
- Raw diffs
- Transcripts
- Issue body text
- Raw stdout/stderr
- Auth output
- Local paths (unless `--show-local-paths` is explicitly set)
- Secrets

## Schema Extension

The supervised pilot state is embedded in the existing `code_mower.board.v1` payload under a new `supervised` key:

```json
{
  "schema": "code_mower.board.v1",
  "supervised": {
    "schema": "code_mower.supervisedPilot.v1",
    "enabled": true,
    "cycle_state": "active",
    "active_issues": [...],
    "active_prs": [...],
    "lanes": {...},
    "next_action": "..."
  }
}
```

### `code_mower.supervisedPilot.v1`

Top-level fields:

- `schema`: always `code_mower.supervisedPilot.v1`
- `enabled`: whether supervised pilot mode is active
- `cycle_state`: current controller state: `idle`, `active`, `waiting`, `blocked`, or `error`
- `active_issues`: list of issues currently being worked
- `active_prs`: list of PRs in review or merge flow
- `lanes`: status for each builder and reviewer lane
- `next_action`: concise operator action
- `last_cycle_at`: UTC timestamp of last controller cycle

### Active Issue Fields

Each `active_issues[]` entry includes:

- `number`: issue number
- `title`: issue title
- `url`: HTTP(S) issue URL
- `state`: `dispatched`, `building`, `reviewing`, `blocked`, or `needs_owner`
- `builder_lane`: assigned builder lane
- `builder_branch`: PR branch if created
- `pr_number`: PR number if created
- `next_action`: concise next step for this issue
- `dispatched_at`: UTC timestamp when issue was dispatched
- `updated_at`: UTC timestamp of last state change

### Active PR Fields

Each `active_prs[]` entry includes:

- `number`: PR number
- `title`: PR title
- `url`: HTTP(S) PR URL
- `branch`: branch name
- `head_sha`: current head SHA
- `head_sha_prefix`: first 12 characters of head SHA
- `author`: PR author login
- `builder_lane`: builder lane that created this PR
- `review_state`: `pending`, `in_review`, `approved`, `blocked`, or `ready`
- `gate_state`: `pending`, `running`, `passed`, `failed`, or `stale`
- `merge_state`: merge state from GitHub
- `is_draft`: whether PR is draft
- `needs_owner`: whether PR needs owner decision
- `next_action`: concise next step for this PR
- `labels`: grouped label names (builder, needs, done, blocked)
- `updated_at`: UTC timestamp

### Lane Status Fields

The `lanes` object groups builder and reviewer lanes:

```json
{
  "builders": {
    "cursor": {
      "wip_count": 1,
      "wip_cap": 2,
      "available": true,
      "active_prs": [123]
    }
  },
  "reviewers": {
    "claude": {
      "queue_depth": 2,
      "active_prs": [123, 456]
    }
  }
}
```

Each builder lane includes:

- `wip_count`: current work-in-progress count
- `wip_cap`: maximum WIP allowed
- `available`: whether lane is ready to accept work
- `active_prs`: list of PR numbers this lane is building

Each reviewer lane includes:

- `queue_depth`: number of PRs waiting for this reviewer
- `active_prs`: list of PR numbers this reviewer is auditing

## Controller Cycle States

### `idle`

No active issues or PRs. Ready to dispatch new work.

**Next action:** "waiting for ready issues"

### `active`

Controller is actively dispatching, building, or reviewing.

**Next action:** Derived from most urgent item state.

### `waiting`

All active items are waiting for external events (checks, owner decisions).

**Next action:** "waiting for checks" or "waiting for owner decisions"

### `blocked`

One or more items have failed audits or checks that need attention.

**Next action:** "fix BLOCKED audit on PR #123" (most urgent)

### `error`

Controller encountered an error and cannot proceed.

**Next action:** "inspect controller error; see logs"

## Issue States

- `dispatched`: Issue handed to builder lane but no PR yet
- `building`: Builder lane is actively implementing
- `reviewing`: PR created and in review
- `blocked`: PR has blocked audit or failed check
- `needs_owner`: PR needs human decision

## PR Review States

- `pending`: PR created, waiting for first reviewer
- `in_review`: At least one reviewer is active
- `approved`: All required reviewers passed
- `blocked`: At least one reviewer blocked
- `ready`: Approved and checks passed, ready to merge

## PR Gate States

- `pending`: Gate check not started
- `running`: Gate check in progress
- `passed`: Gate check succeeded
- `failed`: Gate check failed
- `stale`: Gate evidence older than configured threshold

## Local-Only Default

Supervised pilot state is **local-only** by default. It is:

- Collected from GitHub PR/issue/workflow metadata
- Optionally augmented by local process inspection
- Never uploaded unless explicitly requested
- Included in Board `/api/status` endpoint
- Redacted in board event snapshots like other Board state

## Degradation

The Board degrades gracefully when:

- **GitHub unavailable:** Shows `enabled: false` with message "GitHub required for supervised pilot state"
- **No config:** Shows `enabled: false` when no supervised pilot config exists
- **Local processes unavailable:** Shows GitHub-only state without local process hints

## Privacy Boundary

Supervised pilot state follows the same privacy boundary as existing Board contracts:

- Metadata-only by default
- No source, diffs, transcripts, issue bodies, or secrets
- Local paths redacted unless `--show-local-paths`
- URLs only when HTTP(S)
- SHA prefixes truncated to 12 characters

## Testing

Tests must use **mocked inputs** and work when no local Board adapters are present:

```python
def test_supervised_pilot_shows_active_issue_and_pr():
    def gh_json(args):
        if args[:2] == ["issue", "list"]:
            return [{"number": 573, "title": "Board visibility", ...}]
        if args[:2] == ["pr", "list"]:
            return [{"number": 600, "title": "Implement #573", ...}]
        raise LaneStatusUnavailable("mock unavailable")
    
    payload = board.status_payload(config, gh_json_runner=gh_json, ...)
    supervised = payload.get("supervised", {})
    
    assert supervised["schema"] == "code_mower.supervisedPilot.v1"
    assert supervised["cycle_state"] == "active"
    assert len(supervised["active_issues"]) == 1
    assert len(supervised["active_prs"]) == 1
```

## Implementation Guidance

1. Add `supervised_pilot_payload()` function in `board.py`
2. Call from `status_payload()` and embed under `"supervised"` key
3. Add `render_supervised_pilot_section()` for HTML display
4. Update board HTML template to show supervised section
5. Add tests with mocked GitHub responses
6. Keep all inputs mockable; no direct GitHub API calls from tests

## Open Questions

- Should we show builder transcript summaries? **No** - keep metadata-only
- Should we auto-refresh faster in active mode? **Maybe** - follow-up work
- Should we show merge history? **No** - out of scope for first slice
