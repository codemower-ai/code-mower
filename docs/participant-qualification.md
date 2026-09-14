# Participant Qualification

This source contract is part of the stabilization work for the next package.
It does not change the published `v1.4.0` artifacts or their qualification
history. Claude and Codex remain the default pair. Existing non-Devin role
policy is preserved; the new decision does not assert new qualification for
other optional providers.

A participant selection names a product. A transport describes the implemented
execution path. Repository policy can narrow an allowed role. Qualification is
maintained evidence for a particular product, role, transport, capability, and
scope. Runtime readiness is a separate observation by the trusted local caller.
All must agree before execution starts.

| Devin role | Maintained qualification | Current admission |
| --- | --- | --- |
| Local CLI builder | Bounded local builder baseline, `devin-cli-builder-v1` | Requires bounded work, allowed policy, and ready runtime |
| Hosted builder | Bounded PR-bound baseline, `devin-hosted-builder-v140` | Requires bounded work, allowed policy, and ready runtime |
| Informational reviewer | No merge qualification required | Informational only; supported transport and ready runtime still required |
| Merge-authority reviewer | No maintained role record | Ineligible |
| Orchestrator | No maintained role record | Ineligible; hosted transport also lacks coordination support |

The builder records refer to the [local builder qualification](https://github.com/codemower-ai/code-mower/issues/659)
and [hosted pilot scorecard](https://github.com/codemower-ai/code-mower/issues/900#issuecomment-5657839697).
Neither qualifies another role. The existing decisions establish no elapsed-time
expiry; a record can be revoked, assigned an expiry, or become stale when its
bound capability changes. A missing, expired, revoked, or mismatched record
cannot authorize new work.

## Shared Decision

`code_mower.role_eligibility.decide_role` returns the closed
[`code_mower.roleEligibility.v1` schema](../src/code_mower/role_eligibility.schema.json).
It reports product, role, transport, scope, capability, qualification, policy,
runtime, status, and reason. It contains no provider references, local paths,
account identifiers, credentials, or free-form evidence. Session briefs and
Devin setup readiness expose this same decision. The future Slack supervisor
adapter must consume this callable with trusted policy and readiness; the current
Slack ingress foundation does not launch workers or grant roles.

`require_role` admits a pending runtime check for planning, but requires a ready
runtime for execution. Callers must compute the decision from trusted local
configuration and readiness facts; a provider-supplied object is not an
admission token. A denial provides one actionable diagnostic and does not
substitute another participant.

`session start` evaluates host/orchestrator and reviewer roles before acquiring
a lease or saving state, including `--dry-run` and `--no-lease` paths. Fresh
context mutations recheck saved host/orchestrator admission; an old brief cannot
resume a now-ineligible role. Historical brief reads and context status remain
available. An eligible
mutating start prints the exact lease inspection and release commands; see
[session lifecycle](sessions.md#single-orchestrator-lease).
The hosted work-order library checks dispatch, clarification, and fix admission
before state locks, reservations, or provider calls. The maintained raw
`session dispatch/message --provider devin` CLI uses the same `require_builder`
entrypoint with explicit trusted configuration and runtime readiness, before
credential or prose access. Read-only inspection and
collection, and cancellation of an existing binding, remain usable after
eligibility changes. Qualification never becomes part of the immutable
work-order identity; see [hosted work orders](devin-work-orders.md).

## Policy And Future Promotion

A repository can disable a role or select a maintained qualification ID:

```yaml
role_policy:
  devin:
    builder:
      enabled: true
      qualification: devin-hosted-builder-v140
    orchestrator:
      enabled: false
```

Qualification IDs are transport-specific. This example requires the hosted
builder record; it does not silently switch a local builder to the API.
Selecting an unknown or mismatched ID denies the role. `enabled: true`, a
working CLI, `merge_authority: true`, and a successful install campaign cannot
create qualification. The parser rejects self-attested `qualified` or
`verified` policy fields.

Future promotion requires a separately reviewed maintained record with the
matching role, supported transport/capability, scope, and evidence. A Devin
review lane must also reference that record with `role_qualification` and
explicitly retain its product and transport identity. Repository selection and
provider output cannot create records. Unsupported transport roles stay
ineligible even when a record exists. Independent current-head review and
contributor exclusion remain separate requirements for a merge verdict.
