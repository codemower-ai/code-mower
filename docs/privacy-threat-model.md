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
- Slack OAuth codes and tokens, signing secrets, response URLs, trigger IDs,
  workspace/channel/user identifiers, mappings, and command/modal text;
- benchmark results before the user chooses to share them.

## Trust Boundaries

| Boundary | What can cross it | Default posture |
| --- | --- | --- |
| Local CLI provider | Prompt, diff, selected files, context packs | Explicit lane config and doctor visibility |
| SaaS reviewer GitHub App | Pull request diff and repository context | Manual or informational until calibrated |
| Local model endpoint | Prompt and selected code context | User-controlled endpoint; informational by default |
| GitHub Actions | Generated workflows, labels, comments, artifacts | Least privilege, guarded triggers, no surprise cron |
| Cloud benchmark export | Sanitized report bundle | Opt-in only; inspect bundle before upload |
| Hosted Slack app | OAuth installation and explicitly submitted commands/modals | One private workspace and private unshared channel; exact member/repository/channel bindings |
| OAuth query-scrubbing relay | One-time OAuth query parameters | Dedicated relay, no request logging/tracing, query-free handoff to the application |
| Slack supervisor bridge | Normalized private work request and lifecycle action | Reauthorize at execution; numeric caps and qualified supervisor required |

## Data Minimization Rules

- Send diffs plus bounded context packs, not entire repositories by default.
- Keep raw reviewer outputs local unless the user intentionally commits or
  uploads them.
- Redact auth probe output content. Store shape diagnostics such as return code
  and line count instead of account text.
- Prefer secret file references for local keys and avoid printing secret values.
- Keep provider spend and hosted reviewer triggers explicit.
- Treat generated calibration manifests as shareable only after a privacy scan.
- Keep Slack credentials and routing identifiers in the hosted secret/policy
  stores. Do not put them in CLI arguments, shell history, source, tickets,
  snapshots, Board, cloud exports, or public support reports.
- Do not log Slack request headers or bodies, OAuth queries, command/modal text,
  response URLs, trigger IDs, or the private binding tuple. Retain only the
  minimum normalized receipt and bounded deduplication metadata needed for
  delivery and lifecycle reconciliation.

## Hosted Slack boundary

Slack is optional and does not change the default local Claude + Codex path.
The hosted v1.5.0 app requests only the bot `commands` scope and has no user
scope, message/channel history, posting, file, email, Events API, Socket Mode,
or organization-wide permission. Slack still sees text a user submits to its
slash command or modal and delivers that text plus routing identifiers to Code
Mower. Code Mower maps the authenticated actor, installation, private channel,
and repository alias through server-held policy before accepting an intent.

The OAuth relay is a distinct trust boundary. It receives Slack's short-lived
callback query, removes it from the browser-facing handoff URL, then gives the
application the callback in a query-free request. Operators must disable
platform request logging, tracing, previews, custom-domain analytics, and any
other capture point that could retain that query. OAuth state protects the
browser flow; it is not authorization to run provider work.

Accepted Slack input may become a bounded work request to the configured
supervisor and builder. That provider can receive task text and repository
context under its existing authorization. Every execution is reauthorized
against current bindings, supervisor qualification, and task/campaign caps.
Slack cannot approve provider permissions, change safe mode, grant merge
authority, or prove completion/cancellation. Removing the Slack app stops future
Slack access but does not cancel work already dispatched to a provider.

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
