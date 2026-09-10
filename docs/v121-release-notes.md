# Code Mower v1.2.1 Release Notes

Code Mower v1.2.1 is a focused reliability release for maintained Claude
release qualification. It preserves the v1.2 supervised-pilot posture and all
existing provider, campaign, gate, and cloud contracts.

## Claude qualification timing

The maintained Claude adapter now tells Claude to report each step duration as
a non-negative integer number of seconds, represent measured setup or
serialization outside named checks as an explicit `overhead` step, and set the
top-level duration to the exact arithmetic sum of the emitted step durations.

The change addresses a failure mode where an otherwise valid qualification
could report an independently measured total that differed from the rounded
step sum. It does not rewrite provider-authored evidence or relax the closed
adoption-result schema. A mismatch still fails closed, and Code Mower still
performs one paid provider invocation with no automatic retry.

Focused regression coverage proves that an inconsistent result is rejected
after exactly one invocation and that exact integer arithmetic is accepted.
A live maintained-adapter canary against `v1.2.0` passed on its first attempt:
Claude emitted step durations of 3, 12, and 0 seconds and a matching 15-second
total. The exact PR head also passed Claude merge-authority review, Codex
informational review, Gitar, and the Python 3.12 through 3.14 package matrix.

## Install or upgrade

With `uv`:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.2.1
code-mower --version
```

With `pipx`:

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.2.1
code-mower --version
```

Expected version output: `code-mower 1.2.1`.

## Privacy

The privacy boundary is unchanged. Campaign adapters parse provider output
transiently and persist only validated `code_mower.adoptionResult.v1`
metadata. Code Mower does not upload source, Jira summaries or descriptions,
issue bodies, comments, attachments, raw diffs, prompts, transcripts, raw
provider output, authentication output, local paths, or secrets.
