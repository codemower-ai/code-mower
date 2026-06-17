"""Structured GitHub Actions recent-failure inspection results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ActionsBillingBlock:
    run_id: Any
    workflow: str
    head_sha: str
    job_id: Any
    check_run_id: str
    job: str

    def as_detail(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "head_sha": self.head_sha,
            "job_id": self.job_id,
            "check_run_id": self.check_run_id,
            "job": self.job,
        }


@dataclass(frozen=True)
class ActionsFailureInspection:
    inspected_failed_runs: int = 0
    inspected_failed_jobs: int = 0
    billing_blocks: tuple[ActionsBillingBlock, ...] = ()
    incomplete_inspections: tuple[dict[str, Any], ...] = ()
    unavailable_detail: Mapping[str, Any] | None = None
    missing_workflow_runs: bool = False

    @property
    def has_billing_blocks(self) -> bool:
        return bool(self.billing_blocks)

    @property
    def incomplete_inspection_count(self) -> int:
        return len(self.incomplete_inspections)


__all__ = ("ActionsBillingBlock", "ActionsFailureInspection")
