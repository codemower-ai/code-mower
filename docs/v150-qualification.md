# v1.5.0 qualification contract and evidence matrix

This is the immutable qualification contract for release preparation issue
#1027, under #903 / #923. It defines which observations are required; it is not
itself an evidence record or a claim that publication or private acceptance has
occurred. Record sanitized observed results on #923 and the GitHub Release,
linked to their immutable public identifiers. Do not change qualified source to
insert its own SHA or later results. Historical v1.4.x qualification records
remain unchanged.

## Identity and evidence locations

| Identity | Authoritative record |
| --- | --- |
| Release preparation PR and reviewed head | The final docs-integrity release PR after #1037; it contains the release notes, this contract and publication instructions, with independent exact-head audit and CI/gate checks |
| Final source | That PR's actual `mergeCommit.oid`, never its earlier head or mutable main |
| Candidate workflow | Successful first attempt of `Code Mower Immutable Candidate` on main, with both run `head_sha` and `expected_sha` equal to the merge SHA; reruns are refused |
| Wheel and sdist | `code-mower-candidate` artifact: `code_mower-1.5.0-py3-none-any.whl` and `code_mower-1.5.0.tar.gz` |
| Digests/inventory | `candidate.json`: source SHA, merged PR, SHA-256 of each artifact, complete inspected member lists and default dependencies |
| Disposable rehearsals | `rehearsal.json`, bound to that source SHA and exact wheel digest |
| Bounded canary carry-forward | `code_mower.canary_candidate_equivalence.v1` report on #923, binding the retained canary and final candidates, ancestry, both wheel digests, the complete changed-member list and exact operational closure |
| Private qualification | Sanitized pass/fail and immutable identifiers on #918 and #920, never raw private observations |
| Publication | #923 owner decision and observed evidence, unchanged `v1.5.0` tag SHA, publication and release-event runs, GitHub Release asset digests, canonical index digests and independent reinstall |

## Required observations

| Boundary | Required evidence | Status in this source record |
| --- | --- | --- |
| Source | One Codex writer; #1007, #1024, #1025, #1031, #1037 and release-critical #1043 included, together with the final reviewed closeout source and packaged docs; identity/readiness, package guards, privacy, lint, full tests, independent current-head review, normal CI and authoritative gate | Record actual head and check results on the final release PR |
| Candidate | Build once from a clean merge-SHA checkout; twine and both inventories pass; retain artifacts and digests | Requires merged release PR; pre-merge builds are rehearsals only |
| Default install | Installed-wheel provenance; only base dependencies; init preview without Slack; no Slack network, login or service | Disposable rehearsal script; not live administration |
| Slack opt-in | Installed setup creates mode-0600 hosted manifest and refuses overwrite; default and all-green offline doctor deny readiness | Disposable rehearsal script; no private probe is supplied |
| Graphify compatibility | Installed wheel accepts/excludes `doc_ref`, reports reader/search available, preserves ambiguity-only partial usability with complete generation, and returns bounded `reader_incompatible` for a same-version wrong-distribution unknown type without content/type/path leakage | Three named checks in `rehearsal.json`, required by publication; synthetic public fixtures only |
| Upgrade | Published 1.4.2 wheel verified against historical digest, then exact candidate wheel; synthetic config/receipt/reservation bytes preserved | Disposable rehearsal script |
| Disable/removal | Disabled offline observation denies; local manifest removed; package uninstall preserves synthetic state | Offline only; live disable/uninstall belongs to #918 |
| Rollback | Disposable package restores exact digest-verified 1.4.2 and preserves synthetic state | Does not authorize downgrading live v2 claims or schema |
| Private administration | Install/bind/readiness, rotation, disable/uninstall and retained state observed against this candidate | #918, requires owner-controlled private interfaces |
| Two accepted canary outcomes | One accepted completion and one accepted confirmed cancellation, writer/reviewer exit observed, independent review/gate and uncertainty preserved; every failed, retired, replacement or recovery attempt and reservation remains count-preserved. Outcomes use the final candidate by default. The observed v1.5.0 closeout may use only the runbook's machine-verified closed-member carry-forward from an ancestor candidate, with exact final-candidate #918 acceptance and audit-receipt replay; any operational member difference requires newly authorized canaries | #920, only after explicit numeric authorization; the prep PR does not run them and no retry may be silent |
| Publish/reinstall | Final source SHA and final candidate bytes after #918 and #920 pass directly or through the bounded canary carry-forward, owner decision, manual publication, verified non-publishing release-event run, exact GitHub Release assets, independent canonical PyPI reinstall and temporary variable cleanup | Record observed results on #923 and the GitHub Release; not run by the prep PR |

An offline synthetic green observation always exits nonzero and reports
`ready=false`, `dispatch_authorized=false`. A live probe needs the trusted
private host and fresh observations; this package does not invent one. Synthetic
state preservation proves installer behavior, not hosted migration safety.

Follow the [v1.5.0 runbook](v150-release-runbook.md). Keep raw local logs private;
publish only counts, check outcomes, public source/run identities and artifact
digests. The GitHub Release and #923 are the evidence records for what was
actually observed; this packaged contract remains unchanged. A failed or expired
candidate is a stop: do not silently rebuild, relabel a different SHA, replay
prior receipts or substitute source modules. The bounded carry-forward compares
retained immutable candidates and replays only their audit receipts; it never
replays or invents a provider outcome.
