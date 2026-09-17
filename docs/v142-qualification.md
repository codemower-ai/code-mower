# v1.4.2 qualification and evidence matrix

This is an unexecuted release procedure. The Claude source PR owns source
changes only. The orchestrator runs the canonical full suite once, an
independent exact current-head Codex audit and CI/`code-mower/gate`, then
serializes merge and owner-authorized release operations. Preserve v1.4.0 and
v1.4.1 artifacts and historical campaign truth. Do not create paid hosted
Devin sessions from an old runbook.

Record these as separate states on #952 and parent #945/#900:

| Boundary | Required evidence | Current source-writer outcome |
| --- | --- | --- |
| Source | PR/head, sole Claude writer, independent qualified Codex excluded from diff contributors, focused/full tests and exact-head CI/gate | Focused results in PR; remaining orchestrator gates pending |
| Candidate | Fresh clone at reviewed merge commit; package matrix, privacy, release readiness, no-publish workflow, wheel/sdist names and SHA-256 digests | Pending |
| Required inclusion | Inspect actual artifacts for #999/#1000/#1001/#1002/#1003, and confirm no new cloud event fields | Merged baseline is not artifact evidence |
| #951 boundary | Merged local-evidence code accepted; bounded hosted Devin canary still pending; this release does not claim the hosted result or close #951 | Pending; record separately from source acceptance |
| Headless candidate | Cold install and upgrade from v1.4.1 on Linux with supported uv/Python 3.12; CLI/wrapper/pin/serving Board version agreement after restart of the Board processes named in #952 | Pending; record OS/architecture, credential posture, and the exact reconciled Board inventory |
| Publication | Exact reviewed merge SHA, annotated tag, workflow/run identity and expected head, canonical PyPI names/digests matching approved artifacts | Pending; no source-only publication claim |
| Installed published | Repeat cold/upgrade, Board doctor and restart checks for every reconciled Board process using downloaded canonical package | Pending; no checkout substitution |
| Metadata upload | Dry-run-first preview of already-allowlisted campaign/Board metadata; CodeMower.com accepts only allowlisted fields; no new field required | Pending; preview only, no apply from this source PR |
| Cloud | Metadata-only preview, stored receipt, then fresh authenticated aggregate visibility observed separately (#974/#976) | Pending; stale view requires follow-up, not a pass |

For the candidate and canonical published packages, exercise fresh and
explicit repositories exactly as documented for prior releases; ordinary
no-campaign adoption adds no campaign-auth owner action, and unselected
integrations stay quiet.

## Board doctor and multi-service restart verification

`code-mower board doctor` must pass against each Board process named in
#952's target inventory. A read-only `code-mower board list --json` observed
two live local Board services pre-release: port 5332 (`codemower-ai/code-mower`)
and one additional private-repository port. Each port's exact posture --
launchd-managed via #961, or a transient process -- is classified from
`code-mower board service status --json`, never assumed, and a managed port
is restarted with `code-mower board service restart --replace` rather than
stop/serve so its supervision is never downgraded. Every port must report
`serving == installed == 1.4.2` with preserved repositories/stores after
this restart. #961's managed persistent-service semantics and stale
keepalive rejection apply unchanged; this source PR prepares the
verification guidance but does not restart installed services.

## Installed lineage replay without source substitution

`tests/test_lineage_producer_artifacts.py` retains the installed lineage
hooks exercised for prior releases. Set `CODE_MOWER_QUALIFICATION_WHEEL` to
the absolute path of the exact verified downloaded candidate or canonical
published wheel to bypass local building. Run from the reviewed test harness
with its dependencies available and temporary state outside every Git
repository (not an alternate product source):

```bash
CODE_MOWER_QUALIFICATION_WHEEL="$VERIFIED_WHEEL" PYTHONPATH=tests \
  python -m unittest \
  test_lineage_producer_artifacts.ArtifactTests.test_real_rendered_workflow_and_runner_failure_rows \
  test_lineage_producer_artifacts.ArtifactTests.test_installed_candidate_supervisor_and_public_readback_business \
  test_release_v142.InstalledPromptPackTests
```

Bind the tested artifact's name/digest and harness revision to the release
record. Never replace product modules with checkout files, use editable
installs as published-package evidence, replay old receipts, or treat zero
observed usage as settled billing. Record failed preliminary runs and
corrected causes honestly. Raw logs and account/provider bindings stay in
authorized local evidence; publish only sanitized counts/outcomes and
allowlisted metadata.
