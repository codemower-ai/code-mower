"""Structured doctor check results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .groups import GROUP_LABELS, doctor_check_group_id


STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    lane: str | None = None
    detail: Mapping[str, Any] | None = None
    remediation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.detail is None:
            data.pop("detail")
        if self.lane is None:
            data.pop("lane")
        if self.remediation is None:
            data.pop("remediation")
        return data


@dataclass(frozen=True)
class DoctorReport:
    config_path: str
    provider_templates_path: str
    profile: str | None
    checks: tuple[DoctorCheck, ...]

    @property
    def failures(self) -> int:
        return sum(1 for check in self.checks if check.status == STATUS_FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == STATUS_WARN)

    @property
    def status(self) -> str:
        if self.failures:
            return STATUS_FAIL
        if self.warnings:
            return STATUS_WARN
        return STATUS_PASS

    @property
    def run_plan(self) -> tuple[dict[str, Any], ...]:
        """Return the structured doctor run plan when a report contains one."""

        for check in self.checks:
            if check.name != "doctor.plan" or not isinstance(check.detail, Mapping):
                continue
            stages = check.detail.get("stages")
            if not isinstance(stages, list):
                return ()
            plan: list[dict[str, Any]] = []
            for stage in stages:
                if not isinstance(stage, Mapping):
                    continue
                plan.append(
                    {
                        "id": str(stage.get("id", "")),
                        "group": str(stage.get("group", "")),
                        "optional": bool(stage.get("optional", False)),
                    }
                )
            return tuple(plan)
        return ()

    def group_summary(self) -> dict[str, dict[str, int | str]]:
        summary: dict[str, dict[str, int | str]] = {}
        for group_id, label in GROUP_LABELS.items():
            group_checks = [
                check
                for check in self.checks
                if doctor_check_group_id(check.name, check.lane) == group_id
            ]
            if not group_checks:
                continue
            failures = sum(1 for check in group_checks if check.status == STATUS_FAIL)
            warnings = sum(1 for check in group_checks if check.status == STATUS_WARN)
            skipped = sum(1 for check in group_checks if check.status == STATUS_SKIP)
            summary[group_id] = {
                "label": label,
                "checks": len(group_checks),
                "failures": failures,
                "warnings": warnings,
                "skipped": skipped,
            }
        return summary

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "doctor",
            "status": self.status,
            "profile": self.profile,
            "config_path": self.config_path,
            "provider_templates_path": self.provider_templates_path,
            "summary": {
                "checks": len(self.checks),
                "failures": self.failures,
                "warnings": self.warnings,
            },
            "run_plan": list(self.run_plan),
            "groups": self.group_summary(),
            "checks": [check.as_dict() for check in self.checks],
        }
