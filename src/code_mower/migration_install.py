#!/usr/bin/env python3
"""Install and command primitives for Code Mower migration rehearsals."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .git_identity import scratch_git_config_commands

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
    def __init__(self, message: str, steps: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.steps = steps


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
    command = [str(venv_python), "-m", "pip", "install"]
    if pip_no_cache:
        command.append("--no-cache-dir")
    if pip_index_url:
        command.extend(["--index-url", pip_index_url])
    for extra_index_url in pip_extra_index_urls or ():
        if extra_index_url:
            command.extend(["--extra-index-url", extra_index_url])
    command.append(package_spec)
    return command


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
) -> subprocess.CompletedProcess[str]:
    if attempts < 1:
        raise ValueError("--pip-install-attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("--pip-retry-delay must be zero or greater")

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
                    raise RehearsalError(
                        (
                            f"pip install failed after {attempts} attempts. "
                            "If this followed a fresh package publish, retry after "
                            "PyPI/TestPyPI propagation or install the local wheel to "
                            "separate source failures from package-index lag."
                        ),
                        steps,
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
