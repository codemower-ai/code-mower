"""Per-invocation builder capabilities confined to one dedicated Git checkout."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .lane_delivery import LaneDeliveryError

PROFILE = "code-mower-builder"


def codex_config(checkout: Path) -> list[str]:
    """Use supported permission profiles; never turn off the OS sandbox.

    Explicit .git permission is needed even with a writable checkout. Preserve
    the pre-push hook, its policy, and Git configuration as read-only children.
    No shared Git directory or inherited writable temporary root is granted.
    """
    root = checkout.resolve(strict=True)
    git = root / ".git"
    if (not git.is_dir() or git.is_symlink() or (git / "commondir").exists()
            or git.resolve() != git):
        raise LaneDeliveryError("builder requires a dedicated checkout with local Git metadata")
    entries = {str(root): "write", str(git): "write"}
    for child in (".git/hooks", ".git/config", ".git/code-mower-lane-guard.json", ".codex", ".agents"):
        entries[str(root / child)] = "read"
    filesystem = ",".join(json.dumps(path) + "=" + json.dumps(access) for path, access in entries.items())
    return [f'default_permissions="{PROFILE}"',
            f'permissions.{PROFILE}={{extends=":read-only",filesystem={{{filesystem}}},network={{enabled=true}}}}']


def prepare(checkout: Path, python: str = "") -> dict:
    root = checkout.resolve(strict=True)
    config = codex_config(root)
    executable = shutil.which(python or sys.executable)
    if not executable:
        raise LaneDeliveryError("configured builder Python is unavailable")
    result = subprocess.run(
        [executable, "-c", "import sys; raise SystemExit(sys.version_info < (3, 12))"],
        capture_output=True, timeout=10,
    )
    if result.returncode:
        raise LaneDeliveryError("builder runtime requires Python 3.12 or newer")
    runtime = root / ".code-mower" / "runtime"
    if runtime.is_symlink() or (root / ".code-mower").is_symlink():
        raise LaneDeliveryError("builder runtime directory must remain inside the checkout")
    for child in (runtime / "bin", runtime / "tmp"):
        if child.is_symlink():
            raise LaneDeliveryError("builder runtime directory must not be linked")
        child.mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3"):
        shim = runtime / "bin" / name
        if shim.is_symlink() or (shim.exists() and shim.stat().st_nlink != 1):
            raise LaneDeliveryError("builder runtime shim must not be linked")
        shim.write_text("#!/bin/sh\nexec " + shlex.quote(executable) + ' "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return {"python": executable, "bin_dir": str(runtime / "bin"),
            "tmp_dir": str(runtime / "tmp"), "codex_config": config}


def preflight(checkout: Path, codex: str, config: list[str], python: str) -> None:
    """Prove the installed Codex can enforce this profile before spending a run."""
    import tempfile
    root = checkout.resolve(strict=True)
    # The probe never touches user files: its own marker is removed in finally.
    marker = root / ".git" / ("code-mower-capability-" + os.urandom(8).hex())
    with tempfile.TemporaryDirectory(prefix="code-mower-boundary-", dir=root.parent) as outside:
        forbidden = Path(outside).resolve() / "must-not-write"
        script = (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('probe')\n"
            f"outside = Path({str(forbidden)!r})\n"
            "try:\n    outside.write_text('unexpected')\n"
            "except PermissionError:\n    pass\n"
            "else:\n    raise SystemExit(1)\n"
        )
        argv = [codex, "sandbox", "-P", PROFILE, "-C", str(root)]
        for setting in config:
            argv.extend(["-c", setting])
        try:
            result = subprocess.run([*argv, python, "-c", script],
                                    capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            if result.returncode or not marker.exists() or forbidden.exists():
                raise LaneDeliveryError("Codex Git capability or checkout boundary unverified; update the configured CLI")
        finally:
            marker.unlink(missing_ok=True)
