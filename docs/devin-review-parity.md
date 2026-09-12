# Informational Devin review adapters

`code_mower.devin_review` supplies an embedding API for one exact-head review
through `devin_cli` or `devin_api_v3`. It reuses the Devin CLI finding/verdict
validator and the packaged audit JSON schema. Hosted execution uses `DevinClient`,
`DevinProvider`, and `RemoteSessions` v1, including private storage, create
checkpoints, reconciliation, and explicit apply. There is no new scheduler,
GitHub writer, reviewer promotion, or merge authority. Default installation
remains Claude + Codex. The existing standalone CLI workflow is unchanged.

The trusted orchestrator constructs `ReviewInput` from authenticated repository,
PR, head, author, changed-file and context metadata. Its `current` callback must
re-fetch those inputs from trusted control authorities, using the existing context
input discovery and authorization/delivery path. Never populate this callback
from provider assertions. Supply a current explicit context revision, including
`optional_unavailable` when context is optional and absent. Missing, expired,
required-unavailable or superseded context cannot be accepted. This API checks
revision metadata; approved packet delivery remains the orchestrator's existing
`context_audit`/`context_delivery` responsibility.

For local execution, call `local_review(input, current, run)`. The `run` callback
must use the existing Devin CLI bounded process and disposable exact-head checkout
contract, including checkout verification before and after the process; return
`(stdout, returncode)`. The adapter requires a single JSON object (no fences,
prose, duplicate keys or ambiguous candidates), validates findings against the
changed files, and rechecks current input after completion. It does not launch
an arbitrary CLI or read credentials itself.

For hosted execution, construct `HostedReview.devin(private_root, client, input,
current)`, then call `dispatch(approved_prompt, limit=1, apply=True)` and
`collect(apply=True)` when complete. Both operations preview by default. The
prompt includes the trusted input binding and requests read-only review. Dispatch
retains the v1 64 KiB prompt ceiling and explicit ACU limit. Use existing session
status/reconciliation/cancel operations with the adapter's `session` identifier;
never replay uncertain delivery with a fresh identifier. A review session is
immutable: a message or cancellation disqualifies its evidence even if the
provider later reports completion. Start a fresh context revision for a new
review after resolving the old session. The hosted service must
be configured with read-only repository/tool permissions: a prompt is not an
access-control boundary.

`accept()` returns private evidence, and `evidence.accept(current)` revalidates
before each consumer uses the common `DevinCliVerdict` (PASS/BLOCKED, summary,
findings and P0–P3 counts). Hosted evidence also refreshes remote status on each
acceptance. A pending mutation or non-complete session cannot expose collected
results. All failures are informational failures requiring a fresh review;
none grants implicit merge authority. Do not treat a cached verdict as current.

Input objects, findings, summaries, prompts, results, and exception causes belong
only in protected local processing. Dispatch/collect return the existing closed
remote metadata projection; only that projection is suitable for Board/cloud.
Never serialize the evidence object or raw result into public metadata.

## Calibration and live acceptance

`tests/test_devin_review.py` adjudicates four synthetic controls identically for
both transports: clean empty review, clean P3 advisory, blocked P1 authorization
omission, and blocked P2 null dereference (including a false PASS declaration).
It also checks stale inputs before and after completion, later consumption,
missing/ambiguous/malformed results, uncertain delivery, state invalidation,
privacy, and preview semantics. These offline controls establish contract parity,
not model accuracy and not evidence for promotion.

Devin CLI was found installed during implementation; authentication was not
inspected and no live provider was invoked. The orchestrator can first confirm
existing local authentication without printing credentials. If authorized and
bounded, run the same two clean and two blocked fixtures in disposable exact-head
checkouts through the existing CLI wrapper with a short timeout, existing byte
limits, and no GitHub posting. CLI `--dry-run` suppresses posting but can still
invoke a paid provider, so do not mistake it for a cost-free preview. Record
adjudication and usage privately. Hosted live dispatch requires separate approval
of repository access, context delivery and spending (suggested initial cap: one
session, 1 ACU); no live hosted acceptance was performed here. Promotion still
requires a separate evidence-backed policy decision.
