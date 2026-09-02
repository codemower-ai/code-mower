"""GitHub repository variable checks for trusted audit-comment authors."""

from __future__ import annotations

from collections.abc import Iterable

from .github_api import _github_api_json


def trusted_author_variable_statuses(
    *,
    gh_path: str,
    slug: str,
    variables: Iterable[str],
    http_timeout: int,
) -> dict[str, str]:
    """Return safe presence statuses for trusted-author repository variables."""

    statuses: dict[str, str] = {}
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
            statuses[name] = "missing"
        elif str(payload.get("value") or "").strip():
            statuses[name] = "present"
        else:
            statuses[name] = "empty"
    return statuses


__all__ = ("trusted_author_variable_statuses",)
