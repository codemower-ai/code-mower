#!/usr/bin/env python3
"""Release campaign orchestrator for multi-provider release qualification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import lane_status
    from code_mower.provider_registry import REFERENCE_PROVIDERS, ProviderLane
    from code_mower.release_qualify import (
        _detect_host_class,
        _detect_runtime_class,
        _extract_package_identity,
        _validate_qualification_context,
        _validate_safe_identifier,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        run_release_qualification,
    )
else:
    from . import lane_status
    from .provider_registry import REFERENCE_PROVIDERS, ProviderLane
    from .release_qualify import (
        _detect_host_class,
        _detect_runtime_class,
        _extract_package_identity,
        _validate_qualification_context,
        _validate_safe_identifier,
        _validate_starting_version,
        _validate_tag_format,
        _version_key,
        run_release_qualification,
    )

CAMPAIGN_SCHEMA = "code_mower.releaseCampaign.v1"
DISPATCH_SCHEMA = "code_mower.releaseCampaignDispatch.v1"
BOARD_RELEASE_CAMPAIGNS_SCHEMA = "code_mower.boardReleaseCampaigns.v1"
DEFAULT_CAMPAIGNS_RELATIVE_DIR = Path(".code-mower") / "campaigns"

DEFAULT_CAMPAIGN_PROVIDERS = (
    "claude",
    "codex",
    "antigravity",
    "muse",
    "cursor_bugbot",
    "devin",
)

PROVIDER_ALIAS_MAP: dict[str, str] = {
    "claude": "claude_audit",
    "claude_code": "claude_audit",
    "claude_audit": "claude_audit",
    "claude_review": "claude_review",
    "codex": "codex",
    "antigravity": "antigravity_cli",
    "agy": "antigravity_cli",
    "antigravity_cli": "antigravity_cli",
    "muse": "muse_cli",
    "muse_cli": "muse_cli",
    "cursor": "cursor_bugbot",
    "cursor_bugbot": "cursor_bugbot",
    "cursor_grok_bot": "cursor_bugbot",
    "cursor_cloud_agent": "cursor_bugbot",
    "grok_bot": "cursor_bugbot",
    "grok": "grok_build",
    "grok_build": "grok_build",
    "devin": "devin",
}

VALID_PROVIDER_STATES = {
    "queued",
    "running",
    "blocked",
    "unavailable",
    "complete",
}

DISPATCH_MARKER_RE = re.compile(
    r"<!--\s*CODE_MOWER_RELEASE_CAMPAIGN:\s*(\{.*?\})\s*-->",
    re.DOTALL,
)

ADOPTION_RESULT_MARKER_RE = re.compile(
    r"<!--\s*CODE_MOWER_ADOPTION_RESULT:\s*(\{.*?\})\s*-->",
    re.DOTALL,
)


@dataclass
class CampaignProvider:
    """Metadata-only state for one campaign provider participant."""

    provider: str
    lane_id: str
    driver: str
    state: str
    environment: str
    elapsed_seconds: float
    idempotency_key: str
    dispatch_mode: str = "dry_run"
    dispatched_at: str | None = None
    completed_at: str | None = None
    next_action: str = ""
    next_detail: str = ""
    error: str = ""
    dispatch_ref: dict[str, Any] = field(default_factory=dict)
    adoption_result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in VALID_PROVIDER_STATES:
            self.state = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReleaseCampaign:
    """Versioned metadata-only release qualification campaign."""

    schema: str
    campaign_id: str
    release_tag: str
    package_identity: str
    package_spec: str
    normalized_version: str
    qualification_context: str
    starting_version: str
    repo_slug: str
    status: str
    dry_run: bool
    elapsed_seconds: float
    created_at: str
    updated_at: str
    next_action: str
    next_detail: str
    providers: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_campaigns_dir(repo_path: Path | str = ".") -> Path:
    return Path(repo_path) / DEFAULT_CAMPAIGNS_RELATIVE_DIR


def _safe_campaign_filename(campaign_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", campaign_id)
    return f"{cleaned}.json"


def resolve_provider_lane(name: str) -> tuple[str, ProviderLane]:
    """Resolve a provider alias to its canonical key and declarative lane configuration."""
    normalized = name.strip().lower()
    target_lane_id = PROVIDER_ALIAS_MAP.get(normalized, normalized)
    if target_lane_id in REFERENCE_PROVIDERS:
        lane = REFERENCE_PROVIDERS[target_lane_id]
        return lane.provider, lane

    from .provider_registry import LaneLabels

    fallback_lane = ProviderLane(
        lane_id=normalized,
        lane_type="audit",
        driver="manual",
        provider=normalized,
        labels=LaneLabels(
            needs=f"needs-{normalized}-audit",
            done=f"{normalized}-audit-done",
            blocked=f"{normalized}-audit-blocked",
        ),
        trigger_policy="manual",
    )
    return normalized, fallback_lane


def _compute_idempotency_key(
    campaign_id: str,
    provider: str,
    release_tag: str,
    qualification_context: str,
) -> str:
    seed = f"{campaign_id}:{provider}:{release_tag}:{qualification_context}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:16]


def _detect_environment() -> str:
    host_class = _detect_host_class()
    runtime_class = _detect_runtime_class()
    return f"{host_class}/{runtime_class}"


def _find_command(
    lane: ProviderLane,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> str | None:
    config = lane.provider_config
    cmd = config.get("command") or lane.provider
    if which_fn(cmd):
        return cmd
    for alt in config.get("alternate_commands", ()):
        if which_fn(alt):
            return alt
    return None


def _check_credentials(
    lane: ProviderLane,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    current_env = os.environ if env is None else env
    if lane.token_env:
        found = any(current_env.get(token) for token in lane.token_env)
        if not found:
            return False, lane.token_env[0]
    required_any = lane.provider_config.get("required_env_any", ())
    if required_any and not any(current_env.get(var) for var in required_any):
        return False, required_any[0]
    return True, ""


def _provider_next_action(
    provider: str,
    lane: ProviderLane,
    state: str,
    *,
    command_available: bool,
    has_credentials: bool,
    has_issue: bool,
    dry_run: bool,
    error: str = "",
) -> tuple[str, str]:
    if state == "complete":
        return "none", ""
    if state == "blocked":
        return f"inspect {provider} qualification failures", error
    if state == "running":
        if lane.driver in {"saas_event", "hosted_bridge"}:
            return f"poll {provider} remote progress marker", ""
        return f"poll {provider} local process", ""
    if state == "unavailable":
        if lane.driver == "local_cli" and not command_available:
            cmd = lane.provider_config.get("command") or provider
            return f"install {cmd} CLI on PATH or record manual result", error or f"command not found: {cmd}"
        if lane.driver in {"hosted_bridge", "saas_event"} and not has_credentials:
            token = error or (lane.token_env[0] if lane.token_env else "credentials")
            return f"set {token} or record manual result", error
        if lane.driver in {"saas_event", "hosted_bridge"} and not has_issue:
            return f"provide GitHub issue number via --issue for {provider} dispatch", error
        return f"configure {provider} prerequisites or record manual result", error
    if state == "queued":
        if dry_run:
            if lane.driver == "local_cli":
                return f"run with --apply to execute {provider} qualification", ""
            if lane.driver == "saas_event":
                return f"run with --apply to dispatch {provider} via GitHub comment", ""
            if lane.driver == "hosted_bridge":
                return f"run with --apply to dispatch paid remote {provider} qualification", ""
            return f"run with --apply to execute {provider}", ""
        return f"dispatch {provider}", ""
    return f"inspect {provider}", error


def _aggregate_campaign_status(
    providers: Sequence[dict[str, Any]],
    *,
    dry_run: bool,
) -> tuple[str, str, str]:
    if not providers:
        return "queued", "add providers to campaign", ""

    blocked = [p["provider"] for p in providers if p.get("state") == "blocked"]
    running = [p["provider"] for p in providers if p.get("state") == "running"]
    queued = [p["provider"] for p in providers if p.get("state") == "queued"]
    unavailable = [p["provider"] for p in providers if p.get("state") == "unavailable"]
    complete = [p["provider"] for p in providers if p.get("state") == "complete"]

    if blocked:
        return (
            "blocked",
            f"inspect qualification failures for {', '.join(blocked)}",
            f"{len(blocked)} provider(s) failed qualification checks",
        )
    if running:
        return (
            "running",
            f"poll running providers: {', '.join(running)}",
            f"{len(running)} provider(s) currently running",
        )
    if len(complete) == len(providers):
        return (
            "complete",
            "campaign complete; all providers passed",
            f"all {len(complete)} provider(s) qualified successfully",
        )
    if dry_run:
        return (
            "queued",
            "run with --apply to dispatch providers",
            f"dry-run preview with {len(queued)} queued and {len(unavailable)} unavailable provider(s)",
        )
    if queued:
        return (
            "queued",
            f"dispatch queued providers: {', '.join(queued)}",
            f"{len(queued)} provider(s) waiting for dispatch",
        )
    if len(unavailable) == len(providers) - len(complete):
        return (
            "unavailable",
            f"configure prerequisites for unavailable providers: {', '.join(unavailable)}",
            f"{len(unavailable)} provider(s) unavailable",
        )
    return "queued", "inspect campaign providers", ""


def save_campaign(
    campaign: ReleaseCampaign | dict[str, Any],
    campaigns_dir: Path,
) -> Path:
    campaigns_dir.mkdir(parents=True, exist_ok=True)
    payload = campaign.to_dict() if isinstance(campaign, ReleaseCampaign) else campaign
    filename = _safe_campaign_filename(payload["campaign_id"])
    target_path = campaigns_dir / filename
    temp_target = campaigns_dir / f".tmp.{filename}"
    with temp_target.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    temp_target.replace(target_path)
    return target_path


def load_campaign(
    campaign_id_or_tag: str,
    campaigns_dir: Path,
) -> dict[str, Any] | None:
    if not campaigns_dir.is_dir():
        return None

    safe_name = _safe_campaign_filename(campaign_id_or_tag)
    direct_path = campaigns_dir / safe_name
    if direct_path.is_file():
        try:
            with direct_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and data.get("schema") == CAMPAIGN_SCHEMA:
                    return data
        except (OSError, json.JSONDecodeError):
            pass

    for entry in campaigns_dir.glob("*.json"):
        if entry.name.startswith(".tmp."):
            continue
        try:
            with entry.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and data.get("schema") == CAMPAIGN_SCHEMA:
                    if (
                        data.get("campaign_id") == campaign_id_or_tag
                        or data.get("release_tag") == campaign_id_or_tag
                    ):
                        return data
        except (OSError, json.JSONDecodeError):
            continue
    return None


def list_campaigns(campaigns_dir: Path) -> list[dict[str, Any]]:
    if not campaigns_dir.is_dir():
        return []
    campaigns: list[dict[str, Any]] = []
    for entry in campaigns_dir.glob("*.json"):
        if entry.name.startswith(".tmp."):
            continue
        try:
            with entry.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and data.get("schema") == CAMPAIGN_SCHEMA:
                    campaigns.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    campaigns.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
    return campaigns


def _extract_adoption_result_from_text(
    text: str,
    *,
    release_tag: str,
    provider: str,
) -> dict[str, Any] | None:
    candidates: list[str] = []
    for match in ADOPTION_RESULT_MARKER_RE.finditer(text):
        candidates.append(match.group(1))

    code_block_matches = re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for match in code_block_matches:
        if "adoptionResult.v1" in match.group(1):
            candidates.append(match.group(1))

    for raw in candidates:
        try:
            parsed = json.loads(raw)
            if (
                isinstance(parsed, dict)
                and parsed.get("schema") == "code_mower.adoptionResult.v1"
                and parsed.get("release_tag") == release_tag
                and parsed.get("provider") == provider
            ):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _dispatch_github_comment(
    repo_slug: str,
    issue_number: int | str,
    campaign_id: str,
    release_tag: str,
    package_spec: str,
    provider: str,
    qualification_context: str,
    idempotency_key: str,
    *,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> tuple[bool, dict[str, Any], str]:
    if not repo_slug or not issue_number:
        return False, {}, "missing repo_slug or issue_number"

    dispatch_marker = {
        "schema": DISPATCH_SCHEMA,
        "campaign_id": campaign_id,
        "release_tag": release_tag,
        "package_spec": package_spec,
        "provider": provider,
        "qualification_context": qualification_context,
        "idempotency_key": idempotency_key,
    }
    marker_str = json.dumps(dispatch_marker, sort_keys=True)
    body = (
        f"### Code Mower Release Qualification Dispatch\n\n"
        f"- **Release Tag:** `{release_tag}`\n"
        f"- **Package Spec:** `{package_spec}`\n"
        f"- **Provider:** `{provider}`\n"
        f"- **Context:** `{qualification_context}`\n"
        f"- **Idempotency Key:** `{idempotency_key}`\n\n"
        f"<!-- CODE_MOWER_RELEASE_CAMPAIGN: {marker_str} -->\n"
    )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as fh:
        body_path = Path(fh.name)
        fh.write(body)

    try:
        completed = command_runner(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo_slug,
                "--body-file",
                str(body_path),
            ]
        )
    except (OSError, ValueError) as exc:
        return False, {}, f"gh issue comment error: {exc}"
    finally:
        try:
            body_path.unlink()
        except OSError:
            pass

    returncode = getattr(completed, "returncode", 1)
    if returncode != 0:
        detail = getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or str(returncode)
        return False, {}, f"gh issue comment failed: {detail.strip()}"

    return True, {"issue_number": str(issue_number), "comment_posted": True}, ""


def _poll_github_comments(
    repo_slug: str,
    issue_number: int | str,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
) -> tuple[list[dict[str, Any]], str]:
    if not repo_slug or not issue_number:
        return [], "missing repo_slug or issue_number"
    data, error = gh_json_runner(
        ["issue", "view", str(issue_number), "--repo", repo_slug, "--json", "comments"]
    )
    if error:
        return [], error
    if not isinstance(data, dict):
        return [], "malformed gh issue response"
    comments = data.get("comments")
    if not isinstance(comments, list):
        return [], "comments field not a list"
    return [c for c in comments if isinstance(c, dict)], ""


def initialize_campaign(
    *,
    release_tag: str,
    package_spec: str = "",
    qualification_context: str = "cold_install",
    starting_version: str = "",
    providers: Sequence[str] = (),
    repo_slug: str = "",
    campaign_id: str = "",
) -> ReleaseCampaign:
    valid, normalized_version, error = _validate_tag_format(release_tag)
    if not valid:
        raise ValueError(error)

    if not package_spec:
        package_spec = f"code-mower=={normalized_version}"

    package_identity = _extract_package_identity(package_spec)
    spec_match = re.match(r"^[\w-]+==(.+)$", package_spec)
    if not spec_match or spec_match.group(1) != normalized_version:
        raise ValueError(f"Version mismatch: tag {normalized_version} vs spec {package_spec}")

    _validate_qualification_context(qualification_context)
    _validate_starting_version(starting_version)
    if qualification_context == "upgrade" and not starting_version:
        raise ValueError("starting_version is required for upgrade qualification")
    if qualification_context != "upgrade" and starting_version:
        raise ValueError("starting_version is only valid for upgrade qualification")
    if (
        qualification_context == "upgrade"
        and _version_key(starting_version) >= _version_key(normalized_version)
    ):
        raise ValueError("starting_version must be lower than the target version")

    if not campaign_id:
        campaign_id = f"campaign-{release_tag}"
    _validate_safe_identifier(re.sub(r"[-.]", "_", campaign_id), "campaign_id")

    provider_keys = list(providers) if providers else list(DEFAULT_CAMPAIGN_PROVIDERS)
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    environment = _detect_environment()

    campaign_providers: list[dict[str, Any]] = []
    for p_name in provider_keys:
        canonical_name, lane = resolve_provider_lane(p_name)
        idemp_key = _compute_idempotency_key(campaign_id, canonical_name, release_tag, qualification_context)
        cp = CampaignProvider(
            provider=canonical_name,
            lane_id=lane.lane_id,
            driver=lane.driver,
            state="queued",
            environment=environment,
            elapsed_seconds=0.0,
            idempotency_key=idemp_key,
            dispatch_mode="dry_run",
            next_action=f"run with --apply to execute {canonical_name}",
            next_detail="",
        )
        campaign_providers.append(cp.to_dict())

    overall_status, next_action, next_detail = _aggregate_campaign_status(
        campaign_providers,
        dry_run=True,
    )

    return ReleaseCampaign(
        schema=CAMPAIGN_SCHEMA,
        campaign_id=campaign_id,
        release_tag=release_tag,
        package_identity=package_identity,
        package_spec=package_spec,
        normalized_version=normalized_version,
        qualification_context=qualification_context,
        starting_version=starting_version,
        repo_slug=repo_slug,
        status=overall_status,
        dry_run=True,
        elapsed_seconds=0.0,
        created_at=now_utc,
        updated_at=now_utc,
        next_action=next_action,
        next_detail=next_detail,
        providers=campaign_providers,
    )


def dispatch_or_advance_campaign(
    campaign: dict[str, Any],
    *,
    apply: bool = False,
    issue_number: str | int = "",
    repo_path: Path | None = None,
    campaigns_dir: Path | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    run_qualification_fn: Any = run_release_qualification,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute dispatch, polling, or status progression on a campaign."""
    current_env = os.environ if env is None else env
    repo_path = repo_path or Path.cwd()
    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path)
    results_dir = campaigns_dir / "results"
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    campaign["dry_run"] = not apply
    campaign_id = str(campaign.get("campaign_id") or "")
    release_tag = str(campaign.get("release_tag") or "")
    package_spec = str(campaign.get("package_spec") or "")
    context = str(campaign.get("qualification_context") or "cold_install")
    starting_version = str(campaign.get("starting_version") or "")
    repo_slug = str(campaign.get("repo_slug") or "")

    for provider_data in campaign.get("providers", []):
        provider = str(provider_data.get("provider") or "")
        _, lane = resolve_provider_lane(provider)
        current_state = str(provider_data.get("state") or "queued")

        # 1. Check local result drop-in first (manual override or completed run)
        local_result_file = results_dir / f"{campaign_id}_{provider}.json"
        if local_result_file.is_file():
            try:
                with local_result_file.open("r", encoding="utf-8") as fh:
                    adoption_res = json.load(fh)
                if (
                    isinstance(adoption_res, dict)
                    and adoption_res.get("schema") == "code_mower.adoptionResult.v1"
                    and adoption_res.get("release_tag") == release_tag
                ):
                    provider_data["adoption_result"] = adoption_res
                    provider_data["elapsed_seconds"] = float(adoption_res.get("elapsed_seconds") or 0.0)
                    outcome = adoption_res.get("outcome")
                    if outcome in {"pass", "pass_with_warnings"}:
                        provider_data["state"] = "complete"
                        provider_data["next_action"] = "none"
                        provider_data["next_detail"] = ""
                    else:
                        provider_data["state"] = "blocked"
                        provider_data["next_action"] = f"inspect {provider} qualification failures"
                        provider_data["next_detail"] = f"outcome: {outcome}"
                    provider_data["completed_at"] = now_utc
                    continue
            except (OSError, json.JSONDecodeError):
                pass

        # 2. If already complete, preserve state
        if current_state == "complete":
            provider_data["next_action"] = "none"
            continue

        # 3. If running, poll for progress
        if current_state == "running":
            dispatch_ref = provider_data.get("dispatch_ref", {})
            ref_issue = dispatch_ref.get("issue_number") or issue_number
            if ref_issue and repo_slug:
                comments, error = _poll_github_comments(
                    repo_slug,
                    ref_issue,
                    gh_json_runner=gh_json_runner,
                )
                if error:
                    provider_data["next_detail"] = f"GitHub poll notice: {error}"
                else:
                    for comment in comments:
                        body = str(comment.get("body") or "")
                        found_result = _extract_adoption_result_from_text(
                            body,
                            release_tag=release_tag,
                            provider=provider,
                        )
                        if found_result:
                            provider_data["adoption_result"] = found_result
                            provider_data["elapsed_seconds"] = float(
                                found_result.get("elapsed_seconds") or 0.0
                            )
                            provider_data["completed_at"] = now_utc
                            outcome = found_result.get("outcome")
                            if outcome in {"pass", "pass_with_warnings"}:
                                provider_data["state"] = "complete"
                                provider_data["next_action"] = "none"
                            else:
                                provider_data["state"] = "blocked"
                                provider_data["next_action"] = (
                                    f"inspect {provider} qualification failures"
                                )
                            break
            continue

        # 4. Check capabilities and readiness
        cmd_found = _find_command(lane, which_fn=which_fn)
        has_creds, missing_cred = _check_credentials(lane, env=current_env)
        has_issue = bool(issue_number)

        # 5. Dry-run evaluations
        if not apply:
            provider_data["dispatch_mode"] = "dry_run"
            if lane.driver == "local_cli" and not cmd_found:
                provider_data["state"] = "unavailable"
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=False,
                    has_credentials=True,
                    has_issue=True,
                    dry_run=True,
                    error=f"command not found: {lane.provider_config.get('command') or provider}",
                )
            elif lane.driver in {"hosted_bridge", "saas_event"} and not has_creds:
                provider_data["state"] = "unavailable"
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=False,
                    has_issue=has_issue,
                    dry_run=True,
                    error=missing_cred,
                )
            else:
                provider_data["state"] = "queued"
                action, detail = _provider_next_action(
                    provider,
                    lane,
                    "queued",
                    command_available=bool(cmd_found),
                    has_credentials=has_creds,
                    has_issue=has_issue,
                    dry_run=True,
                )
            provider_data["next_action"] = action
            provider_data["next_detail"] = detail
            continue

        # 6. Applied dispatch (requires explicit apply flag)
        provider_data["dispatch_mode"] = "applied"
        if lane.driver == "local_cli":
            if not cmd_found:
                provider_data["state"] = "unavailable"
                provider_data["error"] = f"command not found: {lane.provider_config.get('command') or provider}"
                provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=False,
                    has_credentials=True,
                    has_issue=True,
                    dry_run=False,
                    error=provider_data["error"],
                )
            else:
                start_p = time.time()
                try:
                    results_dir.mkdir(parents=True, exist_ok=True)
                    result_path = results_dir / f"{campaign_id}_{provider}.json"
                    result_dict = run_qualification_fn(
                        release_tag=release_tag,
                        package_spec=package_spec,
                        output_path=result_path,
                        repo_path=repo_path,
                        repo_slug=repo_slug,
                        dry_run=False,
                        qualification_context=context,
                        starting_version=starting_version,
                        provider=provider,
                        executor=f"{provider}_cli",
                    )
                    provider_data["adoption_result"] = result_dict
                    provider_data["dispatched_at"] = now_utc
                    provider_data["completed_at"] = now_utc
                    provider_data["elapsed_seconds"] = round(time.time() - start_p, 2)
                    outcome = result_dict.get("outcome")
                    if outcome in {"pass", "pass_with_warnings"}:
                        provider_data["state"] = "complete"
                        provider_data["next_action"] = "none"
                        provider_data["next_detail"] = ""
                    else:
                        provider_data["state"] = "blocked"
                        provider_data["next_action"] = f"inspect {provider} qualification failures"
                        provider_data["next_detail"] = f"outcome: {outcome}"
                except Exception as exc:
                    provider_data["state"] = "blocked"
                    provider_data["error"] = str(exc)
                    provider_data["next_action"] = f"inspect {provider} qualification error"
                    provider_data["next_detail"] = str(exc)

        elif lane.driver in {"saas_event", "hosted_bridge"}:
            if not has_creds:
                provider_data["state"] = "unavailable"
                provider_data["error"] = f"missing token: {missing_cred}"
                provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=False,
                    has_issue=has_issue,
                    dry_run=False,
                    error=missing_cred,
                )
            elif not issue_number or not repo_slug:
                provider_data["state"] = "unavailable"
                provider_data["error"] = "missing issue number or repository slug"
                provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                    provider,
                    lane,
                    "unavailable",
                    command_available=True,
                    has_credentials=True,
                    has_issue=False,
                    dry_run=False,
                    error="missing issue number",
                )
            else:
                ok, ref, err = _dispatch_github_comment(
                    repo_slug,
                    issue_number,
                    campaign_id,
                    release_tag,
                    package_spec,
                    provider,
                    context,
                    provider_data["idempotency_key"],
                    command_runner=command_runner,
                )
                if ok:
                    provider_data["state"] = "running"
                    provider_data["dispatched_at"] = now_utc
                    provider_data["dispatch_ref"] = ref
                    provider_data["next_action"], provider_data["next_detail"] = _provider_next_action(
                        provider,
                        lane,
                        "running",
                        command_available=True,
                        has_credentials=True,
                        has_issue=True,
                        dry_run=False,
                    )
                else:
                    provider_data["state"] = "unavailable"
                    provider_data["error"] = err
                    provider_data["next_action"] = f"retry {provider} dispatch when GitHub is available"
                    provider_data["next_detail"] = err
        else:
            provider_data["state"] = "unavailable"
            provider_data["next_action"] = f"record manual adoption result for {provider}"
            provider_data["next_detail"] = "manual adapter fallback"

    overall_status, next_action, next_detail = _aggregate_campaign_status(
        campaign.get("providers", []),
        dry_run=campaign.get("dry_run", True),
    )
    campaign["status"] = overall_status
    campaign["next_action"] = next_action
    campaign["next_detail"] = next_detail
    campaign["updated_at"] = now_utc

    total_elapsed = sum(
        float(p.get("elapsed_seconds") or 0.0)
        for p in campaign.get("providers", [])
    )
    campaign["elapsed_seconds"] = round(total_elapsed, 2)

    save_campaign(campaign, campaigns_dir)
    return campaign


def record_manual_result(
    campaign: dict[str, Any],
    provider: str,
    result_path_or_dict: Path | dict[str, Any],
    *,
    campaigns_dir: Path | None = None,
    repo_path: Path | None = None,
) -> dict[str, Any]:
    if isinstance(result_path_or_dict, Path):
        with result_path_or_dict.open("r", encoding="utf-8") as fh:
            adoption_res = json.load(fh)
    else:
        adoption_res = result_path_or_dict

    if not isinstance(adoption_res, dict) or adoption_res.get("schema") != "code_mower.adoptionResult.v1":
        raise ValueError("Invalid adoption result schema")

    release_tag = campaign.get("release_tag")
    if adoption_res.get("release_tag") != release_tag:
        raise ValueError(f"Adoption result tag {adoption_res.get('release_tag')} does not match campaign {release_tag}")

    canonical_provider, _ = resolve_provider_lane(provider)
    matched = False
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for p in campaign.get("providers", []):
        if p.get("provider") == canonical_provider:
            p["adoption_result"] = adoption_res
            p["elapsed_seconds"] = float(adoption_res.get("elapsed_seconds") or 0.0)
            outcome = adoption_res.get("outcome")
            if outcome in {"pass", "pass_with_warnings"}:
                p["state"] = "complete"
                p["next_action"] = "none"
                p["next_detail"] = ""
            else:
                p["state"] = "blocked"
                p["next_action"] = f"inspect {canonical_provider} qualification failures"
                p["next_detail"] = f"outcome: {outcome}"
            p["completed_at"] = now_utc
            matched = True
            break

    if not matched:
        raise ValueError(f"Provider {provider} not found in campaign")

    overall_status, next_action, next_detail = _aggregate_campaign_status(
        campaign.get("providers", []),
        dry_run=campaign.get("dry_run", True),
    )
    campaign["status"] = overall_status
    campaign["next_action"] = next_action
    campaign["next_detail"] = next_detail
    campaign["updated_at"] = now_utc

    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path or Path.cwd())
    save_campaign(campaign, campaigns_dir)
    return campaign


def render_campaign_text(campaign: Mapping[str, Any]) -> str:
    lines = [
        f"Release Campaign: {campaign.get('release_tag')} ({campaign.get('qualification_context')})",
        f"Status: {campaign.get('status')} ({'dry-run' if campaign.get('dry_run') else 'applied'})",
        f"Next: {campaign.get('next_action')}",
    ]
    if campaign.get("next_detail"):
        lines.append(f"Detail: {campaign.get('next_detail')}")
    lines.append("Providers:")
    for p in campaign.get("providers", []):
        state = p.get("state")
        env = p.get("environment")
        elapsed = p.get("elapsed_seconds") or 0.0
        action = p.get("next_action") or "none"
        detail = f" ({p.get('next_detail')})" if p.get("next_detail") else ""
        lines.append(f"- {p.get('provider')}: {state} ({env}, elapsed {elapsed:.1f}s) -> {action}{detail}")
    return "\n".join(lines)


def release_campaigns_board_payload(
    repo_path: Path | str = ".",
    *,
    campaigns_dir: Path | None = None,
    show_local_paths: bool = False,
) -> dict[str, Any]:
    dir_path = campaigns_dir or default_campaigns_dir(repo_path)
    if not dir_path.is_dir():
        return {
            "schema": BOARD_RELEASE_CAMPAIGNS_SCHEMA,
            "available": True,
            "campaigns": [],
            "card_count": 0,
            "next_action": "no active campaigns",
            "next_detail": "run code-mower release campaign --release-tag <tag> to start one",
        }

    campaigns = list_campaigns(dir_path)
    projected_campaigns: list[dict[str, Any]] = []
    total_cards = 0

    for c in campaigns:
        cards: list[dict[str, Any]] = []
        for p in c.get("providers", []):
            cards.append(
                {
                    "release": c.get("release_tag", ""),
                    "provider": p.get("provider", ""),
                    "lane_id": p.get("lane_id", p.get("provider", "")),
                    "environment": p.get("environment", "local"),
                    "state": p.get("state", "queued"),
                    "elapsed_seconds": float(p.get("elapsed_seconds") or 0.0),
                    "next_action": str(p.get("next_action") or ""),
                    "next_detail": str(p.get("next_detail") or ""),
                }
            )
        total_cards += len(cards)
        projected_campaigns.append(
            {
                "campaign_id": c.get("campaign_id", ""),
                "release_tag": c.get("release_tag", ""),
                "package_spec": c.get("package_spec", ""),
                "qualification_context": c.get("qualification_context", ""),
                "status": c.get("status", "queued"),
                "dry_run": bool(c.get("dry_run", True)),
                "elapsed_seconds": float(c.get("elapsed_seconds") or 0.0),
                "next_action": str(c.get("next_action") or ""),
                "next_detail": str(c.get("next_detail") or ""),
                "cards": cards,
            }
        )

    overall_action = (
        projected_campaigns[0]["next_action"]
        if projected_campaigns
        else "no active campaigns"
    )
    overall_detail = (
        projected_campaigns[0]["next_detail"]
        if projected_campaigns
        else ""
    )

    return {
        "schema": BOARD_RELEASE_CAMPAIGNS_SCHEMA,
        "available": True,
        "campaigns": projected_campaigns,
        "card_count": total_cards,
        "next_action": overall_action,
        "next_detail": overall_detail,
    }


def campaign_command(
    *,
    action: str | None = None,
    release_tag: str = "",
    package_spec: str = "",
    providers: Sequence[str] = (),
    qualification_context: str = "cold_install",
    starting_version: str = "",
    repo_path: Path | None = None,
    repo_slug: str = "",
    issue: str | int = "",
    apply: bool = False,
    resume: bool = False,
    status: bool = False,
    campaign_id: str = "",
    campaigns_dir: Path | None = None,
    record_result: Path | None = None,
    record_provider: str = "",
    emit_json: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_qualification_fn: Any = run_release_qualification,
    env: Mapping[str, str] | None = None,
) -> int:
    repo_path = repo_path or Path.cwd()
    campaigns_dir = campaigns_dir or default_campaigns_dir(repo_path)

    is_status = status or action == "status"
    is_resume = resume or action == "resume"

    identifier = campaign_id or release_tag or (f"campaign-{release_tag}" if release_tag else "")
    existing = load_campaign(identifier, campaigns_dir) if identifier else None

    if record_result:
        if not existing:
            print("error: cannot record result without existing campaign", file=sys.stderr)
            return 1
        if not record_provider:
            print("error: --record-provider required when recording result", file=sys.stderr)
            return 1
        try:
            updated = record_manual_result(
                existing,
                record_provider,
                record_result,
                campaigns_dir=campaigns_dir,
                repo_path=repo_path,
            )
            if emit_json:
                print(json.dumps(updated, indent=2, sort_keys=True))
            else:
                print(render_campaign_text(updated))
            return 0 if updated.get("status") != "blocked" else 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if is_status:
        if not existing:
            all_c = list_campaigns(campaigns_dir)
            if not all_c:
                print("error: no campaigns found", file=sys.stderr)
                return 1
            existing = all_c[0]
        if emit_json:
            print(json.dumps(existing, indent=2, sort_keys=True))
        else:
            print(render_campaign_text(existing))
        return 0

    if existing and (is_resume or not release_tag):
        updated = dispatch_or_advance_campaign(
            existing,
            apply=apply,
            issue_number=issue,
            repo_path=repo_path,
            campaigns_dir=campaigns_dir,
            which_fn=which_fn,
            command_runner=command_runner,
            gh_json_runner=gh_json_runner,
            run_qualification_fn=run_qualification_fn,
            env=env,
        )
        if emit_json:
            print(json.dumps(updated, indent=2, sort_keys=True))
        else:
            print(render_campaign_text(updated))
        return 0 if updated.get("status") != "blocked" else 1

    if not release_tag:
        print("error: --release-tag is required to create a campaign", file=sys.stderr)
        return 1

    try:
        campaign_obj = initialize_campaign(
            release_tag=release_tag,
            package_spec=package_spec,
            qualification_context=qualification_context,
            starting_version=starting_version,
            providers=providers,
            repo_slug=repo_slug,
            campaign_id=campaign_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    campaign_dict = campaign_obj.to_dict()
    updated = dispatch_or_advance_campaign(
        campaign_dict,
        apply=apply,
        issue_number=issue,
        repo_path=repo_path,
        campaigns_dir=campaigns_dir,
        which_fn=which_fn,
        command_runner=command_runner,
        gh_json_runner=gh_json_runner,
        run_qualification_fn=run_qualification_fn,
        env=env,
    )

    if emit_json:
        print(json.dumps(updated, indent=2, sort_keys=True))
    else:
        print(render_campaign_text(updated))
    return 0 if updated.get("status") != "blocked" else 1
