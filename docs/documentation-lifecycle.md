# Documentation lifecycle

`docs/docs-manifest.yml` classifies every Markdown document under `docs/`.
The classification prevents a current guide, a supporting explanation, and a
historical release record from silently becoming competing sources of truth.

Use these statuses:

- **canonical** — the maintained owner of one user-facing subject. Canonical
  documents require a unique `subject`.
- **supporting** — maintained detail that links back to canonical guidance and
  does not redefine its procedure.
- **frozen** — immutable release or qualification evidence. Its SHA-256 digest
  is recorded in the manifest.
- **archived** — immutable historical material outside the maintained journey.
  Its SHA-256 digest is also recorded.

Add every new Markdown file to the manifest in the same change. After changing
canonical ownership, regenerate the index and validate the inventory:

```bash
python -m code_mower.docs_lifecycle --write-index
python -m code_mower.docs_lifecycle
```

Changing a frozen or archived document requires moving its status back to a
maintained lifecycle through a reviewed decision. Do not refresh an immutable
digest merely to make a validation failure pass. Release tags remain the final
authority for bytes already published.

The generated [documentation index](README.md) is committed so GitHub and
source distributions remain readable without a documentation service. Edit
canonical ownership in the manifest rather than editing the table directly.
