"""Scope and freshness checks for local-repository (graph) context evidence.

A repository-kind connection has no OAuth principal, so the packet bindings in
``context_contract`` cannot bound where its citations point. A local graph
provider indexes a checkout and emits file/line citations, and nothing in the
generic packet schema stops it from citing its own cache, a sibling worktree,
an absolute path outside the indexed root, or an innocently named symlink that
reaches any of those.

These helpers are provider-neutral. They are the offline half of the Graphify
evaluation in issue #876: they establish what a local graph adapter must prove
before it can deliver evidence, without installing a graph package, running an
indexer, or adding a dependency. Nothing here performs retrieval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from .context_contract import ContextError, _text


#: ``path``, ``path#L12`` or ``path#L12-L20``. Line numbers are 1-based.
_CITATION = re.compile(r"(?P<path>[^#]{1,1024})(?:#L(?P<start>\d{1,7})(?:-L(?P<end>\d{1,7}))?)?\Z")
MAX_GRAPH_CITATIONS = 200

#: Directories a local indexer writes to or reads across. Citing into one of
#: these means the graph escaped the immutable checkout it was asked to index.
#: Compared case-folded: on a case-insensitive filesystem (APFS and NTFS by
#: default) ``.GIT/config`` names the same directory as ``.git/config``.
_EXCLUDED_ROOTS = frozenset({".git", ".graph", ".graphify", ".code-mower"})


def _names_private_state(parts: Iterable[str]) -> bool:
    """True when any segment names indexer or version-control private state.

    Every segment is checked, not just the first: a vendored submodule's
    ``vendor/.git`` is as private as the top-level one, and a resolved symlink
    target can land inside a nested cache directory.
    """
    return any(part.casefold() in _EXCLUDED_ROOTS for part in parts)


@dataclass(frozen=True)
class GraphCitation:
    """A citation resolved against the indexed root, with an optional line span."""

    path: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class GraphEvidenceReport:
    """A bounded, shareable quality verdict for one local-repository packet.

    ``resolved``/``total`` count line-bearing citations only: a citation with no
    line span names a file but makes no line claim to resolve. Such a citation
    is still held to the scope policy, so a full ``resolution_rate`` means every
    line claim checked out, not that every cited file exists. ``revision_state``
    repeats the packet's own binding so a consumer can refuse stale evidence
    without decoding the private payload again.
    """

    revision_state: str
    total_citations: int
    line_citations: int
    resolved_line_citations: int
    completeness: str
    truncated: bool

    @property
    def resolution_rate(self) -> float:
        """1.0 when nothing makes a line claim; an unresolvable claim lowers it."""
        if not self.line_citations:
            return 1.0
        return self.resolved_line_citations / self.line_citations

    def meets_gate(self, *, minimum_resolution: float = 0.9) -> bool:
        """The adopt gate from #876: fresh evidence with resolvable citations.

        Stale or unknown revision binding fails regardless of resolution: the
        citations may resolve perfectly against a revision nobody asked about.
        """
        return self.revision_state == "matching" and self.resolution_rate >= minimum_resolution

    def shareable_summary(self) -> dict[str, Any]:
        return {
            "schema": "code_mower.contextGraphQuality.v1",
            "revision_state": self.revision_state,
            "citations": self.total_citations,
            "line_citations": self.line_citations,
            "resolved_line_citations": self.resolved_line_citations,
            "completeness": self.completeness,
            "truncated": self.truncated,
        }


def parse_graph_citation(source: Any) -> GraphCitation:
    """Parse a repository-relative citation. Anything escaping the root fails.

    Absolute paths, parent traversal, Windows separators, and the indexer's own
    cache directories are rejected here rather than at read time, so an adapter
    cannot launder an out-of-scope path through a structurally valid packet.

    This sees only the text a provider wrote. Where the path actually lands on
    disk is a separate question, answered by the scope check in
    ``evaluate_graph_evidence``.
    """
    match = _CITATION.fullmatch(_text(source, maximum=2048))
    if match is None:
        raise ContextError("local graph citation must be a path with an optional line span")
    raw = match.group("path")
    path = PurePosixPath(raw)
    parts = raw.split("/")
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in raw
        or _names_private_state(parts)
    ):
        raise ContextError("local graph citation must stay inside the indexed repository")
    start = match.group("start")
    end = match.group("end")
    if start is None:
        return GraphCitation(raw, None, None)
    start_line = int(start)
    end_line = int(end) if end is not None else start_line
    if not 1 <= start_line <= end_line:
        raise ContextError("local graph citation line span must be a positive ordered range")
    return GraphCitation(raw, start_line, end_line)


def _scope_checker(anchor: Path) -> Callable[[GraphCitation], bool]:
    """Hold a citation's real filesystem target to the scope policy.

    A textually clean path can still land outside the indexed checkout, or
    inside private state the policy excludes, because a symlink planted in the
    checkout redirects it: ``example_pkg/linked.py`` may point at another
    worktree, and ``metadata/config`` may reach ``.git/config`` under an
    innocent name. Both are decided here, on the resolved path, so the text
    check and the filesystem check cannot disagree.

    Applies to every citation, with or without a line span: a citation that
    makes no line claim is never scored, so this is the only check that bounds
    where it points.
    """

    def in_scope(citation: GraphCitation) -> bool:
        try:
            candidate = (anchor / citation.path).resolve()
        except OSError:  # pragma: no cover - platform-specific resolve failure
            return False
        try:
            relative = candidate.relative_to(anchor)
        except ValueError:
            return False  # the citation resolved outside the indexed checkout
        if not relative.parts:
            return False  # the citation resolved to the checkout root itself
        return not _names_private_state(relative.parts)

    return in_scope


def _default_resolver(anchor: Path) -> Callable[[GraphCitation], bool]:
    """Confirm line claims against the indexed checkout.

    Only reached for citations the scope check already accepted, so this answers
    one question: does the cited file really carry the line the graph claimed?

    A line claim is confirmed as soon as its last claimed line is seen, so a
    citation into a large generated file costs the lines up to the claim rather
    than a full read. ``MAX_GRAPH_CITATIONS`` bounds how many citations a packet
    may carry, but nothing bounds the size of any single cited file.
    """

    def resolve(citation: GraphCitation) -> bool:
        candidate = anchor / citation.path
        if not candidate.is_file():
            return False
        if citation.start_line is None:
            return True
        claimed = citation.end_line or citation.start_line
        try:
            with candidate.open("rb") as source:
                for seen, _ in enumerate(source, start=1):
                    if seen >= claimed:
                        return True
        except OSError:
            return False
        return False  # the file ended before the claimed line

    return resolve


def evaluate_graph_evidence(
    packet: Mapping[str, Any],
    *,
    repository_root: Path | None = None,
    revision_state: str,
    resolve: Callable[[GraphCitation], bool] | None = None,
) -> GraphEvidenceReport:
    """Score a validated repository-kind packet for scope, freshness and citations.

    ``packet`` must already have passed ``context_contract.load_packet``; this
    adds the local-graph rules that generic validation cannot express. Pass
    ``revision_state`` from the ``ValidatedPacket`` rather than recomputing it.

    A citation whose filesystem target escapes the indexed root or lands in
    excluded private state rejects the whole packet, the same way a textually
    out-of-scope path does; an unresolvable line claim only lowers the score.
    Supply ``resolve`` to score line claims without reading files, and omit
    ``repository_root`` to score with no filesystem at all — then only the
    text-level scope rules apply, since there is no target to resolve.
    """
    if packet.get("kind") != "repository":
        raise ContextError("local graph evidence requires a repository-kind packet")
    if revision_state not in ("matching", "stale", "unknown"):
        raise ContextError("unsupported local graph revision state")
    in_scope: Callable[[GraphCitation], bool] | None = None
    if repository_root is None:
        if resolve is None:
            raise ContextError("local graph evidence requires an indexed root or a resolver")
    else:
        if not repository_root.is_absolute():
            raise ContextError("local context repository root must be absolute")
        anchor = repository_root.resolve()
        in_scope = _scope_checker(anchor)
        if resolve is None:
            resolve = _default_resolver(anchor)

    # A validated packet always carries these; fail closed rather than raising a
    # bare KeyError if a caller scores something load_packet never accepted.
    if not {"documents", "completeness", "truncated"} <= packet.keys():
        raise ContextError("local graph evidence requires a validated context packet")

    total = 0
    line_claims = 0
    resolved = 0
    for document in packet["documents"]:
        if document.get("confidence") not in ("extracted", "inferred", "unknown"):
            raise ContextError("unsupported context evidence confidence")
        citations = document.get("citations")
        if not isinstance(citations, list) or not citations:
            raise ContextError("local graph evidence requires citations")
        for citation in citations:
            total += 1
            if total > MAX_GRAPH_CITATIONS:
                raise ContextError("local graph citation count exceeds its budget")
            if not isinstance(citation, Mapping):
                raise ContextError("local graph citation must be a structured reference")
            parsed = parse_graph_citation(citation.get("source"))
            if in_scope is not None and not in_scope(parsed):
                raise ContextError("local graph citation must stay inside the indexed repository")
            if parsed.start_line is None:
                # Nothing to score: the citation names a file but claims no
                # line. The scope check above is the only bound it ever gets,
                # which is why it runs before this skip rather than inside the
                # resolver.
                continue
            line_claims += 1
            resolved += bool(resolve(parsed))

    completeness = packet["completeness"]
    truncated = packet["truncated"]
    if truncated and completeness == "complete":
        raise ContextError("context completeness and truncation are inconsistent")
    return GraphEvidenceReport(
        revision_state=revision_state,
        total_citations=total,
        line_citations=line_claims,
        resolved_line_citations=resolved,
        completeness=completeness,
        truncated=truncated,
    )
