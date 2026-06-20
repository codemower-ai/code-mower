"""Provider-vs-lens effect reports for calibration runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .evidence_report import build_reviewer_evidence_report
from .run_results import corpus_with_run_results
from .run_status import (
    RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_INFRA_ERROR,
    RUN_STATUS_PASS,
    normalize_run_status_category,
)


KNOWN_LENSES = (
    "context-driven-quality",
    "generic-programming",
    "security-threat-model",
    "calibration-policy",
    "package-runtime",
    "operability",
    "base-audit",
)
BASE_LENS = "base-audit"


@dataclass
class CellStats:
    provider: str
    lens: str
    profiles: set[str] = field(default_factory=set)
    runs: int = 0
    known_blocked_runs: int = 0
    known_blocked_caught_runs: int = 0
    known_blocked_missed_runs: int = 0
    audit_input_insufficient_runs: int = 0
    infra_error_runs: int = 0
    unknown_blocked_runs: int = 0
    known_clean_runs: int = 0
    known_clean_pass_runs: int = 0
    blocking_false_positive_runs: int = 0
    clean_nonblocking_finding_runs: int = 0
    duration_seconds_total: float = 0.0

    @property
    def evaluable_known_blocked_runs(self) -> int:
        return self.known_blocked_caught_runs + self.known_blocked_missed_runs

    @property
    def catch_rate(self) -> float | None:
        total = self.evaluable_known_blocked_runs
        if total <= 0:
            return None
        return self.known_blocked_caught_runs / total

    @property
    def effective_catch_rate(self) -> float | None:
        if self.known_blocked_runs <= 0:
            return None
        return self.known_blocked_caught_runs / self.known_blocked_runs

    @property
    def false_blocker_rate(self) -> float | None:
        if self.known_clean_runs <= 0:
            return None
        return self.blocking_false_positive_runs / self.known_clean_runs

    @property
    def seconds_per_run(self) -> float | None:
        if self.runs <= 0:
            return None
        return self.duration_seconds_total / self.runs

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "lens": self.lens,
            "profiles": sorted(self.profiles),
            "runs": self.runs,
            "known_blocked_runs": self.known_blocked_runs,
            "evaluable_known_blocked_runs": self.evaluable_known_blocked_runs,
            "known_blocked_caught_runs": self.known_blocked_caught_runs,
            "known_blocked_missed_runs": self.known_blocked_missed_runs,
            "catch_rate": _round_or_none(self.catch_rate),
            "effective_catch_rate": _round_or_none(self.effective_catch_rate),
            "known_clean_runs": self.known_clean_runs,
            "known_clean_pass_runs": self.known_clean_pass_runs,
            "blocking_false_positive_runs": self.blocking_false_positive_runs,
            "false_blocker_rate": _round_or_none(self.false_blocker_rate),
            "clean_nonblocking_finding_runs": self.clean_nonblocking_finding_runs,
            "audit_input_insufficient_runs": self.audit_input_insufficient_runs,
            "infra_error_runs": self.infra_error_runs,
            "unknown_blocked_runs": self.unknown_blocked_runs,
            "duration_seconds_total": round(self.duration_seconds_total, 3),
            "seconds_per_run": _round_or_none(self.seconds_per_run),
        }


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def reviewer_dimensions(profile_id: str, record: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Infer provider/lens dimensions from a reviewer id.

    Callers can override the inference by passing ``provider`` or ``lens`` fields
    on reviewer-run records. The fallback intentionally handles the built-in
    Code Mower reviewer ids and keeps unknown ids usable as base-lens providers.
    """

    record = record or {}
    provider = str(record.get("provider") or "").strip()
    lens = str(record.get("lens") or "").strip()
    profile = profile_id.strip()
    normalized = profile.replace("_", "-")

    if not provider:
        if normalized.startswith("antigravity-"):
            provider = "antigravity"
        elif normalized.startswith("gemini-"):
            provider = "gemini"
        elif normalized.startswith("claude-"):
            provider = "claude"
        elif normalized.startswith("codex-"):
            provider = "codex"
        elif normalized.startswith("hermes-"):
            provider = "hermes"
        elif normalized.startswith("coderabbit-"):
            provider = "coderabbit"
        elif normalized.startswith("gitar"):
            provider = "gitar"
        elif normalized.startswith("qwen"):
            provider = "qwen"
        elif normalized.startswith("gemma"):
            provider = "gemma"
        else:
            provider = normalized.split("-", 1)[0] or "unknown"

    if not lens:
        for candidate in KNOWN_LENSES:
            if candidate in normalized:
                lens = candidate
                break
    if not lens:
        lens = BASE_LENS
    if lens in {"audit", "base"}:
        lens = BASE_LENS

    return {"provider": provider, "lens": lens}


def _run_profile_id(run: Mapping[str, Any]) -> str:
    return str(
        run.get("profile_id")
        or run.get("reviewer")
        or run.get("lane")
        or "unknown-reviewer"
    ).strip()


def build_effect_report(
    corpus: Mapping[str, Any],
    *,
    run_results: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a factor-style report comparing provider and lens effects."""

    if run_results:
        corpus = corpus_with_run_results(corpus, run_results)
    evidence = build_reviewer_evidence_report(corpus)
    cells: dict[tuple[str, str], CellStats] = {}

    for run in evidence.get("reviewer_runs", []) or []:
        if not isinstance(run, Mapping):
            continue
        profile_id = _run_profile_id(run)
        dims = reviewer_dimensions(profile_id, run)
        key = (dims["provider"], dims["lens"])
        cell = cells.setdefault(key, CellStats(provider=dims["provider"], lens=dims["lens"]))
        cell.profiles.add(profile_id)
        cell.runs += 1
        try:
            cell.duration_seconds_total += float(run.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            pass

        status_category = normalize_run_status_category(
            run.get("status_category") or run.get("status") or run.get("verdict")
        )
        known_blocked = bool(run.get("known_blocked"))
        known_clean = bool(run.get("known_clean"))
        expected_caught = bool(run.get("expected_blocker_caught"))
        try:
            finding_count = int(run.get("finding_count") or 0)
        except (TypeError, ValueError):
            finding_count = 0

        if known_blocked:
            cell.known_blocked_runs += 1
            if expected_caught:
                cell.known_blocked_caught_runs += 1
            elif status_category in {RUN_STATUS_PASS, RUN_STATUS_BLOCKED}:
                cell.known_blocked_missed_runs += 1
            elif status_category == RUN_STATUS_AUDIT_INPUT_INSUFFICIENT:
                cell.audit_input_insufficient_runs += 1
            elif status_category == RUN_STATUS_INFRA_ERROR:
                cell.infra_error_runs += 1
            else:
                cell.unknown_blocked_runs += 1
        if known_clean:
            cell.known_clean_runs += 1
            if status_category == RUN_STATUS_PASS and finding_count == 0:
                cell.known_clean_pass_runs += 1
            elif status_category == RUN_STATUS_BLOCKED:
                cell.blocking_false_positive_runs += 1
            elif status_category == RUN_STATUS_PASS and finding_count > 0:
                cell.clean_nonblocking_finding_runs += 1

    cell_rows = [cell.as_dict() for cell in sorted(cells.values(), key=lambda item: (item.provider, item.lens))]
    lens_lifts = _lens_lifts(cells)
    provider_spreads = _provider_spreads(cells)
    summary = _effect_summary(lens_lifts, provider_spreads)

    return {
        "mode": "code-mower-provider-lens-effect-report",
        "corpus_name": corpus.get("name", ""),
        "source_item_count": evidence.get("source_item_count", 0),
        "reviewer_run_count": len(evidence.get("reviewer_runs", []) or []),
        "cells": cell_rows,
        "lens_lifts": lens_lifts,
        "provider_spreads": provider_spreads,
        "summary": summary,
        "caveat": (
            "This compares observed reviewer-run outcomes. It is strongest when "
            "each provider/lens cell sees the same corpus heads and when run-level "
            "dispositions distinguish expected catches from nearby but non-target findings."
        ),
    }


def _lens_lifts(cells: Mapping[tuple[str, str], CellStats]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    providers = sorted({provider for provider, _lens in cells})
    for provider in providers:
        base = cells.get((provider, BASE_LENS))
        if base is None or (
            base.catch_rate is None and base.effective_catch_rate is None
        ):
            continue
        for (candidate_provider, lens), cell in sorted(cells.items()):
            if candidate_provider != provider or lens == BASE_LENS:
                continue
            if cell.catch_rate is None and cell.effective_catch_rate is None:
                continue
            rows.append(
                {
                    "provider": provider,
                    "lens": lens,
                    "base_catch_rate": _round_or_none(base.catch_rate),
                    "lens_catch_rate": _round_or_none(cell.catch_rate),
                    "catch_rate_delta": _rate_delta(cell.catch_rate, base.catch_rate),
                    "base_effective_catch_rate": _round_or_none(base.effective_catch_rate),
                    "lens_effective_catch_rate": _round_or_none(cell.effective_catch_rate),
                    "effective_catch_rate_delta": _rate_delta(
                        cell.effective_catch_rate,
                        base.effective_catch_rate,
                    ),
                    "base_false_blocker_rate": _round_or_none(base.false_blocker_rate),
                    "lens_false_blocker_rate": _round_or_none(cell.false_blocker_rate),
                    "false_blocker_rate_delta": _rate_delta(
                        cell.false_blocker_rate,
                        base.false_blocker_rate,
                    ),
                    "base_evaluable_blocked_runs": base.evaluable_known_blocked_runs,
                    "lens_evaluable_blocked_runs": cell.evaluable_known_blocked_runs,
                }
            )
    return rows


def _provider_spreads(cells: Mapping[tuple[str, str], CellStats]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lenses = sorted({lens for _provider, lens in cells})
    for lens in lenses:
        lens_cells = [
            cell
            for (provider, candidate_lens), cell in cells.items()
            if candidate_lens == lens
            and (cell.catch_rate is not None or cell.effective_catch_rate is not None)
        ]
        if len(lens_cells) < 2:
            continue
        rates = [cell.catch_rate for cell in lens_cells if cell.catch_rate is not None]
        effective_rates = [
            cell.effective_catch_rate
            for cell in lens_cells
            if cell.effective_catch_rate is not None
        ]
        false_rates = [
            cell.false_blocker_rate
            for cell in lens_cells
            if cell.false_blocker_rate is not None
        ]
        rows.append(
            {
                "lens": lens,
                "providers": sorted(cell.provider for cell in lens_cells),
                "provider_count": len(lens_cells),
                "min_catch_rate": round(min(rates), 4) if len(rates) >= 2 else None,
                "max_catch_rate": round(max(rates), 4) if len(rates) >= 2 else None,
                "catch_rate_spread": (
                    round(max(rates) - min(rates), 4) if len(rates) >= 2 else None
                ),
                "effective_catch_rate_spread": (
                    round(max(effective_rates) - min(effective_rates), 4)
                    if len(effective_rates) >= 2
                    else None
                ),
                "false_blocker_rate_spread": (
                    round(max(false_rates) - min(false_rates), 4)
                    if len(false_rates) >= 2
                    else None
                ),
                "evaluable_blocked_runs": sum(
                    cell.evaluable_known_blocked_runs for cell in lens_cells
                ),
            }
        )
    return rows


def _rate_delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left - right, 4)


def _effect_summary(
    lens_lifts: list[Mapping[str, Any]],
    provider_spreads: list[Mapping[str, Any]],
) -> dict[str, Any]:
    lens_deltas = [
        abs(float(row["catch_rate_delta"]))
        for row in lens_lifts
        if row.get("catch_rate_delta") is not None
    ]
    lens_effective_deltas = [
        abs(float(row["effective_catch_rate_delta"]))
        for row in lens_lifts
        if row.get("effective_catch_rate_delta") is not None
    ]
    provider_deltas = [
        abs(float(row["catch_rate_spread"]))
        for row in provider_spreads
        if row.get("catch_rate_spread") is not None
    ]
    provider_effective_deltas = [
        abs(float(row["effective_catch_rate_spread"]))
        for row in provider_spreads
        if row.get("effective_catch_rate_spread") is not None
    ]
    lens_mean = sum(lens_deltas) / len(lens_deltas) if lens_deltas else None
    lens_effective_mean = (
        sum(lens_effective_deltas) / len(lens_effective_deltas)
        if lens_effective_deltas
        else None
    )
    provider_mean = sum(provider_deltas) / len(provider_deltas) if provider_deltas else None
    provider_effective_mean = (
        sum(provider_effective_deltas) / len(provider_effective_deltas)
        if provider_effective_deltas
        else None
    )
    lens_max = max(lens_deltas) if lens_deltas else None
    lens_effective_max = max(lens_effective_deltas) if lens_effective_deltas else None
    provider_max = max(provider_deltas) if provider_deltas else None
    provider_effective_max = (
        max(provider_effective_deltas) if provider_effective_deltas else None
    )
    comparison = "insufficient_data"
    effective_ratio = None
    evaluable_ratio = None
    if lens_effective_mean is not None and provider_effective_mean is not None:
        if lens_effective_mean > 0:
            effective_ratio = provider_effective_mean / lens_effective_mean
        if lens_effective_mean > provider_effective_mean * 1.25:
            comparison = "lens_effect_larger"
        elif provider_effective_mean > lens_effective_mean * 1.25:
            comparison = "provider_effect_larger"
        else:
            comparison = "similar_magnitude"
    if lens_mean is not None and provider_mean is not None and lens_mean > 0:
        evaluable_ratio = provider_mean / lens_mean
    return {
        "mean_absolute_lens_catch_delta": _round_or_none(lens_mean),
        "max_absolute_lens_catch_delta": _round_or_none(lens_max),
        "mean_provider_catch_spread": _round_or_none(provider_mean),
        "max_provider_catch_spread": _round_or_none(provider_max),
        "mean_absolute_lens_effective_catch_delta": _round_or_none(
            lens_effective_mean
        ),
        "max_absolute_lens_effective_catch_delta": _round_or_none(
            lens_effective_max
        ),
        "mean_provider_effective_catch_spread": _round_or_none(
            provider_effective_mean
        ),
        "max_provider_effective_catch_spread": _round_or_none(
            provider_effective_max
        ),
        "provider_to_lens_effective_ratio": _round_or_none(effective_ratio),
        "provider_to_lens_evaluable_ratio": _round_or_none(evaluable_ratio),
        "lens_comparison_count": len(lens_deltas),
        "lens_effective_comparison_count": len(lens_effective_deltas),
        "provider_comparison_count": len(provider_deltas),
        "provider_effective_comparison_count": len(provider_effective_deltas),
        "comparison": comparison,
    }


def _format_rate(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def render_effect_report_text(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), Mapping) else {}
    comparison = str(summary.get("comparison", "insufficient_data"))
    if comparison == "provider_effect_larger":
        interpretation = (
            "Provider/runtime choice moved outcomes more than doctrine wording on "
            "the primary coverage-inclusive metric."
        )
    elif comparison == "lens_effect_larger":
        interpretation = (
            "Doctrine lens choice moved outcomes more than provider/runtime choice "
            "on the primary coverage-inclusive metric."
        )
    elif comparison == "similar_magnitude":
        interpretation = (
            "Doctrine lens and provider/runtime effects were similar on the primary "
            "coverage-inclusive metric."
        )
    else:
        interpretation = "The corpus does not yet contain enough paired comparisons."
    lines = [
        "# Code Mower Provider vs Lens Effect Report",
        "",
        f"Corpus: `{report.get('corpus_name', '')}`",
        f"Items: {report.get('source_item_count', 0)}",
        f"Reviewer runs: {report.get('reviewer_run_count', 0)}",
        "",
        "## Answer",
        "",
        (
            f"- Mean absolute lens effective-catch delta: "
            f"{_format_rate(summary.get('mean_absolute_lens_effective_catch_delta'))}"
        ),
        (
            f"- Mean provider effective-catch spread: "
            f"{_format_rate(summary.get('mean_provider_effective_catch_spread'))}"
        ),
        (
            f"- Provider/lens effective-effect ratio: "
            f"{_format_rate(summary.get('provider_to_lens_effective_ratio'))}x"
        ),
        (
            f"- Mean absolute lens evaluable-catch delta: "
            f"{_format_rate(summary.get('mean_absolute_lens_catch_delta'))}"
        ),
        (
            f"- Mean provider evaluable-catch spread: "
            f"{_format_rate(summary.get('mean_provider_catch_spread'))}"
        ),
        (
            f"- Provider/lens evaluable-effect ratio: "
            f"{_format_rate(summary.get('provider_to_lens_evaluable_ratio'))}x"
        ),
        f"- Comparison: `{comparison}`",
        f"- Interpretation: {interpretation}",
        "",
        "## Provider/Lens Cells",
        "",
        "| Provider | Lens | Runs | Blocked caught/missed | Input gaps | Clean pass/false block | Effective catch | Evaluable catch | False-blocker rate | Sec/run |",
        "| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for cell in report.get("cells", []) or []:
        if not isinstance(cell, Mapping):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{cell.get('provider')}`",
                    f"`{cell.get('lens')}`",
                    str(cell.get("runs", 0)),
                    f"{cell.get('known_blocked_caught_runs', 0)}/{cell.get('known_blocked_missed_runs', 0)}",
                    str(cell.get("audit_input_insufficient_runs", 0)),
                    f"{cell.get('known_clean_pass_runs', 0)}/{cell.get('blocking_false_positive_runs', 0)}",
                    _format_rate(cell.get("effective_catch_rate")),
                    _format_rate(cell.get("catch_rate")),
                    _format_rate(cell.get("false_blocker_rate")),
                    _format_rate(cell.get("seconds_per_run")),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Same-Provider Lens Lift",
            "",
            "| Provider | Lens | Base effective | Lens effective | Effective delta | Evaluable delta | False-blocker delta |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report.get("lens_lifts", []) or []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.get('provider')}`",
                    f"`{row.get('lens')}`",
                    _format_rate(row.get("base_effective_catch_rate")),
                    _format_rate(row.get("lens_effective_catch_rate")),
                    _format_rate(row.get("effective_catch_rate_delta")),
                    _format_rate(row.get("catch_rate_delta")),
                    _format_rate(row.get("false_blocker_rate_delta")),
                ]
            )
            + " |"
        )
    if not report.get("lens_lifts"):
        lines.append("| none | none | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Cross-Provider Spread",
            "",
            "| Lens | Providers | Effective-catch spread | Evaluable-catch spread | False-blocker spread | Evaluable blocked runs |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report.get("provider_spreads", []) or []:
        if not isinstance(row, Mapping):
            continue
        providers = ", ".join(f"`{provider}`" for provider in row.get("providers", []) or [])
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.get('lens')}`",
                    providers,
                    _format_rate(row.get("effective_catch_rate_spread")),
                    _format_rate(row.get("catch_rate_spread")),
                    _format_rate(row.get("false_blocker_rate_spread")),
                    str(row.get("evaluable_blocked_runs", 0)),
                ]
            )
            + " |"
        )
    if not report.get("provider_spreads"):
        lines.append("| none | none | - | - | - | 0 |")

    lines.extend(["", f"_Caveat: {report.get('caveat', '')}_"])
    return "\n".join(lines) + "\n"
