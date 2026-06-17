"""Doctor check grouping helpers shared by JSON and text output."""

from __future__ import annotations

from collections import OrderedDict


GROUP_LABELS = OrderedDict(
    (
        ("setup", "Setup"),
        ("runtime", "Runtime"),
        ("providers", "Provider lanes"),
        ("github", "GitHub"),
        ("cloud", "Code Mower Cloud"),
        ("output", "Output"),
        ("other", "Other"),
    )
)


def doctor_check_group_id(name: str, lane: str | None = None) -> str:
    """Return the stable reporting group for a doctor check name."""

    if name.startswith("github."):
        return "github"
    if name.startswith("cloud."):
        return "cloud"
    if name.startswith("output."):
        return "output"
    if lane or name.startswith(("env.", "provider.", "runtime.local_cli")):
        return "providers"
    if name.startswith("runtime."):
        return "runtime"
    if name.startswith(("config.", "provider_templates.", "profile.", "doctor.")):
        return "setup"
    return "other"
