"""Scope and freshness checks for local-repository (graph) context evidence.

A repository-kind connection has no OAuth principal, so the packet bindings in
``context_contract`` cannot bound where its citations point. A local graph
provider indexes a checkout and emits file/line citations, and nothing in the
generic packet schema stops it from citing its own cache, a sibling worktree,
or an absolute path outside the indexed root.

These helpers are provider-neutral. They are the offline half of the Graphify
evaluation in issue #876: they establish what a local graph adapter must prove
before it can deliver evidence, without installing a graph package, running an
indexer, or adding a dependency. Nothing here performs retrieval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .context_contract import ContextError, _text


#: ``path``, ``path#L12`` or ``path#L12-L20``. Line numbers are 1-based.
_CITATION = re.compile(r"(?P<path>[^#]{1,1024})(?:#L(?P<start>\d{1,7})(?:-L(?P<end>\d{1,7}))?)?\Z")
MAX_GRAPH_CITATIONS = 200

#: Directories a local indexer writes to or reads across. Citing into one of
#: these means the graph escaped the immutable checkout it was asked to index.
#: Compared case-folded: on a case-insensitive filesystem (APFS and NTFS by
#: default) ``.GIT/config`` names the same directory as ``.git/config``.
_EXCLUDED_ROOTS = frozenset({".git", ".graph", ".graphify", ".code-mower"})


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
    line span names a file but makes no line claim to resolve. ``revision_state``
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
        or parts[0].lower() in _EXCLUDED_ROOTS
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


def _default_resolver(root: Path) -> Callable[[GraphCitation], bool]:
    """Resolve line spans against the indexed checkout without following links.

    ``Path.resolve`` on the candidate is compared to the resolved root so a
    symlink planted inside the checkout cannot point the citation elsewhere.

    A line claim is confirmed as soon as its last claimed line is seen, so a
    citation into a large generated file costs the lines up to the claim rather
    than a full read. ``MAX_GRAPH_CITATIONS`` bounds how many citations a packet
    may carry, but nothing bounds the size of any single cited file.
    """
    anchor = root.resolve()

    def resolve(citation: GraphCitation) -> bool:
        candidate = (anchor / citation.path).resolve()
        if candidate != anchor and anchor not in candidate.parents:
            return False
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
    Supply ``resolve`` to score without touching a filesystem.
    """
    if packet.get("kind") != "repository":
        raise ContextError("local graph evidence requires a repository-kind packet")
    if revision_state not in ("matching", "stale", "unknown"):
        raise ContextError("unsupported local graph revision state")
    if resolve is None:
        if repository_root is None:
            raise ContextError("local graph evidence requires an indexed root or a resolver")
        if not repository_root.is_absolute():
            raise ContextError("local context repository root must be absolute")
        resolve = _default_resolver(repository_root)

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
            if parsed.start_line is None:
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
