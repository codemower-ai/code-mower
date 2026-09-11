# v1.3 context qualification

The optional Coworker integration is operationally qualified for supervised use.
The pilot demonstrated account-bound delivery and found one useful prior-work
reference across two frozen cases. It did not demonstrate faster delivery or
find a defect requiring either existing patch to change.

## Evidence boundaries

Implementation stages C1–C6 shipped independently through the Code Mower gate,
with a Codex builder and independent current-head Claude review. The final
implementation stage passed 2,795 local tests, Python 3.12–3.14 CI, package
checks, cold-clone installation and no-provider smoke. See epic [#868](https://github.com/codemower-ai/code-mower/issues/868)
and PRs [#883](https://github.com/codemower-ai/code-mower/pull/883),
[#884](https://github.com/codemower-ai/code-mower/pull/884),
[#885](https://github.com/codemower-ai/code-mower/pull/885) and
[#886](https://github.com/codemower-ai/code-mower/pull/886).

The private pilot used two already-implemented PRs as frozen reference cases.
Their repository, work-item text, source, identities and citations remain private.
The existing lane retains tracker and human merge ownership. Qualification made
no GitHub or Jira mutations and did not install a new gate in those repositories.
Local reference input records exercised delivery and review authorization; they
are not live GitHub gate verdicts.

The existing frozen heads also had Code Mower review evidence: one completed
Codex audit for case A, and completed Claude and Codex audits for case B. Those
pre-existing results are distinct from the context assessment.

The baseline was each frozen PR description and diff, not a fresh authoritative
tracker read or a blind implementation trial. Claude independently assessed the
Code Mower work orders and the same bounded evidence that Codex inspected. This
was one retrospective model call across both cases. It was not a new source-code
implementation or a substitute for the existing PRs' reviews.

## Live retrieval and delivery

| Observation | Case A | Case B |
| --- | --- | --- |
| Selected session host | Codex | Claude |
| Evidence records | 3 | 3 |
| Provider read requests | 2 | 2 |
| Discovery pages | 1 | 1 |
| Response bytes | 1,589 | 1,531 |
| Observed fetch time | 5.474 s | 1.356 s |
| Completeness | Partial | Partial |
| Text truncated | No | No |
| Source revision/confidence | Unknown | Unknown |
| Provider-reported monetary cost | Unavailable | Unavailable |

Each request performed one discovery page and one bounded search. Neither search
was expanded to improve the score. All six approved Claude/Codex
orchestrator/builder/reviewer roles received identical evidence bytes, with a
fresh online authorization before replay. Subsequent processes resumed from the
private stored packet without another search. Authorization was checked again
before saving the assessment as private feedback.

An explicit context doctor probe also refreshed the selected connection and
reported ready without searching. The earlier disposable-grant qualification
confirmed refresh-token revocation; the active pilot grant was not revoked to
repeat that experiment. Runtime and regression coverage separately exercise
wrong account/destination, expired or revoked authorization, interruption,
changed context on an unchanged head, required-input failure, optional outages,
malicious source instructions, bounds and private-output handling.

## Usefulness assessment

| Case | Added value | Disposition |
| --- | --- | --- |
| A: bounded parser hardening | Retrieved adjacent project work; no direct new requirement, owner question or regression test. | Keep the existing patch scope. |
| B: accessible control wording | Found prior work on a related control with different terminology, raising a possible consistency question. | Source confirmed the difference; record an owner terminology question before changing the patch. |

The related-control lead came from a memory summary. A subsequent read of source
and its existing regression assertion at the frozen PR head confirmed the
different labels. This is evidence for an owner consistency question, not policy
or a new acceptance criterion. Product tests were not rerun during this frozen
assessment; it made no source changes. Existing targeted
regression tests already covered both patches' stated intent; no additional test
was justified solely by the retrieved material. One model description conflated
partial evidence with truncation; the structured packet correctly reported
partial, untruncated evidence and governs this scorecard.

The independent assessment took 23.121 seconds wall time and reported $0.0902698
in model cost. That is the assessment call only, not the complete pilot cost.
Coworker monetary cost and total operator effort were not available. Original
implementation elapsed time, time to reviewed PR attributable to context, defect
escape rate and time saved were not measured. Two retrospective cases cannot
establish a productivity improvement.

## Adoption implications

Keep context optional by default. Read the cited packet before treating it as a
constraint, especially when the provider reports partial results or unresolved
entities. Narrow work-item scope and explicit refresh are useful controls;
retrieval relevance and source verification remain areas to improve.

The base install requires no Coworker SDK, account or login. Configured use needs
an explicit private account/workspace, an available supported OS vault and
approved repository/recipient destinations. Hosted reviewers need equivalent
local authorization to consume private context; they cannot fall back to an
unrelated account. See [setup](context-setup.md) and [delivery](context-delivery.md).
Graphify remains a separate candidate under [#876](https://github.com/codemower-ai/code-mower/issues/876),
with a synthetic local-graph contract fixture rather than a shipped adapter.
