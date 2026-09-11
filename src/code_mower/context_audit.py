"""Shared private context lifecycle for independent Claude and Codex reviews."""

from __future__ import annotations

from dataclasses import dataclass, field
import subprocess

from .context_contract import ContextError, normalize_policy
from .context_delivery import Delivery, deliver, save_feedback
from .context_review import latest_input, marker, review_matches
from .context_store import ContextStore


def required_for_repo(repo_path, base_ref):
    """Read only trusted-base policy, including an explicitly absent config."""
    if repo_path is None or not (repo_path / '.git').exists():
        return False  # The caller's ordinary diff path validates its checkout.
    from .config import _YamlSubsetParser
    try:
        listing = subprocess.run(['git', 'ls-tree', '--name-only', base_ref, '--', 'code-mower.yml'],
            cwd=repo_path, capture_output=True, text=True, check=True, timeout=10)
        if not listing.stdout.strip():
            return False
        result = subprocess.run(['git', 'show', base_ref + ':code-mower.yml'], cwd=repo_path,
            capture_output=True, text=True, check=True, timeout=10)
        config = _YamlSubsetParser(result.stdout).parse()
        if not isinstance(config, dict):
            raise ValueError('configuration must be a mapping')
        policy = normalize_policy(config.get('context'))
        return bool(policy and policy['required'])
    except Exception:
        # Optional policy discovery must not make an otherwise ordinary audit
        # unavailable. Explicit revisions and trusted PR declarations still
        # fail closed below; the generated gate independently enforces the
        # repository's required-policy flag when discovery is unavailable.
        return False


@dataclass
class ReviewContext:
    metadata: dict | None
    ready: bool
    delivery: Delivery | None = field(default=None, repr=False)
    store: ContextStore | None = field(default=None, repr=False)
    recipient: str = field(default="", repr=False)
    repository: str = field(default="", repr=False)
    pr: int = 0
    head: str = ""
    authorities: tuple[str, ...] = ()
    fetch_comments: object = field(default=None, repr=False)
    backend: object = field(default=None, repr=False)

    @property
    def text(self):
        return self.delivery.text if self.delivery is not None else ""

    @property
    def private(self):
        return self.delivery is not None

    def finish(self, *, head, prose):
        """Check current input and fresh authorization before accepting a verdict."""
        if not self.ready or head != self.head:
            return False
        try:
            current = latest_input(self.fetch_comments(), authorities=self.authorities)
            if current != self.metadata:
                return False
            if not review_matches(marker(current, review=True), current, head=head):
                return False
            if self.delivery is not None:
                verified = deliver(self.store, current["revision"], repository=self.repository,
                    pr=self.pr, head=head, recipient=self.recipient, current=current, backend=self.backend)
                if verified.text != self.delivery.text:
                    return False
                save_feedback(self.store, verified, self.recipient.split(":")[0], prose)
            return True
        except Exception:
            # No provider text, paths, identity or credential errors escape.
            return False


def prepare(*, repository, pr, head, host, authorities, fetch_comments,
            revision=None, state_dir=None, store=None, backend=None, repo_path=None, base_ref='origin/main'):
    """Discover only declarations from trusted control authorities.

    With no configured authority and no explicit request, context cannot be
    selected. This preserves the ordinary no-context audit path without loading
    an optional SDK or private store.
    """
    state = ReviewContext(metadata=None, ready=False, repository=repository, pr=pr, head=head,
                          recipient=host + ":reviewer", authorities=tuple(authorities),
                          fetch_comments=fetch_comments, backend=backend)
    try:
        required = required_for_repo(repo_path, base_ref)
        if not authorities and revision is None and not required:
            return None
        current = latest_input(fetch_comments(), authorities=authorities)
        if current is None:
            return state if required or revision is not None else None
        state.metadata = current
        if required and not current['required']:
            return state
        if revision is not None and revision != current["revision"]:
            return state
        if current["head"] != head or current["state"] == "required_unavailable":
            return state
        if current["state"] == "optional_unavailable":
            state.ready = review_matches(marker(current, review=True), current, head=head)
            return state
        state.store = store if store is not None else ContextStore(state_dir)
        state.delivery = deliver(state.store, current["revision"], repository=repository,
            pr=pr, head=head, recipient=state.recipient, current=current, backend=backend)
        state.ready = True
    except (ContextError, OSError, ValueError, RuntimeError, TypeError):
        pass
    return state
