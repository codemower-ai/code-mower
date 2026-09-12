"""Repository-local documentation integrity checks."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")
EXPLICIT_ANCHOR_RE = re.compile(
    r"""<(?:a|span)\s+(?:[^>]*?\s)?(?:id|name)=["']([^"']+)["']""",
    re.I,
)


def _markdown_files() -> list[Path]:
    ignored = {".git", ".venv", "build", "dist"}
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not ignored.intersection(path.relative_to(ROOT).parts)
    )


def _github_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\- ]", "", text.lower())
    return re.sub(r"\s+", "-", text.strip())


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("\u0060" * 3, "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence = marker
            elif marker == fence:
                in_fence = False
            continue
        if in_fence:
            continue
        anchors.update(EXPLICIT_ANCHOR_RE.findall(line))
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = _github_slug(match.group(2).rstrip("#").rstrip())
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _link_destination(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    return raw.split(maxsplit=1)[0]


def _text_outside_fences(path: Path) -> str:
    lines: list[str] = []
    in_fence = False
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("\u0060" * 3, "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence = marker
            elif marker == fence:
                in_fence = False
            continue
        if not in_fence:
            lines.append(line)
    return "\n".join(lines)


class DocumentationTests(unittest.TestCase):
    def test_fenced_link_examples_are_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "example.md"
            marker = "\u0060" * 3
            path.write_text(
                f"[real](target.md)\n\n{marker}md\n[example](missing.md)\n{marker}\n",
                encoding="utf-8",
            )

            destinations = [
                _link_destination(match.group(1))
                for match in LINK_RE.finditer(_text_outside_fences(path))
            ]

        self.assertEqual(destinations, ["target.md"])

    def test_relative_markdown_links_and_anchors_resolve(self) -> None:
        anchors_by_path: dict[Path, set[str]] = {}
        failures: list[str] = []
        for source in _markdown_files():
            text = _text_outside_fences(source)
            for match in LINK_RE.finditer(text):
                destination = _link_destination(match.group(1))
                if (
                    not destination
                    or destination.startswith(("http://", "https://", "mailto:", "data:"))
                ):
                    continue
                path_text, separator, fragment = destination.partition("#")
                path_text = unquote(path_text.split("?", 1)[0])
                if path_text.startswith("/"):
                    continue
                target = source if not path_text else (source.parent / path_text).resolve()
                try:
                    target.relative_to(ROOT)
                except ValueError:
                    failures.append(
                        f"{source.relative_to(ROOT)}: link escapes repository: {destination}"
                    )
                    continue
                if not target.exists():
                    failures.append(
                        f"{source.relative_to(ROOT)}: missing target: {destination}"
                    )
                    continue
                if separator and fragment and target.is_file() and target.suffix.lower() == ".md":
                    fragment = unquote(fragment).lower()
                    anchors = anchors_by_path.setdefault(target, _anchors(target))
                    if fragment not in anchors:
                        failures.append(
                            f"{source.relative_to(ROOT)}: missing anchor: {destination}"
                        )
        self.assertEqual(failures, [], "\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
