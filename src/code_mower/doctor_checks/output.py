"""Human-readable doctor output rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from .groups import GROUP_LABELS, doctor_check_group_id
from .models import STATUS_FAIL, STATUS_SKIP, STATUS_WARN, DoctorCheck
from .models import DoctorReport
from .models import is_owner_action_check, is_promotion_todo_check


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
    owner_actions = report.owner_actions
    if owner_actions:
        parts.append(f"{owner_actions} owner actions")
    promotion_todos = report.promotion_todos
    if promotion_todos:
        parts.append(f"{promotion_todos} promotion todos")
    if report.warnings:
        parts.append(f"{report.warnings} warnings")
    skipped = sum(1 for check in report.checks if check.status == STATUS_SKIP)
    if skipped:
        parts.append(f"{skipped} skipped")
    if len(parts) == 1:
        parts.append("all passing")
    return ", ".join(parts)


def _format_run_plan(report: DoctorReport) -> str | None:
    if not report.run_plan:
        return None
    stages = []
    for stage in report.run_plan:
        suffix = " optional" if stage.get("optional") else ""
        stages.append(f"{stage.get('id', '')} ({stage.get('group', '')}{suffix})")
    return ", ".join(stages)


def _format_check(check: DoctorCheck) -> list[str]:
    lane = f" [{check.lane}]" if check.lane else ""
    if is_owner_action_check(check):
        status = "OWNER-ACTION"
    elif is_promotion_todo_check(check):
        status = "PROMOTION-TODO"
    else:
        status = check.status.upper()
    lines = [f"- {status} {check.name}{lane}: {check.message}"]
    if check.remediation:
        lines.append(f"  remediation: {check.remediation}")
    return lines


def _adoption_posture_hint(check: DoctorCheck) -> bool:
    return check.name == "doctor.adoption.posture_hint"


def render_doctor_summary(report: DoctorReport) -> str:
    """Render a posture-scoped summary of a doctor report.

    Every check still ran and every check is still in the JSON report: this view
    only chooses what a first run reads first. Active failures and owner actions
    are shown in full, because they are the only entries that ask for an action;
    remaining warnings are counted by group so an intended posture's optional
    providers do not bury them. The full text view stays one flag away and is
    named here rather than assumed.
    """
    lines = [
        "Code Mower doctor (concise)",
        f"Status: {report.status}",
        f"Config: {report.config_path}",
    ]
    if report.profile:
        lines.append(f"Profile: {report.profile}")
    lines.append(f"Checks: {_format_status_summary(report)}")
    lines.append("")
    for check in report.checks:
        if _adoption_posture_hint(check):
            lines.append(
                f"Adoption posture: {check.status.upper()} {check.name}: {check.message}"
            )
            if check.remediation:
                lines.append(f"  remediation: {check.remediation}")
            lines.append("")
            break
    if not report.checks:
        lines.append("No checks ran.")
        return "\n".join(lines) + "\n"

    prioritized = [
        check
        for check in report.checks
        if not _adoption_posture_hint(check)
        and (check.status == STATUS_FAIL or is_owner_action_check(check))
    ]
    if prioritized:
        lines.append("Active failures and owner actions")
        for check in prioritized:
            lines.extend(_format_check(check))
        lines.append("")
    else:
        lines.append("No active failures or owner actions.")
        lines.append("")

    remaining: list[str] = []
    for group_id, checks in _group_checks(report.checks).items():
        counts = []
        promotion_todos = sum(1 for check in checks if is_promotion_todo_check(check))
        warnings = (
            sum(1 for check in checks if check.status == STATUS_WARN)
            - sum(1 for check in checks if is_owner_action_check(check))
            - promotion_todos
        )
        if promotion_todos:
            counts.append(f"{promotion_todos} promotion todos")
        if warnings:
            counts.append(f"{warnings} warnings")
        if counts:
            label = GROUP_LABELS.get(group_id, group_id.title())
            remaining.append(f"- {label}: {', '.join(counts)}")
    if remaining:
        lines.append("Remaining detail by group")
        lines.extend(remaining)
        lines.append("")
    lines.append(
        "Full detail: rerun the same command with --advanced for every check, or "
        "with --json for the complete report."
    )
    return "\n".join(lines) + "\n"


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
    run_plan = _format_run_plan(report)
    if run_plan:
        lines.append(f"Run plan: {run_plan}")
    lines.append(f"Checks: {_format_status_summary(report)}")
    lines.append("")
    # Surface the adoption posture hint before provider-specific warning
    # detail so hosted/orchestrator operators do not mistake default
    # local-provider warnings for their intended posture. JSON check IDs
    # and ordering are unchanged; the hint renders only here, not again
    # in its group below.
    for check in report.checks:
        if _adoption_posture_hint(check):
            lines.append(f"Adoption posture: {check.status.upper()} {check.name}: {check.message}")
            if check.remediation:
                lines.append(f"  remediation: {check.remediation}")
            lines.append("")
            break

    if not report.checks:
        lines.append("No checks ran.")
        return "\n".join(lines) + "\n"

    for group_id, checks in _group_checks(report.checks).items():
        checks = [check for check in checks if not _adoption_posture_hint(check)]
        if not checks:
            continue
        failed = sum(1 for check in checks if check.status == STATUS_FAIL)
        owner_actions = sum(1 for check in checks if is_owner_action_check(check))
        promotion_todos = sum(1 for check in checks if is_promotion_todo_check(check))
        warnings = (
            sum(1 for check in checks if check.status == STATUS_WARN)
            - owner_actions
            - promotion_todos
        )
        summary = []
        if failed:
            summary.append(f"{failed} failed")
        if owner_actions:
            summary.append(f"{owner_actions} owner actions")
        if promotion_todos:
            summary.append(f"{promotion_todos} promotion todos")
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
