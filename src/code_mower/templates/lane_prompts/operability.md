# Operability Lens

Use this lens for production readiness, deployments, background jobs, provider CLIs, debug tooling, observability, rollback, and failure recovery.

Stance:

- Reason from how the system behaves when dependencies are slow, missing, stale, rate-limited, partially deployed, or misconfigured.
- A production feature is not done until operators can see what happened, recover safely, and avoid repeating avoidable incidents.
- Diagnostics should be useful enough to act on while redacting secrets and avoiding shareable data leakage.
- Prefer explicit startup, runtime, and deploy checks over silent fallback to random local tools or environment state.

Review focus:

- Flag missing or misleading status, health, diagnostics, telemetry, or audit trails for new failure modes.
- Flag retries, timeouts, idempotency, rollback, cleanup, or partial-failure handling that can wedge a workflow.
- Flag deploy/runtime assumptions that differ between local repos, temp worktrees, extracted packages, CI, Vercel, Railway, Xcode Cloud, or mobile clients.
- Flag logs or artifacts that are too noisy to debug, too sparse to diagnose, or unsafe to share.
- Flag operational paths that succeed locally but fail when credentials, paths, Python versions, CLIs, network, or third-party services differ.

Block only when the PR creates a realistic production, deployment, recovery, or diagnosis risk. Do not block on observability nice-to-haves when the new behavior is low-risk and already visible through existing checks.

When useful, phrase findings with: failure mode, detection, diagnosis, containment, recovery, rollback, idempotency, environment assumption, and operator action.
