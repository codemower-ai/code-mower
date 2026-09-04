# Release Qualification

The `code-mower release qualify` command provides local release qualification with a stable adoption-result schema.

## Usage

### Dry Run (Default)

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output adoption-result.json
```

### Execute Qualification

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output adoption-result.json \
  --execute
```

## Schema

The `code_mower.adoptionResult.v1` schema includes:

- `schema`: Schema version identifier
- `timestamp_utc`: Qualification timestamp
- `release_tag`: Exact release tag
- `package_identity`: Sanitized package name
- `normalized_version`: Version from tag
- `qualification_context`: `cold_install`, `upgrade`, or `unknown`
- `starting_version`: Version before qualification (operator context)
- `ending_version`: Version after qualification (isolated rehearsal)
- `provider`: Provider identity (safe identifier)
- `executor`: Executor identity (safe identifier)
- `host_class`: Host classification (`local`, `ci`, `github_actions`)
- `runtime_class`: Python runtime
- `elapsed_seconds`: Total time
- `outcome`: `pass`, `pass_with_warnings`, or `fail`
- `step_count`: Number of steps executed
- `warning_count`: Warning count
- `owner_action_count`: Owner action count

Privacy-safe: no local paths, secrets, or raw command output.

## Validation

Tag validation is strict and anchored:
- `v1.0.0` ✓
- `v1.0.0-alpha.1` → `1.0.0a1` ✓
- `v1.0.0-beta.2` → `1.0.0b2` ✓
- `v1.0.0-rc.1` → `1.0.0rc1` ✓
- `1.0.0` ✗ (missing v prefix)

Provider and executor must be safe identifiers: `^[a-z][a-z0-9_]{0,31}$`
