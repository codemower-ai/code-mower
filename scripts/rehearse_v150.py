"""Disposable wheel-only offline lifecycle rehearsal; never live Slack evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from release_candidate import NAMES, verify


def rehearse(dist, sha, work):
    manifest = verify(dist, sha)
    work.mkdir(parents=True, exist_ok=False)
    # Only infrastructure variables enter the disposable product processes.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    env.update(HOME=str(work / "home"), PIP_CONFIG_FILE=os.devnull, PYTHONNOUSERSITE="1")
    (work / "home").mkdir()

    def command(*args, expected=0):
        result = subprocess.run([str(a) for a in args], cwd=work, env=env,
                                text=True, capture_output=True, timeout=240)
        if result.returncode != expected:
            raise RuntimeError(f"rehearsal command failed (expected {expected}, got {result.returncode}): {args[0]}")
        return result.stdout

    def pip(python, *args):
        return command(python, "-m", "pip", "--isolated", *args)

    def installed(python, code, *args, expected=0):
        # No checkout imports (-I), sockets, service processes or provider CLI.
        guard = """
import sys
def guard(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise RuntimeError('offline rehearsal forbids network and child processes')
sys.addaudithook(guard)
"""
        return command(python, "-I", "-c", guard + code, *args, expected=expected)

    def cli(python, *args, expected=0):
        return installed(python, "from code_mower.cli import main\nraise SystemExit(main(sys.argv[1:]))", *args, expected=expected)

    wheel = dist / NAMES[0]
    checks = []
    fresh = work / "fresh"
    command(sys.executable, "-m", "venv", fresh)
    py = fresh / "bin/python"
    pip(py, "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple/", wheel)
    pip(py, "check")
    assert cli(py, "--version").strip() == "code-mower 1.5.0"
    installed(py, """
import importlib.util
import code_mower
from pathlib import Path
assert Path(code_mower.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert all(importlib.util.find_spec(n) is None for n in ('slack_sdk', 'slack_bolt', 'mcp', 'keyring', 'graphify'))
""")
    preview = cli(py, "init", "--easy")
    assert "slack" not in preview.lower()
    assert list((work / "home").iterdir()) == []
    checks.append("fresh_default_slack_free_no_network_or_service")
    output = work / "slack-app.json"
    cli(py, "slack", "setup", "--manifest", output, "--yes")
    assert output.stat().st_mode & 0o777 == 0o600
    hosted_manifest = json.loads(output.read_text())
    assert hosted_manifest["oauth_config"]["scopes"]["bot"] == ["commands"]
    cli(py, "slack", "setup", "--manifest", output, "--yes", expected=1)
    checks.append("explicit_slack_setup_exclusive_private_manifest")
    report = json.loads(cli(py, "slack", "doctor", "--json", expected=1))
    assert report["ready"] is False and report["dispatch_authorized"] is False
    # Construct a synthetic all-green observation using the installed contract.
    snapshot = work / "offline.json"
    installed(py, """
import json, time
from pathlib import Path
from code_mower import slack_readiness as r
now = time.time()
value = {'schema': r.PROBE_SCHEMA, 'nonce': '0'*64, 'observed_at': now, 'expires_at': now+100,
    'components': {k:v[0] for k,v in r.COMPONENTS.items()},
    'supervisor_product': 'codex', 'supervisor_contract': r.SUPERVISOR_SCHEMA,
    'caps': {'task_acu':1,'campaign_acu':2,'reserved_acu':0,'task_limit':2,'reserved_tasks':0,
    'runtime_calls':4,'runtime_seconds':120,'review_rounds':1,'review_budget_usd':2,
    'clarification_answers':1,'fix_requests':0,'recovery_creates':0}}
Path(sys.argv[1]).write_text(json.dumps(value))
""", snapshot)
    report = json.loads(cli(py, "slack", "doctor", "--snapshot", snapshot, "--json", expected=1))
    assert report["basis"] == "offline" and not report["ready"] and not report["dispatch_authorized"]
    checks.append("all_green_offline_snapshot_cannot_claim_live_readiness")
    # Simulate disabled control-plane observations only; no live admin mutation.
    observation = json.loads(snapshot.read_text())
    observation["components"].update(ingress="disabled", bridge="disabled", installation="disabled")
    snapshot.write_text(json.dumps(observation))
    report = json.loads(cli(py, "slack", "doctor", "--snapshot", snapshot, "--json", expected=1))
    assert not report["ready"] and not report["dispatch_authorized"]
    output.unlink()  # The only local opt-in artifact; there is no Slack service.
    checks.append("offline_disabled_snapshot_and_local_manifest_removal")

    # Preserve synthetic operator evidence outside site-packages, byte for byte.
    state = work / "operator-state"
    state.mkdir()
    for name, content in {"config.json": '{"enabled":false}\n',
                          "reservation.json": '{"reserved_acu":2,"refunded":false}\n',
                          "receipt.json": '{"synthetic":true,"reconciled":true}\n'}.items():
        (state / name).write_text(content)

    def state_hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.iterdir()}

    before = state_hashes()
    upgrade = work / "upgrade"
    command(sys.executable, "-m", "venv", upgrade)
    py = upgrade / "bin/python"
    old = work / "rollback-artifact"
    old.mkdir()
    pip(py, "download", "--no-cache-dir", "--index-url", "https://pypi.org/simple/",
        "--only-binary=:all:", "--no-deps", "--dest", old, "code-mower==1.4.2")
    old_wheel, = old.glob("code_mower-1.4.2-*.whl")
    old_digest = hashlib.sha256(old_wheel.read_bytes()).hexdigest()
    assert old_digest == "f8bf24dd8a982ed5ab28302e837cd5d2aeece6d984ed1c44fcb4688c3fb7a522"
    pip(py, "install", "--no-cache-dir", "--index-url", "https://pypi.org/simple/", old_wheel)
    assert cli(py, "--version").strip() == "code-mower 1.4.2"
    pip(py, "install", "--no-index", "--no-deps", "--upgrade", wheel)
    assert cli(py, "--version").strip() == "code-mower 1.5.0"
    assert before == state_hashes()
    checks.append("upgrade_1_4_2_to_exact_wheel_preserves_synthetic_state")
    pip(py, "install", "--no-index", "--no-deps", "--force-reinstall", old_wheel)
    assert cli(py, "--version").strip() == "code-mower 1.4.2"
    assert before == state_hashes()
    checks.append("disposable_rollback_to_digest_verified_1_4_2_preserves_state")
    pip(py, "uninstall", "--yes", "code-mower")
    installed(py, "import importlib.util\nassert importlib.util.find_spec('code_mower') is None")
    assert before == state_hashes()
    pip(fresh / "bin/python", "uninstall", "--yes", "code-mower")
    installed(fresh / "bin/python", "import importlib.util\nassert importlib.util.find_spec('code_mower') is None")
    assert before == state_hashes()
    checks.append("uninstall_preserves_synthetic_state")
    result = {"schema": "code_mower.v150_rehearsal.v1", "source_sha": sha,
              "artifact_sha256": manifest["artifacts"][NAMES[0]], "checks": checks,
              "rollback_wheel_sha256": old_digest, "synthetic_state_preserved": True,
              "live_slack_readiness": "not_run", "live_disable_uninstall": "not_run",
              "paid_canaries": "not_run", "status": "pass"}
    (work / "rehearsal.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rehearse(args.dist.resolve(), args.source_sha, args.work_dir.resolve()), sort_keys=True))


if __name__ == "__main__":
    main()
