# Privacy And Threat Model

Code Mower's job is to route code review work through multiple agents while
making each trust boundary explicit. The safe default is local setup and
inspectable artifacts; hosted review and cloud reporting are opt-in.

## Assets

Protect these by default:

- repository source code and diffs;
- pull request titles, bodies, comments, labels, and commit metadata;
- local checkout paths and machine/user names;
- GitHub tokens, provider API keys, deploy keys, and CLI session state;
- reviewer raw stdout/stderr, prompts, and context-pack material;
- benchmark results before the user chooses to share them.

## Trust Boundaries

| Boundary | What can cross it | Default posture |
| --- | --- | --- |
| Local CLI provider | Prompt, diff, selected files, context packs | Explicit lane config and doctor visibility |
| SaaS reviewer GitHub App | Pull request diff and repository context | Manual or informational until calibrated |
| Local model endpoint | Prompt and selected code context | User-controlled endpoint; informational by default |
| GitHub Actions | Generated workflows, labels, comments, artifacts | Least privilege, guarded triggers, no surprise cron |
| Cloud benchmark export | Sanitized report bundle | Opt-in only; inspect bundle before upload |

## Data Minimization Rules

- Send diffs plus bounded context packs, not entire repositories by default.
- Keep raw reviewer outputs local unless the user intentionally commits or
  uploads them.
- Redact auth probe output content. Store shape diagnostics such as return code
  and line count instead of account text.
- Prefer secret file references for local keys and avoid printing secret values.
- Keep provider spend and hosted reviewer triggers explicit.
- Treat generated calibration manifests as shareable only after a privacy scan.

## Public Repository Hygiene

The public OSS repo should not contain private reference-repo names, personal
paths, raw provider outputs, private account identifiers, or live calibration
artifacts from proprietary products. Use anonymized summaries in docs and
generic examples such as `owner/repo`.

## Cloud Benchmarking

The hosted benchmarking service is a commercial surface. The OSS core should
produce local reports and an inspectable export bundle. Upload should require an
explicit command, clear destination, and a chance to review the bundle contents.

Future cloud uploads should support:

- anonymous or organization-scoped submission modes;
- source-free metric summaries;
- optional redacted finding text;
- explicit retention policy;
- user-controlled deletion/export; and
- clear separation between public aggregate benchmarks and private product
  reports.

## Release Gate

Before release, run the privacy scan and inspect any changed calibration
artifacts. A release should fail if it contains personal paths, private repo
slugs, raw auth output, or likely secrets.
