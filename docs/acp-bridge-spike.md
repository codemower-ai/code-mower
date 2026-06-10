# Code Mower ACP Bridge Spike

Agent Client Protocol is a JSON-RPC-over-stdio protocol for driving coding-agent CLIs through a provider-neutral client. Code Mower should treat it as a research lane until one provider can run a full head-bound audit through it without weakening the existing audit invariants.

## Goal

Replace per-provider shell-and-parse glue with one client primitive where the underlying CLI supports ACP.

The first useful proof is an informational audit lane that:

- accepts repo, PR number, head SHA, prompt lenses, and artifact output path;
- starts the configured ACP command from `CODE_MOWER_ACP_COMMAND`;
- sends the same trusted review doctrine used by other audit wrappers;
- records stdout/stderr/session metadata in a held blind-review artifact;
- emits a normal trailer-bearing audit comment only after schema validation.

Hermes Agent is a good future candidate because its documentation exposes
ACP-related surfaces. The first plain `hermes-cli` one-shot calibration now
exists and showed useful #347 signal, but also a known-clean blocker, an
infra/parse failure, and audit-input gaps on large historical diffs. That moves
Hermes from "unknown" to "interesting but not boring." ACP should reduce adapter
churn only after the provider runtime, auth, prompt, head-SHA, context-pack,
parser, and artifact contracts are already boring.

## Non-goals

- Do not make ACP a merge-authority lane during the spike.
- Do not import long-lived conversational state into Code Mower. Audits stay stateless and head-bound.
- Do not bypass existing wrappers for providers that already have reliable structured lanes.
- Do not use ACP as a dashboard, IDE, browser, or human cockpit surface.

## Acceptance Criteria

1. `code-mower doctor --profile cli_research` reports the configured ACP command and protocol.
2. A fixture-backed client test proves request/response framing without requiring a live provider.
3. A dry-run audit can generate a held artifact without posting a merge signal.
4. The bridge refuses to produce a done/blocked trailer when the response is missing the expected schema or head SHA.
5. The provider catalog continues to mark `acp_bridge` as manual and informational.

## Open Questions

- Which provider should be the first proof case?
- Does Hermes' ACP surface preserve the same stateless, head-bound audit
  invariants as its one-shot CLI path?
- Can an ACP-backed Hermes proof resolve the #390 context/input gap better than
  the one-shot CLI without importing long-lived session state?
- Should ACP responses use the provider's native schema, or should Code Mower require a normalized audit schema at the bridge boundary?
- Can the client capture enough token, model, and elapsed-time metadata for spend/value analytics without provider-specific code?
