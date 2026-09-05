"""User-level release-campaign discovery across checkouts of one repository.

Campaign files stay in repo-local ``.code-mower/campaigns`` (or an explicit
``--campaigns-dir``). A metadata-only index records those directories under a
stable repository identity so status, watch, upload, and Board can find the
same campaign set from another worktree without copying or merging files.

The index may store repository identity, campaign ids, timestamps, and the
directory path needed to reopen storage. Paths never appear in command output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cloud_client.git_metadata import detect_repo_slug, run_git
from .file_locks import FileLockError, exclusive_file_lock

DISCOVERY_SCHEMA = "code_mower.campaignDiscoveryIndex.v1"
DISCOVERY_RELATIVE_DIR = Path("campaign-discovery")
DEFAULT_STATE_DIR = Path("~/.cache/code-mower")
MAX_DISCOVERY_DIRECTORIES = 16
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INDEX_LOCK_NAME = ".discovery.lock"
INDEX_TEMP_PREFIX = ".tmp."


def default_discovery_dir() -> Path:
    raw = os.environ.get("CODE_MOWER_STATE_DIR", "").strip()
    root = Path(raw).expanduser() if raw else DEFAULT_STATE_DIR.expanduser()
    return root / DISCOVERY_RELATIVE_DIR


def normalize_repo_identity(value: str) -> str:
    slug = value.strip().strip("/")
    return slug if REPO_SLUG_RE.fullmatch(slug) else ""


def resolve_repo_identity(repo_path: Path | str = ".", repo_slug: str = "") -> str:
    slug = normalize_repo_identity(repo_slug)
    if slug:
        return slug
    detected = detect_repo_slug(Path(repo_path))
    if detected:
        return detected
    raw = run_git(Path(repo_path), ["rev-parse", "--git-common-dir"])
    if not raw:
        return ""
    common = Path(raw) if Path(raw).is_absolute() else Path(repo_path) / raw
    try:
        digest = hashlib.sha256(str(common.resolve()).encode("utf-8")).hexdigest()[:16]
    except OSError:
        return ""
    return f"git:{digest}"


def _index_path(repo_identity: str, *, discovery_dir: Path | None = None) -> Path:
    digest = hashlib.sha256(repo_identity.encode("utf-8")).hexdigest()[:32]
    return (discovery_dir or default_discovery_dir()) / f"{digest}.json"


def _resolved_dir(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _campaign_ids(campaigns_dir: Path) -> list[str]:
    from .release_campaigns import is_valid_campaign_id, list_campaigns

    ids = [
        str(campaign.get("campaign_id") or "")
        for campaign in list_campaigns(campaigns_dir)
        if is_valid_campaign_id(campaign.get("campaign_id"))
    ]
    return sorted(set(ids))


def _read_index(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != DISCOVERY_SCHEMA:
        return {}
    return payload


def _registered_directories(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("directories")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        ids = item.get("campaign_ids")
        campaign_ids = [
            value
            for value in ids
            if isinstance(ids, list) and isinstance(value, str)
        ] if isinstance(ids, list) else []
        updated = item.get("updated_at")
        entries.append(
            {
                "path": path,
                "campaign_ids": campaign_ids,
                "updated_at": updated if isinstance(updated, str) else "",
            }
        )
    return entries


def discover_campaign_directories(
    repo_identity: str,
    *,
    local_dir: Path | None = None,
    discovery_dir: Path | None = None,
) -> list[Path]:
    """Return unique existing campaign directories for one repository identity."""
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = _resolved_dir(path)
        if resolved is None or resolved in seen:
            return
        seen.add(resolved)
        found.append(resolved)

    if local_dir is not None:
        add(local_dir)
    if not repo_identity:
        return found
    payload = _read_index(_index_path(repo_identity, discovery_dir=discovery_dir))
    for entry in _registered_directories(payload):
        add(Path(entry["path"]))
    return found


def publish_campaigns_directory(
    campaigns_dir: Path,
    repo_identity: str,
    *,
    discovery_dir: Path | None = None,
) -> None:
    """Record a campaigns directory in the user-level index. Best-effort."""
    if not repo_identity:
        return
    resolved = _resolved_dir(campaigns_dir)
    if resolved is None:
        return
    campaign_ids = _campaign_ids(resolved)
    if not campaign_ids:
        return
    index_dir = discovery_dir or default_discovery_dir()
    index_path = _index_path(repo_identity, discovery_dir=index_dir)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(index_dir / INDEX_LOCK_NAME):
            payload = _read_index(index_path)
            entries = [
                entry
                for entry in _registered_directories(payload)
                if _resolved_dir(Path(entry["path"])) != resolved
            ]
            entries.append(
                {
                    "path": str(resolved),
                    "campaign_ids": campaign_ids,
                    "updated_at": now,
                }
            )
            entries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            payload = {
                "schema": DISCOVERY_SCHEMA,
                "repo_identity": repo_identity,
                "updated_at": now,
                "directories": entries[:MAX_DISCOVERY_DIRECTORIES],
            }
            temp_path = index_dir / f"{INDEX_TEMP_PREFIX}{index_path.name}"
            try:
                with temp_path.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                os.replace(temp_path, index_path)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
    except (OSError, FileLockError, TypeError, ValueError):
        return


def _ambiguous_tag_error(release_tag: str, matches: Sequence[Mapping[str, Any]]) -> str:
    from .release_campaigns import AMBIGUOUS_RELEASE_TAG_ID_LIMIT, is_valid_campaign_id

    named = sorted(
        str(campaign.get("campaign_id"))
        for campaign in matches
        if is_valid_campaign_id(campaign.get("campaign_id"))
    )
    listed = ", ".join(named[:AMBIGUOUS_RELEASE_TAG_ID_LIMIT])
    if not listed:
        detail = ""
    elif len(named) > AMBIGUOUS_RELEASE_TAG_ID_LIMIT:
        detail = f" ({listed}, ...)"
    else:
        detail = f" ({listed})"
    return (
        f"release tag {release_tag!r} matches {len(matches)} campaigns{detail}; "
        "name the one you mean with --campaign-id"
    )


def _ambiguous_id_error(campaign_id: str) -> str:
    return (
        f"campaign id {campaign_id!r} is stored in more than one campaign directory; "
        "pass --campaigns-dir to select one"
    )


def _scan_campaigns(
    directories: Sequence[Path],
) -> list[tuple[dict[str, Any], Path]]:
    from .release_campaigns import is_valid_campaign_id, list_campaigns

    found: list[tuple[dict[str, Any], Path]] = []
    seen_dirs: set[Path] = set()
    for directory in directories:
        resolved = _resolved_dir(Path(directory))
        if resolved is None or resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)
        for campaign in list_campaigns(resolved):
            if is_valid_campaign_id(campaign.get("campaign_id")):
                found.append((campaign, resolved))
    return found


def iter_discovered_campaigns(
    directories: Sequence[Path],
) -> tuple[list[tuple[dict[str, Any], Path]], tuple[str, ...]]:
    """Load campaigns from each directory without rewriting files.

    Returns unique ``(campaign, source_dir)`` pairs and colliding campaign ids.
    Colliding ids are omitted rather than merged.
    """
    by_id: dict[str, tuple[dict[str, Any], Path]] = {}
    collisions: set[str] = set()
    for campaign, source in _scan_campaigns(directories):
        campaign_id = str(campaign.get("campaign_id") or "")
        if campaign_id in collisions:
            continue
        if campaign_id in by_id:
            collisions.add(campaign_id)
            by_id.pop(campaign_id, None)
            continue
        by_id[campaign_id] = (campaign, source)
    unique = sorted(
        by_id.values(),
        key=lambda item: str(item[0].get("updated_at") or ""),
        reverse=True,
    )
    return unique, tuple(sorted(collisions))


def list_discovered_campaigns(directories: Sequence[Path]) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    unique, collisions = iter_discovered_campaigns(directories)
    return [campaign for campaign, _source in unique], collisions


def resolve_command_campaigns_dir(
    *,
    write_dir: Path,
    repo_identity: str,
    campaign_id: str = "",
    release_tag: str = "",
    select_newest: bool = False,
    discovery_dir: Path | None = None,
) -> tuple[Path, str]:
    """Choose the campaigns directory for one CLI invocation.

    Explicit callers never reach here. Unqualified create/resume/upload keep
    ``write_dir``. Identifier lookups and newest-status/watch search discovered
    directories and fail closed on ambiguity without naming local paths.
    """
    directories = discover_campaign_directories(
        repo_identity,
        local_dir=write_dir,
        discovery_dir=discovery_dir,
    )
    scanned = _scan_campaigns(directories)
    unique, collisions = iter_discovered_campaigns(directories)
    if campaign_id:
        hits = [
            (campaign, source)
            for campaign, source in scanned
            if campaign.get("campaign_id") == campaign_id
        ]
        if len(hits) > 1:
            return write_dir, _ambiguous_id_error(campaign_id)
        if not hits:
            return write_dir, ""
        campaign, source = hits[0]
        if release_tag and str(campaign.get("release_tag") or "") != release_tag:
            return (
                write_dir,
                f"campaign {campaign_id!r} does not match --release-tag {release_tag!r}",
            )
        return source, ""
    if release_tag:
        hits = [
            (campaign, source)
            for campaign, source in scanned
            if campaign.get("release_tag") == release_tag
        ]
        if not hits:
            return write_dir, ""
        ids = {str(campaign.get("campaign_id") or "") for campaign, _source in hits}
        if len(hits) > 1 and len(ids) == 1:
            return write_dir, _ambiguous_id_error(next(iter(ids)))
        if len(ids) > 1:
            return write_dir, _ambiguous_tag_error(
                release_tag, [campaign for campaign, _source in hits]
            )
        return hits[0][1], ""
    if select_newest:
        if unique:
            return unique[0][1], ""
        if collisions:
            return write_dir, _ambiguous_id_error(collisions[0])
    return write_dir, ""
