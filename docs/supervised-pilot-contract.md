# Supervised Pilot Contract

Code Mower v1.0 is a supervised autonomous pilot. The operator promise is that
Code Mower can keep a repository moving by selecting ready work, dispatching
builder and reviewer lanes, watching the gate, requesting auto-merge only when
policy evidence is complete, and stopping with a clear owner action when it is
not.

The goal is not unattended arbitrary autonomy. v1.0 should make the happy path
faster while keeping every merge decision observable, reproducible from
metadata, and reversible by the repository owner.

## Automation Boundary

Code Mower may automate:

- queue inspection and next-action selection;
- builder/reviewer dispatch when the issue, labels, and lane config allow it;
- stale evidence detection and safe requeue recommendations;
- merge-readiness decisions from promoted reviewer lanes and `code-mower/gate`;
- GitHub auto-merge enablement for a PR that is already green; and
- Board and CodeMower.com metadata events for operator visibility.

Code Mower must stop or escalate when:

- the author lane would be asked to gate its own work;
- reviewer evidence is missing, blocked, stale, or from an untrusted identity;
- the required `code-mower/gate` status is missing, failing, stale, or not
  required by branch protection in promoted mode;
- branch protection, auto-merge, or a merge-capable credential is missing;
- a PR is behind, conflicted, draft, oversized by policy, or owner-labeled;
- a release step has not completed its serialized checklist; or
- two active writers would mutate the same branch or contract surface.

## Event Types

All supervised-pilot events use the existing cloud event envelope
`code_mower.benchmarkEvent.v1`. They are additive to v0.9.x uploads and remain
metadata-only.

`controller_decision` records one controller cycle: selected issue or PR,
decision state, next action, stop condition when present, mode, lane, and coarse
counts. It never stores issue body text, prompts, source, diffs, transcripts, or
raw command output.

`merge_decision` records whether a PR was eligible for manual merge, auto-merge
request, or no merge. It may reference reviewer lanes, verdicts, check names,
branch name, PR number, and short SHA prefixes, but not diff contents or review
comment text.

`queue_state_snapshot` records aggregate queue posture: active lanes, blocked
lanes, ready PR count, owner-action count, release-stop state, and current
parallelism. It is a snapshot, not a work log.

`owner_intervention` records why the controller needs the repository owner:
missing GitHub settings, missing credentials, conflicting reviewer decisions,
release approval, lane promotion, or explicit override. It may include safe
setup URLs and option labels, but not secrets, auth output, or private issue
text.

Reviewer outcomes referenced by supervised-pilot events should point to
metadata that already exists in `reviewer_run`, `board_snapshot`, or GitHub
check/status metadata: lane id, verdict, check name, PR number, URL, short SHA
prefix, status, and whether the lane is promoted. They should not duplicate raw
audit comments.

## Required Metadata

Every supervised-pilot event should include these dimensions:

- `dimensions.supervised_pilot_schema`: `code_mower.supervisedPilot.v1`;
- `dimensions.controller_mode`: `dry_run`, `no_merge`, `manual`, or
  `promoted`;
- `dimensions.decision_state`: a stable state such as `dispatch_builder`,
  `blocked_audit`, `ready_to_merge`, `owner_action`, or `release_stop`;
- `dimensions.next_action`: concise operator-facing next action;
- `dimensions.repo_slug`: same value as top-level `repo_slug` when known; and
- `tool.role`: `controller`, `reporter`, `builder`, or `reviewer`.

Optional dimensions include PR number, issue number, branch, author login, lane
id, reviewer outcome references, gate status, stop condition, safe run URL, and
short SHA prefix.

## Metrics

Metrics are coarse numbers only. Common fields include:

- `active_lane_count`;
- `ready_pr_count`;
- `blocked_pr_count`;
- `owner_action_count`;
- `reviewer_pass_count`;
- `reviewer_blocked_count`;
- `stale_evidence_count`;
- `wall_seconds`; and
- `cost_usd`, `input_tokens`, `output_tokens`, or `total_tokens` when a
  provider exposes them safely.

Missing provider metrics mean unknown, not zero.

## Privacy Boundary

Supervised-pilot events must not include source code, raw diffs, transcripts,
issue body text, raw stdout/stderr, auth output, local paths by default, or
secrets. They must also avoid raw review comment bodies and raw prompt text.
CodeMower.com should continue accepting v0.9.x uploads that omit all
supervised-pilot event types.

Example fixtures live in
`tests/fixtures/supervised_pilot_events.json`.
