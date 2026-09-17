# Optional Devin Setup Prompt

Use this document only when the owner has asked for Devin. The default adoption
is Claude + Codex, and nothing here adds Devin work to a repository that did not
select it.

This is the detailed companion to the short pointer in
[Orchestrator Prompt Pack](orchestrator-prompt-pack.md#optional-devin-setup).
The prompt pack keeps the authority and lease guardrails that apply to every
participant; this page keeps the provider-specific command detail so the
universal prompt stays readable for adopters who never select Devin.

## Authority Boundary

Role qualification and session leases are unchanged by anything on this page.
See [Participant Qualification](participant-qualification.md). Selecting a
transport grants no review or merge authority: Devin stays limited to bounded
builder work and informational review until the repository promotes that lane
under [Lane Promotion Policy](lane-promotion-policy.md).

## Setup Prompt

```text
The owner selected Devin for OWNER/REPO.

Report the current posture first: run code-mower doctor CONFIG --profile PROFILE
--devin and read the selected transport, readiness, and next actions. Use the
same CONFIG and PROFILE the repository actually uses; a bare run inspects the
packaged starter instead. A checkout that has no code-mower.yml has no path to
name: run code-mower doctor --packaged-starter --profile PROFILE --devin, which
selects the maintained packaged starter wherever this installation keeps it, and
use --packaged-starter in place of CONFIG only for discovery and setup
preview/staging below. That selector
ignores cwd-local config files and keeps the profile you name, so it reports the
same posture from any directory; --easy does not, because it is a first-run
profile alias whose starter fallback depends on what the working directory
contains. Never substitute the starter for a repository configuration to shorten
a command; it inspects a different posture.

To change transports, preview the selection with code-mower init CONFIG
--profile PROFILE --set-transport devin=devin_api_v3 --dry-run, then stage it
with --apply --output-dir .code-mower.generated. Staging writes only that review
tree: the active posture keeps reporting the installed configuration until the
generated files are reviewed and installed through the normal setup PR.
After starter adoption is reviewed and installed, explicitly switch verification
to code-mower doctor code-mower.yml --profile PROFILE --devin. For an existing
explicit configuration, verify with code-mower doctor CONFIG --profile PROFILE
--devin using that same original CONFIG. Never verify installed setup with
--packaged-starter: that immutable resource still describes the starter.
Add --json to either verification command for machine-readable remediation;
keep the same CONFIG and PROFILE in text and JSON. Do not embed an absolute
package installation path in a setup or verification command.

If the repository still carries .github/workflows/devin-audit-bridge.yml or
.github/workflows/devin-audit-labeler.yml, report them as superseded by the
maintained Sessions API v3 transport and propose removing exactly those files in
the same reviewed PR. Do not delete or rewrite repository-owned workflow files
yourself.

Do not set up owner credentials, do not start paid sessions, and do not treat an
ordinary CLI login as campaign readiness.
```

## Related Documents

- [Orchestrator Prompt Pack](orchestrator-prompt-pack.md)
- [Devin Work Orders](devin-work-orders.md)
- [Devin Review Parity](devin-review-parity.md)
- [Provider Matrix](provider-matrix.md)
