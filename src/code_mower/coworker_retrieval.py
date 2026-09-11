"""Normalize only the qualified Coworker read operation, never arbitrary tools."""

from __future__ import annotations

import json
from typing import Any

from .context_contract import ContextError, _text, _timestamp

SEARCH_TOOL = "om2_search"
SEARCH_FIELDS = {
    "query": "string", "timezone": "string", "source": "string",
    "top_k": "integer", "include_raw_documents": "boolean",
}


def verify_search_schema(tool: dict[str, Any]) -> None:
    """Descriptions/annotations do not authorize operations or change arguments."""
    schema = tool.get("inputSchema")
    if tool.get("name") != SEARCH_TOOL or not isinstance(schema, dict):
        raise ContextError("qualified Coworker search is unavailable")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    mode = properties.get("search_mode") if isinstance(properties, dict) else None
    if (schema.get("type") != "object" or not isinstance(properties, dict)
            or not isinstance(required, list) or not all(isinstance(key, str) for key in required)
            or not set(required) <= set(SEARCH_FIELDS) | {"search_mode"}
            or any(not isinstance(properties.get(key), dict) or properties[key].get("type") != kind
                   for key, kind in SEARCH_FIELDS.items())
            or not isinstance(mode, dict) or not isinstance(mode.get("enum"), list)
            or "fast" not in mode["enum"]):
        raise ContextError("Coworker search schema changed; requalification is required")


def search_arguments(query: str, source: str | None, limits) -> dict[str, Any]:
    result = {"query": _text(query, maximum=2000), "timezone": "UTC",
              "top_k": min(limits["max_documents"], 5), "search_mode": "fast",
              "include_raw_documents": False}
    if source is not None:
        result["source"] = _text(source, maximum=80)
    return result


def normalize_search(value, *, limits, maximum_results):
    """Keep source IDs and dates; similarity is not evidence confidence."""
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise ContextError("Coworker search returned an unsupported result")
    result = value["result"]
    records, retrieval = result.get("results"), result.get("retrieval")
    if (not isinstance(records, list) or not isinstance(retrieval, dict)
            or len(records) > maximum_results
            or type(retrieval.get("returned")) is not int or retrieval["returned"] != len(records)
            or type(retrieval.get("has_more")) is not bool
            or result.get("status") not in ("complete", "partial")):
        raise ContextError("Coworker search completeness or result count is invalid")
    partial = result["status"] == "partial" or retrieval["has_more"]
    truncated = bool(value.get("compaction"))
    omissions = []
    if result["status"] == "partial":
        omissions.append("provider_partial")
    if retrieval["has_more"]:
        omissions.append("provider_has_more")
    if truncated:
        omissions.append("provider_compaction")
    resolution = result.get("resolution", {})
    if not isinstance(resolution, dict) or not isinstance(result.get("warnings", []), list):
        raise ContextError("Coworker search uncertainty metadata is invalid")
    partial = partial or resolution.get("status") == "unresolved" or bool(result.get("warnings"))
    if resolution.get("status") == "unresolved":
        omissions.append("unresolved_entities")
    if result.get("warnings"):
        omissions.append("provider_warning")
    documents, used_bytes = [], 0
    for record in records:
        if (not isinstance(record, dict) or record.get("kind") != "Attribute"
                or not isinstance(record.get("text"), str) or not record["text"].strip()):
            raise ContextError("Coworker returned an unqualified evidence shape")
        source = _text(record.get("source_row_id"), maximum=2048)
        title = _text(record.get("doc_title"), maximum=512)
        raw = record["text"].encode("utf-8")
        available = min(limits["max_document_bytes"], limits["max_text_bytes"] - used_bytes)
        if len(documents) >= limits["max_documents"] or available <= 0:
            truncated = True
            omissions.append("document_limit" if len(documents) >= limits["max_documents"] else "text_limit")
            break
        if len(raw) > available:
            raw = raw[:available]
            truncated = True
            omissions.append("text_limit")
        text = raw.decode("utf-8", errors="ignore")
        if not text.strip():
            truncated = True
            continue
        updated = record.get("date")
        if updated is not None:
            _timestamp(updated)
        documents.append({"text": text, "citations": [{"source": source, "title": title}],
                          "confidence": "unknown", "source_date": updated})
        used_bytes += len(text.encode("utf-8"))
    return {"documents": documents, "completeness": "partial" if partial or truncated else "complete",
            "truncated": truncated, "source_revision": None, "source_built_at": None,
            "omissions": sorted(set(omissions)),
            "response_bytes": len(json.dumps(value, ensure_ascii=False).encode("utf-8"))}
