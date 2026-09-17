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
from collections.abc import Callable, Iterable, Mapping, Sequence
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
    "listener_inventory_unavailable",
    "external_supervisor",
    "backup_failed",
    "unload_failed",
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

# An owner and a repository are named by different rules, and imposing the
# owner's on both is what rejects real slugs. A GitHub owner is alphanumerics
# and hyphens and must start with an alphanumeric; a repository name may begin
# with punctuation -- `owner/.github` is the canonical example, and Board serves
# it happily -- so requiring a leading alphanumeric there locked those
# repositories out of service render/install/restart and `board stop --repo`
# while serving them worked. The one thing a repository component may not be is
# a name with no alphanumeric in it at all: `.` and `..` are path traversal
# rather than repositories, and this slug becomes a path component downstream.
_OWNER_COMPONENT_RE = r"[A-Za-z0-9][A-Za-z0-9-]*"
_REPO_COMPONENT_RE = r"[A-Za-z0-9._-]*[A-Za-z0-9][A-Za-z0-9._-]*"
REPO_SLUG_RE = re.compile(rf"^{_OWNER_COMPONENT_RE}/{_REPO_COMPONENT_RE}$")
ORIGIN_SLUG_RE = re.compile(
    r"^(?:git@[^:]+:|(?:https?|ssh|git)://(?:[^@/]+@)?[^/]+/)(?P<slug>.+?)(?:\.git)?$"
)
_LABEL_PORT_RE = re.compile(rf"^{re.escape(SERVICE_LABEL_PREFIX)}\.(\d+)$")
_LAUNCHCTL_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)
_LAUNCHCTL_STATE_RE = re.compile(r"^\s*state\s*=\s*(\S+)", re.MULTILINE)
_LAUNCHCTL_ARGUMENTS_OPEN_RE = re.compile(r"^\s*arguments\s*=\s*\{\s*$")
_LAUNCHCTL_BLOCK_CLOSE_RE = re.compile(r"^\s*\}\s*$")
_PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")

# What launchd is known to hold, and what it merely failed to tell us. A query
# that errored or timed out is `JOB_UNKNOWN`, never `JOB_ABSENT`: absence has to
# be positively established before anything deletes a definition or hands a port
# away.
JOB_LOADED = "loaded"
JOB_ABSENT = "absent"
JOB_UNKNOWN = "unknown"

# `launchctl` exits `EX_NOTFOUND` for a job the domain does not hold, and prints
# one of these for it. Any other failure says nothing about whether the job is
# there.
_LAUNCHCTL_NOT_FOUND_CODE = 113
_LAUNCHCTL_NOT_FOUND_RE = re.compile(r"could not find service|no such process", re.IGNORECASE)

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


def installed_distribution_version() -> str | None:
    """The installed distribution version, or `None` in a source checkout.

    The Board answers the same question with `board.board_version_payload()`,
    which reports an empty `installed_version` when there is no distribution
    metadata. Keeping the "no distribution" case distinguishable here -- rather
    than folding it into the imported version -- is what lets the serving gate
    hold both sides to the same contract instead of comparing a fallback
    against an empty string forever.
    """

    try:
        return metadata.version("code-mower")
    except metadata.PackageNotFoundError:
        return None


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


def module_search_path() -> str:
    """The directory `code_mower` has to be importable from.

    Resolved from this module's own file, so it is a canonical absolute path
    rather than whatever the invoking shell happened to put on `PYTHONPATH`.
    For an installed distribution this is `site-packages`, which the interpreter
    already searches; for a source checkout run as `PYTHONPATH=src` it is that
    `src`, which nothing else would tell the service about.
    """

    return str(Path(__file__).resolve().parent.parent)


def uses_module_entry_point(program: Sequence[str]) -> bool:
    """Whether a program starts the Board through `-m`, not a console script."""

    return "-m" in [str(item) for item in program]


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
    module_path_env: str = ""

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
    # `loaded` answers "is launchd known to hold this job", which collapses "it
    # does not" and "launchd would not say" into one `False`. Anything deciding
    # whether it is safe to *stop* caring about the job needs those apart, so
    # the provider's three-state answer is carried alongside rather than
    # reconstructed from a boolean that cannot express it.
    load_state: str = JOB_ABSENT
    readable: bool = True
    message: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_path(value: str) -> bool:
    return value.startswith("/") or value.startswith("~")


def is_loopback_host(host: str) -> bool:
    """The one loopback rule, shared with `board serve`.

    `board serve` refuses a non-loopback host outright, so a service that names
    one describes a Board that can never come up: launchd would restart the
    failing process forever, and a replacement would additionally stop the
    working service and leave the invalid definition installed once the health
    window expired. The rule lives here and `board` defers to it so the two can
    never drift into disagreeing about what this service is allowed to bind.
    """

    value = _text(host)
    return value in {"localhost", "::1"} or value.startswith("127.")


def redact_path(value: object, *, show_local_paths: bool) -> str:
    text = _text(value)
    if not text:
        return ""
    return text if show_local_paths else lane_status.LOCAL_PATH_REDACTION


# A local path embedded in free-form text, such as the definition path launchd
# names when it refuses to load one. The lookbehind keeps `Input/output` and
# `owner/repo` whole -- a slash that continues a word is not the start of a path
# -- and keeps `http://host` out of it, while still catching the `--flag=/path`
# and `path: /path` spellings a diagnostic actually uses.
_PATH_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_:/])(?:~|/)[A-Za-z0-9._~@+/-][^\s'\"]*")

# Punctuation that ends a path where a diagnostic keeps writing afterwards, as
# in `/path/to/a.plist: No such file`. A path may contain none of these, so a
# run that ends with one is a path whose end is known.
_PATH_TERMINATORS = (":", ";", ",", ")", "]", ">")


def known_path_spellings(paths: Iterable[object]) -> tuple[str, ...]:
    """Every spelling a known local path can appear under, longest first.

    A diagnostic may name the same file as launchd was given it or with the
    home prefix abbreviated, and replacing a longer path before a shorter one
    keeps a parent directory from eating the child's replacement.
    """

    home = str(Path.home())
    spellings: set[str] = set()
    for item in paths:
        text = _text(item)
        if len(text) < 2 or not _looks_like_path(text):
            continue
        spellings.add(text)
        if home and text.startswith(home + "/"):
            spellings.add("~" + text[len(home) :])
        if text.startswith("~/") and home:
            spellings.add(home + text[1:])
    return tuple(sorted(spellings, key=len, reverse=True))


def _redact_diagnostic_line(line: str) -> str:
    """One line of free-form diagnostic, with no path suffix left behind.

    A path-shaped run ends at whitespace, so a path containing a space --
    `/opt/Private Projects/agent.plist` -- matches only its first word and
    substituting that run alone would publish the rest. Whether the words after
    such a run continue the path or resume the message cannot be told apart from
    the text, so when the run's end is not marked by punctuation the remainder of
    the line is withheld rather than guessed at. Whatever came *before* the path
    is kept either way: that is where the operation and the failure are named.
    """

    pieces: list[str] = []
    position = 0
    for match in _PATH_IN_TEXT.finditer(line):
        pieces.append(line[position : match.start()])
        pieces.append(lane_status.LOCAL_PATH_REDACTION)
        position = match.end()
        tail = line[position:]
        # A run followed immediately by a quote or bracket ended there; one
        # followed by a space may be the first word of a longer path.
        if tail[:1].isspace() and not match.group(0).endswith(_PATH_TERMINATORS):
            return "".join(pieces)
    pieces.append(line[position:])
    return "".join(pieces)


def redact_diagnostic(
    value: object, *, show_local_paths: bool, known_paths: Iterable[object] = ()
) -> str:
    """Hide local paths inside a subprocess diagnostic, keeping the diagnostic.

    `launchctl` names the definition file it could not load, and that text is
    copied into operation messages and rollback details that print in both text
    and JSON. Redacting the whole string would throw away the reason the
    operation failed, which is the only part an operator can act on.

    So the paths whose exact spelling this operation already knows -- the
    definition it wrote, the checkout it serves, the logs it opened -- are
    replaced first, whole, however many spaces they contain. Only what is left
    goes through the free-form pass, which cannot know where an unknown path
    ends and therefore errs towards withholding.
    """

    text = _text(value)
    if show_local_paths or not text:
        return text
    for spelling in known_path_spellings(known_paths):
        text = text.replace(spelling, lane_status.LOCAL_PATH_REDACTION)
    return "\n".join(_redact_diagnostic_line(line) for line in text.split("\n"))


def _redact_diagnostics(value: Any, *, show_local_paths: bool, known_paths: Iterable[object] = ()) -> Any:
    """`redact_diagnostic` over a nested payload fragment, strings only."""

    paths = tuple(known_paths)
    if isinstance(value, str):
        return redact_diagnostic(value, show_local_paths=show_local_paths, known_paths=paths)
    if isinstance(value, Mapping):
        return {
            key: _redact_diagnostics(item, show_local_paths=show_local_paths, known_paths=paths)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_diagnostics(item, show_local_paths=show_local_paths, known_paths=paths) for item in value]
    return value


def _redact_argument(value: str) -> str:
    """Hide the local path in one argument, whatever spelling carries it.

    `binding_from_arguments` accepts both `--repo-path /checkout` and
    `--repo-path=/checkout`, so a definition may legitimately state its path in
    either. Only the standalone form begins with `/` or `~`, so testing the
    whole argument leaves the joined form fully visible while the payload still
    claims `arguments_redacted`. The name of the option is not private and is
    what makes a redacted argv readable, so only the value after the first `=`
    is replaced.
    """

    if _looks_like_path(value):
        return lane_status.LOCAL_PATH_REDACTION
    name, separator, tail = value.partition("=")
    if separator and _looks_like_path(tail):
        return f"{name}={lane_status.LOCAL_PATH_REDACTION}"
    return value


def redact_arguments(arguments: Sequence[str], *, show_local_paths: bool) -> list[str]:
    """Keep argument *shape* public while hiding every local path inside it.

    Reviewers need to see that the service serves `--repo owner/repo --port
    5332`; nobody outside this machine needs the checkout it serves from.
    """

    if show_local_paths:
        return [str(item) for item in arguments]
    return [_redact_argument(str(item)) for item in arguments]


def definition_digest(text: str) -> str:
    """A stable name for a definition that reveals none of its contents."""

    return definition_digest_bytes(text.encode("utf-8"))


def definition_digest_bytes(raw: bytes) -> str:
    """The same name, computed from the bytes actually on disk.

    For a UTF-8 definition this is identical to the digest of its text, since
    re-encoding a decoded definition reproduces those bytes exactly. For one
    that cannot be decoded at all it is still a stable, distinct name, which is
    what lets an unreadable definition be compared rather than crash.
    """

    return "sha256:" + hashlib.sha256(raw).hexdigest()


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
    module_path_env: str | None = None,
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
    if not is_loopback_host(host_value):
        raise ServiceRequestError("--host must be loopback; use 127.0.0.1 or localhost")
    canonical = Path(repo_path).expanduser().resolve()
    if not canonical.is_dir():
        raise ServiceRequestError("--repo-path must be an existing directory")
    program_args = tuple(str(item) for item in (program or default_program()))
    if not program_args:
        raise ServiceRequestError("service program is empty")
    # A console script carries its own interpreter and package location, so it
    # needs nothing from the environment. The `-m` fallback does: the generated
    # launchd environment keeps only PATH and the service label, and the working
    # directory is the *served repository*, so a child started from a source
    # checkout has no way to reach `code_mower.cli` and the keepalive job would
    # fail and respawn forever. Name the search path in the definition, where it
    # is reviewable, instead of inheriting whatever the installing shell had.
    module_path = module_path_env if module_path_env is not None else ""
    if module_path_env is None and uses_module_entry_point(program_args):
        module_path = module_search_path()
        if not (Path(module_path) / "code_mower" / "__init__.py").is_file():
            raise ServiceRequestError(
                "the module entry point is not importable from a canonical path; install code-mower "
                "so its console script is on PATH, or pass an explicit program"
            )
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
        module_path_env=module_path,
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

    environment = {
        "PATH": spec.path_env,
        "CODE_MOWER_BOARD_SERVICE_LABEL": spec.label,
    }
    # Only a program that needs it carries it, so a console-script definition
    # renders exactly the bytes it always did.
    if spec.module_path_env:
        environment["PYTHONPATH"] = spec.module_path_env
    return {
        "Label": spec.label,
        "ProgramArguments": list(spec.arguments),
        "WorkingDirectory": str(spec.repo_path),
        "RunAtLoad": True,
        "KeepAlive": bool(spec.keepalive),
        "ProcessType": "Background",
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.error_log_path),
        "EnvironmentVariables": environment,
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


def program_arguments(data: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The definition's argument vector, or None when it is not one.

    `plistlib` will happily return whatever the file said, so a syntactically
    valid definition can carry an integer, a boolean or a string where the argv
    belongs. Iterating an integer raises `TypeError`, which is not an `OSError`
    and not a plist parse error, so it would escape both handlers in
    `read_service()` and end enumeration -- `board list`, `board stop`, service
    status and removal -- in a traceback. A string is worse than a crash: it
    iterates into one argument per character and reads as a plausible argv.
    Only a genuine list or tuple of scalars is an argument vector; everything
    else makes this definition unreadable, which is a fact the caller can act
    on.
    """

    raw = data.get("ProgramArguments")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        return None
    arguments = []
    for item in raw:
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            arguments.append(str(item))
        else:
            return None
    return tuple(arguments)


def _service_from_definition(
    label: str, path: Path, data: Mapping[str, Any], *, arguments: tuple[str, ...], digest: str
) -> ManagedService:
    binding = binding_from_arguments(arguments)
    keepalive_value = data.get("KeepAlive")
    keepalive = bool(keepalive_value) if not isinstance(keepalive_value, Mapping) else True
    return ManagedService(
        # The filename label, never the embedded one. They are proved equal
        # before this is reached, and this is the label every mutation -- the
        # `bootout`, the `delete_definition` -- is addressed to.
        label=label,
        definition_path=path,
        arguments=arguments,
        repo=str(binding["repo"]),
        repo_path=str(binding["repo_path"]) or _text(data.get("WorkingDirectory")),
        host=str(binding["host"]) or DEFAULT_HOST,
        port=binding["port"],
        keepalive=keepalive,
        digest=digest,
    )


def _unreadable_service(label: str, path: Path, *, digest: str, message: str) -> ManagedService:
    """A definition that is installed but cannot be understood.

    The label, and therefore the port, still come from the filename; nothing
    else about the definition can be trusted.
    """

    return ManagedService(
        label=label,
        definition_path=path,
        arguments=(),
        repo="",
        repo_path="",
        host=DEFAULT_HOST,
        port=port_from_label(label),
        keepalive=False,
        digest=digest,
        readable=False,
        message=message,
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
        """The installed definition, always carrying the job's runtime state.

        Runtime state comes from the label, which is the filename, so it is just
        as knowable for a definition that cannot be parsed as for one that can.
        A service whose plist was corrupted is still a service launchd is
        supervising under our label, and anything deciding who owns its port has
        to see that.
        """

        path = self.definition_path(label)

        def with_runtime(service: ManagedService) -> ManagedService:
            state, pid = self.runtime_state(label)
            return dataclasses.replace(
                service, loaded=state == JOB_LOADED, pid=pid, load_state=state
            )

        # Read bytes, not text. A binary plist -- or a definition corrupted into
        # invalid UTF-8 -- makes `read_text` raise `UnicodeDecodeError`, which is
        # a `ValueError` and so escapes an `OSError` handler entirely: enumeration
        # and everything built on it (`board list`, `board stop`, service status
        # and removal) would end in a traceback instead of reporting the one fact
        # that matters, which is that this definition cannot be understood.
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return with_runtime(
                _unreadable_service(
                    label,
                    path,
                    digest="",
                    message=f"service definition could not be read ({exc.__class__.__name__})",
                )
            )
        digest = definition_digest_bytes(raw)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            # Parseable or not, a definition that is not UTF-8 text is one this
            # lane cannot round-trip: the rollback that protects a replacement
            # restores the previous definition by writing its text back, so a
            # definition with no text has no recoverable backup. That is exactly
            # what `readable=False` means here, and it is what makes `--replace`
            # the only way to take this one over.
            return with_runtime(
                _unreadable_service(
                    label,
                    path,
                    digest=digest,
                    message="service definition is not UTF-8 text",
                )
            )
        try:
            data = plistlib.loads(raw)
        except Exception:  # noqa: BLE001 - any malformed plist is the same fact
            return with_runtime(
                _unreadable_service(
                    label,
                    path,
                    digest=digest,
                    message="service definition is not a readable plist",
                )
            )
        if not isinstance(data, Mapping):
            return with_runtime(
                _unreadable_service(
                    label,
                    path,
                    digest=digest,
                    message="service definition is not a plist dictionary",
                )
            )
        embedded = _text(data.get("Label"))
        if embedded != label:
            # The filename is what this service is selected by, and the embedded
            # Label is what launchd registers the job as. When they disagree,
            # neither one describes the whole service: returning the embedded
            # label would aim `remove`'s bootout and delete at a *different*
            # installed Board -- unloading and deleting that one while the
            # definition actually selected stayed exactly where it was. The
            # disagreement is the unreadability, and it is reported under the
            # label that was asked for.
            return with_runtime(
                _unreadable_service(
                    label,
                    path,
                    digest=digest,
                    message=(
                        "service definition declares a different Label than its filename, so the "
                        "job launchd registers is not the one this definition names"
                    ),
                )
            )
        arguments = program_arguments(data)
        if arguments is None:
            return with_runtime(
                _unreadable_service(
                    label,
                    path,
                    digest=digest,
                    message="service definition does not carry a list of program arguments",
                )
            )
        return with_runtime(
            _service_from_definition(label, path, data, arguments=arguments, digest=digest)
        )

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

    def _print(self, label: str) -> tuple[str, str | None]:
        """The job's load state, plus its dump when launchd produced one.

        "launchd does not hold this job" and "launchd could not be asked" are
        different facts, and collapsing them into one is what lets a timed-out
        or failed `launchctl print` read as confirmed absence. Only a failure
        launchd itself characterises as a missing job -- `EX_NOTFOUND`, or the
        message it prints for one -- is absence; anything else is unknown, and
        the callers that would otherwise delete a definition or give up a port
        on that answer refuse instead.
        """

        completed = self._run(["launchctl", "print", f"{self.domain}/{label}"])
        if completed is None:
            return JOB_UNKNOWN, None
        if completed.returncode == 0:
            return JOB_LOADED, completed.stdout or ""
        reported = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if completed.returncode == _LAUNCHCTL_NOT_FOUND_CODE or _LAUNCHCTL_NOT_FOUND_RE.search(reported):
            return JOB_ABSENT, None
        return JOB_UNKNOWN, None

    def runtime_state(self, label: str) -> tuple[str, int | None]:
        """`JOB_LOADED`/`JOB_ABSENT`/`JOB_UNKNOWN`, and the supervised pid.

        A zero exit from `launchctl print` is the load state; the pid is only
        present while the job is actually running, so a loaded-but-crashed job
        reports no pid rather than being called healthy.
        """

        state, stdout = self._print(label)
        if state != JOB_LOADED or stdout is None:
            return state, None
        pid_match = _LAUNCHCTL_PID_RE.search(stdout)
        state_match = _LAUNCHCTL_STATE_RE.search(stdout)
        pid = int(pid_match.group(1)) if pid_match else None
        if pid is None and state_match and "running" not in state_match.group(1).lower():
            return JOB_LOADED, None
        return JOB_LOADED, pid

    def runtime(self, label: str) -> tuple[bool, int | None]:
        """Whether launchd is known to hold the job, and the pid it supervises.

        Only a positively loaded job is `True` here, so an unknown answer never
        claims a job is running. Callers deciding whether it is safe to *stop*
        caring about a job must use `_job_load_state()`, which fails the other
        way.
        """

        state, pid = self.runtime_state(label)
        return state == JOB_LOADED, pid

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

        state, stdout = self._print(label)
        if state != JOB_LOADED or stdout is None:
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

    def runtime_state(self, label: str) -> tuple[str, int | None]:
        return JOB_ABSENT, None

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

    # An IPv6 literal has to be bracketed or the colons in the address run into
    # the port and the URL names a different thing entirely. `board._server_url`
    # already does this; a healthy `--host ::1` service would otherwise fail
    # every identity probe and time the health window out.
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"http://{display_host}:{int(port)}/api/identity"
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

    The listeners alone, which is enough to answer "who holds this port" and not
    enough to answer "is this port free": an empty list is also what a host
    returns when the inventory could not be taken. Callers that act on emptiness
    use `port_listener_inventory`.
    """

    return port_listener_inventory(port, command_runner)["listeners"]


def port_listener_inventory(port: int, command_runner: lane_status.CommandRunner) -> dict[str, Any]:
    """`port_listeners`, with whether the host could be asked kept alongside it.

    No listeners and no inventory look identical in a list, and they are
    opposite facts: the first says the port is free, the second says nobody
    knows. Every decision that turns emptiness into an action -- install here,
    replace this, report the port released, pass the binding gate -- reads this
    rather than the bare list, and refuses while `available` is false.
    """

    inventory = lane_status.local_listener_inventory(command_runner)
    listeners = [
        dict(item)
        for item in inventory["listeners"]
        if isinstance(item, Mapping) and int(item.get("port") or -1) == int(port)
    ]
    return {"available": bool(inventory["available"]), "listeners": listeners}


def port_ownership(
    port: int,
    *,
    provider: Any,
    label: str,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> dict[str, Any]:
    """Who holds a port, in the only terms an apply decision may use.

    `free`, `managed_self`, `managed_other` (another Code Mower label),
    `external_supervisor` (a supervised process that is not ours), `foreign`
    (a process we cannot claim), or `unknown` (the local listener inventory
    could not be taken at all). Only `free` and `managed_self` may be replaced.

    Ownership is proved against the pid launchd is supervising, never against a
    definition that merely names the port. A managed job that is stopped or
    crashed leaves its definition installed, and an unrelated process is free to
    take the port it vacated: calling that listener ours would let a restart
    mutate the service instead of refusing, and leave a keepalive job trying
    forever to bind an occupied port. Every listener has to clear that bar --
    one unowned listener on the port is enough to refuse.
    """

    inventory = port_listener_inventory(port, command_runner)
    listeners = inventory["listeners"]
    if not inventory["available"]:
        # Not "nothing holds this port": neither `lsof` nor `ss` could be run,
        # so nothing at all is known about the port. Calling that free would
        # bootstrap a keepalive job straight into a conflict it then retries
        # forever, which is the failure this lifecycle exists to remove.
        return {
            "state": "unknown",
            "pid": None,
            "label": "",
            "detail": "the local listener inventory could not be read, so port occupancy is unknown",
        }
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
    """Restate a reported argument list in the installed definition's terms.

    A pip installation puts a console script at `.../bin/code-mower`, and that
    single path is what the definition names. The script carries a `#!`, so the
    process that is actually forked is the interpreter with the script as its
    first argument, and a provider reporting the exec'd form says
    `/.../python3 /.../bin/code-mower board serve ...`. Both spellings describe
    the same process, so an exact comparison against the definition would fail
    every healthy console-script service.

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
        checks.append(
            _check(
                "service.label",
                "fail",
                redact_diagnostic(
                    service.message or "service definition is unreadable",
                    show_local_paths=show_local_paths,
                    known_paths=(service.definition_path, service.repo_path, spec.repo_path),
                ),
            )
        )
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
        # This is the argv of the job launchd is supervising as `pid`, which is
        # what catches a service that came back on an argument list the
        # definition no longer carries. That the pid is this service's process
        # is proved separately: `service.loaded` takes the pid from launchd,
        # `process.supervisor` and `process.repo_path` read that same pid, and
        # `binding.port` requires it to be the process holding the port.
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
        # The same bar `port_ownership` applies before an apply: every listener
        # on the port has to be this process. Mere membership passes a port the
        # service shares with something else on another address -- a binding
        # `restart` then refuses, and a conflicting listener arriving during
        # delayed health is accepted instead of failing the window.
        inventory = port_listener_inventory(spec.port, command_runner)
        listeners = inventory["listeners"]
        listener_pids = {int(item.get("pid") or -1) for item in listeners}
        port_match = bool(inventory["available"]) and bool(listeners) and listener_pids == {pid}
        if not inventory["available"]:
            port_message = "the local listener inventory could not be read, so the port binding is unproven"
        elif not listeners:
            port_message = "nothing is listening on the expected port"
        elif port_match:
            port_message = "the service process exclusively holds the expected port"
        elif pid in listener_pids:
            port_message = "the expected port is shared with another listening process"
        else:
            port_message = "the expected port is held by another process"
        checks.append(
            _check(
                "binding.port",
                "pass" if port_match else "fail",
                port_message,
                port=spec.port,
                listener_inventory_available=bool(inventory["available"]),
                listener_count=len(listeners),
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
    # Two supported launch modes, one contract. An installed distribution names
    # its version on both sides and they must be equal. A source checkout has no
    # distribution metadata on either side, so the Board reports an empty
    # installed version and the honest expectation is that it stays empty --
    # demanding the imported version there would fail a healthy service forever.
    expected_installed = installed_distribution_version()
    reported_installed = _text(version.get("installed_version"))
    if expected_installed is None:
        installed_ok = reported_installed == ""
        installed_message = (
            "neither side has distribution metadata, as expected for a source checkout"
            if installed_ok
            else "the served Board reports an installed distribution this checkout does not have"
        )
    else:
        installed_ok = reported_installed == expected_installed
        installed_message = (
            "the served installed version matches this installation"
            if installed_ok
            else "the served installed version does not match this installation"
        )
    checks.append(
        _check(
            "binding.installed_version",
            "pass" if installed_ok else "fail",
            installed_message,
            installed_version=reported_installed,
            expected_installed_version=expected_installed or "",
            source_checkout=expected_installed is None,
        )
    )
    # The serving version is the code the process is actually running, and both
    # modes populate it. Comparing it against this Code Mower -- rather than
    # against whatever the Board reports as installed -- is the one comparison
    # that means the same thing in both modes, and `restart_recommended` still
    # carries the Board's own verdict that it is running behind its install.
    reported_serving = _text(version.get("serving_version"))
    serving_ok = reported_serving == CODE_MOWER_VERSION and not version.get("restart_recommended")
    checks.append(
        _check(
            "binding.serving_version",
            "pass" if serving_ok else "fail",
            "the serving version matches this Code Mower"
            if serving_ok
            else "the serving version is stale against this Code Mower",
            serving_version=reported_serving,
            expected_serving_version=CODE_MOWER_VERSION,
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
    if state == "unknown":
        return _operation_payload(
            "listener_inventory_unavailable",
            f"port {spec.port} occupancy could not be checked because neither lsof nor ss could be "
            "run; nothing was changed",
            spec,
            expected,
            show_local_paths=show_local_paths,
        )
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

    # The backup is acquired first, before a directory is created, a job is
    # booted out or a byte is written. `previous_text` is the whole of the
    # rollback: `_rollback` writes it back, and reads an empty one as "there was
    # nothing here", which is why a failed read may never be spelled the same
    # way as a definition that was genuinely absent. A read that fails after the
    # original has already been unloaded and overwritten cannot be retried --
    # the contents are gone by then -- so a definition this run was told is
    # readable, and then cannot read, refuses the whole replacement while
    # everything it would have replaced is still exactly where it was.
    previous_text = ""
    if previous is not None and previous.readable:
        try:
            previous_text = previous.definition_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return _operation_payload(
                "backup_failed",
                (
                    "the installed definition could not be read for rollback "
                    f"({exc.__class__.__name__}); it was left loaded and exactly as it was rather "
                    "than replaced with no way back. Check that its file is readable, then retry."
                ),
                spec,
                expected,
                show_local_paths=show_local_paths,
                known_paths=_provider_known_paths(provider, spec.label),
                installed_readable=True,
            )
    # `previous_text` is now either the exact previous definition, or empty for
    # the two cases where empty is the truth: no definition was installed, or
    # `--replace` is knowingly taking over one that could not be read at all,
    # whose contents were never recoverable and whose refusal said so.

    # Before anything is booted out: launchd cannot start a job whose log files
    # it cannot open, and a filesystem failure must not be discovered after the
    # previously working service has already been stopped.
    log_failure = ensure_log_directories(spec)
    if log_failure:
        return _operation_payload(
            "apply_failed", log_failure, spec, expected, show_local_paths=show_local_paths
        )

    if previous is not None:
        # The definition on disk is the only description of the job launchd is
        # holding, and `write_definition` swaps it atomically: overwriting it
        # while the old job is still loaded loses the original contents and
        # leaves the running service described by a definition that is not its
        # own. The bootstrap below would fail anyway -- launchd will not accept
        # a label its domain already holds -- and the rollback would then
        # preserve the replacement rather than restore an original that is by
        # then gone. So the unload has to be established first, on the same
        # terms `remove` and `_rollback` already use: a bootout that reports
        # success, or an independently confirmed-absent job.
        unloaded, unload_detail = provider.bootout(spec.label)
        if not unloaded:
            still_loaded, _pid = _job_load_state(provider, spec.label)
            if still_loaded:
                return _operation_payload(
                    "unload_failed",
                    (
                        (unload_detail or "the installed service could not be unloaded")
                        + "; its definition was left exactly as it was rather than replaced under a "
                        "job launchd still holds. Run code-mower board service status to see what "
                        "launchd reports for this label, then retry."
                    ),
                    spec,
                    expected,
                    show_local_paths=show_local_paths,
                    known_paths=_provider_known_paths(provider, spec.label),
                )
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
            known_paths=_provider_known_paths(provider, spec.label),
            rollback=restore,
        )
    ok, detail = provider.bootstrap(spec.label)
    if not ok:
        rollback = _rollback(provider, spec, previous_text)
        status = "rollback_failed" if not rollback["ok"] else "apply_failed"
        message = detail if rollback["ok"] else f"{detail}; rollback also failed: {rollback['detail']}"
        return _operation_payload(
            status,
            message,
            spec,
            expected,
            show_local_paths=show_local_paths,
            known_paths=_provider_known_paths(provider, spec.label),
            rollback=rollback,
        )

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
        # Nothing was here before, so rolling back means leaving nothing behind
        # -- and whether that happened is a fact about the host, not about two
        # return values. A job launchd still holds keeps restarting the service
        # that just failed to apply, and a definition that survives starts it
        # again at the next login. Either one means the rollback did not happen,
        # and the caller must say `rollback_failed` rather than `apply_failed`.
        unloaded, unload_detail = provider.bootout(spec.label)
        # The load state decides whether the definition may go, so it is read
        # before anything is deleted. A job launchd still holds -- or one it
        # will not confirm absent -- keeps its definition, because the
        # definition is the only handle `board service status`, `remove` and
        # the `board stop` keepalive guard have on it: deleting it strands a
        # running, self-restarting service outside the inventory entirely.
        # This is the rule `remove_service` already follows.
        still_loaded, _pid = _job_load_state(provider, spec.label)
        if still_loaded:
            return {
                "ok": False,
                "detail": (
                    "could not remove the definition that failed to apply: "
                    + (unload_detail or "launchd still holds the job")
                    + "; its definition was kept so the still-loaded job stays discoverable by "
                    "board service status, remove and the board stop keepalive guard"
                ),
                "restored": False,
                "deleted": False,
            }
        deleted = provider.delete_definition(spec.label)
        present = bool(getattr(provider, "definition_exists", lambda _label: not deleted)(spec.label))
        if present:
            return {
                "ok": False,
                "detail": (
                    "could not remove the definition that failed to apply: its definition is still "
                    "installed and would start it again at the next login"
                ),
                "restored": False,
                "deleted": False,
            }
        return {
            "ok": True,
            "detail": "removed the definition that failed to apply",
            "restored": False,
            "deleted": True,
            "unloaded": unloaded,
        }
    # A bootstrap that reported failure may still have registered the job --
    # timing out after launchd accepted it is exactly that -- so the replacement
    # has to be unloaded before anything is restored. Writing the previous
    # definition over it would leave launchd supervising the replacement while
    # the definition on disk describes the service it replaced, and the
    # restoring bootstrap would fail anyway because the label is already loaded.
    # This is the same ordering the first-install rollback above follows.
    unloaded, unload_detail = provider.bootout(spec.label)
    still_loaded, _pid = _job_load_state(provider, spec.label)
    if still_loaded:
        return {
            "ok": False,
            "detail": (
                "the replacement job could not be unloaded, so the previous definition was left "
                "in place rather than written under a job launchd still supervises: "
                + (unload_detail or "launchd still holds the job")
            ),
            "restored": False,
            "unloaded": unloaded,
        }
    try:
        provider.write_definition(spec.label, previous_text)
    except OSError as exc:
        return {"ok": False, "detail": f"could not restore the previous definition ({exc.__class__.__name__})", "restored": False}
    ok, detail = provider.bootstrap(spec.label)
    return {"ok": ok, "detail": detail or "restored the previous definition", "restored": ok}


def _runtime_arguments_drifted(provider: Any, spec: ServiceSpec) -> bool:
    """Whether launchd is running this label on arguments the plist no longer has.

    Only a concrete argument list launchd reported can say this. A provider that
    cannot be asked, or a job launchd reports no argument list for, leaves the
    question unanswered -- and an unanswered question is not drift, because
    treating it as drift would turn every unreportable job into a replacement.
    The gate fails that service on `process.arguments` either way; what is
    decided here is only whether a restart should reload the definition rather
    than kickstart the registered job.
    """

    reader = getattr(provider, "job_arguments", None)
    if reader is None:
        return False
    try:
        reported = reader(spec.label)
    except (OSError, subprocess.SubprocessError):
        return False
    if reported is None:
        return False
    wanted = tuple(spec.arguments)
    live = tuple(str(item) for item in reported)
    # The same normalization the gate applies, so a console script reported with
    # its interpreter is not mistaken for drift and reloaded on every restart.
    return normalize_live_arguments(live, wanted) != wanted


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
    definition_matches = existing.readable and existing.digest == expected
    # The definition on disk matching the rendered one is not the same fact as
    # launchd *running* it. `launchctl kickstart -k` re-execs the job launchd
    # already registered; it never rereads the plist. A job registered from an
    # earlier definition -- bootstrapped before the file was replaced, or before
    # an upgrade rewrote the argv -- therefore comes back on exactly the
    # arguments that failed the gate last time, and every restart repeats that.
    # The only reload that applies the file is a bootout and a bootstrap, which
    # is what the replacement path below already does under its rollback
    # guarantees.
    runtime_is_stale = definition_matches and existing.loaded and _runtime_arguments_drifted(provider, spec)
    if runtime_is_stale and not replace:
        return _operation_payload(
            "stale_arguments",
            "launchd is running this label on an argument list the installed definition no longer "
            "carries; rerun with --replace to reload the definition into launchd",
            spec,
            expected,
            show_local_paths=show_local_paths,
            installed_repo=existing.repo,
        )
    if definition_matches and not runtime_is_stale:
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
            return _operation_payload(
                "apply_failed",
                detail,
                spec,
                expected,
                show_local_paths=show_local_paths,
                known_paths=_provider_known_paths(provider, spec.label),
            )
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
        succeeded_message=(
            "reloaded the definition launchd was running on stale arguments and validated the binding"
            if runtime_is_stale
            else "replaced the stale managed binding and validated the new one"
        ),
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

    A provider that cannot answer, an answer that fails, and a query that could
    not be made at all all leave the job unconfirmed -- and an unconfirmed job
    is treated as still loaded so nothing destructive proceeds on a guess. Only
    `JOB_ABSENT`, which the provider reports solely for a failure launchd
    characterises as a missing job, releases the definition.
    """

    runtime_state = getattr(provider, "runtime_state", None)
    if runtime_state is not None:
        try:
            state, pid = runtime_state(label)
        except (OSError, subprocess.SubprocessError):
            return True, None
        return state != JOB_ABSENT, pid
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
    ok, raw_detail = provider.bootout(service.label)
    # `remove` builds its payloads here rather than through `_operation_payload`,
    # so the same sanitizing the shared chokepoint does has to happen at the
    # source: every message below is built from this string.
    detail = redact_diagnostic(
        raw_detail,
        show_local_paths=show_local_paths,
        known_paths=(service.definition_path, service.repo_path),
    )
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
    remaining: dict[str, Any] = (
        port_listener_inventory(service.port, command_runner)
        if service.port
        else {"available": True, "listeners": []}
    )
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
    if remaining["listeners"]:
        payload["status"] = "remove_incomplete"
        payload["message"] = (
            f"the definition was removed but port {service.port} is still held; "
            "inspect the remaining listener before starting a replacement"
        )
        return payload
    if not remaining["available"]:
        # The definition is gone, which is real and is reported. "Released its
        # port" is a second claim, and it rests on an inventory that could not
        # be taken -- an operator who reads it as a free port starts a
        # replacement into whatever is still there.
        payload["status"] = "remove_incomplete"
        payload["message"] = (
            "the managed Board service was removed, but port "
            f"{service.port} could not be checked because neither lsof nor ss could be run; "
            "confirm the port is free before starting a replacement"
        )
        return payload
    payload["status"] = "removed"
    payload["message"] = "removed the managed Board service and released its port"
    return payload


def _provider_known_paths(provider: Any, label: str) -> tuple[str, ...]:
    """The definition path a provider diagnostic names, when it can be asked."""

    try:
        return (str(provider.definition_path(label)),)
    except Exception:  # pragma: no cover - a provider that cannot say still redacts
        return ()


def spec_known_paths(spec: ServiceSpec, *extra: object) -> tuple[str, ...]:
    """The local paths this operation put into the definition, plus any given.

    These are the paths a provider diagnostic is most likely to name back, and
    knowing their exact spelling is what lets `redact_diagnostic` replace them
    whole -- spaces included -- instead of falling back to withholding the rest
    of the line.
    """

    candidates: list[object] = [spec.repo_path, spec.log_path, spec.error_log_path, *spec.arguments, *extra]
    return known_path_spellings(candidates)


def _operation_payload(
    status: str,
    message: str,
    spec: ServiceSpec,
    expected_digest: str,
    *,
    delayed: Mapping[str, Any] | None = None,
    show_local_paths: bool = False,
    known_paths: Iterable[object] = (),
    **extra: Any,
) -> dict[str, Any]:
    # Every operation payload passes through here, which makes it the one place
    # a provider diagnostic can be sanitized before it is published. `message`
    # and `rollback.detail` are built from `launchctl` output, which names the
    # definition file by path; without this they print the checkout location in
    # both text and JSON while `repo_path` beside them says it is hidden.
    paths = spec_known_paths(spec, *known_paths)
    payload: dict[str, Any] = {
        "schema": BOARD_SERVICE_SCHEMA,
        "status": status,
        "message": redact_diagnostic(message, show_local_paths=show_local_paths, known_paths=paths),
        "provider": LAUNCHD_PROVIDER,
        "label": spec.label,
        "repo": spec.repo,
        "host": spec.host,
        "port": spec.port,
        "repo_path": redact_path(str(spec.repo_path), show_local_paths=show_local_paths),
        "repo_path_redacted": not show_local_paths,
        "digest": expected_digest,
        **_redact_diagnostics(extra, show_local_paths=show_local_paths, known_paths=paths),
    }
    if delayed is not None:
        payload["delayed_health"] = _redact_diagnostics(
            dict(delayed), show_local_paths=show_local_paths, known_paths=paths
        )
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
            # `loaded: false` alone cannot say whether launchd does not hold the
            # job or merely would not answer, and an operator reading a status
            # needs that apart as much as `board stop` does.
            "load_state": service.load_state,
            "pid": service.pid,
            "digest": service.digest,
            "repo_path": redact_path(service.repo_path, show_local_paths=show_local_paths),
            "repo_path_redacted": not show_local_paths,
            "arguments": redact_arguments(service.arguments, show_local_paths=show_local_paths),
            "arguments_redacted": not show_local_paths,
        }
        if not service.readable:
            row["binding"] = {
                "status": "fail",
                "message": redact_diagnostic(
                    service.message,
                    show_local_paths=show_local_paths,
                    known_paths=(service.definition_path, service.repo_path),
                ),
            }
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
    supported_platform = (
        getattr(active, "name", "") == LAUNCHD_PROVIDER
        and str(getattr(active, "platform", "")).startswith("darwin")
    )
    if not available and not supported_platform:
        # No managed-service implementation exists for this platform at all, so
        # there is nothing installed to enumerate. That is a different answer
        # from the one below.
        return []
    try:
        services = active.list_services()
    except OSError:
        return []
    if available:
        return services
    # macOS, but `launchctl` could not be probed. The definitions are still
    # installed and the jobs they describe may still be running: answering "no
    # managed services" here would let `board stop --yes` signal a listener
    # launchd restarts within moments, which is the same failure the unknown
    # supervision state exists to refuse. Every service keeps its definition and
    # loses its runtime claim, because no answer from this provider about what
    # launchd holds can be trusted while the probe itself fails.
    return [
        dataclasses.replace(service, loaded=False, pid=None, load_state=JOB_UNKNOWN)
        for service in services
    ]


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
