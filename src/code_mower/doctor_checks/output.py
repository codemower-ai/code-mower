"""Human-readable doctor output rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from .models import STATUS_FAIL, STATUS_SKIP, STATUS_WARN, DoctorCheck
from .models import DoctorReport


_GROUP_LABELS = OrderedDict(
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


def doctor_output_group(check: DoctorCheck) -> str:
    """Return the human-output group for a doctor check."""

    name = check.name
    if name.startswith("github."):
        return "github"
    if name.startswith("cloud."):
        return "cloud"
    if name.startswith("output."):
        return "output"
    if check.lane or name.startswith(("env.", "provider.", "runtime.local_cli")):
        return "providers"
    if name.startswith("runtime."):
        return "runtime"
    if name.startswith(("config.", "provider_templates.", "profile.", "doctor.")):
        return "setup"
    return "other"


def _group_checks(checks: Iterable[DoctorCheck]) -> OrderedDict[str, list[DoctorCheck]]:
    grouped: OrderedDict[str, list[DoctorCheck]] = OrderedDict(
        (group_id, []) for group_id in _GROUP_LABELS
    )
    for check in checks:
        grouped.setdefault(doctor_output_group(check), []).append(check)
    return grouped


def _format_status_summary(report: DoctorReport) -> str:
    parts = [f"{len(report.checks)} total"]
    if report.failures:
        parts.append(f"{report.failures} failed")
    if report.warnings:
        parts.append(f"{report.warnings} warnings")
    skipped = sum(1 for check in report.checks if check.status == STATUS_SKIP)
    if skipped:
        parts.append(f"{skipped} skipped")
    if len(parts) == 1:
        parts.append("all passing")
    return ", ".join(parts)


def _format_check(check: DoctorCheck) -> list[str]:
    lane = f" [{check.lane}]" if check.lane else ""
    lines = [f"- {check.status.upper()} {check.name}{lane}: {check.message}"]
    if check.remediation:
        lines.append(f"  remediation: {check.remediation}")
    return lines


def render_doctor_text(report: DoctorReport) -> str:
    """Render a doctor report for terminal output."""
    lines = [
        "Code Mower doctor",
        f"Status: {report.status}",
        f"Config: {report.config_path}",
        f"Provider templates: {report.provider_templates_path}",
    ]
    if report.profile:
        lines.append(f"Profile: {report.profile}")
    lines.append(f"Checks: {_format_status_summary(report)}")
    lines.append("")

    if not report.checks:
        lines.append("No checks ran.")
        return "\n".join(lines) + "\n"

    for group_id, checks in _group_checks(report.checks).items():
        if not checks:
            continue
        failed = sum(1 for check in checks if check.status == STATUS_FAIL)
        warnings = sum(1 for check in checks if check.status == STATUS_WARN)
        summary = []
        if failed:
            summary.append(f"{failed} failed")
        if warnings:
            summary.append(f"{warnings} warnings")
        heading = _GROUP_LABELS.get(group_id, group_id.title())
        if summary:
            heading = f"{heading} ({', '.join(summary)})"
        lines.append(heading)
        for check in checks:
            lines.extend(_format_check(check))
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
