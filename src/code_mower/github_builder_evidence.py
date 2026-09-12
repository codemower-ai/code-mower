"""Bounded, read-only GitHub GraphQL observations for hosted builder evidence."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .devin_sessions import REPO
from .devin_work_orders import Candidates, PullRequest, _branch, _positive
from .remote_session import RemoteError

MAX_BYTES = 512 * 1024
FIELDS = """
number state repository { nameWithOwner }
author { login ... on User { databaseId } ... on Bot { databaseId } }
headRepository { nameWithOwner } headRefName headRefOid baseRefName
closingIssuesReferences(first: 2) {
  nodes { number repository { nameWithOwner } } pageInfo { hasNextPage }
}
"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubBuilderEvidence:
    """Optional runner(query, variables, headers) replaces HTTP entirely in tests.

    Runner implementations must enforce the same 30s deadline and byte bound while
    reading. Queries never request prose, files, diffs or review content.
    """
    def __init__(self, token: str, *, runner=None):
        if not isinstance(token, str) or not token or any(ord(c) < 33 for c in token):
            raise RemoteError("github_authentication_required")
        self._token, self._runner = token, runner

    def __getstate__(self):
        raise TypeError("GitHub clients cannot be persisted")

    def _query(self, query, variables):
        started = time.monotonic()
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": "code-mower-builder-evidence"}
        try:
            if self._runner:
                value = self._runner(query, variables, headers)
            else:
                request = urllib.request.Request(
                    "https://api.github.com/graphql", method="POST", headers=headers,
                    data=json.dumps({"query": query, "variables": variables}).encode())
                with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                    chunks = bytearray()
                    while True:
                        remaining = 30 - (time.monotonic() - started)
                        if remaining <= 0:
                            raise ValueError("deadline")
                        response.fp.raw._sock.settimeout(remaining)
                        chunk = response.read1(min(65536, MAX_BYTES + 1 - len(chunks)))
                        chunks.extend(chunk)
                        if len(chunks) > MAX_BYTES:
                            raise ValueError("size")
                        if not chunk or response.isclosed():
                            break
                    value = json.loads(chunks)
            if (time.monotonic() - started > 30 or not isinstance(value, dict)
                    or len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES
                    or value.get("errors")):
                raise ValueError("invalid")
            repo = value["data"]["repository"]
            if not isinstance(repo, dict):
                raise ValueError("missing")
            return repo
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RemoteError("github_unavailable") from None
        except Exception:
            raise RemoteError("github_unavailable") from None

    @staticmethod
    def _repository(repository):
        if not isinstance(repository, str) or len(repository) > 256 or not REPO.fullmatch(repository):
            raise RemoteError("invalid_request")
        owner, name = repository.split("/")
        return {"owner": owner, "name": name}

    @staticmethod
    def _pr(raw):
        try:
            links = raw["closingIssuesReferences"]
            if (links["pageInfo"]["hasNextPage"] is not False
                    or not isinstance(links["nodes"], list) or len(links["nodes"]) > 2):
                raise ValueError("incomplete")
            issues = tuple((issue["repository"]["nameWithOwner"], issue["number"])
                           for issue in links["nodes"])
            if any(not isinstance(repo, str) or not _positive(number) for repo, number in issues):
                raise ValueError("invalid")
            return PullRequest(
                raw["repository"]["nameWithOwner"], raw["number"], issues,
                raw["author"]["databaseId"], raw["author"]["login"],
                raw["headRepository"]["nameWithOwner"], raw["headRefName"], raw["headRefOid"],
                raw["baseRefName"], raw["state"].lower())
        except Exception:
            raise RemoteError("github_invalid_response") from None

    def candidates(self, repository, branch, *, limit=2):
        if type(limit) is not int or limit != 2 or not _branch(branch):
            raise RemoteError("invalid_request")
        query = ("query($owner:String!,$name:String!,$branch:String!){"
                 "repository(owner:$owner,name:$name){"
                 "pullRequests(first:2,headRefName:$branch,states:[OPEN,CLOSED,MERGED]){"
                 "pageInfo{hasNextPage} nodes{" + FIELDS + "}}}}")
        data = self._query(query, {**self._repository(repository), "branch": branch})
        try:
            page = data["pullRequests"]
            nodes, more = page["nodes"], page["pageInfo"]["hasNextPage"]
            if not isinstance(nodes, list) or len(nodes) > 2 or type(more) is not bool:
                raise ValueError("invalid")
            return Candidates(tuple(self._pr(node) for node in nodes), not more)
        except Exception:
            raise RemoteError("github_invalid_response") from None

    def read(self, repository, number):
        if not _positive(number):
            raise RemoteError("invalid_request")
        query = ("query($owner:String!,$name:String!,$number:Int!){"
                 "repository(owner:$owner,name:$name){pullRequest(number:$number){" + FIELDS + "}}}")
        data = self._query(query, {**self._repository(repository), "number": number})
        return self._pr(data.get("pullRequest"))
