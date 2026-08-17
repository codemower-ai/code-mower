"""Local plan-of-record context for audit prompts."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_PROJECT_CONTEXT_MANIFEST = Path(
    ".code-mower/project-context/project-context-manifest.json"
)
DEFAULT_EXTERNAL_CONTEXT_MANIFEST = Path(
    ".code-mower/context/external/external-context-manifest.json"
)
DEFAULT_MAX_TOTAL_BYTES = 80_000
DEFAULT_MAX_FILE_BYTES = 20_000


PLAN_CONFORMANCE_INSTRUCTIONS = """# Plan-Conformance Lens

Apply the normal correctness review and also ask:

- Does this PR contradict the plan of record, project architecture, data
  ownership boundaries, supported transports, privacy rules, or documented
  quality bar?
- If it contradicts trusted project context, report a P2 finding named
  "Contradicts plan of record" with the specific contract it violates.
- If no trusted project context is provided, do not invent one and do not block
  solely because context is absent.
"""


@dataclass(frozen=True)
class RenderedPlanContext:
    text: str
    included_documents: int
    included_bytes: int
    warnings: tuple[str, ...] = ()


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read plan context manifest {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"plan context manifest must be a JSON object: {path}")
    return payload


def _resolve_path(repo_root: Path, value: Any) -> Path:
    root = repo_root.expanduser().resolve()
    raw_text = str(value or "").strip()
    raw = Path(raw_text).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"plan context path escapes repo root: {raw_text}") from exc
    return resolved


def _resolve_manifest_path(repo_root: Path, value: Any) -> Path:
    raw = Path(str(value or "")).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (repo_root / raw).resolve()


def _repo_relative_path(repo_root: Path, value: Any) -> Path:
    return _resolve_path(repo_root, value).relative_to(repo_root.expanduser().resolve())


def _git_show_bytes(repo_root: Path, trusted_git_ref: str, path: Path) -> bytes | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "show",
            f"{trusted_git_ref}:{path.as_posix()}",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _load_git_json(repo_root: Path, trusted_git_ref: str, path: Path) -> Mapping[str, Any] | None:
    data = _git_show_bytes(repo_root, trusted_git_ref, path)
    if data is None:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"unable to read plan context manifest {trusted_git_ref}:{path.as_posix()}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ValueError(
            "plan context manifest must be a JSON object: "
            f"{trusted_git_ref}:{path.as_posix()}"
        )
    return payload


def _read_bounded_text(path: Path, max_bytes: int) -> tuple[str, int, bool]:
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), len(data), truncated


def _read_git_bounded_text(
    repo_root: Path,
    trusted_git_ref: str,
    path: Path,
    max_bytes: int,
) -> tuple[str, int, bool] | None:
    data = _git_show_bytes(repo_root, trusted_git_ref, path)
    if data is None:
        return None
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), len(data), truncated


def _remaining_budget(max_total_bytes: int, used: int, max_file_bytes: int) -> int:
    return max(0, min(max_file_bytes, max_total_bytes - used))


def _section(
    *,
    title: str,
    path: Path,
    repo_root: Path,
    max_bytes: int,
) -> tuple[str, int, str]:
    display_path = str(path)
    try:
        display_path = str(path.relative_to(repo_root))
    except ValueError:
        pass
    text, byte_count, truncated = _read_bounded_text(path, max_bytes)
    truncation = "\n[truncated]\n" if truncated else ""
    return (
        "\n".join(
            [
                f"## {title}",
                f"Path: {display_path}",
                "",
                "```",
                text.rstrip(),
                "```",
                truncation.rstrip(),
            ]
        ).rstrip(),
        byte_count,
        display_path,
    )


def _section_from_text(
    *,
    title: str,
    display_path: str,
    text: str,
    truncated: bool,
) -> str:
    truncation = "\n[truncated]\n" if truncated else ""
    return (
        "\n".join(
            [
                f"## {title}",
                f"Path: {display_path}",
                "",
                "```",
                text.rstrip(),
                "```",
                truncation.rstrip(),
            ]
        ).rstrip()
    )


def _project_context_sections(
    repo_root: Path,
    manifest_path: Path,
    *,
    max_total_bytes: int,
    max_file_bytes: int,
    used_bytes: int,
    trusted_git_ref: str | None = None,
    manifest_repo_path: Path | None = None,
) -> tuple[list[str], int, list[str]]:
    if trusted_git_ref and manifest_repo_path is not None:
        try:
            manifest = _load_git_json(repo_root, trusted_git_ref, manifest_repo_path)
        except ValueError as exc:
            return [], used_bytes, [str(exc)]
        if manifest is None:
            return [], used_bytes, []
    else:
        if not manifest_path.is_file():
            return [], used_bytes, []
        try:
            manifest = _load_json(manifest_path)
        except ValueError as exc:
            return [], used_bytes, [str(exc)]
    sections: list[str] = []
    warnings: list[str] = []
    for document in manifest.get("documents", []) or []:
        if not isinstance(document, Mapping):
            continue
        budget = _remaining_budget(max_total_bytes, used_bytes, max_file_bytes)
        if budget <= 0:
            warnings.append("plan context total byte budget exhausted")
            break
        try:
            relative_path = _repo_relative_path(repo_root, document.get("path"))
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        title = str(document.get("title") or relative_path.name)
        display_path = relative_path.as_posix()
        if trusted_git_ref:
            git_text = _read_git_bounded_text(
                repo_root,
                trusted_git_ref,
                relative_path,
                budget,
            )
            if git_text is None:
                warnings.append(f"missing project context document: {trusted_git_ref}:{display_path}")
                continue
            text, byte_count, truncated = git_text
            rendered = _section_from_text(
                title=title,
                display_path=display_path,
                text=text,
                truncated=truncated,
            )
        else:
            path = repo_root / relative_path
            if not path.is_file():
                warnings.append(f"missing project context document: {path}")
                continue
            try:
                rendered, byte_count, _display = _section(
                    title=title,
                    path=path,
                    repo_root=repo_root,
                    max_bytes=budget,
                )
            except OSError as exc:
                warnings.append(f"unable to read project context document {path}: {exc}")
                continue
        sections.append(rendered)
        used_bytes += byte_count
    return sections, used_bytes, warnings


def _external_context_sections(
    repo_root: Path,
    manifest_path: Path,
    *,
    max_total_bytes: int,
    max_file_bytes: int,
    used_bytes: int,
    trusted_git_ref: str | None = None,
    manifest_repo_path: Path | None = None,
) -> tuple[list[str], int, list[str]]:
    if trusted_git_ref and manifest_repo_path is not None:
        try:
            manifest = _load_git_json(repo_root, trusted_git_ref, manifest_repo_path)
        except ValueError as exc:
            return [], used_bytes, [str(exc)]
        if manifest is None:
            return [], used_bytes, []
    else:
        if not manifest_path.is_file():
            return [], used_bytes, []
        try:
            manifest = _load_json(manifest_path)
        except ValueError as exc:
            return [], used_bytes, [str(exc)]
    sections: list[str] = []
    warnings: list[str] = []
    for entry in manifest.get("entries", []) or []:
        if not isinstance(entry, Mapping):
            continue
        filename = str(entry.get("filename") or "external context")
        if not entry.get("text_preview_included"):
            sections.append(
                "\n".join(
                    [
                        f"## External Context: {filename}",
                        "Preview: not included; registered as metadata only.",
                        f"Bytes: {entry.get('bytes', '')}",
                        f"SHA-256: {entry.get('sha256', '')}",
                    ]
                ).rstrip()
            )
            continue
        budget = _remaining_budget(max_total_bytes, used_bytes, max_file_bytes)
        if budget <= 0:
            warnings.append("plan context total byte budget exhausted")
            break
        try:
            relative_path = _repo_relative_path(repo_root, entry.get("text_preview_path"))
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        display_path = relative_path.as_posix()
        if trusted_git_ref:
            git_text = _read_git_bounded_text(
                repo_root,
                trusted_git_ref,
                relative_path,
                budget,
            )
            if git_text is None:
                warnings.append(
                    f"missing external context preview: {trusted_git_ref}:{display_path}"
                )
                continue
            text, byte_count, truncated = git_text
            rendered = _section_from_text(
                title=f"External Context: {filename}",
                display_path=display_path,
                text=text,
                truncated=truncated,
            )
        else:
            preview_path = repo_root / relative_path
            if not preview_path.is_file():
                warnings.append(f"missing external context preview: {preview_path}")
                continue
            try:
                rendered, byte_count, _display = _section(
                    title=f"External Context: {filename}",
                    path=preview_path,
                    repo_root=repo_root,
                    max_bytes=budget,
                )
            except OSError as exc:
                warnings.append(
                    f"unable to read external context preview {preview_path}: {exc}"
                )
                continue
        sections.append(rendered)
        used_bytes += byte_count
    return sections, used_bytes, warnings


def _manifest_candidates(explicit: Path | None, default_path: Path) -> Iterable[Path]:
    if explicit is not None:
        return (explicit,)
    return (default_path,)


def render_plan_context(
    *,
    repo_root: Path,
    project_context_manifest: Path | None = None,
    external_context_manifest: Path | None = None,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    trusted_git_ref: str | None = None,
) -> RenderedPlanContext:
    """Render trusted local planning context for a reviewer prompt."""

    if max_total_bytes <= 0:
        raise ValueError("max_total_bytes must be greater than zero")
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be greater than zero")

    repo_root = repo_root.expanduser().resolve()
    sections: list[str] = []
    warnings: list[str] = []
    used_bytes = 0

    for manifest_path in _manifest_candidates(
        project_context_manifest,
        DEFAULT_PROJECT_CONTEXT_MANIFEST,
    ):
        use_git_ref = trusted_git_ref if project_context_manifest is None else None
        project_sections, used_bytes, project_warnings = _project_context_sections(
            repo_root,
            _resolve_manifest_path(repo_root, manifest_path),
            max_total_bytes=max_total_bytes,
            max_file_bytes=max_file_bytes,
            used_bytes=used_bytes,
            trusted_git_ref=use_git_ref,
            manifest_repo_path=Path(manifest_path) if use_git_ref else None,
        )
        sections.extend(project_sections)
        warnings.extend(project_warnings)

    for manifest_path in _manifest_candidates(
        external_context_manifest,
        DEFAULT_EXTERNAL_CONTEXT_MANIFEST,
    ):
        use_git_ref = trusted_git_ref if external_context_manifest is None else None
        external_sections, used_bytes, external_warnings = _external_context_sections(
            repo_root,
            _resolve_manifest_path(repo_root, manifest_path),
            max_total_bytes=max_total_bytes,
            max_file_bytes=max_file_bytes,
            used_bytes=used_bytes,
            trusted_git_ref=use_git_ref,
            manifest_repo_path=Path(manifest_path) if use_git_ref else None,
        )
        sections.extend(external_sections)
        warnings.extend(external_warnings)

    lines = [
        PLAN_CONFORMANCE_INSTRUCTIONS.rstrip(),
        "",
        "# Trusted Local Project Context",
    ]
    if sections:
        lines.extend(sections)
    else:
        lines.append("(no project-context or external-context previews registered)")
    if warnings:
        lines.extend(["", "# Plan Context Warnings"])
        lines.extend(f"- {warning}" for warning in warnings)
    return RenderedPlanContext(
        text="\n\n".join(lines).rstrip() + "\n",
        included_documents=len(sections),
        included_bytes=used_bytes,
        warnings=tuple(warnings),
    )
