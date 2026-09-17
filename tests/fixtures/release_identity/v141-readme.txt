# Code Mower

Code Mower adds a supervised operating layer around AI coding agents. It helps
teams give one builder ownership of a change, obtain independent reviews on the
current pull-request head, recover stalled work, and measure which builder and
reviewer combinations are useful on their own codebase.

The current release is supervised-pilot, bring-your-own-agent-loop software.
It is not a drop-in unattended merge gate. Humans still own credentials,
repository policy, reviewer promotion, and exceptional decisions.

The current source candidate is `v1.4.1`, with target install spec
`code-mower==1.4.1`. Publication and installed-package qualification are pending
[#915](https://github.com/codemower-ai/code-mower/issues/915). The published
`v1.4.0` artifacts remain unchanged. Install commands below target v1.4.1 after
publication; candidate rehearsals use the exact verified artifact.

Documentation on `main` follows the source on `main`. After v1.4.1 publication, start with the
[`v1.4.1` guide](https://github.com/codemower-ai/code-mower/blob/v1.4.1/docs/try-in-10-minutes.md).

## What Code Mower Adds

