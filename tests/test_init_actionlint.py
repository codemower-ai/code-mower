from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from code_mower import init as code_mower_init
from code_mower.config import ConfigError, RenderedPlan


def _plan_for_workflow(path: str = ".github/workflows/generated.yml") -> RenderedPlan:
    return RenderedPlan(
        text="",
        data={
            "generated_files": [
                {
                    "path": path,
                    "source": "test-workflow",
                    "copy_from": "workflow.yml",
                }
            ],
            "labels": [],
            "required_secrets": [],
            "required_variables": [],
            "smoke_tests": [],
        },
    )


def _write_actionlint(path: Path, exit_code: int) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("fake actionlint\\n")
            raise SystemExit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


class InitActionlintTests(unittest.TestCase):
    def test_init_apply_refuses_to_replace_output_when_actionlint_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            source.joinpath("workflow.yml").write_text(
                "name: bad\non: push\njobs:\n  bad:\n    runs-on: ubuntu-latest\n",
                encoding="utf-8",
            )
            output = root / "generated"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("old output\n", encoding="utf-8")
            actionlint = root / "actionlint"
            _write_actionlint(actionlint, 1)

            with self.assertRaisesRegex(ConfigError, "actionlint failed"):
                code_mower_init.apply_init_plan(
                    _plan_for_workflow(),
                    output,
                    source_root=source,
                    actionlint_bin=str(actionlint),
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "old output\n")
            self.assertFalse(output.joinpath(".github/workflows/generated.yml").exists())

    def test_init_apply_records_actionlint_evidence_when_lint_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            source.joinpath("workflow.yml").write_text(
                "name: good\non: push\njobs:\n  good:\n    runs-on: ubuntu-latest\n",
                encoding="utf-8",
            )
            actionlint = root / "actionlint"
            _write_actionlint(actionlint, 0)

            result = code_mower_init.apply_init_plan(
                _plan_for_workflow(),
                root / "generated",
                source_root=source,
                actionlint_bin=str(actionlint),
            )

        self.assertEqual(result["actionlint"]["workflow_count"], 1)
        self.assertEqual(
            result["actionlint"]["workflows"],
            [".github/workflows/generated.yml"],
        )


if __name__ == "__main__":
    unittest.main()
