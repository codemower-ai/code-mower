#!/usr/bin/env python3
"""Read-only Jira Cloud transport, credential path, and metadata helpers.

This module is the single bounded Jira Cloud client for issue #800. It is
read-only by construction: every primitive issues an HTTP GET, except the
JQL search and the bulk permission check, which use POST endpoints that only
read. There is no mutation/apply surface here. ``_check_request_allowed`` is
the single request-policy seam: the guarded plan/apply surface in
``jira_mutations`` subclasses this client to widen it to a closed write
allow-list, and only after both the repository write guard and the runtime
apply flag are present. Nothing may write through this class itself.

Transport rules:

- Scoped API tokens go through the Atlassian API gateway
  ``https://api.atlassian.com/ex/jira/{cloud_id}``. The configured HTTPS
  site URL is browse/display identity only and is never the REST gateway.
- Credentials resolve fail-closed with the same precedence as every other
  provider (see provider_credentials): ambient environment first, an
  explicit credential file or profile second, then exactly one secure
  discovered profile. Files broader than 0600 on POSIX are rejected.
- A profile may name a macOS Keychain generic-password service through
  ``JIRA_KEYCHAIN_SERVICE`` instead of storing the token on disk. The
  token is retrieved through the ``security`` CLI without ever appearing in
  argv, logs, exceptions, JSON diagnostics, Board data, or cloud data.
- All network access flows through an injected HTTP runner so tests perform
  no live Jira calls. Responses are size-bounded, retries use bounded
  exponential backoff plus jitter with injectable sleep/random helpers, and
  cancellation is cooperative through an injectable predicate.
  ``_attempts_for`` is the single retry-policy seam: reads keep the full
  budget, and a writing subclass narrows it for requests that cannot be
  safely repeated. ``_on_request_attempt`` is the transport-attempt seam,
  fired once per real HTTP attempt so a writing subclass can count every
  write attempt it made, including the ambiguous and retried ones.
- Failures map to closed reason codes; raw response bodies never enter
  diagnostics, and only bounded metadata fields are ever requested or
  returned (never summary, description, comments, attachments, issue body,
  source, diffs, prompts, transcripts, raw output, auth output, local
  paths, or secrets).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import random as _random
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .provider_credentials import (
    check_file_permissions,
    display_profile_path,
    effective_env_value,
    parse_env_file,
    resolve_provider_credentials,
    validate_jira_email,
)

API_GATEWAY = "https://api.atlassian.com"
SITE_EXAMPLE = "https://example.atlassian.net"

JIRA_EMAIL_ENV = "JIRA_API_EMAIL"
#: Backward-compatible alias for JIRA_API_EMAIL kept for long-standing
#: local setups. The primary name wins when both are set, and neither
#: value is ever printed, logged, or placed in diagnostics.
JIRA_ACCOUNT_EMAIL_ENV = "JIRA_ACCOUNT_EMAIL"
JIRA_TOKEN_ENV = "JIRA_API_TOKEN"
JIRA_KEYCHAIN_SERVICE_ENV = "JIRA_KEYCHAIN_SERVICE"

REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
RETRY_AFTER_CAP_SECONDS = 60
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_ISSUES = 200
#: Maximum enhanced-JQL page fetches per search call. This bound is
#: independent of the count of collected usable issues so that empty or
#: unusable pages with fresh continuation tokens cannot loop forever.
SEARCH_MAX_PAGES = 20
#: Create-metadata page size and maximum page fetches per discovery call.
CREATEMETA_PAGE_SIZE = 50
CREATEMETA_MAX_PAGES = 8
MAX_REQUIRED_CREATE_FIELDS = 64
KEYCHAIN_TIMEOUT_SECONDS = 10
MAX_METADATA_VALUE_LENGTH = 128
MAX_LABEL_LENGTH = 128

#: Jira documents 255 characters for both remote-link global ids and issue
#: property keys; Code Mower stays inside that bound and only builds tokens.
MAX_GLOBAL_ID_LENGTH = 255
MAX_PROPERTY_KEY_LENGTH = 255

_CLOUD_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
_PROJECT_ID_RE = re.compile(r"^[0-9]{1,32}$")
_TOKEN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_PROPERTY_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_GLOBAL_ID_RE = re.compile(r"^[A-Za-z0-9_.:/#-]{1,255}$")

#: Closed transport reason codes. A JiraApiError only ever carries one of
#: these -- never a response body, token, path, or exception message.
SAFE_ERROR_CODES = frozenset(
    {
        "jira_unauthorized",  # 401: expired, revoked, or invalid token
        "jira_forbidden",  # 403: authenticated but not permitted
        "jira_not_found",  # 404: wrong cloud id, project, or issue reference
        "jira_conflict",  # 409: workflow/state changed under the request
        "jira_rate_limited",  # 429 after bounded retries
        "jira_unavailable",  # transient 5xx, network, timeout, or oversize body
        "jira_rejected",  # other 4xx client errors
        "jira_cancelled",  # cooperative cancellation before/during retries
    }
)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: The only non-GET endpoints the read client may call. Both read despite
#: using POST; neither creates, updates, or deletes anything.
READ_ONLY_POST_PATHS = frozenset(
    {"/rest/api/3/search/jql", "/rest/api/3/permissions/check"}
)

#: The only JQL search fields this client ever requests: bounded metadata.
#: Status, issue type, labels, assignee presence, and timestamps. Search
#: requests for any other field are rejected before any network call.
SAFE_SEARCH_FIELDS = frozenset(
    {"status", "issuetype", "labels", "assignee", "created", "updated", "project"}
)

#: Permissions probed by the effective-permission check. Read-oriented first;
#: write-oriented entries report capability only and never authorize a write.
DEFAULT_PROBE_PERMISSIONS = (
    "BROWSE_PROJECTS",
    "CREATE_ISSUES",
    "EDIT_ISSUES",
    "TRANSITION_ISSUES",
    "ADD_COMMENTS",
)

JiraHttpRunner = Callable[
    [str, str, Mapping[str, str], bytes | None],
    tuple[int, Mapping[str, str], bytes],
]
JiraSleep = Callable[[float], None]
JiraRandom = Callable[[], float]
JiraCancelled = Callable[[], bool]
KeychainRunner = Callable[[Sequence[str], Mapping[str, str]], str]


class JiraApiError(Exception):
    """Jira transport failure carrying only a closed reason code."""

    def __init__(self, code: str, *, endpoint: str = "") -> None:
        self.code = code if code in SAFE_ERROR_CODES else "jira_unavailable"
        self.endpoint = endpoint
        super().__init__(self.code)


class KeychainError(Exception):
    """Safe Keychain failure; messages are fixed strings, never values."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class KeychainUnavailable(KeychainError):
    """The macOS Keychain tool is missing or unusable on this machine."""

    def __init__(self) -> None:
        super().__init__("macOS Keychain is unavailable")


class KeychainMissing(KeychainError):
    """The named Keychain entry could not be read."""

    def __init__(self) -> None:
        super().__init__("Keychain entry is missing or unreadable")


def gateway_base(cloud_id: str) -> str:
    """Return the scoped-token API gateway base for a cloud id."""
    if not _CLOUD_ID_RE.fullmatch(cloud_id):
        raise ValueError("cloud_id must be a bounded token of letters, digits, or hyphens")
    return f"{API_GATEWAY}/ex/jira/{cloud_id}"


def display_site_url(site_url: str) -> str:
    """Validate a browse/display site URL without using it as a gateway."""
    parsed = urllib.parse.urlparse(site_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("site_url must be an HTTPS Jira Cloud site URL")
    return site_url


def validate_issue_ref(issue_id_or_key: str) -> str:
    """Return one bounded issue id/key token, or raise ValueError.

    Issue references reach URL paths, so they stay a closed token shape:
    no slashes, query separators, whitespace, or path traversal.
    """
    ref = str(issue_id_or_key or "").strip()
    if not ref or len(ref) > 64 or not _TOKEN_ID_RE.fullmatch(ref):
        raise ValueError("issue reference must be a bounded id or key token")
    return ref


def validate_property_key(property_key: str) -> str:
    """Return one bounded issue-property key token, or raise ValueError."""
    key = str(property_key or "").strip()
    if not key or len(key) > MAX_PROPERTY_KEY_LENGTH or not _PROPERTY_KEY_RE.fullmatch(key):
        raise ValueError("property key must be a bounded token")
    return key


def validate_remote_link_global_id(global_id: str) -> str:
    """Return one bounded remote-link global id, or raise ValueError.

    Jira documents a 255-character maximum. Code Mower only ever builds its
    own deterministic ids, so the shape stays a closed token: a global id is
    the idempotency key that keeps replay from creating a second link.
    """
    value = str(global_id or "").strip()
    if not value or len(value) > MAX_GLOBAL_ID_LENGTH or not _GLOBAL_ID_RE.fullmatch(value):
        raise ValueError("remote link global id must be a bounded token")
    return value


def _bounded_str(value: Any, limit: int = MAX_METADATA_VALUE_LENGTH) -> str:
    text = str(value or "")
    if len(text) > limit:
        text = text[:limit]
    return text.replace("\n", " ").replace("\r", " ")


def _bounded_labels(value: Any) -> list[str]:
    names: list[str] = []
    items = value if isinstance(value, (list, tuple)) else []
    for item in items:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "")
        else:
            name = str(item or "")
        name = name.strip().replace("\n", " ").replace("\r", " ")
        if name:
            names.append(name[:MAX_LABEL_LENGTH])
    return sorted(set(names))[:64]


def default_keychain_runner(argv: Sequence[str], env: Mapping[str, str]) -> str:
    """Read one generic-password entry through the macOS ``security`` CLI.

    The token travels in process stdout only; argv carries the service and
    account names, never the token value.
    """
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            text=True,
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            env=dict(env),
        )
    except FileNotFoundError as exc:
        raise KeychainUnavailable() from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeychainMissing() from exc
    if completed.returncode != 0:
        raise KeychainMissing()
    return (completed.stdout or "").splitlines()[0] if completed.stdout else ""


def read_keychain_token(
    service: str,
    account: str,
    *,
    keychain_runner: KeychainRunner | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the token stored under a Keychain generic-password service.

    Raises KeychainUnavailable when the platform tool is missing and
    KeychainMissing when the entry cannot be read. Neither exception carries
    the service name, account, or token value.
    """
    service_name = service.strip()
    account_name = account.strip()
    if not service_name or len(service_name) > 128:
        raise KeychainMissing()
    if not account_name or not validate_jira_email(account_name):
        raise KeychainMissing()
    runner = default_keychain_runner if keychain_runner is None else keychain_runner
    argv = (
        "security",
        "find-generic-password",
        "-s",
        service_name,
        "-a",
        account_name,
        "-w",
    )
    current_env = os.environ if env is None else env
    try:
        token = runner(argv, dict(current_env))
    except KeychainError:
        raise
    except FileNotFoundError as exc:
        raise KeychainUnavailable() from exc
    except (OSError, ValueError) as exc:
        raise KeychainMissing() from exc
    token_value = str(token or "").strip()
    if not token_value or len(token_value) > 4096:
        raise KeychainMissing()
    return token_value


@dataclass(frozen=True)
class JiraCredentialResolution:
    """Bounded, persistence-safe result of Jira credential resolution."""

    status: str  # "ok", "missing", "ambiguous", "malformed", "insecure_permissions"
    source: str
    email: str = ""  # In-memory only, never serialized!
    token: str = ""  # In-memory only, never serialized!
    keychain_used: bool = False
    profile_file: Path | None = None
    candidate_files: tuple[str, ...] = ()  # Filenames only!
    message: str = ""
    remediation: str = ""
    missing_variables: tuple[str, ...] = ()

    @property
    def has_credentials(self) -> bool:
        return bool(self.status == "ok" and self.email and self.token)

    def safe_detail(self) -> dict[str, Any]:
        """Return safe diagnostic metadata without values or absolute paths."""
        detail: dict[str, Any] = {
            "provider": "jira",
            "source": self.source,
            "status": self.status,
        }
        if self.profile_file is not None:
            detail["profile_file"] = display_profile_path(self.profile_file)
        if self.candidate_files:
            detail["candidate_files"] = list(self.candidate_files)
        if self.missing_variables:
            detail["missing_variables"] = list(self.missing_variables)
        if self.keychain_used:
            detail["keychain"] = True
        return detail


def _map_base_resolution(
    base: Any,
    *,
    email: str = "",
    token: str = "",
    keychain_used: bool = False,
) -> JiraCredentialResolution:
    return JiraCredentialResolution(
        status=base.status,
        source=base.source,
        email=email,
        token=token,
        keychain_used=keychain_used,
        profile_file=base.profile_file,
        candidate_files=tuple(base.candidate_files),
        message=base.message,
        remediation=base.remediation,
        missing_variables=tuple(base.missing_variables),
    )


def _env_jira_email(env: Mapping[str, str]) -> str:
    """Return the valid account email from the environment, else "".

    Reads JIRA_API_EMAIL first with JIRA_ACCOUNT_EMAIL as a
    backward-compatible alias. Values are validated by shape only and are
    never logged or placed in diagnostics.
    """
    email = effective_env_value("jira", env, JIRA_EMAIL_ENV)
    return email if validate_jira_email(email) else ""


def _email_from_profile_file(path: Path) -> str:
    try:
        parsed = parse_env_file(path)
    except (ValueError, OSError):
        return ""
    email = effective_env_value("jira", parsed, JIRA_EMAIL_ENV)
    return email if validate_jira_email(email) else ""


def resolve_jira_credentials(
    *,
    credential_file: Path | None = None,
    profile: str = "",
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    keychain_runner: KeychainRunner | None = None,
) -> JiraCredentialResolution:
    """Resolve Jira account email and API token fail-closed.

    Precedence mirrors provider_credentials exactly: ambient environment,
    then an explicit credential file or profile, then exactly one secure
    discovered profile. When the token is absent but a Keychain service is
    named through ``JIRA_KEYCHAIN_SERVICE`` (environment or profile file),
    the token is completed from the macOS Keychain. Diagnostics carry
    filenames only, never values or absolute paths.
    """
    current_env = os.environ if env is None else env
    base = resolve_provider_credentials(
        "jira",
        credential_file=credential_file,
        profile=profile,
        config_dir=config_dir,
        env=current_env,
    )
    if base.status == "ok":
        email = str(base.credentials.get(JIRA_EMAIL_ENV) or "").strip()
        token = str(base.credentials.get(JIRA_TOKEN_ENV) or "").strip()
        if email and token and validate_jira_email(email):
            return _map_base_resolution(base, email=email, token=token)
        return _map_base_resolution(base)

    if base.status in ("ambiguous", "insecure_permissions"):
        # Never guess between profiles and never read past bad permissions,
        # even when a Keychain service is configured.
        return _map_base_resolution(base)

    # "missing" or "malformed": the token may still be completable from the
    # Keychain when a service is named and the email is known and valid.
    # Precedence mirrors ambient-first resolution: the environment wins for
    # each value independently, then the selected profile file completes
    # whichever value is still missing. In particular a selected secure
    # profile supplies the email even when JIRA_KEYCHAIN_SERVICE already
    # comes from the environment.
    service = str(current_env.get(JIRA_KEYCHAIN_SERVICE_ENV) or "").strip()
    email = _env_jira_email(current_env)
    if (not service or not email) and base.profile_file is not None and base.profile_file.is_file():
        if base.source in ("credential_file", "profile", "single_profile"):
            if check_file_permissions(base.profile_file):
                try:
                    parsed = parse_env_file(base.profile_file)
                except (ValueError, OSError):
                    parsed = {}
                if not service:
                    file_service = str(
                        parsed.get(JIRA_KEYCHAIN_SERVICE_ENV) or ""
                    ).strip()
                    if file_service and len(file_service) <= 128:
                        service = file_service
                if not email:
                    email = _email_from_profile_file(base.profile_file)

    if not service or not email:
        return _map_base_resolution(base)

    try:
        keychain_token = read_keychain_token(
            service, email, keychain_runner=keychain_runner, env=current_env
        )
    except KeychainUnavailable:
        missing = _map_base_resolution(base)
        return JiraCredentialResolution(
            status="missing",
            source=missing.source,
            profile_file=missing.profile_file,
            candidate_files=missing.candidate_files,
            missing_variables=(JIRA_TOKEN_ENV,),
            message="Jira API token is unavailable from the macOS Keychain on this machine",
            remediation=(
                f"Set {JIRA_TOKEN_ENV} in the environment or store the token "
                f"in {display_profile_path(Path('~/.config/code-mower/jira.env'))} (chmod 600)."
            ),
        )
    except KeychainError:
        missing = _map_base_resolution(base)
        return JiraCredentialResolution(
            status="missing",
            source=missing.source,
            profile_file=missing.profile_file,
            candidate_files=missing.candidate_files,
            missing_variables=(JIRA_TOKEN_ENV,),
            message="Jira API token is missing from the configured Keychain entry",
            remediation=(
                f"Set {JIRA_TOKEN_ENV} in the environment or add the token to "
                f"the macOS Keychain entry named by {JIRA_KEYCHAIN_SERVICE_ENV}."
            ),
        )
    return JiraCredentialResolution(
        status="ok",
        source=base.source,
        email=email,
        token=keychain_token,
        keychain_used=True,
        profile_file=base.profile_file,
        candidate_files=tuple(base.candidate_files),
        message="Jira credentials resolved from the macOS Keychain",
    )


class JiraRedirectRejected(OSError):
    """An HTTP redirect was rejected instead of followed.

    Carries only a fixed message (status code); never a URL, header, or
    credential. The client maps this to ``jira_unavailable`` without
    retrying, so a redirect can never forward Basic Authorization.
    """

    def __init__(self, status: int | str, location: str = "") -> None:
        try:
            parsed_status = int(status)
        except (TypeError, ValueError):
            parsed_status = 0
        self.status = parsed_status
        self.location = str(location or "")[:1024]
        super().__init__(
            f"refusing HTTP redirect ({parsed_status})"
            if parsed_status
            else "refusing HTTP redirect"
        )


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that rejects every redirect before following it.

    urllib's default redirect handler would re-send the request --
    including the Basic Authorization header -- to the Location URL, and
    would additionally allow an HTTPS-to-HTTP downgrade. Raising here
    means no second request is ever built, so credentials cannot leak to
    the redirect target, same-host or cross-host alike.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        location = ""
        try:
            location = str(headers.get("Location") or headers.get("location") or "")
        except (AttributeError, TypeError, ValueError):
            pass
        raise JiraRedirectRejected(int(code), location)


def default_http_runner(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    *,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    return_redirect: bool = False,
) -> tuple[int, Mapping[str, str], bytes]:
    """Perform one bounded HTTPS request without following secrets anywhere.

    Redirects are rejected before urllib can forward the Authorization
    header (see _RejectRedirectHandler). When ``return_redirect`` is true,
    the original status and bounded Location header are returned without a
    second request; otherwise the rejection propagates. HTTP error statuses are returned
    as data (never raised) so the caller maps them to closed codes.
    Transport failures raise OSError/ValueError subclasses, which the
    caller maps to ``jira_unavailable``.
    """
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    opener = urllib.request.build_opener(_RejectRedirectHandler)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(max_response_bytes + 1)
            return (
                int(response.status),
                {str(k): str(v) for k, v in response.headers.items()},
                raw,
            )
    except JiraRedirectRejected as exc:
        if not return_redirect:
            raise
        return (
            exc.status,
            {"Location": exc.location} if exc.location else {},
            b"",
        )
    except urllib.error.HTTPError as exc:
        try:
            raw_error = exc.read(max_response_bytes + 1)
        except (OSError, ValueError):
            raw_error = b""
        headers_out: dict[str, str] = {}
        try:
            headers_out = {str(k): str(v) for k, v in exc.headers.items()}
        except (AttributeError, ValueError):
            pass
        return (int(exc.code), headers_out, raw_error)


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    raw = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds > RETRY_AFTER_CAP_SECONDS:
        return RETRY_AFTER_CAP_SECONDS if seconds > RETRY_AFTER_CAP_SECONDS else None
    return float(seconds)


def _backoff_delay(attempt: int, random_fn: JiraRandom) -> float:
    capped = min(BACKOFF_BASE_SECONDS * (2.0**attempt), BACKOFF_CAP_SECONDS)
    try:
        jitter = float(random_fn()) * BACKOFF_BASE_SECONDS
    except (TypeError, ValueError):
        jitter = 0.0
    return min(capped + max(0.0, jitter), BACKOFF_CAP_SECONDS)


def _parse_createmeta_total(data: Mapping[str, Any], *, endpoint: str) -> int:
    """Parse a create-metadata ``total`` or fail closed.

    Booleans, non-numeric values, and out-of-range totals are malformed
    pagination state: callers must raise, never treat a partial page as
    exhaustive.
    """
    total_raw = data.get("total")
    if isinstance(total_raw, bool):
        raise JiraApiError("jira_unavailable", endpoint=endpoint)
    try:
        total = int(total_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise JiraApiError("jira_unavailable", endpoint=endpoint) from None
    if total < 0 or total > 10000:
        raise JiraApiError("jira_unavailable", endpoint=endpoint)
    return total


def _check_createmeta_start(
    data: Mapping[str, Any], start_at: int, *, endpoint: str
) -> None:
    """Verify the server's ``startAt`` echo matches the requested offset.

    A missing echo is tolerated when the server sent no pagination state
    at all; a present but mismatched or malformed echo fails closed.
    """
    server_start_raw = data.get("startAt")
    if server_start_raw is None:
        if "total" in data:
            raise JiraApiError("jira_unavailable", endpoint=endpoint)
        return
    if isinstance(server_start_raw, bool):
        raise JiraApiError("jira_unavailable", endpoint=endpoint)
    try:
        server_start = int(server_start_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise JiraApiError("jira_unavailable", endpoint=endpoint) from None
    if server_start != start_at:
        raise JiraApiError("jira_unavailable", endpoint=endpoint)


@dataclass
class JiraReadClient:
    """Authenticated read-only client bound to one Jira Cloud tenant.

    ``site_url`` is browse/display identity only; every request goes to the
    scoped-token API gateway for ``cloud_id``. Instances hold the token in
    memory only and never emit it: headers are built per request and never
    logged, stored, or included in diagnostics.
    """

    cloud_id: str
    email: str
    token: str
    site_url: str = ""
    http_runner: JiraHttpRunner | None = None
    sleep_fn: JiraSleep = field(default_factory=lambda: time.sleep)
    random_fn: JiraRandom = field(default_factory=lambda: _random.random)
    cancelled_fn: JiraCancelled = field(default_factory=lambda: (lambda: False))
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_attempts: int = MAX_ATTEMPTS

    def __post_init__(self) -> None:
        if not _CLOUD_ID_RE.fullmatch(self.cloud_id):
            raise ValueError("cloud_id must be a bounded token of letters, digits, or hyphens")
        if not validate_jira_email(self.email):
            raise ValueError("email must be a bounded user@domain address")
        if not self.token or len(self.token) > 4096:
            raise ValueError("token must be a non-empty bounded string")
        if self.site_url:
            display_site_url(self.site_url)
        self.max_attempts = max(1, min(int(self.max_attempts or 1), 8))

    @property
    def base_url(self) -> str:
        return gateway_base(self.cloud_id)

    def browse_url(self, path: str = "") -> str:
        """Return a display-only browse URL under the configured site."""
        if not self.site_url:
            return ""
        return self.site_url.rstrip("/") + "/" + path.lstrip("/")

    def _auth_headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        raw = f"{self.email}:{self.token}".encode("utf-8")
        headers = {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json",
            "User-Agent": "code-mower-jira-read/1.0",
        }
        if extra:
            headers.update(dict(extra))
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        endpoint: str = "",
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Perform one read request with bounded retries, mapped to codes.

        Only GET is allowed, plus the read-only search and permission-check
        POST endpoints declared below. Raw bodies never cross this boundary.
        ``allow_empty`` accepts an empty 2xx body as ``{}`` for endpoints that
        answer 204 No Content.
        """
        value = self._request_parsed(
            method,
            path,
            query=query,
            json_body=json_body,
            endpoint=endpoint,
            allow_empty=allow_empty,
        )
        if not isinstance(value, dict):
            raise JiraApiError("jira_unavailable", endpoint=endpoint)
        return dict(value)

    def request_list(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        endpoint: str = "",
    ) -> list[Any]:
        """Same as request_json for endpoints returning a top-level array."""
        value = self._request_parsed(
            method, path, query=query, json_body=None, endpoint=endpoint
        )
        if not isinstance(value, list):
            raise JiraApiError("jira_unavailable", endpoint=endpoint)
        return list(value)

    def _check_request_allowed(self, method: str, path: str) -> None:
        """Reject any request outside this client's closed request policy.

        The base client is read-only by construction: GET, plus the two
        read-only POST endpoints above. A guarded mutation surface must
        subclass and widen this allow-list deliberately (see
        ``jira_mutations.JiraMutationClient``); nothing else may write.
        """
        allowed_post = method == "POST" and path in READ_ONLY_POST_PATHS
        if method != "GET" and not allowed_post:
            raise ValueError("jira_cloud client is read-only: refusing non-read request")
        if not path.startswith("/rest/api/3/"):
            raise ValueError("jira_cloud client refuses paths outside /rest/api/3/")

    def _attempts_for(self, method: str, path: str, endpoint: str = "") -> int:
        """Return the retry budget for one request.

        This is the single retry-policy seam, and the counterpart of
        ``_check_request_allowed``. Every request this client can issue is a
        read, and a repeated read cannot change Jira, so all of them keep the
        full bounded budget. A subclass that can write must narrow this for
        any request without a server-side idempotency key: an ambiguous
        timeout, 429, or 5xx cannot be told apart from a request Jira already
        committed, so a blind retry there would double-apply (see
        ``jira_mutations.JiraMutationClient``).

        ``endpoint`` is the caller's closed intent label. Two calls can share
        a method and a path and still need different budgets: acquiring a
        create-or-update claim depends on the 201-vs-200 distinction, which a
        retry destroys, while rewriting that same key afterwards does not.
        """
        return max(1, self.max_attempts)

    def _on_request_attempt(self, method: str, path: str) -> None:
        """Transport-attempt hook, fired once per real HTTP attempt.

        This runs immediately before the runner is invoked, so it sees every
        attempt that actually leaves this process: successes, failures, and
        each retry separately. The base client only reads, so it counts
        nothing. A writing subclass overrides this to account for write
        attempts whose outcome may be ambiguous, which is the only honest way
        to report how much this process may have changed at Jira (see
        ``jira_mutations.JiraMutationClient``).
        """

    def _request_parsed(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        endpoint: str = "",
        allow_empty: bool = False,
    ) -> Any:
        return self._request_status_parsed(
            method,
            path,
            query=query,
            json_body=json_body,
            endpoint=endpoint,
            allow_empty=allow_empty,
        )[1]

    def _request_status_parsed(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        endpoint: str = "",
        allow_empty: bool = False,
    ) -> tuple[int, Any]:
        """As ``_request_parsed``, also returning the successful status code.

        Jira distinguishes create from update by status on some endpoints:
        ``PUT`` of an issue property answers 201 when it created the value and
        200 when it replaced an existing one. That distinction is the only
        at-most-once signal those endpoints offer, so it must survive the
        transport instead of being flattened into "2xx".
        """
        status, _, value = self._request_status_headers_parsed(
            method,
            path,
            query=query,
            json_body=json_body,
            endpoint=endpoint,
            allow_empty=allow_empty,
        )
        return status, value

    def _request_status_headers_parsed(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        endpoint: str = "",
        allow_empty: bool = False,
        accepted_statuses: frozenset[int] = frozenset(),
    ) -> tuple[int, Mapping[str, str], Any]:
        """As :meth:`_request_status_parsed`, preserving safe headers.

        ``accepted_statuses`` is for documented asynchronous endpoints that
        answer with a redirect carrying a task location. Redirects are never
        followed here, so credentials cannot leave the configured gateway.
        """
        self._check_request_allowed(method, path)

        url = self.base_url + path
        if query:
            encoded = urllib.parse.urlencode(
                {str(k): str(v) for k, v in query.items()}, doseq=False
            )
            url = f"{url}?{encoded}"
        body: bytes | None = None
        headers = self._auth_headers()
        if json_body is not None:
            body = json.dumps(dict(json_body), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if len(body) > 64 * 1024:
                raise ValueError("jira_cloud request body exceeds the read-request bound")

        runner = self.http_runner
        attempts = self._attempts_for(method, path, endpoint)
        last_code = "jira_unavailable"
        for attempt in range(attempts):
            if self.cancelled_fn():
                raise JiraApiError("jira_cancelled", endpoint=endpoint)
            self._on_request_attempt(method, path)
            try:
                if runner is not None:
                    status, resp_headers, raw = runner(method, url, headers, body)
                elif accepted_statuses:
                    status, resp_headers, raw = default_http_runner(
                        method,
                        url,
                        headers,
                        body,
                        timeout_seconds=self.timeout_seconds,
                        max_response_bytes=self.max_response_bytes,
                        return_redirect=True,
                    )
                else:
                    status, resp_headers, raw = default_http_runner(
                        method,
                        url,
                        headers,
                        body,
                        timeout_seconds=self.timeout_seconds,
                        max_response_bytes=self.max_response_bytes,
                    )
            except JiraApiError:
                raise
            except JiraRedirectRejected:
                # Rejected redirects fail fast with the request endpoint:
                # retrying cannot help and must never forward credentials.
                raise JiraApiError("jira_unavailable", endpoint=endpoint) from None
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                socket.timeout,
                TimeoutError,
                OSError,
                ValueError,
            ):
                last_code = "jira_unavailable"
                status = -1
                resp_headers = {}
                raw = b""
            if status == -1:
                pass  # transport failure mapped below; may retry
            elif 200 <= status < 300 or status in accepted_statuses:
                if len(raw) > self.max_response_bytes:
                    raise JiraApiError("jira_unavailable", endpoint=endpoint)
                if allow_empty and not raw.strip():
                    return status, dict(resp_headers), {}
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise JiraApiError("jira_unavailable", endpoint=endpoint) from None
                if not isinstance(value, (dict, list)):
                    raise JiraApiError("jira_unavailable", endpoint=endpoint)
                return status, dict(resp_headers), value
            elif 300 <= status < 400:
                raise JiraApiError("jira_unavailable", endpoint=endpoint)
            elif status == 401:
                raise JiraApiError("jira_unauthorized", endpoint=endpoint)
            elif status == 403:
                raise JiraApiError("jira_forbidden", endpoint=endpoint)
            elif status == 404:
                raise JiraApiError("jira_not_found", endpoint=endpoint)
            elif status == 409:
                # The issue/workflow changed under this request. Retrying
                # cannot fix drift, and a blind retry could double-apply.
                raise JiraApiError("jira_conflict", endpoint=endpoint)
            elif status == 429:
                last_code = "jira_rate_limited"
            elif status in RETRYABLE_STATUS_CODES:
                last_code = "jira_unavailable"
            else:
                raise JiraApiError("jira_rejected", endpoint=endpoint)

            if attempt + 1 >= attempts:
                break
            if status == 429:
                retry_after = _parse_retry_after(resp_headers)
                delay = (
                    retry_after
                    if retry_after is not None
                    else _backoff_delay(attempt, self.random_fn)
                )
                # Jitter the capped Retry-After so parallel workers do not
                # thundering-herd the gateway when capacity returns.
                try:
                    delay = min(delay + float(self.random_fn()) * BACKOFF_BASE_SECONDS, RETRY_AFTER_CAP_SECONDS)
                except (TypeError, ValueError):
                    pass
            else:
                delay = _backoff_delay(attempt, self.random_fn)
            self.sleep_fn(delay)
        raise JiraApiError(last_code, endpoint=endpoint)

    # -- Read primitives (metadata only) ---------------------------------

    def get_server_info(self) -> dict[str, str]:
        """Verify cloud identity; returns bounded display fields only."""
        data = self.request_json("GET", "/rest/api/3/serverInfo", endpoint="serverInfo")
        return {
            "base_url": _bounded_str(data.get("baseUrl"), 256),
            "server_title": _bounded_str(data.get("serverTitle")),
        }

    def get_project(self, project_id: str) -> dict[str, str]:
        """Fetch one project by immutable id (never by key internally)."""
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("project_id must be a bounded numeric id")
        quoted = urllib.parse.quote(project_id, safe="")
        data = self.request_json(
            "GET", f"/rest/api/3/project/{quoted}", endpoint="project"
        )
        return {
            "id": _bounded_str(data.get("id"), 32),
            "key": _bounded_str(data.get("key"), 32),
            "name": _bounded_str(data.get("name")),
        }

    def list_projects(self, *, max_results: int = 50) -> list[dict[str, str]]:
        """Discover projects; returns bounded identity triples only."""
        bounded = max(1, min(int(max_results), 100))
        data = self.request_json(
            "GET",
            "/rest/api/3/project/search",
            query={"maxResults": str(bounded), "orderBy": "key"},
            endpoint="projectSearch",
        )
        values = data.get("values")
        projects: list[dict[str, str]] = []
        for entry in values if isinstance(values, list) else []:
            if not isinstance(entry, Mapping):
                continue
            projects.append(
                {
                    "id": _bounded_str(entry.get("id"), 32),
                    "key": _bounded_str(entry.get("key"), 32),
                    "name": _bounded_str(entry.get("name")),
                }
            )
        return projects[:bounded]

    def get_issue_types(self, project_id: str) -> list[dict[str, str]]:
        """List issue types for a project; id plus display name only.

        Follows bounded create-metadata pagination. Repeated or malformed
        pagination state fails closed instead of presenting a partial page
        as the exhaustive set. A response with no pagination keys is one
        exhaustive page.
        """
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("project_id must be a bounded numeric id")
        quoted = urllib.parse.quote(project_id, safe="")
        path = f"/rest/api/3/issue/createmeta/{quoted}/issuetypes"
        issue_types: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        start_at = 0
        seen_starts: set[int] = set()
        for _ in range(CREATEMETA_MAX_PAGES):
            if start_at in seen_starts:
                raise JiraApiError("jira_unavailable", endpoint="createMeta")
            seen_starts.add(start_at)
            data = self.request_json(
                "GET",
                path,
                query={"startAt": str(start_at), "maxResults": str(CREATEMETA_PAGE_SIZE)},
                endpoint="createMeta",
            )
            raw_types = data.get("issueTypes")
            if not isinstance(raw_types, list):
                raise JiraApiError("jira_unavailable", endpoint="createMeta")
            for entry in raw_types:
                if not isinstance(entry, Mapping):
                    continue
                type_id = _bounded_str(entry.get("id"), 32)
                if not type_id or not _TOKEN_ID_RE.fullmatch(type_id):
                    continue
                if type_id in seen_ids:
                    continue
                seen_ids.add(type_id)
                issue_types.append(
                    {"id": type_id, "name": _bounded_str(entry.get("name"))}
                )
            if "total" not in data and "startAt" not in data:
                break
            total = _parse_createmeta_total(data, endpoint="createMeta")
            _check_createmeta_start(data, start_at, endpoint="createMeta")
            if start_at + len(raw_types) >= total:
                break
            next_start = start_at + len(raw_types)
            if next_start <= start_at or next_start in seen_starts:
                raise JiraApiError("jira_unavailable", endpoint="createMeta")
            start_at = next_start
        else:
            raise JiraApiError("jira_unavailable", endpoint="createMeta")
        return issue_types

    def get_required_create_fields(
        self, project_id: str, issue_type_id: str
    ) -> list[str]:
        """Return required create field ids for a project/issue-type pair.

        Only field ids (never values, labels-as-prose, or defaults) cross
        this boundary, so workflow variation is visible without prose.
        Malformed or repeated pagination state raises JiraApiError instead
        of returning a partial set.
        """
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("project_id must be a bounded numeric id")
        if not issue_type_id or not _TOKEN_ID_RE.fullmatch(issue_type_id):
            raise ValueError("issue_type_id must be a bounded token")
        quoted_project = urllib.parse.quote(project_id, safe="")
        quoted_type = urllib.parse.quote(issue_type_id, safe="")
        path = f"/rest/api/3/issue/createmeta/{quoted_project}/issuetypes/{quoted_type}"
        # The endpoint returns ``fields`` as a paginated array of records
        # shaped ``{"fieldId": ..., "required": bool}`` with
        # ``startAt``/``maxResults``/``total`` pagination. Pages are
        # followed with bounded state; malformed or repeated pagination
        # state fails closed with a stable safe error instead of returning
        # a partial required-field set that a caller could mistake for
        # complete. Malformed records inside a well-formed page are still
        # skipped; a malformed page is not.
        required: set[str] = set()
        start_at = 0
        seen_starts: set[int] = set()
        for _ in range(CREATEMETA_MAX_PAGES):
            if start_at in seen_starts:
                raise JiraApiError("jira_unavailable", endpoint="createMeta")
            seen_starts.add(start_at)
            data = self.request_json(
                "GET",
                path,
                query={"startAt": str(start_at), "maxResults": str(CREATEMETA_PAGE_SIZE)},
                endpoint="createMeta",
            )
            raw_fields = data.get("fields")
            if not isinstance(raw_fields, list):
                raise JiraApiError("jira_unavailable", endpoint="createMeta")
            for record in raw_fields:
                if not isinstance(record, Mapping):
                    continue
                field_id = record.get("fieldId")
                if (
                    isinstance(field_id, str)
                    and _TOKEN_ID_RE.fullmatch(field_id)
                    and record.get("required") is True
                ):
                    required.add(field_id)
            if "total" not in data and "startAt" not in data:
                break
            total = _parse_createmeta_total(data, endpoint="createMeta")
            _check_createmeta_start(data, start_at, endpoint="createMeta")
            if start_at + len(raw_fields) >= total:
                break
            next_start = start_at + len(raw_fields)
            if next_start <= start_at or next_start in seen_starts:
                raise JiraApiError("jira_unavailable", endpoint="createMeta")
            start_at = next_start
        else:
            raise JiraApiError("jira_unavailable", endpoint="createMeta")
        if len(required) > MAX_REQUIRED_CREATE_FIELDS:
            raise JiraApiError("jira_unavailable", endpoint="createMeta")
        return sorted(required)

    def get_status_categories(self) -> list[dict[str, str]]:
        """List status categories; bounded id/key/name triples only."""
        raw = self.request_list(
            "GET", "/rest/api/3/statuscategory", endpoint="statusCategory"
        )
        return _bounded_id_key_name_list(raw)

    def get_statuses(self) -> list[dict[str, str]]:
        """List statuses with their category; bounded metadata only."""
        raw = self.request_list("GET", "/rest/api/3/status", endpoint="status")
        statuses: list[dict[str, str]] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, Mapping):
                continue
            status_id = _bounded_str(entry.get("id"), 32)
            if not status_id:
                continue
            category = entry.get("statusCategory") if isinstance(
                entry.get("statusCategory"), Mapping
            ) else {}
            statuses.append(
                {
                    "id": status_id,
                    "name": _bounded_str(entry.get("name")),
                    "category_id": _bounded_str(category.get("id"), 32),
                    "category_key": _bounded_str(category.get("key"), 32),
                }
            )
        return statuses[:256]

    def search_issues(
        self,
        jql: str,
        *,
        fields: Sequence[str] = tuple(sorted(SAFE_SEARCH_FIELDS)),
        max_issues: int = SEARCH_MAX_ISSUES,
    ) -> dict[str, Any]:
        """Search with enhanced JQL pagination (nextPageToken), metadata only.

        ``fields`` must stay inside SAFE_SEARCH_FIELDS; requests naming any
        other field (including text/prose fields) are rejected before any
        network call. Returned issues carry bounded status/type/label/
        assignment/timestamp metadata only. Page fetches are bounded
        independently of usable results; a repeated token returns the
        collected issues with ``truncated`` set, and a malformed token
        raises JiraApiError.
        """
        query = jql.strip()
        if not query or len(query) > 2000 or "\n" in query or "\r" in query:
            raise ValueError("jql must be a single line of at most 2000 characters")
        requested = list(fields)
        if not requested:
            raise ValueError("search must request at least one metadata field")
        for name in requested:
            if name not in SAFE_SEARCH_FIELDS:
                raise ValueError(f"search field {name!r} is not readable metadata")
        bounded_total = max(1, min(int(max_issues), 1000))
        collected: list[dict[str, Any]] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()
        truncated = False
        # Pages are bounded independently of the usable-issue count: empty
        # or unusable pages with fresh tokens must terminate, and a
        # repeated token returns what was collected with truncated set
        # rather than looping forever.
        for _ in range(SEARCH_MAX_PAGES):
            body: dict[str, Any] = {
                "jql": query,
                "fields": sorted(set(requested)),
                "maxResults": min(SEARCH_PAGE_SIZE, bounded_total - len(collected)),
            }
            if next_token:
                body["nextPageToken"] = next_token
            data = self.request_json(
                "POST", "/rest/api/3/search/jql", json_body=body, endpoint="search"
            )
            raw_issues = data.get("issues")
            for entry in raw_issues if isinstance(raw_issues, list) else []:
                if len(collected) >= bounded_total:
                    break
                parsed = _parse_search_issue(entry)
                if parsed is not None:
                    collected.append(parsed)
            if len(collected) >= bounded_total:
                truncated = bool(
                    data.get("nextPageToken") or data.get("isLast") is False
                )
                break
            token = data.get("nextPageToken")
            is_last = data.get("isLast")
            if token is None or token == "":
                # No continuation offered. When the server still claims
                # more data exists, say so explicitly instead of
                # presenting a partial page as exhaustive.
                if is_last is False:
                    truncated = True
                break
            if not isinstance(token, str):
                raise JiraApiError("jira_unavailable", endpoint="search")
            if is_last is True:
                break
            if token in seen_tokens:
                truncated = True
                break
            seen_tokens.add(token)
            next_token = token
        else:
            # Page cap reached while the server kept offering continuation.
            truncated = True
        return {"issues": collected, "truncated": truncated}

    def get_transitions(self, issue_id_or_key: str) -> list[dict[str, str]]:
        """List available transitions for one issue; bounded metadata only."""
        quoted = urllib.parse.quote(validate_issue_ref(issue_id_or_key), safe="")
        data = self.request_json(
            "GET", f"/rest/api/3/issue/{quoted}/transitions", endpoint="transitions"
        )
        raw = data.get("transitions")
        transitions: list[dict[str, str]] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, Mapping):
                continue
            transition_id = _bounded_str(entry.get("id"), 32)
            if not transition_id:
                continue
            target = entry.get("to") if isinstance(entry.get("to"), Mapping) else {}
            transitions.append(
                {
                    "id": transition_id,
                    "name": _bounded_str(entry.get("name")),
                    "to_status_id": _bounded_str(target.get("id"), 32),
                    "to_status_name": _bounded_str(target.get("name")),
                }
            )
        return transitions[:64]

    def get_my_account_id(self) -> str:
        """Return only the authenticated Atlassian account id.

        The endpoint also returns display name, email, locale, and avatars.
        None of that is read here: an account id is the minimum needed to
        recognize a self-claim, and the account email never leaves the
        response buffer.
        """
        data = self.request_json("GET", "/rest/api/3/myself", endpoint="myself")
        return _bounded_str(data.get("accountId"), 128)

    def get_issue_state(self, issue_id_or_key: str) -> dict[str, Any]:
        """Read one issue's bounded lifecycle state before a guarded write.

        Only ``status``, ``assignee``, ``project``, and ``issuetype`` are
        requested, so no summary, description, comment, or attachment prose
        is ever fetched. ``assignee_account_id`` stays for local comparison
        only and must not reach operator output.
        """
        quoted = urllib.parse.quote(validate_issue_ref(issue_id_or_key), safe="")
        data = self.request_json(
            "GET",
            f"/rest/api/3/issue/{quoted}",
            query={"fields": "status,assignee,project,issuetype"},
            endpoint="issue",
        )
        raw_fields = data.get("fields")
        fields = raw_fields if isinstance(raw_fields, Mapping) else {}

        def _sub(name: str) -> Mapping[str, Any]:
            value = fields.get(name)
            return value if isinstance(value, Mapping) else {}

        assignee = _sub("assignee")
        return {
            "id": _bounded_str(data.get("id"), 32),
            "key": _bounded_str(data.get("key"), 32),
            "project_id": _bounded_str(_sub("project").get("id"), 32),
            "status_id": _bounded_str(_sub("status").get("id"), 32),
            "status_name": _bounded_str(_sub("status").get("name")),
            "issue_type_id": _bounded_str(_sub("issuetype").get("id"), 32),
            "assigned": bool(fields.get("assignee")),
            "assignee_account_id": _bounded_str(assignee.get("accountId"), 128),
        }

    def get_issue_property(
        self, issue_id_or_key: str, property_key: str
    ) -> dict[str, Any] | None:
        """Read one issue property value, or None when it is not set.

        A missing property is normal first-run state, so 404 maps to None.
        Every other transport failure still raises a closed reason code.
        """
        quoted = urllib.parse.quote(validate_issue_ref(issue_id_or_key), safe="")
        key = validate_property_key(property_key)
        try:
            data = self.request_json(
                "GET",
                f"/rest/api/3/issue/{quoted}/properties/{urllib.parse.quote(key, safe='')}",
                endpoint="issueProperty",
            )
        except JiraApiError as exc:
            if exc.code == "jira_not_found":
                return None
            raise
        value = data.get("value")
        return dict(value) if isinstance(value, Mapping) else None

    def has_remote_link(self, issue_id_or_key: str, global_id: str) -> bool:
        """Report whether a remote link with ``global_id`` already exists."""
        quoted = urllib.parse.quote(validate_issue_ref(issue_id_or_key), safe="")
        wanted = validate_remote_link_global_id(global_id)
        try:
            value = self._request_parsed(
                "GET",
                f"/rest/api/3/issue/{quoted}/remotelink",
                query={"globalId": wanted},
                endpoint="remoteLink",
            )
        except JiraApiError as exc:
            if exc.code == "jira_not_found":
                return False
            raise
        entries = value if isinstance(value, list) else [value]
        return any(
            isinstance(entry, Mapping)
            and _bounded_str(entry.get("globalId"), MAX_GLOBAL_ID_LENGTH) == wanted
            for entry in entries
        )

    def check_permissions(
        self,
        project_id: str,
        permissions: Sequence[str] = DEFAULT_PROBE_PERMISSIONS,
    ) -> dict[str, bool]:
        """Probe effective permissions for a project; booleans only.

        This is a read-only capability probe. A ``True`` value never
        authorizes a write: writes stay disabled until an explicit,
        separately gated mutation surface exists.
        """
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("project_id must be a bounded numeric id")
        requested = [str(name) for name in permissions if str(name)]
        if not requested or len(requested) > 32:
            raise ValueError("check a bounded non-empty permission list")
        for name in requested:
            if not re.fullmatch(r"[A-Z_]{1,64}", name):
                raise ValueError(f"permission {name!r} is not a bounded permission token")
        data = self.request_json(
            "POST",
            "/rest/api/3/permissions/check",
            json_body={
                "projectPermissions": [
                    {"permissions": requested, "projects": [int(project_id)]}
                ]
            },
            endpoint="permissions",
        )
        wanted = int(project_id)
        result: dict[str, bool] = {name: False for name in requested}
        entries = data.get("projectPermissions")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("permission")
            if not isinstance(name, str) or name not in result:
                continue
            granted = entry.get("projects")
            if not isinstance(granted, list):
                continue
            for raw_id in granted:
                if isinstance(raw_id, bool):
                    continue
                if isinstance(raw_id, int):
                    candidate: int | None = raw_id
                elif isinstance(raw_id, str) and _PROJECT_ID_RE.fullmatch(raw_id):
                    candidate = int(raw_id)
                else:
                    continue
                if candidate == wanted:
                    result[name] = True
                    break
        return result


def _bounded_id_key_name_list(raw: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            continue
        item_id = _bounded_str(entry.get("id"), 32)
        if not item_id:
            continue
        items.append(
            {
                "id": item_id,
                "key": _bounded_str(entry.get("key"), 32),
                "name": _bounded_str(entry.get("name")),
            }
        )
    return items[:64]


def _parse_search_issue(entry: Any) -> dict[str, Any] | None:
    """Reduce one raw search hit to bounded metadata; None when unusable."""
    if not isinstance(entry, Mapping):
        return None
    issue_id = _bounded_str(entry.get("id"), 32)
    key = _bounded_str(entry.get("key"), 32)
    if not issue_id or not key:
        return None
    raw_fields = entry.get("fields")
    fields_in = raw_fields if isinstance(raw_fields, Mapping) else {}
    status = fields_in.get("status") if isinstance(fields_in.get("status"), Mapping) else {}
    issue_type = fields_in.get("issuetype") if isinstance(
        fields_in.get("issuetype"), Mapping
    ) else {}
    project = fields_in.get("project") if isinstance(
        fields_in.get("project"), Mapping
    ) else {}
    assignee = fields_in.get("assignee")
    out_fields: dict[str, Any] = {
        "status_id": _bounded_str(status.get("id"), 32),
        "status_name": _bounded_str(status.get("name")),
        "issue_type_id": _bounded_str(issue_type.get("id"), 32),
        "issue_type_name": _bounded_str(issue_type.get("name")),
        "labels": _bounded_labels(fields_in.get("labels")),
        "assigned": assignee is not None,
        "created": _bounded_str(fields_in.get("created"), 64),
        "updated": _bounded_str(fields_in.get("updated"), 64),
    }
    return {
        "id": issue_id,
        "key": key,
        "project_id": _bounded_str(project.get("id"), 32),
        "fields": out_fields,
    }


def quote_jql_string(value: str) -> str:
    """Quote one JQL string literal, escaping backslashes and quotes."""
    text = value.strip()
    if not text or len(text) > 64 or "\n" in text or "\r" in text:
        raise ValueError("JQL literal must be a bounded single-line string")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_project_jql(
    project_id: str = "", project_key: str = "", extra: str = ""
) -> str:
    """Build a project-scoped JQL fragment using the immutable id first.

    Project keys (including JQL reserved words such as AND, OR, or IF) are
    always quoted; the numeric project id is the preferred primitive and is
    never quoted. ``extra`` must already be a bounded single-line fragment.
    """
    clause = ""
    if project_id.strip():
        if not _PROJECT_ID_RE.fullmatch(project_id.strip()):
            raise ValueError("project_id must be a bounded numeric id")
        clause = f"project = {project_id.strip()}"
    elif project_key.strip():
        clause = f"project = {quote_jql_string(project_key.strip())}"
    else:
        raise ValueError("build_project_jql requires a project id or key")
    fragment = extra.strip()
    if fragment:
        if len(fragment) > 1900 or "\n" in fragment or "\r" in fragment:
            raise ValueError("extra JQL must be a bounded single-line fragment")
        clause = f"{clause} AND ({fragment})"
    if len(clause) > 2000:
        raise ValueError("project JQL exceeds the bounded length")
    return clause


def map_status_to_lifecycle(
    status_id: str,
    statuses: Sequence[Mapping[str, Any]],
    status_category_map: Mapping[str, Sequence[str]] | None,
) -> str | None:
    """Map a Jira status id onto a portable lifecycle category.

    Returns None when the status id is unknown or unmapped, so callers stay
    explicit instead of guessing a lifecycle.
    """
    wanted = str(status_id or "").strip()
    if not wanted:
        return None
    known = {str(item.get("id") or "") for item in statuses if isinstance(item, Mapping)}
    if wanted not in known:
        return None
    mapping = status_category_map or {}
    for category in ("new", "in_progress", "blocked", "done"):
        ids = mapping.get(category) or []
        if wanted in {str(item) for item in ids}:
            return category
    return None
