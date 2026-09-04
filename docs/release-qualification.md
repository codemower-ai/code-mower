# Release Qualification

The `code-mower release qualify` command runs local release qualification with a stable adoption-result schema.

## Usage

### Qualifying a Release (Execute Mode)

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output result.json \
  --qualification-context cold_install \
  --execute
```

The `--execute` flag runs all qualification steps including package installation. Without it, the command runs in preview mode (dry-run) which checks environment readiness but skips actual package installation.

### Preview Mode (Dry-run)

Omit `--execute` for preview-only output:

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output result.json \
  --qualification-context cold_install
```

Preview mode emits a `planned` package-install step and produces a non-passing outcome. Use this to validate environment and configuration before running the full qualification.

Runs from current directory by default. Specify `--repo-path` to qualify a different checkout.

## Schema

`code_mower.adoptionResult.v1` includes:

- `schema`: Schema version
- `timestamp_utc`: UTC timestamp
- `release_tag`: Exact tag
- `package_identity`: Package name (code-mower)
- `normalized_version`: Normalized version
- `qualification_context`: Context (cold_install/upgrade/unknown)
- `starting_version`: Starting version for upgrade context, empty otherwise
- `ending_version`: Version after rehearsal (empty in preview mode)
- `provider`, `executor`: Safe identifiers
- `host_class`, `runtime_class`: Environment classification
- `elapsed_seconds`: Total time
- `outcome`: pass/pass_with_warnings/fail
- `steps`: List with id, status, elapsed_seconds, warning_count, owner_action_count

No local paths, secrets, commands, or raw output.

## Validation

- Tag must match `v<major>.<minor>.<patch>[-<stage>.<num>]`
- Package must be exact index spec: `code-mower==1.0.0`
- Tag and spec versions must match
- Provider/executor must be safe identifiers: `^[a-z][a-z0-9_]{0,31}$`
- Context must be: cold_install, upgrade, or unknown
- Upgrade context requires `--starting-version` with valid normalized version
