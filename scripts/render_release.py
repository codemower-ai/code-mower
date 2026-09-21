#!/usr/bin/env python3
"""Render current release facts from release.yml into maintained documentation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower.release_metadata import ReleaseMetadataError, load_release_metadata  # noqa: E402


START = "<!-- code-mower:release-facts:start -->"
END = "<!-- code-mower:release-facts:end -->"


def _replace_region(text: str, body: str, path: Path) -> str:
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) >= text.index(END):
        raise ValueError(f"{path} must contain exactly one ordered release-facts region")
    prefix, rest = text.split(START, 1)
    _, suffix = rest.split(END, 1)
    return f"{prefix}{START}\n{body.rstrip()}\n{END}{suffix}"


def _readme_body(metadata) -> str:
    notes = metadata.documents["notes"]
    qualification = metadata.documents["qualification"]
    return (
        f"This source defines Code Mower `{metadata.tag}`, with package spec\n"
        f"`{metadata.package_spec}`. Confirm the release tag on GitHub Releases and the package\n"
        "version on the selected index before using an index install command; source version\n"
        "and publication state are separate facts.\n"
        f"Python {metadata.python_minimum} or newer is required.\n"
        f"See the [release notes](https://github.com/codemower-ai/code-mower/blob/main/{notes})\n"
        f"and [qualification contract](https://github.com/codemower-ai/code-mower/blob/main/{qualification})."
    )


def _install_body(metadata) -> str:
    qualification = Path(metadata.documents["qualification"]).name
    runbook = Path(metadata.documents["runbook"]).name
    return (
        f"{metadata.tag} uses the exact install pin `{metadata.package_spec}` and requires Python\n"
        f"{metadata.python_minimum} or newer. Confirm that version is published on the selected index, then\n"
        "verify the command path and version after installing. The\n"
        f"[qualification contract]({qualification}) defines the required evidence. Use the\n"
        f"[candidate runbook]({runbook}) for prepublication local-wheel rehearsals."
    )


def _publication_body(metadata) -> str:
    runbook = Path(metadata.documents["runbook"]).name
    qualification = Path(metadata.documents["qualification"]).name
    return (
        "Code Mower users install from PyPI. For the current release, build the immutable\n"
        "merge-SHA candidate first, qualify those retained bytes, then tag and publish the\n"
        "unchanged SHA. The release workflow retrieves and verifies the candidate without\n"
        f"rebuilding. Follow the [{metadata.tag} runbook]({runbook}) and\n"
        f"[qualification contract]({qualification}); observed evidence belongs on the release\n"
        "issue and GitHub Release.\n\n"
        "```bash\n"
        f'CODE_MOWER_PYTHON="$(command -v python{metadata.python_minimum})"\n'
        f'pipx install --python "$CODE_MOWER_PYTHON" {metadata.package_spec}\n'
        "```"
    )


def render(repo_root: Path, *, check: bool) -> list[str]:
    metadata = load_release_metadata(repo_root)
    regions = {
        Path("README.md"): _readme_body(metadata),
        Path(metadata.documents["installation"]): _install_body(metadata),
        Path(metadata.documents["publication"]): _publication_body(metadata),
    }
    changed: list[str] = []
    for relative, body in regions.items():
        path = repo_root / relative
        current = path.read_text(encoding="utf-8")
        rendered = _replace_region(current, body, path)
        if current != rendered:
            changed.append(relative.as_posix())
            if not check:
                path.write_text(rendered, encoding="utf-8")
    if check and changed:
        raise ValueError("release facts are stale in: " + ", ".join(changed))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = render(args.repo.expanduser().resolve(), check=args.check)
    except (OSError, ValueError, ReleaseMetadataError) as exc:
        parser.exit(1, f"Release rendering refused: {exc}\n")
    print("release facts are current" if not changed else "updated: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
