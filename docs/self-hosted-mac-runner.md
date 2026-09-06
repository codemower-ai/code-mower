# Self-Hosted Mac Runner

This guide is for the macOS GitHub Actions runner that executes Code Mower
local CLI audit lanes such as Codex and Claude and, in the build loop, local
builder lanes such as Codex and Claude Code.

## Install

Create the runner from the repository settings page:

1. Open Settings, Actions, Runners, New self-hosted runner.
2. Choose macOS and the CPU architecture for the machine.
3. Run the GitHub-provided `config.sh` commands from the macOS user account
   that owns `gh`, `codex`, and `claude` logins.
4. Add the custom runner label configured by the generated workflow. The local
   audit workflow uses `owner_surface.local_audit_runner_label`, default
   `code-mower-audit`. The build-loop lane runner uses
   `owner_surface.lane_runner_labels`, default `self-hosted`, `macOS`,
   `code-mower-lane`.

The generated local audit workflow must match that label:

```yaml
runs-on: [self-hosted, macOS, code-mower-audit]
```

After changing `owner_surface.local_audit_runner_label`, regenerate workflows
with `code-mower init --easy --apply` and run `code-mower doctor --runner`.
After changing `owner_surface.lane_runner_labels`, regenerate workflows with
`code-mower init --builders codex,claude,cursor --apply` and run
`code-mower doctor --runner`.

## LaunchAgent

Run `./run.sh` first. Install the runner as a service only after local smoke
checks pass from the same account.

If `svc.sh install` writes `SessionCreate=true` into
`~/Library/LaunchAgents/actions.runner.*.plist`, remove it. That launchd key
starts the runner in a new security session and can prevent Claude Code from
reading the login keychain.

Check the plist:

```bash
plutil -p ~/Library/LaunchAgents/actions.runner.*.plist | grep SessionCreate || true
```

Remove the key from each affected plist:

```bash
for plist in ~/Library/LaunchAgents/actions.runner.*.plist; do
  /usr/libexec/PlistBuddy -c "Delete :SessionCreate" "$plist" 2>/dev/null || true
done
```

## Environment

Service runners should have these variables in the runner `.env` file:

```bash
USER=runner-user
LOGNAME=runner-user
SHELL=/bin/zsh
LANG=en_US.UTF-8
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

Use the actual macOS login for `USER` and `LOGNAME`. If `codex`, `claude`,
`gh`, or `python3` live outside the default paths, include their directories in
`.env` or in `CODE_MOWER_LOCAL_AUDIT_PATH` in the generated workflow.

## Restart After Env Edits

Editing `.env` is not enough. Verify that the old listener exits and a new
listener starts after the edit.

```bash
old_pids="$(pgrep -f 'Runner.Listener' || true)"
printf 'old Runner.Listener pids: %s\n' "${old_pids:-none}"

./svc.sh stop || true
while pgrep -f 'Runner.Listener' >/dev/null; do
  sleep 1
done

./svc.sh start
new_pids="$(pgrep -f 'Runner.Listener' || true)"
printf 'new Runner.Listener pids: %s\n' "${new_pids:-none}"
first_pid="$(printf '%s\n' "$new_pids" | head -n 1)"
test -n "$first_pid" && ps -p "$first_pid" -o pid=,lstart=,command=
```

If `svc.sh stop/start` does not replace the listener, unload and reload the
LaunchAgent:

```bash
plist="$(ls ~/Library/LaunchAgents/actions.runner.*.plist | head -n 1)"
launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
while pgrep -f 'Runner.Listener' >/dev/null; do
  sleep 1
done
launchctl bootstrap "gui/$(id -u)" "$plist"
```

Then run:

```bash
code-mower doctor --runner
```

## Auth Checks

Verify auth from a runner job or from the same service user environment:

```bash
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
codex login status >/dev/null 2>&1 && echo "codex auth ok" || { echo "codex auth NOT ready"; false; }
codex exec --skip-git-repo-check --sandbox read-only "Reply with exactly: ok"
claude auth status >/dev/null 2>&1 && echo "claude auth ok" || { echo "claude auth NOT ready"; false; }
claude -p "Reply with exactly: ok" --output-format json
```

For API-key runner auth, store `OPENAI_API_KEY` in the runner's secret manager
or private `.env`, then load it into the setup shell and run:

```bash
printf '%s\n' "$OPENAI_API_KEY" | codex login --with-api-key
codex login status >/dev/null 2>&1 && echo "codex auth ok" || { echo "codex auth NOT ready"; false; }
codex exec --skip-git-repo-check --sandbox read-only "Reply with exactly: ok"
```

Do not paste raw provider auth/status output into issues, pull requests, chats,
or logs. Use quiet probes like these or `code-mower doctor --runner` so account
and credential-adjacent details stay local.

`claude auth status` is not enough by itself. The prompt smoke is the useful
signal because login-keychain or inherited-env failures often appear only when
Claude makes a real non-interactive request.

## Builder Delivery And Recovery

`tools/lanes/run_mac_lane.sh` decides whether a unit of work landed from a
validated GitHub state transition, not from the provider exit code. It
snapshots the target issue/PR before the provider runs, snapshots it again
afterwards, and passes both to `code-mower lane-delivery classify`. A build or
fix round only passes when a new pull request appears or the lane's PR head
advances. When the honest answer is that nothing needed to change, the provider
writes `.code-mower/lane-outcome.json` in the working copy:

```json
{"outcome": "no_change", "summary": "one line"}
```

`owner_action` is the other accepted value. Both need a `summary` that is a
non-empty one-line string: it is the only thing that tells the owner why the
unit closed without a change, so a declaration missing one is discarded and the
run counts as undelivered.

The summary is validated, never repaired. Leading and trailing whitespace is
stripped — a line written with a trailing newline is still one line — and what
remains must be a single line of at most 280 characters. A summary that carries
an embedded line break, a carriage return, any other control character, or more
characters than that is discarded whole rather than cut down to its first line
or first 280 characters: keeping part of it would hand the owner a truncated
half of their only explanation while still counting the run as delivered. A
discarded declaration is named in the undelivered note. The runner — never the
provider —
posts the resulting comment and applies `needs-owner`, so the provider never
needs credentials of its own. Runs without a validated delivery exit `3` and
leave a note on the unit. `code-mower lane-delivery scan-prompt` refuses to
start a provider whose assembled prompt would send it looking for
authentication material.

A declaration is what a unit may pass on *instead of* a pull request, never
alongside one. The runner takes the after snapshot and reads the transition —
`code-mower lane-delivery transition`, the same comparison `classify` makes —
before it writes anything to the target, and brokers the declaration only when
it observed no new pull request and no advanced head. A provider that both
declared and pushed has delivered: the push is the outcome, the declaration is
dropped and named in the run log, and the owner is not left with an
owner-blocked pull request sitting next to a comment saying nothing changed. A
transition the runner cannot resolve is not an observed `none` either — an
incomplete after snapshot, or a `code-mower` too old to answer, withholds the
declaration and leaves the unit open. When the runner does broker one it reads
the target once more afterwards, so classification checks the comment and the
`needs-owner` label against GitHub rather than against the runner's belief that
its own edits landed.

Brokering is ordered so the block never arrives without its explanation. The
comment goes first, and `needs-owner` is applied only after GitHub returns the
comment it created; if the comment does not land, the declaration is voided,
no label is applied, and the undelivered note names it. The reverse order would
leave the owner a blocked unit with nothing saying why, since classification
rejects a declaration with no runner comment behind it and no later round clears
a label it did not apply.

Hitting the `--max-minutes` cap does not exempt a run from the contract. A
timed-out provider that pushed nothing is an unfinished unit and still exits
`3`; the cap alone only reports success when the classification passed.

The cap is a clock, not a verdict on delivery. When the supervisor stops a run —
the wall-clock cap, output overflow, or interruption — the exit code is the
supervisor's own, so classification ignores it and goes by the observed
transition alone: a PR or head advance the provider pushed before it was stopped
is still a delivery, and a stopped run that left no transition is still
undelivered. A run the supervisor stopped may not declare a bounded outcome,
because a half-written `lane-outcome.json` is exactly what a killed provider
leaves behind. A provider that chose its own nonzero exit is a failed unit
whatever the target looks like.

The runner reports the same way. An undelivered run that the supervisor stopped
exits `3`, never the supervisor's own `124`, `125`, or `130`: those are not the
provider's verdict, and returning one would report a cap as a provider failure
and bury the classification the caller acts on. Only a provider that ended
itself has its exit code passed through, so an auth failure or a crash still
reaches the caller intact.

Snapshots fail closed. Each GitHub lookup behind a snapshot is retried, and a
lookup that still fails marks the snapshot incomplete rather than recording an
empty value — an empty `pr_number` or head would otherwise be indistinguishable
from "no PR yet" and would read as a delivery the target never made. An
incomplete snapshot before the run refuses the unit with exit `2`; an incomplete
one afterwards classifies as `target_snapshot_unavailable` and exits `3`.

Both snapshots must name the same target. A transition is the difference between
two readings of one unit; across two units the same subtraction reads one
target's open PR against another's absent one as `pr_opened`. `classify` exits
`2` on a `--before` and `--after` that disagree on kind or number, and records no
outcome — there is no single unit to classify, so there is none to report as
undelivered either.

Providers run under `code-mower lane-delivery supervise`, which starts them in
their own process group and terminates plus reaps that whole group on timeout,
interruption, and output overflow. Cap provider output with `LANE_MAX_LOG_BYTES`
(default 32 MiB); the supervisor exits `125` on overflow, and the runner turns
that into `3` or `0` from the classification. Install the `code-mower` CLI on
the runner's `PATH` — without it the runner falls back to the older
direct-child timeout, which can leave inert provider transports behind.

The provider's own exit ends the run, open output pipe or not. A background
descendant that inherited stdout keeps the pipe from ever reaching EOF, so the
supervisor drains what is already in flight, then terminates and reaps the
group. Waiting for EOF instead would burn the whole lane timeout and then report
a timeout — rejecting the delivery — for a provider that had already finished.

### Which `lane-delivery` the runner uses

Resolution is explicit, so the contract never runs against whichever
`code-mower` happens to be first on `PATH`:

1. `CODE_MOWER_LANE_DELIVERY_CMD`, when the runner owner pins one.
2. this source checkout, when the runner ships beside `src/code_mower`.
3. an installed `code-mower` that actually implements `lane-delivery`.

An installed CLI that predates the command leaves the contract inactive for that
run and says so on stderr, rather than failing every unit.

Like every other command override in the runner, the pin is **one executable
path or name, not a command line**. Paths on a Mac routinely contain spaces, and
splitting an environment string into argv truncates the executable at the first
one. To pin a multi-argument invocation, ship an executable wrapper and pin the
wrapper:

```bash
cat > /usr/local/bin/lane-delivery <<'SH'
#!/usr/bin/env bash
exec /opt/code-mower/bin/python3 -m code_mower.lane_delivery "$@"
SH
chmod 755 /usr/local/bin/lane-delivery
export CODE_MOWER_LANE_DELIVERY_CMD=/usr/local/bin/lane-delivery
```

A pin that does not resolve to an executable stops the run with exit `2` instead
of quietly disabling the contract.

Each run writes one metadata-only delivery outcome next to the run log for
Board and productivity reporting: provider exit, delivery transition, handoff,
elapsed time, and intervention count. No prompts, transcripts, output, paths, or
secrets are recorded.

### Recovering a PR owned by another lane

Single-writer enforcement is unchanged: a lane writes only branches carrying
its own prefixes. To hand a stuck PR to a different lane, the orchestrator must
say so explicitly and pin the head it inspected:

```bash
tools/lanes/run_mac_lane.sh --lane claude --repo OWNER/REPO \
  --target pr:750 \
  --handoff-source-lane codex \
  --handoff-expected-head <40-char sha>
```

The runner validates the handoff, refuses it if the expected head is stale or
the destination lane is not the one running, records it in the pre-push guard
config, and posts an audit comment on the PR. Without those flags, a foreign
head branch stays a hard refusal — there is no implicit cross-lane takeover.

A handoff can only hand over a branch the named source lane actually owns. The
runner looks the source lane's branch prefixes up in its own identity config,
never from the caller, and refuses the handoff if the PR head branch does not
carry one of them, or if the named source lane has no configured prefixes at
all. So `--handoff-source-lane codex` recovers a `codex/` branch and nothing
else: not another builder's branch, and not a bot's.

The pinned head is enforced again at push time, not only when the handoff is
validated. The pre-push guard compares the sha the remote advertises for the
handed-over branch against the handoff's expected head, so a source lane that
kept writing after the handoff was issued makes the push fail instead of being
overwritten — including by `--force-with-lease`, whose lease is taken against a
freshly fetched ref and would otherwise permit exactly that. A recovery run may
still push more than once: the guard also accepts a remote sitting at a head the
same run already wrote. A refused push means the handoff is stale; re-issue it
against the current head.

## Keychain And Signing Notes

Use a regular logged-in macOS account for the runner. Do not run local audit
lanes from a root LaunchDaemon. Keep provider OAuth and keychain items owned by
the same account that runs `Runner.Listener`.

If Gatekeeper blocks `codex`, `claude`, `gh`, or helper binaries after an
update, approve the binary from System Settings or reinstall it through the
package manager used on the runner. Keep Developer ID prompts out of unattended
audit jobs by smoke-testing every updated CLI before trusting the runner again.

## Power Settings

Audit jobs can run for many minutes. On a MacBook runner, prevent idle sleep on
battery and power adapter:

```bash
sudo pmset -a sleep 0 disksleep 0
sudo pmset -b displaysleep 10
pmset -g custom
```

Prefer leaving the runner plugged in. If the machine must run on battery, make
sure macOS low power or managed device policy does not override the `sleep 0`
setting.

## Shared Token Budget

The generated local audit workflow uses `DISPATCH_TOKEN` for trailer comments
that must trigger labeler workflows. This should be a human-owned fine-grained
PAT or GitHub App token with the documented repository scopes and an expiry
recorded in `DISPATCH_TOKEN_EXPIRES_AT`.

Set `CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED=true` only after the runner is
registered, labeled, online, and authenticated. Until then the generated local
audit workflow skips matching events instead of leaving jobs queued for a
missing self-hosted runner.

Treat the token as a shared rate-limit budget:

- Use one runner lane job per provider, not unbounded parallel jobs.
- Keep stale retry loops bounded by the generated concurrency group.
- Avoid manual reruns across many PRs while a provider outage is producing
  UNKNOWN verdicts.
- Rotate the token before its expiry date and restart the listener after `.env`
  or secret delivery changes.

## Doctor

Run the strict runner preset after install, after `.env` edits, after workflow
regeneration, and after CLI auth changes:

```bash
code-mower doctor --runner --json
```

The runner preset checks:

- LaunchAgent plists do not set `SessionCreate=true`.
- `USER`, `LOGNAME`, `SHELL`, and `LANG` are present.
- Codex and Claude local CLI lanes can complete non-interactive auth probes.
- Registered runner labels match generated workflow `runs-on` labels.
- `actionlint` is installed and generated workflows lint clean.
- `Runner.Listener` started after the runner `.env` file was last edited.
