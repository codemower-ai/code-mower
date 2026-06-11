# Code Mower Public Release Checklist

Use this checklist before changing the standalone `code-mower` repository from
private to public.

## Required Before Public Visibility

- Apache-2.0 `LICENSE` and `NOTICE` are present and correct.
- `README.md` is public-safe, product-oriented, and does not require access to
  private product/reference repositories.
- Public docs explain repo strategy, commercial boundary, GitHub setup, provider
  setup, cloud export privacy, and easy-mode first run.
- Standalone CI passes from a clean clone.
- `code-mower init --easy` and `code-mower doctor --easy` work in a fresh toy
  repo.
- `scripts/smoke_easy_mode.py --json` passes in a fresh virtual environment.
- `scripts/fresh_clone_rehearsal.py --json` passes against the release commit.
- Secret scans are clean.
- Generated package manifest contains no private repo paths or private product
  assumptions.
- Provider matrix identifies which lanes are local, hosted, manual, optional,
  informational, or merge-gating eligible.
- Cloud export docs state that upload is opt-in and inspectable.
- Issue templates, contributing guide, security policy, and support boundaries
  are ready enough for early OSS users.
- Initial release tag/version policy is chosen.

## Recommended Before Public Visibility

- Publish a short "easy mode" walkthrough using a toy repo.
- Add a troubleshooting section for Python, GitHub auth, provider CLIs, and
  private repository permissions.
- Add a migration note for teams that want to start with informational-only
  lanes.
- Add a short explanation of how local benchmark reports can later be shared with
  the hosted service.

## After Public Visibility

- Validate clone, install, init, doctor, and smoke test from a fresh machine.
- Create the first GitHub release.
- Keep commercial backend code and commercialization plans in a private repo.
- Keep product repos private unless there is a separate product reason to make
  them public.

## Product Safety Rule

Public release work should happen primarily in the standalone Code Mower repo.
Product repos should only receive pinned wrapper/config updates after standalone
changes prove clean.
