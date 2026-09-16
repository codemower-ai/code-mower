#!/usr/bin/env python3
"""Managed local Board service lifecycle.

A Board started from a shell dies with that shell. This module owns the
provider-neutral surface for a *persistent* local Board: render a reviewable
service definition, install it, inspect it, restart it, and remove it. macOS is
the only provider implemented today (launchd); every other platform reports
`unsupported_platform` rather than pretending a transient process is a service.

Two rules shape every payload here:

* **Fail closed.** Stale arguments, a definition owned by another repository, a
  port held by another supervisor, an ambiguous repository selection, or a
  failed rollback all stop the operation with a diagnostic instead of replacing
  a binding that was never proven to be ours.
* **Local paths stay local.** Exact repository paths are the thing a binding is
  validated against, so the comparison happens here and only its *result*
  leaves: payloads carry `[local path hidden]` and a pass/fail, never the path,
  unless a local operator explicitly asks with `--show-local-paths`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__ as CODE_MOWER_VERSION
from . import lane_status


BOARD_SERVICE_SCHEMA = "code_mower.boardService.v1"
BOARD_SERVICE_DEFINITION_SCHEMA = "code_mower.boardServiceDefinition.v1"
BOARD_SERVICE_STATUS_SCHEMA = "code_mower.boardServiceStatus.v1"
BOARD_SERVICE_BINDING_SCHEMA = "code_mower.boardServiceBinding.v1"

SERVICE_LABEL_PREFIX = "ai.codemower.board"
DEFAULT_HOST = "127.0.0.1"
LAUNCHD_PROVIDER = "launchd"

# The closed outcome vocabulary. A caller may branch on these; new states are a
# contract change, not an implementation detail.
SERVICE_STATUSES = (
    "installed",
    "restarted",
    "removed",
    "unchanged",
    "ok",
    "not_installed",
    "already_installed",
    "invalid_request",
    "ambiguous_repository",
    "ownership_mismatch",
    "stale_arguments",
    "port_conflict",
    "external_supervisor",
    "apply_failed",
    "rollback_failed",
    "remove_incomplete",
    "delayed_health_failed",
    "unsupported_platform",
    "provider_unavailable",
)

# Delayed-health vocabulary (#976): a managed service is not accepted the moment
# launchd returns. The apply settles for `settle_seconds`, then the binding is
# refreshed every `refresh_seconds` until `timeout_seconds` elapses. Every
# refreshed attempt re-validates the whole binding, so a service that comes back
# on stale arguments fails the gate instead of passing on a single early probe.
DEFAULT_SETTLE_SECONDS = 5.0
DEFAULT_REFRESH_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 60.0
DELAYED_HEALTH_STATES = ("pass", "fail", "skipped")

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
ORIGIN_SLUG_RE = re.compile(
    r"^(?:git@[^:]+:|(?:https?|ssh|git)://(?:[^@/]+@)?[^/]+/)(?P<slug>.+?)(?:\.git)?$"
)
_LABEL_PORT_RE = re.compile(rf"^{re.escape(SERVICE_LABEL_PREFIX)}\.(\d+)$")
_LAUNCHCTL_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)
_LAUNCHCTL_STATE_RE = re.compile(r"^\s*state\s*=\s*(\S+)", re.MULTILINE)
_LAUNCHCTL_ARGUMENTS_OPEN_RE = re.compile(r"^\s*arguments\s*=\s*\{\s*$")
_LAUNCHCTL_BLOCK_CLOSE_RE = re.compile(r"^\s*\}\s*$")
_PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")

# The binding checks that make up the serving gate. Named here so a runbook can
# assert the gate still covers everything it claims to cover.
BINDING_CHECK_IDS = (
    "service.label",
    "service.definition",
    "service.keepalive",
    "service.loaded",
    "process.arguments",
    "process.repo_path",
    "process.supervisor",
    "binding.port",
    "binding.repo",
    "binding.installed_version",
    "binding.serving_version",
)

Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class ServiceRequestError(ValueError):
    """Raised for a request that can never be valid, such as a bad repo slug."""


def installed_version() -> str:
    """The installed distribution version, falling back to the imported one."""

    try:
        return metadata.version("code-mower")
    except metadata.PackageNotFoundError:
        return CODE_MOWER_VERSION


def default_service_root() -> Path:
    """Where managed service definitions live.

    Overridable so a test, a sandbox, or a second account never has to write
    into the real per-user LaunchAgents directory.
    """

    override = os.environ.get("CODE_MOWER_BOARD_SERVICE_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def label_for_port(port: int) -> str:
    return f"{SERVICE_LABEL_PREFIX}.{int(port)}"


def port_from_label(label: str) -> int | None:
    match = _LABEL_PORT_RE.match(str(label or "").strip())
    return int(match.group(1)) if match else None


def is_managed_label(label: str) -> bool:
    return port_from_label(label) is not None


def default_program() -> tuple[str, ...]:
    """The executable and leading arguments that start a Board.

    The console script is preferred because its command line is what local
    inventory already recognizes; a source checkout without the script installed
    falls back to the module entry point, which inventory also recognizes.
    """

    console = shutil.which("code-mower")
    if console:
        return (str(Path(console).resolve()),)
    return (sys.executable, "-m", "code_mower.cli")


@dataclass(frozen=True)
class ServiceSpec:
    """Everything the rendered definition is a pure function of."""

    repo: str
    repo_path: Path
    port: int
    host: str
    label: str
    arguments: tuple[str, ...]
    log_path: Path
    error_log_path: Path
    path_env: str
    keepalive: bool = True

    @property
    def executable(self) -> str:
        return self.arguments[0] if self.arguments else ""


@dataclass(frozen=True)
class ManagedService:
    """An installed definition, plus whatever runtime state the provider saw."""

    label: str
    definition_path: Path
    arguments: tuple[str, ...]
    repo: str
    repo_path: str
    host: str
    port: int | None
    keepalive: bool
    digest: str
    loaded: bool = False
    pid: int | None = None
    readable: bool = True
    message: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_path(value: str) -> bool:
    return value.startswith("/") or value.startswith("~")


def redact_path(value: object, *, show_local_paths: bool) -> str:
    text = _text(value)
    if not text:
        return ""
    return text if show_local_paths else lane_status.LOCAL_PATH_REDACTION


def redact_arguments(arguments: Sequence[str], *, show_local_paths: bool) -> list[str]:
    """Keep argument *shape* public while hiding every local path inside it.

    Reviewers need to see that the service serves `--repo owner/repo --port
    5332`; nobody outside this machine needs the checkout it serves from.
    """

    if show_local_paths:
        return [str(item) for item in arguments]
    return [
        lane_status.LOCAL_PATH_REDACTION if _looks_like_path(str(item)) else str(item)
        for item in arguments
    ]


def definition_digest(text: str) -> str:
    """A stable name for a definition that reveals none of its contents."""

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_spec(
    *,
    repo: str,
    repo_path: str | Path,
    port: int,
    host: str = DEFAULT_HOST,
    record_events: bool = True,
    program: Sequence[str] | None = None,
    extra_arguments: Sequence[str] = (),
    log_dir: str | Path | None = None,
    path_env: str | None = None,
) -> ServiceSpec:
    """Validate a request and freeze it into a deterministic spec."""

    slug = _text(repo)
    if not REPO_SLUG_RE.match(slug):
        raise ServiceRequestError("--repo must be OWNER/REPO")
    try:
        port_value = int(port)
    except (TypeError, ValueError) as exc:
        raise ServiceRequestError("--port must be an integer") from exc
    if not 1 <= port_value <= 65535:
        raise ServiceRequestError("--port must be between 1 and 65535")
    host_value = _text(host) or DEFAULT_HOST
    canonical = Path(repo_path).expanduser().resolve()
    if not canonical.is_dir():
        raise ServiceRequestError("--repo-path must be an existing directory")
    program_args = tuple(str(item) for item in (program or default_program()))
    if not program_args:
        raise ServiceRequestError("service program is empty")
    log_root = Path(log_dir).expanduser().resolve() if log_dir else canonical / ".code-mower" / "board" / "logs"
    label = label_for_port(port_value)
    arguments = (
        *program_args,
        "board",
        "serve",
        "--repo",
        slug,
        "--repo-path",
        str(canonical),
        "--host",
        host_value,
        "--port",
        str(port_value),
        *(("--record-events",) if record_events else ()),
        *(str(item) for item in extra_arguments),
    )
    return ServiceSpec(
        repo=slug,
        repo_path=canonical,
        port=port_value,
        host=host_value,
        label=label,
        arguments=arguments,
        log_path=log_root / f"board-{port_value}.log",
        error_log_path=log_root / f"board-{port_value}.err.log",
        path_env=path_env if path_env is not None else os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
    )


def spec_from_service(service: ManagedService) -> ServiceSpec:
    """Read an installed definition back as a spec, without re-validating it.

    A stale service may name a path that no longer exists; inspecting it must
    still work, so nothing here resolves or requires the filesystem.
    """

    return ServiceSpec(
        repo=service.repo,
        repo_path=Path(service.repo_path),
        port=int(service.port or 0),
        host=service.host or DEFAULT_HOST,
        label=service.label,
        arguments=tuple(service.arguments),
        log_path=Path(""),
        error_log_path=Path(""),
        path_env="",
        keepalive=service.keepalive,
    )


def launchd_definition(spec: ServiceSpec) -> dict[str, Any]:
    """The launchd job description, as data, so it can be reviewed and diffed."""

    return {
        "Label": spec.label,
        "ProgramArguments": list(spec.arguments),
        "WorkingDirectory": str(spec.repo_path),
        "RunAtLoad": True,
        "KeepAlive": bool(spec.keepalive),
        "ProcessType": "Background",
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.error_log_path),
        "EnvironmentVariables": {
            "PATH": spec.path_env,
            "CODE_MOWER_BOARD_SERVICE_LABEL": spec.label,
        },
    }


def render_definition(spec: ServiceSpec) -> str:
    """Render the exact definition that would be applied.

    Deterministic: the same spec always renders byte-identical text, which is
    what makes `definition_digest` usable as an idempotence claim.
    """

    return plistlib.dumps(launchd_definition(spec), sort_keys=True).decode("utf-8")


def log_directories(spec: ServiceSpec) -> tuple[Path, ...]:
    """The parent directories launchd must be able to open the log files in."""

    parents = []
    for path in (spec.log_path, spec.error_log_path):
        parent = path.parent
        if not str(path) or str(parent) in {"", "."}:
            continue
        if parent not in parents:
            parents.append(parent)
    return tuple(parents)


def ensure_log_directories(spec: ServiceSpec) -> str:
    """Create the log parents, returning "" on success or a reason on failure.

    The definition directs both output streams into `.code-mower/board/logs`,
    which does not exist in a fresh checkout. launchd will not start a job whose
    `StandardOutPath` cannot be opened, so the directories are created *before*
    the job is loaded -- and, on an apply that replaces a running service,
    before the running one is booted out, so a filesystem failure is reported
    without having disrupted a Board that was working.
    """

    for parent in log_directories(spec):
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"could not create the service log directory ({exc.__class__.__name__})"
    return ""


def binding_from_arguments(arguments: Sequence[str]) -> dict[str, Any]:
    """Recover the Board binding a command line encodes.

    Used for both an installed definition and a live process command line, so a
    service and the process it supervises are compared on the same terms.
    """

    values = [str(item) for item in arguments]
    binding: dict[str, Any] = {"repo": "", "repo_path": "", "host": "", "port": None}
    flags = {"--repo": "repo", "--repo-path": "repo_path", "--host": "host", "--port": "port"}
    index = 0
    while index < len(values):
        current = values[index]
        key = None
        value = None
        if current in flags:
            key = flags[current]
            value = values[index + 1] if index + 1 < len(values) else ""
            index += 2
        elif "=" in current and current.split("=", 1)[0] in flags:
            name, _, value = current.partition("=")
            key = flags[name]
            index += 1
        else:
            index += 1
            continue
        if key == "port":
            try:
                binding["port"] = int(value)
            except (TypeError, ValueError):
                binding["port"] = None
        else:
            binding[key] = _text(value)
    return binding


def _service_from_definition(path: Path, data: Mapping[str, Any], text: str) -> ManagedService:
    arguments = tuple(str(item) for item in (data.get("ProgramArguments") or []))
    binding = binding_from_arguments(arguments)
    keepalive_value = data.get("KeepAlive")
    keepalive = bool(keepalive_value) if not isinstance(keepalive_value, Mapping) else True
    return ManagedService(
        label=_text(data.get("Label")),
        definition_path=path,
        arguments=arguments,
        repo=str(binding["repo"]),
        repo_path=str(binding["repo_path"]) or _text(data.get("WorkingDirectory")),
        host=str(binding["host"]) or DEFAULT_HOST,
        port=binding["port"],
        keepalive=keepalive,
        digest=definition_digest(text),
    )


def parse_launchctl_arguments(stdout: str) -> tuple[str, ...] | None:
    """The `arguments = { ... }` block of a `launchctl print` dump.

    launchd prints one argument per line, so an argument containing spaces
    arrives whole. None distinguishes "launchd did not report an argument
    list" from "the job was exec'd with no arguments".
    """

    lines = (stdout or "").splitlines()
    for index, line in enumerate(lines):
        if not _LAUNCHCTL_ARGUMENTS_OPEN_RE.match(line):
            continue
        arguments: list[str] = []
        for entry in lines[index + 1 :]:
            if _LAUNCHCTL_BLOCK_CLOSE_RE.match(entry):
                return tuple(arguments)
            arguments.append(entry.strip())
        # An unterminated block is a dump we do not understand; say so rather
        # than validating against half an argument list.
        return None
    return None


class LaunchdProvider:
    """macOS provider. Every mutation goes through `launchctl`."""

    name = LAUNCHD_PROVIDER

    def __init__(
        self,
        *,
        command_runner: lane_status.CommandRunner = lane_status.run_command,
        root: str | Path | None = None,
        uid: int | None = None,
        platform: str = sys.platform,
    ) -> None:
        self.command_runner = command_runner
        self.root = Path(root).expanduser() if root else default_service_root()
        self.uid = os.getuid() if uid is None else int(uid)
        self.platform = platform

    # -- capability ---------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        """Whether this host can actually be asked to manage a service.

        The capability is probed through the same command runner every mutation
        uses, so a host that cannot run `launchctl` reports unavailable instead
        of failing halfway through an apply.
        """

        if not self.platform.startswith("darwin"):
            return False, "managed Board services require macOS launchd on this platform"
        completed = self._run(["launchctl", "version"])
        if completed is None or completed.returncode != 0:
            return False, "launchctl could not be run on this host"
        return True, ""

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def definition_path(self, label: str) -> Path:
        return self.root / f"{label}.plist"

    # -- reads --------------------------------------------------------------
    def read_service(self, label: str) -> ManagedService | None:
        path = self.definition_path(label)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            return ManagedService(
                label=label,
                definition_path=path,
                arguments=(),
                repo="",
                repo_path="",
                host=DEFAULT_HOST,
                port=port_from_label(label),
                keepalive=False,
                digest="",
                readable=False,
                message=f"service definition could not be read ({exc.__class__.__name__})",
            )
        try:
            data = plistlib.loads(text.encode("utf-8"))
        except Exception:  # noqa: BLE001 - any malformed plist is the same fact
            return ManagedService(
                label=label,
                definition_path=path,
                arguments=(),
                repo="",
                repo_path="",
                host=DEFAULT_HOST,
                port=port_from_label(label),
                keepalive=False,
                digest=definition_digest(text),
                readable=False,
                message="service definition is not a readable plist",
            )
        if not isinstance(data, Mapping):
            return ManagedService(
                label=label,
                definition_path=path,
                arguments=(),
                repo="",
                repo_path="",
                host=DEFAULT_HOST,
                port=port_from_label(label),
                keepalive=False,
                digest=definition_digest(text),
                readable=False,
                message="service definition is not a plist dictionary",
            )
        service = _service_from_definition(path, data, text)
        loaded, pid = self.runtime(label)
        return dataclasses.replace(service, loaded=loaded, pid=pid)

    def list_services(self) -> list[ManagedService]:
        try:
            entries = sorted(self.root.glob(f"{SERVICE_LABEL_PREFIX}.*.plist"))
        except OSError:
            return []
        services = []
        for entry in entries:
            label = entry.name[: -len(".plist")]
            if not is_managed_label(label):
                continue
            service = self.read_service(label)
            if service is not None:
                services.append(service)
        return services

    def _print(self, label: str) -> str | None:
        """The launchd job dump, or None when launchd does not hold the job."""

        completed = self._run(["launchctl", "print", f"{self.domain}/{label}"])
        if completed is None or completed.returncode != 0:
            return None
        return completed.stdout or ""

    def runtime(self, label: str) -> tuple[bool, int | None]:
        """Whether launchd holds the job, and the pid it is supervising.

        A zero exit from `launchctl print` is the load state; the pid is only
        present while the job is actually running, so a loaded-but-crashed job
        reports `(True, None)` rather than being called healthy.
        """

        stdout = self._print(label)
        if stdout is None:
            return False, None
        pid_match = _LAUNCHCTL_PID_RE.search(stdout)
        state_match = _LAUNCHCTL_STATE_RE.search(stdout)
        pid = int(pid_match.group(1)) if pid_match else None
        if pid is None and state_match and "running" not in state_match.group(1).lower():
            return True, None
        return True, pid

    def job_arguments(self, label: str) -> tuple[str, ...] | None:
        """The argument vector launchd actually exec'd, with its boundaries.

        `ps -o command=` renders an argv as one space-joined string with no
        quoting, so a checkout or executable path containing a space cannot be
        split back into the original arguments -- `shlex.split()` would turn one
        argument into two and fail a healthy service. launchd is the platform's
        own record of the job it forked and prints one argument per line, so the
        boundaries survive. None means launchd did not report them, which the
        gate treats as a failure rather than guessing.
        """

        stdout = self._print(label)
        if stdout is None:
            return None
        return parse_launchctl_arguments(stdout)

    # -- writes -------------------------------------------------------------
    def write_definition(self, label: str, text: str) -> Path:
        """Replace the definition file atomically, never in place."""

        path = self.definition_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.staged")
        staged.write_text(text, encoding="utf-8")
        os.replace(staged, path)
        return path

    def delete_definition(self, label: str) -> bool:
        """Whether the definition is gone afterwards, not whether we unlinked it."""

        path = self.definition_path(label)
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def definition_exists(self, label: str) -> bool:
        try:
            return self.definition_path(label).exists()
        except OSError:
            return True

    def bootstrap(self, label: str) -> tuple[bool, str]:
        completed = self._run(["launchctl", "bootstrap", self.domain, str(self.definition_path(label))])
        return self._result(completed, "bootstrap")

    def bootout(self, label: str) -> tuple[bool, str]:
        completed = self._run(["launchctl", "bootout", f"{self.domain}/{label}"])
        if completed is not None and completed.returncode != 0:
            combined = f"{completed.stdout or ''}{completed.stderr or ''}".lower()
            # Not loaded is the state bootout was asked to reach.
            if "no such process" in combined or "could not find" in combined:
                return True, ""
        return self._result(completed, "bootout")

    def kickstart(self, label: str) -> tuple[bool, str]:
        completed = self._run(["launchctl", "kickstart", "-k", f"{self.domain}/{label}"])
        return self._result(completed, "kickstart")

    # -- plumbing -----------------------------------------------------------
    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.command_runner(args)
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _result(completed: subprocess.CompletedProcess[str] | None, action: str) -> tuple[bool, str]:
        if completed is None:
            return False, f"launchctl {action} could not be run"
        if completed.returncode == 0:
            return True, ""
        detail = _text(completed.stderr) or _text(completed.stdout) or f"exit {completed.returncode}"
        return False, f"launchctl {action} failed: {detail[:200]}"


class UnsupportedProvider:
    """Every platform without a managed-service implementation."""

    name = "unsupported"

    def __init__(self, *, platform: str = sys.platform) -> None:
        self.platform = platform
        self.root = default_service_root()

    def available(self) -> tuple[bool, str]:
        return False, f"managed Board services are not implemented for {self.platform}"

    def definition_path(self, label: str) -> Path:
        return self.root / f"{label}.plist"

    def read_service(self, label: str) -> ManagedService | None:
        return None

    def definition_exists(self, label: str) -> bool:
        return False

    def list_services(self) -> list[ManagedService]:
        return []

    def job_arguments(self, label: str) -> tuple[str, ...] | None:
        return None

    def runtime(self, label: str) -> tuple[bool, int | None]:
        return False, None


def select_provider(
    *,
    platform: str = sys.platform,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    root: str | Path | None = None,
    uid: int | None = None,
) -> Any:
    if platform.startswith("darwin"):
        return LaunchdProvider(command_runner=command_runner, root=root, uid=uid, platform=platform)
    return UnsupportedProvider(platform=platform)


# -- local inspection -------------------------------------------------------


def repository_origin_slug(
    repo_path: Path,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> str:
    """The origin slug of a checkout, or "" when it cannot be read.

    Local-only: the path goes into the command, never into a payload.
    """

    try:
        completed = command_runner(["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"])
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    match = ORIGIN_SLUG_RE.match(_text(completed.stdout))
    return match.group("slug").strip().lower() if match else ""


def process_parent_pid(pid: int, command_runner: lane_status.CommandRunner) -> int | None:
    try:
        completed = command_runner(["ps", "-p", str(int(pid)), "-o", "ppid="])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return int(_text(completed.stdout))
    except ValueError:
        return None


def process_cwd(pid: int, command_runner: lane_status.CommandRunner) -> str:
    try:
        completed = command_runner(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"])
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if completed.returncode != 0:
        return ""
    for line in (completed.stdout or "").splitlines():
        if line.startswith("n"):
            return line[1:].strip()
    return ""


def probe_identity(host: str, port: int, *, timeout: float = 0.75) -> dict[str, Any]:
    """Ask a listener who it is. Loopback only; no secrets, no paths."""

    url = f"http://{host}:{int(port)}/api/identity"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            raw = response.read().decode("utf-8") or "{}"
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return {"available": False, "message": "Board identity probe failed"}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"available": False, "message": "Board identity response was not JSON"}
    if not isinstance(payload, Mapping):
        return {"available": False, "message": "Board identity response was not an object"}
    return dict(payload)


def port_listeners(port: int, command_runner: lane_status.CommandRunner) -> list[dict[str, Any]]:
    """Every local process holding a port, Board-shaped or not.

    Deliberately unfiltered. "Is this port free" and "was this port released"
    are questions about the port, not about Code Mower: a Node server on 5332,
    or an unrelated Python process on a nondefault port, holds the port just as
    firmly as a Board does. Asking the Board-shaped inventory would call those
    ports free and let an apply mutate local state into a conflict, or report a
    port released while another process still owns it.
    """

    listeners = []
    for item in lane_status.local_listeners(command_runner):
        if isinstance(item, Mapping) and int(item.get("port") or -1) == int(port):
            listeners.append(dict(item))
    return listeners


def port_ownership(
    port: int,
    *,
    provider: Any,
    label: str,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> dict[str, Any]:
    """Who holds a port, in the only terms an apply decision may use.

    `free`, `managed_self`, `managed_other` (another Code Mower label),
    `external_supervisor` (a supervised process that is not ours), or
    `foreign` (a process we cannot claim). Only `free` and `managed_self` may
    be replaced.

    Ownership is proved against the pid launchd is supervising, never against a
    definition that merely names the port. A managed job that is stopped or
    crashed leaves its definition installed, and an unrelated process is free to
    take the port it vacated: calling that listener ours would let a restart
    mutate the service instead of refusing, and leave a keepalive job trying
    forever to bind an occupied port. Every listener has to clear that bar --
    one unowned listener on the port is enough to refuse.
    """

    listeners = port_listeners(port, command_runner)
    if not listeners:
        return {"state": "free", "pid": None, "label": ""}
    supervised = {
        int(service.pid): service
        for service in provider.list_services()
        if service.loaded and service.pid
    }
    verdicts: list[dict[str, Any]] = []
    for listener in listeners:
        pid = int(listener.get("pid") or 0) or None
        owner = supervised.get(pid) if pid else None
        if owner is not None:
            state = "managed_self" if owner.label == label else "managed_other"
            verdicts.append({"state": state, "pid": pid, "label": owner.label, "repo": owner.repo})
            continue
        parent = process_parent_pid(pid, command_runner) if pid else None
        if parent == 1:
            verdicts.append(
                {
                    "state": "external_supervisor",
                    "pid": pid,
                    "label": "",
                    "detail": (
                        "the listening process is supervised by launchd under a definition "
                        "Code Mower does not own"
                    ),
                }
            )
            continue
        verdicts.append({"state": "foreign", "pid": pid, "label": ""})
    for state in ("managed_other", "external_supervisor", "foreign"):
        for verdict in verdicts:
            if verdict["state"] == state:
                return verdict
    return verdicts[0]


# -- binding validation -----------------------------------------------------


def _check(check_id: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"id": check_id, "status": status, "message": message, **extra}


def _is_python_interpreter(value: str) -> bool:
    name = os.path.basename(str(value or "")).lower()
    return bool(_PYTHON_EXECUTABLE_RE.match(name))


def normalize_live_arguments(live: Sequence[str], expected: Sequence[str]) -> tuple[str, ...]:
    """Restate an observed command line in the installed definition's terms.

    A pip installation puts a console script at `.../bin/code-mower`, and that
    single path is what the definition names. The script carries a `#!`, so the
    process launchd actually forks is the interpreter with the script as its
    first argument, and `ps` reports `/.../python3 /.../bin/code-mower board
    serve ...`. Both spellings describe the same process, so an exact
    comparison against the definition would fail every healthy console-script
    service.

    The interpreter/script prefix is folded back to the script only when the
    observed list is exactly one argument longer, its first argument is a
    Python interpreter, and the script it runs is the very executable the
    definition names. Anything else is returned untouched and still has to
    match the definition exactly -- this narrows a false failure, it does not
    widen what counts as the same process.
    """

    observed = tuple(str(item) for item in live)
    wanted = tuple(str(item) for item in expected)
    if not observed or not wanted:
        return observed
    if len(observed) != len(wanted) + 1:
        return observed
    if not _is_python_interpreter(observed[0]) or _is_python_interpreter(wanted[0]):
        return observed
    if not _same_path(observed[1], wanted[0]):
        return observed
    return (wanted[0], *observed[2:])


def validate_binding(
    spec: ServiceSpec,
    *,
    provider: Any,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    identity_probe: Callable[[str, int], Mapping[str, Any]] | None = None,
    show_local_paths: bool = False,
    expected_digest: str | None = None,
    expected_arguments: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The serving gate.

    Validates the applied label, the executable and the full argument list, the
    port, the repository slug, the exact private repository path, the installed
    version and the serving version. The path comparison happens here, locally;
    only its verdict is reported.
    """

    probe = identity_probe or (lambda host, port: probe_identity(host, port))
    expected = expected_digest or definition_digest(render_definition(spec))
    wanted_arguments = tuple(expected_arguments) if expected_arguments is not None else tuple(spec.arguments)
    checks: list[dict[str, Any]] = []
    service = provider.read_service(spec.label)

    if service is None:
        checks.append(_check("service.label", "fail", "no managed service definition is installed for this port"))
        return _binding_payload(spec, checks, None, show_local_paths=show_local_paths, expected_digest=expected)
    if not service.readable:
        checks.append(_check("service.label", "fail", service.message or "service definition is unreadable"))
        return _binding_payload(spec, checks, service, show_local_paths=show_local_paths, expected_digest=expected)

    checks.append(
        _check(
            "service.label",
            "pass" if service.label == spec.label else "fail",
            "service label matches the expected label"
            if service.label == spec.label
            else "installed service carries another label",
        )
    )
    definition_matches = service.digest == expected
    checks.append(
        _check(
            "service.definition",
            "pass" if definition_matches else "fail",
            "installed definition matches the rendered definition"
            if definition_matches
            else "installed definition has stale arguments; restart with --replace to apply the rendered definition",
            expected_digest=expected,
            installed_digest=service.digest,
        )
    )
    checks.append(
        _check(
            "service.keepalive",
            "pass" if service.keepalive else "fail",
            "service is keepalive-managed" if service.keepalive else "service is not keepalive-managed",
        )
    )
    checks.append(
        _check(
            "service.loaded",
            "pass" if service.loaded and service.pid else "fail",
            "service is loaded with a running process"
            if service.loaded and service.pid
            else "service is not running",
            pid=service.pid,
        )
    )

    pid = service.pid
    if pid:
        # From launchd, not from `ps`: the argument boundaries have to survive a
        # checkout or executable path containing a space, and a space-joined
        # `ps -o command=` line cannot be split back into the original argv.
        reported = provider.job_arguments(spec.label) if hasattr(provider, "job_arguments") else None
        live_arguments = tuple(reported or ())
        compared_arguments = normalize_live_arguments(live_arguments, wanted_arguments)
        arguments_match = reported is not None and compared_arguments == wanted_arguments
        if reported is None:
            arguments_message = "launchd did not report the argument list of the running job"
        elif arguments_match:
            arguments_message = "running process argument list matches the definition exactly"
        else:
            arguments_message = "running process was started with different arguments than the definition"
        checks.append(
            _check(
                "process.arguments",
                "pass" if arguments_match else "fail",
                arguments_message,
                # The observed list, not the normalized one: a reviewer reading a
                # failure needs what launchd actually reported.
                arguments=redact_arguments(live_arguments, show_local_paths=show_local_paths),
                arguments_redacted=not show_local_paths,
                arguments_reported=reported is not None,
                interpreter_prefix_normalized=compared_arguments != live_arguments,
            )
        )
        cwd = process_cwd(pid, command_runner)
        declared = binding_from_arguments(live_arguments).get("repo_path") or ""
        expected_path = str(spec.repo_path)
        path_match = bool(declared) and _same_path(declared, expected_path)
        cwd_match = bool(cwd) and _same_path(cwd, expected_path)
        checks.append(
            _check(
                "process.repo_path",
                "pass" if path_match and cwd_match else "fail",
                "running process is bound to the expected repository path"
                if path_match and cwd_match
                else "running process is bound to a different repository path",
                repo_path=redact_path(expected_path, show_local_paths=show_local_paths),
                repo_path_redacted=not show_local_paths,
                declared_path_match=path_match,
                working_directory_match=cwd_match,
            )
        )
        parent = process_parent_pid(pid, command_runner)
        checks.append(
            _check(
                "process.supervisor",
                "pass" if parent == 1 else "fail",
                "running process is supervised, so it survives the invoking shell"
                if parent == 1
                else "running process is not supervised; it would die with its invoking shell",
                parent_pid=parent,
            )
        )
        listeners = port_listeners(spec.port, command_runner)
        listener_pids = {int(item.get("pid") or -1) for item in listeners}
        port_match = pid in listener_pids
        checks.append(
            _check(
                "binding.port",
                "pass" if port_match else "fail",
                "the service process holds the expected port"
                if port_match
                else "the expected port is held by another process",
                port=spec.port,
            )
        )
    else:
        for check_id in ("process.arguments", "process.repo_path", "process.supervisor", "binding.port"):
            checks.append(_check(check_id, "fail", "no running service process to inspect"))

    identity = probe(spec.host, spec.port)
    board = identity.get("board") if isinstance(identity.get("board"), Mapping) else {}
    version = board.get("version") if isinstance(board.get("version"), Mapping) else {}
    serving_repo = _text(identity.get("repo"))
    repo_match = serving_repo.lower() == spec.repo.lower()
    checks.append(
        _check(
            "binding.repo",
            "pass" if repo_match else "fail",
            "the served repository slug matches the expected slug"
            if repo_match
            else "the port is serving another repository",
            repo=spec.repo,
        )
    )
    expected_installed = installed_version()
    reported_installed = _text(version.get("installed_version"))
    installed_ok = bool(reported_installed) and reported_installed == expected_installed
    checks.append(
        _check(
            "binding.installed_version",
            "pass" if installed_ok else "fail",
            "the served installed version matches this installation"
            if installed_ok
            else "the served installed version does not match this installation",
            installed_version=reported_installed,
            expected_installed_version=expected_installed,
        )
    )
    reported_serving = _text(version.get("serving_version"))
    serving_ok = bool(reported_serving) and reported_serving == reported_installed and not version.get("restart_recommended")
    checks.append(
        _check(
            "binding.serving_version",
            "pass" if serving_ok else "fail",
            "the serving version matches the installed version"
            if serving_ok
            else "the serving version is stale against the installed version",
            serving_version=reported_serving,
        )
    )
    return _binding_payload(spec, checks, service, show_local_paths=show_local_paths, expected_digest=expected)


def _same_path(left: str, right: str) -> bool:
    """Compare two local paths on one canonical spelling.

    `build_spec` resolves the requested checkout, so every rendered argv and
    every stored definition already carries the canonical path. A live process
    can still report a symlinked spelling for the same directory -- on macOS
    `/var/...` is `/private/var/...` -- and comparing those lexically would
    fail a healthy binding. Both sides are resolved so the comparison is the
    one the rest of the module already makes. Local only: the verdict leaves,
    the paths do not.
    """

    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except (TypeError, ValueError, OSError):
        return False


def _binding_payload(
    spec: ServiceSpec,
    checks: list[dict[str, Any]],
    service: ManagedService | None,
    *,
    show_local_paths: bool,
    expected_digest: str,
) -> dict[str, Any]:
    failing = [check["id"] for check in checks if check.get("status") != "pass"]
    return {
        "schema": BOARD_SERVICE_BINDING_SCHEMA,
        "status": "pass" if not failing else "fail",
        "label": spec.label,
        "repo": spec.repo,
        "port": spec.port,
        "host": spec.host,
        "repo_path": redact_path(str(spec.repo_path), show_local_paths=show_local_paths),
        "repo_path_redacted": not show_local_paths,
        "expected_digest": expected_digest,
        "installed_digest": service.digest if service else "",
        "pid": service.pid if service else None,
        "checks": checks,
        "failing_checks": failing,
    }


def delayed_health(
    spec: ServiceSpec,
    *,
    provider: Any,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    identity_probe: Callable[[str, int], Mapping[str, Any]] | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    show_local_paths: bool = False,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Settle, then refresh the full binding gate until it passes or times out.

    A service that answers immediately and then dies, or that comes back with
    stale arguments, fails here rather than being accepted on one early probe.
    """

    settle = max(0.0, float(settle_seconds))
    refresh = max(0.1, float(refresh_seconds))
    timeout = max(settle, float(timeout_seconds))
    if settle:
        sleeper(settle)
    deadline = clock() + max(0.0, timeout - settle)
    attempts = 0
    binding: dict[str, Any] = {}
    while True:
        attempts += 1
        binding = validate_binding(
            spec,
            provider=provider,
            command_runner=command_runner,
            identity_probe=identity_probe,
            show_local_paths=show_local_paths,
        )
        if binding.get("status") == "pass" or clock() >= deadline:
            break
        sleeper(refresh)
    state = "pass" if binding.get("status") == "pass" else "fail"
    return {
        "state": state,
        "settle_seconds": settle,
        "refresh_seconds": refresh,
        "timeout_seconds": timeout,
        "attempts": attempts,
        "binding": binding,
    }


# -- operations -------------------------------------------------------------


def definition_payload(spec: ServiceSpec, *, show_local_paths: bool = False) -> dict[str, Any]:
    text = render_definition(spec)
    payload: dict[str, Any] = {
        "schema": BOARD_SERVICE_DEFINITION_SCHEMA,
        "provider": LAUNCHD_PROVIDER,
        "label": spec.label,
        "repo": spec.repo,
        "host": spec.host,
        "port": spec.port,
        "keepalive": spec.keepalive,
        "executable": redact_path(spec.executable, show_local_paths=show_local_paths),
        "arguments": redact_arguments(spec.arguments, show_local_paths=show_local_paths),
        "arguments_redacted": not show_local_paths,
        "repo_path": redact_path(str(spec.repo_path), show_local_paths=show_local_paths),
        "repo_path_redacted": not show_local_paths,
        "digest": definition_digest(text),
    }
    if show_local_paths:
        payload["definition"] = text
    return payload


def _unsupported(provider: Any, message: str) -> dict[str, Any]:
    return {
        "schema": BOARD_SERVICE_SCHEMA,
        "status": "unsupported_platform",
        "message": message,
        "provider": getattr(provider, "name", "unsupported"),
    }


def _ownership_refusal(spec: ServiceSpec, ownership: str, message: str) -> dict[str, Any]:
    """An ownership refusal, reported before anything is applied.

    `ownership` discriminates the two ways proving ownership fails without
    widening `SERVICE_STATUSES`; a caller that only branches on `status` keeps
    treating both as the same refusal. No digest and no definition: nothing was
    rendered against this path, and nothing local changed.
    """

    return {
        "schema": BOARD_SERVICE_SCHEMA,
        "status": "ownership_mismatch",
        "ownership": ownership,
        "message": message,
        "label": spec.label,
        "repo": spec.repo,
        "port": spec.port,
    }


def _origin_guard(
    spec: ServiceSpec,
    command_runner: lane_status.CommandRunner,
) -> dict[str, Any] | None:
    """Refuse a path this lane cannot prove is a checkout of `--repo`.

    Ownership is established from the checkout's own origin slug, so the two
    ways it fails are symmetric and both refuse: an origin naming a different
    repository, and an origin that cannot be read at all. Treating an unreadable
    origin as consent is the exact failure this module exists to remove -- the
    v1.4.0 inventory gate accepted a reclaimed port because it checked the slug
    and the versions but never proved which checkout was being served.
    """

    origin = repository_origin_slug(spec.repo_path, command_runner)
    if not origin:
        return _ownership_refusal(
            spec,
            "unverified",
            "the repository path has no readable git origin, so it cannot be proven to be a checkout of --repo",
        )
    if origin != spec.repo.lower():
        return _ownership_refusal(
            spec,
            "mismatch",
            "the repository path is a checkout of a different repository than --repo",
        )
    return None


def _unreadable_refusal(
    spec: ServiceSpec,
    expected: str,
    existing: ManagedService,
    *,
    show_local_paths: bool,
) -> dict[str, Any]:
    """Refuse an installed definition we cannot read, unless takeover is asked for.

    A malformed or unreadable plist is the one case where nothing can be
    compared, which makes it the last case that should be treated as consent.
    Falling through would boot the job out and overwrite the file with no
    recoverable backup -- the definition's contents could not be read, so a
    rollback could not restore them. `--replace` is the same explicit takeover
    that stale arguments require.
    """

    return _operation_payload(
        "stale_arguments",
        f"a definition is installed for this port but cannot be read ({existing.message or 'unreadable'}); "
        "rerun with --replace to overwrite it, which discards its current contents",
        spec,
        expected,
        show_local_paths=show_local_paths,
        installed_readable=False,
    )


def install_service(
    spec: ServiceSpec,
    *,
    provider: Any,
    replace: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    identity_probe: Callable[[str, int], Mapping[str, Any]] | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    show_local_paths: bool = False,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    available, why = provider.available()
    if not available:
        return _unsupported(provider, why)
    guard = _origin_guard(spec, command_runner)
    if guard is not None:
        return guard

    rendered = render_definition(spec)
    expected = definition_digest(rendered)
    existing = provider.read_service(spec.label)
    if existing is not None and not existing.readable and not replace:
        return _unreadable_refusal(spec, expected, existing, show_local_paths=show_local_paths)
    if existing is not None and existing.readable and existing.digest == expected and not replace:
        health = delayed_health(
            spec,
            provider=provider,
            command_runner=command_runner,
            identity_probe=identity_probe,
            settle_seconds=0.0,
            refresh_seconds=refresh_seconds,
            timeout_seconds=0.0,
            show_local_paths=show_local_paths,
            sleeper=sleeper,
            clock=clock,
        )
        return _operation_payload(
            "unchanged" if health["state"] == "pass" else "delayed_health_failed",
            "the rendered definition is already installed and serving"
            if health["state"] == "pass"
            else "the rendered definition is already installed but its binding does not validate",
            spec,
            expected,
            delayed=health,
            show_local_paths=show_local_paths,
        )
    if existing is not None and existing.readable and existing.digest != expected and not replace:
        return _operation_payload(
            "stale_arguments",
            "another definition is installed for this port; rerun with --replace to take it over",
            spec,
            expected,
            show_local_paths=show_local_paths,
            installed_repo=existing.repo,
        )

    ownership = port_ownership(spec.port, provider=provider, label=spec.label, command_runner=command_runner)
    conflict = _ownership_conflict(ownership, spec, expected, show_local_paths=show_local_paths)
    if conflict is not None:
        return conflict
    return _apply(
        spec,
        provider=provider,
        rendered=rendered,
        expected=expected,
        previous=existing,
        succeeded_status="installed",
        succeeded_message="installed and validated the managed Board service",
        command_runner=command_runner,
        identity_probe=identity_probe,
        settle_seconds=settle_seconds,
        refresh_seconds=refresh_seconds,
        timeout_seconds=timeout_seconds,
        show_local_paths=show_local_paths,
        sleeper=sleeper,
        clock=clock,
    )


def _ownership_conflict(
    ownership: Mapping[str, Any],
    spec: ServiceSpec,
    expected: str,
    *,
    show_local_paths: bool,
) -> dict[str, Any] | None:
    state = str(ownership.get("state") or "")
    if state in {"free", "managed_self"}:
        return None
    if state == "managed_other":
        return _operation_payload(
            "ownership_mismatch",
            f"port {spec.port} is owned by managed Board service {ownership.get('label')}",
            spec,
            expected,
            show_local_paths=show_local_paths,
        )
    if state == "external_supervisor":
        return _operation_payload(
            "external_supervisor",
            f"port {spec.port} is held by a process another supervisor owns; stop that supervisor first",
            spec,
            expected,
            show_local_paths=show_local_paths,
        )
    return _operation_payload(
        "port_conflict",
        f"port {spec.port} is already held by another local process",
        spec,
        expected,
        show_local_paths=show_local_paths,
    )


def _apply(
    spec: ServiceSpec,
    *,
    provider: Any,
    rendered: str,
    expected: str,
    previous: ManagedService | None,
    succeeded_status: str,
    succeeded_message: str,
    command_runner: lane_status.CommandRunner,
    identity_probe: Callable[[str, int], Mapping[str, Any]] | None,
    settle_seconds: float,
    refresh_seconds: float,
    timeout_seconds: float,
    show_local_paths: bool,
    sleeper: Sleeper,
    clock: Clock,
) -> dict[str, Any]:
    """Replace a managed binding atomically, or restore what was there before."""

    # Before anything is booted out: launchd cannot start a job whose log files
    # it cannot open, and a filesystem failure must not be discovered after the
    # previously working service has already been stopped.
    log_failure = ensure_log_directories(spec)
    if log_failure:
        return _operation_payload(
            "apply_failed", log_failure, spec, expected, show_local_paths=show_local_paths
        )

    previous_text = ""
    if previous is not None and previous.readable:
        try:
            previous_text = previous.definition_path.read_text(encoding="utf-8")
        except OSError:
            previous_text = ""
    if previous is not None:
        provider.bootout(spec.label)
    try:
        provider.write_definition(spec.label, rendered)
    except OSError as exc:
        detail = f"could not write the service definition ({exc.__class__.__name__})"
        if previous is None:
            return _operation_payload(
                "apply_failed", detail, spec, expected, show_local_paths=show_local_paths
            )
        # The previous service was already booted out, so failing here without
        # reloading it would leave a Board that was working stopped.
        restore = _restore_unwritten(provider, spec)
        return _operation_payload(
            "apply_failed" if restore["ok"] else "rollback_failed",
            detail if restore["ok"] else f"{detail}; rollback also failed: {restore['detail']}",
            spec,
            expected,
            show_local_paths=show_local_paths,
            rollback=restore,
        )
    ok, detail = provider.bootstrap(spec.label)
    if not ok:
        rollback = _rollback(provider, spec, previous_text)
        status = "rollback_failed" if not rollback["ok"] else "apply_failed"
        message = detail if rollback["ok"] else f"{detail}; rollback also failed: {rollback['detail']}"
        return _operation_payload(status, message, spec, expected, show_local_paths=show_local_paths, rollback=rollback)

    health = delayed_health(
        spec,
        provider=provider,
        command_runner=command_runner,
        identity_probe=identity_probe,
        settle_seconds=settle_seconds,
        refresh_seconds=refresh_seconds,
        timeout_seconds=timeout_seconds,
        show_local_paths=show_local_paths,
        sleeper=sleeper,
        clock=clock,
    )
    if health["state"] != "pass":
        return _operation_payload(
            "delayed_health_failed",
            "the service applied but its binding did not validate within the delayed health window",
            spec,
            expected,
            delayed=health,
            show_local_paths=show_local_paths,
        )
    return _operation_payload(
        succeeded_status,
        succeeded_message,
        spec,
        expected,
        delayed=health,
        show_local_paths=show_local_paths,
    )


def _restore_unwritten(provider: Any, spec: ServiceSpec) -> dict[str, Any]:
    """Reload the previous service after its replacement failed to be written.

    Distinct from `_rollback`, and deliberately so. `write_definition` swaps
    atomically, so a failed write leaves the previous definition on disk exactly
    as it was: there is nothing to rewrite, and nothing to delete -- deleting
    it, which is what `_rollback` correctly does for a definition we *did*
    write, would destroy the very thing being recovered. All that is owed here
    is loading the untouched definition again, and saying whether that worked.
    """

    ok, detail = provider.bootstrap(spec.label)
    return {"ok": ok, "detail": detail or "reloaded the previous definition", "restored": ok}


def _rollback(provider: Any, spec: ServiceSpec, previous_text: str) -> dict[str, Any]:
    """Put back exactly what was there, or say plainly that we could not."""

    if not previous_text:
        provider.bootout(spec.label)
        removed = provider.delete_definition(spec.label)
        return {"ok": True, "detail": "removed the definition that failed to apply", "restored": False, "deleted": removed}
    try:
        provider.write_definition(spec.label, previous_text)
    except OSError as exc:
        return {"ok": False, "detail": f"could not restore the previous definition ({exc.__class__.__name__})", "restored": False}
    ok, detail = provider.bootstrap(spec.label)
    return {"ok": ok, "detail": detail or "restored the previous definition", "restored": ok}


def restart_service(
    spec: ServiceSpec,
    *,
    provider: Any,
    replace: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    identity_probe: Callable[[str, int], Mapping[str, Any]] | None = None,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    show_local_paths: bool = False,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Restart a managed Board, idempotently, failing closed on anything stale."""

    available, why = provider.available()
    if not available:
        return _unsupported(provider, why)
    guard = _origin_guard(spec, command_runner)
    if guard is not None:
        return guard

    rendered = render_definition(spec)
    expected = definition_digest(rendered)
    existing = provider.read_service(spec.label)
    ownership = port_ownership(spec.port, provider=provider, label=spec.label, command_runner=command_runner)
    conflict = _ownership_conflict(ownership, spec, expected, show_local_paths=show_local_paths)
    if conflict is not None:
        return conflict

    if existing is None:
        return install_service(
            spec,
            provider=provider,
            command_runner=command_runner,
            identity_probe=identity_probe,
            settle_seconds=settle_seconds,
            refresh_seconds=refresh_seconds,
            timeout_seconds=timeout_seconds,
            show_local_paths=show_local_paths,
            sleeper=sleeper,
            clock=clock,
        )
    if not existing.readable and not replace:
        return _unreadable_refusal(spec, expected, existing, show_local_paths=show_local_paths)
    if existing.readable and existing.digest != expected and not replace:
        return _operation_payload(
            "stale_arguments",
            "the installed definition differs from the rendered definition; rerun with --replace to take it over",
            spec,
            expected,
            show_local_paths=show_local_paths,
            installed_repo=existing.repo,
        )
    if existing.readable and existing.digest == expected:
        log_failure = ensure_log_directories(spec)
        if log_failure:
            return _operation_payload("apply_failed", log_failure, spec, expected, show_local_paths=show_local_paths)
        # `kickstart` restarts a job launchd already holds; it cannot load one
        # that is not registered. A valid definition whose job was booted out --
        # after a logout, or a manual `launchctl bootout` -- is recovered by
        # bootstrapping it, which is what the runbook's restart has to do.
        if existing.loaded:
            ok, detail = provider.kickstart(spec.label)
            restarted_message = "restarted the managed Board service in place and validated its binding"
        else:
            ok, detail = provider.bootstrap(spec.label)
            restarted_message = "loaded the installed definition, which was not running, and validated its binding"
        if not ok:
            return _operation_payload("apply_failed", detail, spec, expected, show_local_paths=show_local_paths)
        health = delayed_health(
            spec,
            provider=provider,
            command_runner=command_runner,
            identity_probe=identity_probe,
            settle_seconds=settle_seconds,
            refresh_seconds=refresh_seconds,
            timeout_seconds=timeout_seconds,
            show_local_paths=show_local_paths,
            sleeper=sleeper,
            clock=clock,
        )
        status = "restarted" if health["state"] == "pass" else "delayed_health_failed"
        message = (
            restarted_message
            if health["state"] == "pass"
            else "the service restarted but its binding did not validate within the delayed health window"
        )
        return _operation_payload(status, message, spec, expected, delayed=health, show_local_paths=show_local_paths)
    return _apply(
        spec,
        provider=provider,
        rendered=rendered,
        expected=expected,
        previous=existing,
        succeeded_status="restarted",
        succeeded_message="replaced the stale managed binding and validated the new one",
        command_runner=command_runner,
        identity_probe=identity_probe,
        settle_seconds=settle_seconds,
        refresh_seconds=refresh_seconds,
        timeout_seconds=timeout_seconds,
        show_local_paths=show_local_paths,
        sleeper=sleeper,
        clock=clock,
    )


def resolve_service(
    *,
    provider: Any,
    repo: str = "",
    port: int | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Resolve exactly one managed service, or refuse without touching any.

    An ambiguous repository -- the same slug served by more than one managed
    service -- is never resolved by picking one.
    """

    services = provider.list_services()
    candidates = list(services)
    if label:
        candidates = [item for item in candidates if item.label == label]
    if port is not None:
        candidates = [item for item in candidates if item.port == int(port)]
    if repo:
        candidates = [item for item in candidates if item.repo.lower() == repo.strip().lower()]
    if not (label or port is not None or repo):
        return {"status": "invalid_request", "message": "pass --repo, --port, or --label", "service": None, "matches": []}
    if not candidates:
        return {"status": "not_installed", "message": "no managed Board service matches that selector", "service": None, "matches": []}
    if len(candidates) > 1:
        return {
            "status": "ambiguous_repository",
            "message": "the selector matches more than one managed Board service; add --port to name exactly one",
            "service": None,
            "matches": [item.label for item in candidates],
        }
    return {"status": "ok", "message": "", "service": candidates[0], "matches": [candidates[0].label]}


def _job_load_state(provider: Any, label: str) -> tuple[bool, int | None]:
    """Whether launchd still holds a job, erring towards "it does".

    A provider that cannot answer, or an answer that fails, leaves the job
    unconfirmed -- and an unconfirmed job is treated as still loaded so nothing
    destructive proceeds on a guess.
    """

    runtime = getattr(provider, "runtime", None)
    if runtime is None:
        return True, None
    try:
        loaded, pid = runtime(label)
    except (OSError, subprocess.SubprocessError):
        return True, None
    return bool(loaded), pid


def remove_service(
    *,
    provider: Any,
    repo: str = "",
    port: int | None = None,
    label: str = "",
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    show_local_paths: bool = False,
    sleeper: Sleeper = time.sleep,
) -> dict[str, Any]:
    available, why = provider.available()
    if not available:
        return _unsupported(provider, why)
    resolved = resolve_service(provider=provider, repo=repo, port=port, label=label)
    if resolved["status"] != "ok":
        return {
            "schema": BOARD_SERVICE_SCHEMA,
            "status": resolved["status"],
            "message": resolved["message"],
            "matches": resolved["matches"],
        }
    service = resolved["service"]
    ok, detail = provider.bootout(service.label)
    if not ok:
        # The definition is how this service is discovered at all: `status`,
        # `remove` and the `board stop` keepalive guard all scan definition
        # files. Deleting it while launchd still holds the job would strand a
        # running, self-restarting service with nothing left to manage it by,
        # and let `board stop` signal a process launchd immediately replaces.
        # Only an independently confirmed-absent job permits deletion.
        still_loaded, _pid = _job_load_state(provider, service.label)
        if still_loaded:
            return {
                "schema": BOARD_SERVICE_SCHEMA,
                "status": "remove_incomplete",
                "label": service.label,
                "repo": service.repo,
                "port": service.port,
                "definition_deleted": False,
                "definition_present": True,
                "message": (
                    f"{detail or 'the service could not be unloaded'}; its definition was kept so the "
                    "still-loaded job stays discoverable by board service status, remove and the "
                    "board stop keepalive guard"
                ),
            }
    deleted = provider.delete_definition(service.label)
    # The authority is the filesystem, not the return value: a definition still
    # in LaunchAgents starts the service again at the next login, so removal has
    # not happened however cleanly the unload went.
    definition_present = bool(getattr(provider, "definition_exists", lambda _label: not deleted)(service.label))
    if settle_seconds:
        sleeper(max(0.0, float(settle_seconds)))
    remaining = port_listeners(service.port, command_runner) if service.port else []
    payload = {
        "schema": BOARD_SERVICE_SCHEMA,
        "label": service.label,
        "repo": service.repo,
        "port": service.port,
        "definition_deleted": deleted and not definition_present,
        "definition_present": definition_present,
    }
    if not ok:
        # Reached only when the unload reported a failure but launchd no longer
        # holds the job, so deleting the definition was safe. Still not
        # `removed`: the operator asked for a clean unload and did not get one.
        payload["status"] = "remove_incomplete"
        payload["message"] = (
            f"{detail or 'the service could not be unloaded'}; launchd no longer holds the job, "
            "so its definition was removed"
        )
        return payload
    if definition_present:
        payload["status"] = "remove_incomplete"
        payload["message"] = (
            "the service was unloaded but its definition could not be deleted, so it would "
            "start again at the next login; remove the definition by hand before treating "
            "this port as free"
        )
        return payload
    if remaining:
        payload["status"] = "remove_incomplete"
        payload["message"] = (
            f"the definition was removed but port {service.port} is still held; "
            "inspect the remaining listener before starting a replacement"
        )
        return payload
    payload["status"] = "removed"
    payload["message"] = "removed the managed Board service and released its port"
    return payload


def _operation_payload(
    status: str,
    message: str,
    spec: ServiceSpec,
    expected_digest: str,
    *,
    delayed: Mapping[str, Any] | None = None,
    show_local_paths: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": BOARD_SERVICE_SCHEMA,
        "status": status,
        "message": message,
        "provider": LAUNCHD_PROVIDER,
        "label": spec.label,
        "repo": spec.repo,
        "host": spec.host,
        "port": spec.port,
        "repo_path": redact_path(str(spec.repo_path), show_local_paths=show_local_paths),
        "repo_path_redacted": not show_local_paths,
        "digest": expected_digest,
        **extra,
    }
    if delayed is not None:
        payload["delayed_health"] = dict(delayed)
    return payload


def service_status(
    *,
    provider: Any,
    repo: str = "",
    port: int | None = None,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    identity_probe: Callable[[str, int], Mapping[str, Any]] | None = None,
    show_local_paths: bool = False,
) -> dict[str, Any]:
    """Inspect every managed Board service, validating each binding."""

    available, why = provider.available()
    if not available:
        return {
            "schema": BOARD_SERVICE_STATUS_SCHEMA,
            "status": "unsupported_platform",
            "message": why,
            "provider": getattr(provider, "name", "unsupported"),
            "services": [],
            "failing": [],
        }
    services = provider.list_services()
    rows: list[dict[str, Any]] = []
    for service in services:
        if repo and service.repo.lower() != repo.strip().lower():
            continue
        if port is not None and service.port != int(port):
            continue
        row: dict[str, Any] = {
            "label": service.label,
            "repo": service.repo,
            "port": service.port,
            "host": service.host,
            "keepalive": service.keepalive,
            "loaded": service.loaded,
            "pid": service.pid,
            "digest": service.digest,
            "repo_path": redact_path(service.repo_path, show_local_paths=show_local_paths),
            "repo_path_redacted": not show_local_paths,
            "arguments": redact_arguments(service.arguments, show_local_paths=show_local_paths),
            "arguments_redacted": not show_local_paths,
        }
        if not service.readable:
            row["binding"] = {"status": "fail", "message": service.message}
            rows.append(row)
            continue
        if service.port and service.repo and service.repo_path:
            # The installed definition is the expectation here, not a freshly
            # rendered one: status answers "is this service serving what it
            # says it serves", and a drifting rendering is restart's problem.
            row["binding"] = validate_binding(
                spec_from_service(service),
                provider=provider,
                command_runner=command_runner,
                identity_probe=identity_probe,
                show_local_paths=show_local_paths,
                expected_digest=service.digest,
                expected_arguments=service.arguments,
            )
        else:
            row["binding"] = {"status": "fail", "message": "the installed definition does not encode a Board binding"}
        rows.append(row)
    failing = [row["label"] for row in rows if (row.get("binding") or {}).get("status") != "pass"]
    if not available:
        status = "unsupported_platform"
        message = why
    elif not rows:
        status = "not_installed"
        message = "no managed Board service is installed"
    elif failing:
        status = "delayed_health_failed"
        message = f"{len(failing)} managed Board service binding(s) do not validate"
    else:
        status = "ok"
        message = f"{len(rows)} managed Board service binding(s) validate"
    return {
        "schema": BOARD_SERVICE_STATUS_SCHEMA,
        "status": status,
        "message": message,
        "provider": getattr(provider, "name", "unsupported"),
        "services": rows,
        "failing": failing,
    }


def managed_services(
    *,
    provider: Any = None,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    platform: str = sys.platform,
) -> list[ManagedService]:
    """Every installed managed Board service, or an empty list off macOS.

    Used by `board stop` so a transient process is never confused with a
    keepalive-managed one.
    """

    active = provider or select_provider(platform=platform, command_runner=command_runner)
    available, _why = active.available()
    if not available:
        return []
    try:
        return active.list_services()
    except OSError:
        return []


def render_definition_text(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower Board service definition ({payload.get('provider') or 'unknown provider'})",
        f"Label: {payload.get('label')}",
        f"Repo: {payload.get('repo')}  port: {payload.get('port')}  host: {payload.get('host')}",
        f"Keepalive: {'yes' if payload.get('keepalive') else 'no'}",
        f"Repository path: {payload.get('repo_path')}",
        f"Arguments: {' '.join(str(item) for item in payload.get('arguments') or [])}",
        f"Digest: {payload.get('digest')}",
    ]
    if payload.get("definition"):
        lines.extend(["", str(payload["definition"]).rstrip()])
    else:
        lines.extend(
            [
                "",
                "Local paths are redacted. Pass --show-local-paths to review the exact",
                "definition, or --output FILE to write it for review before applying.",
            ]
        )
    return "\n".join(lines) + "\n"


def render_operation_text(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower Board service: {payload.get('status') or 'unknown'}",
        str(payload.get("message") or ""),
    ]
    if payload.get("label"):
        lines.append(f"Label: {payload.get('label')} repo={payload.get('repo')} port={payload.get('port')}")
    delayed = payload.get("delayed_health") if isinstance(payload.get("delayed_health"), Mapping) else {}
    if delayed:
        lines.append(
            f"Delayed health: {delayed.get('state')} after {delayed.get('attempts')} refresh(es) "
            f"(settle {delayed.get('settle_seconds')}s, timeout {delayed.get('timeout_seconds')}s)"
        )
        binding = delayed.get("binding") if isinstance(delayed.get("binding"), Mapping) else {}
        for check in binding.get("checks") or []:
            if isinstance(check, Mapping) and check.get("status") != "pass":
                lines.append(f"- {check.get('id')}: {check.get('message')}")
    rollback = payload.get("rollback") if isinstance(payload.get("rollback"), Mapping) else {}
    if rollback:
        lines.append(f"Rollback: {'ok' if rollback.get('ok') else 'failed'} - {rollback.get('detail')}")
    return "\n".join(line for line in lines if line) + "\n"


def render_status_text(payload: Mapping[str, Any]) -> str:
    lines = [
        f"Code Mower Board services: {payload.get('status') or 'unknown'}",
        str(payload.get("message") or ""),
    ]
    for row in payload.get("services") or []:
        if not isinstance(row, Mapping):
            continue
        binding = row.get("binding") if isinstance(row.get("binding"), Mapping) else {}
        lines.append(
            f"- {row.get('label')} repo={row.get('repo') or 'unknown repo'} port={row.get('port')} "
            f"pid={row.get('pid')} binding={binding.get('status') or 'unknown'}"
        )
        for check in binding.get("checks") or []:
            if isinstance(check, Mapping) and check.get("status") != "pass":
                lines.append(f"  - {check.get('id')}: {check.get('message')}")
    return "\n".join(line for line in lines if line) + "\n"
