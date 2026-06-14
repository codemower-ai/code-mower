"""Named doctor check groups.

The current doctor command still orchestrates checks directly, but these group
definitions give future registry-backed checks a stable vocabulary and keep the
CLI output categories from drifting.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DoctorCheckGroup:
    id: str
    description: str


DEFAULT_CHECK_GROUPS = (
    DoctorCheckGroup("runtime", "Python, package, and local toolchain checks"),
    DoctorCheckGroup("github", "GitHub auth, workflow, and private-repo cost checks"),
    DoctorCheckGroup("providers", "Reviewer provider CLI and secret checks"),
    DoctorCheckGroup("cloud", "Optional CodeMower.com token and service checks"),
    DoctorCheckGroup("output", "Report, JSON, and human-readable output checks"),
)


def default_check_group_ids() -> tuple[str, ...]:
    return tuple(group.id for group in DEFAULT_CHECK_GROUPS)
