"""Metadata-only local audit publication through a default-branch workflow.

This module is also shipped as a standalone tools helper. Keep it stdlib-only:
no PR checkout, installed package, or submitted Python participates in trust.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SCHEMA = "code_mower.auditPublication.v1"
WORKFLOW = ".github/workflows/local-audit-publication.yml"
WORKFLOW_NAME = "Code Mower Local Audit Publication"
EVENT = "code-mower-local-audit"
LABEL_EVENT = "code-mower-local-audit-published"
SOURCE_WORKFLOW = ".github/workflows/local-cli-audit.yml"
MAX_BYTES = 2048
MAX_EVENT_BYTES = 128 * 1024
MAX_AGE = 24 * 60 * 60
MAX_PAGES = 10
FIELDS = frozenset(
    {
        "schema",
        "repository_id",
        "pr_number",
        "head_sha_start",
        "head_sha_end",
        "lane",
        "verdict",
        "created_at",
        "source_run_id",
        "source_run_attempt",
    }
)
MARKER = "<!-- CODE_MOWER_AUDIT_PUBLICATION: "
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
RECEIPT = re.compile(r"Local audit pr=([1-9][0-9]*) comment=([1-9][0-9]*) digest=([0-9a-f]{64})")


class Refused(ValueError):
    """An expected refusal; its arguments are never safe to print directly."""


# Public diagnostic vocabulary. Keys must cover only literal refusal reasons;
# values are stable codes, never derived from an exception or submitted data.
# Unknown reasons and all other exception types fail closed as INTERNAL_ERROR.
REFUSAL_CODES = {
    "GitHub response size limit": "GITHUB_RESPONSE_SIZE_LIMIT",
    "PR target/head changed": "PR_TARGET_HEAD_CHANGED",
    "artifact already dispatched": "ARTIFACT_ALREADY_DISPATCHED",
    "artifact is not merge authority": "ARTIFACT_IS_NOT_MERGE_AUTHORITY",
    "artifact stale or future dated": "ARTIFACT_STALE_OR_FUTURE_DATED",
    "audit head changed": "AUDIT_HEAD_CHANGED",
    "comment publication mismatch": "COMMENT_PUBLICATION_MISMATCH",
    "complete GitHub history exceeds page limit": "COMPLETE_GITHUB_HISTORY_EXCEEDS_PAGE_LIMIT",
    "context-bound artifacts need their context-aware direct path": "CONTEXT_BOUND_ARTIFACT",
    "duplicate JSON key": "DUPLICATE_JSON_KEY",
    "duplicate publication reservations": "DUPLICATE_PUBLICATION_RESERVATIONS",
    "input size limit": "INPUT_SIZE_LIMIT",
    "invalid GitHub page": "INVALID_GITHUB_PAGE",
    "invalid JSON": "INVALID_JSON",
    "invalid artifact timestamp": "INVALID_ARTIFACT_TIMESTAMP",
    "invalid comment size": "INVALID_COMMENT_SIZE",
    "invalid dispatch schema": "INVALID_DISPATCH_SCHEMA",
    "invalid local artifact": "INVALID_LOCAL_ARTIFACT",
    "invalid local artifact timestamp": "INVALID_LOCAL_ARTIFACT_TIMESTAMP",
    "invalid publication schema": "INVALID_PUBLICATION_SCHEMA",
    "invalid publishing run": "INVALID_PUBLISHING_RUN",
    "invalid repository": "INVALID_REPOSITORY",
    "invalid review request": "INVALID_REVIEW_REQUEST",
    "invalid run/comment id": "INVALID_RUN_COMMENT_ID",
    "invalid source run/attempt": "INVALID_SOURCE_RUN_ATTEMPT",
    "invalid target": "INVALID_TARGET",
    "invalid workflow SHA": "INVALID_WORKFLOW_SHA",
    "local repository/lane mismatch": "LOCAL_REPOSITORY_LANE_MISMATCH",
    "local timestamp lacks timezone": "LOCAL_TIMESTAMP_LACKS_TIMEZONE",
    "local verdict/trailer mismatch": "LOCAL_VERDICT_TRAILER_MISMATCH",
    "missing or repeated publication metadata": "MISSING_OR_REPEATED_PUBLICATION_METADATA",
    "missing publication receipt": "MISSING_PUBLICATION_RECEIPT",
    "missing publication run": "MISSING_PUBLICATION_RUN",
    "missing workflow identity": "MISSING_WORKFLOW_IDENTITY",
    "noncanonical publication bytes": "NONCANONICAL_PUBLICATION_BYTES",
    "publication already reserved": "PUBLICATION_ALREADY_RESERVED",
    "publication comment not found at current head": "PUBLICATION_COMMENT_NOT_FOUND_AT_CURRENT_HEAD",
    "publication digest mismatch": "PUBLICATION_DIGEST_MISMATCH",
    "publication receipt missing or ambiguous": "PUBLICATION_RECEIPT_MISSING_OR_AMBIGUOUS",
    "publication run not successful": "PUBLICATION_RUN_NOT_SUCCESSFUL",
    "publication timed out; inspect the workflow run before retrying": "PUBLICATION_TIMED_OUT",
    "published comment binding failed": "PUBLISHED_COMMENT_BINDING_FAILED",
    "quarantined local artifact": "QUARANTINED_LOCAL_ARTIFACT",
    "replayed publication": "REPLAYED_PUBLICATION",
    "reservation lacks publishing run": "RESERVATION_LACKS_PUBLISHING_RUN",
    "run lookup mismatch": "RUN_LOOKUP_MISMATCH",
    "source reviewer seal missing or ambiguous": "SOURCE_REVIEWER_SEAL_MISSING_OR_AMBIGUOUS",
    "unexpected publishing identity": "UNEXPECTED_PUBLISHING_IDENTITY",
    "unsupported publication command": "UNSUPPORTED_PUBLICATION_COMMAND",
    "unsupported publication schema": "UNSUPPORTED_PUBLICATION_SCHEMA",
    "unsupported reviewer lane": "UNSUPPORTED_REVIEWER_LANE",
    "unsupported verdict": "UNSUPPORTED_VERDICT",
    "untrusted review request": "UNTRUSTED_REVIEW_REQUEST",
    "untrusted review target": "UNTRUSTED_REVIEW_TARGET",
    "untrusted source audit run": "UNTRUSTED_SOURCE_AUDIT_RUN",
    "untrusted staging environment": "UNTRUSTED_STAGING_ENVIRONMENT",
    "untrusted workflow name": "UNTRUSTED_WORKFLOW_NAME",
    "untrusted workflow ref": "UNTRUSTED_WORKFLOW_REF",
    "untrusted workflow/event": "UNTRUSTED_WORKFLOW_EVENT",
    "workflow rerun refused": "WORKFLOW_RERUN_REFUSED",
    "wrong PR/head": "WRONG_PR_HEAD",
    "wrong comment binding": "WRONG_COMMENT_BINDING",
    "wrong dispatch PR": "WRONG_DISPATCH_PR",
    "wrong dispatch event": "WRONG_DISPATCH_EVENT",
    "wrong dispatch repository": "WRONG_DISPATCH_REPOSITORY",
    "wrong publication receipt": "WRONG_PUBLICATION_RECEIPT",
    "wrong publishing run binding": "WRONG_PUBLISHING_RUN_BINDING",
    "wrong reconciliation event": "WRONG_RECONCILIATION_EVENT",
    "wrong repository": "WRONG_REPOSITORY",
    "wrong run": "WRONG_RUN",
    "wrong run repository": "WRONG_RUN_REPOSITORY",
    "wrong staging binding": "WRONG_STAGING_BINDING",
    "wrong workflow/ref/attempt": "WRONG_WORKFLOW_REF_ATTEMPT",
}


def refusal_code(error):
    """Select a literal code without formatting untrusted exception arguments."""
    if type(error) is Refused and len(error.args) == 1 and type(error.args[0]) is str:
        return REFUSAL_CODES.get(error.args[0], "INTERNAL_ERROR")
    return "INTERNAL_ERROR"


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(text):
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode(text, limit=MAX_BYTES):
    require(isinstance(text, str) and len(text.encode("utf-8")) <= limit, "input size limit")
    try:
        return json.loads(text, object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        raise Refused("invalid JSON") from None


def positive(value):
    return type(value) is int and 0 < value < 2**53


def validate(text, expected_digest, *, now=None):
    value = decode(text)
    require(isinstance(value, dict) and set(value) == FIELDS, "invalid publication schema")
    require(value["schema"] == SCHEMA, "unsupported publication schema")
    require(canonical(value) == text, "noncanonical publication bytes")
    require(
        isinstance(expected_digest, str)
        and DIGEST.fullmatch(expected_digest)
        and digest(text) == expected_digest,
        "publication digest mismatch",
    )
    require(positive(value["repository_id"]) and positive(value["pr_number"]), "invalid target")
    require(
        positive(value["source_run_id"])
        and value["source_run_attempt"] == 1
        and type(value["source_run_attempt"]) is int,
        "invalid source run/attempt",
    )
    require(value["lane"] in ("claude", "codex"), "unsupported reviewer lane")
    require(value["verdict"] in ("PASS", "BLOCKED"), "unsupported verdict")
    require(
        isinstance(value["head_sha_start"], str)
        and SHA.fullmatch(value["head_sha_start"])
        and value["head_sha_start"] == value["head_sha_end"],
        "audit head changed",
    )
    require(positive(value["created_at"]), "invalid artifact timestamp")
    if now is not None:
        require(0 <= now - value["created_at"] <= MAX_AGE, "artifact stale or future dated")
    return value


def receipt_name(value, comment_id):
    return f"Local audit pr={value['pr_number']} comment={comment_id} digest={digest(canonical(value))}"


def seal_name(value):
    return "Code Mower reviewer seal " + digest(canonical(value))


def verify_source(io, value, repository):
    """Require a completed seal step in a default-branch reviewer job.

    Dispatch credentials cannot write Actions job/step records. The enclosing
    job may still be waiting for publication, so only the seal must be terminal.
    repository_dispatch always runs default-branch code, including the source
    wrapper. Unlike pull_request_target there is no alternate base-ref workflow
    that a builder can replace. The sealed digest binds the actual reviewed head.
    """
    source = io.request(f"/actions/runs/{value['source_run_id']}")
    require(
        source.get("id") == value["source_run_id"]
        and source.get("run_attempt") == value["source_run_attempt"]
        and source.get("path") == SOURCE_WORKFLOW
        and source.get("event") == "repository_dispatch"
        and source.get("head_branch") == repository["default_branch"]
        and isinstance(source.get("head_sha"), str)
        and SHA.fullmatch(source["head_sha"])
        and source.get("repository", {}).get("id") == repository["id"]
        and source.get("head_repository", {}).get("id") == repository["id"],
        "untrusted source audit run",
    )
    jobs = io.pages(f"/actions/runs/{source['id']}/attempts/1/jobs", "jobs")
    matching = [
        job
        for job in jobs
        if job.get("run_id") == source["id"]
        and job.get("name") == f"audit ({value['lane']})"
        and any(
            step.get("name") == seal_name(value)
            and step.get("status") == "completed"
            and step.get("conclusion") == "success"
            for step in job.get("steps", [])
        )
    ]
    require(len(matching) == 1, "source reviewer seal missing or ambiguous")
    return source


def receipts(run):
    jobs = run.get("publication_jobs")
    require(isinstance(jobs, list), "missing publication receipt")
    return [
        job
        for job in jobs
        if job.get("run_id") == run["id"]
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and isinstance(job.get("name"), str)
        and RECEIPT.fullmatch(job["name"])
    ]


def load_run(io, run_id):
    run = io.request(f"/actions/runs/{run_id}")
    require(str(run.get("id")) == str(run_id), "run lookup mismatch")
    run["publication_jobs"] = io.pages(f"/actions/runs/{run_id}/jobs", "jobs")
    return run


def trailer(value):
    lane = value["lane"]
    state = "done" if value["verdict"] == "PASS" else "blocked"
    return f"<!-- {lane.upper()}_AUDIT_STATE: {lane}-audit-{state} -->"


def reservation(value):
    return f"<!-- CODE_MOWER_AUDIT_RESERVATION: sha256={digest(canonical(value))} -->"


def render(value, run, comment_id):
    require(positive(run.get("id")) and positive(comment_id), "invalid run/comment id")
    require(
        isinstance(run.get("head_sha"), str) and SHA.fullmatch(run["head_sha"]),
        "invalid workflow SHA",
    )
    body = (
        f"## {value['lane'].title()} audit (merge-authority lane)\n\n"
        f"Head SHA: `{value['head_sha_start']}`\nVerdict: {value['verdict']}\n"
        "Review details remain in the local audit artifact.\n"
        f"Publication workflow: `{WORKFLOW}` at `{run['head_sha']}`\n"
        f"{MARKER}{canonical(value)} -->\n{reservation(value)}\n"
        f"<!-- CODE_MOWER_AUDIT_RUN: run_id={run['id']} comment_id={comment_id} -->\n"
        f"{trailer(value)}\n"
    )
    body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return body.replace(
        f"comment_id={comment_id} -->", f"comment_id={comment_id} body_sha256={body_digest} -->"
    )


def metadata(body):
    require(isinstance(body, str) and len(body.encode("utf-8")) <= 4096, "invalid comment size")
    require(body.count(MARKER) == 1, "missing or repeated publication metadata")
    text = body.split(MARKER, 1)[1].split(" -->", 1)[0]
    return validate(text, digest(text))


def verify_run(run, repository, *, run_id=None, terminal=False):
    require(isinstance(run, dict) and isinstance(repository, dict), "missing workflow identity")
    require(
        positive(run.get("id")) and (run_id is None or str(run["id"]) == str(run_id)), "wrong run"
    )
    require(
        run.get("path") == WORKFLOW and run.get("event") == "repository_dispatch",
        "untrusted workflow/event",
    )
    require(run.get("run_attempt") == 1, "workflow rerun refused")
    require(
        run.get("repository", {}).get("id") == repository.get("id")
        and run.get("repository", {}).get("full_name") == repository.get("full_name")
        and run.get("head_repository", {}).get("id") == repository.get("id"),
        "wrong run repository",
    )
    require(
        run.get("head_branch") == repository.get("default_branch")
        and bool(repository.get("default_branch")),
        "untrusted workflow ref",
    )
    require(
        isinstance(run.get("head_sha"), str) and SHA.fullmatch(run["head_sha"]),
        "invalid workflow SHA",
    )
    # repository_dispatch uses the event type as display_title; name identifies
    # the workflow, alongside the trusted path checked above.
    require(run.get("name") == WORKFLOW_NAME, "untrusted workflow name")
    if terminal:
        require(
            run.get("status") == "completed" and run.get("conclusion") == "success",
            "publication run not successful",
        )


def attested(*, body, comment_id, repo, issue_number, head_sha, run, repository):
    """Strict extension of CODE_MOWER_AUDIT_RUN for repository_dispatch runs."""
    try:
        verify_run(run, repository, terminal=True)
        value = metadata(body)
        require(
            repository.get("full_name") == repo and value["repository_id"] == repository.get("id"),
            "wrong repository",
        )
        require(
            value["pr_number"] == issue_number and value["head_sha_start"] == head_sha,
            "wrong PR/head",
        )
        proof = receipts(run)
        require(
            len(proof) == 1 and proof[0]["name"] == receipt_name(value, comment_id),
            "wrong publication receipt",
        )
        require(
            str(comment_id).isdigit() and body == render(value, run, int(comment_id)),
            "wrong comment binding",
        )
        return True
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return False


class GitHub:
    def __init__(self, repo, token):
        require(
            isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo),
            "invalid repository",
        )
        self.repo, self.token = repo, token

    def request(self, path, *, method="GET", body=None):
        # Fixed host and relative paths only. Neither artifacts nor URLs select a destination.
        req = urllib.request.Request(
            "https://api.github.com/repos/" + self.repo + path,
            data=None if body is None else canonical(body).encode("ascii"),
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        require(len(raw) <= 8 * 1024 * 1024, "GitHub response size limit")
        return decode(raw.decode("utf-8"), 8 * 1024 * 1024) if raw else None

    def pages(self, path, key=None):
        items = []
        sep = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            value = self.request(f"{path}{sep}per_page=100&page={page}")
            batch = value.get(key) if key else value
            require(
                isinstance(batch, list) and all(isinstance(item, dict) for item in batch),
                "invalid GitHub page",
            )
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise Refused("complete GitHub history exceeds page limit")


def current_pr(io, value):
    pr = io.request(f"/pulls/{value['pr_number']}")
    require(
        pr.get("number") == value["pr_number"]
        and pr.get("state") == "open"
        and pr.get("base", {}).get("repo", {}).get("id") == value["repository_id"]
        and pr.get("head", {}).get("sha") == value["head_sha_start"],
        "PR target/head changed",
    )
    return pr


def recent_runs(io, value):
    since = datetime.fromtimestamp(value["created_at"] - 60, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    query = urllib.parse.urlencode({"event": "repository_dispatch", "created": ">=" + since})
    return io.pages(
        f"/actions/workflows/{WORKFLOW.rsplit('/', 1)[1]}/runs?{query}", "workflow_runs"
    )


def publish(event, env, io, *, now=None):
    require(
        env.get("GITHUB_EVENT_NAME") == "repository_dispatch" and event.get("action") == EVENT,
        "wrong dispatch event",
    )
    payload = event.get("client_payload")
    require(
        isinstance(payload, dict) and set(payload) == {"artifact", "digest", "pr_number"},
        "invalid dispatch schema",
    )
    value = validate(
        payload["artifact"], payload["digest"], now=int(time.time()) if now is None else now
    )
    require(
        type(payload["pr_number"]) is int and payload["pr_number"] == value["pr_number"],
        "wrong dispatch PR",
    )
    repository = io.request("")
    require(
        repository.get("full_name") == io.repo
        and repository.get("id") == value["repository_id"]
        and event.get("repository", {}).get("id") == value["repository_id"],
        "wrong dispatch repository",
    )
    default_ref = "refs/heads/" + repository["default_branch"]
    require(
        env.get("GITHUB_REF") == default_ref
        and env.get("GITHUB_WORKFLOW_REF") == f"{io.repo}/{WORKFLOW}@{default_ref}"
        and env.get("GITHUB_RUN_ATTEMPT") == "1",
        "wrong workflow/ref/attempt",
    )
    run_id = env.get("GITHUB_RUN_ID", "")
    require(run_id.isdigit(), "invalid publishing run")
    run = io.request(f"/actions/runs/{run_id}")
    verify_run(run, repository, run_id=run_id)
    require(run["head_sha"] == env.get("GITHUB_SHA"), "wrong publishing run binding")
    verify_source(io, value, repository)
    # The global workflow concurrency group serializes reservations. Successful
    # receipt jobs are a second durable replay ledger even if a comment is deleted.
    for earlier in recent_runs(io, value):
        if earlier.get("id", 0) < run["id"] and earlier.get("conclusion") == "success":
            proof = receipts(load_run(io, earlier["id"]))
            require(
                not any(RECEIPT.fullmatch(job["name"])[3] == payload["digest"] for job in proof),
                "replayed publication",
            )
    current_pr(io, value)
    comments = io.pages(f"/issues/{value['pr_number']}/comments")
    require(
        not any(
            reservation(value) in str(item.get("body", ""))
            and item.get("user", {}).get("login") == "github-actions[bot]"
            for item in comments
        ),
        "publication already reserved",
    )
    pending = (
        "Local audit publication reserved; no audit verdict yet.\n"
        + reservation(value)
        + f"\n<!-- CODE_MOWER_AUDIT_PENDING: run_id={run['id']} -->"
    )
    comment = io.request(
        f"/issues/{value['pr_number']}/comments", method="POST", body={"body": pending}
    )
    require(
        positive(comment.get("id"))
        and comment.get("user", {}).get("login") == "github-actions[bot]",
        "unexpected publishing identity",
    )
    path = f"/issues/comments/{comment['id']}"
    try:
        current_pr(io, value)
        verify_source(io, value, repository)
        body = render(value, run, comment["id"])
        posted = io.request(path, method="PATCH", body={"body": body})
        require(
            posted.get("id") == comment["id"]
            and posted.get("body") == body
            and posted.get("user", {}).get("login") == "github-actions[bot]",
            "comment publication mismatch",
        )
        current_pr(io, value)
        verify_source(io, value, repository)
        return posted
    except Exception:
        # Consumers require successful completion: even a failed cleanup cannot
        # give a transient comment merge authority.
        io.request(path, method="PATCH", body={"body": pending + "\nPublication failed closed."})
        raise


def project_local(artifact, repository, *, lane, now):
    """Project fields explicitly; never upload the local artifact or its prose."""
    require(
        isinstance(artifact, dict)
        and artifact.get("schema") == "code_mower.auditVerdictArtifact.v1",
        "invalid local artifact",
    )
    require(
        artifact.get("repo") == repository.get("full_name")
        and artifact.get("lane_id") == lane + "-audit",
        "local repository/lane mismatch",
    )
    require(
        artifact.get("quarantined") is not True and not artifact.get("quarantine_reason"),
        "quarantined local artifact",
    )
    body = artifact.get("comment_body")
    require(
        isinstance(body, str)
        and body.startswith(f"## {lane.title()} audit (merge-authority lane)\n\n"),
        "artifact is not merge authority",
    )
    require(
        "CODE_MOWER_CONTEXT_REVIEW:" not in body,
        "context-bound artifacts need their context-aware direct path",
    )
    try:
        created = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
        require(created.tzinfo is not None, "local timestamp lacks timezone")
        created_at = int(created.timestamp())
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise Refused("invalid local artifact timestamp") from None
    value = {
        "schema": SCHEMA,
        "repository_id": repository["id"],
        "pr_number": artifact.get("pr_number"),
        "head_sha_start": artifact.get("head_sha_start"),
        "head_sha_end": artifact.get("head_sha_end"),
        "lane": lane,
        "verdict": artifact.get("verdict"),
        "created_at": created_at,
        "source_run_id": artifact.get("source_run_id"),
        "source_run_attempt": artifact.get("source_run_attempt"),
    }
    text = canonical(value)
    validate(text, digest(text), now=now)
    require(
        artifact.get("trailer") == trailer(value) and trailer(value) in body,
        "local verdict/trailer mismatch",
    )
    return value


def read_local(path):
    with Path(path).expanduser().open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    artifact = decode(raw.decode("utf-8"), 1024 * 1024)
    require(isinstance(artifact, dict), "invalid local artifact")
    return artifact


def unavailable_notice(path, lane):
    """Visible, metadata-only failure status, with no authority or audit trailer."""
    artifact = read_local(path)
    if not (
        artifact.get("quarantine_reason")
        or artifact.get("quarantined") is True
        or artifact.get("verdict") in ("UNKNOWN", "STALE")
    ):
        return None
    head = artifact.get("head_sha_start")
    head_line = f"Head SHA: `{head}`\n" if isinstance(head, str) and SHA.fullmatch(head) else ""
    return (
        f"## {lane.title()} audit unavailable\n\n{head_line}Verdict: UNKNOWN\n"
        "No merge-authority verdict was published. The local artifact was quarantined, "
        "stale, or inconclusive. Check the local runner and requeue this audit.\n"
    )


def stage(path, *, token, lane, env=None, io=None):
    """Save metadata locally; a later trusted step seals it before dispatch."""
    env = os.environ if env is None else env
    artifact = read_local(path)
    io = io or GitHub(artifact.get("repo"), token)
    repository = io.request("")
    require(
        env.get("GITHUB_EVENT_NAME") == "repository_dispatch"
        and env.get("GITHUB_WORKFLOW_REF")
        == f"{io.repo}/{SOURCE_WORKFLOW}@refs/heads/{repository['default_branch']}"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("CODE_MOWER_LOCAL_AUDIT_LANE") == lane
        and env.get("PR_HEAD_SHA") == artifact.get("head_sha_start"),
        "untrusted staging environment",
    )
    artifact.update(source_run_id=int(env["GITHUB_RUN_ID"]), source_run_attempt=1)
    value = project_local(artifact, repository, lane=lane, now=int(time.time()))
    current_pr(io, value)
    Path(path).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    Path(env["CODE_MOWER_AUDIT_STAGE_PATH"]).write_text(canonical(value), encoding="ascii")
    return {"body": "Local audit staged; awaiting trusted workflow publication.", "html_url": ""}


def submit(path, *, token, lane, timeout=900, io=None, clock=time.monotonic, sleep=time.sleep):
    artifact = read_local(path)
    io = io or GitHub(artifact.get("repo"), token)
    repository = io.request("")
    value = project_local(artifact, repository, lane=lane, now=int(time.time()))
    return submit_value(value, io=io, timeout=timeout, clock=clock, sleep=sleep)


def submit_value(value, *, io, timeout=900, clock=time.monotonic, sleep=time.sleep):
    repository = io.request("")
    validate(canonical(value), digest(canonical(value)), now=int(time.time()))
    verify_source(io, value, repository)
    current_pr(io, value)
    require(
        not any(
            reservation(value) in str(item.get("body", ""))
            and item.get("user", {}).get("login") == "github-actions[bot]"
            for item in io.pages(f"/issues/{value['pr_number']}/comments")
        ),
        "artifact already dispatched",
    )
    io.request(
        "/dispatches",
        method="POST",
        body={
            "event_type": EVENT,
            "client_payload": {
                "artifact": canonical(value),
                "digest": digest(canonical(value)),
                "pr_number": value["pr_number"],
            },
        },
    )
    deadline = clock() + timeout
    while clock() < deadline:
        current_pr(io, value)
        comments = io.pages(f"/issues/{value['pr_number']}/comments")
        matching = [
            item
            for item in comments
            if reservation(value) in str(item.get("body", ""))
            and item.get("user", {}).get("login") == "github-actions[bot]"
        ]
        require(len(matching) <= 1, "duplicate publication reservations")
        if matching:
            marker = re.search(
                r"CODE_MOWER_AUDIT_(?:RUN|PENDING): run_id=([0-9]+)", matching[0]["body"]
            )
            require(marker is not None, "reservation lacks publishing run")
            run = load_run(io, marker[1])
            if run.get("status") == "completed":
                verify_run(run, repository, terminal=True)
                comment = matching[0]
                require(
                    attested(
                        body=comment.get("body"),
                        comment_id=comment.get("id"),
                        repo=io.repo,
                        issue_number=value["pr_number"],
                        head_sha=value["head_sha_start"],
                        run=run,
                        repository=repository,
                    ),
                    "published comment binding failed",
                )
                current_pr(io, value)
                return comment
        sleep(5)
    raise Refused("publication timed out; inspect the workflow run before retrying")


def prepare_label_event(event, env, io, *, sleep=time.sleep):
    """Adapt a verified publication notification to the comment labeler.

    The publication workflow uses ``repository_dispatch`` for this notification
    because GitHub suppresses ``workflow_run`` chains after three levels.  The
    notification carries only a run id; the fetched terminal workflow, receipt,
    comment and current PR/head remain the authority.
    """
    repository = io.request("")
    payload = event.get("client_payload")
    require(
        event.get("action") == LABEL_EVENT
        and isinstance(payload, dict)
        and set(payload) == {"publication_run_id"}
        and positive(payload.get("publication_run_id")),
        "missing publication run",
    )
    run_id = payload["publication_run_id"]
    run = None
    for attempt in range(10):
        run = load_run(io, run_id)
        verify_run(run, repository, run_id=run_id)
        if run.get("status") == "completed":
            break
        if attempt < 9:
            sleep(1)
    assert run is not None
    verify_run(run, repository, run_id=run_id, terminal=True)
    proof = receipts(run)
    require(len(proof) == 1, "publication receipt missing or ambiguous")
    number = int(RECEIPT.fullmatch(proof[0]["name"])[1])
    comments = io.pages(f"/issues/{number}/comments")
    pr = io.request(f"/pulls/{number}")
    accepted = [
        item
        for item in comments
        if item.get("user", {}).get("login") == "github-actions[bot]"
        and attested(
            body=item.get("body"),
            comment_id=item.get("id"),
            repo=io.repo,
            issue_number=number,
            head_sha=pr.get("head", {}).get("sha"),
            run=run,
            repository=repository,
        )
    ]
    require(len(accepted) == 1, "publication comment not found at current head")
    value = metadata(accepted[0]["body"])
    current_pr(io, value)
    if value["lane"] != env["TRAILER_LANE"]:
        return None
    return {"action": "edited", "issue": {**pr, "pull_request": {}}, "comment": accepted[0]}


def prepare_review(event, env, io):
    repository = io.request("")
    default_ref = "refs/heads/" + repository["default_branch"]
    legacy = env.get("GITHUB_EVENT_NAME") == "pull_request_target"
    require(
        (
            (
                env.get("GITHUB_EVENT_NAME") == "repository_dispatch"
                and event.get("action") == "code-mower-local-review"
            )
            or (legacy and event.get("action") in ("opened", "synchronize", "labeled"))
        )
        and env.get("GITHUB_REF") == default_ref
        and env.get("GITHUB_WORKFLOW_REF") == f"{io.repo}/{SOURCE_WORKFLOW}@{default_ref}"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and event.get("repository", {}).get("id") == repository["id"],
        "untrusted review request",
    )
    payload = event.get("client_payload")
    if legacy:
        pr = event.get("pull_request", {})
        payload = {"pr_number": pr.get("number"), "head_sha": pr.get("head", {}).get("sha")}
    require(
        isinstance(payload, dict)
        and set(payload) == {"pr_number", "head_sha"}
        and positive(payload["pr_number"])
        and isinstance(payload["head_sha"], str)
        and SHA.fullmatch(payload["head_sha"]),
        "invalid review request",
    )
    pr = current_pr(
        io,
        dict(
            pr_number=payload["pr_number"],
            repository_id=repository["id"],
            head_sha_start=payload["head_sha"],
        ),
    )
    require(
        pr.get("head", {}).get("repo", {}).get("id") == repository["id"]
        and pr.get("base", {}).get("ref") == repository["default_branch"],
        "untrusted review target",
    )
    return payload


def main():
    try:
        env = os.environ
        io = GitHub(env["GITHUB_REPOSITORY"], env["GH_TOKEN"])
        event = None
        if sys.argv[1:] in (["publish"], ["prepare-label-event"], ["prepare-review"]):
            with Path(env["GITHUB_EVENT_PATH"]).open("rb") as stream:
                event = decode(stream.read(MAX_EVENT_BYTES + 1).decode("utf-8"), MAX_EVENT_BYTES)
        if sys.argv[1:] == ["prepare-review"]:
            requested = prepare_review(event, env, io)
            with Path(env["GITHUB_OUTPUT"]).open("a") as output:
                output.write(
                    f"ready=true\npr_number={requested['pr_number']}\nhead_sha={requested['head_sha']}\n"
                )
        elif sys.argv[1:] == ["publish"]:
            posted = publish(event, env, io)
            value = metadata(posted["body"])
            with Path(env["GITHUB_OUTPUT"]).open("a") as output:
                output.write(
                    f"pr_number={value['pr_number']}\ncomment_id={posted['id']}\ndigest={digest(canonical(value))}\n"
                )
        elif sys.argv[1:] == ["prepare-label-event"]:
            require(
                env.get("GITHUB_EVENT_NAME") == "repository_dispatch",
                "wrong reconciliation event",
            )
            prepared = prepare_label_event(event, env, io)
            if prepared is not None:
                path = Path(env["RUNNER_TEMP"]) / "local-audit-publication-event.json"
                path.write_text(canonical(prepared), encoding="utf-8")
                with Path(env["GITHUB_OUTPUT"]).open("a") as output:
                    output.write(
                        f"event_path={path}\npr_number={prepared['issue']['number']}\nready=true\n"
                    )
        elif sys.argv[1:] in (["prepare-seal"], ["submit-staged"]):
            with Path(env["CODE_MOWER_AUDIT_STAGE_PATH"]).open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1).decode("ascii")
            value = validate(raw, digest(raw), now=int(time.time()))
            require(
                str(value["source_run_id"]) == env["GITHUB_RUN_ID"]
                and str(value["source_run_attempt"]) == env["GITHUB_RUN_ATTEMPT"]
                and value["lane"] == env["CODE_MOWER_LOCAL_AUDIT_LANE"]
                and value["head_sha_start"] == env["PR_HEAD_SHA"]
                and str(value["pr_number"]) == env["PR_NUMBER"],
                "wrong staging binding",
            )
            current_pr(io, value)
            if sys.argv[1:] == ["prepare-seal"]:
                with Path(env["GITHUB_OUTPUT"]).open("a") as output:
                    output.write(f"digest={digest(raw)}\n")
            else:
                submit_value(value, io=io)
        else:
            raise Refused("unsupported publication command")
        return 0
    except Exception as error:
        # Only a catalog code reaches stderr, never exception text or submitted data.
        print(f"Local audit publication refused [{refusal_code(error)}].", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
