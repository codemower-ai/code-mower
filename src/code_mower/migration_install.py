#!/usr/bin/env python3
"""Install and command primitives for Code Mower migration rehearsals."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .git_identity import scratch_git_config_commands

#: Closed taxonomy for package-install failure reasons. Always one of these
#: stable codes; never raw output, paths, or secrets. Used only when
#: package_install fails; omitted for pass/warn/unavailable/planned.
PACKAGE_INSTALL_FAILURE_REASONS = frozenset(
    {
        "network",  # DNS, connection, timeout, proxy, SSL errors
        "package_index",  # 404, index propagation, malformed index response
        "runtime",  # Python version, missing system library, incompatible deps
        "sandbox_permission",  # Permission denied, disk full, OS-level sandbox denial
        "unknown",  # Unclassifiable failures
    }
)

# Grammar for the one exact package-index spec shape a candidate-only download
# accepts: <name>==<version>. Anything else (ranges, extras, paths, URLs) has
# no single artifact a downloaded file could be checked against, so it is
# refused rather than downloaded.
_EXACT_NAME_VERSION_SPEC_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==(?P<version>[A-Za-z0-9.]+)$"
)
# Wheel and sdist filenames as pip/setuptools emit them: the distribution name
# and version are escaped (runs of ``-_.`` collapsed to ``_``) ahead of a
# fixed suffix -- wheel tags for a wheel, ``.tar.gz``/``.zip`` for an sdist.
_WHEEL_FILENAME_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)"
    r"(?:-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$"
)
_SDIST_FILENAME_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)\.(?:tar\.gz|zip)$"
)
_PIP_SOURCE_RESET_ARGS = (
    "--extra-index-url",
    "",
    "--find-links",
    "",
)

MIRRORED_IMPLEMENTATION_PATTERNS = (
    "tools/code_mower_*.py",
    "tools/*_audit_pr.py",
    "tools/*_labeler.py",
    "tools/lane_prompts/*.md",
    "tools/calibration_corpus*.json",
    "tools/reviewer_spend*.json",
    "tools/context_packs*.json",
    "tools/CODE_MOWER*.md",
)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout_sha256: str
    stderr_preview: str
    stdout_preview: str

    @classmethod
    def from_completed(
        cls,
        command: Sequence[str],
        completed: subprocess.CompletedProcess[str],
    ) -> "CommandResult":
        return cls(
            command=tuple(command),
            returncode=int(completed.returncode),
            stdout_sha256=hashlib.sha256(
                completed.stdout.encode("utf-8", errors="replace")
            ).hexdigest(),
            stdout_preview=completed.stdout[:800],
            stderr_preview=completed.stderr[:1200],
        )


@dataclass(frozen=True)
class RunOutput:
    public: CommandResult
    stdout: str


class RehearsalError(RuntimeError):
    def __init__(
        self, message: str, steps: list[dict[str, Any]], *, failure_reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.steps = steps
        self.failure_reason = failure_reason


def classify_package_install_failure(*, exception: Exception, steps: list[dict[str, Any]]) -> str:
    """Classify a package-install failure into a closed reason taxonomy.

    Returns one of PACKAGE_INSTALL_FAILURE_REASONS: network, package_index,
    runtime, sandbox_permission, or unknown. Never returns raw output, paths,
    or authentication details.
    """
    # Collect stderr/stdout previews from the provided step slice
    error_text = str(exception).lower()
    for step in steps:
        if isinstance(step, dict):
            error_text += " " + step.get("stderr_preview", "").lower()[:2000]
            error_text += " " + step.get("stdout_preview", "").lower()[:2000]

    # Network errors: DNS, connection refused, timeouts, SSL, proxy
    # Note: Generic timeouts are NOT included here; classify them as network
    # only when accompanied by network-specific evidence below.
    network_indicators = (
        "connection refused",
        "connection timed out",
        "connection reset",
        "name or service not known",
        "nodename nor servname provided",
        "could not resolve host",
        "temporary failure in name resolution",
        "network is unreachable",
        "ssl",
        "certificate verify failed",
        "max retries exceeded",
        "http error 50",  # 500-level server errors
        "http error 52",
        "http error 53",
        "proxy",
        "[errno 111]",  # ECONNREFUSED
        "[errno 110]",  # ETIMEDOUT
        "[errno 113]",  # EHOSTUNREACH
    )

    # Package index errors: 404, propagation delay, malformed responses
    package_index_indicators = (
        "http error 404",
        "could not find a version",
        "no matching distribution found",
        "error 404",
        "package not found",
        "index error",
        "invalid package",
        "malformed",
    )

    # Runtime errors: Python version, missing libraries, incompatible deps
    runtime_indicators = (
        "python version",
        "requires python",
        "unsupported python",
        "no module named",
        "importerror",
        "modulenotfounderror",
        "cannot import",
        "syntax error",
        "invalid syntax",
        "incompatible",
        "dependency",
        "version conflict",
        "requires a different version",
        "libc",
        "glibc",
        ".so",
        "shared library",
    )

    # Sandbox/permission errors: permission denied, disk full, OS restrictions
    sandbox_permission_indicators = (
        "permission denied",
        "[errno 13]",  # EACCES
        "[errno 1]",  # EPERM
        "disk full",
        "no space left",
        "[errno 28]",  # ENOSPC
        "read-only file system",
        "[errno 30]",  # EROFS
        "operation not permitted",
    )

    # Check in priority order: most specific first
    if any(indicator in error_text for indicator in sandbox_permission_indicators):
        return "sandbox_permission"

    # Check for any HTTP 5xx server error (500-599)
    if re.search(r"http error 5\d\d", error_text):
        return "network"

    has_network_evidence = any(indicator in error_text for indicator in network_indicators)

    if has_network_evidence:
        return "network"

    if any(indicator in error_text for indicator in package_index_indicators):
        return "package_index"

    if any(indicator in error_text for indicator in runtime_indicators):
        return "runtime"

    return "unknown"


def _default_product_rehearsal_local_command(repo_path: Path) -> tuple[str, ...]:
    """Prefer local fallback before mirror removal, wrapper default after it."""

    wrapper = repo_path / "tools" / "code_mower"
    if not wrapper.is_file():
        return ("env", "CODE_MOWER_USE_LOCAL=1", "tools/code_mower")
    mirrored_candidates = _glob_relative_files(repo_path, MIRRORED_IMPLEMENTATION_PATTERNS)
    if mirrored_candidates:
        return ("env", "CODE_MOWER_USE_LOCAL=1", "tools/code_mower")
    return ("tools/code_mower",)


def _venv_python(venv_dir: Path) -> Path:
    unix_python = venv_dir / "bin" / "python"
    if unix_python.exists():
        return unix_python
    return venv_dir / "Scripts" / "python.exe"


def _venv_code_mower(venv_dir: Path) -> Path:
    if os.name != "nt":
        return venv_dir / "bin" / "code-mower"
    return venv_dir / "Scripts" / "code-mower.exe"


def _run(command: Sequence[str], *, cwd: Path, timeout: int) -> RunOutput:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return RunOutput(
        public=CommandResult.from_completed(command, completed),
        stdout=completed.stdout,
    )


def _run_rehearsal_step(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    steps: list[dict[str, Any]],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    step = {
        "command": list(command),
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout_preview": completed.stdout[-4000:],
        "stderr_preview": completed.stderr[-4000:],
    }
    steps.append(step)
    if completed.returncode != 0:
        raise RehearsalError(
            f"command failed: {' '.join(str(part) for part in command)}",
            steps,
        )
    return completed


def _run_rehearsal_step_to_file(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    steps: list[dict[str, Any]],
    timeout: int,
    stdout_path: Path,
) -> subprocess.CompletedProcess[str]:
    completed = _run_rehearsal_step(
        command,
        cwd=cwd,
        env=env,
        steps=steps,
        timeout=timeout,
    )
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    return completed


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_release_readiness() -> Any:
    """Load the release-readiness helper without breaking legacy tools imports."""

    if __package__ in {None, ""}:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    module_names: list[str] = []
    if __package__:
        module_names.append(f"{__package__}.release_readiness")
    module_names.extend(["code_mower.release_readiness", "release_readiness"])
    last_error: ImportError | None = None
    for module_name in module_names:
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
    raise ImportError("unable to import release_readiness helper") from last_error


def _resolve_install_package_spec(package_spec: str, *, base_dir: Path | None = None) -> str:
    candidate_text = package_spec.strip()
    if not candidate_text:
        return package_spec
    if candidate_text.startswith(("git+", "http://", "https://")):
        return package_spec
    looks_path_like = (
        candidate_text.startswith((".", "/", "~"))
        or os.sep in candidate_text
        or (os.altsep is not None and os.altsep in candidate_text)
    )
    if not looks_path_like:
        return package_spec

    base = (base_dir or Path.cwd()).expanduser().resolve()
    candidate = Path(candidate_text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        return package_spec
    return str(candidate)


def _package_spec_uses_package_index(package_spec: str) -> bool:
    candidate_text = package_spec.strip()
    if not candidate_text:
        return False
    if candidate_text.startswith(("git+", "http://", "https://", "file://")):
        return False
    if "://" in candidate_text:
        return False
    looks_path_like = (
        candidate_text.startswith((".", "/", "~"))
        or os.sep in candidate_text
        or "/" in candidate_text
        or "\\" in candidate_text
        or (os.altsep is not None and os.altsep in candidate_text)
    )
    return not looks_path_like


def _pip_upgrade_command(venv_python: Path) -> list[str]:
    return [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"]


def _pip_install_command(
    venv_python: Path,
    package_spec: str,
    *,
    pip_index_url: str = "",
    pip_extra_index_urls: Sequence[str] | None = None,
    pip_no_cache: bool = False,
) -> list[str]:
    extra_index_urls = tuple(url for url in pip_extra_index_urls or () if url)
    source_isolated = bool(pip_index_url or extra_index_urls)
    command = [str(venv_python), "-m", "pip"]
    if source_isolated:
        command.append("--isolated")
    command.append("install")
    if pip_no_cache:
        command.append("--no-cache-dir")
    if pip_index_url:
        command.extend(["--index-url", pip_index_url])
    if source_isolated:
        command.extend(_PIP_SOURCE_RESET_ARGS)
    for extra_index_url in extra_index_urls:
        command.extend(["--extra-index-url", extra_index_url])
    command.append(package_spec)
    return command


def _normalize_distribution_name(name: str) -> str:
    """PEP 503 normalization, so a spec and a downloaded filename agree on identity."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _parse_exact_name_version_spec(package_spec: str) -> tuple[str, str]:
    """Parse a candidate-only download spec into ``(identity, version)``.

    Only the exact ``<name>==<version>`` shape has one artifact a downloaded
    file can be checked against; anything else is refused rather than
    downloaded, matching :func:`_package_spec_uses_package_index`'s refusal of
    inexact specs elsewhere in this module.
    """
    match = _EXACT_NAME_VERSION_SPEC_PATTERN.match(package_spec.strip())
    if not match:
        raise ValueError(
            "candidate-only download requires an exact <name>==<version> spec, "
            f"got: {package_spec!r}"
        )
    return _normalize_distribution_name(match.group("name")), match.group("version")


def _parse_downloaded_artifact_identity(filename: str) -> tuple[str, str]:
    """Parse a downloaded wheel/sdist filename into ``(identity, version)``.

    Raises ``ValueError`` for any filename that is not a recognized wheel or
    sdist shape, so a candidate-only download can fail closed on a malformed
    artifact instead of accepting it on faith.
    """
    match = _WHEEL_FILENAME_PATTERN.match(filename) or _SDIST_FILENAME_PATTERN.match(filename)
    if not match:
        raise ValueError(
            f"downloaded candidate artifact has an unrecognized filename: {filename!r}"
        )
    return _normalize_distribution_name(match.group("name")), match.group("version")


def _pip_download_candidate_command(
    venv_python: Path,
    package_spec: str,
    *,
    index_url: str,
    dest_dir: Path,
    pip_no_cache: bool = False,
) -> list[str]:
    """Download the exact candidate artifact with ``index_url`` as the *only* index.

    No ``--extra-index-url`` is ever added here: pip does not prioritize
    ``--index-url`` over ``--extra-index-url``, so a single combined install
    command cannot prove which configured index actually supplied a
    candidate. Passing exactly one index -- and ``--no-deps``, since
    dependencies are not part of this release candidate -- is what makes the
    proof possible: nothing but ``index_url`` can satisfy this command.
    """
    command = [str(venv_python), "-m", "pip", "--isolated", "download", "--no-deps"]
    if pip_no_cache:
        command.append("--no-cache-dir")
    command.extend(["--index-url", index_url, "--dest", str(dest_dir)])
    command.extend(_PIP_SOURCE_RESET_ARGS)
    command.append(package_spec)
    return command


def _pip_install_local_artifact_command(
    venv_python: Path,
    artifact_path: Path,
    *,
    dependency_index_url: str = "",
    pip_no_cache: bool = False,
) -> list[str]:
    """Install an already-downloaded local artifact file, not a name/version spec.

    The candidate's own distribution comes from the local file, so its
    identity is not subject to index resolution at all here; only its
    dependencies -- which are not part of this release candidate -- resolve
    against ``dependency_index_url``.
    """
    command = [str(venv_python), "-m", "pip", "--isolated", "install"]
    if pip_no_cache:
        command.append("--no-cache-dir")
    if dependency_index_url:
        command.extend(["--index-url", dependency_index_url])
    command.extend(_PIP_SOURCE_RESET_ARGS)
    command.append(str(artifact_path))
    return command


def _isolated_pip_environment() -> dict[str, str]:
    """Return the process environment with ambient pip index policy disabled."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def _preview_timeout_output(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")[-4000:]
    return output[-4000:]


def _annotate_pip_install_attempt(
    steps: list[dict[str, Any]],
    *,
    command: Sequence[str],
    cwd: Path,
    attempt: int,
    attempts: int,
    timeout: int,
    timeout_error: subprocess.TimeoutExpired | None = None,
) -> None:
    if timeout_error is not None:
        steps.append(
            {
                "command": list(command),
                "cwd": str(cwd),
                "returncode": -1,
                "stdout_preview": _preview_timeout_output(timeout_error.stdout),
                "stderr_preview": _preview_timeout_output(timeout_error.stderr),
                "timeout_seconds": timeout,
                "error": "timeout expired",
            }
        )
    steps[-1]["pip_install_attempt"] = attempt
    steps[-1]["pip_install_max_attempts"] = attempts
    steps[-1]["pip_cache_disabled"] = "--no-cache-dir" in command


def _run_pip_install_with_retries(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    steps: list[dict[str, Any]],
    timeout: int,
    attempts: int,
    retry_delay_seconds: float,
    package_index: bool,
    command_label: str = "pip install",
) -> subprocess.CompletedProcess[str]:
    if attempts < 1:
        raise ValueError("--pip-install-attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("--pip-retry-delay must be zero or greater")

    # Capture boundary before this retry group to isolate classification
    attempt_steps_start = len(steps)

    for attempt in range(1, attempts + 1):
        try:
            completed = _run_rehearsal_step(
                command,
                cwd=cwd,
                env=env,
                steps=steps,
                timeout=timeout,
            )
            _annotate_pip_install_attempt(
                steps,
                command=command,
                cwd=cwd,
                attempt=attempt,
                attempts=attempts,
                timeout=timeout,
            )
            return completed
        except (RehearsalError, subprocess.TimeoutExpired) as exc:
            _annotate_pip_install_attempt(
                steps,
                command=command,
                cwd=cwd,
                attempt=attempt,
                attempts=attempts,
                timeout=timeout,
                timeout_error=exc if isinstance(exc, subprocess.TimeoutExpired) else None,
            )
            if attempt >= attempts:
                if package_index:
                    failure_reason = classify_package_install_failure(
                        exception=exc, steps=steps[attempt_steps_start:]
                    )
                    raise RehearsalError(
                        (
                            f"{command_label} failed after {attempts} attempts. "
                            "If this followed a fresh package publish, retry after "
                            "PyPI/TestPyPI propagation or install the local wheel to "
                            "separate source failures from package-index lag."
                        ),
                        steps,
                        failure_reason=failure_reason,
                    ) from exc
                raise
            steps[-1]["retry_scheduled_seconds"] = retry_delay_seconds
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)

    raise AssertionError("pip install retry loop exited unexpectedly")


def _write_public_rehearsal_toy_repo(
    toy_repo: Path,
    *,
    steps: list[dict[str, Any]],
    env: dict[str, str],
    timeout: int,
) -> None:
    toy_repo.mkdir(parents=True)
    git = shutil.which("git")
    if not git:
        (toy_repo / "README.md").write_text(
            "# Code Mower package-install rehearsal\n",
            encoding="utf-8",
        )
        return
    _run_rehearsal_step([git, "init", "-q"], cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    for command in scratch_git_config_commands(git):
        _run_rehearsal_step(list(command), cwd=toy_repo, env=env, steps=steps, timeout=timeout)
    (toy_repo / "README.md").write_text(
        "# Code Mower package-install rehearsal\n",
        encoding="utf-8",
    )
    _run_rehearsal_step(
        [git, "add", "README.md"], cwd=toy_repo, env=env, steps=steps, timeout=timeout
    )
    _run_rehearsal_step(
        [git, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "Initial rehearsal repo"],
        cwd=toy_repo,
        env=env,
        steps=steps,
        timeout=timeout,
    )


def _json_payload(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _glob_relative_files(repo_path: Path, patterns: Sequence[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        for path in repo_path.glob(pattern):
            if path.is_file():
                found.add(path.relative_to(repo_path).as_posix())
    return sorted(found)
