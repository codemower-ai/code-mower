"""Structured doctor check results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


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
            "checks": [check.as_dict() for check in self.checks],
        }
