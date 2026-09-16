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

Acquisition requires an explicit operator decision and network access. Choose a
fresh, absolute, operator-owned location **outside the target checkout and all
other Git repositories**. Replace the example below with that location; a
Git-ignored directory inside the checkout is still refused by containment.
Keep these absolute paths in the same shell for acquisition and the later build:

```bash
GRAPHIFY_ROOT="/absolute/operator-owned/graphify-0.9.58"
GRAPHIFY_ENV="$GRAPHIFY_ROOT/venv"
GRAPHIFY_WHEELS="$GRAPHIFY_ROOT/wheels"
GRAPHIFY_INDEXER="$GRAPHIFY_ENV/bin/graphify"
```

Download only the wheel, then verify before installing:

```bash
set -euo pipefail
python3.12 -m venv "$GRAPHIFY_ENV"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$GRAPHIFY_ENV/bin/python" -m pip --isolated download --no-cache-dir \
  --index-url https://pypi.org/simple/ --only-binary=:all: --no-deps \
  --dest "$GRAPHIFY_WHEELS" graphifyy==0.9.58
"$GRAPHIFY_ENV/bin/python" - "$GRAPHIFY_WHEELS" <<'PY'
from pathlib import Path
import hashlib
import sys
wheel, = Path(sys.argv[1]).glob('graphifyy-0.9.58-*.whl')
expected = 'e239803288e91c723d6e30540860bd6d5a1dc3f0914b9fc1104b0233e98aaeb8'
if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
    raise SystemExit('Graphify wheel digest mismatch; stop before installation')
PY
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$GRAPHIFY_ENV/bin/python" -m pip --isolated install --no-cache-dir \
  --index-url https://pypi.org/simple/ \
  "$GRAPHIFY_WHEELS"/graphifyy-0.9.58-*.whl
```

This installs the verified local wheel in the separate environment, with
dependency downloads restricted to canonical PyPI. Keep this environment,
the downloaded wheel, graph state and all provider output outside Git repositories.
Both pip commands clear ambient index and link variables and disable all pip
configuration files with `PIP_CONFIG_FILE=/dev/null`. `--isolated` alone still
permits global/site configuration and a file selected by `PIP_CONFIG_FILE`;
those sources must not add an alternate index or local dependency source.

## Separate contained offline build

Save the [accepted pin JSON](context-graph-lifecycle.md#commands) as
`$GRAPHIFY_ROOT/pin.json`. From the selected repository, in the same shell, bind
the immutable tracked commit and carry the absolute indexer path into the build:

```bash
set -euo pipefail
GRAPHIFY_REVISION="$(git rev-parse HEAD)"
code-mower context-graph build --pin-file "$GRAPHIFY_ROOT/pin.json" \
  --indexer "$GRAPHIFY_INDEXER" --revision "$GRAPHIFY_REVISION"
```

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
