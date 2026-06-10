#!/usr/bin/env python3
"""Validate and render a dry-run plan for a Code Mower config.

This is intentionally dependency-free. It supports the documented
`code-mower.example.yml` subset so the reference repos can prove the
configuration contract before the OSS package chooses a full YAML dependency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


ALLOWED_LANE_TYPES = {"audit", "review"}
ALLOWED_DRIVERS = {"local_cli", "manual", "hosted_bridge", "saas_event", "api_model"}
ALLOWED_EVENTS = {"issue_comment", "pull_request_review", "check_run"}
ALLOWED_SPEND_POLICIES = {"none", "local", "included", "paid"}
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_WORKFLOW_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.ya?ml$")
GENERATED_WORKFLOW_BY_DRIVER = {
    "api_model": "trailer-comment labeler + API/model runner",
    "hosted_bridge": "hosted bridge + trailer-comment labeler",
    "local_cli": "local CLI runner + trailer-comment labeler",
    "manual": "manual review state machine",
    "saas_event": "SaaS reviewer event labeler",
}


class ConfigError(ValueError):
    """Raised when the config file cannot be parsed."""


@dataclass(frozen=True)
class ConfigIssue:
    path: str
    message: str


@dataclass(frozen=True)
class RenderedPlan:
    text: str
    data: Mapping[str, Any]


def _strip_inline_comment(raw: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(raw):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or raw[index - 1].isspace():
                return raw[:index].rstrip()
    return raw.rstrip()


def _strip_comments_and_blank_lines(text: str) -> list[tuple[int, str, int]]:
    lines: list[tuple[int, str, int]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        raw = _strip_inline_comment(raw)
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if raw[:indent].replace(" ", ""):
            raise ConfigError(f"line {line_no}: indentation must use spaces")
        lines.append((indent, raw.strip(), line_no))
    return lines


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if value == "{}":
        return {}
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inside = value[1:-1].strip()
        if not inside:
            return []
        return [_parse_scalar(part.strip()) for part in _split_inline_list(inside)]
    return value


def _split_inline_list(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    bracket_depth = 0
    for char in value:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == "[":
                bracket_depth += 1
            elif char == "]" and bracket_depth:
                bracket_depth -= 1
            elif char == "," and bracket_depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(char)
    parts.append("".join(current).strip())
    return parts


def _mapping_separator_index(text: str) -> int | None:
    in_single = False
    in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            if index == len(text) - 1 or text[index + 1].isspace():
                return index
    return None


def _split_key_value(text: str, line_no: int) -> tuple[str, str | None]:
    separator = _mapping_separator_index(text)
    if separator is None:
        raise ConfigError(f"line {line_no}: expected key/value pair")
    key = text[:separator]
    value = text[separator + 1 :]
    key = key.strip()
    if not key:
        raise ConfigError(f"line {line_no}: empty key")
    value = value.strip()
    return key, value or None


class _YamlSubsetParser:
    def __init__(self, text: str) -> None:
        self.lines = _strip_comments_and_blank_lines(text)
        self.index = 0

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value = self._parse_block(self.lines[0][0])
        if self.index != len(self.lines):
            _, _, line_no = self.lines[self.index]
            raise ConfigError(f"line {line_no}: unexpected trailing content")
        return value

    def _parse_block(self, indent: int) -> Any:
        if self.index >= len(self.lines):
            return {}
        current_indent, text, line_no = self.lines[self.index]
        if current_indent != indent:
            raise ConfigError(
                f"line {line_no}: expected indent {indent}, got {current_indent}"
            )
        if text.startswith("- "):
            return self._parse_list(indent)
        return self._parse_mapping(indent)

    def _parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.index < len(self.lines):
            current_indent, text, line_no = self.lines[self.index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ConfigError(f"line {line_no}: unexpected nested mapping")
            if text.startswith("- "):
                break

            key, value = _split_key_value(text, line_no)
            self.index += 1
            if value is not None:
                result[key] = _parse_scalar(value)
                continue

            if self.index >= len(self.lines):
                result[key] = {}
                continue
            next_indent, _, next_line_no = self.lines[self.index]
            if next_indent <= indent:
                result[key] = {}
                continue
            if next_indent != indent + 2:
                raise ConfigError(
                    f"line {next_line_no}: expected indent {indent + 2}, got {next_indent}"
                )
            result[key] = self._parse_block(next_indent)
        return result

    def _parse_list(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.index < len(self.lines):
            current_indent, text, line_no = self.lines[self.index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ConfigError(f"line {line_no}: unexpected nested list item")
            if not text.startswith("- "):
                break

            item_text = text[2:].strip()
            self.index += 1
            if not item_text:
                if self.index >= len(self.lines):
                    items.append({})
                    continue
                next_indent, _, next_line_no = self.lines[self.index]
                if next_indent <= indent:
                    items.append({})
                    continue
                if next_indent != indent + 2:
                    raise ConfigError(
                        f"line {next_line_no}: expected indent {indent + 2}, got {next_indent}"
                    )
                items.append(self._parse_block(next_indent))
                continue

            if _mapping_separator_index(item_text) is not None:
                key, value = _split_key_value(item_text, line_no)
                item: dict[str, Any] = {}
                if value is not None:
                    item[key] = _parse_scalar(value)
                elif self.index < len(self.lines) and self.lines[self.index][0] > indent + 2:
                    item[key] = self._parse_block(self.lines[self.index][0])
                else:
                    item[key] = {}
                if self.index < len(self.lines):
                    next_indent, next_text, next_line_no = self.lines[self.index]
                    if next_indent == indent + 2 and not next_text.startswith("- "):
                        item.update(self._parse_mapping(next_indent))
                    elif next_indent > indent + 2:
                        raise ConfigError(
                            f"line {next_line_no}: expected indent {indent + 2}, got {next_indent}"
                        )
                items.append(item)
                continue

            items.append(_parse_scalar(item_text))
        return items


def load_config(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"unable to read {path}: {exc}") from exc
    parsed = _YamlSubsetParser(text).parse()
    if not isinstance(parsed, Mapping):
        raise ConfigError("top-level config must be a mapping")
    return parsed


def _as_mapping(value: Any, path: str, issues: list[ConfigIssue]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    issues.append(ConfigIssue(path, "must be a mapping"))
    return {}


def _as_sequence(value: Any, path: str, issues: list[ConfigIssue]) -> tuple[Any, ...]:
    if isinstance(value, list):
        return tuple(value)
    issues.append(ConfigIssue(path, "must be a list"))
    return ()


def _require_string(value: Any, path: str, issues: list[ConfigIssue]) -> str | None:
    if isinstance(value, str) and value:
        return value
    issues.append(ConfigIssue(path, "must be a non-empty string"))
    return None


def _require_identifier(value: Any, path: str, issues: list[ConfigIssue]) -> str | None:
    text = _require_string(value, path, issues)
    if text is None:
        return None
    if not SAFE_IDENTIFIER_RE.fullmatch(text):
        issues.append(
            ConfigIssue(
                path,
                "must match [A-Za-z0-9][A-Za-z0-9_-]* for generated file safety",
            )
        )
        return None
    return text


def _require_workflow_path(value: Any, path: str, issues: list[ConfigIssue]) -> str | None:
    text = _require_string(value, path, issues)
    if text is None:
        return None
    parts = text.split("/")
    valid = (
        len(parts) == 3
        and parts[0] == ".github"
        and parts[1] == "workflows"
        and "\\" not in text
        and all(part not in {"", ".", ".."} for part in parts)
        and SAFE_WORKFLOW_FILENAME_RE.fullmatch(parts[2]) is not None
    )
    if not valid:
        issues.append(
            ConfigIssue(
                path,
                "must be a safe direct .github/workflows/*.yml path",
            )
        )
        return None
    return text


def _token_env_any_groups(lane: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    raw_groups = lane.get("token_env_any", []) or []
    if not isinstance(raw_groups, list):
        return ()
    for group in raw_groups:
        if not isinstance(group, list):
            continue
        names = tuple(str(name) for name in group if isinstance(name, str) and name)
        if names:
            groups.append(names)
    return tuple(groups)


def required_secret_entries_for_lane(lane: Mapping[str, Any]) -> tuple[str, ...]:
    secrets: list[str] = []
    seen: set[str] = set()

    def add(secret: str) -> None:
        if not secret or secret == "GITHUB_TOKEN" or secret in seen:
            return
        seen.add(secret)
        secrets.append(secret)

    for token in lane.get("token_env", []) or []:
        if isinstance(token, str):
            add(token)
    for group in _token_env_any_groups(lane):
        alternatives = [token for token in group if token != "GITHUB_TOKEN"]
        if alternatives:
            add(" or ".join(alternatives))
    review_hygiene = lane.get("review_hygiene")
    if isinstance(review_hygiene, Mapping):
        token = review_hygiene.get("token_env")
        if isinstance(token, str):
            add(token)
    return tuple(secrets)


def validate_config(config: Mapping[str, Any]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    if config.get("version") not in {1, "1"}:
        issues.append(ConfigIssue("version", "must be 1"))

    project = _as_mapping(config.get("project"), "project", issues)
    _require_string(project.get("name"), "project.name", issues)
    _require_string(project.get("state_dir"), "project.state_dir", issues)

    seen_repos: set[str] = set()
    for index, repo in enumerate(_as_sequence(config.get("repositories"), "repositories", issues)):
        path = f"repositories[{index}]"
        repo_map = _as_mapping(repo, path, issues)
        slug = _require_string(repo_map.get("slug"), f"{path}.slug", issues)
        _require_string(repo_map.get("default_branch"), f"{path}.default_branch", issues)
        if slug and slug in seen_repos:
            issues.append(ConfigIssue(f"{path}.slug", f"duplicate repository {slug}"))
        if slug:
            seen_repos.add(slug)

    lanes = _as_mapping(config.get("lanes"), "lanes", issues)
    all_labels: dict[str, str] = {}
    for lane_id, lane in lanes.items():
        path = f"lanes.{lane_id}"
        _require_identifier(lane_id, path, issues)
        lane_map = _as_mapping(lane, path, issues)
        lane_type = lane_map.get("type")
        driver = lane_map.get("driver")
        if lane_type not in ALLOWED_LANE_TYPES:
            issues.append(ConfigIssue(f"{path}.type", f"must be one of {sorted(ALLOWED_LANE_TYPES)}"))
        if driver not in ALLOWED_DRIVERS:
            issues.append(ConfigIssue(f"{path}.driver", f"must be one of {sorted(ALLOWED_DRIVERS)}"))
        _require_string(lane_map.get("provider"), f"{path}.provider", issues)
        if lane_map.get("trailer_lane") is not None:
            _require_identifier(lane_map.get("trailer_lane"), f"{path}.trailer_lane", issues)
        if lane_map.get("lane_config") is not None:
            _require_identifier(lane_map.get("lane_config"), f"{path}.lane_config", issues)

        labels = _as_mapping(lane_map.get("labels"), f"{path}.labels", issues)
        for label_key in ("needs", "done", "blocked"):
            label = _require_string(labels.get(label_key), f"{path}.labels.{label_key}", issues)
            if not label:
                continue
            owner = all_labels.setdefault(label, f"{lane_id}.{label_key}")
            if owner != f"{lane_id}.{label_key}":
                issues.append(ConfigIssue(f"{path}.labels.{label_key}", f"duplicates {owner}"))

        if driver == "saas_event":
            _require_identifier(lane_map.get("adapter"), f"{path}.adapter", issues)
            events = lane_map.get("events")
            if not events:
                issues.append(ConfigIssue(f"{path}.events", "saas_event lanes require events"))
            else:
                for event in _as_sequence(events, f"{path}.events", issues):
                    if event not in ALLOWED_EVENTS:
                        issues.append(ConfigIssue(f"{path}.events", f"unsupported event {event!r}"))

        if lane_map.get("informational") and lane_map.get("merge_authority"):
            issues.append(
                ConfigIssue(path, "informational lanes must not also have merge_authority")
            )

        spend_policy = lane_map.get("spend_policy")
        if spend_policy is not None and (
            not isinstance(spend_policy, str)
            or spend_policy not in ALLOWED_SPEND_POLICIES
        ):
            issues.append(
                ConfigIssue(
                    f"{path}.spend_policy",
                    f"must be one of {sorted(ALLOWED_SPEND_POLICIES)}",
                )
            )

        token_env = lane_map.get("token_env")
        if token_env is not None:
            for index, token_name in enumerate(
                _as_sequence(token_env, f"{path}.token_env", issues)
            ):
                if not isinstance(token_name, str) or not token_name:
                    issues.append(
                        ConfigIssue(
                            f"{path}.token_env[{index}]",
                            "must be a non-empty string",
                        )
                    )

        token_env_any = lane_map.get("token_env_any")
        if token_env_any is not None:
            for group_index, token_group in enumerate(
                _as_sequence(token_env_any, f"{path}.token_env_any", issues)
            ):
                group_path = f"{path}.token_env_any[{group_index}]"
                group_values = _as_sequence(token_group, group_path, issues)
                if not group_values:
                    issues.append(ConfigIssue(group_path, "must contain at least one token name"))
                    continue
                for token_index, token_name in enumerate(group_values):
                    if not isinstance(token_name, str) or not token_name:
                        issues.append(
                            ConfigIssue(
                                f"{group_path}[{token_index}]",
                                "must be a non-empty string",
                            )
                        )

        review_hygiene = lane_map.get("review_hygiene")
        if review_hygiene is not None:
            hygiene_map = _as_mapping(review_hygiene, f"{path}.review_hygiene", issues)
            _require_workflow_path(
                hygiene_map.get("workflow"),
                f"{path}.review_hygiene.workflow",
                issues,
            )
            token = hygiene_map.get("token_env")
            if token is not None:
                _require_string(token, f"{path}.review_hygiene.token_env", issues)

    profiles = _as_mapping(config.get("profiles", {}), "profiles", issues)
    for profile_id, profile in profiles.items():
        path = f"profiles.{profile_id}"
        profile_map = _as_mapping(profile, path, issues)
        _require_string(profile_map.get("description"), f"{path}.description", issues)
        for index, lane_id in enumerate(
            _as_sequence(profile_map.get("lanes"), f"{path}.lanes", issues)
        ):
            if not isinstance(lane_id, str) or not lane_id:
                issues.append(
                    ConfigIssue(f"{path}.lanes[{index}]", "must be a non-empty string")
                )
                continue
            if lane_id not in lanes:
                issues.append(ConfigIssue(f"{path}.lanes", f"unknown lane {lane_id!r}"))

    return issues


def _lane_flags(lane: Mapping[str, Any]) -> str:
    flags = []
    if lane.get("merge_authority"):
        flags.append("merge-authority")
    if lane.get("informational"):
        flags.append("informational")
    if lane.get("enabled_by_default") is False:
        flags.append("opt-in")
    trigger_policy = lane.get("trigger_policy")
    if trigger_policy and trigger_policy != "label":
        flags.append(f"trigger={trigger_policy}")
    return ", ".join(flags) if flags else "standard"


def _labels_for(lane: Mapping[str, Any]) -> Mapping[str, str]:
    labels = lane.get("labels")
    return labels if isinstance(labels, Mapping) else {}


def render_dry_run(config: Mapping[str, Any]) -> RenderedPlan:
    issues = validate_config(config)
    if issues:
        issue_text = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        raise ConfigError(f"invalid Code Mower config: {issue_text}")

    project = config["project"]
    repositories = config["repositories"]
    lanes: Mapping[str, Mapping[str, Any]] = config["lanes"]
    profiles: Mapping[str, Mapping[str, Any]] = config.get("profiles", {})

    labels: list[str] = []
    workflows: list[dict[str, str]] = []
    secrets: set[str] = set()
    for lane_id, lane in lanes.items():
        lane_labels = _labels_for(lane)
        labels.extend(str(lane_labels[key]) for key in ("needs", "done", "blocked"))
        secrets.update(required_secret_entries_for_lane(lane))
        workflow_kind = GENERATED_WORKFLOW_BY_DRIVER.get(str(lane["driver"]), "custom")
        workflows.append({"lane": str(lane_id), "kind": workflow_kind})

    plan = {
        "project": project["name"],
        "state_dir": project["state_dir"],
        "repositories": repositories,
        "lanes": {
            lane_id: {
                "driver": lane["driver"],
                "provider": lane["provider"],
                "labels": _labels_for(lane),
                "flags": _lane_flags(lane),
            }
            for lane_id, lane in lanes.items()
        },
        "labels": sorted(set(labels)),
        "required_secrets": sorted(secrets),
        "workflows": workflows,
        "profiles": {
            profile_id: {
                "description": profile.get("description", ""),
                "lanes": profile.get("lanes", []),
            }
            for profile_id, profile in profiles.items()
        },
    }

    lines = [
        "Code Mower dry-run plan",
        f"Project: {project['name']}",
        f"State dir: {project['state_dir']}",
        "",
        "Repositories:",
    ]
    for repo in repositories:
        lines.append(
            f"- {repo['slug']} (default={repo['default_branch']}, local_path_env={repo.get('local_path_env', 'n/a')})"
        )

    lines.extend(["", "Lanes:"])
    for lane_id, lane in lanes.items():
        lane_labels = _labels_for(lane)
        lines.append(
            f"- {lane_id}: {lane['type']}/{lane['driver']} provider={lane['provider']} [{_lane_flags(lane)}]"
        )
        lines.append(
            f"  labels: {lane_labels['needs']} -> {lane_labels['done']} / {lane_labels['blocked']}"
        )
        if lane.get("events"):
            lines.append(f"  events: {', '.join(lane['events'])}")
        if lane.get("adapter"):
            lines.append(f"  adapter: {lane['adapter']}")

    lines.extend(["", "Labels to ensure:"])
    lines.extend(f"- {label}" for label in plan["labels"])

    lines.extend(["", "Workflow/render targets:"])
    for workflow in workflows:
        lines.append(f"- {workflow['lane']}: {workflow['kind']}")

    lines.extend(["", "Required secrets/PAT fallbacks:"])
    if plan["required_secrets"]:
        lines.extend(f"- {secret}" for secret in plan["required_secrets"])
    else:
        lines.append("- none beyond GITHUB_TOKEN")

    if profiles:
        lines.extend(["", "Setup profiles:"])
        for profile_id, profile in profiles.items():
            lane_list = ", ".join(profile.get("lanes", []))
            lines.append(f"- {profile_id}: {lane_list}")
            lines.append(f"  {profile.get('description', '')}")

    return RenderedPlan(text="\n".join(lines) + "\n", data=plan)


def _format_issues(issues: Iterable[ConfigIssue]) -> str:
    return "\n".join(f"- {issue.path}: {issue.message}" for issue in issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="code-mower.example.yml")
    parser.add_argument("--json", action="store_true", help="emit dry-run plan as JSON")
    parser.add_argument("--validate-only", action="store_true", help="only validate the config")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
        issues = validate_config(config)
        if issues:
            print(_format_issues(issues), file=sys.stderr)
            return 1
        if args.validate_only:
            print("Code Mower config OK")
            return 0
        plan = render_dry_run(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(plan.data, indent=2, sort_keys=True))
    else:
        print(plan.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
