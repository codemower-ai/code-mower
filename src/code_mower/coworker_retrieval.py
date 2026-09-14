"""Normalize only the qualified Coworker read operation, never arbitrary tools."""

from __future__ import annotations

import json
from typing import Any

from .context_contract import ContextError, ContextRetrievalError, _text, _timestamp

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
    try:
        return _normalize_search(value, limits=limits, maximum_results=maximum_results)
    except ContextRetrievalError:
        raise
    except (ContextError, UnicodeError, TypeError, ValueError):
        # Never turn provider fields or parser messages into public diagnostics.
        raise ContextRetrievalError("response_invalid") from None


def _normalize_search(value, *, limits, maximum_results):
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise ContextError("Coworker search returned an unsupported result")
    result = value["result"]
    records, retrieval = result.get("results"), result.get("retrieval")
    if (not isinstance(records, list) or not isinstance(retrieval, dict)
            or len(records) > maximum_results
            or type(retrieval.get("returned")) is not int or retrieval["returned"] != len(records)
            or type(retrieval.get("has_more")) is not bool
            or result.get("status") not in ("complete", "partial", "no_data")
            or (result.get("status") == "no_data" and (records or retrieval["has_more"]))):
        raise ContextError("Coworker search completeness or result count is invalid")
    partial = result["status"] == "partial" or retrieval["has_more"]
    truncated = bool(value.get("compaction"))
    omissions = []
    if result["status"] == "no_data":
        partial = True
        omissions.append("provider_no_data")
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
        if not isinstance(record, dict):
            raise ContextError("Coworker returned an unqualified evidence shape")
        if record.get("kind") not in ("Attribute", "SemanticUnit", "Text"):
            partial = True
            omissions.append("unsupported_record_kind")
            continue
        if not isinstance(record.get("text"), str) or not record["text"].strip():
            raise ContextError("Coworker returned an unqualified evidence shape")
        # Both locator fields are observed in qualified fast-search responses.
        # Preserve the provider's locator verbatim; never derive one from text,
        # a record id, a title, or another result in the batch.
        source = record.get("source_row_id")
        if source is None:
            source = record.get("source_id")
        if source is None:
            partial = True
            omissions.append("missing_citation")
            continue
        source = _text(source, maximum=2048)
        title = record.get("doc_title")
        if title is None:
            title = "Source title unavailable"
            partial = True
            omissions.append("source_title_unavailable")
        else:
            title = _text(title, maximum=512)
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
                          "confidence": "unknown", "source_date": updated, "source_kind": record["kind"]})
        used_bytes += len(text.encode("utf-8"))
    if records and not documents:
        if "text_limit" in omissions or "document_limit" in omissions:
            raise ContextRetrievalError("budget_exceeded")
        raise ContextError("Coworker search returned no citable qualified evidence")
    return {"documents": documents, "completeness": "partial" if partial or truncated else "complete",
            "truncated": truncated, "source_revision": None, "source_built_at": None,
            "omissions": sorted(set(omissions)),
            "response_bytes": len(json.dumps(value, ensure_ascii=False).encode("utf-8"))}
