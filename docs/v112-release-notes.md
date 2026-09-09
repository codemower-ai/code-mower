# Code Mower v1.1.2 Release Notes

Code Mower v1.1.2 hardens the opt-in Jira Cloud tracker for large projects and
Jira sites whose create-metadata issue-type inventory is unavailable. The
default GitHub-only workflow is unchanged.

Install or upgrade the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.1.2
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.1.2
code-mower --version
```

## Large Jira Projects

Enhanced-JQL queue reads now request at most 25 issues per page. The smaller
page stays inside Code Mower's bounded response budget for metadata-rich Jira
projects while preserving the prior queue coverage: 250 issues by default and
1,000 at the hard page limit. Partial queues still cannot authorize dispatch.

## Issue-Type Discovery Compatibility

When Jira returns 404 for the create-metadata issue-type inventory, Code Mower
falls back to Jira's project issue-type endpoint and retains only bounded type
IDs and names. The fallback is intentionally narrow: authentication,
authorization, malformed, oversized, and non-404 failures remain visible.
Required create-field metadata also remains fail-closed rather than being
treated as an empty requirement. This fixes issue #831 in PR #832.

## Verification And Privacy

The change was exercised against a large Jira Cloud project with read-only
credentials, deterministic offline fixtures, the full unit suite, the privacy
scan, and Python 3.12, 3.13, and 3.14 CI. Claude's merge-authority audit passed
with no findings and Gitar completed successfully.

Jira queue data remains bounded metadata. Source, Jira summaries and
descriptions, comments, attachments, raw diffs, prompts, transcripts, raw
provider output, authentication output, local paths, and secrets are excluded
from Board and cloud uploads. GitHub remains authoritative for pull requests,
checks, and merge gates.
