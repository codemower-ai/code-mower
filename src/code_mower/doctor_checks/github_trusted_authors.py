"""GitHub repository variable checks for trusted audit-comment authors."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .github_api import _github_api_json


def trusted_author_variable_probe(
    *,
    gh_path: str,
    slug: str,
    variables: Iterable[str],
    http_timeout: int,
) -> dict[str, Any]:
    """Return safe probe results for trusted-author repository variables."""

    statuses: dict[str, str] = {}
    read_errors: dict[str, dict[str, Any]] = {}
    for variable in variables:
        name = str(variable).strip()
        if not name:
            continue
        payload, _detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/variables/{name}",
            http_timeout=http_timeout,
        )
        if payload is None:
            statuses[name] = "not_confirmed"
            read_errors[name] = {
                key: value
                for key, value in _detail.items()
                if key
                in {
                    "endpoint",
                    "returncode",
                    "error_type",
                    "parse_error",
                    "output_redacted",
                    "output_line_count",
                }
            }
        elif str(payload.get("value") or "").strip():
            statuses[name] = "present"
        else:
            statuses[name] = "empty"
    return {"statuses": statuses, "read_errors": read_errors}


def trusted_author_variable_statuses(
    *,
    gh_path: str,
    slug: str,
    variables: Iterable[str],
    http_timeout: int,
) -> dict[str, str]:
    """Return safe presence statuses for trusted-author repository variables."""

    probe = trusted_author_variable_probe(
        gh_path=gh_path,
        slug=slug,
        variables=variables,
        http_timeout=http_timeout,
    )
    statuses = probe.get("statuses")
    return dict(statuses) if isinstance(statuses, dict) else {}


__all__ = ("trusted_author_variable_probe", "trusted_author_variable_statuses")
