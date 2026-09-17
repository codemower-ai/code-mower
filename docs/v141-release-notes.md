# Code Mower v1.4.1 Release Notes

> **v1.4.1 is a completed release.** This page is preserved as the
> source-candidate note it was when it was written for release #915, which has
> since closed. The prepublication procedure and boundaries below are kept
> verbatim as the historical record; they are not current instructions. For the
> current release see the [v1.4.2 release notes](v142-release-notes.md) and the
> [v1.4.2 qualification record](v142-qualification.md), and for what v1.4.1
> actually proved see the
> [v1.4.1 qualification record](v141-qualification.md) and the
> [v1.4.1 GitHub release](https://github.com/codemower-ai/code-mower/releases/tag/v1.4.1).

Status as written: source candidate prepared for #915. At that time this
document was not a publication, installed-package qualification, comparative
scorecard or freshness claim. v1.4.0 tags, assets, release notes and its
historical runbook remain immutable.

## Optional local Graphify context

The accepted Graphify 0.9.58 integration provides revision-bound, contained
code-only/no-cluster extraction, bounded queries and guided context delivery.
`init --graphify` renders opt-in separate-environment acquisition and exact-pin
guidance. It installs, invokes and indexes nothing, adds no base dependency,
and does not enable context. Default Claude + Codex adoption stays quiet.
See [setup](graphify-setup.md) and the [evaluation](graphify-evaluation.md).
The release-specific #876 comparative scorecard remains a qualification gate.

## Stabilization included in the source candidate

The accepted main baseline includes #966 (quiet campaign readiness), #969
(bare-builder init), #971 (current guidance), #973 (session discovery), #980/#981
(default lanes and role/lease documentation), #984 (campaign identity), #985
(operational evidence), #987 (Board diagnostics) and #988 (Devin setup remediation).
The #963 lineage replacement stages #990/#991/#992, culminating in accepted
#997, replace the unaccepted #989 draft. Graphify #914 is accepted through #982.

This candidate clarifies packaged-starter discovery versus installed repository
verification, preserving the selected config/profile in text and JSON. Eligible
mutating session starts acquire a lease with a maintained 12-hour default.
`session show --current` and `session lease show` discover its holder without
mutation. Renew or release from another process using the same session ID after
quiescing writers; read-only `--dry-run` and `--no-lease` alternatives remain.
Devin orchestration remains unqualified and acquires no mutating lease.

## Remaining headless limitations

Repository-aware `board stop --repo` is deferred to #961 for v1.4.2. Inspect
`board status --all`, select the intended running instance with `board stop
--port PORT`, wait for shutdown and use the supported restart; do not infer the
serving version from the CLI alone. Persistent ownership improvements also
remain #961. Isolated non-keyring Codex campaign authentication remains #983
after v1.4.1 and before v1.5.0. Quiet ordinary adoption does not prove optional
campaign readiness. No new paid hosted Devin session is authorized by this
release procedure.

The privacy boundary is unchanged. Upload only the maintained metadata allowlist,
never credentials, source, diffs, prompts, transcripts, private paths, task prose,
graphs, queries, citations or raw provider output. Stored receipts and fresh
authenticated aggregate visibility require separate release-specific evidence.

## Acceptance still required

The supervisor owns the canonical full suite, independent exact-head Claude
review, CI/gate, merge, annotated tag, no-publish build, publication and all
installed-package/campaign/Board/cloud acceptance. Bind actual wheel/sdist
filenames, digests, inspected contents and installed behavior to the reviewed
release commit. Source inclusion alone does not prove published inclusion.
Follow the [v1.4.1 evidence matrix](v141-qualification.md) and
[current runbook](pypi-release.md); keep #915 open until every criterion passes.
