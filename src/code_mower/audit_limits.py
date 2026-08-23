"""Shared audit budget and diff-limit defaults."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Mapping


DEFAULT_MAX_DIFF_BYTES = 180_000
DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES = 1_500_000
DEFAULT_BUDGET_BASE_USD = Decimal("2.00")
DEFAULT_BUDGET_CAP_USD = Decimal("10.00")
DEFAULT_BUDGET_STEP_USD = Decimal("1.00")
DEFAULT_BUDGET_STEP_BYTES = 150_000


@dataclass(frozen=True)
class AuditLimitSettings:
    budget_usd: str = ""
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES
    max_diff_hard_limit_bytes: int = DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES

    @property
    def budget_description(self) -> str:
        if self.budget_usd:
            return f"fixed ${self.budget_usd}"
        return (
            "size-aware default "
            f"(${_format_budget(DEFAULT_BUDGET_BASE_USD)} base, "
            f"+${_format_budget(DEFAULT_BUDGET_STEP_USD)} per "
            f"{DEFAULT_BUDGET_STEP_BYTES} bytes above {self.max_diff_bytes}, "
            f"cap ${_format_budget(DEFAULT_BUDGET_CAP_USD)})"
        )


def _format_budget(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _clean_optional(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    text = _clean_optional(value)
    if not text:
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        parsed = int(text, 10)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def parse_budget_usd(value: Any, *, field_name: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive decimal USD value")
    text = _clean_optional(value)
    if not text:
        return ""
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a positive decimal USD value") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field_name} must be a positive decimal USD value")
    return _format_budget(parsed)


def audit_limits_from_config(config: Mapping[str, Any]) -> AuditLimitSettings:
    raw_audit = config.get("audit")
    audit = raw_audit if isinstance(raw_audit, Mapping) else {}
    budget_usd = parse_budget_usd(
        audit.get("budget_usd"),
        field_name="audit.budget_usd",
    )
    max_diff_bytes = (
        parse_positive_int(
            audit.get("max_diff_bytes"),
            field_name="audit.max_diff_bytes",
        )
        if _clean_optional(audit.get("max_diff_bytes"))
        else DEFAULT_MAX_DIFF_BYTES
    )
    max_diff_hard_limit_bytes = (
        parse_positive_int(
            audit.get("max_diff_hard_limit_bytes"),
            field_name="audit.max_diff_hard_limit_bytes",
        )
        if _clean_optional(audit.get("max_diff_hard_limit_bytes"))
        else DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES
    )
    if max_diff_hard_limit_bytes < max_diff_bytes:
        raise ValueError(
            "audit.max_diff_hard_limit_bytes must be greater than or equal to "
            "audit.max_diff_bytes"
        )
    return AuditLimitSettings(
        budget_usd=budget_usd,
        max_diff_bytes=max_diff_bytes,
        max_diff_hard_limit_bytes=max_diff_hard_limit_bytes,
    )


def resolve_audit_budget_usd(
    included_diff_bytes: int,
    *,
    explicit_budget_usd: str | None = None,
    target_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> str:
    explicit = _clean_optional(explicit_budget_usd)
    if explicit:
        return parse_budget_usd(explicit, field_name="budget_usd")

    included = max(0, int(included_diff_bytes))
    target = max(0, int(target_diff_bytes))
    extra_bytes = max(0, included - target)
    steps = 0
    if extra_bytes:
        steps = int(
            (Decimal(extra_bytes) / Decimal(DEFAULT_BUDGET_STEP_BYTES)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
    budget = DEFAULT_BUDGET_BASE_USD + (DEFAULT_BUDGET_STEP_USD * steps)
    return _format_budget(min(budget, DEFAULT_BUDGET_CAP_USD))


def next_audit_budget_usd(current_budget_usd: str) -> str:
    current = Decimal(parse_budget_usd(current_budget_usd, field_name="budget_usd"))
    return _format_budget(min(current + DEFAULT_BUDGET_STEP_USD, DEFAULT_BUDGET_CAP_USD))
