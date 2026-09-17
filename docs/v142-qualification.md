# v1.4.2 qualification and evidence matrix

This is the **final** qualification record for the published v1.4.2 release.
It replaces the unexecuted procedure that this page carried before the release
ran. The v1.4.0 and v1.4.1 artifacts and their historical campaign truth are
preserved and unchanged.

> The `v1.4.2` tag carries the prepublication snapshot of this page. The tag is
> immutable and is not rewritten. This copy on `main` is the record of what
> actually happened.

Release identity:

| Item | Value |
| --- | --- |
| Release commit | `55339bf1acf76d33be5937e80bdaad772e0b2bf5` |
| Tag | annotated `v1.4.2`, resolving to that exact commit |
| Release PR | #1006, audited at head `32706cf5a01af5863d6c713b83dfb196efde6d4c` |
| Wheel SHA-256 | `f8bf24dd8a982ed5ab28302e837cd5d2aeece6d984ed1c44fcb4688c3fb7a522` |
| Sdist SHA-256 | `aff202eea9748ab3734ea6b90ba3b48ea5aea1e21ae87fba03b77b64d5cdec42` |

## Outcomes

Only outcomes with observed evidence are marked PASS. Everything else says what
was not run, or was not required for publication, rather than being softened
into a pass.

| Boundary | Required evidence | Outcome |
| --- | --- | --- |
| Source | PR/head, sole Claude writer, independent qualified Codex excluded from diff contributors, focused/full tests and exact-head CI/gate | **PASS** -- independent Codex audit of PR #1006 head `32706cf5a01af5863d6c713b83dfb196efde6d4c` returned P0=0, P1=0, P2=0 ([evidence](https://github.com/codemower-ai/code-mower/pull/1006#issuecomment-5709758094)); full PR CI [run 35188065537](https://github.com/codemower-ai/code-mower/actions/runs/35188065537) passed |
| Candidate | Fresh clone at reviewed merge commit; package matrix, privacy, release readiness, no-publish workflow, wheel/sdist names and SHA-256 digests | **PASS** -- fresh-clone build, release-readiness and `twine check` passed; digests above |
| Required inclusion | Inspect actual artifacts for PRs #999/#1000/#1001/#1002/#1003, and confirm no new cloud event field | **PASS** -- inspected in the built artifacts; no new cloud event field |
| #951 boundary | Merged local-evidence code accepted; bounded hosted Devin canary separately observed | **PARTIAL, as designed** -- merged local-evidence code is included and accepted. The hosted Devin canary was **not run**: it requires an explicit 1-ACU owner authorization. This release claims no hosted result and does not close #951 |
| Headless candidate | Cold install and upgrade from v1.4.1 with supported uv/Python 3.12; CLI/wrapper/pin/serving Board version agreement after restart | **PASS** -- cold install and an isolated 1.4.1-to-1.4.2 upgrade both passed with preserved local configuration |
| Publication | Exact reviewed merge SHA, annotated tag, workflow/run identity and expected head, canonical PyPI names/digests matching approved artifacts | **PASS** -- production publish [run 35189302150](https://github.com/codemower-ai/code-mower/actions/runs/35189302150); tag resolves to the release commit; canonical PyPI digests match the approved artifacts |
| Release-triggered verification | Release-event workflow identity and job posture | **PASS** -- [run 35189721623](https://github.com/codemower-ai/code-mower/actions/runs/35189721623) passed. Its publication jobs were **intentionally skipped** by repository variables because the prior explicit publish workflow had already completed; that skip is the expected posture, not a failure |
| Installed published | Repeat cold/upgrade and Board checks for every reconciled Board process using the downloaded canonical package | **PASS** -- TestPyPI and production PyPI installs passed; both known local Board listeners were restarted and verified serving installed 1.4.2 |
| Metadata upload | Dry-run-first preview of already-allowlisted campaign/Board metadata | **Not required for publication.** No new field was needed and none was added |
| Cloud aggregate freshness | Fresh authenticated aggregate visibility observed separately | **Not claimed.** No cloud metadata or aggregate check is claimed as evidence for this release |

## Board restart verification

A read-only `code-mower board list --json` observed
two live local Board services before the release: port 5332
(`codemower-ai/code-mower`) and one additional private-repository port. Each
port's exact posture -- launchd-managed via #961, or a transient process -- was
classified from `code-mower board service status --json` rather than assumed,
and a managed port was restarted with `code-mower board service restart
--replace` rather than stop/serve so its supervision was never downgraded. After
the restart, every port reported `serving == installed == 1.4.2` with preserved
repositories and stores. #961's managed persistent-service semantics and stale
keepalive rejection applied unchanged.

Exact local paths and the private repository's name stay local. They are not
recorded here and must not be added.

## Installed lineage replay without source substitution

`tests/test_lineage_producer_artifacts.py` retains the installed lineage hooks
exercised for prior releases. Set `CODE_MOWER_QUALIFICATION_WHEEL` to the
absolute path of the exact verified canonical published wheel to bypass local
building. Run from the reviewed test harness with its dependencies available and
temporary state outside every Git repository (not an alternate product source):

```bash
VERIFIED_WHEEL="$(cd "$(dirname "$VERIFIED_WHEEL")" && pwd -P)/$(basename "$VERIFIED_WHEEL")"
CODE_MOWER_QUALIFICATION_WHEEL="$VERIFIED_WHEEL" PYTHONPATH=tests \
  python -m unittest \
  test_lineage_producer_artifacts.ArtifactTests.test_real_rendered_workflow_and_runner_failure_rows \
  test_lineage_producer_artifacts.ArtifactTests.test_installed_candidate_supervisor_and_public_readback_business \
  test_release_v142.InstalledPromptPackTests
```

The first line canonicalizes the wheel path. On macOS the system temporary
directory is reached through the `/var` -> `/private/var` symlink, so an
uncanonicalized path and a resolved one name the same file under two spellings.
The harness itself now canonicalizes its own temporary root and both sides of
every installed-module provenance assertion, so the replay is portable there;
canonicalizing the wheel path keeps the recorded artifact identity unambiguous
too.

Bind the tested artifact's name/digest and harness revision to the release
record. Never replace product modules with checkout files, use editable installs
as published-package evidence, replay old receipts, or treat zero observed usage
as settled billing. Record failed preliminary runs and corrected causes
honestly. Raw logs and account/provider bindings stay in authorized local
evidence; publish only sanitized counts/outcomes and allowlisted metadata.
