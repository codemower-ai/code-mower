"""Read-only queue policy; Jira transport and credentials belong to the client.

Readers must be doctor-validated before injection. No exception text, query,
cursor, raw issue, or local display title is returned from this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .tracker_contract import TRACKER_WORK_ITEM_SCHEMA, validate_tracker_work_item


class JiraQueueReader(Protocol):
    """Minimal enhanced-search seam for #800; no transport implementation here."""

    def search_page(
        self, *, jql: str, fields: Sequence[str], max_results: int,
        next_page_token: str | None,
    ) -> Mapping[str, Any]: ...


def resolve_jira_queue_reader(
    config: Mapping[str, Any],
    *,
    credential_file: Path | None = None,
    profile: str = "",
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 20.0,
    client_factory: Any = None,
) -> JiraQueueReader | None:
    """Build the read-only Jira queue client, or fail closed to unavailable.

    Credential diagnostics belong to doctor. Controller and lane status only
    need a reader/no-reader decision and must never print credential values or
    resolver exception text.
    """
    if not jira_enabled(config):
        return None
    from . import jira_cloud

    resolution = jira_cloud.resolve_jira_credentials(
        credential_file=credential_file,
        profile=profile,
        config_dir=config_dir,
        env=env,
    )
    if not resolution.has_credentials:
        return None
    jira = config.get("tracker", {}).get("jira_cloud", {})
    if not isinstance(jira, Mapping):
        return None
    factory = client_factory or jira_cloud.JiraReadClient
    try:
        return factory(
            cloud_id=str(jira.get("cloud_id") or ""),
            email=resolution.email,
            token=resolution.token,
            site_url=str(jira.get("site_url") or ""),
            timeout_seconds=float(timeout_seconds),
        )
    except (TypeError, ValueError):
        return None


def jira_enabled(config: Mapping[str, Any]) -> bool:
    tracker = config.get("tracker")
    return isinstance(tracker, Mapping) and tracker.get("kind") == "jira_cloud"


def _short(value: Any, limit: int) -> str:
    return value if isinstance(value, str) and len(value) <= limit and all(
        ord(char) >= 32 and ord(char) != 127 for char in value
    ) else ""


def _query(config: Mapping[str, Any]) -> str:
    project = config["project_id"]
    if not isinstance(project, str) or not re.fullmatch(r"[0-9]{1,32}", project):
        raise ValueError("invalid project identity")
    # Strip only an unquoted ORDER BY clause; literals may contain those words.
    query = config.get("jql", "")
    if not isinstance(query, str) or len(query) > 2000 or "\n" in query or "\r" in query:
        raise ValueError("invalid query")
    quoted = None
    escaped = False
    paren_depth = 0
    for index, char in enumerate(query):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif quoted:
            if char == quoted:
                quoted = None
        elif char in "\"'":
            quoted = char
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth < 0:
                raise ValueError("invalid query")
        elif re.match(r"(?i)\border\s+by\b", query[index:]) and (
            index == 0 or not query[index - 1].isalnum()
        ):
            query = query[:index]
            break
    if quoted or paren_depth:
        raise ValueError("invalid query")
    predicate = query.strip()
    return f'project = {project}' + (f" AND ({predicate})" if predicate else "") + " ORDER BY created ASC, key ASC"


def normalize_jira_work_item(raw: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Project membership is verified from immutable response identity as well as JQL."""
    fields = raw["fields"]
    if str(fields["project"]["id"]) != config["project_id"]:
        raise ValueError("out of scope")
    issue_id = raw["id"]
    key = raw.get("key", "")
    if not isinstance(issue_id, str) or not re.fullmatch(r"[0-9]{1,32}", issue_id):
        raise ValueError("invalid issue identity")
    if key and (not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key) or len(key) > 128):
        raise ValueError("invalid issue key")
    site = urlsplit(config["site_url"])
    if site.scheme != "https" or not site.hostname or site.username or site.password or site.query or site.fragment or site.path not in ("", "/"):
        raise ValueError("invalid site")
    mappings = config.get("field_mappings") or {}
    status = fields.get(mappings.get("lifecycle_category", "status")) or {}
    category = None
    if isinstance(status, Mapping):
        status_id = str(status.get("id", ""))
        for target, ids in (config.get("status_category_map") or {}).items():
            if status_id in ids:
                category = target
                break
        if category is None:
            category = {"new": "new", "indeterminate": "in_progress", "done": "done"}.get(
                (status.get("statusCategory") or {}).get("key")
            )
    elif status in ("new", "in_progress", "blocked", "done"):
        category = status
    labels = fields.get(mappings.get("labels", "labels")) or []
    if not isinstance(labels, list):
        raise ValueError("invalid labels")
    # Custom fields are restricted to the same bounded scalar contract.
    safe_labels = sorted({_short(label, 128) for label in labels[:100]})
    assignment = fields.get(mappings.get("assigned", "assignee"))
    if mappings.get("assigned", "assignee") != "assignee" and not isinstance(assignment, bool):
        raise ValueError("mapped assignment must be boolean")
    metadata = {}
    for name, value in (
        ("status_name", status.get("name") if isinstance(status, Mapping) else None),
        ("issue_type", (fields.get("issuetype") or {}).get("name")),
    ):
        if short := _short(value, 64):
            metadata[name] = short
    identity = {"cloud_id": config["cloud_id"], "project_id": config["project_id"], "issue_id": issue_id}
    if key:
        identity["issue_key"] = key
    item = {
        "schema": TRACKER_WORK_ITEM_SCHEMA, "source_kind": "jira_cloud",
        "identity": identity, "url": f"https://{site.netloc}/browse/{key or issue_id}",
        "lifecycle_category": category, "labels": [label for label in safe_labels if label.strip()],
        "assigned": bool(assignment), "created_at": fields.get("created"),
        "updated_at": fields.get("updated"), "provider_metadata": metadata,
    }
    if validate_tracker_work_item(item):
        raise ValueError("invalid normalized item")
    return item


def collect_queue(
    config: Mapping[str, Any], *, reader: JiraQueueReader | None = None,
    now: datetime | None = None, max_pages: int = 5, page_size: int = 50,
) -> dict[str, Any]:
    """Bounded enhanced JQL reads. Partial/error results never become eligible."""
    observed = now or datetime.now(UTC)
    result: dict[str, Any] = {
        "schema": "code_mower.trackerQueue.v1", "source_kind": "jira_cloud",
        "available": False, "complete": False, "freshness": "unavailable",
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "errors": [], "items": [],
    }
    if reader is None:
        result["errors"] = ["jira_reader_unavailable"]
        return result
    try:
        jira = config["tracker"]["jira_cloud"]
        query = _query(jira)
        fields = {"project", "status", "labels", "assignee", "created", "updated", "issuetype"}
        for field in (jira.get("field_mappings") or {}).values():
            if field not in fields and not re.fullmatch(r"customfield_[0-9]+", field):
                raise ValueError("unsafe mapped field")
            fields.add(field)
        seen_tokens: set[str] = set()
        items = {}
        token = None
        for _ in range(min(10, max(1, max_pages))):
            page = reader.search_page(jql=query, fields=sorted(fields), max_results=min(100, max(1, page_size)), next_page_token=token)
            if "isLast" in page and not isinstance(page["isLast"], bool):
                raise ValueError("invalid completion marker")
            raw_items = page["issues"]
            if not isinstance(raw_items, list) or len(raw_items) > min(100, max(1, page_size)):
                raise ValueError("invalid page")
            for raw in raw_items:
                item = normalize_jira_work_item(raw, jira)
                identity = item["identity"]["issue_id"]
                previous = items.get(identity)
                if previous is None or datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")) > datetime.fromisoformat(previous["updated_at"].replace("Z", "+00:00")):
                    items[identity] = item
            token = page.get("nextPageToken")
            if token is not None and (not isinstance(token, str) or not token or len(token) > 4096):
                raise ValueError("invalid cursor")
            if page.get("isLast") is True and token is not None:
                raise ValueError("contradictory completion marker")
            if page.get("isLast") is True or not token:
                if page.get("isLast") is False:
                    raise ValueError("missing cursor")
                result["complete"] = True
                break
            if not isinstance(token, str) or len(token) > 4096 or token in seen_tokens:
                raise ValueError("invalid cursor")
            seen_tokens.add(token)
        result["items"] = sorted(items.values(), key=lambda item: (
            datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
            item["identity"].get("issue_key", ""), int(item["identity"]["issue_id"]),
        ))
        result["available"] = True
        result["freshness"] = "live" if result["complete"] else "partial"
        if not result["complete"]:
            result["errors"] = ["jira_page_limit"]
    except Exception:
        # Provider exceptions may include credentials, queries, prose, or paths.
        result["errors"] = ["jira_read_unavailable"]
    return result


def queue_view(
    queue: Mapping[str, Any], *, config: Mapping[str, Any], remote: Mapping[str, Any],
    now: datetime | None = None, stale_minutes: int = 30,
    links: Mapping[tuple[str, str, str], int] | None = None,
) -> dict[str, Any]:
    """Join explicit local identity→PR references against current GitHub only.

    Historical PR/gate fields in a queue snapshot are deliberately ignored.
    Linking/discovery and persistence of references belong to #802.
    """
    from .controller import _builder_lane_from_labels, _gate_state, _owner_label

    freshness = queue.get("freshness")
    if freshness not in ("live", "partial", "unavailable"):
        freshness = "historical"
    try:
        observed = datetime.fromisoformat(queue["observed_at"].replace("Z", "+00:00"))
        age = (now or datetime.now(UTC)) - observed
        if age > timedelta(minutes=stale_minutes) or age < timedelta(0):
            freshness = "historical"
    except (KeyError, TypeError, ValueError):
        freshness = "historical"
    rows = []
    prs = {pr["number"]: pr for pr in remote.get("pull_requests", [])} if remote.get("available") else {}
    for item in queue.get("items", []):
        if not isinstance(item, Mapping) or validate_tracker_work_item(item):
            continue
        identity = item["identity"]
        reference = tuple(identity.get(key, "") for key in ("cloud_id", "project_id", "issue_id"))
        pr_number = (links or {}).get(reference)
        pr = prs.get(pr_number)
        lane = _builder_lane_from_labels(item["labels"], config)
        if pr:
            lane = _builder_lane_from_labels(pr.get("labels", {}).get("builder", []), config) or lane
        eligible = bool(queue.get("available") and queue.get("complete") and freshness == "live" and item["lifecycle_category"] == "new" and not item["assigned"] and not pr_number and _owner_label(config) not in item["labels"] and not any(label.startswith("dispatched:") for label in item["labels"]))
        action = "inspect tracker work"
        if eligible and lane:
            action = "dispatch builder lane"
        if freshness != "live":
            action = "refresh Jira queue"
        if pr_number:
            action = pr["next_action"] if pr else "refresh linked GitHub PR"
        rows.append({"work_item": dict(item), "freshness": freshness, "lane_id": lane,
                     "linked_pr_number": pr_number, "gate_status": _gate_state(pr) if pr else "unknown",
                     "pr_freshness": "live" if pr else "unavailable", "eligible": eligible,
                     "next_action": action})
    return {"source_kind": "jira_cloud", "available": bool(queue.get("available")),
            "complete": bool(queue.get("complete")), "freshness": freshness,
            "errors": [error for error in queue.get("errors", []) if error in (
                "jira_reader_unavailable", "jira_read_unavailable", "jira_page_limit")], "items": rows}


def render_text(view: Mapping[str, Any]) -> list[str]:
    lines = [f"Tracker: jira_cloud ({view['freshness']})"]
    for row in view["items"]:
        item = row["work_item"]
        key = item["identity"].get("issue_key", item["identity"].get("issue_id", ""))
        lines.append(f"- {key} {item['lifecycle_category']} lane={row['lane_id'] or 'unassigned'} PR={row['linked_pr_number'] or '-'} gate={row['gate_status']} next={row['next_action']}")
    lines.extend(view["errors"])
    return lines
