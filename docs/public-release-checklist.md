# Code Mower Public Release / v1.0 Checklist

Use this checklist for public OSS readiness and v1.0 hardening. The standalone
`code-mower` repository is public; the remaining work is to make the first
install, first doctor run, first audit, and first report boring for users who do
not know the original reference repos.

## Current Public Status

- Public repository exists.
- Apache-2.0 `LICENSE` and `NOTICE` are present.
- The package has alpha releases and reports its version with
  `code-mower --version`.
- The v0.5 alpha entrypoint is `v0.5.0-alpha.46`, with `code-mower doctor
  --preflight` as the first-run setup diagnostic. `doctor --v05` remains the
  versioned equivalent for scripts.
- The README now shows a shortened `doctor --preflight` example so fresh users can
  see the payoff before installing.
- The first-run transcript, architecture overview, cloud data contract, and
  changelog exist as public trust/readiness artifacts.
- Top-level CLI help is first-user focused: `code-mower --help` shows the
  launch-safe commands, while `code-mower --help-all` exposes provider,
  migration, labeler, and operator commands.
- `docs/first-user-install-rehearsal.md` records the executable release-gate
  path: package install, easy-mode smoke, first value report, cloud upload dry
  run, and dogfood dry run. Re-run it against the public tag before widening
  beyond friendly alpha users. Alpha-specific rehearsal notes remain as
  historical transcripts.
- Public CI now runs the package-install first-user rehearsal from the current
  checkout, so every PR proves the same installed-package path that early
  adopters exercise from a public tag.
- Private reference/product repos have proven pinned standalone consumption and
  mirror removal while preserving their own CI/deploy gates.
- Hosted/commercial service implementation remains outside the public OSS repo.

## Required For v1.0

- `README.md` is public-safe, product-oriented, and does not require access to
  private product/reference repositories.
- README and first-run docs show concrete output, not only positioning and
  abstract architecture.
- Public docs explain repo strategy, commercial boundary, GitHub setup, provider
  setup, cloud export privacy, cloud data contract, architecture,
  privacy/threat model, code-structure direction, and easy-mode first run.
- README answers why Code Mower exists beyond manually running a single AI
  reviewer: lane consistency, setup diagnostics, calibration, spend/latency, and
  evidence-gated promotion.
- Standalone CI passes from a clean clone.
- `code-mower init --easy` and `code-mower doctor --preflight` work in a fresh toy
  repo.
- `code-mower --help` makes the first-user path obvious without exposing all
  advanced/operator commands by default.
- `code-mower doctor --preflight` runs provider-declared smoke probes and optional
  cloud-token diagnostics without leaking raw auth/provider output or token
  values into shareable JSON.
- `scripts/smoke_easy_mode.py --json` passes in a fresh virtual environment.
- `scripts/fresh_clone_rehearsal.py --json` passes against the release commit.
- `code-mower migration package-install-rehearsal --package-spec ... --json`
  passes in CI and produces a passing `first_user_readiness` scorecard.
- Secret scans are clean.
- Privacy scans are clean: no personal paths, private repo slugs, raw auth
  output, or likely secrets.
- Live calibration artifacts from proprietary products are either omitted,
  anonymized, or intentionally published by the repository owner.
- Generated package manifest contains no private repo paths or private product
  assumptions.
- Provider matrix identifies which lanes are local, hosted, manual, optional,
  informational, or merge-gating eligible.
- Cloud export/upload docs state that upload is opt-in, dry-run-first, and
  inspectable.
- Issue templates, contributing guide, security policy, and support boundaries
  are ready enough for early OSS users.
- The old personal source repo either points clearly to
  `https://github.com/codemower-ai/code-mower` or is archived.
- The public repo has branch protection, required CI, secret scanning,
  Dependabot, security policy, issue templates, pull request template,
  Discussions, and at least two owner/admin-capable maintainers.
- Public-source and package-index install paths are documented separately from
  private-fork/deploy-key install paths.
- At least one fresh public toy repo and one private GitHub repo complete the
  easy-mode flow without reference-repo assumptions.
- Source-checkout and release-rehearsal commands use `scripts/dev-python` or
  `.venv/bin/python`; docs and gates do not rely on ambient `python`/`python3`
  resolving to a safe interpreter.
- Ruff linting runs in CI with a deliberately small `E`/`F` rule set before
  broader formatting/type-checking decisions.
- Core behavior has direct tests for verdict parsing, calibration/value-report
  math, cloud bundle privacy, and at least one provider-runner stub path.
- CodeMower.com has a published retention policy and user-visible deletion or
  export path before broad cloud-data invitations.

## Alpha Release Gate

Before tagging an early alpha, run these from a clean standalone checkout:

```bash
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e . ruff
.venv/bin/code-mower --version
.venv/bin/python -m ruff check .
.venv/bin/python scripts/smoke_easy_mode.py --code-mower-bin .venv/bin/code-mower --json
.venv/bin/code-mower doctor --preflight --json
.venv/bin/python scripts/fresh_clone_rehearsal.py --repo-url . --ref HEAD --python .venv/bin/python --json
.venv/bin/code-mower migration package-install-rehearsal --package-spec . --python .venv/bin/python --json
```

For alpha releases, keep running this from a fresh clone before tagging. Do not
promote an alpha toward v1.0 unless the generated package can pass the same
path outside the developer's long-lived worktree.

`scripts/dev-python` is the checked-in source checkout interpreter resolver. It
refuses Python older than 3.12, including stale virtualenvs and old system
`python3` shims, so release work cannot accidentally use a broken local
interpreter.

## Recommended Before v1.0

- Publish a short "easy mode" walkthrough using a toy repo.
- Publish a GitHub Release for the current public alpha tag and mark alpha/beta
  tags as prereleases in GitHub.
- Confirm the release workflow builds source/wheel distributions for every
  public alpha.
- Configure PyPI trusted publishing before widening beyond friendly alpha users,
  or document why the project is intentionally staying GitHub-install-only.
- Run [docs/pypi-release.md](pypi-release.md) against TestPyPI before
  switching first-user docs from GitHub-tag install to `pipx install
  code-mower`.
- Run `code-mower migration release-readiness --json` before any TestPyPI or
  production PyPI promotion. It should pass with zero failed checks for package
  version consistency, release workflow gates, trusted publishing docs, and
  package-index rehearsal commands.
- Keep calibration auto-discovery in the release gate. The package-install
  rehearsal should generate a draft corpus from offline PR metadata and
  round-trip it through `calibration value-report`; before widening, also run
  `code-mower calibration auto-discover --repo OWNER/REPO --last-n 20` in at
  least one public toy/real repository.
- Add a short terminal recording, screenshot, or transcript of the first
  `doctor --preflight` run to the README/website.
- Expand [docs/troubleshooting.md](troubleshooting.md) as new setup traps are
  found in early-adopter installs.
- Add a migration note for teams that want to start with informational-only
  lanes.
- Add a short explanation of how local benchmark reports can later be shared with
  the hosted service.
- Decompose the largest extraction-era modules before v1.0 where it materially
  improves contributor onboarding: calibration, doctor, cloud, package, and
  provider runners.
- Add focused unit tests around the large modules instead of relying mostly on
  release-hygiene integration tests.
- Make the public repository the clear source of truth for OSS users. Private
  product repos should consume pinned public releases, not appear to generate or
  overwrite the public package.
- Split CLI docs into first-user commands and operator/internal commands so
  `--help` does not bury the early-adopter path under bridge/labeler verbs.

## Ongoing Public-Repo Duties

- Create the first GitHub release.
- Keep commercial backend code and commercialization plans in a private repo.
- Keep product repos private unless there is a separate product reason to make
  them public.
- Keep private reference-repo names out of public docs unless they are necessary
  examples and the repo owner has intentionally made them public.
- Re-run the public fresh-clone/install rehearsal before every alpha promotion.

## Product Safety Rule

Public release work should happen primarily in the standalone Code Mower repo.
Product repos should only receive pinned wrapper/config updates after standalone
changes prove clean.
