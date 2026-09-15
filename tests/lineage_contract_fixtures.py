"""Pure fixtures for the builder lineage contract.

Deliberately self-contained. The lineage contract is pure, so its fixtures must
be too: importing a consumer test module here would pull an adapter -- and its
environment, store and network expectations -- into tests whose whole point is
that the decision needs none of them. Nothing in this module reads the
environment, touches the filesystem or imports anything outside
``code_mower.builder_lineage``.
"""

from __future__ import annotations

from typing import Any

from code_mower.builder_lineage import (
    CONTINUATION_WRITER_STATE,
    ContributionEpisode,
    lineage_comment_marker,
)


REPO = "codemower-ai/code-mower"
BRANCH = "devin/959-release-dogfood"
PR = 959

OPENED = "a" * 40
TAKEN = "b" * 40
MOVED = "c" * 40

AUTHORITY = "codemower-ai"
OUTSIDER = "a-passer-by"

#: The deployment identity contract the product renders, including the branch
#: provenance fields that are read only together.
IDENTITY: dict[str, Any] = {
    "enabled": True,
    "labels": {
        "builder:devin": "devin",
        "builder:codex": "codex",
        "builder:claude": "claude",
    },
    "authors": {
        "devin-ai-integration[bot]": "devin",
        "chatgpt-codex-connector[bot]": "codex",
        "claude[bot]": "claude",
    },
    "branch_prefixes": {
        "devin/": "devin",
        "codex/": "codex",
        "claude/": "claude",
        "feature/cx-": "codex",
    },
    "require_verified_lineage": True,
}

#: The same contract without the branch fields: a deployment that never asked
#: for verified lineage keeps the answer it has always had.
UNCONFIGURED: dict[str, Any] = {
    "enabled": True,
    "labels": dict(IDENTITY["labels"]),
    "authors": dict(IDENTITY["authors"]),
}


def head(index: int) -> str:
    """A distinct, well-formed 40-hex head for chain position ``index``."""

    return f"{index:040x}"


def takeover(**overrides: Any) -> ContributionEpisode:
    """The primary fixture: Devin opened it, Codex verifiably took it over."""

    payload: dict[str, Any] = dict(
        sequence=1,
        repo=REPO,
        pr_number=PR,
        branch=BRANCH,
        source_lane="devin",
        destination_lane="codex",
        expected_head=OPENED,
        resulting_head=TAKEN,
        writer_state="terminated",
    )
    payload.update(overrides)
    return ContributionEpisode(**payload)


def continuation(*, sequence: int, expected: str, resulting: str, lane: str = "codex"):
    """An ordinary fix round by the lane that already holds the pen."""

    return ContributionEpisode(
        sequence=sequence,
        kind="continuation",
        repo=REPO,
        pr_number=PR,
        branch=BRANCH,
        source_lane=lane,
        destination_lane=lane,
        expected_head=expected,
        resulting_head=resulting,
        writer_state=CONTINUATION_WRITER_STATE,
    )


def variant(episode: ContributionEpisode, **overrides: Any) -> ContributionEpisode:
    """The same episode with fields replaced, still strictly constructed."""

    payload = {
        "sequence": episode.sequence,
        "kind": episode.kind,
        "repo": episode.repo,
        "pr_number": episode.pr_number,
        "branch": episode.branch,
        "source_lane": episode.source_lane,
        "destination_lane": episode.destination_lane,
        "expected_head": episode.expected_head,
        "resulting_head": episode.resulting_head,
        "writer_state": episode.writer_state,
    }
    payload.update(overrides)
    return ContributionEpisode(**payload)


def chain(length: int) -> tuple[ContributionEpisode, ...]:
    """A takeover followed by ``length - 1`` continuations by the new writer."""

    episodes = [takeover(resulting_head=head(1))]
    for index in range(2, length + 1):
        episodes.append(
            continuation(
                sequence=index,
                expected=episodes[-1].resulting_head,
                resulting=head(index),
            )
        )
    return tuple(episodes)


def cumulative_comments(episodes, *, author: str = AUTHORITY) -> list[dict[str, Any]]:
    """One published comment per round, each carrying the whole chain so far.

    This is what the supported publication contract actually produces, so it is
    what the arrival bound has to accommodate.
    """

    return [
        comment(body=lineage_comment_marker(episodes[:length]), author=author)
        for length in range(1, len(episodes) + 1)
    ]


def comment(*, body: str, author: str = AUTHORITY, field: str = "user") -> dict[str, Any]:
    """One comment record under either supported transport's author field."""

    return {field: {"login": author}, "body": body}


def published(episodes, *, author: str = AUTHORITY, field: str = "user"):
    """A single trusted comment carrying one published lineage marker."""

    return comment(body=lineage_comment_marker(tuple(episodes)), author=author, field=field)


def pr_meta(
    *,
    author: str = "devin-ai-integration[bot]",
    labels: tuple[str, ...] = ("builder:codex",),
    branch: str = BRANCH,
    sha: str = TAKEN,
) -> dict[str, Any]:
    """Trusted pull request metadata, in GitHub's own shape."""

    return {
        "user": {"login": author},
        "head": {"ref": branch, "sha": sha},
        "labels": [{"name": name} for name in labels],
    }


#: Records whose meaning cannot be recovered. Each is a *present* field holding
#: the wrong type, or a null in a position GitHub never nulls.
MALFORMED_COMMENT_RECORDS: tuple[dict[str, Any], ...] = (
    {"user": {"login": AUTHORITY}, "body": 12345},
    {"user": {"login": AUTHORITY}, "body": {"text": "hi"}},
    {"user": {"login": AUTHORITY}, "body": ["hi"]},
    {"user": AUTHORITY, "body": "hi"},
    {"user": 7, "body": "hi"},
    {"user": [AUTHORITY], "body": "hi"},
    {"user": {"login": {"name": AUTHORITY}}, "body": "hi"},
    {"user": {"login": 7}, "body": "hi"},
    {"user": {"login": [AUTHORITY]}, "body": "hi"},
    {"user": {"login": None}, "body": "hi"},
    {"user": {"login": AUTHORITY}, "body": None},
    {"author": {"login": None}, "body": "hi"},
    {"author": AUTHORITY, "body": "hi"},
    {"author": {"login": 7}, "body": "hi"},
)

#: GitHub's own schema, which must keep working: a comment from a deleted
#: account carries ``user: null``, and ``body`` is optional on some
#: representations. Neither names an author or a marker, and neither is an
#: error.
VALID_COMMENT_RECORDS: tuple[dict[str, Any], ...] = (
    {"user": None, "body": "a deleted account said this"},
    {"user": {"login": AUTHORITY}},
    {"user": {}, "body": "an author object naming nobody"},
    {"body": "a record with no author field at all"},
    {"author": None, "body": "the gh/GraphQL transport, deleted account"},
    {"author": {"login": AUTHORITY}, "body": "the gh/GraphQL transport"},
    {"user": {"login": AUTHORITY}, "body": "ordinary comment"},
)

#: Successful reads that carry no readable history. None of them is "no
#: comments", and normalising them into one is what admits a reviewer onto a
#: diff whose takeover marker was in the part that got dropped.
INVALID_COMMENT_RESPONSES: tuple[Any, ...] = (
    None,
    False,
    {},
    {"comments": [{"user": {"login": AUTHORITY}, "body": "hi"}]},
    "a string",
    [{"user": {"login": AUTHORITY}, "body": "hi"}, "not a comment"],
    [None],
    [[{"user": {"login": AUTHORITY}, "body": "hi"}]],
)
