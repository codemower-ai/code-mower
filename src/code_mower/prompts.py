#!/usr/bin/env python3
"""Load versioned Code Mower review prompt lenses."""

from __future__ import annotations

import os
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_REVIEW_LENSES = ("base-audit",)
SAFE_LENS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def split_lenses(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Return normalized prompt lens ids from comma text or an iterable."""

    if value is None:
        return DEFAULT_REVIEW_LENSES
    if isinstance(value, str):
        lenses = tuple(item.strip() for item in value.split(",") if item.strip())
    else:
        lenses = tuple(str(item).strip() for item in value if str(item).strip())
    return lenses or DEFAULT_REVIEW_LENSES


def default_prompt_dirs() -> tuple[Path, ...]:
    module_dir = Path(__file__).resolve().parent
    return (
        module_dir / "lane_prompts",
        module_dir / "templates" / "lane_prompts",
        module_dir.parent / "tools" / "lane_prompts",
        Path.cwd() / "tools" / "lane_prompts",
    )


def packaged_prompt_dir() -> Path | None:
    module_dir = Path(__file__).resolve().parent
    if module_dir.name == "tools":
        return None
    candidate = module_dir / "templates" / "lane_prompts"
    return candidate if candidate.is_dir() else None


def resolve_prompt_dir(prompt_dir: str | Path | None = None) -> Path:
    if prompt_dir is not None and str(prompt_dir).strip():
        return Path(prompt_dir).expanduser()

    env_dir = os.environ.get("CODE_MOWER_PROMPT_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    for candidate in default_prompt_dirs():
        if candidate.is_dir():
            return candidate
    return default_prompt_dirs()[0]


def _read_lens(prompt_dir: Path, lens: str) -> str:
    if not SAFE_LENS_RE.fullmatch(lens):
        raise ValueError(
            "review lens ids must match [A-Za-z0-9_.-]+: "
            f"{lens!r}"
        )
    path = prompt_dir / f"{lens}.md"
    if not path.is_file():
        raise FileNotFoundError(f"review lens not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _lens_title(text: str, lens_id: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or lens_id
    return lens_id


def list_prompt_lenses(prompt_dir: str | Path | None = None) -> list[dict[str, Any]]:
    root = resolve_prompt_dir(prompt_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"prompt directory not found: {root}")
    lenses: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.md")):
        lens_id = path.stem
        if not SAFE_LENS_RE.fullmatch(lens_id):
            continue
        text = path.read_text(encoding="utf-8")
        lenses.append(
            {
                "id": lens_id,
                "title": _lens_title(text, lens_id),
                "path": str(path),
                "bytes": path.stat().st_size,
            }
        )
    return lenses


def _read_git_lens(repo_root: Path, trusted_git_ref: str, lens: str) -> str | None:
    if not SAFE_LENS_RE.fullmatch(lens):
        raise ValueError(
            "review lens ids must match [A-Za-z0-9_.-]+: "
            f"{lens!r}"
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "show",
            f"{trusted_git_ref}:tools/lane_prompts/{lens}.md",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def load_review_prompt(
    lenses: str | Iterable[str] | None = None,
    *,
    prompt_dir: str | Path | None = None,
    trusted_git_ref: str | None = None,
    repo_root: str | Path | None = None,
    missing_ok: bool = False,
) -> str:
    """Load and concatenate one or more trusted review prompt lenses."""

    explicit_prompt_dir = prompt_dir is not None or bool(os.environ.get("CODE_MOWER_PROMPT_DIR"))
    sections = []
    for lens in split_lenses(lenses):
        text: str | None = None
        if not explicit_prompt_dir and trusted_git_ref and repo_root is not None:
            text = _read_git_lens(Path(repo_root), trusted_git_ref, lens)
            if text is None:
                package_dir = packaged_prompt_dir()
                if package_dir is not None:
                    try:
                        text = _read_lens(package_dir, lens)
                    except FileNotFoundError:
                        text = None
            if text is None and not missing_ok:
                raise FileNotFoundError(
                    "review lens not found in trusted git ref: "
                    f"{trusted_git_ref}:tools/lane_prompts/{lens}.md"
                )
        else:
            root = resolve_prompt_dir(prompt_dir)
            try:
                text = _read_lens(root, lens)
            except FileNotFoundError:
                if not missing_ok:
                    raise
                text = None
        if text is not None:
            sections.append(f"## Review Lens: {lens}\n\n{text}")
    if not sections and missing_ok:
        return ""
    return "\n\n".join(sections).strip() + "\n"


def append_review_prompt(system_prompt: str, review_prompt: str) -> str:
    """Append trusted review doctrine without changing a prompt's opening line."""

    if not review_prompt.strip():
        return system_prompt
    return (
        system_prompt.rstrip()
        + "\n\n# Code Mower Review Doctrine\n\n"
        + review_prompt.strip()
        + "\n"
    )


def render_prompt_lenses_text(lenses: Iterable[Mapping[str, Any]]) -> str:
    lines = ["Code Mower prompt lenses", ""]
    count = 0
    for lens in lenses:
        count += 1
        lines.append(f"- {lens.get('id')}: {lens.get('title')}")
    if count == 0:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--prompt-dir", default=None)
    list_parser.add_argument("--json", action="store_true")

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("lenses", nargs="?", default=",".join(DEFAULT_REVIEW_LENSES))
    show_parser.add_argument("--prompt-dir", default=None)
    show_parser.add_argument("--json", action="store_true")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--lenses", default=",".join(DEFAULT_REVIEW_LENSES))
    validate_parser.add_argument("--prompt-dir", default=None)
    validate_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            lenses = list_prompt_lenses(args.prompt_dir)
            payload = {
                "mode": "code-mower-prompt-lenses",
                "prompt_dir": str(resolve_prompt_dir(args.prompt_dir)),
                "lenses": lenses,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(render_prompt_lenses_text(lenses), end="")
            return 0

        if args.command == "show":
            selected = split_lenses(args.lenses)
            prompt = load_review_prompt(selected, prompt_dir=args.prompt_dir)
            payload = {
                "mode": "code-mower-review-prompt",
                "prompt_dir": str(resolve_prompt_dir(args.prompt_dir)),
                "lenses": list(selected),
                "prompt": prompt,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(prompt, end="")
            return 0

        if args.command == "validate":
            selected = split_lenses(args.lenses)
            prompt = load_review_prompt(selected, prompt_dir=args.prompt_dir)
            payload = {
                "mode": "code-mower-prompt-validation",
                "ok": True,
                "prompt_dir": str(resolve_prompt_dir(args.prompt_dir)),
                "lenses": list(selected),
                "bytes": len(prompt.encode("utf-8")),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("ok")
            return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled prompt command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
