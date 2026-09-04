# Release Qualification

The `code-mower release qualify` command provides local, read-only release qualification with a stable adoption-result schema.

## Usage

### Basic Dry Run (Default)

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

### With Repository Path

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output adoption-result.json \
  --repo-path /path/to/repo
```

## Options

- `--release-tag`: Exact release tag (required, e.g., `v1.0.0`)
- `--package-spec`: Package specification (required, e.g., `code-mower==1.0.0`)
- `--output`: Output path for adoption result JSON (required)
- `--repo-path`: Optional repository path for lanes status checks
- `--execute`: Execute qualification (default is dry-run)
- `--provider`: Provider identity for result metadata (default: `local_cli`)
- `--executor`: Executor identity for result metadata (default: `unknown`)
- `--timeout`: Timeout in seconds for individual steps (default: 180)
- `--json`: Output result in JSON format

## Adoption Result Schema

The command emits `code_mower.adoptionResult.v1` JSON with the following structure:

```json
{
  "schema_version": "code_mower.adoptionResult.v1",
  "timestamp_utc": "2024-01-01T00:00:00Z",
  "release_tag": "v1.0.0",
  "package_spec": "code-mower==1.0.0",
  "normalized_version": "1.0.0",
  "install_mode": "cold_install",
  "starting_version": "",
  "ending_version": "1.0.0",
  "provider": "local_cli",
  "executor": "unknown",
  "host_class": "local",
  "runtime_class": "python_3.12",
  "elapsed_seconds": 10.5,
  "outcome": "pass",
  "steps": [
    {
      "step": "package_install",
      "status": "completed",
      "elapsed_seconds": 5.0
    }
  ],
  "warnings": [],
  "owner_actions": []
}
```

### Schema Fields

- `schema_version`: Schema version identifier
- `timestamp_utc`: Qualification timestamp in UTC
- `release_tag`: Exact release tag
- `package_spec`: Package specification
- `normalized_version`: Normalized version extracted from tag
- `install_mode`: `cold_install` or `upgrade`
- `starting_version`: Version before qualification (empty for cold install)
- `ending_version`: Version after qualification
- `provider`: Provider identity
- `executor`: Executor identity
- `host_class`: Coarse host classification (`local`, `ci`, `github_actions`)
- `runtime_class`: Python runtime version
- `elapsed_seconds`: Total elapsed time
- `outcome`: Final outcome (`pass`, `pass_with_warnings`, `fail`)
- `steps`: List of qualification steps with status and elapsed time
- `warnings`: List of warnings encountered
- `owner_actions`: List of owner actions required

## Validation

The command validates that the release tag and package spec versions agree before execution:

- `v1.0.0` matches `code-mower==1.0.0` ✓
- `v1.0.0-alpha.1` matches `code-mower==1.0.0a1` ✓
- `v1.0.0-beta.2` matches `code-mower==1.0.0b2` ✓
- `v1.0.0-rc.1` matches `code-mower==1.0.0rc1` ✓
- `v1.0.0` does NOT match `code-mower==1.0.1` ✗

## Dry Run Behavior

By default, the command runs in dry-run mode:

- No package installation
- No GitHub mutations
- No provider dispatch
- No cloud uploads
- Doctor and diagnostic checks still run
- Result schema is still emitted

Use `--execute` to perform actual installation and qualification.

## Privacy and Security

The adoption result schema is metadata-only and never contains:

- Local checkout paths
- Captured raw command output
- Secrets or tokens
- Sensitive environment variables

All outputs are safe for upload and sharing.

## Integration with Existing Commands

The qualification reuses existing Code Mower components:

- Package install rehearsal for installation
- Doctor checks for runtime validation
- Lanes status for repository diagnostics
- Board diagnostics for visibility

## Exit Codes

- `0`: Qualification passed or passed with warnings
- `1`: Qualification failed or error occurred

## Examples

### Qualify a Stable Release

```bash
code-mower release qualify \
  --release-tag v1.0.4 \
  --package-spec code-mower==1.0.4 \
  --output qualification-result.json \
  --execute
```

### Qualify a Release Candidate

```bash
code-mower release qualify \
  --release-tag v1.1.0-rc.1 \
  --package-spec code-mower==1.1.0rc1 \
  --output rc-qualification.json \
  --execute
```

### Dry Run with Repository

```bash
code-mower release qualify \
  --release-tag v1.0.4 \
  --package-spec code-mower==1.0.4 \
  --repo-path ~/projects/my-repo \
  --output dry-run-result.json
```
