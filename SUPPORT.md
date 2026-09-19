# Support

Code Mower 1.x is supervised-pilot software. The fastest path to useful help is
to share the smallest safe reproduction and command output that does not
contain private content.

## Setup And Usage Help

- Use [GitHub Discussions](https://github.com/codemower-ai/code-mower/discussions)
  for setup questions, provider/lane calibration, and early-adopter feedback.
- Use [GitHub Issues](https://github.com/codemower-ai/code-mower/issues) for
  reproducible bugs or focused feature requests.
- Start with [docs/try-in-10-minutes.md](docs/try-in-10-minutes.md),
  [docs/quickstart.md](docs/quickstart.md), and
  [docs/troubleshooting.md](docs/troubleshooting.md).
- For hosted Slack installation or lifecycle questions, follow
  [docs/slack-setup.md](docs/slack-setup.md). Ordinary hosted setup uses the
  dashboard OAuth flow; `code-mower slack setup` is for the app/deployment
  operator and does not install the hosted app for a workspace.

## What To Include

Helpful public reports include:

- Code Mower version from `code-mower --version`;
- install method and operating system;
- Python version from `python --version`;
- repository host and whether the repo is public or private;
- provider CLIs involved, without auth output;
- the exact `code-mower` command you ran; and
- sanitized `doctor --adoption --repo OWNER/REPO --json` output, with
  `--orchestrator-only` or `--hosted-builders` when that matches the host.

For Slack issues, also include the stage that failed (install, binding,
readiness, command, interaction, rotation, disable, or removal) and the closed
status/remediation name shown by the UI or `code-mower slack doctor`. Replace
workspace, enterprise, channel, user, repository, task, receipt, deployment,
and provider identifiers with synthetic labels. Do not publish the underlying
probe snapshot or host logs.

## What Not To Share Publicly

Do not post:

- API keys, tokens, private keys, or OAuth secrets;
- OAuth callback URLs or query parameters, authorization codes, state values,
  Slack signing secrets, bot/refresh tokens, response URLs, or trigger IDs;
- raw Slack command/interaction payloads, request headers or bodies, command or
  modal text, workspace/channel/user IDs, private bindings, or probe snapshots;
- credentials or credential-like output from provider CLIs;
- raw provider auth output;
- private source code, raw diffs, or raw model transcripts;
- private repository URLs unless intentionally public;
- customer data or proprietary business context; or
- full cloud upload payloads before inspecting them for sensitive content.

## Security Reports

Report security issues privately through the process in
[SECURITY.md](SECURITY.md). If you are unsure whether something is a security
issue, treat it as one and avoid public details until a maintainer responds.
