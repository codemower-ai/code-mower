"""Repository branch-name policy for builder delivery.

A repository's accepted branch names and a builder's provenance are distinct
contracts. ``builder_identity.branch_prefixes`` maps observed prefixes to lanes
for provenance inference; ``repositories[].delivery_policy.branch_template``
says which branch a builder may open for a work item. When a repository policy
cannot encode the provider (``fix/…``), provenance stays with ``builder:<lane>``
labels, the authenticated PR author, PR markers, and builder-run sidecars.

Only the documented template variables are accepted. Resolution and validation
use repository slug, issue key, work type, lane, and branch-name metadata only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_TEMPLATE = "{lane}/{issue_key}-{slug}"
TEMPLATE_VARIABLES: Mapping[str, str] = {
    "lane": r"[a-z0-9][a-z0-9-]*",
    "issue_key": r"[A-Za-z0-9][A-Za-z0-9_-]*",
    "issue_number": r"[0-9]+",
    "slug": r"[a-z0-9][a-z0-9-]*",
    "work_type": r"[a-z][a-z0-9-]*",
    "repo_name": r"[A-Za-z0-9._-]+",
}
EXAMPLE_VALUES: Mapping[str, str] = {
    "lane": "codex",
    "issue_key": "ABC-123",
    "issue_number": "123",
    "slug": "short-description",
    "work_type": "fix",
    "repo_name": "repo",
}
MAX_BRANCH_LENGTH = 200
MAX_SLUG_LENGTH = 48
_VARIABLE_RE = re.compile(r"\{([^{}]*)\}")
_OPTIONAL_SLUG_RE = re.compile(r"([-_/.])\{slug\}")


class BranchPolicyError(ValueError):
    """A template, variable, or proposed branch violates the delivery policy."""


@dataclass(frozen=True)
class BranchPolicy:
    template: str
    pattern: str
    example: str
    configured: bool

    def describe(self) -> dict[str, str]:
        return {"template": self.template, "pattern": self.pattern, "example": self.example}


def is_valid_ref(branch: Any) -> bool:
    """A conservative subset of git-check-ref-format for branch names."""
    return (isinstance(branch, str) and 0 < len(branch) <= MAX_BRANCH_LENGTH
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.-]*", branch) is not None
            and not any(x in branch for x in ("..", "//", "@{"))
            and all(not p.startswith(".") and not p.endswith((".", ".lock"))
                    for p in branch.split("/")) and not branch.endswith("/"))


def slugify(text: Any, *, limit: int = MAX_SLUG_LENGTH) -> str:
    """Lowercase ASCII words joined by ``-``; empty when nothing survives."""
    if not isinstance(text, str):
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:limit].rstrip("-")
    return slug


def _variables(template: str) -> list[str]:
    names = _VARIABLE_RE.findall(template)
    unknown = [name for name in names if name not in TEMPLATE_VARIABLES]
    if unknown:
        raise BranchPolicyError(
            f"unknown template variable {{{unknown[0]}}}; allowed variables are "
            + ", ".join(f"{{{name}}}" for name in TEMPLATE_VARIABLES)
        )
    return names


def compile_template(template: Any, *, configured: bool = True) -> BranchPolicy:
    """Validate a template and derive its accepted pattern and one example."""
    if not isinstance(template, str) or not template.strip():
        raise BranchPolicyError("branch_template must be a non-empty string")
    if template != template.strip() or "{" in _VARIABLE_RE.sub("", template) \
            or "}" in _VARIABLE_RE.sub("", template):
        raise BranchPolicyError("branch_template has unbalanced braces or surrounding whitespace")
    names = _variables(template)
    if "issue_key" not in names and "issue_number" not in names:
        raise BranchPolicyError(
            "branch_template must include {issue_key} or {issue_number} so each work item "
            "resolves to its own branch"
        )
    pattern = _pattern(template)
    example = render(template, EXAMPLE_VALUES)
    if not is_valid_ref(example) or re.fullmatch(pattern, example) is None:
        raise BranchPolicyError(
            f"branch_template {template!r} does not render to a valid git branch name"
        )
    return BranchPolicy(template, pattern, example, configured)


def _pattern(template: str) -> str:
    parts: list[str] = []
    index = 0
    for match in _VARIABLE_RE.finditer(template):
        literal = template[index:match.start()]
        name = match.group(1)
        if name == "slug" and literal and literal[-1] in "-_/.":
            parts.append(re.escape(literal[:-1]))
            parts.append(f"(?:{re.escape(literal[-1])}{TEMPLATE_VARIABLES[name]})?")
        else:
            parts.append(re.escape(literal))
            parts.append(TEMPLATE_VARIABLES[name])
        index = match.end()
    parts.append(re.escape(template[index:]))
    return "".join(parts)


def render(template: str, values: Mapping[str, str]) -> str:
    """Substitute variables; an empty slug drops itself and its leading separator."""
    if not values.get("slug"):
        template = _OPTIONAL_SLUG_RE.sub("", template)
    return _VARIABLE_RE.sub(lambda m: values.get(m.group(1), ""), template)


def default_policy() -> BranchPolicy:
    """The provider-prefix convention used when no delivery policy is configured."""
    return compile_template(DEFAULT_TEMPLATE, configured=False)


def policy_for_repository(config: Mapping[str, Any], repository: str) -> BranchPolicy:
    """The delivery policy of ``repository`` in ``config``, or the default."""
    for repo in config.get("repositories") or ():
        if not isinstance(repo, Mapping):
            continue
        slug = repo.get("slug")
        if not isinstance(slug, str) or slug.lower() != repository.lower():
            continue
        delivery = repo.get("delivery_policy")
        if isinstance(delivery, Mapping) and delivery.get("branch_template") is not None:
            return compile_template(delivery["branch_template"])
        break
    return default_policy()


def policies_by_repository(config: Mapping[str, Any]) -> dict[str, BranchPolicy]:
    """Lower-cased repository slug to its configured policy.

    Lookup is case-insensitive, so two repository entries that differ only by
    case would otherwise let one policy silently shadow the other; that is an
    error here rather than a lookup-order accident.
    """
    policies: dict[str, BranchPolicy] = {}
    seen: set[str] = set()
    for repo in config.get("repositories") or ():
        if not isinstance(repo, Mapping) or not isinstance(repo.get("slug"), str):
            continue
        slug = repo["slug"].lower()
        if slug in seen:
            raise BranchPolicyError(f"duplicate repository {repo['slug']!r} (slugs compare case-insensitively)")
        seen.add(slug)
        delivery = repo.get("delivery_policy")
        if isinstance(delivery, Mapping) and delivery.get("branch_template") is not None:
            policies[slug] = compile_template(delivery["branch_template"])
    return policies


def configured_policies(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Lower-cased repository slug to pattern/example for configured policies only."""
    return {slug: policy.describe() for slug, policy in policies_by_repository(config).items()}


def resolve_branch(policy: BranchPolicy, *, lane: str, issue_number: Any = None,
                   issue_key: str = "", slug: str = "", work_type: str = "fix",
                   repository: str = "") -> str:
    """Render the branch for one work item and validate it against ``policy``.

    ``{issue_key}`` is the tracker key when bound, otherwise the GitHub issue
    number; a template that needs either fails when both are missing.
    """
    number = "" if issue_number is None else str(issue_number)
    if issue_key and not re.fullmatch(TEMPLATE_VARIABLES["issue_key"], issue_key):
        raise BranchPolicyError("issue_key must be alphanumeric with - or _ separators")
    if number and not number.isdigit():
        raise BranchPolicyError("issue_number must be a positive integer")
    key = issue_key or number
    names = _variables(policy.template)
    if ("issue_key" in names and not key) or ("issue_number" in names and not number):
        raise BranchPolicyError(
            f"branch_template {policy.template!r} needs an issue key or number, and the "
            "work item has neither"
        )
    values = {
        "lane": slugify(lane),
        "issue_key": key,
        "issue_number": number,
        "slug": slugify(slug),
        "work_type": slugify(work_type),
        "repo_name": repository.rsplit("/", 1)[-1] if repository else "",
    }
    for name in names:
        if name not in ("slug", "issue_key", "issue_number") and not values[name]:
            raise BranchPolicyError(f"branch_template needs {{{name}}}, which is unavailable")
    branch = render(policy.template, values)
    validate_branch(policy, branch)
    return branch


def validate_branch(policy: BranchPolicy, branch: Any) -> str:
    """Reject a proposed branch before any create, push, or PR open."""
    if not is_valid_ref(branch):
        raise BranchPolicyError(
            f"proposed branch {branch!r} is not a valid git branch name; "
            f"expected one matching {policy.pattern} (for example {policy.example})"
        )
    if re.fullmatch(policy.pattern, branch) is None:
        source = "repository delivery_policy.branch_template" if policy.configured \
            else "the provider-prefix convention"
        raise BranchPolicyError(
            f"proposed branch {branch!r} does not match {source} {policy.template!r}; "
            f"expected {policy.pattern} (for example {policy.example})"
        )
    return branch
