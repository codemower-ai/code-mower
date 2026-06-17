"""Human-readable doctor output rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from .groups import GROUP_LABELS, doctor_check_group_id
from .models import STATUS_FAIL, STATUS_SKIP, STATUS_WARN, DoctorCheck
from .models import DoctorReport


def doctor_output_group(check: DoctorCheck) -> str:
    """Return the human-output group for a doctor check."""

    return doctor_check_group_id(check.name, check.lane)


def _group_checks(checks: Iterable[DoctorCheck]) -> OrderedDict[str, list[DoctorCheck]]:
    grouped: OrderedDict[str, list[DoctorCheck]] = OrderedDict(
        (group_id, []) for group_id in GROUP_LABELS
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
        heading = GROUP_LABELS.get(group_id, group_id.title())
        if summary:
            heading = f"{heading} ({', '.join(summary)})"
        lines.append(heading)
        for check in checks:
            lines.extend(_format_check(check))
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
