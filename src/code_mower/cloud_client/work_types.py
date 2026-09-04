"""Additive, versioned work-type taxonomy attached to builder/reviewer events."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import CloudBundleError


WORK_TYPE_SCHEMA = "code_mower.workType.v1"

WORK_TYPE_VALUES = (
    "web",
    "backend",
    "ios",
    "macos",
    "android",
    "infrastructure",
    "documentation",
    "unknown",
)
WORK_TYPE_SOURCE_VALUES = (
    "explicit_user",
    "repository_metadata",
    "file_category_metadata",
    "unknown",
)
WORK_TYPE_ROLE_VALUES = ("builder", "reviewer")
WORK_TYPE_ATTRIBUTION_VALUES = (
    "builder_credit",
    "reviewer_credit",
    "excluded_self_review",
)
WORK_TYPE_DIMENSIONS = (
    "work_type_schema",
    "work_type",
    "work_type_source",
    "work_type_role",
    "work_type_provider",
    "work_type_model",
    "work_type_lane_id",
    "work_type_builder_lane_id",
    "work_type_attribution",
)
# Only these event shapes may carry work-type metadata. Events that omit
# work_type_schema entirely (all uploads before this contract, and any other
# event type) remain valid.
WORK_TYPE_SUPPORTED_EVENT_TYPES = (
    "builder_run",
    "reviewer_run",
    "work_order",
    "productivity_summary",
)
# builder_run/reviewer_run pin work_type_role to a fixed value. work_order and
# productivity_summary leave role optional rather than guessing one.
WORK_TYPE_FIXED_EVENT_ROLES = {
    "builder_run": "builder",
    "reviewer_run": "reviewer",
}

# Deterministic tables. Inputs are already-coarse category labels (a GitHub
# primary language or a file-category bucket), never filenames or paths.
REPOSITORY_LANGUAGE_WORK_TYPES = {
    "swift": "ios",
    "objective-c": "ios",
    "kotlin": "android",
    "java": "android",
    "javascript": "web",
    "typescript": "web",
    "html": "web",
    "css": "web",
    "python": "backend",
    "go": "backend",
    "ruby": "backend",
    "rust": "backend",
    "hcl": "infrastructure",
    "dockerfile": "infrastructure",
    "shell": "infrastructure",
    "markdown": "documentation",
}
FILE_CATEGORY_WORK_TYPES = {
    "ios-app": "ios",
    "macos-app": "macos",
    "android-app": "android",
    "web-frontend": "web",
    "web-backend": "backend",
    "backend-service": "backend",
    "infra-config": "infrastructure",
    "ci-config": "infrastructure",
    "docs": "documentation",
}


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def resolve_work_type_classification(
    *,
    explicit: str | None = None,
    repository_language: str | None = None,
    file_category: str | None = None,
) -> tuple[str, str]:
    """Resolve (work_type, source) using explicit > repository > file-category > unknown."""

    explicit_value = _normalized(explicit)
    if explicit_value:
        if explicit_value not in WORK_TYPE_VALUES:
            raise CloudBundleError(f"unsupported explicit work_type {explicit_value!r}")
        return explicit_value, "explicit_user"

    repo_value = REPOSITORY_LANGUAGE_WORK_TYPES.get(_normalized(repository_language))
    if repo_value:
        return repo_value, "repository_metadata"

    file_value = FILE_CATEGORY_WORK_TYPES.get(_normalized(file_category))
    if file_value:
        return file_value, "file_category_metadata"

    return "unknown", "unknown"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudBundleError(f"work_type {field} must be a non-empty string")
    return value.strip()


def _identity(value: object) -> str:
    """Normalize an identity string the same way tool provenance does: strip
    and collapse internal whitespace, no case-folding."""

    return " ".join(str(value or "").strip().split())


def _validate_role_and_attribution(dimensions: Mapping[str, Any], event_type: str) -> None:
    fixed_role = WORK_TYPE_FIXED_EVENT_ROLES.get(event_type)
    role_present = dimensions.get("work_type_role") is not None
    if fixed_role is not None:
        role = _text(dimensions.get("work_type_role"), "dimension 'work_type_role'")
        if role != fixed_role:
            raise CloudBundleError(
                f"event_type {event_type!r} requires work_type_role {fixed_role!r}, not {role!r}"
            )
    elif role_present:
        role = _text(dimensions.get("work_type_role"), "dimension 'work_type_role'")
        if role not in WORK_TYPE_ROLE_VALUES:
            raise CloudBundleError(f"unsupported work_type_role {role!r}")
    else:
        if "work_type_attribution" in dimensions:
            raise CloudBundleError("dimension 'work_type_attribution' requires work_type_role")
        return

    attribution = _text(dimensions.get("work_type_attribution"), "dimension 'work_type_attribution'")
    if attribution not in WORK_TYPE_ATTRIBUTION_VALUES:
        raise CloudBundleError(f"unsupported work_type_attribution {attribution!r}")
    if role == "builder" and attribution != "builder_credit":
        raise CloudBundleError("work_type_role 'builder' requires work_type_attribution 'builder_credit'")
    if role == "reviewer" and attribution == "builder_credit":
        raise CloudBundleError("work_type_role 'reviewer' cannot use work_type_attribution 'builder_credit'")

    lane_id = dimensions.get("work_type_lane_id")
    builder_lane_id = dimensions.get("work_type_builder_lane_id")
    if (
        role == "reviewer"
        and isinstance(lane_id, str)
        and isinstance(builder_lane_id, str)
        and lane_id.strip()
        and lane_id.strip() == builder_lane_id.strip()
        and attribution == "reviewer_credit"
    ):
        raise CloudBundleError(
            "an author lane cannot count as independent review; "
            "use work_type_attribution 'excluded_self_review' when reviewer and builder lanes match"
        )


def _validate_identity_agreement(dimensions: Mapping[str, Any], tool: Mapping[str, Any]) -> None:
    work_provider = _identity(dimensions.get("work_type_provider"))
    tool_provider = _identity(tool.get("provider"))
    if work_provider and tool_provider and work_provider != tool_provider:
        raise CloudBundleError(
            f"work_type_provider {work_provider!r} does not match tool.provider {tool_provider!r}"
        )

    work_model = _identity(dimensions.get("work_type_model"))
    tool_model = _identity(tool.get("model"))
    if work_model and tool_model and work_model != tool_model:
        raise CloudBundleError(
            f"work_type_model {work_model!r} does not match tool.model {tool_model!r}"
        )


def validate_work_type_metadata(
    dimensions: Mapping[str, Any],
    event_type: str,
    tool: Mapping[str, Any] | None = None,
) -> None:
    """Validate optional work-type dimensions; a no-op for legacy uploads."""

    if "work_type_schema" not in dimensions:
        return
    if event_type not in WORK_TYPE_SUPPORTED_EVENT_TYPES:
        allowed = ", ".join(WORK_TYPE_SUPPORTED_EVENT_TYPES)
        raise CloudBundleError(
            f"work_type metadata is not supported on event_type {event_type!r}; allowed: {allowed}"
        )
    if dimensions.get("work_type_schema") != WORK_TYPE_SCHEMA:
        raise CloudBundleError(f"dimensions.work_type_schema must be {WORK_TYPE_SCHEMA!r}")

    work_type = _text(dimensions.get("work_type"), "dimension 'work_type'")
    if work_type not in WORK_TYPE_VALUES:
        raise CloudBundleError(f"unsupported work_type {work_type!r}")

    source = _text(dimensions.get("work_type_source"), "dimension 'work_type_source'")
    if source not in WORK_TYPE_SOURCE_VALUES:
        raise CloudBundleError(f"unsupported work_type_source {source!r}")
    if work_type == "unknown" and source not in {"unknown", "explicit_user"}:
        raise CloudBundleError(
            "work_type 'unknown' requires work_type_source 'unknown' or 'explicit_user'"
        )

    _validate_role_and_attribution(dimensions, event_type)
    _validate_identity_agreement(dimensions, tool if isinstance(tool, Mapping) else {})
