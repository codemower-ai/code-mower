"""Named doctor check groups and run-plan vocabulary.

The public doctor surface is intentionally simple: callers receive a flat list
of checks. Internally, doctor now has explicit groups and stages so new
diagnostics can be added behind stable names instead of growing another large
command adapter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DoctorCheckGroup:
    id: str
    description: str


@dataclass(frozen=True)
class DoctorCheckStage:
    id: str
    group_id: str
    description: str
    optional: bool = False


DEFAULT_CHECK_GROUPS = (
    DoctorCheckGroup("runtime", "Python, package, and local toolchain checks"),
    DoctorCheckGroup("github", "GitHub auth, workflow, and private-repo cost checks"),
    DoctorCheckGroup("providers", "Reviewer provider CLI and secret checks"),
    DoctorCheckGroup("cloud", "Optional CodeMower.com token and service checks"),
    DoctorCheckGroup("output", "Report, JSON, and human-readable output checks"),
)

BASE_DOCTOR_STAGES = (
    DoctorCheckStage("load-inputs", "runtime", "Load Code Mower config and provider templates"),
    DoctorCheckStage("select-profile", "runtime", "Resolve the requested profile and lane set"),
    DoctorCheckStage("runtime", "runtime", "Inspect Python, pytest, GitHub CLI, and ripgrep"),
    DoctorCheckStage("providers", "providers", "Inspect selected provider lanes"),
)

OPTIONAL_DOCTOR_STAGES = (
    DoctorCheckStage(
        "github",
        "github",
        "Inspect GitHub repository metadata, branch rules, workflows, and Actions cost",
        optional=True,
    ),
    DoctorCheckStage(
        "cloud",
        "cloud",
        "Inspect optional CodeMower.com token setup",
        optional=True,
    ),
)


def default_check_group_ids() -> tuple[str, ...]:
    return tuple(group.id for group in DEFAULT_CHECK_GROUPS)


def build_doctor_run_plan(*, github: bool = False, cloud: bool = False) -> tuple[DoctorCheckStage, ...]:
    """Return the named stages that a doctor run will execute."""

    stages = list(BASE_DOCTOR_STAGES)
    optional_flags = {"github": github, "cloud": cloud}
    stages.extend(stage for stage in OPTIONAL_DOCTOR_STAGES if optional_flags.get(stage.id, False))
    return tuple(stages)
