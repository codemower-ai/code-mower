"""Validate immutable release identity and public text, without installing dependencies.

Run directly before tagging or publishing:
    python src/code_mower/release_identity.py --tag v1.5.0
The same check is used by ordinary release-readiness CI.
"""

from __future__ import annotations

import argparse
import ast
import re
import tomllib
from pathlib import Path


_NUMBER = r"(?:0|[1-9][0-9]*)"
_TAG = re.compile(
    rf"v(?P<base>{_NUMBER}\.{_NUMBER}\.{_NUMBER})"
    rf"(?:-(?P<stage>alpha|beta|rc)\.(?P<number>{_NUMBER}))?"
)
_STAGES = {"alpha": "a", "beta": "b", "rc": "rc"}
_HEADING = re.compile(r"^##[ \t]+([^\n]+)", re.MULTILINE)
_TRANSIENT = re.compile(
    r"\b(?:source|release)[ -]candidate\b"
    r"|\b(?:publication[ -]pending|pending[ -]publication)\b"
    r"|\bnot\s+(?:yet\s+)?(?:published|released)\b"
    r"|\b(?:unpublished|unreleased)\s+(?:release|version|candidate)\b"
    r"|\b(?:publication|publishing|release|v[0-9]+\.[0-9]+\.[0-9]+)\b[^.!?]{0,160}"
    r"\b(?:pending|awaiting|incomplete|unpublished|unreleased|blocked|depends?\s+on|not\s+(?:yet\s+)?(?:complete|final))\b"
    r"|\b(?:publication|publishing|release)\b[^.!?]{0,160}"
    r"\b(?:after|once|until|requires?|needs?)\s+(?:issue\s+)?#\d+\b",
    re.IGNORECASE,
)


def version_for_tag(tag: str) -> tuple[str, bool]:
    """Invert the repository's canonical tags; never normalize malformed input."""
    match = _TAG.fullmatch(tag)
    if not match:
        raise ValueError("expected vX.Y.Z or vX.Y.Z-{alpha,beta,rc}.N (no leading zeros)")
    stage = match["stage"]
    version = match["base"]
    if stage:
        version += _STAGES[stage] + match["number"]
    return version, stage is None


def _public_text(text: str) -> str:
    # Keep link labels (including issue numbers), not URLs; Markdown emphasis
    # and wrapped lines must not hide a release-state assertion.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return " ".join(re.sub(r"[`*_]", "", text).split())


def check_release_identity(repo_path: Path, tag: str) -> list[str]:
    """Return actionable failures for the selected tag, using only its checkout.

    Final wording applies to the README introduction (before its first H2)
    and the selected CHANGELOG entry. Older entries, Unreleased, and README
    instruction sections are outside that scope. Prerelease *tags* retain
    candidate wording; using TestPyPI or GitHub's prerelease flag does not
    turn a final vX.Y.Z tag into a prerelease.
    """
    try:
        version, final = version_for_tag(tag)
    except ValueError as exc:
        return [f"release tag {tag!r}: {exc}"]
    problems: list[str] = []

    def read(relative: str) -> str:
        try:
            return (repo_path / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            problems.append(f"{relative}: cannot read release surface ({exc})")
            return ""

    try:
        project = tomllib.loads(read("pyproject.toml")).get("project", {})
    except tomllib.TOMLDecodeError:
        project = {}
    if not isinstance(project, dict):
        project = {}
    if project.get("name") != "code-mower" or project.get("version") != version:
        problems.append(f"pyproject.toml: expected project code-mower version {version} for {tag}")
    if project.get("readme") != "README.md":
        problems.append("pyproject.toml: public package readme must be README.md")

    try:
        tree = ast.parse(read("src/code_mower/__init__.py"))
        versions = [
            node.value.value if isinstance(node.value, ast.Constant) else None
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
        ]
    except SyntaxError:
        versions = []
    if versions != [version]:
        problems.append(f"src/code_mower/__init__.py: expected one literal __version__ = {version!r}")

    readme = read("README.md")
    intro = _HEADING.split(readme, maxsplit=1)[0]
    baseline_label = (
        "release" if final else "release-candidate" if "-rc." in tag else "beta"
    )
    expected = (
        f"The current package-index {baseline_label} baseline is {tag}, "
        f"with pinned package install spec code-mower=={version}."
    )
    public_intro = _public_text(intro)
    baselines = re.findall(r"The current package-index [^.]*? baseline is\b", public_intro)
    if len(baselines) != 1 or expected not in public_intro:
        problems.append(f"README.md: expected exactly one opening release statement: {expected}")

    changelog = read("CHANGELOG.md")
    headings = list(_HEADING.finditer(changelog))
    aliases = {version, tag}
    selected = [
        index for index, heading in enumerate(headings)
        if heading[1].split() and heading[1].split()[0] in aliases
    ]
    active_entry = ""
    if len(selected) != 1:
        problems.append(f"CHANGELOG.md: expected exactly one ## {version} (or ## {tag}) heading")
    else:
        index = selected[0]
        end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
        active_entry = changelog[headings[index].start():end]
        if final and re.search(r"\b(?:candidate|pending|unpublished|unreleased|incomplete|draft)\b",
                               _public_text(headings[index][1]), re.IGNORECASE):
            problems.append(f"CHANGELOG.md: {tag} heading contains transient release text")
        if any(re.match(r"v?[0-9]", heading[1]) for heading in headings[:index]):
            problems.append(f"CHANGELOG.md: {tag} must be the first versioned release heading")

    if final:
        for surface, text in (("README.md introduction", intro), ("CHANGELOG.md active release", active_entry)):
            # A heading or a previous change must not lend its word "release"
            # to an unrelated incomplete feature in the following bullet.
            blocks = re.split(r"\n\s*\n|(?=^(?:#{1,6}|[-*])[ \t])", text, flags=re.MULTILINE)
            for block in blocks:
                match = _TRANSIENT.search(_public_text(block))
                if match:
                    problems.append(f"{surface}: {tag} contains transient release text: {match[0]!r}")
                    break
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Exact selected release tag")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Release checkout (default: cwd)")
    args = parser.parse_args(argv)
    problems = check_release_identity(args.repo, args.tag)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print(f"PASS: {args.tag} package metadata, README, and CHANGELOG agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
