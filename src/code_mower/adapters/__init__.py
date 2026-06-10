"""SaaS reviewer adapter package."""

from __future__ import annotations

from importlib import import_module

try:
    from ._base import SaaSReviewerAdapter
except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
    try:
        from tools.adapters._base import SaaSReviewerAdapter
    except ImportError:
        from adapters._base import SaaSReviewerAdapter


def load_adapter(name: str) -> SaaSReviewerAdapter:
    normalized = name.replace("-", "_")
    if normalized not in {"cursor_bugbot", "greptile", "gitar", "qodo"}:
        raise ValueError(f"unknown SaaS reviewer adapter: {name}")
    module_roots = []
    if __package__:
        module_roots.append(__package__)
    module_roots.extend(["tools.adapters", "adapters"])
    seen_roots: set[str] = set()
    last_missing: ModuleNotFoundError | None = None
    for root in module_roots:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        module_name = f"{root}.{normalized}"
        try:
            module = import_module(module_name)
            return module.ADAPTER
        except ModuleNotFoundError as exc:
            if exc.name == module_name or exc.name == root.split(".", 1)[0]:
                last_missing = exc
                continue
            raise
    raise ValueError(f"unknown SaaS reviewer adapter: {name}") from last_missing
