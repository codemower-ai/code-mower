# Package Runtime Lens

Use this lens when a PR changes packaging, bootstrap, doctor, provider catalog, CLI dispatch, or extracted-package imports.

Focus on:

- Repo execution and extracted package execution both work.
- Runtime choices are explicit: Python version, CLI command, environment variables, token requirements, and optional spend boundaries.
- Package manifests include every file needed at runtime, including templates, lane configs, adapters, and prompt lenses.
- Doctor checks reveal missing local CLIs or configuration instead of allowing silent fallback to random system tooling.

Block if the reference repo passes but the generated package would import, dispatch, or locate templates incorrectly.
