#!/usr/bin/env python3
"""Render the dogfood runner from code-mower.yml and the maintained template."""
from __future__ import annotations

import argparse
from pathlib import Path

from code_mower import config, init

ROOT = Path(__file__).resolve().parents[1]


def rendered_runner(root: Path = ROOT) -> str:
    plan = init.render_init_plan(config.load_config(root / "code-mower.yml"), package_mode=True)
    entry = next(item for item in plan.data["generated_files"] if item["path"] == init.LANE_MAC_RUNNER_SCRIPT_PATH)
    template = (root / init.LANE_MAC_RUNNER_SCRIPT_TEMPLATE).read_text(encoding="utf-8")
    return init._render_workflow_template(template, entry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / init.LANE_MAC_RUNNER_SCRIPT_PATH
    text = rendered_runner()
    if args.check:
        if path.read_text(encoding="utf-8") != text:
            print("runner drift: run scripts/sync_lane_runner.py")
            return 1
    else:
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
