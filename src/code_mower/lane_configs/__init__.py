"""Lane configuration package for trailer-comment audit labelers."""

from __future__ import annotations

from importlib import import_module

if __package__ and __package__.startswith("code_mower."):
    from ..audit_labeler_lib import LaneConfig
else:
    try:
        from tools.audit_labeler_lib import LaneConfig
    except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
        from audit_labeler_lib import LaneConfig


def load_lane_config(name: str) -> "LaneConfig":
    normalized = name.replace("-", "_")
    module_roots = []
    if __package__:
        module_roots.append(__package__)
    module_roots.extend(["tools.lane_configs", "lane_configs"])
    seen_roots: set[str] = set()
    last_missing: ModuleNotFoundError | None = None
    for root in module_roots:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        module_name = f"{root}.{normalized}"
        try:
            module = import_module(module_name)
            return module.CONFIG
        except ModuleNotFoundError as exc:
            if exc.name == module_name or exc.name == root.split(".", 1)[0]:
                last_missing = exc
                continue
            raise
    raise ValueError(f"unknown trailer-comment audit lane: {name}") from last_missing
