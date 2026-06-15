# Support

Code Mower is alpha software. The fastest path to useful help is to share the
smallest safe reproduction and the command output that does not contain private
content.

## Setup And Usage Help

- Use [GitHub Discussions](https://github.com/codemower-ai/code-mower/discussions)
  for setup questions, provider/lane calibration, and early-adopter feedback.
- Use [GitHub Issues](https://github.com/codemower-ai/code-mower/issues) for
  reproducible bugs or focused feature requests.
- Start with [docs/try-in-10-minutes.md](docs/try-in-10-minutes.md),
  [docs/quickstart.md](docs/quickstart.md), and
  [docs/troubleshooting.md](docs/troubleshooting.md).

## What To Include

Helpful public reports include:

- Code Mower version from `code-mower --version`;
- install method and operating system;
- Python version from `python --version`;
- repository host and whether the repo is public or private;
- provider CLIs involved, without auth output;
- the exact `code-mower` command you ran; and
- sanitized `doctor --preflight --json` or `doctor --v05 --json` output.

## What Not To Share Publicly

Do not post:

- API keys, tokens, private keys, or OAuth secrets;
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
