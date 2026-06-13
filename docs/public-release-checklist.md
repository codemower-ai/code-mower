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
- Private reference/product repos have proven pinned standalone consumption and
  mirror removal while preserving their own CI/deploy gates.
- Hosted/commercial service implementation remains outside the public OSS repo.

## Required For v1.0

- `README.md` is public-safe, product-oriented, and does not require access to
  private product/reference repositories.
- Public docs explain repo strategy, commercial boundary, GitHub setup, provider
  setup, cloud export privacy, privacy/threat model, and easy-mode first run.
- Standalone CI passes from a clean clone.
- `code-mower init --easy` and `code-mower doctor --easy` work in a fresh toy
  repo.
- `code-mower doctor --easy --probe-runtime` runs provider-declared smoke probes
  without leaking raw auth/provider output into shareable JSON.
- `scripts/smoke_easy_mode.py --json` passes in a fresh virtual environment.
- `scripts/fresh_clone_rehearsal.py --json` passes against the release commit.
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
- Public-source and package-index install paths are documented separately from
  private-fork/deploy-key install paths.
- At least one fresh public toy repo and one private GitHub repo complete the
  easy-mode flow without reference-repo assumptions.

## Alpha Release Gate

Before tagging an early alpha, run these from a clean standalone checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/code-mower --version
.venv/bin/python scripts/smoke_easy_mode.py --code-mower-bin .venv/bin/code-mower --json
.venv/bin/code-mower doctor --easy --probe-runtime --json
.venv/bin/python scripts/fresh_clone_rehearsal.py --repo-url . --ref HEAD --python python3.12 --json
```

For alpha releases, keep running this from a fresh clone before tagging. Do not
promote an alpha toward v1.0 unless the generated package can pass the same
path outside the developer's long-lived worktree.

## Recommended Before v1.0

- Publish a short "easy mode" walkthrough using a toy repo.
- Add a troubleshooting section for Python, GitHub auth, provider CLIs, and
  private repository permissions.
- Add a migration note for teams that want to start with informational-only
  lanes.
- Add a short explanation of how local benchmark reports can later be shared with
  the hosted service.

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
