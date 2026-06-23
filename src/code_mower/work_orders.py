#!/usr/bin/env python3
"""Project context and work-order helpers for Code Mower authoring loops."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from code_mower import __version__


PROJECT_CONTEXT_SCHEMA = "code_mower.projectContext.v1"
EXTERNAL_CONTEXT_SCHEMA = "code_mower.externalContextManifest.v1"
ISSUE_PLAN_SCHEMA = "code_mower.issuePlan.v1"
WORK_ORDER_SCHEMA = "code_mower.workOrder.v1"
CRITIQUE_PLAN_SCHEMA = "code_mower.workOrderCritiquePlan.v1"
WORK_ORDER_BUILDER_SPEC_SCHEMA = "code_mower.workOrderBuilderExperimentSeed.v1"
BENCHMARK_EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
PLAN_STATE_SCHEMA = "code_mower.planState.v1"
PLAN_STATE_TRAILER_PREFIX = "<!-- CODE_MOWER_PLAN_STATE:"

DEFAULT_PROJECT_CONTEXT_DIR = Path(".code-mower/project-context")
DEFAULT_EXTERNAL_CONTEXT_DIR = Path(".code-mower/context/external")
DEFAULT_WORK_ORDER_DIR = Path(".code-mower/work-orders")
DEFAULT_CRITIQUE_DIR = Path(".code-mower/work-orders/critique-prompts")
SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
TEXT_EXTENSIONS = {
    ".css",
    ".csv",
    ".html",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class ProjectContextDocument:
    filename: str
    title: str
    purpose: str
    sections: tuple[str, ...]


@dataclass(frozen=True)
class IssuePlanInput:
    title: str
    source_text: str
    repo: str
    source: dict[str, Any]


PROJECT_CONTEXT_DOCUMENTS: tuple[ProjectContextDocument, ...] = (
    ProjectContextDocument(
        "architecture.md",
        "Architecture",
        "Durable architecture facts that builders and reviewers should preserve.",
        (
            "System shape",
            "Important boundaries",
            "Data ownership",
            "Dependency policy",
            "Known architectural risks",
        ),
    ),
    ProjectContextDocument(
        "hosting-environment.md",
        "Hosting Environment",
        "Runtime, hosting, deployment, and operational constraints.",
        (
            "Runtime targets",
            "Hosting providers",
            "Required environment variables",
            "Deployment checks",
            "Rollback expectations",
        ),
    ),
    ProjectContextDocument(
        "ci-cd.md",
        "CI/CD",
        "The native check surface that Code Mower should respect before merge.",
        (
            "Required local checks",
            "Required GitHub checks",
            "Cost-sensitive workflows",
            "Release process",
            "Known flaky or informational checks",
        ),
    ),
    ProjectContextDocument(
        "design-system.md",
        "Design System",
        "Product and UI constraints that should guide frontend work.",
        (
            "Product tone",
            "Layout principles",
            "Component conventions",
            "Accessibility expectations",
            "Visual anti-patterns",
        ),
    ),
    ProjectContextDocument(
        "quality-bar.md",
        "Quality Bar",
        "How this repo defines a good change, beyond passing CI.",
        (
            "Correctness expectations",
            "Testing expectations",
            "Security and privacy expectations",
            "Operability expectations",
            "Documentation expectations",
        ),
    ),
    ProjectContextDocument(
        "agent-team.md",
        "Agent Team",
        "Role lenses to use when drafting or critiquing implementation work.",
        (
            "Product manager",
            "Architect",
            "Full-stack implementer",
            "QA and context-driven tester",
            "Security reviewer",
            "Operability reviewer",
            "Devil's advocate",
        ),
    ),
    ProjectContextDocument(
        "work-spec-template.md",
        "Work Spec Template",
        "A reusable issue/work-order template for agent-driven implementation.",
        (
            "Problem",
            "Context files",
            "Non-goals",
            "Implementation plan",
            "Acceptance criteria",
            "Review lanes",
            "Open questions",
        ),
    ),
)

DEFAULT_ROLE_LENSES = (
    "product-manager",
    "architect",
    "implementer",
    "qa-context-driven-tester",
    "security-threat-model",
    "operability",
    "devils-advocate",
)
DEFAULT_REVIEW_LANES = ("codex-audit", "claude-audit", "gitar")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_slug(value: Any, fallback: str = "work-order") -> str:
    text = SAFE_SLUG_RE.sub("-", str(value or "").strip()).strip("._-").lower()
    while ".." in text:
        text = text.replace("..", ".")
    return text or fallback


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc


def _read_text_prefix(path: Path, max_chars: int) -> tuple[str, bool]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            text = handle.read(max_chars + 1)
    except UnicodeDecodeError:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_chars + 1)
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    return text[:max_chars], len(text) > max_chars


def _write_text(path: Path, text: str, *, force: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def _write_json(path: Path, payload: Mapping[str, Any], *, force: bool = True) -> bool:
    return _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        force=force,
    )


def _require_writable(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise ValueError(f"{path} already exists; pass --force to overwrite")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _document_template(document: ProjectContextDocument, project_name: str) -> str:
    lines = [
        f"# {document.title}",
        "",
        f"Project: {project_name or 'TODO'}",
        "",
        document.purpose,
        "",
        "This file is local project context. Keep it source-controlled only if it is safe",
        "for your repo. Code Mower treats it as planning input, not cloud-bound data.",
        "",
    ]
    for section in document.sections:
        lines.extend([f"## {section}", "", "TODO", ""])
    return "\n".join(lines).rstrip() + "\n"


def create_project_context(
    *,
    output_dir: Path = DEFAULT_PROJECT_CONTEXT_DIR,
    project_name: str = "",
    force: bool = False,
) -> dict[str, Any]:
    created: list[str] = []
    skipped: list[str] = []
    documents: list[dict[str, Any]] = []
    for document in PROJECT_CONTEXT_DOCUMENTS:
        path = output_dir / document.filename
        wrote = _write_text(
            path,
            _document_template(document, project_name),
            force=force,
        )
        if wrote:
            created.append(str(path))
        else:
            skipped.append(str(path))
        documents.append(
            {
                "path": str(path),
                "title": document.title,
                "purpose": document.purpose,
            }
        )
    manifest = {
        "schema": PROJECT_CONTEXT_SCHEMA,
        "created_at": _utc_now(),
        "project_name": project_name,
        "output_dir": str(output_dir),
        "documents": documents,
        "cloud_policy": "local-only; do not upload raw context docs by default",
    }
    manifest_path = output_dir / "project-context-manifest.json"
    _write_json(manifest_path, manifest, force=True)
    return {
        "mode": "project-context-init",
        "schema": PROJECT_CONTEXT_SCHEMA,
        "project_name": project_name,
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path),
        "created": created,
        "skipped": skipped,
    }


def _render_project_context_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower project context",
        f"Output: {report.get('output_dir')}",
        f"Manifest: {report.get('manifest_path')}",
        "",
        f"Created: {len(report.get('created', []) or [])}",
        f"Skipped existing: {len(report.get('skipped', []) or [])}",
    ]
    created = report.get("created", [])
    if isinstance(created, list) and created:
        lines.extend(["", "Created files:"])
        lines.extend(f"- {path}" for path in created)
    skipped = report.get("skipped", [])
    if isinstance(skipped, list) and skipped:
        lines.extend(["", "Skipped files:"])
        lines.extend(f"- {path}" for path in skipped)
    return "\n".join(lines) + "\n"


def _external_context_entry(
    source: Path,
    *,
    output_dir: Path,
    include_preview: bool,
    max_preview_chars: int,
) -> dict[str, Any]:
    resolved = source.expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"external context file does not exist: {source}")
    if not resolved.is_file():
        raise ValueError(f"external context path must be a file: {source}")
    entry: dict[str, Any] = {
        "source_path": str(resolved),
        "filename": resolved.name,
        "extension": resolved.suffix.lower(),
        "bytes": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
        "text_preview_included": False,
        "cloud_policy": "metadata-only by default; raw file stays local",
    }
    if include_preview and resolved.suffix.lower() in TEXT_EXTENSIONS:
        preview, preview_truncated = _read_text_prefix(resolved, max_preview_chars)
        preview_name = f"{_safe_slug(resolved.stem, 'external')}-{entry['sha256'][:10]}.preview.txt"
        preview_path = output_dir / "previews" / preview_name
        _write_text(preview_path, preview.rstrip() + "\n", force=True)
        entry.update(
            {
                "text_preview_included": True,
                "text_preview_path": str(preview_path),
                "text_preview_chars": len(preview),
                "text_preview_truncated": preview_truncated,
            }
        )
    return entry


def _external_context_entry_key(entry: Mapping[str, Any]) -> str:
    source_path = str(entry.get("source_path") or "")
    if source_path:
        return f"source:{source_path}"
    sha256 = str(entry.get("sha256") or "")
    if sha256:
        return f"sha256:{sha256}"
    return f"filename:{entry.get('filename', '')}"


def _read_existing_external_context_entries(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read existing external context manifest {manifest_path}: {exc}") from exc
    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def _merge_external_context_entries(
    existing: Sequence[Mapping[str, Any]],
    added: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for entry in existing:
        key = _external_context_entry_key(entry)
        positions[key] = len(merged)
        merged.append(dict(entry))
    for entry in added:
        key = _external_context_entry_key(entry)
        if key in positions:
            merged[positions[key]] = dict(entry)
        else:
            positions[key] = len(merged)
            merged.append(dict(entry))
    return merged


def add_external_context(
    paths: Sequence[Path],
    *,
    output_dir: Path = DEFAULT_EXTERNAL_CONTEXT_DIR,
    include_preview: bool = False,
    max_preview_chars: int = 8000,
) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one --external file is required")
    if max_preview_chars < 0:
        raise ValueError("--max-preview-chars must be greater than or equal to zero")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "external-context-manifest.json"
    existing_entries = _read_existing_external_context_entries(manifest_path)
    added_entries = [
        _external_context_entry(
            path,
            output_dir=output_dir,
            include_preview=include_preview,
            max_preview_chars=max_preview_chars,
        )
        for path in paths
    ]
    entries = _merge_external_context_entries(existing_entries, added_entries)
    added_keys = {_external_context_entry_key(entry) for entry in added_entries}
    preserved_entry_count = sum(
        1 for entry in existing_entries if _external_context_entry_key(entry) not in added_keys
    )
    manifest = {
        "mode": "external-context-add",
        "schema": EXTERNAL_CONTEXT_SCHEMA,
        "created_at": _utc_now(),
        "output_dir": str(output_dir),
        "entry_count": len(entries),
        "added_entry_count": len(added_entries),
        "preserved_entry_count": preserved_entry_count,
        "entries": entries,
        "cloud_policy": (
            "This manifest records local external context metadata. Do not upload "
            "raw external docs or previews unless a user explicitly opts in."
        ),
    }
    _write_json(manifest_path, manifest, force=True)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _render_external_context_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower external context manifest",
        f"Entries: {report.get('entry_count', 0)}",
        f"Manifest: {report.get('manifest_path')}",
        "",
        "Files:",
    ]
    for entry in report.get("entries", []) or []:
        if isinstance(entry, Mapping):
            preview = " preview" if entry.get("text_preview_included") else ""
            lines.append(f"- {entry.get('filename')} ({entry.get('bytes')} bytes){preview}")
    return "\n".join(lines) + "\n"


def _read_optional_body(body_file: Path | None, body: str | None = None) -> str:
    if body_file is not None:
        return _read_text(body_file)
    return body or ""


def _plan_state_from_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PLAN_STATE_SCHEMA,
        "issue_plan_schema": plan.get("schema"),
        "repo": plan.get("repo") or "",
        "issue_number": str(plan.get("issue_number") or ""),
        "issue_url": plan.get("issue_url") or "",
        "title": plan.get("title") or "",
        "status": "ready",
        "created_at": plan.get("created_at") or _utc_now(),
    }


def _plan_state_trailer(plan: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        _plan_state_from_plan(plan),
        sort_keys=True,
        separators=(",", ":"),
    )
    serialized = serialized.replace("-->", "--\\u003e")
    return f"{PLAN_STATE_TRAILER_PREFIX} {serialized} -->"


def _extract_plan_state(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(PLAN_STATE_TRAILER_PREFIX):
            continue
        raw = stripped.removeprefix(PLAN_STATE_TRAILER_PREFIX).strip()
        if raw.endswith("-->"):
            raw = raw[:-3].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _extract_backticked_metadata(text: str, label: str) -> str:
    pattern = re.compile(rf"^{re.escape(label)}:\s*`([^`]+)`\s*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _extract_issue_metadata(text: str) -> str:
    pattern = re.compile(r"^Issue:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    return "" if value == "TODO" else value


def _source_from_issue_plan(
    *,
    payload: Mapping[str, Any] | None = None,
    text: str = "",
    path: Path,
) -> dict[str, Any]:
    state = _extract_plan_state(text) if text else {}
    payload = payload or {}
    repo = str(payload.get("repo") or state.get("repo") or "").strip()
    issue_number = str(
        payload.get("issue_number") or state.get("issue_number") or ""
    ).strip()
    issue_url = str(payload.get("issue_url") or state.get("issue_url") or "").strip()
    if text:
        repo = repo or _extract_backticked_metadata(text, "Repository")
        issue_value = _extract_issue_metadata(text)
        if issue_value:
            if issue_value.startswith("http"):
                issue_url = issue_url or issue_value
            else:
                issue_number = issue_number or issue_value
    source_type = "github_issue" if repo and (issue_number or issue_url) else "issue_plan"
    return {
        "type": source_type,
        "issue_plan_file": path.name,
        "repo": repo,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "title": str(payload.get("title") or state.get("title") or "").strip(),
        "plan_schema": str(payload.get("schema") or state.get("issue_plan_schema") or "").strip(),
    }


def _read_issue_plan_for_work_order(path: Path) -> IssuePlanInput:
    text = _read_text(path)
    if path.suffix.lower() != ".json":
        title = _strip_issue_plan_title_prefix(_extract_heading_title(text, path.stem))
        source = _source_from_issue_plan(text=text, path=path)
        if source.get("title"):
            title = str(source["title"])
        return IssuePlanInput(
            title=title,
            source_text=text,
            repo=str(source.get("repo") or ""),
            source=source,
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse issue plan JSON {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"issue plan JSON must be an object: {path}")
    title = str(payload.get("title") or _extract_heading_title(text, path.stem))
    source = _source_from_issue_plan(payload=payload, path=path)
    return IssuePlanInput(
        title=title,
        source_text=render_issue_plan_markdown(payload),
        repo=str(source.get("repo") or ""),
        source=source,
    )


def build_issue_plan(
    *,
    title: str,
    body: str,
    issue_url: str = "",
    repo: str = "",
    issue_number: str = "",
) -> dict[str, Any]:
    clean_title = title.strip() or "Untitled work item"
    prompt = body.strip()
    return {
        "mode": "issue-plan",
        "schema": ISSUE_PLAN_SCHEMA,
        "created_at": _utc_now(),
        "title": clean_title,
        "slug": _safe_slug(clean_title),
        "repo": repo,
        "issue_url": issue_url,
        "issue_number": issue_number,
        "source": {
            "type": "github_issue" if repo and (issue_url or issue_number) else "manual_issue",
            "repo": repo,
            "issue_number": issue_number,
            "issue_url": issue_url,
        },
        "body": prompt,
        "sections": {
            "problem": prompt or "TODO: paste the issue problem statement.",
            "context": "TODO: list project-context and external-context files to consult.",
            "non_goals": "TODO: call out what should not change.",
            "acceptance": "TODO: define observable acceptance criteria.",
            "review": "Use the normal Code Mower audit protocol before merge.",
        },
    }


def parse_github_issue_ref(issue_ref: str, *, repo: str = "") -> tuple[str, str]:
    """Return (repo, issue_number) for owner/repo#123, URL, or number + --repo."""

    raw = issue_ref.strip()
    repo = repo.strip()
    if not raw:
        raise ValueError("issue reference is required")
    url_match = re.match(r"^https://([^/\s]+)/([^/\s]+/[^/\s]+)/issues/([0-9]+)(?:[/?#].*)?$", raw)
    if url_match:
        host = url_match.group(1)
        repo_slug = url_match.group(2)
        issue_number = url_match.group(3)
        if host != "github.com":
            repo_slug = f"{host}/{repo_slug}"
        return repo_slug, issue_number
    slug_match = re.match(r"^([^/\s]+/[^#\s]+)#([0-9]+)$", raw)
    if slug_match:
        return slug_match.group(1), slug_match.group(2)
    if raw.isdigit() and repo:
        return repo, raw
    if raw.startswith("#") and raw[1:].isdigit() and repo:
        return repo, raw[1:]
    raise ValueError(
        "issue reference must be owner/repo#123, a GitHub issue URL, "
        "or an issue number with --repo"
    )


def fetch_github_issue(
    issue_ref: str,
    *,
    repo: str = "",
    gh_path: str = "gh",
) -> dict[str, Any]:
    issue_repo, issue_number = parse_github_issue_ref(issue_ref, repo=repo)
    command = [
        gh_path,
        "issue",
        "view",
        issue_number,
        "--repo",
        issue_repo,
        "--json",
        "title,body,number,url,state,labels,author",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not run gh issue view: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValueError(f"gh issue view failed: {detail or completed.returncode}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse gh issue view JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("gh issue view JSON was not an object")
    return {
        **dict(payload),
        "repo": issue_repo,
        "number": str(payload.get("number") or issue_number),
    }


def issue_plan_comment_body(plan: Mapping[str, Any]) -> str:
    return (
        "## Code Mower Issue Plan\n\n"
        f"{render_issue_plan_markdown(plan).strip()}\n\n"
        f"{_plan_state_trailer(plan)}\n"
    )


def post_github_issue_plan(
    plan: Mapping[str, Any],
    *,
    gh_path: str = "gh",
) -> dict[str, Any]:
    repo = str(plan.get("repo") or "")
    issue_number = str(plan.get("issue_number") or "")
    if not repo or not issue_number:
        raise ValueError("cannot post issue plan without repo and issue number")
    body = issue_plan_comment_body(plan)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as fh:
        body_path = Path(fh.name)
        fh.write(body)
    try:
        completed = subprocess.run(
            [
                gh_path,
                "issue",
                "comment",
                issue_number,
                "--repo",
                repo,
                "--body-file",
                str(body_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not run gh issue comment: {exc}") from exc
    finally:
        try:
            body_path.unlink()
        except OSError:
            pass
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValueError(f"gh issue comment failed: {detail or completed.returncode}")
    return {
        "posted": True,
        "repo": repo,
        "issue_number": issue_number,
        "url": (completed.stdout or "").strip(),
    }


def render_issue_plan_markdown(
    plan: Mapping[str, Any],
    *,
    include_state_trailer: bool = False,
) -> str:
    lines = [
        f"# Issue Plan: {plan.get('title')}",
        "",
        f"Schema: `{plan.get('schema')}`",
        f"Repository: `{plan.get('repo') or 'TODO'}`",
        f"Issue: {plan.get('issue_url') or plan.get('issue_number') or 'TODO'}",
        "",
        "## Problem",
        "",
        str(plan.get("sections", {}).get("problem", "") if isinstance(plan.get("sections"), Mapping) else ""),
        "",
        "## Context To Load",
        "",
        str(plan.get("sections", {}).get("context", "") if isinstance(plan.get("sections"), Mapping) else ""),
        "",
        "## Non-Goals",
        "",
        str(plan.get("sections", {}).get("non_goals", "") if isinstance(plan.get("sections"), Mapping) else ""),
        "",
        "## Acceptance Criteria",
        "",
        str(plan.get("sections", {}).get("acceptance", "") if isinstance(plan.get("sections"), Mapping) else ""),
        "",
        "## Review Protocol",
        "",
        str(plan.get("sections", {}).get("review", "") if isinstance(plan.get("sections"), Mapping) else ""),
        "",
    ]
    if include_state_trailer:
        lines.extend([_plan_state_trailer(plan), ""])
    return "\n".join(lines)


def write_issue_plan(plan: Mapping[str, Any], output: Path, *, force: bool = False) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    _require_writable(output, force=force)
    if output.suffix.lower() == ".json":
        _write_json(output, plan, force=True)
    else:
        _write_text(
            output,
            render_issue_plan_markdown(plan, include_state_trailer=True),
            force=True,
        )
    return {**dict(plan), "output_path": str(output)}


def _extract_heading_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    return fallback


def _strip_issue_plan_title_prefix(title: str) -> str:
    prefix = "Issue Plan:"
    if title.lower().startswith(prefix.lower()):
        return title[len(prefix) :].strip() or title
    return title


def _context_manifest_rows(path: Path | None) -> list[str]:
    if path is None:
        return ["- TODO: add project-context or external-context manifest paths."]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"- {path} (could not read: {exc})"]
    rows = [f"- Manifest: `{path}`"]
    for entry in payload.get("entries", []) or payload.get("documents", []) or []:
        if isinstance(entry, Mapping):
            rows.append(f"  - {entry.get('filename') or entry.get('path')}")
    return rows


def _work_order_source_rows(source: Mapping[str, Any] | None) -> list[str]:
    if not source:
        return []
    rows = ["", "## Source", ""]
    source_type = str(source.get("type") or "unknown")
    rows.append(f"- Source type: `{source_type}`")
    repo = str(source.get("repo") or "").strip()
    if repo:
        rows.append(f"- Repository: `{repo}`")
    issue_url = str(source.get("issue_url") or "").strip()
    issue_number = str(source.get("issue_number") or "").strip()
    if issue_url:
        rows.append(f"- Issue: {issue_url}")
    elif issue_number:
        rows.append(f"- Issue: `{issue_number}`")
    issue_plan_file = str(source.get("issue_plan_file") or "").strip()
    if issue_plan_file:
        rows.append(f"- Issue plan file: `{issue_plan_file}`")
    return rows


def _cloud_work_order_event_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.cloud-event.json")


def _work_order_event_id(manifest: Mapping[str, Any]) -> str:
    source = manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    seed = "|".join(
        [
            "code-mower-work-order",
            str(manifest.get("repo") or ""),
            str(source.get("issue_url") or source.get("issue_number") or ""),
            str(manifest.get("slug") or manifest.get("title") or ""),
        ]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def build_work_order_cloud_event(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Build a source-free cloud metadata event for a drafted work order."""

    source = manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    role_lenses = list(manifest.get("role_lenses") or [])
    review_lanes = list(manifest.get("review_lanes") or [])
    return {
        "schema": BENCHMARK_EVENT_SCHEMA,
        "event_id": _work_order_event_id(manifest),
        "event_type": "work_order",
        "created_at": str(manifest.get("created_at") or _utc_now()),
        "repo_slug": str(manifest.get("repo") or source.get("repo") or ""),
        "team_id": "",
        "install_id": "",
        "source": "code-mower-work-order",
        "provider": "code-mower",
        "lens": "planning",
        "status": "drafted",
        "tool": {
            "role": "planner",
            "tool_name": "code-mower",
            "tool_version": __version__,
            "provider": "code-mower",
            "model": "",
            "model_source": "not_applicable",
            "version_source": "package_version",
            "integration": "work-order",
            "lens": "planning",
            "source": "code-mower-work-order",
        },
        "metrics": {
            "role_lens_count": len(role_lenses),
            "review_lane_count": len(review_lanes),
        },
        "dimensions": {
            "source_type": str(source.get("type") or ""),
            "issue_repo": str(source.get("repo") or ""),
            "issue_number": str(source.get("issue_number") or ""),
            "issue_url": str(source.get("issue_url") or ""),
            "issue_plan_file": str(source.get("issue_plan_file") or ""),
            "work_order_file": Path(str(manifest.get("output_path") or "")).name,
            "work_order_manifest_file": Path(str(manifest.get("manifest_path") or "")).name,
            "work_order_slug": str(manifest.get("slug") or ""),
            "role_lenses": role_lenses,
            "review_lanes": review_lanes,
        },
    }


def render_work_order(
    *,
    title: str,
    source_text: str,
    repo: str = "",
    context_manifest: Path | None = None,
    role_lenses: Sequence[str] = (),
    review_lanes: Sequence[str] = (),
    source: Mapping[str, Any] | None = None,
) -> str:
    role_lenses = tuple(role_lenses) or DEFAULT_ROLE_LENSES
    review_lanes = tuple(review_lanes) or DEFAULT_REVIEW_LANES
    lines = [
        f"# Work Order: {title}",
        "",
        f"Schema: `{WORK_ORDER_SCHEMA}`",
        f"Repository: `{repo or 'TODO'}`",
        *_work_order_source_rows(source),
        "",
        "## Objective",
        "",
        source_text.strip() or "TODO: describe the work.",
        "",
        "## Context",
        "",
        *_context_manifest_rows(context_manifest),
        "",
        "## Role/Lens Passes",
        "",
    ]
    for lens in role_lenses:
        lines.extend(
            [
                f"### {lens}",
                "",
                "- What would this role add, constrain, or challenge before implementation?",
                "- What risks would this role want reviewed before merge?",
                "",
            ]
        )
    lines.extend(
        [
            "## Implementation Contract",
            "",
            "- Keep the change scoped to this work order.",
            "- Prefer existing repo patterns and native checks.",
            "- Record assumptions and unresolved questions explicitly.",
            "- Do not use reviewer output as builder input until the builder declares the run complete.",
            "",
            "## Acceptance Criteria",
            "",
            "- TODO: add observable acceptance criteria.",
            "- Native repo checks pass.",
            "- Code Mower merge bar is clean before merge.",
            "",
            "## Review Lanes",
            "",
        ]
    )
    lines.extend(f"- {lane}" for lane in review_lanes)
    lines.extend(
        [
            "",
            "## Builder Prompt",
            "",
            "Implement the work order above. Use the context files intentionally, preserve the implementation contract, and stop only for a true blocker.",
            "",
        ]
    )
    return "\n".join(lines)


def draft_work_order(
    *,
    title: str,
    source_text: str,
    repo: str = "",
    context_manifest: Path | None = None,
    role_lenses: Sequence[str] = (),
    review_lanes: Sequence[str] = (),
    source: Mapping[str, Any] | None = None,
    output: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    title = title.strip() or _extract_heading_title(source_text, "Untitled work order")
    output = output or (DEFAULT_WORK_ORDER_DIR / f"{_safe_slug(title)}.md")
    if output.suffix.lower() == ".json":
        raise ValueError("work-order draft --output must be a Markdown path, not .json")
    manifest_path = output.with_suffix(".json")
    cloud_event_path = _cloud_work_order_event_path(output)
    _require_writable(output, force=force)
    _require_writable(manifest_path, force=force)
    _require_writable(cloud_event_path, force=force)
    effective_role_lenses = tuple(role_lenses) or DEFAULT_ROLE_LENSES
    effective_review_lanes = tuple(review_lanes) or DEFAULT_REVIEW_LANES
    source_metadata = dict(source or {})
    if repo and not source_metadata.get("repo"):
        source_metadata["repo"] = repo
    markdown = render_work_order(
        title=title,
        source_text=source_text,
        repo=repo,
        context_manifest=context_manifest,
        role_lenses=effective_role_lenses,
        review_lanes=effective_review_lanes,
        source=source_metadata,
    )
    _write_text(output, markdown, force=True)
    manifest = {
        "mode": "work-order-draft",
        "schema": WORK_ORDER_SCHEMA,
        "created_at": _utc_now(),
        "title": title,
        "slug": _safe_slug(title),
        "repo": repo,
        "output_path": str(output),
        "context_manifest": str(context_manifest) if context_manifest else "",
        "role_lenses": list(effective_role_lenses),
        "review_lanes": list(effective_review_lanes),
        "source": source_metadata,
        "cloud_event_path": str(cloud_event_path),
    }
    manifest["manifest_path"] = str(manifest_path)
    _write_json(manifest_path, manifest, force=True)
    cloud_event = build_work_order_cloud_event(manifest)
    _write_json(cloud_event_path, cloud_event, force=True)
    return manifest


def create_critique_plan(
    work_order: Path,
    *,
    reviewers: Sequence[str],
    output_dir: Path = DEFAULT_CRITIQUE_DIR,
) -> dict[str, Any]:
    if not reviewers:
        raise ValueError("at least one --reviewer is required")
    text = _read_text(work_order)
    title = _extract_heading_title(text, work_order.stem)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompts: list[dict[str, str]] = []
    for reviewer in reviewers:
        reviewer_slug = _safe_slug(reviewer, "reviewer")
        prompt_path = output_dir / f"{_safe_slug(work_order.stem)}-{reviewer_slug}.md"
        prompt = "\n".join(
            [
                f"# Work Order Critique Prompt: {reviewer}",
                "",
                "You are critiquing the implementation plan, not writing code.",
                "Improve specificity, uncover missing context, and identify risk.",
                "Return only plan improvements, blockers, and questions.",
                "",
                "## Work Order",
                "",
                text,
            ]
        )
        _write_text(prompt_path, prompt, force=True)
        prompts.append({"reviewer": reviewer, "prompt_path": str(prompt_path)})
    manifest = {
        "mode": "work-order-critique-plan",
        "schema": CRITIQUE_PLAN_SCHEMA,
        "created_at": _utc_now(),
        "work_order": str(work_order),
        "title": title,
        "reviewers": list(reviewers),
        "prompts": prompts,
    }
    manifest_path = output_dir / f"{_safe_slug(work_order.stem)}-critique-plan.json"
    _write_json(manifest_path, manifest, force=True)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def seed_builder_experiment(
    work_order: Path,
    *,
    repo: str,
    builders: Sequence[str],
    output: Path,
    task_class: str = "general",
    base_ref: str = "origin/main",
    context_packs: Sequence[str] = (),
    prompt_lenses: Sequence[str] = (),
) -> dict[str, Any]:
    if "/" not in repo:
        raise ValueError("--repo must be an owner/repo slug")
    if not builders:
        raise ValueError("at least one --builder is required")
    text = _read_text(work_order)
    title = _extract_heading_title(text, work_order.stem)
    task_id = _safe_slug(title, "work-order-task")
    spec = {
        "version": 1,
        "schema": WORK_ORDER_BUILDER_SPEC_SCHEMA,
        "name": f"{task_id}-builder-experiment",
        "description": f"Builder experiment seeded from {work_order}",
        "tasks": [
            {
                "task_id": task_id,
                "run_role": "implement",
                "repo": repo,
                "base_ref": base_ref,
                "task_class": task_class,
                "prompt": text,
                "success_criteria": [
                    "native repo checks pass",
                    "Code Mower audit protocol is clean",
                    "post-merge health is verified",
                ],
                "context_packs": list(context_packs),
                "review_classes": [task_class] if task_class else [],
                "notes": "Seeded by code-mower work-order builder-experiment.",
            }
        ],
        "builders": [
            {
                "builder_id": _safe_slug(builder, "builder"),
                "provider": builder.split("-", 1)[0] if "-" in builder else builder,
                "tool": builder,
                "prompt_lenses": list(prompt_lenses),
                "context_packs": list(context_packs),
                "cost_policy": "provider-dependent",
            }
            for builder in builders
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, spec, force=True)
    return {
        "mode": "work-order-builder-experiment",
        "schema": WORK_ORDER_BUILDER_SPEC_SCHEMA,
        "output_path": str(output),
        "task_id": task_id,
        "builder_count": len(builders),
        "builders": list(builders),
    }


def _print_payload(payload: Mapping[str, Any], *, as_json: bool, text: str) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(text, end="")
    return 0


def project_context_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower project-context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--output-dir", type=Path, default=DEFAULT_PROJECT_CONTEXT_DIR)
    init_parser.add_argument("--project-name", default="")
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "init":
        report = create_project_context(
            output_dir=args.output_dir,
            project_name=args.project_name,
            force=args.force,
        )
        return _print_payload(
            report,
            as_json=args.json,
            text=_render_project_context_text(report),
        )
    raise AssertionError(f"unhandled project-context command: {args.command}")


def context_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--external", type=Path, action="append", default=[])
    add_parser.add_argument("--output-dir", type=Path, default=DEFAULT_EXTERNAL_CONTEXT_DIR)
    add_parser.add_argument("--include-preview", action="store_true")
    add_parser.add_argument("--max-preview-chars", type=int, default=8000)
    add_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "add":
        try:
            report = add_external_context(
                args.external,
                output_dir=args.output_dir,
                include_preview=args.include_preview,
                max_preview_chars=args.max_preview_chars,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return _print_payload(
            report,
            as_json=args.json,
            text=_render_external_context_text(report),
        )
    raise AssertionError(f"unhandled context command: {args.command}")


def plan_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower plan")
    subparsers = parser.add_subparsers(dest="command", required=True)
    issue_parser = subparsers.add_parser("from-issue")
    issue_parser.add_argument("--title", required=True)
    issue_parser.add_argument("--body-file", type=Path)
    issue_parser.add_argument("--body", default="")
    issue_parser.add_argument("--issue-url", default="")
    issue_parser.add_argument("--issue-number", default="")
    issue_parser.add_argument("--repo", default="")
    issue_parser.add_argument("--output", type=Path)
    issue_parser.add_argument("--force", action="store_true")
    issue_parser.add_argument("--json", action="store_true")
    github_parser = subparsers.add_parser(
        "from-github-issue",
        help="Create a plan from a GitHub issue, optionally posting it back.",
    )
    github_parser.add_argument(
        "issue_ref",
        help="Issue ref: owner/repo#123, GitHub issue URL, or number with --repo.",
    )
    github_parser.add_argument("--repo", default="", help="owner/repo for numeric issue refs.")
    github_parser.add_argument("--output", type=Path, help="Optional local derived plan path.")
    github_parser.add_argument(
        "--post",
        action="store_true",
        help="Post the plan back to the GitHub issue as a structured comment.",
    )
    github_parser.add_argument("--force", action="store_true")
    github_parser.add_argument("--gh-path", default="gh", help="Path to the GitHub CLI.")
    github_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "from-issue":
        try:
            body = _read_optional_body(args.body_file, args.body)
            plan = build_issue_plan(
                title=args.title,
                body=body,
                issue_url=args.issue_url,
                repo=args.repo,
                issue_number=args.issue_number,
            )
            if args.output:
                payload = write_issue_plan(plan, args.output, force=args.force)
                text = (
                    f"Code Mower issue plan\nTitle: {payload.get('title')}\n"
                    f"Output: {payload.get('output_path', '(stdout only)')}\n"
                )
            else:
                payload = plan
                text = render_issue_plan_markdown(plan)
            return _print_payload(payload, as_json=args.json, text=text)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command == "from-github-issue":
        try:
            issue = fetch_github_issue(
                args.issue_ref,
                repo=args.repo,
                gh_path=args.gh_path,
            )
            plan = build_issue_plan(
                title=str(issue.get("title") or ""),
                body=str(issue.get("body") or ""),
                issue_url=str(issue.get("url") or ""),
                repo=str(issue.get("repo") or ""),
                issue_number=str(issue.get("number") or ""),
            )
            payload: dict[str, Any]
            if args.output:
                payload = write_issue_plan(plan, args.output, force=args.force)
                text = (
                    f"Code Mower issue plan\nTitle: {payload.get('title')}\n"
                    f"Source: {payload.get('issue_url') or args.issue_ref}\n"
                    f"Output: {payload.get('output_path', '(stdout only)')}\n"
                )
            else:
                payload = dict(plan)
                text = render_issue_plan_markdown(plan)
            if args.post:
                post = post_github_issue_plan(plan, gh_path=args.gh_path)
                payload["posted_comment"] = post
                if not args.output:
                    text = (
                        f"Code Mower issue plan\nTitle: {payload.get('title')}\n"
                        f"Source: {payload.get('issue_url') or args.issue_ref}\n"
                    )
                text += f"Posted: {post.get('url') or 'yes'}\n"
            return _print_payload(payload, as_json=args.json, text=text)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    raise AssertionError(f"unhandled plan command: {args.command}")


def work_order_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower work-order")
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft_parser = subparsers.add_parser("draft")
    draft_parser.add_argument("--title", default="")
    draft_parser.add_argument("--body-file", type=Path)
    draft_parser.add_argument("--body", default="")
    draft_parser.add_argument("--issue-plan", type=Path)
    draft_parser.add_argument("--repo", default="")
    draft_parser.add_argument("--context-manifest", type=Path)
    draft_parser.add_argument("--role-lens", action="append", default=[])
    draft_parser.add_argument("--review-lane", action="append", default=[])
    draft_parser.add_argument("--output", type=Path)
    draft_parser.add_argument("--force", action="store_true")
    draft_parser.add_argument("--json", action="store_true")

    critique_parser = subparsers.add_parser("critique-plan")
    critique_parser.add_argument("work_order", type=Path)
    critique_parser.add_argument("--reviewer", action="append", default=[])
    critique_parser.add_argument("--output-dir", type=Path, default=DEFAULT_CRITIQUE_DIR)
    critique_parser.add_argument("--json", action="store_true")

    builder_parser = subparsers.add_parser("builder-experiment")
    builder_parser.add_argument("work_order", type=Path)
    builder_parser.add_argument("--repo", required=True)
    builder_parser.add_argument("--builder", action="append", default=[])
    builder_parser.add_argument("--task-class", default="general")
    builder_parser.add_argument("--base-ref", default="origin/main")
    builder_parser.add_argument("--context-pack", action="append", default=[])
    builder_parser.add_argument("--prompt-lens", action="append", default=[])
    builder_parser.add_argument("--output", type=Path, required=True)
    builder_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    try:
        if args.command == "draft":
            source: Mapping[str, Any] | None = None
            if args.issue_plan:
                issue_plan = _read_issue_plan_for_work_order(args.issue_plan)
                title = args.title or issue_plan.title
                source_text = issue_plan.source_text
                repo = args.repo or issue_plan.repo
                source = issue_plan.source
            else:
                source_text = _read_optional_body(args.body_file, args.body)
                title = args.title
                repo = args.repo
            payload = draft_work_order(
                title=title,
                source_text=source_text,
                repo=repo,
                context_manifest=args.context_manifest,
                role_lenses=args.role_lens,
                review_lanes=args.review_lane,
                source=source,
                output=args.output,
                force=args.force,
            )
            text = (
                f"Code Mower work order\nOutput: {payload.get('output_path')}\n"
                f"Manifest: {payload.get('manifest_path')}\n"
            )
            return _print_payload(payload, as_json=args.json, text=text)
        if args.command == "critique-plan":
            payload = create_critique_plan(
                args.work_order,
                reviewers=args.reviewer,
                output_dir=args.output_dir,
            )
            text = (
                f"Code Mower work-order critique plan\n"
                f"Manifest: {payload.get('manifest_path')}\n"
                f"Prompts: {len(payload.get('prompts', []) or [])}\n"
            )
            return _print_payload(payload, as_json=args.json, text=text)
        if args.command == "builder-experiment":
            payload = seed_builder_experiment(
                args.work_order,
                repo=args.repo,
                builders=args.builder,
                output=args.output,
                task_class=args.task_class,
                base_ref=args.base_ref,
                context_packs=args.context_pack,
                prompt_lenses=args.prompt_lens,
            )
            text = (
                f"Code Mower builder experiment seed\n"
                f"Output: {payload.get('output_path')}\n"
                f"Builders: {payload.get('builder_count')}\n"
            )
            return _print_payload(payload, as_json=args.json, text=text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled work-order command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(project_context_main())
