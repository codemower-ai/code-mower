"""Guided, resumable context retrieval and work-order preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import context_packets, context_session, work_orders
from .context_contract import ContextError, ContextRequest, _text
from .context_store import ContextStore
from .participants import PARTICIPANTS, participant_id


PREPARE_SCHEMA = "code_mower.contextPrepare.v1"
MAX_WORK_ORDER_BODY_BYTES = 262_144
DEFAULT_WORK_ORDER_BODY = (
    "Implement the selected work item for this repository. Use the authorized "
    "organizational context to resolve requirements and constraints. Verify the "
    "result against the work item's acceptance criteria with focused tests and "
    "an independent current-head review."
)
DEFAULT_QUERY_PREFIX = "Find requirements, decisions, constraints, and prior discussion for work item "


def _request_hash(query: str, source: str | None, recipient: str) -> str:
    raw = json.dumps(
        {"query": query, "source": source, "recipient": recipient},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _work_order_body(path: Path | None) -> str:
    if path is None:
        return DEFAULT_WORK_ORDER_BODY
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise ContextError("guided work-order input is unavailable") from None
    if len(raw) > MAX_WORK_ORDER_BODY_BYTES:
        raise ContextError("guided work-order input exceeds its size bound")
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ContextError("guided work-order input must be UTF-8 text") from None
    try:
        work_orders.validate_work_order_source_text(body)
    except ValueError as exc:
        raise ContextError(str(exc)) from None
    return body


def _review_lanes(record: Mapping[str, Any], builder: str) -> tuple[str, ...]:
    lanes = tuple(
        lane
        for participant in record["participants"]
        if participant != builder
        if (lane := PARTICIPANTS[participant].review_lane) is not None
    )
    if not lanes:
        raise ContextError(
            "guided context preparation requires a selected independent reviewer"
        )
    return lanes


def _work_order_destination(
    repo_root: Path,
    session_id: str,
    requested: Path | None,
    existing: str | None,
) -> tuple[str, Path]:
    value = str(requested) if requested is not None else (
        existing or f".code-mower/work-orders/session-{session_id}.md"
    )
    reference = context_session.work_order_reference(value)
    root = Path(repo_root).resolve()
    destination = (root / reference).resolve(strict=False)
    if not destination.is_relative_to(root):
        raise ContextError("guided work orders must stay inside the repository")
    return reference, destination


def _replacement_reference(session_id: str, generation: int) -> str:
    return f".code-mower/work-orders/session-{session_id}-g{generation}.md"


def _reuse_existing_work_order(
    path: Path,
    *,
    title: str,
    repo: str,
    builder: str,
    packet: str,
    review_lanes: tuple[str, ...],
) -> bool:
    manifest_path = path.with_suffix(".json")
    event_path = path.with_name(f"{path.stem}.cloud-event.json")
    artifacts = (path, manifest_path, event_path)
    if not any(item.exists() for item in artifacts):
        return False
    if not all(item.is_file() for item in artifacts):
        raise ContextError(
            "guided work-order output already exists or is incomplete; choose a different --output"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContextError(
            "guided work-order output already exists or is incomplete; choose a different --output"
        ) from None
    expected_source = {
        "type": "guided_context_session", "repo": repo, "builder": builder,
    }
    matches = (
        isinstance(manifest, Mapping)
        and manifest.get("schema") == work_orders.WORK_ORDER_SCHEMA
        and manifest.get("title") == title
        and manifest.get("repo") == repo
        and manifest.get("output_path") == str(path)
        and manifest.get("context_packet") == packet
        and manifest.get("role_lenses") == ["implementation"]
        and manifest.get("review_lanes") == list(review_lanes)
        and manifest.get("source") == expected_source
        and manifest.get("manifest_path") == str(manifest_path)
        and manifest.get("cloud_event_path") == str(event_path)
        and isinstance(event, Mapping)
        and event == work_orders.build_work_order_cloud_event(manifest)
    )
    if not matches:
        raise ContextError(
            "guided work-order output already exists for different input; choose a different --output"
        )
    return True


def _report(
    status: str,
    *,
    stage: str,
    dependent_work: str,
    next_action: str,
    work_order: str | None = None,
    reused: bool | None = None,
) -> dict[str, Any]:
    return {
        "schema": PREPARE_SCHEMA,
        "status": status,
        "stage": stage,
        "dependent_work": dependent_work,
        "owner_action": status.endswith("unavailable"),
        "next_action": next_action,
        **({"work_order": work_order} if work_order is not None else {}),
        **({"reused": reused} if reused is not None else {}),
    }


def _unavailable(required: bool) -> tuple[dict[str, Any], int]:
    status = "required_unavailable" if required else "optional_unavailable"
    return (
        _report(
            status,
            stage="preparing",
            dependent_work="paused" if required else "usable",
            next_action=(
                "Verify the selected connection, then rerun prepare with --refresh; "
                "no search retries automatically."
            ),
        ),
        1 if required else 0,
    )


def prepare(
    association_store: ContextStore,
    record: Mapping[str, Any],
    *,
    repo_root: Path,
    context_root: Path | None = None,
    packet_store: ContextStore | None = None,
    query: str | None = None,
    source: str | None = None,
    title: str | None = None,
    builder: str | None = None,
    body_file: Path | None = None,
    output: Path | None = None,
    refresh: bool = False,
    backend: Any = None,
) -> tuple[dict[str, Any], int]:
    """Prepare once and resume safely without repeating provider searches."""
    record = context_session.validate(record)
    if record["connection"] is None or record["policy"] is None:
        return _report(
            "not_configured",
            stage="not_configured",
            dependent_work="usable",
            next_action="Continue the ordinary workflow or configure an optional context connection.",
        ), 0
    if record["host"] not in {"claude", "codex"}:
        raise ContextError("guided private context currently supports Claude and Codex hosts")
    selected_builder = participant_id(builder or record["builder"] or record["host"])
    if (
        selected_builder not in record["participants"]
        or not PARTICIPANTS[selected_builder].builder
    ):
        raise ContextError("--builder must name a selected builder participant")
    if record["stage"] in {"attached", "reviewed"}:
        if refresh:
            raise ContextError("start a new session before refreshing context already attached to a PR")
        return _report(
            "already_attached",
            stage=record["stage"],
            dependent_work="usable",
            next_action="Continue the current-head review and feedback workflow.",
            work_order=record["work_order"],
            reused=True,
        ), 0

    explicit_query = query is not None
    effective_query = _text(
        query if explicit_query else DEFAULT_QUERY_PREFIX + record["work_item"],
        maximum=2000,
    )
    effective_source = _text(source, maximum=80) if source is not None else None
    recipient = record["host"] + ":orchestrator"
    fingerprint = _request_hash(effective_query, effective_source, recipient)
    if not refresh and record["request_hash"] is not None:
        source_changed = record["retrieval_source"] != effective_source
        query_changed = (
            explicit_query or record["query_mode"] == "work_item"
        ) and record["request_hash"] != fingerprint
        if source_changed or query_changed:
            raise ContextError("retrieval input changed; rerun prepare with --refresh")

    body = _work_order_body(body_file)
    title = _text(title or "Selected work item", maximum=200)
    lanes = _review_lanes(record, selected_builder)
    replacement = refresh or (
        record["builder"] is not None and record["builder"] != selected_builder
    )
    existing_work_order = record["work_order"]
    if output is None and record["stage"] != "selected" and (
        replacement or existing_work_order is None
    ):
        existing_work_order = _replacement_reference(
            record["session_id"], record["generation"] + 1,
        )
    work_order_ref, work_order_path = _work_order_destination(
        repo_root, record["session_id"], output, existing_work_order,
    )
    packet_store = packet_store or ContextStore(context_root)

    if (
        record["stage"] == "prepared"
        and not refresh
        and record["builder"] == selected_builder
    ):
        if output is not None:
            context_session.resolve_bound("work order", record["work_order"], work_order_ref)
        try:
            context_packets.load_authorized(
                packet_store,
                record["connection"],
                record["packet"],
                record["policy"],
                ContextRequest(record["repo"], record["work_item"], recipient),
                backend=backend,
            )
        except ContextError as exc:
            context_session.update(
                association_store,
                record["session_id"],
                expected_generation=record["generation"],
                changes={
                    "stage": "preparing", "packet": None, "work_order": None,
                    "context_state": context_session.failure_state(exc),
                },
            )
            return _unavailable(record["policy"]["required"])
        if record["context_state"] != "ready":
            record = context_session.update(
                association_store,
                record["session_id"],
                expected_generation=record["generation"],
                changes={"context_state": "ready"},
            )
        return _report(
            "prepared",
            stage="prepared",
            dependent_work="usable",
            next_action="Continue the build and attach context when a pull request exists.",
            work_order=record["work_order"],
            reused=True,
        ), 0

    if (
        record["stage"] == "prepared"
        and not refresh
        and record["builder"] != selected_builder
    ):
        record = context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={
                "stage": "preparing", "builder": selected_builder,
                "work_order": work_order_ref,
            },
        )

    if (
        record["stage"] == "preparing"
        and record["packet"] is not None
        and record["builder"] != selected_builder
    ):
        record = context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={"builder": selected_builder, "work_order": work_order_ref},
        )

    if record["stage"] == "preparing" and record["packet"] is None and not refresh:
        raise ContextError(
            "the previous context preparation did not complete; rerun prepare with --refresh"
        )

    if refresh or record["stage"] == "selected":
        record = context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={
                "stage": "preparing",
                "builder": selected_builder,
                "query_mode": "stdin" if explicit_query else "work_item",
                "retrieval_source": effective_source,
                "request_hash": fingerprint,
                "context_state": "unchecked",
                "packet": None,
                "work_order": work_order_ref,
                "pr": None,
                "head": None,
                "revision": None,
                "attachment_state": "none",
            },
        )

    packet_handle = record["packet"]
    reused = packet_handle is not None
    if packet_handle is not None:
        try:
            context_packets.load_authorized(
                packet_store,
                record["connection"],
                packet_handle,
                record["policy"],
                ContextRequest(record["repo"], record["work_item"], recipient),
                backend=backend,
            )
        except ContextError as exc:
            context_session.record_failure(association_store, record, exc)
            return _unavailable(record["policy"]["required"])
        if record["context_state"] != "ready":
            record = context_session.update(
                association_store,
                record["session_id"],
                expected_generation=record["generation"],
                changes={"context_state": "ready"},
            )
    else:
        try:
            result = context_packets.fetch(
                packet_store,
                record["connection"],
                {
                    "repository": record["repo"],
                    "work_item": record["work_item"],
                    "recipient": recipient,
                    "query": effective_query,
                    "source": effective_source,
                    "policy": record["policy"],
                },
                backend=backend,
                refresh=refresh,
            )
        except ContextError as exc:
            context_session.record_failure(association_store, record, exc)
            return _unavailable(record["policy"]["required"])
        packet_handle = result["packet_handle"]
        reused = bool(result["reused"])
        record = context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={"packet": packet_handle, "context_state": "ready"},
        )

    if not _reuse_existing_work_order(
        work_order_path,
        title=title,
        repo=record["repo"],
        builder=selected_builder,
        packet=packet_handle,
        review_lanes=lanes,
    ):
        try:
            work_orders.draft_work_order(
                title=title,
                source_text=body,
                repo=record["repo"],
                role_lenses=("implementation",),
                review_lanes=lanes,
                source={
                    "type": "guided_context_session",
                    "repo": record["repo"],
                    "builder": selected_builder,
                },
                output=work_order_path,
                force=False,
                context_packet=packet_handle,
            )
        except (OSError, ValueError) as exc:
            raise ContextError("guided work-order preparation did not complete") from exc
    record = context_session.update(
        association_store,
        record["session_id"],
        expected_generation=record["generation"],
        changes={"stage": "prepared"},
    )
    return _report(
        "prepared",
        stage="prepared",
        dependent_work="usable",
        next_action="Continue the build and attach context when a pull request exists.",
        work_order=record["work_order"],
        reused=reused,
    ), 0
