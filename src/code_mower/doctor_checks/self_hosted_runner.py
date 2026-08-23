"""Self-hosted macOS runner doctor checks."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .models import STATUS_FAIL, STATUS_PASS, STATUS_SKIP, DoctorCheck
from .privacy import auth_probe_output_detail
from .provider_local_cli_commands import (
    local_cli_command,
    resolved_local_cli_command,
)
from .provider_local_cli_probe_config import (
    local_cli_probe_args,
    local_cli_probe_env,
    local_cli_probe_timeout,
)
from .provider_probe import evaluate_json_probe, local_cli_probe_remediation

try:  # pragma: no cover - fallback supports extracted tools shims.
    from code_mower import config as code_mower_config
    from code_mower import init as code_mower_init
    from code_mower.workflow_actionlint import (
        GeneratedWorkflow,
        WorkflowLintError,
        custom_self_hosted_runner_labels,
        run_actionlint_on_workflows,
    )
except ImportError:  # pragma: no cover - direct tools execution fallback.
    from tools import code_mower_config, code_mower_init
    from tools.workflow_actionlint import (
        GeneratedWorkflow,
        WorkflowLintError,
        custom_self_hosted_runner_labels,
        run_actionlint_on_workflows,
    )


REQUIRED_RUNNER_ENV = ("USER", "LOGNAME", "SHELL", "LANG")
RUNNER_LABEL_ENV_NAMES = (
    "CODE_MOWER_RUNNER_LABELS",
    "ACTIONS_RUNNER_LABELS",
    "RUNNER_LABELS",
)
RUNNER_ROOT_ENV_NAMES = ("CODE_MOWER_RUNNER_ROOT", "ACTIONS_RUNNER_ROOT", "RUNNER_ROOT")
PS_LISTENER_RE = re.compile(
    r"^\s*(?P<pid>\d+)\s+"
    r"(?P<start>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+"
    r"(?P<command>.+)$"
)

RunFn = Callable[..., subprocess.CompletedProcess[str]]
WhichFn = Callable[[str], str | None]


@dataclass(frozen=True)
class RunnerListenerProcess:
    pid: int
    start_time: datetime
    command: str


def check_runner_launchagent(
    *,
    home: Path | None = None,
    platform: str = sys.platform,
) -> DoctorCheck:
    if platform != "darwin":
        return DoctorCheck(
            name="runtime.runner_launchagent",
            status=STATUS_SKIP,
            message="runner LaunchAgent check skipped on this platform",
        )

    try:
        home_dir = home or Path.home()
    except RuntimeError as exc:
        return DoctorCheck(
            name="runtime.runner_launchagent",
            status=STATUS_FAIL,
            message="could not resolve home directory for runner LaunchAgent check",
            detail={"error": str(exc)},
            remediation="Set HOME for the runner account, then rerun `code-mower doctor --runner`.",
        )

    launch_agents = home_dir / "Library" / "LaunchAgents"
    plists = sorted(launch_agents.glob("actions.runner.*.plist"))
    if not plists:
        return DoctorCheck(
            name="runtime.runner_launchagent",
            status=STATUS_SKIP,
            message="no GitHub Actions runner LaunchAgent plist found",
            detail={"glob": str(launch_agents / "actions.runner.*.plist")},
        )

    bad: list[str] = []
    unreadable: list[str] = []
    for plist_path in plists:
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            unreadable.append(str(plist_path))
            continue
        if isinstance(payload, dict) and payload.get("SessionCreate") is True:
            bad.append(str(plist_path))
    if bad:
        return DoctorCheck(
            name="runtime.runner_launchagent",
            status=STATUS_FAIL,
            message="runner LaunchAgent has SessionCreate=true",
            detail={"session_create_plists": bad, "unreadable_plists": unreadable},
            remediation=(
                "Remove the `SessionCreate` key from actions.runner.*.plist, "
                "unload and reload the LaunchAgent, then rerun the Claude prompt "
                "smoke from a runner job."
            ),
        )
    if unreadable:
        return DoctorCheck(
            name="runtime.runner_launchagent",
            status=STATUS_FAIL,
            message="could not inspect every runner LaunchAgent plist",
            detail={"unreadable_plists": unreadable},
            remediation="Fix permissions on the unreadable plist(s), then rerun doctor.",
        )
    return DoctorCheck(
        name="runtime.runner_launchagent",
        status=STATUS_PASS,
        message="runner LaunchAgent plists do not set SessionCreate=true",
        detail={"plists": [str(path) for path in plists]},
    )


def check_runner_required_env(
    *,
    environ: Mapping[str, str] | None = None,
) -> DoctorCheck:
    env = os.environ if environ is None else environ
    missing = [name for name in REQUIRED_RUNNER_ENV if not str(env.get(name, "")).strip()]
    if missing:
        return DoctorCheck(
            name="runtime.runner_env",
            status=STATUS_FAIL,
            message="runner service environment is missing required variables",
            detail={"missing": missing, "required": list(REQUIRED_RUNNER_ENV)},
            remediation=(
                "Set USER, LOGNAME, SHELL, and LANG in the runner `.env`, then "
                "fully recycle the Runner.Listener process."
            ),
        )
    return DoctorCheck(
        name="runtime.runner_env",
        status=STATUS_PASS,
        message="runner service environment exposes USER, LOGNAME, SHELL, and LANG",
        detail={"required": list(REQUIRED_RUNNER_ENV)},
    )


def _provider_name(lane: Mapping[str, Any]) -> str:
    return str(lane.get("provider") or "").replace("_", "-")


def _provider_lanes(
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    selected: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for lane_id, lane in lanes:
        if lane.get("driver") != "local_cli":
            continue
        provider = _provider_name(lane)
        if provider in {"codex", "claude"}:
            selected.setdefault(provider, (lane_id, lane))
    return selected


def _missing_cli_probe(command: str, provider: str, lane_id: str) -> DoctorCheck:
    return DoctorCheck(
        name="runtime.runner_cli_auth",
        status=STATUS_FAIL,
        lane=lane_id,
        message=f"{command} was not found for {provider} auth probe",
        detail={"command": command, "provider": provider},
        remediation=f"Install {command}, authenticate it as the runner user, and ensure it is on PATH.",
    )


def _check_codex_auth_probe(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    run: RunFn = subprocess.run,
    timeout_seconds: int = 60,
) -> DoctorCheck:
    resolved_pair = resolved_local_cli_command(lane)
    if resolved_pair is None:
        return _missing_cli_probe(local_cli_command(lane), "codex", lane_id)
    command, resolved = resolved_pair
    child_env, env_detail = local_cli_probe_env(lane)
    with tempfile.TemporaryDirectory(prefix="code-mower-codex-runner-probe-") as raw:
        output_path = Path(raw) / "last-message.txt"
        args = (
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--ephemeral",
            "--output-last-message",
            str(output_path),
            "Reply with exactly: ok",
        )
        try:
            completed = run(
                [resolved, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return DoctorCheck(
                name="runtime.runner_cli_auth",
                status=STATUS_FAIL,
                lane=lane_id,
                message=f"{command} auth prompt probe failed to run: {exc}",
                detail={
                    "command": command,
                    "path": resolved,
                    "args": list(args[:-1]),
                    "timeout_seconds": timeout_seconds,
                    **env_detail,
                },
                remediation="Run `codex login status`, refresh Codex auth if needed, then rerun doctor.",
            )
        output = ""
        try:
            output = output_path.read_text(encoding="utf-8").strip()
        except OSError:
            output = (completed.stdout or completed.stderr or "").strip()
    status = STATUS_PASS if completed.returncode == 0 and output.lower() == "ok" else STATUS_FAIL
    return DoctorCheck(
        name="runtime.runner_cli_auth",
        status=status,
        lane=lane_id,
        message=(
            f"{command} auth prompt probe succeeded"
            if status == STATUS_PASS
            else f"{command} auth prompt probe did not return the expected sentinel"
        ),
        detail={
            "command": command,
            "path": resolved,
            "args": list(args[:-1]),
            "timeout_seconds": timeout_seconds,
            "returncode": completed.returncode,
            "expected_sentinel": "ok",
            "sentinel_matched": output.lower() == "ok",
            **env_detail,
            **auth_probe_output_detail(output or completed.stdout or completed.stderr or ""),
        },
        remediation=(
            None
            if status == STATUS_PASS
            else "Run `codex login status`, refresh Codex auth if needed, then rerun doctor."
        ),
    )


def _check_claude_auth_probe(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    run: RunFn = subprocess.run,
    http_timeout: int,
) -> DoctorCheck:
    resolved_pair = resolved_local_cli_command(lane)
    if resolved_pair is None:
        return _missing_cli_probe(local_cli_command(lane), "claude", lane_id)
    command, resolved = resolved_pair
    probe_args = local_cli_probe_args(lane, command)
    timeout_seconds = local_cli_probe_timeout(lane, http_timeout)
    child_env, env_detail = local_cli_probe_env(lane)
    try:
        completed = run(
            [resolved, *probe_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(
            name="runtime.runner_cli_auth",
            status=STATUS_FAIL,
            lane=lane_id,
            message=f"{command} auth prompt probe failed to run: {exc}",
            detail={
                "command": command,
                "path": resolved,
                "args": list(probe_args),
                "timeout_seconds": timeout_seconds,
                **env_detail,
            },
            remediation=local_cli_probe_remediation(
                command,
                probe_args,
                lane,
                auth_error_detected=True,
            ),
        )
    output = (completed.stdout or completed.stderr or "").strip()
    provider_config = lane.get("provider_config", {})
    if isinstance(provider_config, Mapping) and provider_config.get("doctor_probe_expect_json"):
        status, json_message, json_detail = evaluate_json_probe(
            provider_config,
            output,
            returncode=completed.returncode,
        )
        passed = status == STATUS_PASS
    else:
        json_message = ""
        json_detail = {}
        passed = completed.returncode == 0
    return DoctorCheck(
        name="runtime.runner_cli_auth",
        status=STATUS_PASS if passed else STATUS_FAIL,
        lane=lane_id,
        message=(
            f"{command} {json_message}"
            if passed and json_message
            else f"{command} auth prompt probe succeeded"
            if passed
            else f"{command} auth prompt probe failed"
        ),
        detail={
            "command": command,
            "path": resolved,
            "args": list(probe_args),
            "timeout_seconds": timeout_seconds,
            "returncode": completed.returncode,
            **env_detail,
            **json_detail,
            **auth_probe_output_detail(output),
        },
        remediation=(
            None
            if passed
            else local_cli_probe_remediation(
                command,
                probe_args,
                lane,
                auth_error_detected=bool(json_detail.get("auth_error_detected")),
            )
        ),
    )


def check_runner_cli_auth(
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    run: RunFn = subprocess.run,
    http_timeout: int,
) -> tuple[DoctorCheck, ...]:
    provider_lanes = _provider_lanes(lanes)
    checks: list[DoctorCheck] = []
    for provider in ("codex", "claude"):
        lane_pair = provider_lanes.get(provider)
        if lane_pair is None:
            checks.append(
                DoctorCheck(
                    name="runtime.runner_cli_auth",
                    status=STATUS_SKIP,
                    message=f"no selected local {provider} lane requires a runner auth probe",
                    detail={"provider": provider},
                )
            )
            continue
        lane_id, lane = lane_pair
        if provider == "codex":
            checks.append(_check_codex_auth_probe(lane_id, lane, run=run))
        else:
            checks.append(
                _check_claude_auth_probe(
                    lane_id,
                    lane,
                    run=run,
                    http_timeout=http_timeout,
                )
            )
    return tuple(checks)


def _synthetic_all_lanes_profile(
    config: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    profile_id = "doctor-runner"
    lanes = config.get("lanes", {})
    lane_ids = sorted(str(lane_id) for lane_id in lanes) if isinstance(lanes, Mapping) else []
    profiles = dict(config.get("profiles", {}) if isinstance(config.get("profiles"), Mapping) else {})
    while profile_id in profiles:
        profile_id += "-all"
    profiles[profile_id] = {
        "description": "doctor runner all selected lanes",
        "lanes": lane_ids,
    }
    return {**dict(config), "profiles": profiles}, profile_id


def generated_workflows_from_config(
    *,
    config: Mapping[str, Any],
    profile: str | None,
    config_path: Path,
    source_root: Path,
) -> tuple[GeneratedWorkflow, ...]:
    init_config, init_profile = (
        (config, profile) if profile else _synthetic_all_lanes_profile(config)
    )
    plan = code_mower_init.render_init_plan(
        init_config,
        profile_id=str(init_profile),
        config_path=str(config_path),
    )
    workflows: list[GeneratedWorkflow] = []
    for entry in plan.data["generated_files"]:
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path") or "")
        if not path.startswith(".github/workflows/"):
            continue
        materialized = code_mower_init._materialize_generated_file(
            entry,
            path,
            Path(path),
            source_root=source_root,
        )
        workflows.append(GeneratedWorkflow(path=path, text=materialized.text))
    return tuple(workflows)


def _env_runner_labels(environ: Mapping[str, str]) -> tuple[str, ...]:
    for name in RUNNER_LABEL_ENV_NAMES:
        raw = str(environ.get(name, "")).strip()
        if raw:
            return tuple(label.strip() for label in raw.split(",") if label.strip())
    return ()


def _configured_repo_slug(config: Mapping[str, Any]) -> str:
    repositories = config.get("repositories", [])
    if not isinstance(repositories, list):
        return ""
    for repo in repositories:
        if isinstance(repo, Mapping) and repo.get("slug"):
            return str(repo["slug"])
    return ""


def _github_slug_from_remote(remote: str) -> str:
    normalized = remote.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if "github.com:" in normalized:
        slug = normalized.rsplit("github.com:", 1)[1]
    elif "github.com/" in normalized:
        slug = normalized.rsplit("github.com/", 1)[1]
    else:
        return ""
    parts = [part for part in slug.split("/") if part]
    if len(parts) < 2:
        return ""
    return "/".join(parts[:2])


def _remote_repo_slug(
    repo_root: Path,
    *,
    run: RunFn = subprocess.run,
) -> str:
    try:
        completed = run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return _github_slug_from_remote(completed.stdout.strip())


def _runner_labels_from_github(
    *,
    config: Mapping[str, Any],
    repo_root: Path,
    environ: Mapping[str, str],
    run: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
) -> tuple[str, ...]:
    runner_name = str(environ.get("RUNNER_NAME", "")).strip()
    if not runner_name:
        return ()
    repo_slug = _configured_repo_slug(config) or _remote_repo_slug(repo_root, run=run)
    if not repo_slug or which("gh") is None:
        return ()
    page = 1
    while True:
        try:
            completed = run(
                ["gh", "api", f"repos/{repo_slug}/actions/runners?per_page=100&page={page}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ()
        if completed.returncode != 0:
            return ()
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ()
        runners = payload.get("runners", []) if isinstance(payload, Mapping) else []
        if not isinstance(runners, list):
            return ()
        for runner in runners:
            if not isinstance(runner, Mapping) or str(runner.get("name") or "") != runner_name:
                continue
            labels = runner.get("labels", [])
            if not isinstance(labels, list):
                return ()
            return tuple(
                str(label.get("name") or "")
                for label in labels
                if isinstance(label, Mapping) and str(label.get("name") or "")
            )
        if len(runners) < 100:
            return ()
        page += 1


def _discover_runner_labels(
    *,
    config: Mapping[str, Any],
    repo_root: Path,
    environ: Mapping[str, str],
    run: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
) -> tuple[str, ...]:
    return _env_runner_labels(environ) or _runner_labels_from_github(
        config=config,
        repo_root=repo_root,
        environ=environ,
        run=run,
        which=which,
    )


def check_runner_workflow_labels(
    *,
    config: Mapping[str, Any],
    profile: str | None,
    config_path: Path,
    repo_root: Path,
    source_root: Path,
    environ: Mapping[str, str] | None = None,
    actual_labels: Sequence[str] | None = None,
    run: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
) -> DoctorCheck:
    env = os.environ if environ is None else environ
    try:
        workflows = generated_workflows_from_config(
            config=config,
            profile=profile,
            config_path=config_path,
            source_root=source_root,
        )
    except code_mower_config.ConfigError as exc:
        return DoctorCheck(
            name="runtime.runner_workflow_labels",
            status=STATUS_FAIL,
            message="could not render generated workflows to inspect runs-on labels",
            detail={"error": str(exc)},
            remediation="Fix the Code Mower config, then rerun `code-mower doctor --runner`.",
        )
    expected = custom_self_hosted_runner_labels(workflows)
    if not expected:
        return DoctorCheck(
            name="runtime.runner_workflow_labels",
            status=STATUS_SKIP,
            message="no generated self-hosted workflow runner labels found",
        )
    labels = tuple(actual_labels) if actual_labels is not None else _discover_runner_labels(
        config=config,
        repo_root=repo_root,
        environ=env,
        run=run,
        which=which,
    )
    if not labels:
        return DoctorCheck(
            name="runtime.runner_workflow_labels",
            status=STATUS_FAIL,
            message="could not discover this runner's registered labels",
            detail={"expected_custom_labels": list(expected)},
            remediation=(
                "Run doctor inside a runner job with CODE_MOWER_RUNNER_LABELS set, "
                "or run it on the runner Mac with RUNNER_NAME and `gh api` access "
                "to the repository runner inventory."
            ),
        )
    missing = sorted(set(expected) - set(labels))
    if missing:
        return DoctorCheck(
            name="runtime.runner_workflow_labels",
            status=STATUS_FAIL,
            message="runner labels do not match generated workflow runs-on labels",
            detail={
                "expected_custom_labels": list(expected),
                "actual_labels": sorted(labels),
                "missing_labels": missing,
            },
            remediation=(
                "Add the missing custom label to the self-hosted runner or update "
                "owner_surface.local_audit_runner_label and regenerate workflows."
            ),
        )
    return DoctorCheck(
        name="runtime.runner_workflow_labels",
        status=STATUS_PASS,
        message="runner labels match generated workflow runs-on labels",
        detail={
            "expected_custom_labels": list(expected),
            "actual_labels": sorted(labels),
        },
    )


def check_runner_actionlint_available(
    *,
    actionlint_bin: str = "actionlint",
    which: WhichFn = shutil.which,
) -> DoctorCheck:
    resolved = which(actionlint_bin)
    if not resolved and Path(actionlint_bin).expanduser().is_file():
        resolved = str(Path(actionlint_bin).expanduser().resolve())
    if resolved:
        return DoctorCheck(
            name="runtime.runner_actionlint",
            status=STATUS_PASS,
            message="actionlint executable found",
            detail={"actionlint_bin": resolved},
        )
    return DoctorCheck(
        name="runtime.runner_actionlint",
        status=STATUS_FAIL,
        message="actionlint executable was not found",
        detail={"actionlint_bin": actionlint_bin},
        remediation="Install actionlint, for example `brew install actionlint`, then rerun doctor.",
    )


def check_runner_generated_workflows_actionlint(
    *,
    config: Mapping[str, Any],
    profile: str | None,
    config_path: Path,
    source_root: Path,
    actionlint_bin: str = "actionlint",
    run: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
) -> DoctorCheck:
    try:
        workflows = generated_workflows_from_config(
            config=config,
            profile=profile,
            config_path=config_path,
            source_root=source_root,
        )
        result = run_actionlint_on_workflows(
            workflows,
            actionlint_bin=actionlint_bin,
            run=run,
            which=which,
        )
    except (WorkflowLintError, code_mower_config.ConfigError) as exc:
        return DoctorCheck(
            name="runtime.runner_generated_workflows",
            status=STATUS_FAIL,
            message="generated workflows did not pass actionlint",
            detail={"error": str(exc)},
            remediation=(
                "Fix the workflow template or config, run "
                "`code-mower init --easy --apply`, and rerun actionlint."
            ),
        )
    if result.workflow_count == 0:
        return DoctorCheck(
            name="runtime.runner_generated_workflows",
            status=STATUS_SKIP,
            message="no generated workflow files found to lint",
        )
    return DoctorCheck(
        name="runtime.runner_generated_workflows",
        status=STATUS_PASS,
        message=f"actionlint passed for {result.workflow_count} generated workflows",
        detail=result.as_dict(),
    )


def parse_runner_listener_processes(ps_output: str) -> tuple[RunnerListenerProcess, ...]:
    processes: list[RunnerListenerProcess] = []
    for line in ps_output.splitlines():
        if "Runner.Listener" not in line:
            continue
        match = PS_LISTENER_RE.match(line)
        if not match:
            continue
        try:
            start_time = datetime.strptime(match.group("start"), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
        processes.append(
            RunnerListenerProcess(
                pid=int(match.group("pid")),
                start_time=start_time,
                command=match.group("command"),
            )
        )
    return tuple(processes)


def _ps_runner_listener_processes(
    *,
    run: RunFn = subprocess.run,
) -> tuple[RunnerListenerProcess, ...]:
    try:
        completed = run(
            ["ps", "-axo", "pid=,lstart=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if completed.returncode != 0:
        return ()
    return parse_runner_listener_processes(completed.stdout)


def _runner_root_from_command(command: str) -> Path | None:
    marker = "/bin/Runner.Listener"
    marker_index = command.find(marker)
    if marker_index == -1:
        return None
    path_start = command[:marker_index].rfind(" /")
    if path_start == -1:
        path_start = command.find("/")
    else:
        path_start += 1
    if path_start == -1:
        return None
    path_end = marker_index + len(marker)
    return Path(command[path_start:path_end]).parent.parent


def _runner_roots(
    *,
    environ: Mapping[str, str],
    processes: Sequence[RunnerListenerProcess],
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for name in RUNNER_ROOT_ENV_NAMES:
        raw = str(environ.get(name, "")).strip()
        if raw:
            roots.append(Path(raw).expanduser())
    for process in processes:
        root = _runner_root_from_command(process.command)
        if root is not None:
            roots.append(root)
    return tuple(dict.fromkeys(roots))


def _env_file_for_process(
    process: RunnerListenerProcess,
    env_files_by_root: Mapping[Path, Path],
    *,
    listener_count: int,
) -> Path | None:
    root = _runner_root_from_command(process.command)
    if root is not None:
        env_path = env_files_by_root.get(root)
        if env_path is not None:
            return env_path
        for candidate_root, candidate_env in env_files_by_root.items():
            try:
                if root.resolve() == candidate_root.resolve():
                    return candidate_env
            except OSError:
                continue
        return None
    if listener_count == 1 and len(env_files_by_root) == 1:
        return next(iter(env_files_by_root.values()))
    return None


def check_runner_listener_env_freshness(
    *,
    environ: Mapping[str, str] | None = None,
    processes: Sequence[RunnerListenerProcess] | None = None,
    run: RunFn = subprocess.run,
) -> DoctorCheck:
    env = os.environ if environ is None else environ
    listener_processes = tuple(processes) if processes is not None else _ps_runner_listener_processes(run=run)
    roots = _runner_roots(environ=env, processes=listener_processes)
    env_files_by_root = {root: root / ".env" for root in roots if (root / ".env").is_file()}
    env_files = tuple(env_files_by_root.values())
    if not env_files:
        return DoctorCheck(
            name="runtime.runner_listener_env",
            status=STATUS_SKIP,
            message="no runner .env file found to compare with Runner.Listener start time",
            detail={"runner_roots": [str(root) for root in roots]},
        )
    if not listener_processes:
        return DoctorCheck(
            name="runtime.runner_listener_env",
            status=STATUS_FAIL,
            message="runner .env exists but no Runner.Listener process was found",
            detail={"env_files": [str(path) for path in env_files]},
            remediation="Fully start the GitHub Actions runner listener, then rerun doctor.",
        )
    env_mtimes = {path: datetime.fromtimestamp(path.stat().st_mtime) for path in env_files}
    process_env_files = {
        process: env_path
        for process in listener_processes
        if (
            env_path := _env_file_for_process(
                process,
                env_files_by_root,
                listener_count=len(listener_processes),
            )
        )
        is not None
    }
    if not process_env_files:
        return DoctorCheck(
            name="runtime.runner_listener_env",
            status=STATUS_SKIP,
            message="no Runner.Listener process could be associated with a runner .env file",
            detail={
                "env_files": [str(path) for path in env_files],
                "listener_start_times": {
                    str(process.pid): process.start_time.isoformat()
                    for process in listener_processes
                },
            },
        )
    stale = [
        process
        for process, env_path in process_env_files.items()
        if process.start_time < env_mtimes[env_path]
    ]
    if stale:
        return DoctorCheck(
            name="runtime.runner_listener_env",
            status=STATUS_FAIL,
            message="Runner.Listener started before the runner .env was last edited",
            detail={
                "env_files": [str(path) for path in env_files],
                "env_mtimes": {
                    str(path): env_mtime.isoformat() for path, env_mtime in env_mtimes.items()
                },
                "listener_env_files": {
                    str(process.pid): str(env_path)
                    for process, env_path in process_env_files.items()
                },
                "stale_listener_pids": [process.pid for process in stale],
                "listener_start_times": {
                    str(process.pid): process.start_time.isoformat()
                    for process in listener_processes
                },
            },
            remediation=(
                "Fully recycle Runner.Listener and verify the old PID exited; "
                "`svc.sh stop/start` alone may leave stale process environment."
            ),
        )
    return DoctorCheck(
        name="runtime.runner_listener_env",
        status=STATUS_PASS,
        message="Runner.Listener processes started after their runner .env mtimes",
        detail={
            "env_files": [str(path) for path in env_files],
            "env_mtimes": {
                str(path): env_mtime.isoformat() for path, env_mtime in env_mtimes.items()
            },
            "listener_env_files": {
                str(process.pid): str(env_path)
                for process, env_path in process_env_files.items()
            },
            "listener_start_times": {
                str(process.pid): process.start_time.isoformat()
                for process in listener_processes
            },
        },
    )


def check_self_hosted_runner(
    *,
    config: Mapping[str, Any],
    profile: str | None,
    config_path: Path,
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
    repo_root: Path,
    provider_templates_root: Path,
    http_timeout: int,
    actionlint_bin: str = "actionlint",
) -> tuple[DoctorCheck, ...]:
    return (
        check_runner_launchagent(),
        check_runner_required_env(),
        *check_runner_cli_auth(lanes, http_timeout=http_timeout),
        check_runner_workflow_labels(
            config=config,
            profile=profile,
            config_path=config_path,
            repo_root=repo_root,
            source_root=provider_templates_root,
        ),
        check_runner_actionlint_available(actionlint_bin=actionlint_bin),
        check_runner_generated_workflows_actionlint(
            config=config,
            profile=profile,
            config_path=config_path,
            source_root=provider_templates_root,
            actionlint_bin=actionlint_bin,
        ),
        check_runner_listener_env_freshness(),
    )
