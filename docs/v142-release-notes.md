# Code Mower v1.4.2 Release Notes

Status: **published**. v1.4.2 is the current package-index release baseline,
with pinned package install spec `code-mower==1.4.2`. It was published from
release commit `55339bf1acf76d33be5937e80bdaad772e0b2bf5` under the annotated
`v1.4.2` tag. The v1.4.0 and v1.4.1 tags, assets, release notes and their
historical runbooks remain immutable and unchanged by this release.

> The copies of this page and of the
> [qualification record](v142-qualification.md) that the immutable `v1.4.2`
> tag carries are the prepublication source-candidate snapshots, written
> before the release ran. The tag is not rewritten. These pages on `main` are
> the final release and qualification record; read them rather than the
> tag-pinned copies when you want the outcome.

## What shipped in v1.4.2

This release delivers the accepted #945/#900 Board clarity work:

- the work-first Now/Timeline/Releases/Health views (PR #1000);
- provider-neutral remote lifecycle observations (PR #1002);
- managed persistent Board services with stale-keepalive rejection during a
  release restart, closing issue #961 (PR #1001);
- exact local work observations (PR #999); and
- the qualified independent head-bound evidence and session-visibility
  composition from issue #951's merged local-evidence code (PR #1003).

No new cloud event field was added. Slack-specific and hosted-cloud mappings
remain #921. Graphify, shipped in v1.4.1, is unchanged here: still separately
installed, explicitly activated, and outside the base dependency set.

## Release evidence

| Item | Value |
| --- | --- |
| Release commit | `55339bf1acf76d33be5937e80bdaad772e0b2bf5` |
| Tag | annotated `v1.4.2`, resolving to that exact commit |
| GitHub release | <https://github.com/codemower-ai/code-mower/releases/tag/v1.4.2> |
| Independent audit | Codex audit of PR #1006 head `32706cf5a01af5863d6c713b83dfb196efde6d4c`, PASS with P0=0, P1=0, P2=0 ([evidence](https://github.com/codemower-ai/code-mower/pull/1006#issuecomment-5709758094)) |
| Full PR CI | [run 35188065537](https://github.com/codemower-ai/code-mower/actions/runs/35188065537) |
| Production publish | [run 35189302150](https://github.com/codemower-ai/code-mower/actions/runs/35189302150) |
| Release-triggered verification | [run 35189721623](https://github.com/codemower-ai/code-mower/actions/runs/35189721623) |
| Wheel SHA-256 | `f8bf24dd8a982ed5ab28302e837cd5d2aeece6d984ed1c44fcb4688c3fb7a522` |
| Sdist SHA-256 | `aff202eea9748ab3734ea6b90ba3b48ea5aea1e21ae87fba03b77b64d5cdec42` |

The publication jobs inside the release-triggered verification run were
skipped on purpose: the repository publish variables were off, because the
prior explicit publish workflow had already completed. A skipped publish job
there is the expected outcome, not a failed one.

## What was verified after publication

- Fresh-clone build, release-readiness and `twine check`.
- TestPyPI and production PyPI installs of the canonical artifacts.
- Cold install, and an isolated 1.4.1-to-1.4.2 upgrade that preserved existing
  local configuration.
- Both known local Board listeners were restarted from the release and verified
  serving the installed 1.4.2: port 5332 (`codemower-ai/code-mower`) and one
  additional private-repository Board. Each port was restarted according to its
  own classified managed-or-transient posture rather than a blind stop/serve.

The [v1.4.2 qualification record](v142-qualification.md) lists each boundary,
what evidence was required, and which outcomes were proven versus not run.

## Boundaries that stay open

- **#951's bounded hosted Devin canary.** #951's merged code evidence is in
  this release. Its hosted canary requires an explicit 1-ACU owner
  authorization, has not been run, and is not claimed here. #951 stays open for
  that canary alone.
- **Isolated non-keyring Codex campaign authentication** remains #983.
- **Slack worker delivery** remains an ingress foundation only; the supervised
  Slack runtime is the `v1.5.0` phase tracked by #903 / #923.
- No new paid hosted Devin session was authorized by this release.

The privacy boundary is unchanged. Upload only the maintained metadata
allowlist, never credentials, source, diffs, prompts, transcripts, private
paths, task prose, graphs, queries, citations or raw provider output.

## Installing this release

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.4.2
command -v code-mower
code-mower --version
```

See [Install And Bootstrap](install.md) for the uv, contributor, upgrade and
optional Coworker paths.
