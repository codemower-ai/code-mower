"""Human-readable doctor output rendering."""

from __future__ import annotations

from .models import DoctorReport


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
    lines.extend(
        [
            f"Checks: {len(report.checks)} ({report.failures} failed, {report.warnings} warnings)",
            "",
        ]
    )
    for check in report.checks:
        lane = f" [{check.lane}]" if check.lane else ""
        lines.append(f"- {check.status.upper()} {check.name}{lane}: {check.message}")
        if check.remediation:
            lines.append(f"  remediation: {check.remediation}")
    return "\n".join(lines) + "\n"
