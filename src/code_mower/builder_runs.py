"""Source-free builder run provenance for authoring-side Code Mower loops."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from code_mower import __version__
from code_mower.work_orders import parse_github_issue_ref, parse_github_pr_ref


BENCHMARK_EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
DEFAULT_BUILDER_RUN_DIR = Path(".code-mower/builder-runs")
SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
CURSOR_AGENT_URL_RE = re.compile(
    r"https?://(?:www\.)?cursor\.com/(?:agents|background-agent)/[^\s<>)\"']+",
    re.IGNORECASE,
)
CURSOR_AGENT_FOOTER_RE = re.compile(
    r"\b(?:cursor\s+(?:background\s+)?agent|view\s+cursor\s+agent\s+run|"
    r"(?:generated|authored|opened)\s+by\s+cursor)\b",
    re.IGNORECASE,
)
AUTHOR_INFERENCE_RULES = {
    "chatgpt-codex-connector": ("codex", "chatgpt-codex-connector"),
    "chatgpt-codex-connector[bot]": ("codex", "chatgpt-codex-connector"),
    "openai-codex[bot]": ("codex", "chatgpt-codex-connector"),
    "codex[bot]": ("codex", "chatgpt-codex-connector"),
    "claude[bot]": ("claude", "claude_code_action"),
    "claude-bot": ("claude", "claude_code_action"),
    "cursor[bot]": ("cursor_cloud_agent", "cursor_cloud_agent"),
    "cursor": ("cursor_cloud_agent", "cursor_cloud_agent"),
}
BRANCH_PREFIX_INFERENCE_RULES = (
    ("cursor/", "cursor_cloud_agent", "cursor_cloud_agent"),
    ("cursor-", "cursor_cloud_agent", "cursor_cloud_agent"),
    ("codex/", "codex", "chatgpt-codex-connector"),
    ("codex-", "codex", "chatgpt-codex-connector"),
    ("chatgpt-codex/", "codex", "chatgpt-codex-connector"),
    ("chatgpt-codex-", "codex", "chatgpt-codex-connector"),
    ("claude/", "claude", "claude_code_action"),
    ("claude-", "claude", "claude_code_action"),
)


@dataclass(frozen=True)
class PullRequestMetadata:
    repo: str
    number: str
    url: str
    author: str
    branch: str
    body: str


@dataclass(frozen=True)
class BuilderInference:
    provider: str
    executor: str
    builder_id: str
    run_url: str
    confidence: str
    signals: tuple[str, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_slug(value: Any, fallback: str = "builder-run") -> str:
    text = SAFE_SLUG_RE.sub("-", _text(value)).strip("._-").lower()
    while ".." in text:
        text = text.replace("..", ".")
    return text or fallback


def _optional_nonnegative_float(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero")
    return value


def _optional_nonnegative_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero")
    return value


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _nested_text(payload: Mapping[str, Any], *path: str) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    return _text(current)


def _repo_from_pr_payload(
    payload: Mapping[str, Any],
    pr: Mapping[str, Any],
    repo: str,
) -> str:
    return (
        _text(repo)
        or _nested_text(payload, "repository", "full_name")
        or _nested_text(pr, "base", "repo", "full_name")
        or _nested_text(pr, "head", "repo", "full_name")
    )


def _author_from_pr_payload(pr: Mapping[str, Any]) -> str:
    return (
        _nested_text(pr, "user", "login")
        or _nested_text(pr, "author", "login")
        or _nested_text(pr, "author", "name")
        or _nested_text(pr, "author", "email")
    )


def _branch_from_pr_payload(pr: Mapping[str, Any]) -> str:
    return (
        _nested_text(pr, "head", "ref")
        or _text(pr.get("headRefName"))
        or _text(pr.get("head_ref"))
        or _text(pr.get("branch"))
    )


def load_pull_request_metadata(path: Path, *, repo: str = "") -> PullRequestMetadata:
    payload = _load_json_object(path)
    pr = _record(payload.get("pull_request")) or payload
    number = _text(pr.get("number")) or _text(payload.get("number"))
    url = _text(pr.get("html_url")) or _text(pr.get("url"))
    return PullRequestMetadata(
        repo=_repo_from_pr_payload(payload, pr, repo),
        number=number,
        url=url,
        author=_author_from_pr_payload(pr),
        branch=_branch_from_pr_payload(pr),
        body=_text(pr.get("body")),
    )


def _cursor_agent_url(body: str) -> str:
    match = CURSOR_AGENT_URL_RE.search(body)
    return match.group(0).rstrip(".,") if match else ""


def _builder_id_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return ""
    return path.split("/")[-1]


def infer_builder_from_pr(metadata: PullRequestMetadata) -> BuilderInference | None:
    signals: list[str] = []
    candidate: tuple[str, str, str] | None = None

    author = metadata.author.lower()
    if author in AUTHOR_INFERENCE_RULES:
        provider, executor = AUTHOR_INFERENCE_RULES[author]
        candidate = (provider, executor, "high")
        signals.append(f"author:{metadata.author}")

    cursor_url = _cursor_agent_url(metadata.body)
    if cursor_url:
        signals.append("cursor_agent_url")
        if candidate is None:
            candidate = ("cursor_cloud_agent", "cursor_cloud_agent", "high")
    elif CURSOR_AGENT_FOOTER_RE.search(metadata.body):
        signals.append("cursor_agent_footer")
        if candidate is None:
            candidate = ("cursor_cloud_agent", "cursor_cloud_agent", "medium")

    branch = metadata.branch.lower()
    for prefix, provider, executor in BRANCH_PREFIX_INFERENCE_RULES:
        if branch.startswith(prefix):
            signals.append(f"branch_prefix:{prefix}")
            if candidate is None:
                candidate = (provider, executor, "medium")
            break

    if candidate is None:
        return None

    provider, executor, confidence = candidate
    provider_run_url = cursor_url if provider == "cursor_cloud_agent" else ""
    builder_id = _builder_id_from_url(provider_run_url) or "-".join(
        part
        for part in (
            _safe_slug(provider, ""),
            _safe_slug(metadata.repo, ""),
            f"pr-{_safe_slug(metadata.number, '')}" if metadata.number else "",
        )
        if part
    )
    return BuilderInference(
        provider=provider,
        executor=executor,
        builder_id=builder_id,
        run_url=provider_run_url,
        confidence=confidence,
        signals=tuple(dict.fromkeys(signals)),
    )


def build_auto_builder_run_event(
    metadata: PullRequestMetadata,
    *,
    created_at: str = "",
    lens: str = "implementation",
    status: str = "pr-opened",
) -> tuple[dict[str, Any] | None, BuilderInference | None]:
    inference = infer_builder_from_pr(metadata)
    if inference is None:
        return None, None
    pr_ref = metadata.url or (
        f"{metadata.repo}#{metadata.number}" if metadata.repo and metadata.number else ""
    )
    event = build_builder_run_event(
        provider=inference.provider,
        executor=inference.executor,
        pr=pr_ref,
        repo=metadata.repo,
        branch=metadata.branch,
        builder_id=inference.builder_id,
        run_url=inference.run_url,
        status=status,
        lens=lens,
        integration="hosted_async_builder",
        created_at=created_at,
    )
    event["source"] = "code-mower-builder-auto-record"
    event["tool"]["source"] = "code-mower-builder-auto-record"
    event["dimensions"]["auto_inferred"] = True
    event["dimensions"]["builder_inference_confidence"] = inference.confidence
    event["dimensions"]["builder_inference_signals"] = list(inference.signals)
    event["dimensions"]["pr_author"] = metadata.author
    return event, inference


def _work_order_manifest_path(work_order: Path) -> Path:
    return work_order.with_suffix(".json")


def _load_work_order_manifest(work_order: Path | None) -> dict[str, Any]:
    if work_order is None:
        return {}
    if not work_order.is_file():
        raise ValueError(f"--work-order must be an existing file: {work_order}")
    manifest_path = _work_order_manifest_path(work_order)
    if not manifest_path.is_file():
        return {}
    manifest = _load_json_object(manifest_path)
    if _text(manifest.get("schema")) != "code_mower.workOrder.v1":
        return {}
    return manifest


def _github_url(repo_slug: str, *, path: str, number: str) -> str:
    parts = [part for part in repo_slug.strip().split("/") if part]
    if len(parts) == 2:
        host = "github.com"
        repo_path = "/".join(parts)
    elif len(parts) == 3 and "." in parts[0]:
        host = parts[0]
        repo_path = "/".join(parts[1:])
    else:
        return ""
    return f"https://{host}/{repo_path}/{path}/{number}"


def _pr_url_from_ref(pr_ref: str, *, repo: str) -> tuple[str, str, str]:
    if not pr_ref:
        return "", "", ""
    pr_repo, pr_number = parse_github_pr_ref(pr_ref, repo=repo)
    raw = pr_ref.strip()
    pr_url = (
        raw
        if raw.startswith(("http://", "https://"))
        else _github_url(pr_repo, path="pull", number=pr_number)
    )
    return pr_repo, pr_number, pr_url


def _issue_url_from_ref(issue_ref: str, *, repo: str) -> tuple[str, str, str]:
    if not issue_ref:
        return "", "", ""
    issue_repo, issue_number = parse_github_issue_ref(issue_ref, repo=repo)
    raw = issue_ref.strip()
    issue_url = (
        raw
        if raw.startswith(("http://", "https://"))
        else _github_url(issue_repo, path="issues", number=issue_number)
    )
    return issue_repo, issue_number, issue_url


def _builder_run_event_id(
    *,
    provider: str,
    executor: str,
    run_identity: str,
    repo_slug: str,
    issue_number: str,
    issue_url: str,
    pr_number: str,
    pr_url: str,
    work_order_file: str,
    branch: str,
) -> str:
    seed = "|".join(
        [
            "code-mower-builder-run",
            provider,
            executor,
            run_identity,
            repo_slug,
            issue_number,
            issue_url,
            pr_number,
            pr_url,
            work_order_file,
            branch,
        ]
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _default_output_path(
    *,
    provider: str,
    executor: str,
    run_suffix: str,
    issue_number: str,
    pr_number: str,
    work_order: Path | None,
    branch: str,
) -> Path:
    anchor = (
        f"pr-{pr_number}"
        if pr_number
        else f"issue-{issue_number}"
        if issue_number
        else work_order.stem
        if work_order is not None
        else branch
    )
    filename = "-".join(
        filter(
            None,
            (
                _safe_slug(provider),
                _safe_slug(executor, ""),
                _safe_slug(anchor),
                _safe_slug(run_suffix, ""),
            ),
        )
    )
    return DEFAULT_BUILDER_RUN_DIR / f"{filename}.cloud-event.json"


def _validate_anchor_repositories(repositories: Mapping[str, str]) -> None:
    present = {name: repo for name, repo in repositories.items() if repo}
    unique_repos = sorted(set(present.values()))
    if len(unique_repos) <= 1:
        return
    details = ", ".join(f"{name}={repo}" for name, repo in sorted(present.items()))
    raise ValueError(f"repository mismatch between provenance anchors: {details}")


def build_builder_run_event(
    *,
    provider: str,
    executor: str = "",
    issue: str = "",
    pr: str = "",
    repo: str = "",
    work_order: Path | None = None,
    branch: str = "",
    builder_id: str = "",
    run_url: str = "",
    status: str = "",
    lens: str = "implementation",
    model: str = "",
    model_source: str = "",
    tool_version: str = "",
    version_source: str = "",
    integration: str = "hosted_async_builder",
    created_at: str = "",
    elapsed_seconds: float | None = None,
    cost_usd: float | None = None,
    user_interventions: int | None = None,
) -> dict[str, Any]:
    """Build a metadata-only event describing who authored a PR from a work order."""

    provider = _text(provider)
    executor = _text(executor)
    repo = _text(repo)
    branch = _text(branch)
    if not provider:
        raise ValueError("--provider is required")

    work_order_manifest = _load_work_order_manifest(work_order)
    manifest_repo = _text(work_order_manifest.get("repo"))
    source = work_order_manifest.get("source")
    source = source if isinstance(source, Mapping) else {}

    issue_text = _text(issue)
    issue_repo = _text(source.get("repo"))
    issue_number = _text(source.get("issue_number"))
    issue_url = _text(source.get("issue_url"))
    if issue_text:
        issue_repo, issue_number, issue_url = _issue_url_from_ref(
            issue_text,
            repo=repo or manifest_repo,
        )

    pr_repo = ""
    pr_number = ""
    pr_url = ""
    pr_text = _text(pr)
    if pr_text:
        pr_repo, pr_number, pr_url = _pr_url_from_ref(
            pr_text,
            repo=repo or issue_repo or manifest_repo,
        )

    _validate_anchor_repositories(
        {
            "repo": repo,
            "issue": issue_repo,
            "pull_request": pr_repo,
            "work_order": manifest_repo,
        }
    )

    effective_repo = pr_repo or issue_repo or repo or manifest_repo
    if not any((issue_url, issue_number, pr_url, pr_number, work_order)):
        raise ValueError("record at least one of --issue, --pr, or --work-order")
    if not effective_repo:
        raise ValueError(
            "record repository identity via --repo, --issue, --pr, or a work-order manifest"
        )

    clean_elapsed = _optional_nonnegative_float(elapsed_seconds, "--elapsed-seconds")
    clean_cost = _optional_nonnegative_float(cost_usd, "--cost-usd")
    clean_interventions = _optional_nonnegative_int(user_interventions, "--user-interventions")
    requested_status = _text(status)
    if requested_status == "pr-opened" and not (pr_url or pr_number):
        raise ValueError("--status pr-opened requires --pr")
    default_status = "pr-opened" if (pr_url or pr_number) else "observed"
    builder_id = _text(builder_id)
    run_url = _text(run_url)
    run_identity = builder_id or run_url or str(uuid.uuid4())
    created_at_value = _text(created_at) or _utc_now()

    work_order_file = work_order.name if work_order is not None else ""
    event = {
        "schema": BENCHMARK_EVENT_SCHEMA,
        "event_id": _builder_run_event_id(
            provider=provider,
            executor=executor,
            run_identity=run_identity,
            repo_slug=effective_repo,
            issue_number=issue_number,
            issue_url=issue_url,
            pr_number=pr_number,
            pr_url=pr_url,
            work_order_file=work_order_file,
            branch=branch,
        ),
        "event_type": "builder_run",
        "created_at": created_at_value,
        "repo_slug": effective_repo,
        "team_id": "",
        "install_id": "",
        "source": "code-mower-builder-record",
        "provider": provider,
        "lens": _text(lens),
        "status": requested_status or default_status,
        "tool": {
            "role": "builder",
            "tool_name": provider,
            "tool_version": _text(tool_version),
            "provider": provider,
            "model": _text(model),
            "model_source": _text(model_source) or ("manual" if _text(model) else "missing"),
            "version_source": _text(version_source)
            or ("manual" if _text(tool_version) else "missing"),
            "integration": _text(integration) or "hosted_async_builder",
            "lens": _text(lens),
            "source": "code-mower-builder-record",
            "executor": executor,
            "code_mower_version": __version__,
        },
        "metrics": {},
        "dimensions": {
            "builder_provider": provider,
            "builder_executor": executor,
            "builder_id": builder_id,
            "issue_repo": issue_repo,
            "issue_number": issue_number,
            "issue_url": issue_url,
            "work_order_file": work_order_file,
            "work_order_manifest_file": _work_order_manifest_path(work_order).name
            if work_order is not None and _work_order_manifest_path(work_order).is_file()
            else "",
            "pr_repo": pr_repo,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "branch": branch,
            "builder_run_url": run_url,
            "review_policy": (
                "builder provenance is authoring evidence; Code Mower reviewer "
                "lanes still run after the PR exists"
            ),
        },
    }

    metrics = event["metrics"]
    if clean_elapsed is not None:
        metrics["elapsed_seconds"] = clean_elapsed
    if clean_cost is not None:
        metrics["cost_usd"] = clean_cost
    if clean_interventions is not None:
        metrics["user_interventions"] = clean_interventions
    return event


def write_builder_run_event(
    event: Mapping[str, Any],
    output: Path,
    *,
    force: bool = False,
) -> Path:
    if output.exists() and not force:
        raise ValueError(f"{output} already exists; pass --force to overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(event), allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower builder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser(
        "record",
        help="Write a metadata-only builder_run event after an agent opens a PR.",
    )
    record.add_argument("--provider", required=True, help="Builder/orchestrator identity, e.g. grok_bot.")
    record.add_argument("--executor", default="", help="Execution surface, e.g. cursor_cloud_agent.")
    record.add_argument("--issue", default="", help="GitHub issue ref or URL.")
    record.add_argument("--pr", default="", help="GitHub PR ref or URL.")
    record.add_argument("--repo", default="", help="owner/repo for numeric refs.")
    record.add_argument("--work-order", type=Path)
    record.add_argument("--branch", default="")
    record.add_argument("--builder-id", default="", help="Safe display id for the builder run.")
    record.add_argument("--run-url", default="", help="Safe hosted run URL, if available.")
    record.add_argument("--status", default="")
    record.add_argument("--lens", default="implementation")
    record.add_argument("--model", default="")
    record.add_argument("--model-source", default="")
    record.add_argument("--tool-version", default="")
    record.add_argument("--version-source", default="")
    record.add_argument("--integration", default="hosted_async_builder")
    record.add_argument("--created-at", default="")
    record.add_argument("--elapsed-seconds", type=float)
    record.add_argument("--cost-usd", type=float)
    record.add_argument("--user-interventions", type=int)
    record.add_argument("--output", type=Path)
    record.add_argument("--force", action="store_true")
    record.add_argument("--json", action="store_true")

    auto_record = subparsers.add_parser(
        "auto-record",
        help="Infer builder provenance from pull_request metadata and write a builder_run event.",
    )
    auto_record.add_argument(
        "--pr-json",
        type=Path,
        required=True,
        help="GitHub pull_request event JSON or `gh pr view --json ...` output.",
    )
    auto_record.add_argument("--repo", default="", help="owner/repo fallback for PR metadata.")
    auto_record.add_argument("--status", default="pr-opened")
    auto_record.add_argument("--lens", default="implementation")
    auto_record.add_argument("--created-at", default="")
    auto_record.add_argument("--output", type=Path)
    auto_record.add_argument("--force", action="store_true")
    auto_record.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "record":
            event = build_builder_run_event(
                provider=args.provider,
                executor=args.executor,
                issue=args.issue,
                pr=args.pr,
                repo=args.repo,
                work_order=args.work_order,
                branch=args.branch,
                builder_id=args.builder_id,
                run_url=args.run_url,
                status=args.status,
                lens=args.lens,
                model=args.model,
                model_source=args.model_source,
                tool_version=args.tool_version,
                version_source=args.version_source,
                integration=args.integration,
                created_at=args.created_at,
                elapsed_seconds=args.elapsed_seconds,
                cost_usd=args.cost_usd,
                user_interventions=args.user_interventions,
            )
            output = args.output or _default_output_path(
                provider=args.provider,
                executor=args.executor,
                run_suffix=args.builder_id or event["event_id"][:12],
                issue_number=event["dimensions"]["issue_number"],
                pr_number=event["dimensions"]["pr_number"],
                work_order=args.work_order,
                branch=args.branch,
            )
            output_path = write_builder_run_event(event, output, force=args.force)
            payload = {
                "mode": "builder-record",
                "event_path": str(output_path),
                "event_type": event["event_type"],
                "provider": event["provider"],
                "executor": event["dimensions"]["builder_executor"],
                "repo_slug": event["repo_slug"],
                "pr_url": event["dimensions"]["pr_url"],
                "issue_url": event["dimensions"]["issue_url"],
                "status": event["status"],
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("Code Mower builder run metadata")
                print(f"Event: {output_path}")
                print(f"Provider: {event['provider']}")
                if event["dimensions"]["builder_executor"]:
                    print(f"Executor: {event['dimensions']['builder_executor']}")
                if event["dimensions"]["pr_url"]:
                    print(f"PR: {event['dimensions']['pr_url']}")
                if event["dimensions"]["issue_url"]:
                    print(f"Issue: {event['dimensions']['issue_url']}")
            return 0
        if args.command == "auto-record":
            metadata = load_pull_request_metadata(args.pr_json, repo=args.repo)
            event, inference = build_auto_builder_run_event(
                metadata,
                created_at=args.created_at,
                lens=args.lens,
                status=args.status,
            )
            if event is None or inference is None:
                payload = {
                    "mode": "builder-auto-record",
                    "status": "skipped",
                    "reason": "no_supported_builder_markers",
                    "repo_slug": metadata.repo,
                    "pr_number": metadata.number,
                    "branch": metadata.branch,
                }
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print("No supported builder markers found; skipped builder_run sidecar.")
                return 0
            output = args.output or _default_output_path(
                provider=inference.provider,
                executor=inference.executor,
                run_suffix="auto",
                issue_number="",
                pr_number=event["dimensions"]["pr_number"],
                work_order=None,
                branch=metadata.branch,
            )
            output_path = write_builder_run_event(event, output, force=args.force)
            payload = {
                "mode": "builder-auto-record",
                "status": "recorded",
                "event_path": str(output_path),
                "event_type": event["event_type"],
                "provider": event["provider"],
                "executor": event["dimensions"]["builder_executor"],
                "repo_slug": event["repo_slug"],
                "pr_url": event["dimensions"]["pr_url"],
                "branch": event["dimensions"]["branch"],
                "confidence": inference.confidence,
                "signals": list(inference.signals),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("Code Mower builder run metadata")
                print(f"Event: {output_path}")
                print(f"Provider: {event['provider']}")
                print(f"Executor: {event['dimensions']['builder_executor']}")
                print(f"PR: {event['dimensions']['pr_url']}")
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled builder command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
