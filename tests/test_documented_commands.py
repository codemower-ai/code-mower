"""Documentation contract: advertised top-level commands must exist.

Current guidance is only useful if a cold operator can run what it names.
These checks are fully offline: command handlers are replaced with recording
stubs, so nothing launches a provider, touches the network, or writes state.

Historical records are excluded on purpose. The changelog and per-version
release notes describe commands as they were at that release; correcting them
would falsify the record.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import cli


ROOT = Path(__file__).resolve().parents[1]

# `code-mower <command>` where the command is command-shaped, is not a slash
# command (`/code-mower start`), and is not part of a longer token such as
# `code-mower code-mower==1.4.0`.
INVOCATION_RE = re.compile(r"(?<![\w/-])code-mower[ \t]+([a-z][a-z0-9-]*)(?![\w.=-])")
FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

HISTORICAL_RE = re.compile(r"(^|/)(CHANGELOG\.md|release-history\.md|v[\d]+-release-notes\.md)$")


def _current_guidance_files() -> list[Path]:
    paths = [ROOT / "README.md"]
    paths.extend(
        path
        for path in sorted((ROOT / "docs").rglob("*.md"))
        if not HISTORICAL_RE.search(path.as_posix())
    )
    return [path for path in paths if path.is_file()]


def _code_segments(text: str) -> list[str]:
    """Return fenced-block lines and inline code spans, ignoring prose."""
    segments: list[str] = []
    fence = ""
    for line in text.splitlines():
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if fence:
            segments.append(line)
        else:
            segments.extend(INLINE_CODE_RE.findall(line))
    return segments


def _advertised_commands() -> dict[str, set[str]]:
    """Map each advertised command to the files advertising it."""
    advertised: dict[str, set[str]] = {}
    for path in _current_guidance_files():
        relative = path.relative_to(ROOT).as_posix()
        for segment in _code_segments(path.read_text(encoding="utf-8")):
            for command in INVOCATION_RE.findall(segment):
                advertised.setdefault(command, set()).add(relative)
    return advertised


class DocumentedCommandTests(unittest.TestCase):
    def test_extractor_finds_the_commands_readme_actually_shows(self) -> None:
        # Guards the extractor itself: a silently empty scan would make every
        # other check in this file vacuous.
        advertised = set(_advertised_commands())
        expected = {"init", "doctor", "session", "lanes", "board", "productivity"}
        self.assertTrue(expected.issubset(advertised), msg=sorted(advertised))

    def test_every_advertised_command_parses_under_the_packaged_cli(self) -> None:
        advertised = _advertised_commands()
        self.assertTrue(advertised)
        calls: list[list[str]] = []

        def handler(argv: list[str]) -> int:
            calls.append(argv)
            return 0

        for command, sources in sorted(advertised.items()):
            with self.subTest(command=command):
                calls.clear()
                self.assertIn(
                    command,
                    cli.COMMAND_HANDLERS,
                    msg=f"{command} is documented in {sorted(sources)} but is not a command",
                )
                with mock.patch.dict(
                    cli.COMMAND_HANDLERS, {command: handler}, clear=False
                ):
                    self.assertEqual(cli.main([command, "--offline-contract-probe"]), 0)
                self.assertEqual(calls, [["--offline-contract-probe"]])

    def test_every_advertised_command_has_a_help_description(self) -> None:
        for command in sorted(_advertised_commands()):
            with self.subTest(command=command):
                self.assertTrue(cli.COMMAND_DESCRIPTIONS.get(command))

    def test_fabricated_command_is_rejected(self) -> None:
        # `code-mower devin work-order` was documented for hosted work orders
        # and never existed; hosted dispatch is the DevinWorkOrders library
        # seam. A fabricated command must fail to parse rather than dispatch.
        for argv in (["devin", "work-order"], ["work-orders"], ["devin-work-order"]):
            with self.subTest(argv=argv):
                self.assertNotIn(argv[0], cli.COMMAND_HANDLERS)
                with self.assertRaises(SystemExit) as raised:
                    cli.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_no_document_advertises_a_hosted_work_order_command(self) -> None:
        pattern = re.compile(r"code-mower[ \t]+devin[ \t]+work-order")
        for path in _current_guidance_files():
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
