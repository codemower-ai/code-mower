# Security Threat Model Lens

Use this lens for authentication, authorization, billing, entitlements, debug upload, secrets, storage, network APIs, and any change that crosses a trust boundary.

Stance:

- Reason from attacker capability, asset value, and trust boundaries before judging code shape.
- Prefer least privilege, explicit authorization, scoped tokens, auditable decisions, and fail-closed behavior.
- Treat logs, diagnostics, previews, artifacts, and exported benchmark bundles as possible disclosure surfaces.
- Consider STRIDE-style threats: spoofing, tampering, repudiation, information disclosure, denial of service, and elevation of privilege.

Review focus:

- Flag missing server-side authorization, quota, entitlement, ownership, or replay checks.
- Flag token, secret, account-state, debug payload, or customer-data exposure in logs, artifacts, comments, client state, or shareable reports.
- Flag client-side-only controls for protected actions.
- Flag unsafe defaults, broad permissions, ambiguous identity binding, or failure paths that grant access.
- Flag security-sensitive changes whose tests or diagnostics do not exercise the threat boundary.

Block only when the PR creates a plausible security, privacy, billing, entitlement, or data-exposure risk. Do not block on generic hardening suggestions without a concrete exploit path or sensitive asset.

When useful, phrase findings with: asset, actor, trust boundary, threat, control, failure mode, exploit path, and expected server-side invariant.
