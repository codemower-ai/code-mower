"""Shared fail-closed provider credential resolution and profile loading.

Credential values stay in memory only. Callers should serialize only the safe
diagnostic fields returned by ProviderCredentialResolution.safe_detail().
Local file paths are redacted using display_profile_path; secrets, raw paths,
and auth output are never persisted in campaign files, printed, or uploaded.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .devin_api import validate_devin_org_id

DEFAULT_CONFIG_DIR = Path("~/.config/code-mower")
SAFE_CONFIG_DIR = "~/.config/code-mower"

# Bounded account-email check for the Jira Cloud API-token path. This is a
# shape check only; delivery and revocation state are verified by the live
# read probe in jira_cloud, never by guessing here.
_JIRA_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,128}\.[^@\s]{1,32}$")


def validate_jira_email(email: str) -> bool:
    """Validate that an account email has a bounded user@domain shape."""
    return bool(_JIRA_EMAIL_RE.fullmatch(email))


# Registry of supported providers, their required variables, optional variables, and validators.
# Extensible for other providers, with hosted Devin as the first consumer.
PROVIDER_CREDENTIAL_SPECS: dict[str, dict[str, Any]] = {
    "devin": {
        "required_env": ("DEVIN_API_KEY", "DEVIN_ORG_ID"),
        "optional_env": ("CODE_MOWER_DEVIN_REPOSITORIES", "DEVIN_REPOSITORIES"),
        "alias_groups": (("CODE_MOWER_DEVIN_REPOSITORIES", "DEVIN_REPOSITORIES"),),
        "validators": {
            "DEVIN_ORG_ID": validate_devin_org_id,
        },
        "file_prefix": "devin",
    },
    "jira": {
        "required_env": ("JIRA_API_EMAIL", "JIRA_API_TOKEN"),
        "optional_env": ("JIRA_KEYCHAIN_SERVICE",),
        "validators": {
            "JIRA_API_EMAIL": validate_jira_email,
        },
        "file_prefix": "jira",
    },
}


@dataclass(frozen=True)
class ProviderCredentialResolution:
    """Bounded, persistence-safe result of provider credential resolution."""

    status: str  # "ok", "missing", "ambiguous", "malformed", "insecure_permissions"
    provider: str
    source: str  # "env", "credential_file", "profile", "single_profile", "missing"
    credentials: Mapping[str, str] = field(default_factory=dict)  # In-memory only!
    profile_file: Path | None = None
    candidate_files: tuple[str, ...] = ()  # Filenames only! e.g. ("devin.env",)
    message: str = ""
    remediation: str = ""
    missing_variables: tuple[str, ...] = ()

    @property
    def has_credentials(self) -> bool:
        return bool(self.status == "ok")

    def safe_detail(self) -> dict[str, Any]:
        """Return safe diagnostic metadata without secrets or absolute paths."""
        detail: dict[str, Any] = {
            "provider": self.provider,
            "source": self.source,
            "status": self.status,
        }
        if self.profile_file is not None:
            detail["profile_file"] = display_profile_path(self.profile_file)
        if self.candidate_files:
            detail["candidate_files"] = list(self.candidate_files)
        if self.missing_variables:
            detail["missing_variables"] = list(self.missing_variables)
        return detail

    def apply_to_env(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return a copy of env with resolved credentials merged in.

        Ambient environment values win over stored credentials.
        """
        current = dict(os.environ if env is None else env)
        spec = PROVIDER_CREDENTIAL_SPECS.get(self.provider, {})
        alias_groups = spec.get("alias_groups", ())
        for group in alias_groups:
            effective = ""
            for alias in group:
                val = str(current.get(alias) or "").strip()
                if val:
                    effective = val
                    break
            if effective:
                for alias in group:
                    if not current.get(alias):
                        current[alias] = effective

        for key, val in self.credentials.items():
            if not current.get(key):
                current[key] = val
        return current


def normalize_provider_aliases(
    provider: str,
    mapping: Mapping[str, str],
) -> dict[str, str]:
    """Normalize alias groups within a mapping so primary and aliases share values."""
    spec = PROVIDER_CREDENTIAL_SPECS.get(provider, {})
    alias_groups = spec.get("alias_groups", ())
    result = dict(mapping)
    for group in alias_groups:
        effective = ""
        for alias in group:
            val = str(result.get(alias) or "").strip()
            if val:
                effective = val
                break
        if effective:
            for alias in group:
                result[alias] = effective
    return result


def default_config_dir() -> Path:
    """Return the default configuration directory path."""
    return DEFAULT_CONFIG_DIR.expanduser()


def display_profile_path(path: Path, config_dir: Path | None = None) -> str:
    """Format a path safely without exposing absolute home-directory paths."""
    expanded = path.expanduser()
    base_dir = (config_dir or default_config_dir()).expanduser().resolve(strict=False)
    try:
        rel = expanded.resolve(strict=False).relative_to(base_dir)
        return f"{SAFE_CONFIG_DIR}/{rel.as_posix()}"
    except ValueError:
        pass
    home = Path.home().resolve(strict=False)
    try:
        rel_home = expanded.resolve(strict=False).relative_to(home)
        return f"~/{rel_home.as_posix()}"
    except ValueError:
        return path.name


def check_file_permissions(path: Path) -> bool:
    """Check that file permissions are restricted to user-only access (e.g. 0600 or 0400).

    Returns True if permissions are safe, False if group or others have any permissions.
    """
    if os.name != "posix":
        return True
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return (mode & 0o077) == 0


def _strip_inline_comment(s: str) -> str:
    """Strip shell-compatible unquoted inline comments from a value string.

    Preserves # inside single or double quotes, or escaped with backslash.
    A comment begins with an unquoted # preceded by whitespace or at the start.
    """
    in_single_quote = False
    in_double_quote = False
    escaped = False
    for i, ch in enumerate(s):
        if in_single_quote:
            if ch == "'":
                in_single_quote = False
        elif in_double_quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double_quote = False
        else:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_single_quote = True
            elif ch == '"':
                in_double_quote = True
            elif ch == "#":
                if i == 0 or s[i - 1].isspace():
                    return s[:i]
    return s


def _parse_assignment_value(value: str) -> str:
    value = _strip_inline_comment(value).strip()
    if not value:
        return ""
    try:
        parsed = shlex.split(value, posix=True)
    except ValueError as exc:
        raise ValueError("invalid quoted value in credential file") from exc
    if len(parsed) == 1:
        return parsed[0]
    return value.strip("'\"")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse key=value assignments from an env file.

    Supports comments, export prefix, and quoted values.
    Raises ValueError on decode or syntax errors.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not UTF-8 text") from exc
    except OSError as exc:
        reason = exc.strerror if getattr(exc, "strerror", None) else "I/O error"
        raise ValueError(f"unable to read file ({reason})") from exc

    assignments: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name.replace("_", "").isalnum() or name[:1].isdigit():
            continue
        assignments[name] = _parse_assignment_value(value)
    return assignments


def _safe_file_names(paths: list[Path]) -> tuple[str, ...]:
    return tuple(sorted(p.name for p in paths))


def discover_matching_profiles(
    provider: str,
    config_dir: Path,
) -> list[Path]:
    """Find candidate credential profiles for a provider in config_dir."""
    if not config_dir.is_dir():
        return []

    candidates: set[Path] = set()
    prov_lower = provider.lower()

    for p in config_dir.glob("*.env"):
        name_lower = p.name.lower()
        if name_lower == f"{prov_lower}.env":
            candidates.add(p)
        elif (
            name_lower.startswith(f"{prov_lower}-")
            or name_lower.startswith(f"{prov_lower}_")
            or name_lower.startswith(f"{prov_lower}.")
        ):
            candidates.add(p)

    sub_dir = config_dir / provider
    if sub_dir.is_dir():
        for p in sub_dir.glob("*.env"):
            candidates.add(p)

    return sorted(candidates, key=lambda x: x.name)


def resolve_explicit_profile(
    provider: str,
    profile_name: str,
    config_dir: Path,
) -> list[Path]:
    """Find files matching an explicit profile selector name in config_dir."""
    if not config_dir.is_dir():
        return []

    target_name = profile_name.strip()
    if not target_name:
        return []

    candidates: set[Path] = set()
    test_names = [
        target_name if target_name.endswith(".env") else f"{target_name}.env",
        f"{provider}.{target_name}.env" if not target_name.startswith(f"{provider}.") else f"{target_name}.env",
        f"{provider}-{target_name}.env" if not target_name.startswith(f"{provider}-") else f"{target_name}.env",
        f"{provider}_{target_name}.env" if not target_name.startswith(f"{provider}_") else f"{target_name}.env",
    ]

    for name in test_names:
        direct = config_dir / name
        if direct.is_file():
            candidates.add(direct)
        sub = config_dir / provider / name
        if sub.is_file():
            candidates.add(sub)

    return sorted(candidates, key=lambda x: x.name)


def resolve_provider_credentials(
    provider: str,
    *,
    credential_file: Path | None = None,
    profile: str = "",
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ProviderCredentialResolution:
    """Resolve provider credentials using strict precedence and fail-closed safety.

    Precedence:
    1. Ambient environment values are authoritative.
    2. Explicit provider credential file or profile selector.
    3. Safe automatic discovery in Code Mower's config directory (~/.config/code-mower).

    Never guesses between multiple matching profiles (returns status='ambiguous').
    Validates malformed profiles and rejects insecure file permissions.
    """
    current_env = os.environ if env is None else env
    spec = PROVIDER_CREDENTIAL_SPECS.get(provider, {})
    required_vars = tuple(spec.get("required_env", ()))
    optional_vars = tuple(spec.get("optional_env", ()))
    validators: dict[str, Callable[[str], bool]] = spec.get("validators", {})

    # 1. Ambient environment check
    ambient_present: list[str] = [
        var for var in required_vars if var in current_env and current_env[var] is not None
    ]
    ambient_missing: list[str] = []
    ambient_invalid: list[str] = []
    ambient_creds: dict[str, str] = {}

    for var in required_vars:
        val = str(current_env.get(var) or "").strip()
        if not val:
            ambient_missing.append(var)
        else:
            validator = validators.get(var)
            if validator and not validator(val):
                ambient_invalid.append(var)
            else:
                ambient_creds[var] = val

    if ambient_present:
        if ambient_missing or ambient_invalid:
            # Partial or invalid ambient credentials: fail closed, do not consult stored profiles.
            missing_var = ambient_missing[0] if ambient_missing else ambient_invalid[0]
            status = "missing" if ambient_missing else "malformed"
            if ambient_missing:
                msg = f"{provider.capitalize()} ambient credentials incomplete: {missing_var} is not set"
                rem = f"Set {missing_var} in environment or unset ambient {provider.capitalize()} variables to use stored profiles."
            else:
                msg = f"{provider.capitalize()} ambient credentials invalid: {missing_var} is invalid"
                rem = f"Set a valid {missing_var} in environment or unset ambient {provider.capitalize()} variables to use stored profiles."
            return ProviderCredentialResolution(
                status=status,
                provider=provider,
                source="env",
                missing_variables=tuple(ambient_missing + ambient_invalid),
                message=msg,
                remediation=rem,
            )

        # Complete and valid ambient credentials
        norm_env = normalize_provider_aliases(provider, current_env)
        for opt in optional_vars:
            val = str(norm_env.get(opt) or "").strip()
            if val:
                ambient_creds[opt] = val
        return ProviderCredentialResolution(
            status="ok",
            provider=provider,
            source="env",
            credentials=ambient_creds,
            message=f"{provider.capitalize()} credentials resolved from environment",
        )

    # 2. Check explicit options or environment configuration
    prov_upper = provider.upper()
    explicit_file_env = (
        str(current_env.get(f"CODE_MOWER_{prov_upper}_CREDENTIAL_FILE") or "")
        or str(current_env.get(f"{prov_upper}_CREDENTIAL_FILE") or "")
        or str(current_env.get("CODE_MOWER_PROVIDER_CREDENTIAL_FILE") or "")
    ).strip()
    selected_file = Path(credential_file) if credential_file else (Path(explicit_file_env) if explicit_file_env else None)

    explicit_profile_env = (
        str(current_env.get(f"CODE_MOWER_{prov_upper}_PROFILE") or "")
        or str(current_env.get(f"{prov_upper}_PROFILE") or "")
        or str(current_env.get("CODE_MOWER_PROVIDER_PROFILE") or "")
    ).strip()
    selected_profile = profile or explicit_profile_env

    explicit_config_dir_env = (
        str(current_env.get("CODE_MOWER_PROVIDER_CONFIG_DIR") or "").strip()
        or str(current_env.get("CODE_MOWER_CONFIG_DIR") or "").strip()
    )
    resolved_config_dir = (
        config_dir
        or (Path(explicit_config_dir_env) if explicit_config_dir_env else default_config_dir())
    ).expanduser()

    # Helper to validate and load a single file
    def _load_single_file(path: Path | str, source_label: str) -> ProviderCredentialResolution:
        path = Path(path).expanduser()
        if not path.is_file():
            return ProviderCredentialResolution(
                status="missing",
                provider=provider,
                source=source_label,
                profile_file=path,
                candidate_files=(path.name,),
                message=f"provider credential file not found: {display_profile_path(path, resolved_config_dir)}",
                remediation=f"Ensure {display_profile_path(path, resolved_config_dir)} exists.",
            )

        if not check_file_permissions(path):
            return ProviderCredentialResolution(
                status="insecure_permissions",
                provider=provider,
                source=source_label,
                profile_file=path,
                candidate_files=(path.name,),
                message=f"{provider.capitalize()} credential profile permissions are too broad: {path.name}",
                remediation=f"Run `chmod 600 {display_profile_path(path, resolved_config_dir)}` to restrict permissions.",
            )

        try:
            parsed = parse_env_file(path)
        except ValueError as exc:
            safe_exc = str(exc)
            if str(path) in safe_exc:
                safe_exc = safe_exc.replace(
                    str(path), display_profile_path(path, resolved_config_dir)
                )
            is_read_err = "unable to read file" in safe_exc
            rem = (
                f"Ensure {display_profile_path(path, resolved_config_dir)} is readable."
                if is_read_err
                else f"Fix syntax errors in {display_profile_path(path, resolved_config_dir)}."
            )
            return ProviderCredentialResolution(
                status="malformed",
                provider=provider,
                source=source_label,
                profile_file=path,
                candidate_files=(path.name,),
                message=f"{provider.capitalize()} credential profile is malformed: {safe_exc}",
                remediation=rem,
            )

        # Ambient values override stored values, strictly limited to the provider spec
        norm_env = normalize_provider_aliases(provider, current_env)
        norm_parsed = normalize_provider_aliases(provider, parsed)

        merged: dict[str, str] = {}
        allowed_keys = set(required_vars) | set(optional_vars)
        for key in allowed_keys:
            amb = str(norm_env.get(key) or "").strip()
            if amb:
                merged[key] = amb
            elif key in norm_parsed:
                val = norm_parsed[key].strip()
                if val:
                    merged[key] = val

        # Validate required variables
        for var in required_vars:
            val = merged.get(var, "")
            if not val:
                return ProviderCredentialResolution(
                    status="malformed",
                    provider=provider,
                    source=source_label,
                    profile_file=path,
                    candidate_files=(path.name,),
                    missing_variables=(var,),
                    message=f"{provider.capitalize()} credential profile does not define a valid {var}: {path.name}",
                    remediation=f"Add {var} to {display_profile_path(path, resolved_config_dir)} or set it in the environment.",
                )
            validator = validators.get(var)
            if validator and not validator(val):
                return ProviderCredentialResolution(
                    status="malformed",
                    provider=provider,
                    source=source_label,
                    profile_file=path,
                    candidate_files=(path.name,),
                    missing_variables=(var,),
                    message=f"{provider.capitalize()} credential profile does not define a valid {var}: {path.name}",
                    remediation=f"Set a valid {var} in {display_profile_path(path, resolved_config_dir)} or in the environment.",
                )

        return ProviderCredentialResolution(
            status="ok",
            provider=provider,
            source=source_label,
            profile_file=path,
            candidate_files=(path.name,),
            credentials=merged,
            message=f"{provider.capitalize()} credentials resolved from {path.name}",
        )

    # 2a. Explicit credential file selector
    if selected_file is not None:
        return _load_single_file(selected_file, "credential_file")

    # 2b. Explicit profile selector
    if selected_profile:
        matches = resolve_explicit_profile(provider, selected_profile, resolved_config_dir)
        if not matches:
            return ProviderCredentialResolution(
                status="missing",
                provider=provider,
                source="profile",
                message=f"provider credential profile '{selected_profile}' not found in {display_profile_path(resolved_config_dir)}",
                remediation=f"Create {display_profile_path(resolved_config_dir / f'{selected_profile}.env', resolved_config_dir)} (chmod 600).",
            )
        if len(matches) > 1:
            return ProviderCredentialResolution(
                status="ambiguous",
                provider=provider,
                source="profile",
                candidate_files=_safe_file_names(matches),
                message=f"multiple credential profiles match '{selected_profile}'",
                remediation="Specify an exact profile filename or remove duplicate profiles.",
            )
        return _load_single_file(matches[0], "profile")

    # 3. Safe automatic discovery in Code Mower's config directory
    candidates = discover_matching_profiles(provider, resolved_config_dir)
    if not candidates:
        missing_vars = [
            var
            for var in required_vars
            if not current_env.get(var)
            or (validators.get(var) and not validators[var](str(current_env.get(var)).strip()))
        ]
        missing_name = (
            missing_vars[0]
            if missing_vars
            else (required_vars[0] if required_vars else f"{prov_upper}_API_KEY")
        )
        return ProviderCredentialResolution(
            status="missing",
            provider=provider,
            source="missing",
            missing_variables=tuple(missing_vars) if missing_vars else required_vars,
            message=f"{provider.capitalize()} API credentials missing",
            remediation=(
                f"Set {missing_name} in environment, or store it in "
                f"~/.config/code-mower/{provider}.env (chmod 600)."
            ),
        )

    if len(candidates) > 1:
        candidates_str = ", ".join(_safe_file_names(candidates))
        return ProviderCredentialResolution(
            status="ambiguous",
            provider=provider,
            source="single_profile",
            candidate_files=_safe_file_names(candidates),
            message=f"multiple {provider.capitalize()} credential profiles found ({candidates_str}); no profile selected",
            remediation=(
                f"Specify an explicit profile with --provider-profile or ensure "
                f"only one matching profile exists in ~/.config/code-mower. Candidates: {candidates_str}"
            ),
        )

    return _load_single_file(candidates[0], "single_profile")
