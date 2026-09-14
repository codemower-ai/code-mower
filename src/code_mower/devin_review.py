"""Private informational Devin review adapters over the v1 remote lifecycle.

The embedding orchestrator supplies authenticated current PR/context metadata and
an approved bounded prompt. Nothing returned here is a public metadata payload.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from copy import deepcopy
from pathlib import Path
from typing import Callable

from .context_review import marker, review_matches
from .devin_cli_audit_pr import (
    MAX_DEVIN_STDOUT_BYTES, DevinCliVerdict, _is_excluded_author,
    _validate_devin_cli_verdict,
)
from .devin_sessions import DevinClient, REPO
from .devin_work_orders import LOGIN, SHA
from .remote_session import DevinProvider, RemoteError, RemoteSessions, _digest


def completion_schema() -> dict:
    """Reuse the audit finding/verdict schema with the existing Devin wire shape."""
    value = json.loads(Path(__file__).with_name('codex_audit_verdict.schema.json').read_text())
    value['required'].remove('schema')
    del value['properties']['schema']
    return value


def normalize(output: object, changed_files: set[str]) -> DevinCliVerdict:
    """One bounded strict contract for CLI text and v3 structured output."""
    try:
        if isinstance(output, str):
            if len(output.encode()) > MAX_DEVIN_STDOUT_BYTES:
                raise ValueError
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            output = json.loads(output, object_pairs_hook=unique)
        if len(json.dumps(output, allow_nan=False).encode()) > MAX_DEVIN_STDOUT_BYTES:
            raise ValueError
        if not isinstance(output, dict) or set(output) != {'verdict', 'summary', 'findings'}:
            raise ValueError
        if (output['verdict'] not in ('pass', 'blocked') or
                not isinstance(output['summary'], str) or
                not isinstance(output['findings'], list) or len(output['findings']) > 50):
            raise ValueError
        for finding in output['findings']:
            if (not isinstance(finding, dict) or
                    any(not isinstance(finding.get(k), str)
                        for k in ('severity', 'title', 'file', 'detail')) or
                    type(finding.get('line')) is not int or finding['line'] < 1 or
                    finding['file'] not in changed_files):
                raise ValueError
        result = _validate_devin_cli_verdict(output, changed_files)
        if result.verdict == 'UNKNOWN':
            raise ValueError
        return result
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise RemoteError('invalid_review_output') from None


#: A Git branch name as it appears in authenticated pull request metadata.
#: Deliberately narrower than Git's own rules: this value only ever travels
#: into an exact-match binding, so anything exotic is a mismatch, not a name.
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")

#: How many published lineage markers the embedding orchestrator may carry.
#: The producer republishes the whole chain each round, so a real pull request
#: accumulates several; an unbounded list is refused rather than parsed.
MAX_REVIEW_LINEAGE_MARKERS = 32


@dataclass(frozen=True, repr=False)
class ReviewInput:
    repository: str
    pr: int
    head: str
    author: str
    context: dict
    changed_files: tuple[str, ...]
    #: Authenticated pull request metadata the orchestrator fetched. These are
    #: part of the immutable binding, not decoration: lineage is bound to a
    #: branch, and an episode resolved without one is an episode resolved
    #: against whatever branch happens to be in the record.
    branch: str = ''
    labels: tuple[str, ...] = ()
    #: Bodies of comments the orchestrator already established as written by a
    #: configured decision authority. The marker inside is a transport for
    #: bounded metadata and confers no authority of its own; carrying the whole
    #: body keeps parsing strict and keeps this adapter from having to invent a
    #: second episode format.
    lineage_markers: tuple[str, ...] = ()

    def published_lineage(self) -> tuple:
        """Episodes parsed from the trusted markers the orchestrator carried."""

        from .builder_lineage import episodes_from_comment_body

        if len(self.lineage_markers) > MAX_REVIEW_LINEAGE_MARKERS:
            raise ValueError('review_lineage_unbounded')
        return tuple(
            episode
            for body in self.lineage_markers
            for episode in episodes_from_comment_body(body)
        )

    def pr_metadata(self) -> dict:
        """The trusted metadata shape the shared resolver expects.

        Branch and labels are carried through rather than dropped, so the
        resolver binds episodes to this exact repository, pull request, branch
        and head instead of accepting any record that names the pull request.
        """

        return {
            'user': {'login': self.author},
            'head': {'ref': self.branch, 'sha': self.head},
            'labels': [{'name': name} for name in self.labels],
        }

    def lineage_admits(self) -> bool:
        """Whether verified lineage admits the Devin reviewer lane at this head.

        The author deny list below stays as a floor, but it only ever sees the
        opener. This consults the same shared seam the direct wrappers use, so a
        PR another lane opened and Devin later took over is refused too.
        Unreadable or unresolved evidence is not admission.
        """

        from .builder_lineage import LineageError
        from .provider_runners.lineage import (
            identity_with_lane_floor, load_identity, reviewer_admission, trusted_episodes,
        )

        try:
            episodes = trusted_episodes(
                self.repository, self.pr, published=self.published_lineage()
            )
        except (LineageError, OSError, ValueError):
            return False
        return bool(
            reviewer_admission(
                'devin',
                repo=self.repository,
                pr_number=self.pr,
                pr_meta=self.pr_metadata(),
                head_sha=self.head,
                episodes=episodes,
                identity=identity_with_lane_floor(load_identity(), 'devin'),
            )['admitted']
        )

    def check(self, current: ReviewInput) -> None:
        try:
            valid = (
                self == current and REPO.fullmatch(self.repository)
                and type(self.pr) is int and self.pr > 0 and SHA.fullmatch(self.head)
                and LOGIN.fullmatch(self.author) and not _is_excluded_author(self.author)
                and self.author.lower() not in {'devin-ai-integration', 'devin-ai-integration[bot]',
                                               'devin-cli-audit-bot', 'devin-cli-audit-bot[bot]'}
                and isinstance(self.branch, str) and len(self.branch) <= 255
                and (not self.branch or BRANCH.fullmatch(self.branch))
                and isinstance(self.labels, tuple) and len(self.labels) <= 64
                and all(isinstance(name, str) and 0 < len(name) <= 128 for name in self.labels)
                and isinstance(self.lineage_markers, tuple)
                and len(self.lineage_markers) <= MAX_REVIEW_LINEAGE_MARKERS
                and all(isinstance(body, str) for body in self.lineage_markers)
                and self.lineage_admits()
                and isinstance(self.changed_files, tuple)
                and all(isinstance(p, str) and p and not p.startswith(('/', '\\'))
                        and '\\' not in p and '..' not in p.split('/') for p in self.changed_files)
                and review_matches(marker(self.context, review=True), current.context, head=current.head)
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise RemoteError('review_binding_mismatch')


def check_current(review: ReviewInput, current: Callable[[], ReviewInput]) -> None:
    try:
        review.check(current())
    except Exception:
        raise RemoteError('review_binding_mismatch') from None


@dataclass(frozen=True, repr=False)
class ReviewEvidence:
    transport: str
    input: ReviewInput
    result: DevinCliVerdict
    refresh: Callable | None = field(default=None, repr=False)

    def accept(self, current: Callable[[], ReviewInput]) -> DevinCliVerdict:
        """Revalidate immediately at each private consumer; grants no merge authority."""
        check_current(self.input, current)
        if self.refresh is not None:
            return self.refresh().accept(current)
        return deepcopy(self.result)

    @property
    def merge_authority(self) -> bool:
        return False


def local_review(review: ReviewInput, current: Callable[[], ReviewInput],
                 run: Callable[[], tuple[str, int]]) -> ReviewEvidence:
    """Run the existing bounded disposable CLI audit via an embedding callback."""
    review = deepcopy(review)
    check_current(review, current)
    try:
        output, returncode = run()
    except Exception:
        raise RemoteError('review_execution_failed') from None
    if type(returncode) is not int or returncode != 0:
        raise RemoteError('invalid_review_output')
    result = normalize(output, set(review.changed_files))
    check_current(review, current)
    return ReviewEvidence('devin_cli', deepcopy(review), result)


class HostedReview:
    """A single immutable exact-head input, using RemoteSessions for all lifecycle I/O."""

    def __init__(self, remote: RemoteSessions, review: ReviewInput,
                 current: Callable[[], ReviewInput]):
        check_current(review, current)
        self.remote, self.review, self.current = remote, deepcopy(review), current
        # Content-address the trusted input; another head/context cannot adopt this result.
        self.session = 'review-' + _digest(review.__dict__)

    @classmethod
    def devin(cls, root: Path, client: DevinClient, review: ReviewInput,
              current: Callable[[], ReviewInput]) -> HostedReview:
        return cls(RemoteSessions(root, DevinProvider(client, completion_schema=completion_schema())),
                   review, current)

    def dispatch(self, prompt: str, *, limit: int = 1, apply: bool = False) -> dict:
        check_current(self.review, self.current)
        # Prompt binding is supplied by the adapter, never asserted by remote output.
        bound = ('Read-only informational review. Return only the requested verdict JSON.\n'
                 + json.dumps(self.review.__dict__, sort_keys=True) + '\n' + prompt)
        return self.remote.run('dispatch', self.session, prose=bound,
                               repo=self.review.repository, limit=limit, apply=apply)

    def collect(self, *, apply: bool = False) -> dict:
        check_current(self.review, self.current)
        status = self.remote.run('collect', self.session, apply=apply)
        check_current(self.review, self.current)
        return status

    def accept(self, *, _refresh: bool = True) -> ReviewEvidence:
        check_current(self.review, self.current)
        status = self.remote.run('status', self.session)
        if (status['state'] != 'complete' or status['reason'] != 'none'
                or status['counts'] != {'dispatch': 1, 'message': 0, 'cancel': 0, 'collect': 1}):
            raise RemoteError('review_not_ready')
        result = normalize(self.remote.private_result(self.session), set(self.review.changed_files))
        check_current(self.review, self.current)
        return ReviewEvidence('devin_api_v3', deepcopy(self.review), result,
                              (lambda: self.accept(_refresh=False)) if _refresh else None)
