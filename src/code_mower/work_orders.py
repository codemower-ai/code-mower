#!/usr/bin/env python3
"""Project context and work-order helpers for Code Mower authoring loops."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_CONTEXT_SCHEMA = "code_mower.projectContext.v1"
EXTERNAL_CONTEXT_SCHEMA = "code_mower.externalContextManifest.v1"
ISSUE_PLAN_SCHEMA = "code_mower.issuePlan.v1"
WORK_ORDER_SCHEMA = "code_mower.workOrder.v1"
CRITIQUE_PLAN_SCHEMA = "code_mower.workOrderCritiquePlan.v1"
WORK_ORDER_BUILDER_SPEC_SCHEMA = "code_mower.workOrderBuilderExperimentSeed.v1"

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
        preview = _read_text(resolved)[:max_preview_chars]
        preview_name = f"{_safe_slug(resolved.stem, 'external')}-{entry['sha256'][:10]}.preview.txt"
        preview_path = output_dir / "previews" / preview_name
        _write_text(preview_path, preview.rstrip() + "\n", force=True)
        entry.update(
            {
                "text_preview_included": True,
                "text_preview_path": str(preview_path),
                "text_preview_chars": len(preview),
                "text_preview_truncated": resolved.stat().st_size > len(preview.encode("utf-8")),
            }
        )
    return entry


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
    entries = [
        _external_context_entry(
            path,
            output_dir=output_dir,
            include_preview=include_preview,
            max_preview_chars=max_preview_chars,
        )
        for path in paths
    ]
    manifest = {
        "mode": "external-context-add",
        "schema": EXTERNAL_CONTEXT_SCHEMA,
        "created_at": _utc_now(),
        "output_dir": str(output_dir),
        "entry_count": len(entries),
        "entries": entries,
        "cloud_policy": (
            "This manifest records local external context metadata. Do not upload "
            "raw external docs or previews unless a user explicitly opts in."
        ),
    }
    manifest_path = output_dir / "external-context-manifest.json"
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
        "body": prompt,
        "sections": {
            "problem": prompt or "TODO: paste the issue problem statement.",
            "context": "TODO: list project-context and external-context files to consult.",
            "non_goals": "TODO: call out what should not change.",
            "acceptance": "TODO: define observable acceptance criteria.",
            "review": "Use the normal Code Mower audit protocol before merge.",
        },
    }


def render_issue_plan_markdown(plan: Mapping[str, Any]) -> str:
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
    return "\n".join(lines)


def write_issue_plan(plan: Mapping[str, Any], output: Path, *, force: bool = False) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    _require_writable(output, force=force)
    if output.suffix.lower() == ".json":
        _write_json(output, plan, force=True)
    else:
        _write_text(output, render_issue_plan_markdown(plan), force=True)
    return {**dict(plan), "output_path": str(output)}


def _extract_heading_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    return fallback


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


def render_work_order(
    *,
    title: str,
    source_text: str,
    repo: str = "",
    context_manifest: Path | None = None,
    role_lenses: Sequence[str] = (),
    review_lanes: Sequence[str] = (),
) -> str:
    role_lenses = tuple(role_lenses) or (
        "product-manager",
        "architect",
        "implementer",
        "qa-context-driven-tester",
        "security-threat-model",
        "operability",
        "devils-advocate",
    )
    review_lanes = tuple(review_lanes) or ("codex-audit", "claude-audit", "gitar")
    lines = [
        f"# Work Order: {title}",
        "",
        f"Schema: `{WORK_ORDER_SCHEMA}`",
        f"Repository: `{repo or 'TODO'}`",
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
    output: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    title = title.strip() or _extract_heading_title(source_text, "Untitled work order")
    output = output or (DEFAULT_WORK_ORDER_DIR / f"{_safe_slug(title)}.md")
    if output.suffix.lower() == ".json":
        raise ValueError("work-order draft --output must be a Markdown path, not .json")
    manifest_path = output.with_suffix(".json")
    _require_writable(output, force=force)
    _require_writable(manifest_path, force=force)
    markdown = render_work_order(
        title=title,
        source_text=source_text,
        repo=repo,
        context_manifest=context_manifest,
        role_lenses=role_lenses,
        review_lanes=review_lanes,
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
        "role_lenses": list(role_lenses),
        "review_lanes": list(review_lanes),
    }
    _write_json(manifest_path, manifest, force=True)
    manifest["manifest_path"] = str(manifest_path)
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
                "review_classes": list(prompt_lenses),
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
            if args.issue_plan:
                source_text = _read_text(args.issue_plan)
                title = args.title or _extract_heading_title(source_text, args.issue_plan.stem)
            else:
                source_text = _read_optional_body(args.body_file, args.body)
                title = args.title
            payload = draft_work_order(
                title=title,
                source_text=source_text,
                repo=args.repo,
                context_manifest=args.context_manifest,
                role_lenses=args.role_lens,
                review_lanes=args.review_lane,
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
