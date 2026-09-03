"""Supervised-pilot doctor readiness checks."""

from __future__ import annotations

from typing import Mapping, Sequence

from code_mower import lane_status

from .models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    DoctorCheck,
    is_owner_action_check,
    is_promotion_todo_check,
)

PROMOTION_MODE_VALUES = frozenset({"manual", "promoted"})
CODE_MOWER_GATE_CONTEXT = "code-mower/gate"


def _check_name(check: DoctorCheck) -> str:
    return f"{check.name}:{check.lane}" if check.lane else check.name


def _detail_value(check: DoctorCheck, key: str) -> str:
    if not isinstance(check.detail, Mapping):
        return ""
    return str(check.detail.get(key) or "")


def _check_by_name(checks: Sequence[DoctorCheck], name: str) -> DoctorCheck | None:
    return next((check for check in checks if check.name == name), None)


def _checks_by_name(checks: Sequence[DoctorCheck], name: str) -> list[DoctorCheck]:
    return [check for check in checks if check.name == name]


def _promotion_todos_from_checks(checks: Sequence[DoctorCheck]) -> list[dict[str, str]]:
    todos: list[dict[str, str]] = []
    for check in checks:
        if is_promotion_todo_check(check):
            todos.append(
                {
                    "id": _check_name(check),
                    "kind": _detail_value(check, "promotion_todo_kind"),
                    "message": check.message,
                    "remediation": check.remediation or "",
                }
            )
            continue
        if check.name == "github.branch_protection" and check.status != STATUS_PASS:
            todos.append(
                {
                    "id": check.name,
                    "kind": _detail_value(check, "owner_action_kind")
                    or "branch_protection_gate_requirement",
                    "message": check.message,
                    "remediation": check.remediation or "",
                }
            )
        elif check.name == "github.repo.auto_merge" and check.status != STATUS_PASS:
            todos.append(
                {
                    "id": check.name,
                    "kind": "repo_auto_merge",
                    "message": check.message,
                    "remediation": check.remediation or "",
                }
            )
    return todos


def _owner_actions_from_checks(checks: Sequence[DoctorCheck]) -> list[dict[str, str]]:
    return [
        {
            "id": _check_name(check),
            "kind": _detail_value(check, "owner_action_kind"),
            "message": check.message,
            "remediation": check.remediation or "",
        }
        for check in checks
        if is_owner_action_check(check)
    ]


def _regular_warnings_from_checks(checks: Sequence[DoctorCheck]) -> list[dict[str, str]]:
    return [
        {
            "id": _check_name(check),
            "message": check.message,
            "remediation": check.remediation or "",
        }
        for check in checks
        if check.status == STATUS_WARN
        and not is_owner_action_check(check)
        and not is_promotion_todo_check(check)
        and not check.name.startswith("supervised_pilot.")
    ]


def _failures_from_checks(checks: Sequence[DoctorCheck]) -> list[dict[str, str]]:
    return [
        {
            "id": _check_name(check),
            "message": check.message,
            "remediation": check.remediation or "",
        }
        for check in checks
        if check.status == STATUS_FAIL and not check.name.startswith("supervised_pilot.")
    ]


def _gate_required(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "github.branch_protection")
    raw_contexts = (
        check.detail.get("required_status_contexts")
        if check is not None and isinstance(check.detail, Mapping)
        else []
    )
    contexts = raw_contexts if isinstance(raw_contexts, Sequence) and not isinstance(raw_contexts, str) else []
    return (
        check is not None
        and check.status == STATUS_PASS
        and (
            _detail_value(check, "required_status_context") == CODE_MOWER_GATE_CONTEXT
            or CODE_MOWER_GATE_CONTEXT in contexts
        )
    )


def _auto_merge_enabled(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "github.repo.auto_merge")
    return (
        check is not None
        and check.status == STATUS_PASS
        and isinstance(check.detail, Mapping)
        and bool(check.detail.get("allow_auto_merge"))
    )


def _merge_credential_present(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "github.gate_automerge_token")
    return check is not None and check.status == STATUS_PASS


def _trusted_authors_ready(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "doctor.adoption.trusted_authors")
    return check is not None and check.status == STATUS_PASS


def _owner_login_ready(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "doctor.adoption.owner_login")
    return check is not None and check.status == STATUS_PASS


def _dispatch_token_ready(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "github.human_automation_token")
    return check is not None and check.status == STATUS_PASS


def _cloud_ready(checks: Sequence[DoctorCheck]) -> bool:
    check = _check_by_name(checks, "cloud.token")
    return check is not None and check.status == STATUS_PASS


def _local_runner_ready(checks: Sequence[DoctorCheck]) -> bool:
    runner_checks = [
        check
        for check in checks
        if check.name.startswith("actions.runner.")
        or check.name.startswith("runtime.runner_")
        or check.name == "runner.workflow_labels"
    ]
    return bool(runner_checks) and not any(
        check.status in {STATUS_FAIL, STATUS_WARN} for check in runner_checks
    )


def _local_cli_blockers(checks: Sequence[DoctorCheck]) -> list[str]:
    return [
        _check_name(check)
        for check in checks
        if check.name in {"runtime.local_cli", "runtime.local_cli.probe"}
        and check.status in {STATUS_FAIL, STATUS_WARN}
    ]


def check_supervised_pilot_board_visibility(
    *,
    command_runner: lane_status.CommandRunner | None = None,
) -> DoctorCheck:
    """Report whether a local Code Mower Board listener is visible."""

    collector = lane_status.collect_local_boards
    boards_payload = collector(command_runner) if command_runner else collector()
    boards = boards_payload.get("boards") if isinstance(boards_payload, Mapping) else []
    visible = bool(boards)
    safe_boards = []
    if isinstance(boards, list):
        for board in boards[:5]:
            if not isinstance(board, Mapping):
                continue
            safe_boards.append(
                {
                    "port": board.get("port"),
                    "process": str(board.get("process") or ""),
                    "confidence": str(board.get("confidence") or ""),
                }
            )
    return DoctorCheck(
        name="supervised_pilot.board_visibility",
        status=STATUS_PASS if visible else STATUS_WARN,
        message=(
            "local Code Mower Board listener is visible"
            if visible
            else "local Code Mower Board listener is not visible"
        ),
        detail={
            "board_visible": visible,
            "board_count": len(safe_boards),
            "boards": safe_boards,
            "local_paths_redacted": True,
        },
        remediation=None
        if visible
        else "Run `code-mower board serve --repo OWNER/REPO` for a local read-only operator view.",
    )


def check_supervised_pilot_runner_posture(
    checks: Sequence[DoctorCheck],
    *,
    adoption_posture: str,
) -> DoctorCheck:
    """Summarize whether this host is intended to run local lanes."""

    local_cli_blockers = _local_cli_blockers(checks)
    runner_ready = _local_runner_ready(checks)
    if adoption_posture in {"hosted-builders", "orchestrator-only"}:
        return DoctorCheck(
            name="supervised_pilot.runner_posture",
            status=STATUS_PASS,
            message=f"{adoption_posture} posture does not require local reviewer CLIs",
            detail={
                "adoption_posture": adoption_posture,
                "local_runner_required_here": False,
                "local_runner_ready": runner_ready,
                "local_cli_blockers": local_cli_blockers,
            },
        )
    if runner_ready or not local_cli_blockers:
        return DoctorCheck(
            name="supervised_pilot.runner_posture",
            status=STATUS_PASS,
            message="local reviewer runner posture is ready for this host",
            detail={
                "adoption_posture": adoption_posture,
                "local_runner_required_here": True,
                "local_runner_ready": runner_ready,
                "local_cli_blockers": local_cli_blockers,
            },
        )
    return DoctorCheck(
        name="supervised_pilot.runner_posture",
        status=STATUS_WARN,
        message="local reviewer runner posture is not ready on this host",
        detail={
            "adoption_posture": adoption_posture,
            "local_runner_required_here": True,
            "local_runner_ready": runner_ready,
            "local_cli_blockers": local_cli_blockers,
        },
        remediation=(
            "Use `--orchestrator-only` or `--hosted-builders` on observer hosts, "
            "or finish local CLI and self-hosted runner setup on the machine that "
            "will run reviewer lanes."
        ),
    )


def check_supervised_pilot_readiness(
    checks: Sequence[DoctorCheck],
    *,
    repo_slug: str,
    pilot_mode: str,
    adoption_posture: str,
) -> DoctorCheck:
    """Summarize the repo-level supervised-pilot readiness posture."""

    mode = pilot_mode if pilot_mode in PROMOTION_MODE_VALUES else "manual"
    failures = _failures_from_checks(checks)
    owner_actions = _owner_actions_from_checks(checks)
    warnings = _regular_warnings_from_checks(checks)
    promotion_todos = _promotion_todos_from_checks(checks)
    promoted_ready = (
        not failures
        and not owner_actions
        and not promotion_todos
        and _gate_required(checks)
        and _auto_merge_enabled(checks)
        and _merge_credential_present(checks)
    )
    manual_ready = not failures
    if mode == "promoted" and not promoted_ready:
        status = STATUS_FAIL
        message = "promoted supervised pilot is not ready"
    elif not manual_ready:
        status = STATUS_FAIL
        message = "manual supervised pilot is blocked"
    elif owner_actions or warnings or promotion_todos:
        status = STATUS_WARN
        message = "manual supervised pilot is usable with follow-up items"
    else:
        status = STATUS_PASS
        message = "supervised pilot readiness checks are green"
    next_steps: list[str] = []
    if failures:
        next_steps.append("fix_blockers")
    if owner_actions:
        next_steps.append("complete_owner_actions")
    if promotion_todos:
        next_steps.append("finish_promotion_todos_before_auto_merge")
    if warnings:
        next_steps.append("review_warnings")
    if not next_steps:
        next_steps.append("run_controller_dry_run")
    return DoctorCheck(
        name="supervised_pilot.readiness",
        status=status,
        message=message,
        detail={
            "repo": repo_slug,
            "pilot_mode": mode,
            "adoption_posture": adoption_posture,
            "manual_pilot_ready": manual_ready,
            "promoted_pilot_ready": promoted_ready,
            "required_gate_ready": _gate_required(checks),
            "auto_merge_ready": _auto_merge_enabled(checks),
            "merge_credential_ready": _merge_credential_present(checks),
            "owner_login_ready": _owner_login_ready(checks),
            "trusted_authors_ready": _trusted_authors_ready(checks),
            "dispatch_token_ready": _dispatch_token_ready(checks),
            "cloud_token_ready": _cloud_ready(checks),
            "blockers": failures,
            "owner_actions": owner_actions,
            "promotion_todos": promotion_todos,
            "warnings": warnings,
            "next_steps": next_steps,
            "setup_urls": {
                "actions_secrets": f"https://github.com/{repo_slug}/settings/secrets/actions"
                if repo_slug
                else "",
                "actions_variables": f"https://github.com/{repo_slug}/settings/variables/actions"
                if repo_slug
                else "",
                "branches": f"https://github.com/{repo_slug}/settings/branches"
                if repo_slug
                else "",
            },
        },
        remediation=(
            "Run `code-mower controller run --repo OWNER/REPO --mode dry_run` "
            "after owner actions and promotion todos are resolved."
            if status != STATUS_PASS
            else None
        ),
    )


def check_supervised_pilot_mode(
    *,
    pilot_mode: str,
    adoption_posture: str,
) -> DoctorCheck:
    mode = pilot_mode if pilot_mode in PROMOTION_MODE_VALUES else "manual"
    return DoctorCheck(
        name="supervised_pilot.mode",
        status=STATUS_PASS,
        message=(
            "checking promoted supervised-pilot merge readiness"
            if mode == "promoted"
            else "checking manual supervised-pilot readiness"
        ),
        detail={
            "pilot_mode": mode,
            "adoption_posture": adoption_posture,
            "manual_mode_mutates": False,
            "promoted_mode_requires": [
                "code-mower/gate_required_from_any_source",
                "repository_auto_merge_enabled",
                "merge_capable_gate_credential",
                "trusted_reviewer_evidence",
            ],
        },
    )


def check_supervised_pilot(
    checks: Sequence[DoctorCheck],
    *,
    repo_slug: str,
    pilot_mode: str,
    adoption_posture: str,
    command_runner: lane_status.CommandRunner | None = None,
) -> tuple[DoctorCheck, ...]:
    """Return the high-level supervised-pilot readiness checks."""

    mode_check = check_supervised_pilot_mode(
        pilot_mode=pilot_mode,
        adoption_posture=adoption_posture,
    )
    board_check = check_supervised_pilot_board_visibility(command_runner=command_runner)
    runner_check = check_supervised_pilot_runner_posture(
        (*checks, board_check),
        adoption_posture=adoption_posture,
    )
    readiness = check_supervised_pilot_readiness(
        (*checks, board_check, runner_check),
        repo_slug=repo_slug,
        pilot_mode=pilot_mode,
        adoption_posture=adoption_posture,
    )
    return (mode_check, runner_check, board_check, readiness)


__all__ = [
    "check_supervised_pilot",
    "check_supervised_pilot_board_visibility",
    "check_supervised_pilot_mode",
    "check_supervised_pilot_readiness",
    "check_supervised_pilot_runner_posture",
]
