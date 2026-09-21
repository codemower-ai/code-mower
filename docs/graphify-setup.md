# Optional Graphify setup

Graphify's bounded provider foundation landed in v1.4.0; its complete qualified
integration, scorecard and query behavior shipped in v1.4.1, were extended in
v1.5.0, and remain available in current v1.5.2. It is an **optional** local
repository-graph provider, separately installed into
an operator-owned environment, explicitly activated, and outside the base
dependency set: a default Claude + Codex installation adds no Graphify
dependency, indexing step, hook or background service.

What a graph covers, and what it does not:

- Only the **tracked** tree of one immutable commit is indexed. Untracked and
  ignored files are never written into the build, so they have no path into a
  graph.
- Symlinks and submodules are skipped and recorded as skipped.
- Committed private state -- `.git`, `.graph`, `.graphify`, `graphify-out`,
  `.code-mower` -- is skipped at any depth.
- Nothing **watches the working tree**. There is no hook, watcher, or background
  service; a graph becomes stale the moment `HEAD` moves, and you refresh it
  explicitly.

To inspect the guidance from a fresh directory or your selected repository
configuration:

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

The next two paragraphs are included in v1.5.0 and arrived with
[PR #1007](https://github.com/codemower-ai/code-mower/pull/1007). They are not
part of the historical `v1.4.2` package. See
[v1.5.0 compatibility](#v150-compatibility-and-existing-generations).

Install any required language extras into this same separate environment before
the contained build. For SQL inputs, select `[sql]` on the verified local wheel
using the same canonical-index restrictions; keep the accepted provider version
and checksum unchanged. Missing parsers can otherwise leave inputs unprocessed
and the generation partial.

If runtime ownership checks refuse a Python installation or one of its linked
libraries, recreate the environment from a suitable operator-owned runtime.
Do not relax the ownership checks or broaden shared-runtime permissions. If
the host sandbox refuses multiprocessing, include `"--max-workers", "1"` in
the pin's `options` alongside `"--code-only", "--no-cluster"`. This selects the
provider's supported serial extractor without bypassing containment.

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

## Ramp-up in order

Once the provider environment exists and the pin file is saved, this is the
whole path from nothing to a first answer. Each step is explicit; none of it
runs on its own.

**1. Check the posture before building anything.**

```bash
code-mower context-graph doctor --pin-file "$GRAPHIFY_ROOT/pin.json"
```

`doctor` builds nothing. It reports `skip` rather than `fail` when nothing is
pinned or built, because an operator who never opted in has nothing wrong with
their installation.

**2. Build one generation, bound to an immutable tracked commit.**

```bash
GRAPHIFY_REVISION="$(git rev-parse HEAD)"
code-mower context-graph build --pin-file "$GRAPHIFY_ROOT/pin.json" \
  --indexer "$GRAPHIFY_INDEXER" --revision "$GRAPHIFY_REVISION"
```

The census comes from `git ls-tree -r` against that **commit** -- not the
working tree and not the index. Uncommitted edits are invisible to the build by
construction.

**3. Confirm the published generation is usable.**

```bash
code-mower context-graph status --json
```

`status` exits non-zero when the graph is not usable, so a script can branch on
it. A provider run that admitted an incomplete census publishes a generation
`status` calls `partial` and refuses, rather than describing it as `current`.

The rest of this step, up to step 4, is the **v1.5.0** behavior from #1029 and
#1031. `status` also reports `search`:
whether the installed query reader can consume the generation. A `current`
generation with `search: unavailable` exits non-zero. Read
`query_reader.next_action`:

- `reader: incompatible` is a known provider/reader mismatch. The
  `remediation` block names your installed Code Mower and the release that
  reads the generation. Upgrade Code Mower; the generation needs no rebuild.
  If it names an unreviewed provider instead (another release, or another
  distribution at the same version), refresh with the pinned
  `graphifyy` `0.9.58`.
- `reader: unreadable` means the generation holds something no reader has an
  account of. It fails closed on purpose; report it rather than working around
  it.

The published `v1.4.2` package has no such check. There, `status` and
`connection-status` can report a usable graph and `search: available` while
the first dependency query fails with `unreadable` on a generation containing
`doc_ref` nodes. That query failure is this mismatch; upgrading to the release
containing #1007 and #1029 resolves it.

**4. Register the graph as a local context connection.**

```bash
code-mower context-graph connect --connection local-graph \
  --repository owner/repo --recipient claude:builder --recipient codex:reviewer
code-mower context-graph connection-status --connection local-graph
```

A local connection has no principal, no workspace and no credential. Nothing is
written to the OS credential vault, no browser opens, and no endpoint is
contacted. Which repositories and recipients are approved is an authorization
decision that belongs to the connection, not to any individual query.

**5. Ask one bounded question.**

```bash
code-mower context-graph query --question impact --target parse_config \
  --authorization AUTH.json --packet-out /tmp/packet.json --json
```

`--question` is one of `impact`, `dependency`, `symbol`, or `related_tests`.
`--target` is a symbol name or a repository-relative path. `AUTH.json` is a file
you name, carrying exactly `connection`, `policy`, `repository` and `work_item`;
see [bounded queries and context packets](context-graph-queries.md) for its
contents and for what each question traverses. Standard output is metadata only
-- counts, states, the bound revision and generation, and omission codes. The
evidence goes to the `--packet-out` file, created `0600`, or nowhere at all.

**6. Refresh explicitly after the revision changes.**

```bash
GRAPHIFY_REVISION="$(git rev-parse HEAD)"
code-mower context-graph refresh --pin-file "$GRAPHIFY_ROOT/pin.json" \
  --indexer "$GRAPHIFY_INDEXER" --revision "$GRAPHIFY_REVISION"
```

Nothing watches the working tree, so nothing refreshes on your behalf. When
`HEAD` moves, the published generation is stale for the new revision and
authorization fails outright rather than answering today's question with
yesterday's code. `refresh` rebuilds and atomically publishes a new generation.
A packet bound to the previous generation is refused at load, which is the
intended outcome, not a regression.

**7. Tear down when you are finished.**

```bash
code-mower context-graph disconnect --connection local-graph
code-mower context-graph remove
```

`disconnect` disables the connection and drops the packets it authorized.
`remove` deletes this checkout's private graph state. The operator-owned
provider environment from
[Separate acquisition environment](#separate-acquisition-environment) is yours
to keep or delete separately; Code Mower never touches it.

<a id="published-v142-versus-current-main"></a>

## v1.5.0 compatibility and existing generations

The historical `v1.4.2` package contains the originally shipped optional
Graphify integration. v1.5.0 includes the compatibility and readiness additions
throughout this guide, including the language-extras/runtime-ownership guidance
and reader-based search-readiness checks. None of these additions changes the
accepted provider pin or enables Graphify by default.

[PR #1007](https://github.com/codemower-ai/code-mower/pull/1007) has since
merged to `main` with further real-pilot compatibility fixes: a bounded 16 MiB
provider-manifest reader separate from the 256 KiB bound on Code Mower's own
generation manifest, explicit refusal of an oversized provider manifest,
`doc_ref` nodes accepted as declared non-code exclusions, and `related_tests`
recognition of JavaScript/TypeScript `.test`/`.spec` and `__tests__`
conventions together with `imports` relationships. The language-extras and
runtime-ownership paragraphs under
[Separate acquisition environment](#separate-acquisition-environment) arrived
with the same change. All of it is included in v1.5.0; none of it is in the historical `v1.4.2` package.
PR #1031 adds reader-based readiness, actionable compatibility diagnostics, and
separate generation/query completeness. A bounded partial answer remains usable;
it does not itself require rebuilding an otherwise complete generation.

The accepted provider pin is unchanged. This is a Code Mower compatibility fix,
not a Graphify upgrade: `graphifyy` `0.9.58` and the recorded wheel digest above
stay exactly as they are.

Because a published generation is never rewritten in place, installing v1.5.0 does not repair a generation you already built. That matters only
for a generation one of #1007's compatibility gaps actually affected -- most
often an older frontend generation left **partial**: one whose oversized
provider manifest was refused, or one whose inputs a missing language parser
could not process. Those are the generations to rebuild.

This is not a blanket rebuild of everything built before v1.5.0.
Ask `code-mower context-graph status --json` first: a generation it already
reports usable is unaffected and needs no rebuild. If it reports `partial`,
rebuild that generation explicitly with `code-mower context-graph refresh`
(step 6), which publishes a new generation at the same revision, then confirm
`status` reports it usable rather than `partial`.
