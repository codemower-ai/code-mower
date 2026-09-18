# v1.5.0 qualification and evidence matrix

This is the immutable evidence contract for release preparation issue #1027,
under #903 / #923. It is not a claim that publication or private acceptance has
already occurred. Append observed results to the release PR/issue and GitHub
Release; do not change qualified source to insert its own SHA or later results.
Historical v1.4.x qualification records remain unchanged.

## Identity and evidence locations

| Identity | Authoritative record |
| --- | --- |
| Release preparation PR and reviewed head | The single PR closing #1027; independent exact-head audit and CI/gate checks |
| Final source | That PR's actual `mergeCommit.oid`, never its earlier head or mutable main |
| Candidate workflow | Successful `Code Mower Immutable Candidate` run on main with `expected_sha` equal to the merge SHA |
| Wheel and sdist | `code-mower-candidate` artifact: `code_mower-1.5.0-py3-none-any.whl` and `code_mower-1.5.0.tar.gz` |
| Digests/inventory | `candidate.json`: source SHA, merged PR, SHA-256 of each artifact, complete inspected member lists and default dependencies |
| Disposable rehearsals | `rehearsal.json`, bound to that source SHA and exact wheel digest |
| Private qualification | Sanitized pass/fail and immutable identifiers on #918 and #920, never raw private observations |
| Publication | #923 owner decision, unchanged `v1.5.0` tag SHA, publication run, canonical index digests and independent reinstall |

## Required observations

| Boundary | Required evidence | Status in this source record |
| --- | --- | --- |
| Source | One Codex writer; #1007, #1024, #1025 and #1031 included; identity/readiness, package guards, privacy, lint, full tests, independent current-head review, normal CI and authoritative gate | Record actual head and check results on the PR |
| Candidate | Build once from a clean merge-SHA checkout; twine and both inventories pass; retain artifacts and digests | Requires merged release PR; pre-merge builds are rehearsals only |
| Default install | Installed-wheel provenance; only base dependencies; init preview without Slack; no Slack network, login or service | Disposable rehearsal script; not live administration |
| Slack opt-in | Installed setup creates mode-0600 hosted manifest and refuses overwrite; default and all-green offline doctor deny readiness | Disposable rehearsal script; no private probe is supplied |
| Upgrade | Published 1.4.2 wheel verified against historical digest, then exact candidate wheel; synthetic config/receipt/reservation bytes preserved | Disposable rehearsal script |
| Disable/removal | Disabled offline observation denies; local manifest removed; package uninstall preserves synthetic state | Offline only; live disable/uninstall belongs to #918 |
| Rollback | Disposable package restores exact digest-verified 1.4.2 and preserves synthetic state | Does not authorize downgrading live v2 claims or schema |
| Private administration | Install/bind/readiness, rotation, disable/uninstall and retained state observed against this candidate | #918, requires owner-controlled private interfaces |
| Two canaries | Exactly one completion and one confirmed cancellation, writer/reviewer exit observed, independent review/gate and uncertainty preserved | #920, only after explicit numeric authorization; not run by the prep PR |
| Publish/reinstall | Same source SHA and same bytes after #918/#920 pass, owner decision, independent canonical PyPI reinstall | #923; not run by the prep PR |

An offline synthetic green observation always exits nonzero and reports
`ready=false`, `dispatch_authorized=false`. A live probe needs the trusted
private host and fresh observations; this package does not invent one. Synthetic
state preservation proves installer behavior, not hosted migration safety.

Follow the [v1.5.0 runbook](v150-release-runbook.md). Keep raw local logs private;
publish only counts, check outcomes, public source/run identities and artifact
digests. A failed or expired candidate is a stop: do not silently rebuild,
relabel a different SHA, replay prior receipts or substitute source modules.
