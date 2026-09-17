# v1.4.1 qualification and evidence matrix

> **v1.4.1 is a completed release.** This page preserves the prepublication
> procedure written before release #915 ran, including its then-`Pending`
> outcome column. It is kept as the historical record rather than rewritten.
> Final v1.4.1 evidence is on the
> [v1.4.1 GitHub release](https://github.com/codemower-ai/code-mower/releases/tag/v1.4.1);
> the current release is [v1.4.2](v142-qualification.md).

This was an unexecuted release procedure when written. The Codex source PR owns
source changes only. The supervisor runs the canonical full suite once, the independent exact
current-head Claude review and CI/`code-mower/gate`, then serializes merge and
owner-authorized release operations. Preserve v1.4.0 artifacts and historical
campaign truth. Do not create paid hosted Devin sessions from an old runbook.

Record these as separate states on #915 and parent #902/#979/#900:

| Boundary | Required evidence | Current source-writer outcome |
| --- | --- | --- |
| Source | PR/head, sole Codex writer, independent qualified Claude excluded from diff contributors, focused/full tests and exact-head CI/gate | Focused results in PR; remaining supervisor gates pending |
| Candidate | Fresh clone at reviewed merge commit; package matrix, privacy, release readiness, no-publish workflow, wheel/sdist names and SHA-256 digests | Pending |
| Required inclusion | Inspect actual artifacts for #966/#969/#971/#973/#980/#981/#984/#985/#987/#988, lineage replacement #990/#991/#992/#997 and Graphify #914/#982 | Merged baseline is not artifact evidence |
| Graphify | Accepted 0.9.58 wheel digest; real contained code-only/no-cluster run on immutable public tracked checkout; raw-format query/delivery at consuming revision; #876 comparison set and thresholds, isolation/staleness/completeness/citation limits and no-provider fallback | Release-specific scorecard pending; synthetic fixtures alone are insufficient |
| Headless candidate | Cold install and upgrade from v1.4.0 on Linux with supported uv/Python 3.12; CLI/wrapper/pin/serving Board version agreement after restart | Pending; record OS/architecture and credential posture |
| Publication | Exact reviewed merge SHA, annotated tag, workflow/run identity and expected head, canonical PyPI names/digests matching approved artifacts | Pending; no source-only publication claim |
| Installed published | Repeat cold/upgrade, lineage, adoption, prompt-pack and Board checks using downloaded canonical package | Pending; no checkout substitution |
| Campaign | Authorized Claude + Codex scope, isolated readiness, actual provider execution/exit/review, caps and unknown settlement separately | Pending; an unrun provider cannot pass |
| Cloud | Metadata-only preview, stored receipt, then fresh authenticated aggregate visibility observed separately | Pending; stale view requires follow-up, not a pass |

For both candidate and canonical published packages, exercise fresh and explicit
repositories: bare `init --builders codex,claude,cursor` previews without writes;
selected config/profile survives setup staging; invalid selections are actionable.
Ordinary no-campaign adoption adds no campaign-auth owner action. Unselected
integrations stay quiet; warn/exit 0 alone is not full activation.

Follow the literal optional Devin prompt pack from the installed package source
distribution. Start fresh discovery with `doctor --packaged-starter --profile
PROFILE --devin`; stage with the same selector/profile, then after reviewed
installation use `doctor code-mower.yml --profile PROFILE --devin`. Existing
repositories keep their explicit CONFIG. Check both text and JSON remediation
and assert no package installation path appears. No credentials or paid sessions
are needed for this configuration walkthrough.

Verify visible eligible lease creation, holder discovery, 12-hour default,
same-ID cross-process renew/release and no lease for unqualified Devin
orchestration. Show operations stay read-only. Preview and explain setup drift;
never automatically enable tokens/auto-merge or overwrite repository setup.
Repeat loopback Board cold-to-fresh status/doctor/cache, path redaction and
port-selected stop/restart. #961 and #983 limitations remain explicit.

## Installed lineage replay without source substitution

`tests/test_lineage_producer_artifacts.py` retains #997's installed lineage
hooks. Set `CODE_MOWER_QUALIFICATION_WHEEL` to the absolute path of the exact
verified downloaded candidate or canonical published wheel to bypass local
building. The tests install that wheel in isolation, assert module origin, and
exercise emitted producer/runner and actual supervisor/public-readback behavior.
Run from the reviewed test harness with its dependencies available and temporary
state outside every Git repository (not an alternate product source). The two
lineage runtime hooks execute installed product modules and templates; the
separate manifest/baseline tests inspect source parity only:

```bash
CODE_MOWER_QUALIFICATION_WHEEL="$VERIFIED_WHEEL" PYTHONPATH=tests \
  python -m unittest \
  test_lineage_producer_artifacts.ArtifactTests.test_real_rendered_workflow_and_runner_failure_rows \
  test_lineage_producer_artifacts.ArtifactTests.test_installed_candidate_supervisor_and_public_readback_business \
  test_release_v141.InstalledPromptPackTests
```

Bind the tested artifact's name/digest and harness revision to the release
record. Never replace product modules with checkout files, use editable installs
as published-package evidence, replay old receipts, or treat zero observed usage
as settled billing. Record failed preliminary runs and corrected causes honestly.
Raw logs and account/provider bindings stay in authorized local evidence; publish
only sanitized counts/outcomes and allowlisted metadata.
