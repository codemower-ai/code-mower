# Optional Graphify setup

Graphify is opt-in local code context. Default Claude + Codex installation adds
no Graphify dependency, indexing step, hook or background service. To inspect
the guidance from a fresh directory or your selected repository configuration:

```bash
code-mower init --graphify
code-mower init code-mower.yml --profile recommended --graphify --json
```

This selector only adds guidance to the init plan. It does not install or invoke
Graphify, build an index, change participants, or enable a context connection.
`--apply` still stages the ordinary reviewed setup; Graphify guidance adds no
files or config changes. Existing configuration and profile choices are retained.

## Separate acquisition environment

The accepted provider is `Graphify-Labs/graphify`, distribution `graphifyy`,
version `0.9.58`, tag commit `23f2ffaa43fd12f25d9eabe91e6d184b5d89b474`.
The accepted wheel SHA-256 is
`e239803288e91c723d6e30540860bd6d5a1dc3f0914b9fc1104b0233e98aaeb8`.
See the [evaluation and thresholds](graphify-evaluation.md). `graphify.net` is
not an interchangeable service. Never install an unpinned newer provider as
part of a Code Mower upgrade.

Acquisition requires an explicit operator decision and network access. Download
only the wheel into a new directory, then verify before installing:

```bash
python3.12 -m venv .graphify-env
.graphify-env/bin/python -m pip --isolated download --no-cache-dir \
  --index-url https://pypi.org/simple/ --only-binary=:all: --no-deps \
  --dest .graphify-wheels graphifyy==0.9.58
.graphify-env/bin/python - <<'PY'
from pathlib import Path
import hashlib
wheel, = Path('.graphify-wheels').glob('graphifyy-0.9.58-*.whl')
expected = 'e239803288e91c723d6e30540860bd6d5a1dc3f0914b9fc1104b0233e98aaeb8'
if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
    raise SystemExit('Graphify wheel digest mismatch; stop before installation')
PY
```

After that check, install that exact local wheel in the separate environment
with dependency downloads restricted to canonical PyPI. Keep this environment,
the downloaded wheel, graph state and all provider output out of tracked source.
Do not run `graphify` against the working checkout: explicit builds go through
Code Mower's [contained lifecycle](context-graph-lifecycle.md), using the exact
wheel pin and immutable tracked commit. Code-only/no-cluster extraction is the
maintained mode; failed containment is a refusal, never permission to retry
unsandboxed. Provider acquisition is distinct from contained offline execution.

The [bounded query and guided delivery contract](context-delivery.md) binds
results to the consuming revision. Stale, incomplete, oversized or unresolved
citations do not establish fresh context. Without a provider, ordinary setup and
review remain available. Do not upload graph, query, path, citation or source
content; the release scorecard contains sanitized measurements only.
