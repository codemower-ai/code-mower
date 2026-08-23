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
gh auth status
codex login status
codex exec --skip-git-repo-check --sandbox read-only --ask-for-approval never --ephemeral "Reply with exactly: ok"
claude auth status
claude -p "Reply with exactly: ok" --output-format json
```

`claude auth status` is not enough by itself. The prompt smoke is the useful
signal because login-keychain or inherited-env failures often appear only when
Claude makes a real non-interactive request.

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
